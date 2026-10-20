# -*- coding: utf-8 -*-
"""工作区准备、模型补丁提取、官方测试验证。"""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import env


CACHE_DIRS = {"__pycache__", ".pytest_cache"}


def _git(repo, *args, check=True):
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc


def _rmtree(path):
    def clear_readonly(func, target, _exc):
        os.chmod(target, 0o700)
        func(target)
    shutil.rmtree(path, onerror=clear_readonly)


def prepare_run(instance_id, run_id):
    """从 template 复制一份干净工作区。"""
    template = env.ensure_template(instance_id)
    run_repo = env.instance_dir(instance_id) / "runs" / run_id / "repo"
    run_repo.parent.mkdir(parents=True, exist_ok=True)
    if run_repo.exists():
        _rmtree(run_repo)
    shutil.copytree(
        template, run_repo,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    return run_repo


def _is_cache(rel_path):
    parts = Path(rel_path).parts
    return bool(CACHE_DIRS.intersection(parts)) or rel_path.endswith(".pyc")


def capture_patch(repo):
    """把 agent 的改动导出为 git diff；新文件用 add -N 纳入。"""
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard").stdout
    for rel in untracked.splitlines():
        if rel.strip() and not _is_cache(rel):
            _git(repo, "add", "-N", "--", rel)
    return _git(repo, "diff", "--binary", "--no-color").stdout


def test_files_from_patch(patch):
    paths = set()
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            paths.add(line[6:].strip())
        elif line.startswith("--- a/"):
            paths.add(line[6:].strip())
    return paths


def filter_patch(patch, blocked_paths):
    """从模型补丁里剔掉官方 test patch 会覆盖的文件。"""
    if not patch.strip():
        return ""
    sections = re.split(r"(?=^diff --git )", patch, flags=re.M)
    kept = []
    for section in sections:
        if not section.startswith("diff --git "):
            continue
        first = section.splitlines()[0]
        match = re.match(r"diff --git a/(.+) b/(.+)$", first)
        if not match:
            kept.append(section)
            continue
        if match.group(1) in blocked_paths or match.group(2) in blocked_paths:
            continue
        kept.append(section)
    return "".join(kept)


def reset_repo(repo):
    _git(repo, "reset", "--hard", "-q", "HEAD")
    _git(repo, "clean", "-fdq")


def _apply(repo, patch_text, patch_path):
    patch_path.write_text(patch_text, encoding="utf-8")
    proc = _git(repo, "apply", "--whitespace=nowarn", str(patch_path), check=False)
    return proc.returncode == 0, proc.stderr.strip()


def _parse_counts(output):
    counts = {}
    for key in ("passed", "failed", "error", "errors", "skipped"):
        match = re.search(rf"(\d+) {key}\b", output)
        if match:
            counts[key.rstrip("s") if key == "errors" else key] = int(match.group(1))
    return counts


def verify(repo, task, model_patch, p2p_mode="all", timeout=600, keep_patch_path=None):
    """干净重放：reset -> 模型补丁 -> 官方 test patch -> pytest。"""
    started = time.time()
    reset_repo(repo)

    blocked = test_files_from_patch(task["test_patch"])
    filtered = filter_patch(model_patch or "", blocked)
    patch_file = keep_patch_path or (repo.parent / "model.patch")
    model_ok, model_err = True, ""
    if filtered.strip():
        model_ok, model_err = _apply(repo, filtered, patch_file)
    if not model_ok:
        return {
            "resolved": False,
            "model_patch_applied": False,
            "model_apply_error": model_err,
            "test_patch_applied": False,
            "duration_s": round(time.time() - started, 1),
            "output_tail": "",
        }

    test_ok, test_err = _apply(
        repo, task["test_patch"], repo.parent / "test.patch")
    if not test_ok:
        return {
            "resolved": False,
            "model_patch_applied": True,
            "model_apply_error": "",
            "test_patch_applied": False,
            "test_apply_error": test_err,
            "duration_s": round(time.time() - started, 1),
            "output_tail": "",
        }

    selected = list(task["FAIL_TO_PASS"])
    if p2p_mode == "all":
        selected += list(task["PASS_TO_PASS"])
    cmd = [sys.executable, "-m", "pytest", *selected, "-q", "--no-header",
           "--tb=short", "-p", "no:cacheprovider"]
    try:
        proc = subprocess.run(
            cmd, cwd=repo, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONPATH": str(repo)})
        output = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + (exc.stderr or "")) if isinstance(
            exc.stdout, str) else ""
        output += f"\n[pytest 超时 {timeout}s]"
        rc, timed_out = -1, True

    failed = re.findall(r"^FAILED (\S+)", output, flags=re.M)
    errors = re.findall(r"^ERROR (\S+)", output, flags=re.M)
    counts = _parse_counts(output)
    resolved = rc == 0 and not failed and not errors
    return {
        "resolved": resolved,
        "model_patch_applied": True,
        "model_apply_error": "",
        "test_patch_applied": True,
        "test_apply_error": "",
        "p2p_mode": p2p_mode,
        "selected": len(selected),
        "f2p_count": len(task["FAIL_TO_PASS"]),
        "p2p_count": len(task["PASS_TO_PASS"]) if p2p_mode == "all" else 0,
        "returncode": rc,
        "timed_out": timed_out,
        "counts": counts,
        "failed": failed,
        "errors": errors,
        "duration_s": round(time.time() - started, 1),
        "output_tail": output[-8000:],
    }
