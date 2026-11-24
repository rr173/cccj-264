# -*- coding: utf-8 -*-
"""兼容性别名：台账 CLI 主实现见 history_cli.py。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from history_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
