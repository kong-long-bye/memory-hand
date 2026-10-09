# Memory Hand — 生产镜像
# 本地构建：docker build -t memory-hand:v0.1.0 .
# 导出离线包：docker save memory-hand:v0.1.0 | gzip > dist/memory-hand-v0.1.0.tar.gz

FROM python:3.12-slim

# 时区 + 基础工具（curl 用于健康检查）
ENV TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖，最大化利用构建缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir jieba

# 再拷贝源码
COPY app.py memory_engine.py dataset_adapter.py evaluate.py llm_clinet.py ./

# data_store 挂卷；容器内保留目录
RUN mkdir -p /app/data_store

EXPOSE 8080

# 生产启动：单 worker 保证 BM25 索引 + 幂等 set 只在同一进程内共享；
# 如需横向扩展，先把 MemoryStore 换成 Redis/DB 后端再调 workers>1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
