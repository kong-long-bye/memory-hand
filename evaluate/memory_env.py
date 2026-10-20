# -*- coding: utf-8 -*-
"""把官方 /add 请求在本地重建成内存库，并提供 relevant / noisy 两种记忆环境。

官方 smoke 的两次 Add 是同一批轨迹分别灌给两个 user：
  - 只含 astropy timeseries 轨迹的 user  -> relevant 条件
  - 额外混入 progressbar / furlong 轨迹   -> noisy 条件
这里按“是否命中 timeseries 关键词”自动识别，不写死 user_id。
"""

import sys
from collections import defaultdict

import env


sys.path.insert(0, str(env.SRC_DIR))
import memory_engine  # noqa: E402


def _add_text(body):
    return "\n".join(m.get("content") or "" for m in body.get("messages") or [])


def _is_relevant(body):
    # smoke 数据里两条相关轨迹都围绕 astropy.timeseries；另外两条是
    # progressbar / furlong，完全没有 timeseries 关键词。
    low = _add_text(body).lower()
    return "timeseries" in low or "_required_columns" in low


def _group_adds(requests):
    users = defaultdict(list)
    for item in requests:
        if item.get("path") == "/add":
            body = item.get("body") or {}
            users[body.get("user_id")].append(body)
    return users


def select_user(requests, condition):
    """relevant：只含相关轨迹的 user；noisy：混入无关轨迹的 user。"""
    users = _group_adds(requests)
    stats = {}
    for user_id, adds in users.items():
        relevant = [body for body in adds if _is_relevant(body)]
        stats[user_id] = {
            "adds": len(adds),
            "relevant_adds": len(relevant),
            "has_noise": len(relevant) < len(adds),
        }
    if not stats:
        raise RuntimeError("official_requests.jsonl 里没有 /add 请求")
    if condition == "relevant":
        candidates = [u for u, s in stats.items()
                      if s["relevant_adds"] and not s["has_noise"]]
        if candidates:
            return max(candidates, key=lambda u: stats[u]["relevant_adds"]), stats
    elif condition == "noisy":
        candidates = [u for u, s in stats.items()
                      if s["relevant_adds"] and s["has_noise"]]
        if candidates:
            return max(candidates, key=lambda u: stats[u]["adds"]), stats
    # 数据形状变化时退化为选最相关的 user，避免直接失败
    return max(stats, key=lambda u: (stats[u]["relevant_adds"],
                                     stats[u]["adds"])), stats


class MemoryEnv:
    def __init__(self, store, user_id, condition, adds, relevant_ids, stats):
        self.store = store
        self.user_id = user_id
        self.condition = condition
        self.adds = adds
        self.relevant_ids = relevant_ids
        self.stats = stats

    def search(self, query, top_k=5):
        return self.store.search(self.user_id, query, top_k=top_k)

    def describe(self):
        return {
            "condition": self.condition,
            "user_id": self.user_id,
            "adds": len(self.adds),
            "relevant_parents": len(self.relevant_ids),
            "users": self.stats,
        }


def build(condition="relevant", user_id=None, store_path=None):
    requests = env.load_official_requests()
    auto_user, stats = select_user(requests, condition)
    user = user_id or auto_user
    adds = _group_adds(requests).get(user, [])
    if not adds:
        raise RuntimeError(f"user_id={user} 没有对应的 /add 请求")

    path = store_path or (env.WORK_ROOT / f"memory_{condition}.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    store = memory_engine.MemoryStore(path)

    relevant_ids = []
    for body in adds:
        messages = body.get("messages") or []
        store.add(
            request_id=body.get("request_id"),
            messages=messages,
            user_id=user,
            session_id=body.get("session_id"),
        )
        if _is_relevant(body):
            relevant_ids.append(memory_engine.parent_id(messages))
    return MemoryEnv(store, user, condition, adds, relevant_ids, stats)
