# -*- coding: utf-8 -*-
"""本地评测入口：模拟官方的 Add/Search + 解题 Agent + 测试验证。

用法（必须用评测 venv 的 python）：
    evaluate/.venv/Scripts/python.exe evaluate/run_eval.py --mode oracle
    evaluate/.venv/Scripts/python.exe evaluate/run_eval.py --mode replay --memory noisy
    evaluate/.venv/Scripts/python.exe evaluate/run_eval.py --mode agent --memory relevant
"""

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone

import agent
import env
import memory_env
import sandbox


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id(instance_id, mode, condition):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}_{instance_id}_{mode}_{condition}"


def _save_report(run_id, report):
    env.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = env.REPORTS_DIR / f"{run_id}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def _print_verification(label, verification, report_path):
    print(f"[{label}] resolved={verification.get('resolved')} "
          f"counts={verification.get('counts', {})} "
          f"duration={verification.get('duration_s')}s "
          f"report={report_path}")
    for node in (verification.get("failed") or [])[:5]:
        print(f"  FAILED {node}")


def run_oracle(args, task):
    run_id = _run_id(task["instance_id"], "oracle", "gold")
    repo = sandbox.prepare_run(task["instance_id"], run_id)
    verification = sandbox.verify(
        repo, task, task["patch"], p2p_mode=args.p2p, timeout=args.timeout)
    report = {
        "run_id": run_id,
        "timestamp": _now(),
        "instance_id": task["instance_id"],
        "repo": task["repo"],
        "base_commit": task["base_commit"],
        "mode": "oracle",
        "oracle_patch_chars": len(task["patch"]),
        "verification": verification,
    }
    return run_id, report


def run_replay(args, task):
    memory = memory_env.build(args.memory, user_id=args.user_id)
    run_id = _run_id(task["instance_id"], "replay", args.memory)
    requests = env.load_official_requests()
    queries = [
        item for item in requests
        if item.get("path") == "/search"
        and (item.get("body") or {}).get("user_id") == memory.user_id
    ]
    records = []
    for item in queries:
        body = item.get("body") or {}
        query = body.get("query") or ""
        top_k = max(1, min(int(body.get("top_k") or 5), 20))
        results = memory.search(query, top_k=top_k)
        ids = [result["id"] for result in results]
        ranks = [i + 1 for i, mid in enumerate(ids)
                 if mid in memory.relevant_ids]
        records.append({
            "ts": item.get("ts"),
            "query": query,
            "top_k": top_k,
            "hit_relevant": bool(ranks),
            "first_relevant_rank": ranks[0] if ranks else None,
            "results": [
                {
                    "id": r["id"],
                    "score": round(r["score"], 4),
                    "relevant": r["id"] in memory.relevant_ids,
                    "preview": r["content"][:300],
                }
                for r in results
            ],
        })
    hits = sum(1 for record in records if record["hit_relevant"])
    report = {
        "run_id": run_id,
        "timestamp": _now(),
        "instance_id": task["instance_id"],
        "mode": "replay",
        "memory": memory.describe(),
        "query_count": len(records),
        "hit_count": hits,
        "hit_rate": round(hits / len(records), 4) if records else None,
        "queries": records,
    }
    return run_id, report


def run_agent(args, task):
    _cfg = env.llm_config(args.model)
    memory = memory_env.build(args.memory, user_id=args.user_id)
    run_id = _run_id(task["instance_id"], "agent", args.memory)
    repo = sandbox.prepare_run(task["instance_id"], run_id)
    env.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = env.REPORTS_DIR / f"{run_id}.transcript.jsonl"

    result = agent.run(repo, task, memory, cfg=_cfg, max_steps=args.steps,
                       transcript_path=transcript_path)
    model_patch = sandbox.capture_patch(repo)
    patch_path = env.REPORTS_DIR / f"{run_id}.model.patch"
    patch_path.write_text(model_patch, encoding="utf-8")

    verification = sandbox.verify(
        repo, task, model_patch, p2p_mode=args.p2p, timeout=args.timeout,
        keep_patch_path=repo.parent / "model.patch")

    report = {
        "run_id": run_id,
        "timestamp": _now(),
        "instance_id": task["instance_id"],
        "repo": task["repo"],
        "base_commit": task["base_commit"],
        "mode": "agent",
        "model": _cfg["model"],
        "memory": memory.describe(),
        "agent": {
            key: result[key]
            for key in ("stop_reason", "final", "steps", "usage",
                        "duration_s", "searches", "model")
        },
        "patch": {
            "chars": len(model_patch),
            "path": str(patch_path),
            "transcript": str(transcript_path),
        },
        "verification": verification,
    }
    return run_id, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="astropy__astropy-13033")
    parser.add_argument("--mode", default="agent",
                        choices=["oracle", "replay", "agent", "all"])
    parser.add_argument("--memory", default="relevant",
                        choices=["relevant", "noisy"])
    parser.add_argument("--user-id", default=None,
                        help="覆盖按 relevant/noisy 自动选择的 user")
    parser.add_argument("--model", default=None,
                        help="覆盖 .env 的 LLM_MODE")
    parser.add_argument("--steps", type=int, default=40)
    parser.add_argument("--p2p", default="all", choices=["all", "none"])
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    task = env.load_task(args.instance)
    env.ensure_template(args.instance)
    cfg = env.llm_config(args.model)
    print(f"instance={args.instance} mode={args.mode} memory={args.memory} "
          f"p2p={args.p2p} model={cfg['model'] or '<none>'}")

    modes = ["oracle", "replay", "agent"] if args.mode == "all" else [args.mode]
    failures = 0
    for mode in modes:
        started = time.time()
        try:
            if mode == "oracle":
                run_id, report = run_oracle(args, task)
            elif mode == "replay":
                run_id, report = run_replay(args, task)
            else:
                run_id, report = run_agent(args, task)
            report["wall_time_s"] = round(time.time() - started, 1)
            path = _save_report(run_id, report)
            if mode == "replay":
                print(f"[replay] queries={report['query_count']} "
                      f"hit_rate={report['hit_rate']} "
                      f"memory={report['memory']['condition']} "
                      f"user={report['memory']['user_id'][:12]}... "
                      f"report={path}")
            elif mode == "agent":
                _print_verification("agent", report["verification"], path)
                print(f"  agent: stop={report['agent']['stop_reason']} "
                      f"steps={report['agent']['steps']} "
                      f"tokens={report['agent']['usage']['total_tokens']} "
                      f"patch={report['patch']['chars']} chars")
            else:
                _print_verification("oracle", report["verification"], path)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[{mode}] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            traceback.print_exc()
            error_id = _run_id(task["instance_id"], mode, args.memory)
            _save_report(error_id, {
                "run_id": error_id,
                "timestamp": _now(),
                "instance_id": task["instance_id"],
                "mode": mode,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            })
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
