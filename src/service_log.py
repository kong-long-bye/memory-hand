# -*- coding: utf-8 -*-
"""服务侧日志：只负责格式与落盘，不参与检索/存储逻辑。

一行一个事件，开头是 [启动] / [请求] / [写入记忆] / [检索记忆] / [参数校验失败] /
[未处理异常] 这样的中文标签，评测失败后按标签 grep。
长正文一律截断：一次 top_k=100 的完整结果有好几 MB，全量打印会把有用行
挤出轮转，所以这里只记规模（字符数、条数、分数区间）。
"""
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "memory_service"
MAX_BYTES = 20 * 1024 * 1024
BACKUP_COUNT = 5
TEXT_LIMIT = 120

_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)s %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def get_logger(store_dir) -> logging.Logger:
    """stdout 与 <store_dir>/service.log 双写；重复调用不会叠加 handler。

    store_dir 是挂载卷目录，所以容器重启、`docker logs` 被轮转截断之后
    日志仍然可读。写不进去（权限/只读挂载）只降级成 warning，不影响启动。
    """
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    formatter.converter = time.gmtime  # 与平台/数据集时间对齐，统一 UTC

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    try:
        file_handler = RotatingFileHandler(
            Path(store_dir) / "service.log",
            maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:  # noqa: BLE001
        logger.warning("[启动] 日志文件写不进去，只输出到控制台：目录=%s 原因=%s",
                       store_dir, exc)
    return logger


def brief(value, limit: int = TEXT_LIMIT) -> str:
    """压成单行并截断，日志只留能定位问题的片段。"""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "..."
