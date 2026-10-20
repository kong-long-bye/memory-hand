# -*- coding: utf-8 -*-
"""从当前 venv 里已安装的 astropy 构建可评测的基准工作区。

SWE-bench 官方的 base_commit 镜像不可用时，用 astropy 5.0（未修复版）作为
等价替身：官方 test patch 能正常应用，修复前失败、修复后通过。
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import env


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", default="astropy__astropy-13033")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    try:
        import astropy
    except ImportError:
        print("当前解释器没有 astropy；请用 evaluate/.venv 运行", file=sys.stderr)
        return 1

    package_dir = Path(astropy.__file__).resolve().parent
    version = astropy.__version__
    print(f"source package: {package_dir} (astropy {version})")
    if not version.startswith("5.0"):
        print(f"WARNING: test patch 是在 astropy 5.0 上验证的，当前是 {version}",
              file=sys.stderr)

    dest = env.template_repo(args.instance)
    if dest.exists():
        if not args.force:
            print(f"template 已存在：{dest}（用 --force 重建）")
            return 0
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    shutil.copytree(
        package_dir, dest / "astropy",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    (dest / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "add", "astropy", ".gitignore"], cwd=dest, check=True)
    subprocess.run(
        ["git", "-c", "user.name=eval", "-c", "user.email=eval@local",
         "commit", "-qm", f"base astropy {version} (pre-fix)"],
        cwd=dest, check=True)
    print(f"template ready: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
