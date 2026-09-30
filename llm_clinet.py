
import os
from pathlib import Path

import requests
_ROOT = Path(__file__).parent
_ENV_LOADED = False
_CACHE = {}  # text -> summary，数据集中有重复文本，缓存可省调用


# 系统提示词
_SUMMARY_SYSTEM = (
    "You summarize one software-engineering memory into a single concise sentence "
    "that captures the core problem/topic and the key decision or fix. "
    "Reply with only the summary text: no quotes, no preamble, no markdown."
)


# 配置
def _load_dotenv():
    """极简 .env 加载（不引第三方依赖），仅首次调用生效；不覆盖已有环境变量。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

# 配置api ，url
def _cfg() ->dict:
    _load_dotenv() # 加载 .env 文件
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()

    mode = os.environ.get("LLM_MODE", "").strip().lower()
    try:
        timeout = float(os.environ.get("LLM_TIMEOUT", "20"))
    except ValueError:
        timeout = 20.0

    return {
        "mode": mode,
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": os.environ.get("LLM_MODEL", "").strip() ,
        "timeout": timeout,
    }

# 调用api
def _call_api(text:str) -> str:
    pass
