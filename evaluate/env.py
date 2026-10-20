# -*- coding: utf-8 -*-
"""评测用共享配置：路径、.env、任务数据、官方请求。"""

import json
import os
from pathlib import Path


EVAL_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_ROOT.parent
SRC_DIR = PROJECT_ROOT / "src"
WORK_ROOT = EVAL_ROOT / "work"
REPORTS_DIR = EVAL_ROOT / "reports"
DATASET_PATH = (PROJECT_ROOT / "data" / "SWEContextBench" / "data"
                / "SWEContextBench_Experience.jsonl")
OFFICIAL_REQUESTS = PROJECT_ROOT / "data_store" / "official_requests.jsonl"

_ENV_LOADED = False


def load_dotenv():
    """读取仓库根 .env，不覆盖已有环境变量。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def llm_config(model=None):
    """模型配置；model 参数优先于 .env 的 LLM_MODE。"""
    load_dotenv()
    try:
        timeout = float(os.environ.get("LLM_TIMEOUT", "120"))
    except ValueError:
        timeout = 120.0
    try:
        max_tokens = int(os.environ.get("EVAL_MAX_TOKENS", "8192"))
    except ValueError:
        max_tokens = 8192
    return {
        "api_key": os.environ.get("OPENAI_API_KEY", "").strip(),
        "base_url": os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/"),
        "model": (model or os.environ.get("LLM_MODE", "")).strip(),
        "timeout": timeout,
        "max_tokens": max_tokens,
    }


def _as_list(value):
    """数据集里 FAIL_TO_PASS/PASS_TO_PASS 是 JSON 字符串，也兼容真 list。"""
    if isinstance(value, str):
        return json.loads(value)
    return list(value or [])


def load_task(instance_id):
    if not DATASET_PATH.exists():
        raise FileNotFoundError(f"数据集不存在：{DATASET_PATH}")
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("instance_id") == instance_id:
                row["FAIL_TO_PASS"] = _as_list(row.get("FAIL_TO_PASS"))
                row["PASS_TO_PASS"] = _as_list(row.get("PASS_TO_PASS"))
                return row
    raise KeyError(f"数据集中找不到任务：{instance_id}")


def load_official_requests():
    if not OFFICIAL_REQUESTS.exists():
        raise FileNotFoundError(
            f"官方请求记录不存在：{OFFICIAL_REQUESTS}\n"
            "需要先部署带请求日志的版本并跑一次官方 smoke。")
    with open(OFFICIAL_REQUESTS, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def instance_dir(instance_id):
    return WORK_ROOT / instance_id


def template_repo(instance_id):
    return instance_dir(instance_id) / "template"


def ensure_template(instance_id):
    repo = template_repo(instance_id)
    if not (repo / ".git").exists():
        raise FileNotFoundError(
            f"缺少基准工作区：{repo}\n"
            "先运行：evaluate/.venv/Scripts/python.exe evaluate/setup_workspace.py")
    return repo
