# -*- coding: utf-8 -*-
"""SWEContextBench 检索评测。

    run_add_client.py 导入经验池 -> 本脚本逐任务 /search -> 检索指标

每个 condition 对应服务里一个全局记忆空间（见 dataset_adapter.condition_user_id）：
    relevant  空间里只有被 gold 引用的经验      -> 检索上限
    noisy     空间里是全量经验（大量无关记忆）  -> 抗噪能力

gold 标签来自 Relationship 表；hard negative 数据集无官方标注，
用「同仓库且非 gold」近似，只影响 hn_rejection 一项。

用法：
    python run_add_client.py --split lite --run-id local
    python evaluate.py --split lite --run-id local
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

REPORT_DIR = Path(__file__).parent / "reports"

K_LIST = (1, 3, 5, 10)


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def recall_at_k(ranked_ids: list, gold: set, k: int) -> float:
    """前 k 个结果里命中的 gold 占全部 gold 的比例。"""
    if not gold:
        return 0.0
    return sum(1 for i in ranked_ids[:k] if i in gold) / len(gold)


def mrr(ranked_ids: list, gold: set) -> float:
    """第一个 gold 排在第几位；本数据集 gold 多为单条，这项最能反映排序质量。"""
    for i, id in enumerate(ranked_ids):
        if id in gold:
            return 1 / (i + 1)
    return 0.0


def ndcg_at_k(ranked_ids: list, gold: set, k: int) -> float:
    dcg = idcg = 0.0
    for i, id in enumerate(ranked_ids[:k]):
        if id in gold:
            dcg += 1 / math.log2(i + 2)
        if i < len(gold):
            idcg += 1 / math.log2(i + 2)
    return dcg / idcg if idcg > 0 else 0.0


def noise_at_k(ranked_ids: list, gold: set, k: int) -> float:
    top = ranked_ids[:k]
    if not top:
        return 0.0
    return sum(1 for i in top if i not in gold) / len(top)


def map_hits_to_ids(hits: list, content_to_id: dict, self_id: str) -> list:
    """把 /search 返回的 content 精确映射回经验 ID。

    查询任务本身可能同时存在于经验池里（SWEContextBench 有 1 例），
    命中自己会给出虚假的 rank 1，故直接丢弃。
    """
    ranked = []
    for h in hits:
        id = content_to_id.get(h.get("content"))
        if id is None:
            ranked.append(f"<unmapped:{h.get('id')}>")
        elif id != self_id:
            ranked.append(id)
    return ranked


# ---------------------------------------------------------------------------
# 单个 condition 的评测
# ---------------------------------------------------------------------------
def evaluate_condition(base_url: str, samples: list, memories: list,
                       condition: str, run_id: str, top_k: int) -> dict:
    allowed = {m["id"] for m in memories}
    content_to_id = {}
    dup = 0
    for m in memories:
        if m["content"] in content_to_id:
            dup += 1
        content_to_id.setdefault(m["content"], m["id"])
    if dup:
        print(f"  warn: {dup} 条记忆 content 完全重复，ID 映射可能错配")

    per_task = []
    agg = defaultdict(float)
    for i, sample in enumerate(samples, 1):
        gold = set(sample["gold_memory_ids"]) & allowed
        hard_neg = set(sample["hard_negative_ids"]) & allowed - gold

        payload = da.build_search_request(sample, condition, run_id, top_k)
        resp = requests.post(base_url.rstrip("/") + "/search",
                             json=payload, timeout=120)
        resp.raise_for_status()
        hits = resp.json().get("data", [])
        ranked_ids = map_hits_to_ids(hits, content_to_id, sample["task_id"])

        metrics = {
            "task_id": sample["task_id"],
            "repository": sample["repository"],
            "n_pool": len(memories),
            "n_gold": len(gold),
            "n_returned": len(ranked_ids),
            "mrr": mrr(ranked_ids, gold),
            "ndcg@10": ndcg_at_k(ranked_ids, gold, 10),
            "noise@10": noise_at_k(ranked_ids, gold, 10),
            "hn_in_top10": float(bool(hard_neg) and
                                 any(m in hard_neg for m in ranked_ids[:10])),
            "top10_ids": ranked_ids[:10],
        }
        for k in K_LIST:
            metrics[f"recall@{k}"] = recall_at_k(ranked_ids, gold, k)
        per_task.append(metrics)

        for key in [f"recall@{k}" for k in K_LIST] + ["mrr", "ndcg@10",
                                                      "noise@10", "hn_in_top10"]:
            agg[key] += metrics[key]
        print(f"[{condition}] ({i}/{len(samples)}) {sample['task_id']} "
              f"gold={sample['gold_memory_ids']} "
              f"R@1={metrics['recall@1']:.2f} R@10={metrics['recall@10']:.2f} "
              f"MRR={metrics['mrr']:.2f} top1={ranked_ids[0] if ranked_ids else '-'}")

    n = max(len(samples), 1)
    summary = {key: round(val / n, 4) for key, val in agg.items()}
    summary["hn_rejection@10"] = round(1.0 - summary.pop("hn_in_top10"), 4)
    summary["n_tasks"] = len(samples)
    summary["n_pool"] = len(memories)
    return {"summary": summary, "per_task": per_task}


def main():
    parser = argparse.ArgumentParser(description="SWEContextBench retrieval evaluation")
    parser.add_argument("--base-url", default="http://127.0.0.1:7878")
    parser.add_argument("--split", default="lite", choices=sorted(da.SPLITS))
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--conditions", nargs="+", default=list(da.CONDITIONS),
                        choices=list(da.CONDITIONS))
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", default=None, help="报告输出路径（json）")
    args = parser.parse_args()

    try:
        print("service:", requests.get(args.base_url.rstrip("/") + "/health",
                                       timeout=5).json())
    except Exception as exc:  # noqa: BLE001
        print(f"cannot reach memory service at {args.base_url}: {exc}\n"
              f"先启动：uvicorn app:app --host 0.0.0.0 --port 7878", file=sys.stderr)
        sys.exit(1)

    samples, pool = da.load_split(args.split)
    if args.limit:
        samples = samples[:args.limit]

    report = {"run_id": args.run_id, "split": args.split,
              "include_patch": da.INCLUDE_PATCH, "top_k": args.top_k,
              "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "conditions": {}}

    t0 = time.time()
    for condition in args.conditions:
        memories = da.condition_memories(condition, samples, pool)
        print(f"\n===== {condition}: pool={len(memories)} =====")
        report["conditions"][condition] = evaluate_condition(
            args.base_url, samples, memories, condition, args.run_id, args.top_k)

    keys = [f"recall@{k}" for k in K_LIST] + ["mrr", "ndcg@10", "noise@10",
                                              "hn_rejection@10"]
    print("\n" + "=" * 70)
    print(f"{'Metric':<18}" + "".join(f"{c:>16}" for c in args.conditions))
    print("-" * 70)
    for key in keys:
        row = "".join(f"{report['conditions'][c]['summary'][key]:>16.4f}"
                      for c in args.conditions)
        print(f"{key:<18}{row}")
    print("=" * 70)
    print(f"elapsed: {time.time() - t0:.1f}s")

    REPORT_DIR.mkdir(exist_ok=True)
    out_path = (Path(args.out) if args.out else
                REPORT_DIR / f"eval_swe_{args.split}_{args.run_id}_"
                             f"{time.strftime('%Y%m%d_%H%M%S')}.json")
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"report saved: {out_path}")


if __name__ == "__main__":
    main()
