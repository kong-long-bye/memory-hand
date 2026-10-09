# -*- coding: utf-8 -*-
"""Memory Service：FastAPI 实现官方 /health /add /search 三个接口。

启动：
    uvicorn app:app --host 0.0.0.0 --port 8000
"""
from fastapi import FastAPI
from pydantic import BaseModel, Field

from memory_engine import MemoryStore


app = FastAPI(title="Agent Memory Service", version="0.1.0")
store = MemoryStore() # 存储结构

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

# 路由
# 健康检查
@app.get("/health")
def health():
    return {"status": "ok"}

# 添加记忆
@app.post("/add", response_model=AddResponse)
def add_memory(req: AddRequest):
    store.add(
        request_id=req.request_id,
        messages=[m.model_dump(exclude_none=True) for m in req.messages],# 将模型对象转换为字典，并排除 None 值
        user_id=req.user_id,
        session_id=req.session_id,
    )
    # 返回成功添加记忆
    return AddResponse(
        success=True,
        request_id=req.request_id,
        user_id=req.user_id,
        session_id=req.session_id)

# 搜索记忆
@app.post("/search", response_model=SearchResponse)
def search_memory(req: SearchRequest):
    results = store.search(
        query=req.query,
        
        user_id=req.user_id,
        top_k=req.top_k,
    )
    return SearchResponse(data=results)
    

