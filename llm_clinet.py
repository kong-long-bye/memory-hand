
import os
from pathlib import Path

from openai import OpenAI

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

# 配置 api、url、model
def _cfg() -> dict:
    _load_dotenv() # 加载 .env 文件
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()

    # LLM_MODE 就是模型名；不要 .lower()，部分模型 ID 大小写敏感（如 ZHIPU/GLM-5.3-FlashX）
    model = os.environ.get("LLM_MODE", "").strip()
    try:
        timeout = float(os.environ.get("LLM_TIMEOUT", "60"))
    except ValueError:
        timeout = 60.0

    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
        "timeout": timeout,
    }

# 调用api：使用 OpenAI 官方 SDK 请求 Chat Completions 兼容接口
def _call_api(text: str) -> str:
    cfg = _cfg()
    # 模型名为空或显式关闭，以及关键配置缺失，都视为不可用
    if cfg["model"].lower() in ("", "none", "off", "disabled"):
        return ""
    if not (cfg["api_key"] and cfg["base_url"]):
        return ""

    client = OpenAI(
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        timeout=cfg["timeout"],
    )
    try:
        completion = client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0,

        )
        return (completion.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001
        # 网络/鉴权/超时/结构异常均返回空串，由 summarize() 统一抛错；
        # 注意这会将真实原因掩盖成“未配置”，排查时需先打印异常。
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
    # 注意：demo 需是真实 memory 文本；喂“你好”这类退化输入，
    # 推理模型会把 max_tokens 全花在思考上导致 content 为空。
    demo = (
        "你好"
    )
    print("model  :", _cfg()["model"] or "<none>")
    print("summary:", summarize(demo))
