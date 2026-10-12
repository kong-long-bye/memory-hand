# -*- coding: utf-8 -*-
"""把 SWEContextBench 经验池写入记忆服务（/add）。

每个 condition 写进各自的全局 user_id 空间，跨任务共享，这是该数据集的考点所在。
request_id 带 run_id + 经验 ID，重复执行由服务端幂等跳过。

用法：
    python run_add_client.py --split lite --run-id local
"""
import argparse
import sys
import time

import requests

import dataset_adapter as da


def main():
    parser = argparse.ArgumentParser(description="ingest SWEContextBench experience pool")
    parser.add_argument("--base-url", default="http://127.0.0.1:7878")
    parser.add_argument("--split", default="lite", choices=sorted(da.SPLITS))
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--conditions", nargs="+", default=list(da.CONDITIONS),
                        choices=list(da.CONDITIONS))
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    try:
        print("service:", requests.get(base + "/health", timeout=5).json())
    except Exception as exc:  # noqa: BLE001
        print(f"cannot reach memory service at {base}: {exc}", file=sys.stderr)
        sys.exit(1)

    samples, pool = da.load_split(args.split)
    print(f"split={args.split} tasks={len(samples)} pool={len(pool)} "
          f"include_patch={da.INCLUDE_PATCH}")

    for condition in args.conditions:
        memories = da.condition_memories(condition, samples, pool)
        requests_ = da.build_add_requests(condition, args.run_id, memories)
        chars = sum(len(m["content"]) for m in memories)
        print(f"\n===== {condition}: user_id="
              f"{da.condition_user_id(condition, args.run_id)} "
              f"memories={len(requests_)} avg_chars={chars // max(len(memories), 1)}")

        t0, failed = time.time(), 0
        for i, payload in enumerate(requests_, 1):
            try:
                resp = requests.post(base + "/add", json=payload, timeout=120)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  [{i}] FAILED {payload['request_id']}: {exc}")
                continue
            if i % 50 == 0 or i == len(requests_):
                print(f"  {i}/{len(requests_)} "
                      f"({time.time() - t0:.1f}s)")
        print(f"  done: {len(requests_) - failed} ok, {failed} failed, "
              f"{time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
