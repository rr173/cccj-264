# -*- coding: utf-8 -*-
"""兼容性别名：价格历史台账主实现见 history_ledger.py。"""
from __future__ import annotations

try:  # 直接把 price_correction/ 加入 sys.path 时
    from history_ledger import *  # noqa: F401,F403
    from history_ledger import (  # noqa: F401
        LedgerInputError, LedgerPersistenceError, PriceHistoryLedger,
        bookable_from_content, parse_aware_datetime, render_query_text,
    )
except ImportError:  # 以 price_correction.ledger 包方式导入时
    from .history_ledger import *  # noqa: F401,F403
    from .history_ledger import (  # noqa: F401
        LedgerInputError, LedgerPersistenceError, PriceHistoryLedger,
        bookable_from_content, parse_aware_datetime, render_query_text,
    )
