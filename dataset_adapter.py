# -*- coding: utf-8 -*-
"""数据集适配器：把 coding_memory_benchmark_rich 的原始记录转换为标准评测结构。

标准结构（normalize_sample 输出）::

    {
        "task_id": "task-0001",
        "category": "bug_fix",
        "difficulty": "medium",
        "language": "Python",
        "framework": "pytest",
        "repository": "keystone-services",
        "query": "...",                      # 发给 Search API 的查询
        "expected_behavior": "...",
        "gold_memory_ids": ["t0001_rel_1", ...],
        "hard_negative_ids": ["t0001_near_1", ...],
        "memory_pool": [                     # 全量记忆池
            {"id": "...", "relevance": "gold|hard_negative|noise",
             "content": "...", "timestamp": 1704153601000}
        ],
        "conditions": {"relevant": [ids...], "noisy": [ids...]},
        "add_requests": [...],               # api_fixture 中现成的 /add 负载
        "search_request": {...}              # api_fixture 中现成的 /search 负载
    }

如果日后换数据集，只需修改 normalize_sample()，评测逻辑（evaluate.py）不动。
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data" / "coding_memory_benchmark_rich"
DATASET_PATH = DATA_DIR / "dataset.jsonl"        # 300 条完整集
VALIDATION_PATH = DATA_DIR / "validation.jsonl"  # 30 条验证集

CONDITIONS = ("relevant", "noisy")


def normalize_sample(raw: dict) -> dict:
    """把一行原始 JSONL 记录转换为标准结构。"""
    api_fixture = raw.get("api_fixture", {}) or {}
    return {
        "task_id": raw["task_id"],
        "category": raw.get("category", ""),
        "difficulty": raw.get("difficulty", ""),
        "language": raw.get("language", ""),
        "framework": raw.get("framework", ""),
        "repository": raw.get("repository", ""),
        "query": raw["query"],
        "expected_behavior": raw.get("expected_behavior", ""),
        "gold_memory_ids": list(raw.get("gold_memory_ids", [])),
        "hard_negative_ids": list(raw.get("hard_negative_ids", [])),
        "memory_pool": [
            {
                "id": m["id"],
                "relevance": m.get("relevance", "noise"),
                "content": m["content"],
                "timestamp": m.get("timestamp"),
            }
            for m in raw.get("memory_pool", [])
        ],
        "conditions": {
            cond: list(ids)
            for cond, ids in (raw.get("conditions") or {}).items()
        },
        "add_requests": list(api_fixture.get("add_requests", [])),
        "search_request": dict(api_fixture.get("search_request", {})),
    }


def load_dataset(path=None) -> list:
    """加载并标准化整个数据集（默认 validation split，跑得快）。"""
    path = Path(path) if path else VALIDATION_PATH
    if not path.exists():
        raise FileNotFoundError(f"dataset file not found: {path}")
    samples = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(normalize_sample(json.loads(line)))
    return samples


def condition_user_id(sample: dict, condition: str, run_id: str = "local") -> str:
    """按 任务+条件+run 隔离记忆空间，保证 relevant / noisy 互不污染。"""
    return f"{sample['task_id']}:{condition}:{run_id}"


def build_add_requests(sample: dict, condition: str, run_id: str = "local") -> list:
    """按官方 /add 请求格式，为指定 condition 构造批量写入负载。

    字段格式与 example_api_flow.json / 官方 Add API 保持一致：
    request_id / messages / user_id / session_id。
    """
    allowed = set(sample["conditions"].get(condition) or [])
    pool = [m for m in sample["memory_pool"] if m["id"] in allowed]
    # 按 memory_pool 原始顺序写回，与 add_requests 分块顺序一致
    user_id = condition_user_id(sample, condition, run_id)
    requests = []
    for idx, mem in enumerate(pool):
        msg = {"role": "user", "content": mem["content"]}
        if mem.get("timestamp") is not None:
            msg["timestamp"] = mem["timestamp"]
        requests.append({
            "request_id": f"eval:{run_id}:coding:{sample['repository']}:{sample['task_id']}:{condition}:chunk-{idx:03d}",
            "messages": [msg],
            "user_id": user_id,
            "session_id": f"eval:{run_id}:sample:{sample['task_id']}:{condition}",
        })
    return requests


def build_search_request(sample: dict, condition: str, run_id: str = "local",
                         top_k: int = 100) -> dict:
    """按官方 /search 请求格式构造查询负载。"""
    return {
        "query": sample["query"],
        "user_id": condition_user_id(sample, condition, run_id),
        "top_k": top_k,
    }


if __name__ == "__main__":
    samples = load_dataset()
    print(f"loaded {len(samples)} samples from {VALIDATION_PATH.name}")
    s = samples[0]
    print(f"first task: {s['task_id']} | {s['category']} | {s['language']}/{s['framework']}")
    print(f"  memory_pool={len(s['memory_pool'])} "
          f"gold={len(s['gold_memory_ids'])} hard_neg={len(s['hard_negative_ids'])}")
    print(f"  conditions: relevant={len(s['conditions']['relevant'])} "
          f"noisy={len(s['conditions']['noisy'])}")
    print(f"  add_requests(noisy)={len(build_add_requests(s, 'noisy'))}")
