# -*- coding: utf-8 -*-
"""Memory Engine V2.1：结构化 Parent Memory + message 级 BM25 + 证据组装。

存储（data_store/v1.1/memory.jsonl，每行一次 Add）::

    {
        "id": "mem_3a1f9c2b7d10",
        "user_id": "...",
        "session_id": "...",
        "request_id": "...",
        "messages": [
            {"role": "user", "content": "原始消息", "timestamp": 1759032000000},
            {"role": "assistant", "content": "原始回复"}
        ],
        "timestamp": 1759032000000,
        "created_at": "2026-10-17T00:00:00Z"
    }

检索单位是 message：普通消息整条进 BM25；超过 CHUNK_INDEX_TRIGGER 的消息只在
它自己内部切块，绝不跨消息边界。unit 分数按 Parent 取最大值聚合，每个 Parent
只返回一次；content 由 EvidenceBuilder 在同一 Parent 内组装相关证据，BM25
命中的最高分 unit 始终保留，并额外取前 ANCHOR_UNITS 个 unit 作为证据锚点，
排序与预算仍由本模块控制。
所有返回 content 共享 MAX_RESPONSE_CONTENT_CHARS 硬预算，放不下的候选跳过；
data 长度不超过 top_k；得分为负的命中不返回，没有任何词面证据时返回空列表。

V1.0 的 data_store/memory.jsonl 已把 messages 拼平成 content，role 与消息边界
不可恢复；本版不读取旧格式，旧文件保留作 V1.0 回滚与基线。
"""

import json
import hashlib
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi

from evidence_builder import EvidenceBuilder


jieba.setLogLevel(40)
PROJECT_ROOT = Path(__file__).resolve().parent.parent # 仓库根（容器内 /app）
STORE_DIR = PROJECT_ROOT / "data_store" # 数据存储目录
STORE_PATH = STORE_DIR / "v3.0" / "memory.jsonl" # 结构化存储

CHUNK_SIZE = 4000                # 超长消息内部的块目标大小（按行边界断，不切碎一行）
CHUNK_INDEX_TRIGGER = 20000      # 不超过该长度的 message 整条作为一个检索单元
ANCHOR_UNITS = 3                 # 每个 Parent 最多取几个高分 unit 作为证据锚点
MAX_RESPONSE_CONTENT_CHARS = 60000  # 单次 /search 所有 content 的硬预算（含角色标签）


def _retrieval_mode() -> str:
    """渲染模式开关：v2_1（默认，固定分节）/ v3_0（状态判定+答案优先）。

    只影响 content 的组装方式，不影响召回、排序、去重与持久化；
    出问题时设 RETRIEVAL_MODE=v2_1 即可不改存储直接回滚。
    """
    raw = (os.environ.get("RETRIEVAL_MODE") or "v2_1").strip().lower()
    return "v3_0" if raw in ("v3_0", "v3") else "v2_1"


def memory_id(content: str) -> str:
    """内容寻址 ID：同一份原文在任何 user_id 下都得到同一个 id。

    /search 命中项靠它映射回来源记录（评测、去重都依赖这个可追溯性），
    不再依赖"返回内容完全等于原文"。
    """
    return "mem_" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def parent_id(messages: list) -> str:
    """Parent Memory 的稳定 ID。

    对所有消息的 (role, content) 序列取哈希，role 结构不同就不会被去重吞掉。
    timestamp 不参与，重发同一对话仍命中同一 Parent。
    """

    payload = json.dumps(
        [[m.get("role"), m.get("content")] for m in messages],
        ensure_ascii=False, separators=(",", ":"))
    return memory_id(payload)


def _chunk_ranges(text: str) -> list:
    """单条消息内部切成 [(start, end), ...]；够短就整条一个 unit，绝不跨消息。"""
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


def _normalize_messages(messages: list) -> list:
    """只保留 role/content/timestamp，顺序原样保留。"""
    normalized = []
    for m in messages:
        msg = {"role": m.get("role", ""), "content": m.get("content", "")}
        if m.get("timestamp") is not None:
            msg["timestamp"] = m["timestamp"]
        normalized.append(msg)
    return normalized


def _messages_timestamp(messages):
    """一次 Add 里最新的事件时间戳（毫秒），Parent 只留一个值；没带时返回 None。"""
    marks = [m.get("timestamp") for m in messages if m.get("timestamp") is not None]
    return max(marks) if marks else None


