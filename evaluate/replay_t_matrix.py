# -*- coding: utf-8 -*-
"""T1–T8 检索矩阵回放：同一批 query 在 V2.1 / V3.0-lite 两种模式下对照。

记忆来自 data_store/official_requests.jsonl 的官方 /add 请求（本地重建，禁网）。
验收线（对齐 V3.0 计划 §10）：
  T1 正对照：to_pandas 查询必须在 top-1 返回 remove_indices 修复证据；
  T2 负对照：required-columns 查询的 top-1 绝不出现 remove_indices；
  T6/T7 无答案查询：只允许 source_only / related_only 或空结果。

用法：
    evaluate/.venv/Scripts/python.exe evaluate/replay_t_matrix.py
产物：
    evaluate/reports/v3_t_matrix.json   机读版（两模式完整 top-1 内容 + 检查结果）
    stdout                              人读汇总表
"""

import json
import os
import re
import time
from datetime import datetime, timezone

import env
import memory_env


TOP_K = 20

# expect/forbid 都对 top-1 content 判定（大小写不敏感）；expect_status 仅 v3_0 模式判
T_MATRIX = [
    {"id": "T1", "condition": "relevant",
     "query": "BinnedTimeSeries to_pandas KeyError time_bin_start remove_indices",
     "expect": ["remove_indices"], "forbid": [],
     "expect_status": {"verified_solution", "patch_available", "summary_only"},
     "note": "正对照：历史里确有 to_pandas 修复，必须交付答案性证据"},
    {"id": "T2", "condition": "relevant",
     "query": "TimeSeries required columns invalid expected first columns",
     "expect": ["_check_required_columns"], "forbid": ["remove_indices"],
     "expect_status": {"source_only", "related_only"},
     "note": "负对照：记忆里没有本题答案，只能给源码，禁止 to_pandas 补丁混入"},
    {"id": "T3", "condition": "noisy",
     "query": "_relax_required_columns _check_required_columns",
     "expect": ["_check_required_columns"], "forbid": ["progressbar", "furlong"],
     "expect_status": {"source_only", "related_only", "summary_only"},
     "note": "符号查询不得混入 ProgressBar/furlong 轨迹"},
    {"id": "T4", "condition": "noisy",
     "query": "ProgressBar terminal resize corrupted output",
     "expect": ["console.py"], "forbid": ["timeseries"],
     "expect_status": None,
     "note": "无关轨迹自身的问题应命中自己的文件"},
    {"id": "T5", "condition": "noisy",
     "query": "add furlong imperial unit",
     "expect": ["furlong"], "forbid": [],
     "expect_status": None,
     "note": "furlong 轨迹查询命中自己的证据"},
    {"id": "T6", "condition": "relevant",
     "query": "When Jon has lost his job as a banker?",
     "expect": [], "forbid": ["remove_indices", "_check_required_columns"],
     "expect_status": {"source_only", "related_only"},
     "empty_ok": True,
     "note": "无关问题：允许空结果，禁止包装成 solution"},
    {"id": "T7", "condition": "relevant",
     "query": "missing required column",
     "expect": [], "forbid": ["remove_indices"],
     "expect_status": {"source_only", "related_only"},
     "empty_ok": True,
     "note": "短查询：允许 source_only 低置信度，不得猜测答案"},
    {"id": "T8", "condition": "relevant",
     "query": "TimeSeries required columns invalid expected first columns",
     "expect": [], "forbid": [],
     "expect_status": None,
     "dedup": True,
     "note": "与 T2 同 query：检查 top-1 内容无重复正文块"},
]

_STATUS_RE = re.compile(r"\[solution_status=(\w+)")


def _status_of(content):
    match = _STATUS_RE.match(content or "")
    return match.group(1) if match else None


def _max_line_repeat(content):
    """top-1 内容里同一长行出现的最大次数（>1 即存在重复正文）。"""
    counts = {}
    for line in (content or "").splitlines():
        if len(line) >= 40:
            counts[line] = counts.get(line, 0) + 1
    return max(counts.values()) if counts else 0


