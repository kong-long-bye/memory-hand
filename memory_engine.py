# -*- coding: utf-8 -*-
"""Memory Engine V1：JSONL 持久化存储 + BM25 检索。

对应 Day 1 Task 4（Memory Storage）与 Task 3（Search 第一版：BM25 Retrieval）。

存储格式（data_store/memory.jsonl，每行一条记忆）::

    {
        "id": "mem_000001",
        "user_id": "task-0001:noisy:local",
        "session_id": "eval:local:sample:task-0001:noisy",
        "request_id": "eval:local:coding:...:chunk-000",
        "content": "memory text",
        "created_at": "2026-09-28T12:00:00Z"
    }

检索：按 user_id 隔离构建 BM25 索引（rank_bm25），懒加载 + 写入后失效。
"""

import json
import math
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi


jieba.setLogLevel(40)
STORE_DIR = Path(__file__).parent / "data_store" # 数据存储目录
STORE_PATH = STORE_DIR / "memory.jsonl" # 持久化存储路径



# 存储结果
class MemoryStore:
    """MemoryStore：JSONL 持久化存储 + BM25 检索。"""

    def __init__(self, path: Path = STORE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock() # 互斥锁
        self._memories = []  # 全量记忆列表
        self._by_user = {}  # user_id -> [memory, ...]
        self._index_by_user = {}  # user_id -> (bm25, docs) # 懒加载，真正需要时才构建
        self._request_ids = set()  # 幂等：request_id 去重
        self._load() # 加载现有数据

    # 持久化
    def _load(self):
        if not self.path.exists():
            return
        # 读取 JSONL 文件，按行解析为 memory 对象
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                mem = json.loads(line)
                self._memories.append(mem)
                self._by_user.setdefault(mem["user_id"], []).append(mem)
                self._request_ids.add(mem.get("request_id"))
    # 将文件写入到磁盘
    def _append_file(self, mem: dict):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(mem, ensure_ascii=False) + "\n") # 中文也可以写入
    # 写入
    def add(self, request_id: str, messages: list, user_id: str,
            session_id: str) -> bool:
        """写入一批 messages；request_id 重复时幂等跳过。返回是否新写入。"""
        with self._lock:
            if request_id and request_id in self._request_ids:
                return False
            content = _messages_to_text(messages)
            mem = {
                "id": f"mem_{uuid.uuid4().hex[:12]}",
                "user_id": user_id,
                "session_id": session_id,
                "request_id": request_id,
                "content": content,
                "created_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
            }
            self._append_file(mem)
            self._memories.append(mem)
            self._by_user.setdefault(user_id, []).append(mem)
            self._index_by_user.pop(user_id, None)  # 索引失效，为了懒加载、
            if request_id:
                self._request_ids.add(request_id) # 幂等：request_id 去重
            return True

    # 获取索引
    def _get_index(self, user_id: str):
        cached = self._index_by_user.get(user_id) # 获取缓存的索引
        if cached is None:
            docs = self._by_user.get(user_id, []) # 获取记忆
            tokens_docs = [tokenize(d["content"]) for d in docs] # 分词
            cached = (BM25Okapi(tokens_docs), tokens_docs)
            self._index_by_user[user_id] = cached
        return cached

    # 检索
    def search(self, user_id: str, query: str, top_k: int = 5) -> list:
        with self._lock:
            docs = self._by_user.get(user_id, [])
            if not docs:
                return []
            bm25, docs = self._get_index(user_id) # 获取索引，bm25和docs列表
            #计算分数
            scores = bm25.get_scores(tokenize(query))
            # 排序
            ranked = sorted(
                range(len(docs)), key=lambda i: scores[i], reverse=True)
            results = []
            #返回前 top_k 个结果
            for i in ranked[:top_k]:
                results.append({
                    "id": docs[i]["id"],
                    "content": docs[i]["content"],
                    "score": float(scores[i]),
                    "created_at": docs[i].get("created_at"),
                })
            return results

    # 运维统计
    def stats(self) -> dict:
        return {
            "total_memories": len(self._memories),
            "total_users": len(self._by_user),
            "store_path": str(self.path),
        }

    def reset(self):
        """清空存储（本地评测重跑前使用）。"""
        with self._lock:
            self._memories.clear()
            self._by_user.clear()
            self._index_by_user.clear()
            self._request_ids.clear()
            if self.path.exists():
                self.path.unlink()




def _messages_to_text(messages) -> str:
    return "\n".join(m["content"] for m in messages)

# ---------------------------------------------------------------------------
# Tokenizer（中英混合）：ASCII 标识符拆 snake_case/camelCase；中文用 jieba，缺包回退字符级
# ---------------------------------------------------------------------------
# camelCase 边界：小写/数字→大写（getUser -> get|User），或 缩写→单词（HTTPResponse -> HTTP|Response）
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# 连续 ASCII 标识符/数字，或连续中日韩统一表意文字（\u4e00-\u9fff）
_CHUNK_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
def _split_identifier(raw: str) -> list:
    """ASCII 标识符 -> 小写 token 列表（含完整标识符本身 + 拆分出的子词）。

    顺序严格为：原始串 -> 按 _ 拆 -> 按 camelCase 拆 -> 最后 lowercase。
    绝不在 camelCase 拆分前 lowercase，否则 getUserProfile 会被压平成 getuserprofile。
    """
    subtokens = []
    for piece in (p for p in raw.split("_") if p):         # 先拆 snake_case
        for part in _CAMEL_RE.sub("|", piece).split("|"):  # 再拆 camelCase
            if part:
                subtokens.append(part.lower())             # 最后 lowercase
    # 完整标识符（小写）+ 子词，按序去重，避免单词自身被重复计入 TF
    seen, out = set(), []
    for t in [raw.lower()] + subtokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out

def _split_cjk(chunk: str) -> list:
    """中文分词：装了 jieba 用精确模式 cut；否则回退字符级，保证缺包也能跑。"""
    if jieba is not None:
        return [t for t in (tok.strip() for tok in jieba.cut(chunk, cut_all=False)) if t]
    return list(chunk)
def tokenize(text: str) -> list:
    """把中英混合文本切成 BM25 用的 token 列表。"""
    if not text:
        return []
    tokens = []
    for chunk in _CHUNK_RE.findall(text):
        if chunk[0].isascii():            # ASCII 标识符 / 数字
            tokens.extend(_split_identifier(chunk))
        else:                             # 连续中文
            tokens.extend(_split_cjk(chunk))
    return tokens
