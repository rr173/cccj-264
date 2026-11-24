# -*- coding: utf-8 -*-
"""价格历史台账规则测试：python3 -I -m unittest -v test_history_ledger"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from history_ledger import (  # noqa: E402
    STATUS_BEFORE_BASE, STATUS_MISSING, STATUS_NO_PRICE, STATUS_PRICED,
    LedgerInputError, PriceHistoryLedger,
)


def f(s="S1", p="P1", d="D1"):
    return (s, p, d)


def fact_dict(t):
    return {"store_id": t[0], "product_id": t[1], "date_id": t[2]}


def line(t, before, after):
    return {**fact_dict(t), "before": before, "after": after}


def group(gid, *lines_):
    return {"group_id": gid, "facts": list(lines_)}


def order(oid, at, seq, *groups_):
    return {
        "order_id": oid,
        "effective_at": at,
        "sequence": seq,
        "content": {"groups": list(groups_)},
    }


class HistoryLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "ledger.json"
        self.ledger = PriceHistoryLedger(self.path)
        self.a = f()
        self.b = f("S1", "P2")
        self.ledger.initialize_base("2026-01-01T00:00:00+00:00", [
            {**fact_dict(self.a), "value": 100},
            {**fact_dict(self.b), "value": "NOPRICE"},
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_base_statuses_and_sources(self):
        before = self.ledger.query(*self.a, at="2025-12-31T23:59:59+00:00")
        self.assertEqual(before["status"], STATUS_BEFORE_BASE)
        self.assertFalse(before["found"])

        ok = self.ledger.query(*self.a, at="2026-01-02T00:00:00+00:00")
        self.assertEqual(ok["status"], STATUS_PRICED)
        self.assertEqual(ok["value"], 100)
        self.assertEqual(ok["source"]["type"], "base")

        no_price = self.ledger.query(*self.b, at="2026-01-02T00:00:00+00:00")
        self.assertEqual(no_price["status"], STATUS_NO_PRICE)
        self.assertEqual(no_price["value"], "NOPRICE")
        self.assertTrue(no_price["found"])

        missing = self.ledger.query("S9", "P9", "D9",
                                    at="2026-01-02T00:00:00+00:00")
        self.assertEqual(missing["status"], STATUS_MISSING)
        self.assertIsNone(missing["value"])

    def test_out_of_order_late_early_order_recomputes_history(self):
        later = order("O-LATER", "2026-01-10T00:00:00+00:00", 0,
                      group("G", line(self.a, 90, 80)))
        receipt = self.ledger.deliver(later)
        self.assertEqual(receipt["current_order_status"]["O-LATER"], "blocked")
        self.assertEqual(self.ledger.query(*self.a, at="2026-01-11T")["value"], 100)

        early = order("O-EARLY", "2026-01-05T00:00:00+00:00", 0,
                      group("G", line(self.a, 100, 90)))
        self.ledger.deliver(early)

        jan6 = self.ledger.query(*self.a, at="2026-01-06T00:00:00+00:00")
        self.assertEqual(jan6["value"], 90)
        self.assertEqual(jan6["source"]["order_id"], "O-EARLY")

        jan11 = self.ledger.query(*self.a, at="2026-01-11T00:00:00+00:00")
        self.assertEqual(jan11["value"], 80)
        self.assertEqual([x["order_id"] for x in jan11["sources"][1:]],
                         ["O-EARLY", "O-LATER"])
        eval_later = self.ledger.state["evaluation"]["orders"]["O-LATER"]
        self.assertEqual(eval_later["status"], "applied")

    def test_duplicate_delivery_idempotent_but_same_id_different_content_rejected(self):
        o = order("O1", "2026-01-05T00:00:00Z", 0,
                  group("G", line(self.a, 100, 90)))
        self.ledger.deliver(o)
        again = json.loads(json.dumps(o))
        receipt = self.ledger.deliver(again)
        self.assertEqual(receipt["duplicate_order_ids"], ["O1"])
        self.assertEqual(len(self.ledger.state["orders"]), 1)
        self.assertEqual(len(self.ledger.state["price_events"]), 1)

        changed = json.loads(json.dumps(o))
        changed["content"]["groups"][0]["facts"][0]["after"] = 91
        with self.assertRaises(LedgerInputError) as ctx:
            self.ledger.deliver(changed)
        self.assertTrue(any("单号冲突" in e for e in ctx.exception.errors))
        self.assertEqual(self.ledger.query(*self.a, at="2026-02-01T")["value"], 90)

    def test_duplicate_position_rejected(self):
        o1 = order("O1", "2026-01-05T00:00:00Z", 2,
                   group("G1", line(self.a, 100, 90)))
        self.ledger.deliver(o1)
        o2 = order("O2", "2026-01-05T00:00:00Z", 2,
                   group("G2", line(self.b, "NOPRICE", 5)))
        with self.assertRaises(LedgerInputError) as ctx:
            self.ledger.deliver(o2)
        self.assertTrue(any("重复顺序位置" in e for e in ctx.exception.errors))

    def test_same_timestamp_sequence_orders_values(self):
        seq1 = order("O1", "2026-01-05T00:00:00Z", 1,
                     group("G1", line(self.a, 90, 80)))
        seq0 = order("O0", "2026-01-05T00:00:00Z", 0,
                     group("G0", line(self.a, 100, 90)))
        self.ledger.deliver([seq1, seq0])
        result = self.ledger.query(*self.a, at="2026-01-05T00:00:01Z")
        self.assertEqual(result["value"], 80)
        self.assertEqual(result["source"]["order_id"], "O1")

    def test_stale_item_blocks_whole_group_but_independent_group_applies(self):
        other = f("S2", "P1")
        self.ledger.initialize_base  # no-op reference
        # Add another fact by reinitializing a fresh ledger with two facts.
        ledger = PriceHistoryLedger(self.path.parent / "atomic.json")
        ledger.initialize_base("2026-01-01T00:00:00Z", [
            {**fact_dict(self.a), "value": 100},
            {**fact_dict(other), "value": 50},
        ])
        mixed = order("M", "2026-01-10T00:00:00Z", 0,
                      group("BAD", line(self.a, 999, 90)),
                      group("OK", line(other, 50, 45)))
        receipt = ledger.deliver(mixed)
        self.assertEqual(receipt["current_order_status"]["M"], "partially_applied")
        self.assertEqual(ledger.query(*self.a, at="2026-01-11T")["value"], 100)
        self.assertEqual(ledger.query(*other, at="2026-01-11T")["value"], 45)
        conflicts = ledger.list_conflicts()
        self.assertEqual(len(conflicts), 1)
        c = conflicts[0]["conflicts"][0]
        self.assertEqual(c["original_value"], 999)
        self.assertEqual(c["expected_value"], 100)
        self.assertTrue(conflicts[0]["blocked_changes"])

    def test_zero_change_creates_no_event_and_stale_zero_fact_blocks_group(self):
        zero = order("Z", "2026-01-05T00:00:00Z", 0,
                     group("GZ", line(self.a, 100, 100)))
        self.ledger.deliver(zero)
        result = self.ledger.query(*self.a, at="2026-01-06T")
        self.assertEqual(result["value"], 100)
        self.assertEqual(result["source"]["type"], "base")
        self.assertEqual(len(self.ledger.state["price_events"]), 0)

        stale_zero_group = order("Z2", "2026-01-06T00:00:00Z", 0,
                                 group("G2", line(self.a, 101, 101),
                                       line(self.b, "NOPRICE", 50)))
        self.ledger.deliver(stale_zero_group)
        # 零变动事实的变更前价格过期，整组中的 B 变化也不能生效。
        self.assertEqual(self.ledger.query(*self.b, at="2026-01-07T")["value"],
                         "NOPRICE")
        blocked = self.ledger.query(*self.b, at="2026-01-07T")["blocked_attempts"]
        self.assertTrue(blocked)

    def test_atomic_batch_rejects_all_records_on_hard_error(self):
        good = order("GOOD", "2026-01-05T00:00:00Z", 0,
                     group("G", line(self.a, 100, 90)))
        bad = order("BAD-ID", "2026-01-06T00:00:00Z", 0,
                    group("G", line(self.a, 100, 1)))
        bad["order_id"] = "GOOD"  # same ID but different time/content
        with self.assertRaises(LedgerInputError):
            self.ledger.deliver({"orders": [good, bad]})
        reloaded = PriceHistoryLedger(self.path)
        self.assertNotIn("GOOD", reloaded.state["orders"])
        self.assertEqual(reloaded.query(*self.a, at="2026-02-01T")["value"], 100)

    def test_invalid_fact_and_structure_do_not_write(self):
        unknown = order("U", "2026-01-05T00:00:00Z", 0,
                        group("G", line(f("X"), 1, 2)))
        with self.assertRaises(LedgerInputError):
            self.ledger.deliver(unknown)
        with self.assertRaises(LedgerInputError):
            self.ledger.deliver({"orders": [{"order_id": "NO-TIME"}]})
        self.assertEqual(len(PriceHistoryLedger(self.path).state["orders"]), 0)

    def test_arrival_order_does_not_change_final_history(self):
        orders = [
            order("O1", "2026-01-05T00:00:00Z", 0,
                  group("G1", line(self.a, 100, 90))),
            order("O2", "2026-01-10T00:00:00Z", 0,
                  group("G2", line(self.a, 90, 80))),
            order("O3", "2026-01-12T00:00:00Z", 0,
                  group("G3", line(self.a, 80, 75))),
        ]
        chronological = PriceHistoryLedger(self.path.parent / "chrono.json")
        chronological.initialize_base("2026-01-01T00:00:00Z", [
            {**fact_dict(self.a), "value": 100},
        ])
        for item in orders:
            chronological.deliver(item)

        shuffled = PriceHistoryLedger(self.path.parent / "shuffle.json")
        shuffled.initialize_base("2026-01-01T00:00:00Z", [
            {**fact_dict(self.a), "value": 100},
        ])
        for item in [orders[2], orders[0], orders[1]]:
            shuffled.deliver(item)

        at = "2026-02-01T00:00:00Z"
        self.assertEqual(
            chronological.query(*self.a, at=at)["sources"],
            shuffled.query(*self.a, at=at)["sources"],
        )
        self.assertEqual(
            chronological.export_state()["price_events"],
            shuffled.export_state()["price_events"],
        )

    def test_stale_instance_does_not_overwrite_other_committed_order(self):
        ledger1 = PriceHistoryLedger(self.path)
        ledger2 = PriceHistoryLedger(self.path)
        ledger1.deliver(order("O1", "2026-01-05T00:00:00Z", 0,
                              group("G1", line(self.a, 100, 90))))
        # ledger2 was constructed before O1, but delivery must reload under lock.
        ledger2.deliver(order("O2", "2026-01-06T00:00:00Z", 0,
                              group("G2", line(self.a, 90, 85))))
        reopened = PriceHistoryLedger(self.path)
        self.assertEqual(set(reopened.state["orders"]), {"O1", "O2"})
        self.assertEqual(reopened.query(*self.a, at="2026-01-07T")["value"], 85)

    def test_persistence_across_reopen(self):
        self.ledger.deliver(order("O1", "2026-01-05T00:00:00Z", 0,
                                  group("G", line(self.a, 100, 90))))
        reopened = PriceHistoryLedger(self.path)
        result = reopened.query(*self.a, at="2026-01-06T")
        self.assertEqual(result["value"], 90)
        self.assertEqual(result["source"]["order_id"], "O1")

    def test_timezone_equivalent_inputs_share_position(self):
        o = order("TZ", "2026-01-05T10:00:00+08:00", 0,
                  group("G", line(self.a, 100, 90)))
        self.ledger.deliver(o)
        duplicate_utc = order("TZ", "2026-01-05T02:00:00Z", 0,
                              group("G", line(self.a, 100, 90)))
        receipt = self.ledger.deliver(duplicate_utc)
        self.assertEqual(receipt["duplicate_order_ids"], ["TZ"])

    def test_existing_engine_correction_order_adapter(self):
        import sample_data as sd
        from engine import build_correction_order

        approved = build_correction_order(
            sd.build_ledger(),
            {"store": sd.build_store_manifest(), "hq": sd.build_hq_manifest()},
            {"store": sd.build_store_return(), "hq": sd.build_hq_return()},
            sd.THRESHOLD,
        )
        ledger = PriceHistoryLedger(self.path.parent / "engine.json")
        ledger.initialize_base("2026-01-01T00:00:00Z", sd.build_ledger())
        ledger.deliver({
            "order_id": "ENGINE-GOLDEN",
            "effective_at": "2026-02-01T00:00:00Z",
            "sequence": 0,
            "content": approved,  # dataclass should be adapted automatically
        })
        result = ledger.query("S1", "P1", "D1", at="2026-02-02T")
        self.assertEqual(result["value"], 480)
        self.assertEqual(result["source"]["order_id"], "ENGINE-GOLDEN")
        # Rejected F3 remains explicit NOPRICE from base.
        rejected = ledger.query("S1", "P2", "D1", at="2026-02-02T")
        self.assertEqual(rejected["status"], STATUS_NO_PRICE)
        self.assertEqual(rejected["source"]["type"], "base")
        # Six real price differences become six events.
        self.assertEqual(len(ledger.state["price_events"]), 6)


if __name__ == "__main__":
    unittest.main()
