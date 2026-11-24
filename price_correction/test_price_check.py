# -*- coding: utf-8 -*-
"""成交价核对的规则级测试。

运行：
    python3 -I test_price_check.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from history_ledger import PriceHistoryLedger  # noqa: E402
from price_check import (  # noqa: E402
    STATUS_BEFORE_BASE_EFFECTIVE, STATUS_MATCH, STATUS_MISSING_HISTORY,
    STATUS_NO_PRICE_AT_TIME, STATUS_OVERCHARGED, STATUS_UNDERCHARGED,
    PriceCheckLedgerError, TransactionInputError,
    check_transactions, check_transactions_file, parse_transactions,
    render_report_text,
)

BASE_TIME = "2026-01-01T00:00:00+08:00"
S1P1D1 = ("S1", "P1", "D1")
S1P2D1 = ("S1", "P2", "D1")
S1P3D1 = ("S1", "P3", "D1")


def fact_dict(t):
    return {"store_id": t[0], "product_id": t[1], "date_id": t[2]}


def line_obj(line_id, at, qty, price, t=S1P1D1, **overrides):
    obj = {
        "line_id": line_id,
        "store_id": t[0],
        "product_id": t[1],
        "date_id": t[2],
        "transaction_at": at,
        "quantity": qty,
        "unit_price_fen": price,
    }
    obj.update(overrides)
    return obj


class PriceCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "ledger.json"
        self.ledger = PriceHistoryLedger(self.db)
        self.ledger.initialize_base(BASE_TIME, [
            {**fact_dict(S1P1D1), "value": 1000},
            {**fact_dict(S1P2D1), "value": 500},
            {**fact_dict(S1P3D1), "value": "NOPRICE"},
        ])
        self.ledger.deliver({
            "order_id": "CO-1",
            "effective_at": "2026-01-05T09:00:00+08:00",
            "sequence": 0,
            "content": {"groups": [{
                "group_id": "G1",
                "facts": [{**fact_dict(S1P1D1), "before": 1000, "after": 950}],
            }]},
        })
        self.ledger.deliver({
            "order_id": "CO-2",
            "effective_at": "2026-01-05T10:30:00+08:00",
            "sequence": 0,
            "content": {"groups": [{
                "group_id": "G2",
                "facts": [{**fact_dict(S1P1D1), "before": 950, "after": 920}],
            }]},
        })
        # 明确无价 -> 有价。
        self.ledger.deliver({
            "order_id": "CO-3",
            "effective_at": "2026-01-07T12:00:00+08:00",
            "sequence": 0,
            "content": {"groups": [{
                "group_id": "G3",
                "facts": [{**fact_dict(S1P3D1), "before": "NOPRICE", "after": 700}],
            }]},
        })

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, objs):
        text = "\n".join(json.dumps(o, ensure_ascii=False) for o in objs)
        txs = parse_transactions(text)
        return check_transactions(self.ledger, txs)

    # ---------------- 时点与状态 ---------------- #

    def test_before_and_after_correction_use_transaction_instant(self):
        report = self.check([
            line_obj("L1", "2026-01-05T08:59:59+08:00", 1, 1000),
            line_obj("L2", "2026-01-05T09:00:00+08:00", 1, 1000),
            line_obj("L3", "2026-01-05T10:29:59+08:00", 1, 900),
            line_obj("L4", "2026-01-05T10:30:00+08:00", 1, 920),
        ])
        by_id = {x["line_id"]: x for x in report["lines"]}
        self.assertEqual(by_id["L1"]["status"], STATUS_MATCH)
        self.assertEqual(by_id["L1"]["expected_unit_price_fen"], 1000)
        # 更正生效时间恰好等于成交时间：视为已生效。
        self.assertEqual(by_id["L2"]["status"], STATUS_OVERCHARGED)
        self.assertEqual(by_id["L2"]["expected_unit_price_fen"], 950)
        self.assertEqual(by_id["L2"]["source"]["order_id"], "CO-1")
        self.assertEqual(by_id["L3"]["status"], STATUS_UNDERCHARGED)
        self.assertEqual(by_id["L4"]["status"], STATUS_MATCH)
        self.assertEqual(by_id["L4"]["expected_unit_price_fen"], 920)
        self.assertEqual(
            [x.get("order_id") for x in by_id["L4"]["provenance_chain"][1:]],
            ["CO-1", "CO-2"],
        )

    def test_date_id_is_not_query_time_and_current_price_is_not_used(self):
        # D1 是经营日期 ID，不是成交日期；同一 D1 事实在 1 月 3 日和 1 月 20 日价格不同。
        report = self.check([
            line_obj("EARLY", "2026-01-03T12:00:00+08:00", 1, 1000),
            line_obj("LATE", "2026-01-20T12:00:00+08:00", 1, 920),
        ])
        for item in report["lines"]:
            self.assertEqual(item["date_id"], "D1")
        self.assertEqual(report["lines"][0]["expected_unit_price_fen"], 1000)
        self.assertEqual(report["lines"][1]["expected_unit_price_fen"], 920)
        self.assertEqual(report["lines"][0]["status"], STATUS_MATCH)
        self.assertEqual(report["lines"][1]["status"], STATUS_MATCH)

    def test_quantity_weighted_difference_and_direction(self):
        report = self.check([
            line_obj("OVER", "2026-01-05T09:30:00+08:00", 3, 960),   # +10 * 3
            line_obj("UNDER", "2026-01-05T09:30:00+08:00", 4, 940),  # -10 * 4
        ])
        over, under = report["lines"]
        self.assertEqual(over["status"], STATUS_OVERCHARGED)
        self.assertEqual(over["unit_difference_fen"], 10)
        self.assertEqual(over["total_difference_fen"], 30)
        self.assertEqual(over["expected_total_fen"], 950 * 3)
        self.assertEqual(under["status"], STATUS_UNDERCHARGED)
        self.assertEqual(under["unit_difference_fen"], -10)
        self.assertEqual(under["total_difference_fen"], -40)

    def test_non_priced_statuses_never_reported_as_over_or_under(self):
        report = self.check([
            line_obj("NOPRICE", "2026-01-06T12:00:00+08:00", 1, 650, t=S1P3D1),
            line_obj("AFTER", "2026-01-08T12:00:00+08:00", 2, 750, t=S1P3D1),
            line_obj("MISSING", "2026-01-09T12:00:00+08:00", 3, 999,
                     t=("S9", "PX", "D1")),
            line_obj("BEFORE", "2025-12-31T23:30:00+08:00", 1, 1234),
        ])
        by_id = {x["line_id"]: x for x in report["lines"]}
        for key, status in (
            ("NOPRICE", STATUS_NO_PRICE_AT_TIME),
            ("MISSING", STATUS_MISSING_HISTORY),
            ("BEFORE", STATUS_BEFORE_BASE_EFFECTIVE),
        ):
            item = by_id[key]
            self.assertEqual(item["status"], status)
            self.assertIsNone(item["expected_unit_price_fen"])
            self.assertIsNone(item["unit_difference_fen"])
            self.assertIsNone(item["total_difference_fen"])
            self.assertIsNone(item["difference_direction"])
        self.assertEqual(by_id["AFTER"]["status"], STATUS_OVERCHARGED)
        self.assertEqual(by_id["AFTER"]["expected_unit_price_fen"], 700)

        summary = report["summary"]
        self.assertEqual(summary["priced_transaction_count"], 1)
        self.assertEqual(summary["category_counts"][STATUS_NO_PRICE_AT_TIME], 1)
        self.assertEqual(summary["category_counts"][STATUS_MISSING_HISTORY], 1)
        self.assertEqual(summary["category_counts"][STATUS_BEFORE_BASE_EFFECTIVE], 1)
        self.assertEqual(summary["overcharge_count"], 1)
        self.assertEqual(summary["undercharge_count"], 0)
        self.assertEqual(summary["net_difference_fen"], 100)

    def test_summary_keeps_over_and_under_separate_before_netting(self):
        report = self.check([
            line_obj("O", "2026-01-05T09:30:00+08:00", 2, 960),   # +80
            line_obj("U", "2026-01-05T09:30:00+08:00", 10, 940),  # -100
            line_obj("M", "2026-01-05T09:30:00+08:00", 1, 950),   # 0
        ])
        s = report["summary"]
        self.assertEqual(s["category_counts"][STATUS_OVERCHARGED], 1)
        self.assertEqual(s["category_counts"][STATUS_UNDERCHARGED], 1)
        self.assertEqual(s["category_counts"][STATUS_MATCH], 1)
        self.assertEqual(s["overcharge_amount_fen"], 20)
        self.assertEqual(s["undercharge_amount_fen"], 100)
        self.assertEqual(s["net_difference_fen"], -80)
        self.assertEqual(s["gross_difference_fen"], 120)
        groups = {(g["store_id"], g["product_id"], g["date_id"]): g
                  for g in report["by_fact"]}
        group = groups[("S1", "P1", "D1")]
        self.assertEqual(group["overcharge_amount_fen"], 20)
        self.assertEqual(group["undercharge_amount_fen"], 100)
        self.assertEqual(group["net_difference_fen"], -80)
        self.assertEqual(group["priced_transaction_count"], 3)

    # ---------------- 输入校验：整份拒绝 ---------------- #

    def assert_invalid(self, text, needle):
        with self.assertRaises(TransactionInputError) as ctx:
            parse_transactions(text)
        joined = "\n".join(ctx.exception.errors)
        self.assertIn(needle, joined)
        return ctx.exception.errors

    def test_duplicate_line_id_rejects_whole_input(self):
        text = "\n".join([
            json.dumps(line_obj("SAME", "2026-01-05T09:30:00+08:00", 1, 950)),
            json.dumps(line_obj("SAME", "2026-01-05T10:00:00+08:00", 2, 960)),
            json.dumps(line_obj("OK", "2026-01-05T11:00:00+08:00", 1, 950)),
        ])
        errors = self.assert_invalid(text, "成交行编号重复")
        self.assertTrue(any("SAME" in e for e in errors))

    def test_duplicate_id_detected_even_when_one_occurrence_has_other_errors(self):
        # 第 1 行除编号外还缺字段；第 2 行完全合法。仍应同时报告两种错误。
        broken = line_obj("SAME", "2026-01-05T09:30:00+08:00", 1, 950)
        del broken["unit_price_fen"]
        text = "\n".join([
            json.dumps(broken),
            json.dumps(line_obj("SAME", "2026-01-05T10:30:00+08:00", 1, 950)),
        ])
        errors = self.assert_invalid(text, "成交行编号重复")
        joined = "\n".join(errors)
        self.assertIn("缺少字段 unit_price_fen", joined)
        self.assertIn("第 1, 2 行", joined)

    def test_missing_or_illegal_fields_reject_whole_input(self):
        bad_qty_zero = line_obj("Q0", "2026-01-05T09:30:00+08:00", 0, 950)
        bad_qty_bool = line_obj("QB", "2026-01-05T09:30:00+08:00", True, 950)
        bad_qty_float = line_obj("QF", "2026-01-05T09:30:00+08:00", 1.5, 950)
        bad_price = line_obj("P", "2026-01-05T09:30:00+08:00", 1, -1)
        bad_price_bool = line_obj("PB", "2026-01-05T09:30:00+08:00", 1, False)
        naive_time = line_obj("T", "2026-01-05T09:30:00", 1, 950)
        missing = line_obj("M", "2026-01-05T09:30:00+08:00", 1, 950)
        del missing["store_id"]
        empty_id = line_obj("E", "2026-01-05T09:30:00+08:00", 1, 950)
        empty_id["line_id"] = "  "
        text = "\n".join(json.dumps(x, ensure_ascii=False) for x in [
            bad_qty_zero, bad_qty_bool, bad_qty_float, bad_price,
            bad_price_bool, naive_time, missing, empty_id,
            line_obj("GOOD", "2026-01-05T09:30:00+08:00", 1, 950),
        ])
        errors = self.assert_invalid(text, "第 1 行")
        joined = "\n".join(errors)
        for needle in ("quantity", "unit_price_fen", "时区", "缺少字段 store_id",
                       "line_id 不能为空"):
            self.assertIn(needle, joined)

    def test_json_structure_problems_reject_whole_input(self):
        good = json.dumps(line_obj("GOOD", "2026-01-05T09:30:00+08:00", 1, 950))
        duplicate_key = (
            '{"line_id":"D","line_id":"E","store_id":"S1","product_id":"P1",'
            '"date_id":"D1","transaction_at":"2026-01-05T09:30:00+08:00",'
            '"quantity":1,"unit_price_fen":950}'
        )
        for text, needle in [
            (f"{good}\n\n", "空行"),
            (f"{good}\nnot-json", "JSON 格式错误"),
            (f"{good}\n[1,2,3]", "JSON 对象"),
            (f"{good}\n{duplicate_key}", "字段重复"),
            (
                '{"line_id":"N","store_id":"S1","product_id":"P1","date_id":"D1",'
                '"transaction_at":"2026-01-05T09:30:00+08:00","quantity":1,'
                '"unit_price_fen":NaN}',
                "非标准常量",
            ),
        ]:
            self.assert_invalid(text, needle)

    def test_invalid_file_produces_no_report_object(self):
        path = self.root / "bad.jsonl"
        path.write_text(
            json.dumps(line_obj("DUP", "2026-01-05T09:30:00+08:00", 1, 950)) + "\n" +
            json.dumps(line_obj("DUP", "2026-01-05T10:30:00+08:00", 1, 950)) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(TransactionInputError):
            check_transactions_file(self.ledger, path)

    def test_empty_input_file_rejected(self):
        path = self.root / "empty.jsonl"
        path.write_text("\n", encoding="utf-8")
        with self.assertRaises(TransactionInputError):
            check_transactions_file(self.ledger, path)

    def test_uninitialized_or_missing_ledger_is_not_no_price(self):
        missing_db = self.root / "nested" / "missing.json"
        with self.assertRaises(PriceCheckLedgerError):
            check_transactions_file(PriceHistoryLedger(missing_db), self._write_ok())
        uninit_db = self.root / "uninit.json"
        uninit = PriceHistoryLedger(uninit_db)
        with self.assertRaises(PriceCheckLedgerError):
            check_transactions_file(uninit, self._write_ok())

    def _write_ok(self) -> Path:
        path = self.root / "ok.jsonl"
        path.write_text(
            json.dumps(line_obj("OK", "2026-01-05T09:30:00+08:00", 1, 950)) + "\n",
            encoding="utf-8",
        )
        return path

    # ---------------- 报告稳定性与可读性 ---------------- #

    def test_invalid_input_produces_same_errors_regardless_of_later_good_lines(self):
        good = json.dumps(line_obj("GOOD", "2026-01-05T09:30:00+08:00", 1, 950))
        bad = json.dumps(line_obj("BAD", "2026-01-05T09:30:00+08:00", 1, -5))
        both = f"{bad}\n{good}"
        with self.assertRaises(TransactionInputError) as ctx:
            parse_transactions(both)
        # 报错定位到坏行；好行不会让输入变为“部分可用”。
        self.assertTrue(any(e.startswith("第 1 行:") for e in ctx.exception.errors))
        self.assertFalse(any(e.startswith("第 2 行:") for e in ctx.exception.errors))

    def test_report_is_stable_and_contains_required_trace(self):
        txs = parse_transactions(
            json.dumps(line_obj("L1", "2026-01-05T10:30:00+08:00", 2, 930),
                       ensure_ascii=False)
        )
        report1 = check_transactions(self.ledger, txs, input_meta={"path": "x.jsonl"})
        report2 = check_transactions(self.ledger, txs, input_meta={"path": "x.jsonl"})
        self.assertEqual(report1, report2)
        text1 = render_report_text(report1)
        text2 = render_report_text(report2)
        self.assertEqual(text1, text2)
        self.assertIn("2026-01-05T10:30:00+08:00", text1)
        self.assertIn("2026-01-05T02:30:00Z", text1)
        self.assertIn("CO-1", text1)
        self.assertIn("CO-2", text1)
        self.assertIn("净分差：+20分", text1)
        self.assertNotIn("当前时间", text1)

    def test_utf8_bom_and_blank_trailing_line(self):
        path = self.root / "bom.jsonl"
        payload = json.dumps(line_obj("B", "2026-01-05T09:30:00+08:00", 1, 950),
                             ensure_ascii=False)
        path.write_text(payload + "\n", encoding="utf-8-sig")
        report = check_transactions_file(self.ledger, path)
        self.assertEqual(report["lines"][0]["status"], STATUS_MATCH)

    def test_source_chain_and_timezone_equivalence(self):
        # 同一 UTC 瞬间，不同时区写法必须命中同一更正生效位置。
        a = line_obj("TZ-A", "2026-01-05T10:30:00+08:00", 1, 920)
        b = line_obj("TZ-B", "2026-01-05T02:30:00Z", 1, 920)
        report = self.check([a, b])
        first, second = report["lines"]
        self.assertEqual(first["status"], STATUS_MATCH)
        self.assertEqual(second["status"], STATUS_MATCH)
        self.assertEqual(first["transaction_at"]["utc"], second["transaction_at"]["utc"])
        self.assertEqual(first["source"]["order_id"], "CO-2")
        self.assertEqual(first["expected_unit_price_fen"], 920)
        # 直接来源是更正单时，来源链仍从底档开始。
        self.assertEqual(first["provenance_chain"][0]["kind"], "base")
        self.assertEqual(first["provenance_chain"][0]["value"], 1000)

    def test_negative_zero_fen_prices_are_legal(self):
        ledger = PriceHistoryLedger(self.root / "zero.json")
        ledger.initialize_base(BASE_TIME, [
            {**fact_dict(S1P1D1), "value": 0},
        ])
        txs = parse_transactions(
            json.dumps(line_obj("Z", "2026-01-05T09:30:00+08:00", 2, 0))
        )
        report = check_transactions(ledger, txs)
        line = report["lines"][0]
        self.assertEqual(line["status"], STATUS_MATCH)
        self.assertEqual(line["actual_total_fen"], 0)
        self.assertEqual(line["total_difference_fen"], 0)


if __name__ == "__main__":
    unittest.main()
