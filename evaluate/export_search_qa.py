# -*- coding: utf-8 -*-
"""导出官方 /search 的问题 + 当前 MemoryStore 的实际回答。

用法：
    evaluate/.venv/Scripts/python.exe evaluate/export_search_qa.py
产物：
    evaluate/official-search-qa.md          人读版（问题 + 完整返回内容）
    evaluate/reports/official_search_qa.jsonl  机读版（完整 JSON）
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import env
import memory_env


def _condition_of(requests, user_id):
    relevant_user, _ = memory_env.select_user(requests, "relevant")
    noisy_user, _ = memory_env.select_user(requests, "noisy")
    if user_id == relevant_user:
        return "relevant"
    if user_id == noisy_user:
        return "noisy"
    return "other"


def _build_envs(requests, users):
    envs = {}
    for user_id in users:
        condition = _condition_of(requests, user_id)
        if condition == "other":
            envs[user_id] = None
            continue
        store_path = env.WORK_ROOT / "export" / f"{user_id[:16]}.jsonl"
        envs[user_id] = memory_env.build(
            condition, user_id=user_id, store_path=store_path)
    return envs


def _fence_for(text):
    longest = 0
    run = 0
    for char in text:
        if char == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return "`" * max(4, longest + 1)


def _render(requests, searches, envs, max_chars, format_note, answer_label):
    lines = [
        "# 官方 /search 问题与当前代码回答",
        "",
        f"> 生成时间：{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}  ",
        "> 回答来源：`src/memory_engine.py` 的 `MemoryStore.search()`，"
        "记忆由 `data_store/official_requests.jsonl` 里的官方 `/add` 请求重建。  ",
        f"> {format_note}",
        "",
        "## 索引",
        "",
        "| # | user 条件 | 问题 | 返回条数 | Top-1 id | Top-1 score |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for index, item in enumerate(searches, 1):
        body = item.get("body") or {}
        user_id = body.get("user_id", "")
        results = item["_results"]
        query = (body.get("query") or "").replace("|", "\\|")
        top = results[0] if results else None
        lines.append(
            f"| {index} | {_condition_of(requests, user_id)} | "
            f"{query[:70]} | {len(results)} | "
            f"{top['id'] if top else '-'} | "
            f"{top['score']:.4f} |" if top else
            f"| {index} | {_condition_of(requests, user_id)} | "
            f"{query[:70]} | 0 | - | - |")

    lines.append("")
    for index, item in enumerate(searches, 1):
        body = item.get("body") or {}
        user_id = body.get("user_id", "")
        condition = _condition_of(requests, user_id)
        top_k = body.get("top_k", 5)
        query = body.get("query") or ""
        results = item["_results"]

        lines.extend([
            f"## Search {index:02d}",
            "",
            f"- 时间：{item.get('ts', '-')}",
            f"- user：`{user_id}`（{condition}）",
            f"- top_k：{top_k}",
            "",
            "**问题：**",
            "",
        ])
        lines.extend(f"> {line}" for line in query.splitlines() or [""])
        lines.extend(["", f"**{answer_label}：** {len(results)} 条", ""])

        if not results:
            lines.append(
                "空列表。该 user 在官方数据里没有对应的 `/add` 记忆，"
                "`MemoryStore.search()` 直接返回 `[]`。")
            lines.append("")
            continue

        for rank, result in enumerate(results, 1):
            content = result["content"]
            if max_chars and len(content) > max_chars:
                content = content[:max_chars] + (
                    f"\n... [已截断，原文 {len(result['content'])} 字符；"
                    "完整内容见 JSONL] ...")
            fence = _fence_for(content)
            lines.extend([
                f"### 结果 {rank}",
                "",
                f"- id：`{result['id']}`",
                f"- score：{result['score']:.4f}",
                f"- created_at：{result.get('created_at')}",
                "",
                fence + "text",
                content,
                fence,
                "",
            ])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-chars", type=int, default=0,
                        help="单条 content 截断长度；0 表示不截断")
    parser.add_argument(
        "--output", default=str(env.EVAL_ROOT / "official-search-qa.md"))
    # V1.1 基线报告也要能原样重跑；V2.1 用 --jsonl 指向独立文件，不覆盖基线
    parser.add_argument("--jsonl", default=str(
        env.REPORTS_DIR / "official_search_qa.jsonl"))
    parser.add_argument(
        "--format-note",
        default="每条 content 前带 `[role]`，是当前 V1.1 返回格式。")
    parser.add_argument("--answer-label", default="当前代码的回答")
    args = parser.parse_args()

    requests = env.load_official_requests()
    searches = [item for item in requests
                if item.get("path") == "/search"]
    if not searches:
        raise SystemExit("official_requests.jsonl 里没有 /search 请求")

    users = sorted({(item.get("body") or {}).get("user_id") for item in searches})
    envs = _build_envs(requests, users)

    for item in searches:
        body = item.get("body") or {}
        user_id = body.get("user_id", "")
        memory = envs.get(user_id)
        if memory is None:
            item["_results"] = []
            continue
        top_k = max(1, min(int(body.get("top_k") or 5), 60))
        item["_results"] = memory.search(body.get("query") or "", top_k=top_k)

    markdown = _render(requests, searches, envs, args.max_chars,
                       args.format_note, args.answer_label)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8")

    jsonl_path = Path(args.jsonl)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for index, item in enumerate(searches, 1):
            body = item.get("body") or {}
            record = {
                "index": index,
                "ts": item.get("ts"),
                "user_id": body.get("user_id"),
                "condition": _condition_of(requests, body.get("user_id")),
                "query": body.get("query"),
                "top_k": body.get("top_k"),
                "results": item["_results"],
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"markdown: {output} ({output.stat().st_size} bytes)")
    print(f"jsonl   : {jsonl_path} ({jsonl_path.stat().st_size} bytes)")
    print(f"searches: {len(searches)}")


if __name__ == "__main__":
    main()
