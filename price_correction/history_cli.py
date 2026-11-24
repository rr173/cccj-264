# -*- coding: utf-8 -*-
"""
价格历史台账 CLI。

示例：
  python3 -I history_cli.py init \
      --db ../samples/output/price_history/ledger.json \
      --base ../samples/price_history/base.json

  python3 -I history_cli.py deliver \
      --db ../samples/output/price_history/ledger.json \
      --input ../samples/price_history/batch_01_future.json

  python3 -I history_cli.py query \
      --db ../samples/output/price_history/ledger.json \
      --store S1 --product P100 --date 2026-01-15 \
      --at '2026-01-12T12:00:00+08:00'

硬输入错误退出码为 2；业务组冲突不是投递失败，会落盘并可通过 conflicts 查询。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from history_ledger import (  # noqa: E402
    LedgerInputError, LedgerPersistenceError, PriceHistoryLedger,
    render_query_text,
)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _ledger(args: argparse.Namespace) -> PriceHistoryLedger:
    return PriceHistoryLedger(os.path.abspath(args.db))


def cmd_init(args: argparse.Namespace) -> int:
    raw = _load_json(os.path.abspath(args.base))
    if args.effective_from:
        effective = args.effective_from
        prices = raw.get("prices", raw.get("ledger", raw)) if isinstance(raw, dict) else raw
    elif isinstance(raw, dict) and isinstance(raw.get("base"), dict):
        effective = raw["base"].get("effective_from")
        prices = raw["base"].get("prices", raw["base"].get("ledger"))
    elif isinstance(raw, dict):
        effective = raw.get("effective_from") or raw.get("base_effective_from")
        prices = raw.get("prices", raw.get("ledger"))
    else:
        effective = None
        prices = raw
    if not effective:
        raise LedgerInputError("底档缺少 effective_from（也可用 --effective-from 提供）")
    ledger = _ledger(args)
    receipt = ledger.initialize_base(effective, prices)
    _print_json(receipt)
    return 0


def cmd_deliver(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    if args.input:
        receipt = ledger.deliver_file(os.path.abspath(args.input))
    else:
        order = _load_json(os.path.abspath(args.order))
        envelope = {
            "order_id": args.order_id or order.get("order_id"),
            "effective_at": args.effective_at or order.get("effective_at"),
            "sequence": args.sequence if args.sequence is not None else order.get("sequence"),
            "content": order,
        }
        receipt = ledger.deliver(envelope)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            json.dump(receipt, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    _print_json(receipt)
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    result = ledger.query_price(args.store, args.product, args.date, args.at)
    if args.json:
        _print_json(result)
    else:
        print(render_query_text(result))
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    result = ledger.history_for_fact(args.store, args.product, args.date)
    _print_json(result)
    return 0


def cmd_conflicts(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    _print_json(ledger.list_conflicts())
    return 0


def cmd_orders(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    _print_json({"stats": ledger.stats(), "orders": ledger.list_orders()})
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    _print_json(ledger.export_state())
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", required=True, help="台账 JSON 文件或其所在目录")

    ap = argparse.ArgumentParser(description="价格历史台账：落盘、乱序重算、时点查询与来源追溯")
    sub = ap.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", parents=[common],
                            help="用起始底档初始化台账")
    p_init.add_argument("--base", required=True, help="底档 JSON（含 effective_from 与 prices/ledger）")
    p_init.add_argument("--effective-from", default=None, help="覆盖/补充底档生效时点（ISO 8601，建议带时区）")
    p_init.set_defaults(func=cmd_init)

    p_deliver = sub.add_parser("deliver", parents=[common],
                               help="投递一批或单笔已批准更正单")
    source = p_deliver.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="投递批 JSON，可含 orders 列表")
    source.add_argument("--order", help="现有签审引擎生成的 correction_order.json")
    p_deliver.add_argument("--order-id", default=None, help="与 --order 配套的唯一单号；若 JSON 顶层有 order_id 可省略")
    p_deliver.add_argument("--effective-at", default=None, help="与 --order 配套的带时区生效时间")
    p_deliver.add_argument("--sequence", type=int, default=None, help="与 --order 配套的同时点顺序号")
    p_deliver.add_argument("--output", default=None, help="把投递回执写入 JSON 文件")
    p_deliver.set_defaults(func=cmd_deliver)

    p_query = sub.add_parser("query", parents=[common],
                             help="按门店/商品/日期和时点查询有效价格")
    p_query.add_argument("--store", required=True)
    p_query.add_argument("--product", required=True)
    p_query.add_argument("--date", required=True)
    p_query.add_argument("--at", required=True, help="查询时点 ISO 8601；naive 时间按 UTC 解释")
    p_query.add_argument("--json", action="store_true", help="输出完整 JSON 来源链")
    p_query.add_argument("--output", default=None, help="同时把查询结果写入 JSON")
    p_query.set_defaults(func=cmd_query)

    p_hist = sub.add_parser("history", parents=[common],
                            help="查看一个事实的全部价格事件和被挡变更")
    p_hist.add_argument("--store", required=True)
    p_hist.add_argument("--product", required=True)
    p_hist.add_argument("--date", required=True)
    p_hist.set_defaults(func=cmd_history)

    p_conf = sub.add_parser("conflicts", parents=[common],
                            help="列出当前重算后的全部冲突通过组")
    p_conf.set_defaults(func=cmd_conflicts)

    p_orders = sub.add_parser("orders", parents=[common],
                              help="列出已持久化更正单及当前生效裁定")
    p_orders.set_defaults(func=cmd_orders)

    p_export = sub.add_parser("export", parents=[common],
                              help="导出完整台账状态 JSON")
    p_export.set_defaults(func=cmd_export)
    return ap


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LedgerInputError as exc:
        print("输入无效；若为投递批次，则整批未写入：", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err}", file=sys.stderr)
        return 2
    except (LedgerPersistenceError, OSError) as exc:
        print(f"台账落盘/读取失败，未继续写入：{exc}", file=sys.stderr)
        return 3
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"输入 JSON 结构错误: {exc!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
