# -*- coding: utf-8 -*-
"""
成交价核对可复跑合成样例。

样例全部基于 ``samples/price_history/base.json`` 的底档和
``samples/price_check/corrections.json`` 的已批准更正单，成交明细为
``samples/transactions/transactions.jsonl``。

覆盖：
  * 同一价格事实（S1/P100/2026-01-15）在更正生效前、生效瞬间、生效后各有成交；
  * 同一经营日期 ID 在底档期和多次更正后价格不同，证明按成交时刻而非经营日期取价；
  * 多收与少收同时存在且数量已知，汇总先分别累计再给净分差；
  * 明确无价（底档无价、更正后无价）、缺少历史、底档生效前成交；
  * 无效 JSONL（重复成交行编号、缺字段/非法值、坏 JSON）整份拒绝且零输出；
  * 重复运行报告字节稳定。

运行：
    python3 -I run_price_check_demo.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from history_ledger import PriceHistoryLedger  # noqa: E402
from price_check import (  # noqa: E402
    STATUS_BEFORE_BASE_EFFECTIVE, STATUS_MATCH, STATUS_MISSING_HISTORY,
    STATUS_NO_PRICE_AT_TIME, STATUS_OVERCHARGED, STATUS_UNDERCHARGED,
    TransactionInputError, check_transactions_file, render_report_text,
    report_to_json,
)

ROOT = Path(__file__).resolve().parent.parent
BASE_PATH = ROOT / "samples" / "price_history" / "base.json"
CORRECTIONS_PATH = ROOT / "samples" / "price_check" / "corrections.json"
TX_PATH = ROOT / "samples" / "transactions" / "transactions.jsonl"
INVALID_TX = {
    "duplicate_line_id": ROOT / "samples" / "transactions"
    / "transactions_invalid_duplicate_line_id.jsonl",
    "illegal_fields": ROOT / "samples" / "transactions"
    / "transactions_invalid_fields.jsonl",
    "broken_json": ROOT / "samples" / "transactions"
    / "transactions_invalid_json.jsonl",
}
OUT_DIR = ROOT / "samples" / "output" / "price_check"
DB_PATH = OUT_DIR / "ledger.json"

report_lines: List[str] = []


def out(text: str = "") -> None:
    print(text)
    report_lines.append(text)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def build_ledger() -> PriceHistoryLedger:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = json.loads(BASE_PATH.read_text(encoding="utf-8"))
    ledger = PriceHistoryLedger(DB_PATH)
    ledger.initialize_base(base["effective_from"], base["prices"])
    ledger.deliver_file(CORRECTIONS_PATH)
    return ledger


def by_id(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {line["line_id"]: line for line in report["lines"]}


def expect_line(report: Dict[str, Any], line_id: str, status: str,
                expected: Any = None, unit_diff: Any = None,
                total_diff: Any = None) -> Dict[str, Any]:
    line = by_id(report)[line_id]
    assert line["status"] == status, (line_id, line["status"], status)
    assert line["expected_unit_price_fen"] == expected, (
        line_id, line["expected_unit_price_fen"], expected
    )
    assert line["unit_difference_fen"] == unit_diff, (
        line_id, line["unit_difference_fen"], unit_diff
    )
    assert line["total_difference_fen"] == total_diff, (
        line_id, line["total_difference_fen"], total_diff
    )
    return line


def check_invalid(path: Path, needles: List[str]) -> List[str]:
    try:
        check_transactions_file(PriceHistoryLedger(DB_PATH), path)
    except TransactionInputError as exc:
        joined = "\n".join(exc.errors)
        for needle in needles:
            assert needle in joined, (path.name, needle, joined)
        return list(exc.errors)
    raise AssertionError(f"{path.name} 本应整份拒绝")


def expect_invalid(path: Path, needles: List[str], label: str) -> None:
    errors = check_invalid(path, needles)
    out(f"已整份拒绝 {path.name}（{label}；共 {len(errors)} 项错误），未生成报告：")
    for error in errors:
        out(f"  - {error}")


def main() -> int:
    ledger = build_ledger()
    report = check_transactions_file(ledger, TX_PATH)
    text = render_report_text(report)
    json_text = report_to_json(report) + "\n"

    out("# 成交价核对合成样例")
    out(f"底档：{BASE_PATH}")
    out(f"已批准更正单：{CORRECTIONS_PATH}")
    out(f"成交明细：{TX_PATH}")
    out(f"样例台账：{DB_PATH}")
    out()

    # ---------------- 逐笔断言：状态、分差、来源 ---------------- #
    # 同一事实在 CO-001 生效（2026-01-05T10:00+08:00）前、瞬间、后。
    l1 = expect_line(report, "TX-1001", STATUS_MATCH, 1000, 0, 0)
    assert l1["source"]["type"] == "base"

    l2 = expect_line(report, "TX-1002", STATUS_OVERCHARGED, 950, 30, 60)
    assert l2["source"]["order_id"] == "CO-001-EARLY"
    # 更正生效时间 == 成交时间 => 已生效。
    assert l2["transaction_at"]["utc"] == "2026-01-05T02:00:00Z"
    assert l2["source"]["effective_at"] == "2026-01-05T02:00:00Z"

    l3 = expect_line(report, "TX-1003", STATUS_UNDERCHARGED, 950, -20, -20)
    assert l3["source"]["order_id"] == "CO-001-EARLY"

    # 同一经营日期 ID 在第二次更正后取 900，不用当前/最新价替代。
    l4 = expect_line(report, "TX-1004", STATUS_MATCH, 900, 0, 0)
    assert [x.get("order_id") for x in l4["provenance_chain"][1:]] == [
        "CO-001-EARLY", "CO-002-CHAIN",
    ]

    # 另一个事实，多收。
    l5 = expect_line(report, "TX-1005", STATUS_OVERCHARGED, 1150, 30, 30)
    assert l5["source"]["order_id"] == "CO-003-B-UPDATE"

    # 明确无价（底档）与明确无价（更正后）：均不计算分差。
    l6 = expect_line(report, "TX-1006", STATUS_NO_PRICE_AT_TIME, None, None, None)
    assert l6["source"]["type"] == "base"
    l7 = expect_line(report, "TX-1007", STATUS_NO_PRICE_AT_TIME, None, None, None)
    assert l7["source"]["order_id"] == "CO-005-TO-NOPRICE"

    # 更正生效前仍按 500 计，多收 20；证明不是按“当前无价”回填。
    expect_line(report, "TX-1008", STATUS_OVERCHARGED, 500, 20, 20)

    l9 = expect_line(report, "TX-1009", STATUS_MISSING_HISTORY, None, None, None)
    assert l9["source"] is None
    l10 = expect_line(report, "TX-1010", STATUS_BEFORE_BASE_EFFECTIVE,
                      None, None, None)
    assert l10["source"] is None

    # 同一时刻 seq=0 与 seq=1 均已生效；期望 870，不是 880。
    l11 = expect_line(report, "TX-1011", STATUS_OVERCHARGED, 870, 10, 20)
    assert l11["source"]["order_id"] == "CO-007-SEQ1"
    assert [x.get("order_id") for x in l11["provenance_chain"][1:]] == [
        "CO-001-EARLY", "CO-002-CHAIN", "CO-006-SEQ0", "CO-007-SEQ1",
    ]

    expect_line(report, "TX-1012", STATUS_UNDERCHARGED, 760, -60, -60)
    expect_line(report, "TX-1013", STATUS_OVERCHARGED, 870, 30, 30)

    # ---------------- 汇总断言 ---------------- #
    summary = report["summary"]
    assert summary["category_counts"] == {
        STATUS_MATCH: 2,
        STATUS_OVERCHARGED: 5,
        STATUS_UNDERCHARGED: 2,
        STATUS_NO_PRICE_AT_TIME: 2,
        STATUS_MISSING_HISTORY: 1,
        STATUS_BEFORE_BASE_EFFECTIVE: 1,
    }, summary["category_counts"]
    assert summary["priced_transaction_count"] == 9
    assert summary["non_priced_transaction_count"] == 4
    assert summary["overcharge_amount_fen"] == 160
    assert summary["undercharge_amount_fen"] == 80
    assert summary["net_difference_fen"] == 80
    assert summary["gross_difference_fen"] == 240
    # 若先把多收/少收混在一起，就无法同时看到这两个数；逐笔结果必须仍分列。
    assert len(report["lines"]) == len(by_id(report)) == 13

    groups = {(g["store_id"], g["product_id"], g["date_id"]): g
              for g in report["by_fact"]}
    g_a = groups[("S1", "P100", "2026-01-15")]
    assert g_a["transaction_count"] == 7
    assert g_a["priced_transaction_count"] == 6
    assert g_a["overcharge_amount_fen"] == 110
    assert g_a["undercharge_amount_fen"] == 20
    assert g_a["net_difference_fen"] == 90
    # 非有价行只计行数，不进入分差。
    assert groups[("S1", "P200", "2026-01-15")]["priced_transaction_count"] == 0
    assert groups[("S1", "P200", "2026-01-15")]["overcharge_amount_fen"] == 0
    assert groups[("S2", "P200", "2026-01-15")]["category_counts"][
        STATUS_NO_PRICE_AT_TIME] == 1
    assert groups[("S2", "P100", "2026-01-15")]["net_difference_fen"] == -60

    # 报告内同时出现多收、少收和净额，且不含当前时间等易变内容。
    assert "多收 5 行，合计 +160分" in text
    assert "少收 2 行，合计 -80分" in text
    assert "净分差：+80分" in text
    assert "当前时间" not in text

    # ---------------- 稳定性 ---------------- #
    report_again = check_transactions_file(PriceHistoryLedger(DB_PATH), TX_PATH)
    assert report_to_json(report_again) + "\n" == json_text
    assert render_report_text(report_again) == text

    # ---------------- 无效输入整份拒绝 ---------------- #
    expect_invalid(INVALID_TX["duplicate_line_id"], ["成交行编号重复"],
                   "相同成交行编号出现多次")
    expect_invalid(INVALID_TX["illegal_fields"], [
        "缺少字段 store_id",
        "quantity 必须是正整数",
        "unit_price_fen 必须是非负整数分",
        "必须带时区偏移",
    ], "字段缺失或值不合法")
    expect_invalid(INVALID_TX["broken_json"], ["JSON 格式错误"], "坏 JSON")

    # 落盘样例报告（与 CLI 输出同名同格式）。
    write_text(OUT_DIR / "transaction_price_check.json", json_text)
    write_text(OUT_DIR / "transaction_price_check.txt", text + "\n")

    out()
    out("### 校验过的关键事实")
    out("- TX-1001/1002/1003：同一事实在 CO-001 生效前、生效瞬间、生效后各有成交；")
    out("  TX-1001 一致=1000，TX-1002 多收（期望 950，+30×2），TX-1003 少收（-20）。")
    out("- TX-1004：同一经营日期 ID，按 2026-01-12 成交时点取 900，来源链含 CO-001、CO-002。")
    out("- TX-1006/1007：明确无价（底档/更正后），未计入多收或少收。")
    out("- TX-1008：CO-005 生效前仍按 500 计，多收 20。")
    out("- TX-1009：缺少历史；TX-1010：底档生效前成交；二者均无期望价与分差。")
    out("- TX-1011：同一时点 seq0+seq1 均已生效，期望 870；来源为 CO-007。")
    out("- 汇总：一致 2、多收 5（+160分）、少收 2（-80分）、明确无价 2、"
        "缺少历史 1、底档前 1；净分差 +80分。")
    out("- 重复运行报告逐字节一致；三份无效 JSONL 均整份拒绝且不产生任何报告。")
    out()
    out(f"[输出] 文字报告：{OUT_DIR / 'transaction_price_check.txt'}")
    out(f"[输出] 机器可读：{OUT_DIR / 'transaction_price_check.json'}")
    out("# 全部成交价核对断言通过。")

    write_text(OUT_DIR / "price_check_demo_report.txt",
               "\n".join(report_lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
