
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

# 调用api：OpenAI Chat Completions 兼容接口；失败/未配置返回空串，由上层回退到离线抽取式摘要
def _call_api(text: str) -> str:
    cfg = _cfg()
    # 判定是否启用 LLM：mode 显式关闭 或 关键三项缺失都视为不可用
    if cfg["mode"] in ("", "none", "off", "disabled"):
        return ""
    if not (cfg["api_key"] and cfg["base_url"] and cfg["model"]):
        return ""

    url = cfg["base_url"] + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": 128,
        "stream": False,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload,
                             timeout=cfg["timeout"])
        resp.raise_for_status()
        data = resp.json()
        # 标准 OpenAI 响应：choices[0].message.content
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:  # noqa: BLE001
        # 网络/鉴权/限流/结构异常统一吞掉，让 summarize() 走离线回退
        return ""




# 对外主入口：LLM 摘要 + 进程内缓存
def summarize(text: str) -> str:
    """把一条原始 memory 压缩成一句摘要；相同 text 命中缓存不再调用 API。"""
    if not text:
        return ""
    key = text.strip()
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    summary = _call_api(key)
    if not summary:
        raise RuntimeError("LLM 调用失败或未配置，无法生成摘要")
    _CACHE[key] = summary
    return summary


if __name__ == "__main__":
    demo = (
        "In repository keystone-services, the refresh_cache() function in "
        "cache.py returned stale data because the TTL comparison used seconds "
        "while the expiry field was stored in milliseconds. Fixed by normalizing "
        "both sides to milliseconds before comparison."
    )
    print("mode   :", _cfg()["mode"] or "<none>")
    print("summary:", summarize(demo))