# 存储结果
class MemoryStore:
    """MemoryStore：结构化 JSONL 持久化 + message 级 BM25。"""

    def __init__(self, path: Path = STORE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock() # 互斥锁
        self._memories = []  # 全量 Parent 列表
        self._by_user = {}  # user_id -> [Parent, ...]
        self._ids_by_user = {}  # user_id -> {parent_id, ...} 同一 user 内去重
        self._index_by_user = {}  # user_id -> (bm25, units, tokens) 懒加载
        self._request_ids = set()  # 幂等：request_id 去重
        self.retrieval_mode = _retrieval_mode()  # v2_1 / v3_0，只影响 content 组装
        self._evidence_builder = EvidenceBuilder(mode=self.retrieval_mode)
        self._load() # 加载现有数据

    # 持久化
    def _load(self):
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                mem = json.loads(line)
                if not isinstance(mem.get("messages"), list):
                    raise ValueError(
                        "存储文件是 V1.0 平铺格式（没有 messages），V1.1 无法恢复 "
                        f"role 与消息边界，请换用新的存储文件：{self.path}")
                self._request_ids.add(mem.get("request_id"))
                mid = parent_id(mem["messages"])
                mem["id"] = mid  # id 由结构决定：载入时归一，旧记录也能对上
                ids = self._ids_by_user.setdefault(mem["user_id"], set())
                if mid not in ids:  # 同一 user 内的历史重复行，载入时就不再进索引
                    ids.add(mid)
                    self._memories.append(mem)
                    self._by_user.setdefault(mem["user_id"], []).append(mem)

    def _append_file(self, mem: dict):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(mem, ensure_ascii=False) + "\n") # 中文也可以写入

    # 写入
    def add(self, request_id: str, messages: list, user_id: str,
            session_id: str) -> bool:
        """写入一次 Add（一个 Parent Memory）；request_id 重复时幂等跳过。

        同一 user 内相同 Parent（role + 原文序列一致）也不重复入库：重复项会
        白占 Top-K 名额并稀释自身关键词的 IDF。
        """
        with self._lock:
            if request_id and request_id in self._request_ids:
                return False
            normalized = _normalize_messages(messages) # 只保留 role/content/timestamp
            mid = parent_id(normalized)
            if mid in self._ids_by_user.get(user_id, ()):
                if request_id:
                    self._request_ids.add(request_id)  # 幂等：request_id 去重
                return False
            mem = {
                "id": mid,
                "user_id": user_id,
                "session_id": session_id,
                "request_id": request_id,
                "messages": normalized,
                "timestamp": _messages_timestamp(normalized),
                "created_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
            }
            self._append_file(mem) # 写入磁盘
            self._memories.append(mem)
            self._by_user.setdefault(user_id, []).append(mem)
            self._ids_by_user.setdefault(user_id, set()).add(mid)
            self._index_by_user.pop(user_id, None)  # 写入后索引失效，下次检索重建
            if request_id:
                self._request_ids.add(request_id) # 幂等：request_id 去重
            return True

    # 获取索引
    def _get_index(self, user_id: str):
        """返回 (bm25, units, token_sets)，三者按下标一一对应。

        units 元素为 (记忆下标, 消息下标, 块下标, start, end, role)。
        token_sets 在索引期建好，查询时只做集合交集，不重复构造。
        长消息内部切块后，同一 Parent 会对应多个 unit；返回时必须按记忆下标聚合，
        不能把 unit 当成一条记忆。
        """
        cached = self._index_by_user.get(user_id) # 获取缓存的索引
        if cached is None:
            docs = self._by_user.get(user_id, []) # 获取 Parent
            units, tokens = [], []
            for mi, mem in enumerate(docs): # mi是记忆下表，
                for msg_i, msg in enumerate(mem["messages"]):
                    text = msg.get("content") or ""
                    for ci, (s, e) in enumerate(_chunk_ranges(text)):
                        toks = tokenize(text[s:e])
                        if not toks:  # 空消息不产生检索单元
                            continue
                        units.append((mi, msg_i, ci, s, e, msg.get("role") or ""))
                        tokens.append(toks)
            bm25 = BM25Okapi(tokens) if tokens else None
            cached = (bm25, units, [set(t) for t in tokens])
            self._index_by_user[user_id] = cached
        return cached

    # 检索
    def search(self, user_id: str, query: str, top_k: int = 5) -> list:
        """按 Parent 返回 <= top_k 条命中，content 受全局字符预算约束。"""
        if top_k <= 0:
            return []
        query_tokens = tokenize(query or "")
        if not query_tokens:  # 没切出 token 时 BM25 没有意义，直接返回空
            return []
        with self._lock:
            docs = self._by_user.get(user_id, []) # 得到这个用户的记忆
            if not docs:
                return []
            # bm25 是一个units的索引，
            # units是一个元组列表，表示这个units在用户记忆的位置和范围，通过记忆下标得到mem，通过消息下标得到message，通过快下标+偏移，得到具体的内容范围和角色
            bm25, units, _ = self._get_index(user_id) # 得到这个用户的索引
            if bm25 is None:
                return []
            # 得到每一个unit的分数
            scores = bm25.get_scores(query_tokens)

            # unit -> Parent：Parent 的分数仍是最高分 unit，但证据锚点取前
            # ANCHOR_UNITS 个 unit，让同一 Parent 里分散的证据一起参与组装。
            per_parent = {}
            for unit, score in zip(units, scores):
                if score < 0:
                    continue
                per_parent.setdefault(unit[0], []).append((float(score), unit))

            ranked = []
            for mi, bucket in per_parent.items():
                bucket.sort(key=lambda item: item[0], reverse=True)
                ranked.append((mi, bucket[0][0],
                               [unit for _, unit in bucket[:ANCHOR_UNITS]]))
            ranked.sort(key=lambda item: item[1], reverse=True)
            # 全局预算按排名顺序花：放不下的候选跳过并继续看后面的，
            # 但保留项始终按相关性降序，data 长度不超过 top_k
            results, remaining = [], MAX_RESPONSE_CONTENT_CHARS # 全局预算，单位字（60k）
            for mi, score, anchors in ranked:
                if len(results) >= top_k:
                    break
                content = self._evidence_builder.render(
                    docs[mi], anchors, query, remaining)
                if not content:
                    continue
                results.append({
                    "id": docs[mi]["id"],
                    "content": content,
                    "score": score,
                    "created_at": docs[mi].get("created_at"),
                })
                remaining -= len(content)
            return results

    # 运维统计
    def stats(self) -> dict:
        return {
            "total_memories": len(self._memories),
            "total_units": sum(len(_chunk_ranges(m.get("content") or ""))
                               for mem in self._memories
                               for m in mem["messages"]),
            "total_users": len(self._by_user),
            "retrieval_mode": self.retrieval_mode,
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
            self._evidence_builder.clear()
            if self.path.exists():
                self.path.unlink()


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
