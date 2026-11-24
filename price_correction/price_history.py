# -*- coding: utf-8 -*-
"""兼容性别名：价格历史台账主实现见 history_ledger.py。"""
from __future__ import annotations

try:
    from history_ledger import *  # noqa: F401,F403
    from history_ledger import (  # noqa: F401
        LedgerInputError, LedgerPersistenceError, PriceHistoryLedger,
        render_query_text,
    )
except ImportError:  # pragma: no cover
    from .history_ledger import *  # noqa: F401,F403
    from .history_ledger import (  # noqa: F401
        LedgerInputError, LedgerPersistenceError, PriceHistoryLedger,
        render_query_text,
    )
