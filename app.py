# -*- coding: utf-8 -*-
"""Memory Service：FastAPI 实现官方 /health /add /search 三个接口。

启动：
    uvicorn app:app --host 0.0.0.0 --port 7878

每个请求在 data_store/service.log 留一行 key=value 事件（见 service_log），
只记规模与 ID，不记记忆正文。
"""
import sys
import time

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from memory_engine import MemoryStore
from service_log import brief, get_logger


app = FastAPI(title="Agent Memory Service", version="0.1.0")
store = MemoryStore() # 存储结构

# 日志落在挂载卷里，容器重启和 docker logs 轮转之后仍然可读
LOG_FILE = store.path.parent / "service.log"
logger = get_logger(LOG_FILE.parent)

_boot = store.stats()
logger.info(
    "event=boot version=%s python=%s store_path=%s total_memories=%s "
    "total_chunks=%s total_users=%s log_file=%s",
    app.version, sys.version.split()[0], _boot["store_path"],
    _boot["total_memories"], _boot["total_chunks"], _boot["total_users"],
    LOG_FILE)

# api数据模型，和官方的保持一直

class Message(BaseModel):
    role: str
    content: str
    timestamp: int | None = None

#add request
class AddRequest(BaseModel):
    request_id: str
    messages: list[Message]
    user_id: str
    session_id: str

# add response
class AddResponse(BaseModel):
    success: bool
    request_id: str
    user_id: str
    session_id: str

# search request
class SearchRequest(BaseModel):
    query: str
    options: list[str] | None = None
    user_id: str
    top_k: int = Field(default=100, ge=1, le=1000)

# search response
class SearchHit(BaseModel):
    id: str
    content: object
    score: float | None = None
    created_at: str | None = None


class SearchResponse(BaseModel):
    data: list[SearchHit]

# 中间件
@app.middleware("http")
async def log_request(request: Request, call_next):
    """每个进来的请求都记状态码与耗时，含 /health、HEAD 探测和 4xx。"""
    t0 = time.perf_counter()
    client = request.client.host if request.client else "-"
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001
        # 只标记入口，traceback 由 error_500 记一次，避免重复
        logger.error("event=route method=%s path=%s client=%s exc=%s ms=%.1f",
                     request.method, request.url.path, client,
                     type(exc).__name__, (time.perf_counter() - t0) * 1000)
        raise
    logger.info("event=route method=%s path=%s client=%s status=%s ms=%.1f",
                request.method, request.url.path, client, response.status_code,
                (time.perf_counter() - t0) * 1000)
    return response

# 路由
# 健康检查
@app.get("/health")
def health():
    return {"status": "ok"}

# 添加记忆
@app.post("/add", response_model=AddResponse)
def add_memory(req: AddRequest):
    t0 = time.perf_counter()
    written = store.add(
        request_id=req.request_id,
        messages=[m.model_dump(exclude_none=True) for m in req.messages],# 将模型对象转换为字典，并排除 None 值
        user_id=req.user_id,
        session_id=req.session_id,
    )
    # 下面两个字典只读取用于诊断，不参与检索
    logger.info(
        "event=add request_id=%s user_id=%s session_id=%s msgs=%s content_chars=%s "
        "written=%s user_pool=%s ms=%.1f",
        req.request_id, req.user_id, req.session_id, len(req.messages),
        sum(len(m.content) for m in req.messages), written,
        len(store._by_user.get(req.user_id, ())),
        (time.perf_counter() - t0) * 1000)
    # 返回成功添加记忆
    return AddResponse(
        success=True,
        request_id=req.request_id,
        user_id=req.user_id,
        session_id=req.session_id)

# 搜索记忆
@app.post("/search", response_model=SearchResponse)
def search_memory(req: SearchRequest):
    t0 = time.perf_counter()
    # 下面两个字典只读取用于诊断：调用前没有该 user 的索引，本次就会现建
    user_pool = len(store._by_user.get(req.user_id, ()))
    cold_index = user_pool > 0 and req.user_id not in store._index_by_user
    results = store.search(
        query=req.query,
        
        user_id=req.user_id,
        top_k=req.top_k,
    )
    scores = [r["score"] for r in results if r["score"] is not None]
    logger.info(
        "event=search user_id=%s user_pool=%s top_k=%s query_chars=%s "
        "query=\"%s\" cold_index=%s hits=%s resp_chars=%s score_hi=%s "
        "score_lo=%s ms=%.1f",
        req.user_id, user_pool, req.top_k, len(req.query), brief(req.query),
        cold_index, len(results),
        sum(len(str(r["content"])) for r in results),
        round(max(scores), 3) if scores else "-",
        round(min(scores), 3) if scores else "-",
        (time.perf_counter() - t0) * 1000)
    return SearchResponse(data=results)


# 异常与非法请求：没有这两段，5xx 只在 uvicorn 输出里，422 只剩一个状态码
@app.exception_handler(RequestValidationError)
async def log_validation_error(request: Request,
                               exc: RequestValidationError):
    body = ""
    try:
        body = (await request.body())[:2000].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass  # 读不到 body 也要留下拒绝记录
    logger.error("event=reject_422 method=%s path=%s errors=%s body=\"%s\"",
                 request.method, request.url.path,
                 brief(jsonable_encoder(exc.errors()), 500), brief(body, 500))
    # 与 FastAPI 默认响应保持一致，只加日志
    return JSONResponse(status_code=exc.status_code,
                        content={"detail": jsonable_encoder(exc.errors())})


@app.exception_handler(Exception)
def log_unhandled_error(request: Request, exc: Exception):
    logger.exception("event=error_500 method=%s path=%s err=%s",
                     request.method, request.url.path, exc)
    # Starlette 默认未捕获异常就是这句纯文本，保持不变
    return PlainTextResponse("Internal Server Error", status_code=500)
    

