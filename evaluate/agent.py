# -*- coding: utf-8 -*-
"""本地解题 Agent：OpenAI 兼容接口 + 记忆检索 + 仓库读写/执行工具。"""

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from openai import OpenAI

import env


SYSTEM_PROMPT = """You are an autonomous software engineer fixing a real GitHub issue.
The repository is the current working directory. Work by inspecting the code and making
minimal, correct edits. Do not edit test files: the evaluation applies its own test patch.

You have a memory_search tool containing historical engineering trajectories. It holds
another agent's exploration, NOT a guaranteed final patch: use it to locate relevant code,
then stop searching and reason for yourself. You may call memory_search at most 3 times.
Within your first 8 tool calls, make the source edit. Do not spend the budget repeating
similar searches.

The git history only contains the pre-fix snapshot: there is no gold patch or hidden
answer to recover. Implement the fix from the issue description and the existing tests.

After editing, verify with a short reproduction script or a focused test, then call finish
with a one-paragraph summary.
"""

MAX_TOOL_OUTPUT = 12000
MAX_READ_CHARS = 24000
MAX_SEARCH_OUTPUT = 30000
MAX_HISTORY_CHARS = 240000
KEEP_RECENT_MESSAGES = 12


def _clip(text, limit=MAX_TOOL_OUTPUT):
    if len(text) <= limit:
        return text
    head = text[: limit // 3]
    tail = text[-(limit - len(head) - 20):]
    return f"{head}\n... [truncated {len(text) - limit} chars] ...\n{tail}"


def _safe_path(repo, rel_path):
    target = (repo / rel_path).resolve()
    repo_root = repo.resolve()
    if target != repo_root and not target.is_relative_to(repo_root):
        raise ValueError(f"路径越界：{rel_path}")
    return target


def _read_file(repo, args):
    path = _safe_path(repo, args["path"])
    if not path.is_file():
        return f"ERROR: 不是文件：{args['path']}"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, int(args.get("start_line", 1)))
    end = int(args.get("end_line", start + 399))
    end = min(end, len(lines))
    numbered = [f"{i:5d}\t{lines[i - 1]}" for i in range(start, end + 1)]
    text = "\n".join(numbered)
    note = ""
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS]
        note = "\n... [已截断，请用 start_line/end_line 继续]"
    return f"{args['path']} ({len(lines)} lines)\n{text}{note}"


def _list_dir(repo, args):
    path = _safe_path(repo, args.get("path", "."))
    if not path.is_dir():
        return f"ERROR: 不是目录：{args.get('path', '.')}"
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        if child.name in {"__pycache__", ".git", ".pytest_cache"}:
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))
    return "\n".join(entries[:200]) or "(empty)"


