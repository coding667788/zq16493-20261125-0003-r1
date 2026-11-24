"""命令行薄入口：可在隔离模式 `python -I archivecut_cli.py ...` 下运行。

隔离模式（-I）会移除 cwd 的隐式 sys.path，因此这里显式把脚本所在目录
（仓库根，archivecut 包的父目录）加入 sys.path 后再调用包内 CLI。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from archivecut.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
