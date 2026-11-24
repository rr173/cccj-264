# -*- coding: utf-8 -*-
"""
成交价核对 CLI：用已初始化的价格历史台账核对一份 JSONL 收银成交明细。

示例：
  python3 -I price_check_cli.py \\
      --db ../samples/output/price_check/ledger.json \\
      --transactions ../samples/transactions/transactions.jsonl \\
      --output-dir ../samples/output/price_check

退出码：
  0  核对完成（逐笔结果与汇总均为证据，不因存在多收/少收而失败）
  2  成交明细或命令行输入无效：整份输入零部分报告，不写任何输出文件
  3  台账缺失、未初始化或无法读取
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from history_ledger import (  # noqa: E402
    LedgerInputError, LedgerPersistenceError, PriceHistoryLedger,
)
from price_check import (  # noqa: E402
    PriceCheckLedgerError, TransactionInputError,
    check_transactions_file, render_report_text, report_to_json,
)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def cmd_check(args: argparse.Namespace) -> int:
    ledger = PriceHistoryLedger(os.path.abspath(args.db))
    report = check_transactions_file(ledger, os.path.abspath(args.transactions))
    text = render_report_text(report)
    json_text = report_to_json(report) + "\n"

    if args.output_dir:
        out_dir = Path(os.path.abspath(args.output_dir))
        # 两个文件都先在内存中生成；报告成功才落盘，避免半份结果。
        _atomic_write(out_dir / "transaction_price_check.json", json_text)
        _atomic_write(out_dir / "transaction_price_check.txt", text + "\n")

    if args.json:
        sys.stdout.write(json_text)
    else:
        print(text)
        if args.output_dir:
            print(f"\n[输出] 文字报告：{Path(os.path.abspath(args.output_dir)) / 'transaction_price_check.txt'}")
            print(f"[输出] 机器可读：{Path(os.path.abspath(args.output_dir)) / 'transaction_price_check.json'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="按成交发生时刻核对收银成交明细与价格历史台账"
    )
    ap.add_argument("--db", required=True, help="已初始化的价格历史台账 JSON 文件或其目录")
    ap.add_argument("--transactions", required=True,
                    help="成交明细 JSONL（每行一笔成交）")
    ap.add_argument("--output-dir", default=None,
                    help="成功时写出 transaction_price_check.{json,txt}")
    ap.add_argument("--json", action="store_true", help="向标准输出打印完整 JSON 报告")
    return ap


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return cmd_check(args)
    except LedgerInputError as exc:
        print("成交明细或输入无效；整份输入未产生任何报告：", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err}", file=sys.stderr)
        return 2
    except (LedgerPersistenceError, OSError) as exc:
        print(f"台账读取/落盘失败，未产生报告：{exc}", file=sys.stderr)
        return 3
    except (KeyError, TypeError, ValueError) as exc:
        print(f"输入结构错误：{exc!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