def _grep(repo, args):
    try:
        pattern = re.compile(args["pattern"])
    except re.error as exc:
        return f"ERROR: 正则无效：{exc}"
    root = _safe_path(repo, args.get("path", "."))
    glob = args.get("glob") or "*.py"
    matches = []
    files = [root] if root.is_file() else root.rglob("*")
    for path in files:
        if not path.is_file() or not fnmatch.fnmatch(path.name, glob):
            continue
        if {"__pycache__", ".git", ".pytest_cache"} & set(path.parts):
            continue
        try:
            for lineno, line in enumerate(
                    path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if pattern.search(line):
                    rel = path.relative_to(repo)
                    matches.append(f"{rel}:{lineno}: {line.strip()[:300]}")
                    if len(matches) >= 200:
                        return "\n".join(matches) + "\n... [达到 200 条上限]"
        except OSError:
            continue
    return "\n".join(matches) or "no matches"


def _write_file(repo, args):
    path = _safe_path(repo, args["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    content = args.get("content", "")
    path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {args['path']}"


def _edit_file(repo, args):
    path = _safe_path(repo, args["path"])
    if not path.is_file():
        return f"ERROR: 不是文件：{args['path']}"
    text = path.read_text(encoding="utf-8")
    old = args["old_string"]
    new = args["new_string"]
    count = text.count(old)
    if count == 0:
        return "ERROR: old_string 在文件中不存在"
    if count > 1:
        return f"ERROR: old_string 出现 {count} 次，请补充上下文使其唯一"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"replaced 1 occurrence in {args['path']}"


NETWORK_RE = re.compile(
    r"(https?://|\b(?:curl|wget)\b|\bpip[0-9]?\s+(?:install|download|index)\b|"
    r"\bgit\s+(?:clone|fetch|pull|remote)\b|\b(?:urllib|httpx|aiohttp)\b|"
    r"\brequests\.(?:get|post|put|delete|request|Session)\b|"
    r"\bsocket\.(?:socket|create_connection)\b)",
    re.I)


def _run_command(repo, args):
    command = args["command"]
    if NETWORK_RE.search(command):
        return ("ERROR: network access is disabled in the evaluation sandbox. "
                "Work from the issue, the local code and the memory evidence.")
    timeout = min(int(args.get("timeout_seconds", 180)), 300)
    run_env = {**os.environ}
    run_env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + run_env.get("PATH", "")
    run_env["PYTHONIOENCODING"] = "utf-8"
    # 双保险：即使命令绕过关键词，HTTP 客户端也会撞上不可达代理
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        run_env[key] = "http://127.0.0.1:9"
    run_env["NO_PROXY"] = run_env["no_proxy"] = ""
    started = time.time()
    try:
        bash = shutil.which("bash")
        if bash:
            proc = subprocess.run(
                [bash, "-c", command], cwd=repo, capture_output=True, text=True,
                timeout=timeout, env=run_env, encoding="utf-8", errors="replace")
        else:
            proc = subprocess.run(
                command, cwd=repo, shell=True, capture_output=True, text=True,
                timeout=timeout, env=run_env, encoding="utf-8", errors="replace")
        output = (proc.stdout or "") + (proc.stderr or "")
        return f"exit_code={proc.returncode} duration={time.time()-started:.1f}s\n{_clip(output)}"
    except subprocess.TimeoutExpired:
        return f"ERROR: 命令超时（{timeout}s）"


def _search_memory(memory, args):
    query = args["query"]
    top_k = max(1, min(int(args.get("top_k", 5)), 20))
    results = memory.search(query, top_k=top_k)
    if not results:
        return "no memory results"
    parts = []
    for rank, item in enumerate(results, 1):
        header = (f"[{rank}] score={item['score']:.4f} id={item['id']} "
                  f"created_at={item.get('created_at')}")
        parts.append(header + "\n" + item["content"])
    return _clip("\n\n".join(parts), MAX_SEARCH_OUTPUT)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search historical engineering trajectories for relevant experience.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "description": "1-20, default 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Regex search inside repository files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "glob": {"type": "string", "default": "*.py"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one unique occurrence of old_string with new_string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the repository (e.g. pytest).",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "default": 180},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call when the fix is complete.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]


def _dispatch(repo, memory, name, args, searches):
    if name == "search_memory":
        if len(searches) >= 3:
            return ("Memory search budget exhausted (3 queries). Use the code "
                    "you have already seen and implement the fix."), False
        result = _search_memory(memory, args)
        searches.append({
            "query": args.get("query"),
            "top_k": args.get("top_k", 5),
            "result_ids": re.findall(r"id=(mem_[0-9a-f]+)", result),
            "preview": result[:600],
        })
        return result, False
    if name == "list_dir":
        return _list_dir(repo, args), False
    if name == "read_file":
        return _read_file(repo, args), False
    if name == "grep":
        return _grep(repo, args), False
    if name == "write_file":
        return _write_file(repo, args), False
    if name == "edit_file":
        return _edit_file(repo, args), False
    if name == "run_command":
        return _run_command(repo, args), False
    if name == "finish":
        return args.get("summary", ""), True
    return f"ERROR: unknown tool {name}", False


def _trim_history(messages):
    """上下文超预算时，把较早的大块工具结果替换成占位符。"""
    total = sum(len(m["content"]) for m in messages
                if isinstance(m.get("content"), str))
    if total <= MAX_HISTORY_CHARS:
        return
    protected = set(range(max(0, len(messages) - KEEP_RECENT_MESSAGES),
                          len(messages)))
    for index, message in enumerate(messages):
        if total <= MAX_HISTORY_CHARS:
            break
        content = message.get("content")
        if (message.get("role") != "tool" or index in protected
                or not isinstance(content, str) or len(content) < 500):
            continue
        replacement = "[earlier tool result omitted to save context]"
        total -= len(content) - len(replacement)
        message["content"] = replacement


def _chat(client, cfg, messages):
    last_error = None
    for attempt in range(1, 4):
        try:
            return client.chat.completions.create(
                model=cfg["model"],
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0,
                max_tokens=cfg["max_tokens"],
            )
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 3:
                time.sleep(2 * attempt)
    raise RuntimeError(f"LLM 调用失败：{last_error}")


def run(repo, task, memory, cfg=None, max_steps=40, transcript_path=None):
    cfg = cfg or env.llm_config()
    for key in ("api_key", "base_url", "model"):
        if not cfg.get(key):
            raise ValueError(f"缺少模型配置：{key}（检查 .env 或 --model）")

    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"],
                    timeout=cfg["timeout"])
    user_prompt = (
        f"Instance: {task['instance_id']}\nRepo: {task['repo']}\n\n"
        f"Problem statement:\n{task['problem_statement']}\n\n"
        "Start by locating the relevant source code. The repository root is the "
        "current working directory; all tool paths are relative to it."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    transcript, searches = [], []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    stop_reason, final = "max_steps", ""
    started = time.time()
    if transcript_path:
        Path(transcript_path).parent.mkdir(parents=True, exist_ok=True)
        Path(transcript_path).write_text("", encoding="utf-8")

    def _emit(entry):
        transcript.append(entry)
        if transcript_path:
            with open(transcript_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    for step in range(1, max_steps + 1):
        _trim_history(messages)
        step_started = time.time()
        response = _chat(client, cfg, messages)
        if getattr(response, "usage", None):
            for key in usage:
                usage[key] += getattr(response.usage, key, 0) or 0
        message = response.choices[0].message
        tool_calls = list(message.tool_calls or [])
        content = message.content or ""

        assistant_entry = {
            "step": step,
            "llm_duration_s": round(time.time() - step_started, 1),
            "content": content,
            "tool_calls": [],
        }
        api_message = {"role": "assistant", "content": content}
        if tool_calls:
            api_message["tool_calls"] = [
                {"id": call.id, "type": "function",
                 "function": {"name": call.function.name,
                              "arguments": call.function.arguments}}
                for call in tool_calls
            ]
        messages.append(api_message)

        if not tool_calls:
            final = content
            stop_reason = "assistant_message"
            _emit(assistant_entry)
            break

        finished = False
        for call in tool_calls:
            name = call.function.name
            call_finished = False
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
                result = "ERROR: 工具参数不是合法 JSON"
            else:
                try:
                    result, call_finished = _dispatch(
                        repo, memory, name, args, searches)
                except Exception as exc:  # noqa: BLE001
                    result = f"ERROR: {type(exc).__name__}: {exc}"
            messages.append({
                "role": "tool", "tool_call_id": call.id, "content": result})
            assistant_entry["tool_calls"].append({
                "name": name, "args": args, "result": _clip(result, 4000)})
            if call_finished:
                final = args.get("summary", "")
                finished = True
        _emit(assistant_entry)
        if finished:
            stop_reason = "finish"
            break

    return {
        "stop_reason": stop_reason,
        "final": final,
        "steps": len(transcript),
        "usage": usage,
        "duration_s": round(time.time() - started, 1),
        "transcript": transcript,
        "searches": searches,
        "model": cfg["model"],
    }
