# -*- coding: utf-8 -*-
"""下载 SWEContextBench 数据集到项目 data/ 目录。

默认 HuggingFace 缓存写在 C 盘（~/.cache/huggingface），这里统一改到 data/ 下。
仓库是一组 parquet（Related / Experience / Relationship 及各自 Lite 变体），
每个 schema 不同，因此按文件分别导出 JSONL，供 dataset_adapter 使用。

运行： D:/environment/miniconda/envs/memory/python src/download_dataset.py
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent # 仓库根
DATA_DIR = BASE_DIR / "data"
LOCAL_DIR = DATA_DIR / "SWEContextBench"

# 必须在 import datasets 之前设置：库在导入时就解析默认缓存路径
os.environ.setdefault("HF_HOME", str(DATA_DIR / "hf_cache"))
os.environ.setdefault("HF_DATASETS_CACHE", str(DATA_DIR / "hf_cache" / "datasets"))
os.environ.setdefault("HF_HUB_CACHE", str(DATA_DIR / "hf_cache" / "hub"))

from huggingface_hub import snapshot_download  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

REPO_ID = "jiayuanz3/SWEContextBench"


def export_parquet(path: Path) -> int:
    """把一个 parquet 逐行写成 JSONL，返回行数。"""
    out_path = path.with_suffix(".jsonl")
    table = pq.read_table(path)
    rows = table.to_pylist()
    with open(out_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print(f"{path.stem}: {len(rows)} rows, cols={table.column_names}")
    return len(rows)


def main():
    snapshot_download(repo_id=REPO_ID, repo_type="dataset", local_dir=str(LOCAL_DIR))

    files = sorted(LOCAL_DIR.rglob("*.parquet"))
    if not files:
        raise SystemExit(f"no parquet found under {LOCAL_DIR}")
    for path in files:
        export_parquet(path)


if __name__ == "__main__":
    main()
