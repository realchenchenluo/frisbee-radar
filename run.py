#!/usr/bin/env python
"""飞盘讯息抓取 —— 程序入口。

用法示例：
    python run.py targets                     # 看看配了哪些目标
    python run.py crawl --mock                # 离线跑通全流程（不联网）
    python run.py login --platform xiaohongshu
    python run.py crawl --source xiaohongshu
    python run.py report --days 7

完整说明见 README.md。
"""

import sys
from pathlib import Path

# 允许在任意工作目录下执行 python run.py
sys.path.insert(0, str(Path(__file__).resolve().parent))

from frisbee_radar.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
