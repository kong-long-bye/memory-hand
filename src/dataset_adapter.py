# -*- coding: utf-8 -*-
"""SWEContextBench 适配器：把三张表 join 成评测用的 (samples, pool)。

数据集形态（data/SWEContextBench/data/）：
    Experience   经验池，每行是一个已解决的历史任务（SWE-bench 12 列）
    Related      待解任务，结构同上，用 problem_statement 作为检索 query
    Relationship 有向边 related_instance_id -> experience_instance_id，即 gold 标注

两个 condition 各占一个全局 user_id 命名空间（跨任务共享，这是该数据集的核心考点）：
    relevant  池 = 所有被 gold 引用过的经验（去重），衡量检索上限
    noisy     池 = 全量经验，衡量大量无关记忆下的抗噪能力

写入与评测必须共用本模块，保证 /add 的 content 与映射表严格一致。
"""
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent # 仓库根
DATA_DIR = BASE_DIR / "data" / "SWEContextBench" / "data"

SPLITS = {
    "lite": {
        "experience": DATA_DIR / "SWEContextBench_Lite_Experience.jsonl",
        "related": DATA_DIR / "SWEContextBench_Related_Lite.jsonl",
    },
    "full": {
        "experience": DATA_DIR / "SWEContextBench_Experience.jsonl",
        "related": DATA_DIR / "SWEContextBench_Related.jsonl",
    },
}
RELATIONSHIP_PATH = DATA_DIR / "SWEContextBench_Relationship.jsonl"

CONDITIONS = ("relevant", "noisy")

# 一条经验记忆写入什么：问题描述 +（可选）gold patch。
# test_patch / PASS_TO_PASS 体积大且对检索无增益，不进记忆。
INCLUDE_PATCH = True

_CREATED_AT_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S")


def _read_jsonl(path: Path) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _to_millis(created_at: str):
    """created_at 有两种格式（ISO 带 Z / 空格分隔），统一成 epoch 毫秒。"""
    for fmt in _CREATED_AT_FORMATS:
        try:
            dt = datetime.strptime(created_at, fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=None).timestamp() * 1000)
    return None


def experience_text(row: dict) -> str:
    """把一条经验任务渲染成记忆文本。"""
    parts = [row["problem_statement"].strip()]
    if INCLUDE_PATCH and row.get("patch", "").strip():
        parts.append(row["patch"].strip())
    return "\n\n".join(parts)


def load_split(split: str = "lite"):
    """返回 (samples, pool)。pool 是全量经验，samples 只存 id 引用。"""
    if split not in SPLITS:
        raise KeyError(f"unknown split {split!r}, choose from {sorted(SPLITS)}")
    for path in (*SPLITS[split].values(), RELATIONSHIP_PATH):
        if not path.exists():
            raise FileNotFoundError(f"{path} not found, run download_dataset.py first")

    experiences = _read_jsonl(SPLITS[split]["experience"])
    related = _read_jsonl(SPLITS[split]["related"])
    edges = _read_jsonl(RELATIONSHIP_PATH)

    pool = [
        {
            "id": row["instance_id"],
            "repo": row["repo"],
            "timestamp": _to_millis(row["created_at"]),
            "content": experience_text(row),
        }
        for row in experiences
    ]
    pool_ids = {m["id"] for m in pool}
    pool_repo = {m["id"]: m["repo"] for m in pool}

    gold = defaultdict(set)
    for edge in edges:
        if edge["experience_instance_id"] in pool_ids:
            gold[edge["related_instance_id"]].add(edge["experience_instance_id"])

    seen, unique_related = set(), []
    for row in related:
        # full split 的 Related 有 362 行但只有 357 个唯一 instance_id，
        # 重复行 problem_statement 可能不一致；按 ID 保首次出现，避免同一任务重复计分。
        if row["instance_id"] in seen:
            continue
        seen.add(row["instance_id"])
        unique_related.append(row)

    samples = []
    for row in unique_related:
        task_id = row["instance_id"]
        gold_ids = sorted(gold.get(task_id, set()))
        # 近似 hard negative：同仓库、非 gold。数据集无官方标注，仅供噪声拒绝参考。
        hard_neg = [pid for pid in pool_ids
                    if pid not in gold_ids and pool_repo[pid] == row["repo"]]
        samples.append({
            "task_id": task_id,
            "repository": row["repo"],
            "query": row["problem_statement"].strip(),
            "gold_memory_ids": gold_ids,
            "hard_negative_ids": sorted(hard_neg),
        })
    return samples, pool


def condition_user_id(condition: str, run_id: str) -> str:
    """每个 condition 一个全局记忆空间，跨任务共享。"""
    return f"swe:{condition}:{run_id}"


def condition_memories(condition: str, samples: list, pool: list) -> list:
    """按 condition 取该空间里应该存在的记忆。"""
    if condition == "noisy":
        return list(pool)
    referenced = {gid for s in samples for gid in s["gold_memory_ids"]}
    return [m for m in pool if m["id"] in referenced]


def build_add_requests(condition: str, run_id: str, memories: list) -> list:
    """按官方 /add 格式构造写入负载；一条经验一次请求，request_id 幂等。"""
    user_id = condition_user_id(condition, run_id)
    return [
        {
            "request_id": f"swe:{run_id}:{condition}:{m['id']}",
            "messages": [
                {"role": "user", "content": m["content"],
                 **({"timestamp": m["timestamp"]} if m["timestamp"] else {})}
            ],
            "user_id": user_id,
            "session_id": f"swe:{condition}:{m['id']}",
        }
        for m in memories
    ]


def build_search_request(sample: dict, condition: str, run_id: str,
                         top_k: int = 10) -> dict:
    return {
        "query": sample["query"],
        "user_id": condition_user_id(condition, run_id),
        "top_k": top_k,
    }


if __name__ == "__main__":
    for name in SPLITS:
        s, p = load_split(name)
        gold_union = {g for x in s for g in x["gold_memory_ids"]}
        print(f"[{name}] pool={len(p)} tasks={len(s)} "
              f"gold_edges={sum(len(x['gold_memory_ids']) for x in s)} "
              f"unique_gold={len(gold_union)} "
              f"relevant_pool={len(condition_memories('relevant', s, p))}")
        no_gold = [x["task_id"] for x in s if not x["gold_memory_ids"]]
        print(f"         无 gold 的任务={len(no_gold)} {no_gold[:3]}")
