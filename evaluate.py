
"""Evaluation Script（Day 1 Task 1 闭环的最后一步）：

    Search API -> 检索指标 -> Baseline Score

对每个任务的每个 condition（relevant / noisy）发送官方格式 /search 请求，
把返回结果映射回 memory_pool id，计算数据集建议的检索指标：

    Recall@5, Recall@10, MRR, nDCG@10, Noise@10, hard-negative rejection

前置条件：先用 run_add_client.py 以相同 --data / --run-id 导入数据。

用法：
    python scripts/evaluate.py                        # validation 集
    python scripts/evaluate.py --data data/coding_memory_benchmark_rich/dataset.jsonl
"""

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

import dataset_adapter as da
from app import health

# 目录 reports/ 下存放评测报告，按 run_id 命名
REPORT_DIR = Path(__file__).resolve().parents[1] / "reports"

# 评测指标

# recall_at_k
# 召回率，表示在前 k 个结果中，有多少个是相关的
def recall_at_k(ranked_ids: list, gold: set, k: int) -> float:
    if not gold:
        return 0.0
    hits = sum(1 for i in ranked_ids[:k] if i in gold)
    return hits / len(gold)
# mrr
# 平均 reciprocal rank，表示在前 k 个结果中，第一个相关结果的 reciprocal rank 的平均值
def mrr(ranked_ids: list, gold: set) -> float:
    for i, id in enumerate(ranked_ids):
        if id in gold:
            return 1 / (i + 1)
    return 0.0

# ndcg_at_k
# normalized discounted cumulative gain at k，表示在前 k 个结果中，相关结果的 discounted cumulative gain 的平均值
def ndcg_at_k(ranked_ids: list, gold: set, k: int) -> float:
    dcg = 0.0
    idcg = 0.0
    for i, id in enumerate(ranked_ids[:k]):
        if id in gold:
            dcg += 1 / math.log2(i + 2)
        if i < len(gold):
            idcg += 1 / math.log2(i + 2)
    return dcg / idcg if idcg > 0 else 0.0
# noise_at_k
# 噪声率，表示在前 k 个结果中，有多少个是噪声的
def noise_at_k(ranked_ids: list, gold: set, k: int) -> float:
    top = ranked_ids[:k]
    if not top:
        return 0.0
    return sum(1 for i in top if i not in gold) / len(top)

def map_hits_to_ids(hits: list, pool_by_content: dict) -> list:
    """把 /search 返回的 content 精确映射回 memory_pool id（每个 id 只用一次）。"""
    ranked = []
    for h in hits:
        candidates = pool_by_content.get(h.get("content"))
        if candidates:
            ranked.append(candidates.pop(0))
        else:
            ranked.append(f"<unmapped:{h.get('id')}>")
    return ranked

# ---------------------------------------------------------------------------
# 评测主流程
# ---------------------------------------------------------------------------
def evaluate_condition(base_url: str, samples: list, condition: str,
                       run_id: str, top_k: int) -> dict:
    per_task = [] # 每个任务的评测结果
    agg = defaultdict(float)
    for i, sample in enumerate(samples,1):
        allowed = set(sample["conditions"].get(condition) or [])# 允许的 memory_pool id
        pool = [m for m in sample["memory_pool"] if m["id"] in allowed]#  memory_pool 中的样本
        pool_by_content = defaultdict(list)
        for m in pool:
            pool_by_content[m["content"]].append(m["id"])# 按 content 分组，方便映射回 id

        gold = set(sample["gold_memory_ids"]) & allowed # gold memory_pool id
        hard_neg = set(sample["hard_negative_ids"]) & allowed # hard negative memory_pool id
        # 构造 /search 请求
        search_req = da.build_search_request(sample, condition, run_id, top_k)
        resp = requests.post(base_url.rstrip("/") + "/search",
                             json=search_req, timeout=60)
        resp.raise_for_status()
        hits = resp.json().get("data", [])# 取出结构
        ranked_ids = map_hits_to_ids(hits, pool_by_content)
        # 计算指标
        metrics = {
            "task_id": sample["task_id"],
            "category": sample["category"],
            "n_pool": len(pool),
            "n_returned": len(hits),
            "recall@5": recall_at_k(ranked_ids, gold, 5),
            "recall@10": recall_at_k(ranked_ids, gold, 10),
            "mrr": mrr(ranked_ids, gold),
            "ndcg@10": ndcg_at_k(ranked_ids, gold, 10),
            "noise@10": noise_at_k(ranked_ids, gold, 10),
            "hn_in_top10": float(any(m in hard_neg for m in ranked_ids[:10])),
            "top10_ids": ranked_ids[:10],
        }
        per_task.append(metrics)

        for key in ("recall@5", "recall@10", "mrr", "ndcg@10",
                    "noise@10", "hn_in_top10"):
            agg[key] += metrics[key]
        print(f"[{condition}] ({i}/{len(samples)}) {sample['task_id']} "
              f"R@5={metrics['recall@5']:.2f} MRR={metrics['mrr']:.2f} "
              f"nDCG@10={metrics['ndcg@10']:.2f}")
    n = max(len(samples), 1)
    summary = {key: round(val / n, 4) for key, val in agg.items()}
    summary["hn_rejection@10"] = round(1.0 - summary.pop("hn_in_top10"), 4)
    summary["n_tasks"] = len(samples)
    return {"summary": summary, "per_task": per_task}

def main():
    # 设置参数
    parser = argparse.ArgumentParser(description="local retrieval evaluation")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--data", default=None,
                        help="数据集 jsonl 路径（默认 validation.jsonl，需与导入一致）")
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", default=None, help="报告输出路径（json）")
    args = parser.parse_args()

    try:
        health = requests.get(args.base_url.rstrip("/") + "/health",timeout=5).json()
        print(f"service healthy: {health}")
    except Exception as e:  # noqa: BLE001
        print(f"cannot reach memory service at {args.base_url}: {e}\n"
              f"please make sure the service is running and reachable.", file=sys.stderr)
        sys.exit(1)
    samples = da.load_dataset(args.data)
    if args.limit:
        samples = samples[:args.limit]
    report = {"run_id": args.run_id, "data": str(args.data or da.VALIDATION_PATH),
              "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "conditions": {}}

    t0 = time.time()
    for cond in da.CONDITIONS:
        print(f"\n===== condition: {cond} =====")
        report["conditions"][cond] = evaluate_condition(
            args.base_url, samples, cond, args.run_id, args.top_k)
    # 汇总输出
    print("\n" + "=" * 62)
    print(f"{'Metric':<20}" + "".join(f"{c:>15}" for c in da.CONDITIONS))
    print("-" * 62)
    metric_keys = ["recall@5", "recall@10", "mrr", "ndcg@10",
                   "noise@10", "hn_rejection@10"]
    for key in metric_keys:
        row = "".join(f"{report['conditions'][c]['summary'][key]:>15.4f}"
                      for c in da.CONDITIONS)
        print(f"{key:<20}{row}")
    print("=" * 62)
    print(f"elapsed: {time.time() - t0:.1f}s")

    REPORT_DIR.mkdir(exist_ok=True)
    out_path = Path(args.out) if args.out else REPORT_DIR / \
                                               f"eval_{args.run_id}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"report saved: {out_path}")

if __name__ == "__main__":
    main()
