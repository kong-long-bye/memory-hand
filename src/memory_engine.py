# -*- coding: utf-8 -*-
"""Memory Engine V1：JSONL 持久化存储 + BM25 检索。

对应 Day 1 Task 4（Memory Storage）与 Task 3（Search 第一版：BM25 Retrieval）。

存储格式（data_store/memory.jsonl，每行一条记忆）::

    {
        "id": "mem_3a1f9c2b7d10",
        "user_id": "task-0001:noisy:local",
        "session_id": "eval:local:sample:task-0001:noisy",
        "request_id": "eval:local:coding:...:chunk-000",
        "content": "memory text",
        "timestamp": 1759032000000,
        "created_at": "2026-09-28T12:00:00Z"
    }

检索：按 user_id 隔离构建 BM25 索引（rank_bm25），懒加载 + 写入后失效。
短记忆整段索引、整段返回；超过 CHUNK_INDEX_TRIGGER 的记忆按块索引，返回时拼
"命中块 + 邻居块 + 尾部方案段"的证据视图，原始全文仍完整存在 JSONL 里。
"""

import json
import hashlib
import math
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi


jieba.setLogLevel(40)
PROJECT_ROOT = Path(__file__).resolve().parent.parent # 仓库根（容器内 /app）
STORE_DIR = PROJECT_ROOT / "data_store" # 数据存储目录
STORE_PATH = STORE_DIR / "memory.jsonl" # 持久化存储路径


def memory_id(content: str) -> str:
    """内容寻址 ID：同一份原文在任何 user_id 下都得到同一个 id。

    /search 命中项靠它映射回来源记录（评测、去重、后续分块视图都依赖这个可追溯性），
    不再依赖"返回内容完全等于原文"。
    """
    return "mem_" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 证据视图（改进 1）：长轨迹切块索引 + 命中邻近平滑 + 尾部方案段保底 + 按块控制预算
#
# 平台送来的单条轨迹 5.6 万~40 万字符，"改法"只落在最后 1~4KB；整段原文回给平台
# 解题模型会在答案侧预算里被截断，恰好砍掉尾部。故索引按块建，返回按块拼。
# 原始全文始终整条存在 JSONL 里，这里只做视图，不删不改。
# ---------------------------------------------------------------------------
CHUNK_SIZE = 4000               # 长文本切块目标大小（按行边界断，不切碎一行）
CHUNK_INDEX_TRIGGER = 20000     # 短于该长度的记忆整段进索引，与切块视图等价
NEIGHBOR_CHUNKS = 2             # 命中块前后各带几块（Memoria 的邻近平滑）
TAIL_CHUNKS = 2                 # 尾部保底进入返回的块数（方案段在最后 1~4KB）
MAX_HIT_CHUNKS = 2              # 一条记忆最多带几个命中块，防止一条巨型轨迹吃满预算
MIN_HIT_CHARS = 12000           # 每条命中的预算下限：装得下命中块 + 尾部两块
MAX_RESPONSE_CHARS = 60000      # 单次 /search 软预算，按条分摊；条数多时由下限兜住 60k