def _check(case, results, mode):
    checks = {}
    top1 = results[0]["content"] if results else ""
    if case["expect"]:
        checks["expect"] = all(e.lower() in top1.lower() for e in case["expect"])
    if case["forbid"]:
        checks["forbid"] = not any(f.lower() in top1.lower()
                                   for f in case["forbid"])
    status = _status_of(top1)
    if mode == "v3_0" and case.get("expect_status"):
        checks["status"] = (status in case["expect_status"]
                            or (not results and case.get("empty_ok", False)))
    if case.get("dedup"):
        checks["dedup"] = _max_line_repeat(top1) <= 1
    return checks, status


def _run_case(memory, case, mode):
    started = time.perf_counter()
    results = memory.search(case["query"], top_k=TOP_K)
    elapsed_ms = (time.perf_counter() - started) * 1000
    top1 = results[0] if results else None
    checks, status = _check(case, results, mode)
    return {
        "id": case["id"],
        "condition": case["condition"],
        "query": case["query"],
        "mode": mode,
        "n_results": len(results),
        "top1_id": top1["id"] if top1 else None,
        "top1_score": top1["score"] if top1 else None,
        "top1_chars": len(top1["content"]) if top1 else 0,
        "status": status,
        "checks": checks,
        "all_pass": all(checks.values()) if checks else None,
        "latency_ms": round(elapsed_ms, 1),
        "top1_content": top1["content"] if top1 else "",
        "result_ids": [r["id"] for r in results],
    }


def run_mode(mode):
    os.environ["RETRIEVAL_MODE"] = mode
    requests = env.load_official_requests()
    envs = {}
    for condition in ("relevant", "noisy"):
        user_id, _ = memory_env.select_user(requests, condition)
        store_path = env.WORK_ROOT / "t_matrix" / f"{mode}_{condition}.jsonl"
        envs[condition] = memory_env.build(
            condition, user_id=user_id, store_path=store_path)
    return [_run_case(envs[case["condition"]], case, mode)
            for case in T_MATRIX]


def main():
    modes = ["v2_1", "v3_0"]
    report = {
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "top_k": TOP_K,
        "matrix": [{**case,
                    "expect_status": (sorted(case["expect_status"])
                                      if case.get("expect_status") else None)}
                   for case in T_MATRIX],
        "modes": {},
    }
    for mode in modes:
        report["modes"][mode] = run_mode(mode)

    out = env.REPORTS_DIR / "v3_t_matrix.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    for mode in modes:
        print(f"\n== RETRIEVAL_MODE={mode} ==")
        print(f"{'id':<4}{'n':>3} {'top1_id':<18}{'score':>8}{'chars':>7}"
              f" {'status':<18}{'checks':<28}{'ms':>7}")
        for row in report["modes"][mode]:
            checks = ",".join(
                f"{k}:{'Y' if v else 'N'}" for k, v in row["checks"].items())
            print(f"{row['id']:<4}{row['n_results']:>3} "
                  f"{(row['top1_id'] or '-'):<18}"
                  f"{(row['top1_score'] or 0):>8.3f}{row['top1_chars']:>7}"
                  f" {(row['status'] or '-'):<18}{checks or '-':<28}"
                  f"{row['latency_ms']:>7.1f}")

    # 验收线：两种模式下 T1 expect 与 T2 forbid 都必须成立
    verdict = True
    for mode in modes:
        rows = {row["id"]: row for row in report["modes"][mode]}
        t1 = rows["T1"]["checks"].get("expect")
        t2 = rows["T2"]["checks"].get("forbid")
        ok = bool(t1) and bool(t2)
        verdict = verdict and ok
        print(f"\n[{mode}] T1 expect(remove_indices)={t1}  "
              f"T2 forbid(remove_indices)={t2}  -> {'PASS' if ok else 'FAIL'}")
    print(f"\nreport: {out}")
    print("VERDICT:", "PASS" if verdict else "FAIL")
    raise SystemExit(0 if verdict else 1)


if __name__ == "__main__":
    main()