def _chunk_ranges(text: str) -> list:
    """长文本切成 [(start, end), ...]，覆盖全文、不重叠、按原文顺序。"""
    total = len(text)
    if total <= CHUNK_INDEX_TRIGGER:
        return [(0, total)]
    ranges, start = [], 0
    while start < total:
        end = min(start + CHUNK_SIZE, total)
        if end < total:
            nl = text.rfind("\n", start + CHUNK_SIZE // 2, end) # 尽量按行断 ，而不是直接断开
            if nl != -1:
                end = nl + 1
        ranges.append((start, end))
        start = end
    return ranges


def _merge_ranges(ranges: list) -> list:
    """合并相邻/重叠区间，返回按起点排序的不相交区间列表。"""
    merged = []
    for s, e in sorted(ranges):
        if merged and s <= merged[-1][1]: # 合并区间
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _pick_ranges(ranges: list, hit_chunks: list, cap: int) -> list:
    """必带：最好的命中块 + 最后 TAIL_CHUNKS 块（方案段在尾部）。

    必带块不受 cap 约束：预算不够宁可多回几 KB，也不能丢改法、丢命中或丢条数。
    其余块（别的命中块、命中块的邻居）才在 cap 内追加。
    """
    last = len(ranges) - 1
    hits = [ci for ci in hit_chunks if 0 <= ci <= last][:MAX_HIT_CHUNKS]

    must = []
    # 最好的命中块 + 最后 TAIL_CHUNKS 块
    for ci in (hits[:1] + list(range(last, last - TAIL_CHUNKS, -1))):
        if 0 <= ci <= last and ci not in must:
            must.append(ci)
    #其他命中块 + 邻居块，按分数排序，按 cap 追加
    optional = [ci for ci in hits if ci not in must]
    # 下面这种方法，分别遍历距离，主要是为了后面的预算控制
    for ci in hits[:1]: # 取出第一个命中块一个
        for off in range(1, NEIGHBOR_CHUNKS + 1):
            for nb in (ci - off, ci + off):
                if 0 <= nb <= last and nb not in must and nb not in optional:
                    optional.append(nb)

    chosen = list(must)
    # 计算已选块的总字符数
    used = sum(ranges[ci][1] - ranges[ci][0] for ci in must)
    for ci in optional:
        size = ranges[ci][1] - ranges[ci][0]
        if used + size > cap:
            continue
        chosen.append(ci)
        used += size
    return _merge_ranges([ranges[ci] for ci in chosen])


def _render_view(mid: str, text: str, spans: list) -> str:
    """把选中的块拼成返回内容；整段命中时原样返回，部分命中时带可追溯标记。"""
    total = len(text)
    if len(spans) == 1 and spans[0] == (0, total):
        return text
    mark = "[memory={};spans={}/{}]".format(
        mid, "+".join("{}-{}".format(s, e) for s, e in spans), total)
    return mark + "\n" + "\n...\n".join(text[s:e] for s, e in spans)


# 存储结果
class MemoryStore:
    """MemoryStore：JSONL 持久化存储 + BM25 检索。"""

    def __init__(self, path: Path = STORE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock() # 互斥锁
        self._memories = []  # 全量记忆列表
        self._by_user = {}  # user_id -> [memory, ...]
        self._ids_by_user = {}  # user_id -> {memory_id, ...} 同一 user 内同内容去重
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
                self._request_ids.add(mem.get("request_id"))
                mid = memory_id(mem["content"])
                mem["id"] = mid  # id 由内容决定：历史 uuid id 载入即归一，无需重新写入
                ids = self._ids_by_user.setdefault(mem["user_id"], set())
                if mid not in ids:  # 同一 user 内同内容的历史重复行，载入时就不再进索引
                    ids.add(mid)
                    self._memories.append(mem)
                    self._by_user.setdefault(mem["user_id"], []).append(mem)
    # 将文件写入到磁盘
    def _append_file(self, mem: dict):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(mem, ensure_ascii=False) + "\n") # 中文也可以写入
    # 写入
    def add(self, request_id: str, messages: list, user_id: str,
            session_id: str) -> bool:
        """写入一批 messages；request_id 重复时幂等跳过。返回是否新写入。

        同一 user 内同内容（换了 request_id 重发）也不重复入库：
        重复项会白占 Top-K 名额并稀释自身关键词的 IDF。
        """
        with self._lock:
            if request_id and request_id in self._request_ids:
                return False
            content = _messages_to_text(messages)
            mid = memory_id(content)
            if mid in self._ids_by_user.get(user_id, ()):
                if request_id:
                    self._request_ids.add(request_id)  # 幂等：request_id 去重
                return False
            mem = {
                "id": mid,
                "user_id": user_id,
                "session_id": session_id,
                "request_id": request_id,
                "content": content,
                "timestamp": _messages_timestamp(messages),
                "created_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
            }
            self._append_file(mem)
            self._memories.append(mem)
            self._by_user.setdefault(user_id, []).append(mem)
            self._ids_by_user.setdefault(user_id, set()).add(mid)
            self._index_by_user.pop(user_id, None)  # 索引失效，为了懒加载、
            if request_id:
                self._request_ids.add(request_id) # 幂等：request_id 去重
            return True

    # 获取索引
    def _get_index(self, user_id: str):
        """返回 (bm25, corpus)。corpus 与 BM25 文档一一对应：(记忆下标, 块下标)。

        长记忆按块进索引，所以语料条目比记忆多；返回时必须按记忆下标聚合，
        不能把语料条目当成记忆。
        """
        cached = self._index_by_user.get(user_id) # 获取缓存的索引
        if cached is None:
            docs = self._by_user.get(user_id, []) # 获取记忆
            corpus, tokens_docs = [], []
            for mi, d in enumerate(docs):
                for ci, (s, e) in enumerate(_chunk_ranges(d["content"])): # ci是块下标，s,e是块的起止位置，_chunk_ranges返回的是一个列表，里面是每个块的起止位置
                    corpus.append((mi, ci))
                    tokens_docs.append(tokenize(d["content"][s:e])) # 对每个块进行分词，得到一个二维列表，里面是每个块的分词结果
            cached = (BM25Okapi(tokens_docs), corpus) # 创建 BM25 索引，返回索引和语料
            self._index_by_user[user_id] = cached
        return cached

    # 检索
    def search(self, user_id: str, query: str, top_k: int = 5) -> list:
        with self._lock:
            docs = self._by_user.get(user_id, [])
            if not docs:
                return []
            bm25, corpus = self._get_index(user_id) # 获取索引，bm25和(记忆,块)语料
            #计算分数（块级）
            scores = bm25.get_scores(tokenize(query))
            # 块 -> 记忆：一条记忆取其最好块的分数，命中块按分数从高到低记序
            grouped = {}
            # 将块级分数聚合到记忆级别，取每条记忆的最高分数，并记录命中块的下标
            for k, s in enumerate(scores):
                mi, ci = corpus[k] # mi是记忆下标，ci是块下标
                s = float(s)
                g = grouped.setdefault(mi, {"score": float("-inf"), "hits": []})
                g["score"] = max(g["score"], s)
                g["hits"].append((s, ci))
            # 记忆级排序，返回前 top_k 条记忆（条数语义不变：仍是 <= top_k 条记忆）
            ranked = sorted(grouped.items(),
                            key=lambda kv: kv[1]["score"], reverse=True)[:top_k]
            # 预算按返回条数分摊（每条有下限），只砍块、不丢整条命中，
            # 也绝不把 top_k 静默砍短——历史上这么做过，recall@10 当场掉 0.08
            cap = max(MIN_HIT_CHARS, MAX_RESPONSE_CHARS // max(len(ranked), 1)) # 这里可能直接超过128k
            results = []
            for mi, g in ranked:
                text = docs[mi]["content"]
                ranges = _chunk_ranges(text)
                if len(ranges) == 1:
                    content = text
                else:
                    # 多块命中时，按分数排序，取前 cap 个字符
                    hits = [ci for _s, ci in
                            sorted(g["hits"], key=lambda x: x[0], reverse=True)] # 按分数排序命中块的下标
                    content = _render_view(docs[mi]["id"], text,
                                           _pick_ranges(ranges, hits, cap))
                results.append({
                    "id": docs[mi]["id"],
                    "content": content,
                    "score": g["score"],
                    "created_at": docs[mi].get("created_at"),
                })
            return results

    # 运维统计
    def stats(self) -> dict:
        return {
            "total_memories": len(self._memories),
            "total_chunks": sum(len(_chunk_ranges(m["content"]))
                                for m in self._memories),
            "total_users": len(self._by_user),
            "store_path": str(self.path),
        }

    def reset(self):
        """清空存储（本地评测重跑前使用）。"""
        with self._lock:
            self._memories.clear()
            self._by_user.clear()
            self._ids_by_user.clear()
            self._index_by_user.clear()
            self._request_ids.clear()
            if self.path.exists():
                self.path.unlink()




def _messages_to_text(messages) -> str:
    return "\n".join(m["content"] for m in messages)


def _messages_timestamp(messages):
    """一批 messages 里最新的事件时间戳（毫秒），一条记忆只留一个值。

    平台带 timestamp 才有可靠的顺序信号（跨记录邻近平滑要用）；没带时返回 None。
    """
    marks = [m.get("timestamp") for m in messages if m.get("timestamp") is not None]
    return max(marks) if marks else None

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
