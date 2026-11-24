# -*- coding: utf-8 -*-
"""规则级单元测试：python3 -I -m unittest -v test_engine"""

from __future__ import annotations

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (  # noqa: E402
    HQ, NOPRICE, STORE, ValidationError, build_correction_order, fmt_value,
)
import sample_data as sd  # noqa: E402


def _good_inputs():
    return (
        sd.build_ledger(),
        {STORE: sd.build_store_manifest(), HQ: sd.build_hq_manifest()},
        {STORE: sd.build_store_return(), HQ: sd.build_hq_return()},
    )


class EngineRuleTests(unittest.TestCase):

    def test_golden_sample_matches_snapshot(self):
        ledger, manifests, returns = _good_inputs()
        order = build_correction_order(ledger, manifests, returns, sd.THRESHOLD)
        self.assertEqual(
            {c.fact for c in order.changes},
            {
                ("S1", "P1", "D1"), ("S1", "P1", "D2"), ("S2", "P2", "D1"),
                ("S2", "P1", "D3"), ("S1", "P2", "D3"), ("S2", "P2", "D3"),
            },
        )
        rejected = {f for r in order.rejected_components for f in r.facts}
        self.assertEqual(
            rejected,
            {("S1", "P2", "D1"), ("S2", "P1", "D1"),
             ("S2", "P1", "D2"), ("S1", "P1", "D3")},
        )

    def test_threshold_boundary_equal_is_small_change(self):
        # 差额恰好等于门槛 -> 小额，一方签即可；超过门槛才需双签。
        ledger, manifests, returns = _good_inputs()
        ledger[("S1", "P1", "D1")] = 500
        for c in returns[STORE].cells:
            if c["row"] == "S1|P1" and c["column"] == "D1":
                c["value"] = 600          # 差额恰好 100 == threshold
        order = build_correction_order(ledger, manifests, returns, 100)
        tr = order.fact_trace["S1|P1|D1"]
        self.assertTrue(tr["passed"])
        self.assertFalse(tr["needs_dual_signature"])
        self.assertTrue(tr["changed"])

    def test_threshold_exceeded_requires_dual(self):
        ledger, manifests, returns = _good_inputs()
        ledger[("S1", "P1", "D1")] = 500
        for c in returns[STORE].cells:
            if c["row"] == "S1|P1" and c["column"] == "D1":
                c["value"] = 601          # 差额 101 > 100，且仅门店签
        order = build_correction_order(ledger, manifests, returns, 100)
        tr = order.fact_trace["S1|P1|D1"]
        self.assertFalse(tr["passed"])
        self.assertTrue(tr["needs_dual_signature"])
        self.assertIn("MISSING_DUAL_SIGNATURE", tr["failure_codes"])

    def test_noprice_swap_each_direction_needs_dual(self):
        ledger, manifests, returns = _good_inputs()
        # F7: 300 -> NOPRICE 双签 -> 通过
        order = build_correction_order(ledger, manifests, returns, 100)
        tr7 = order.fact_trace["S2|P2|D1"]
        self.assertTrue(tr7["passed"])
        self.assertTrue(tr7["needs_dual_signature"])
        self.assertEqual(tr7["effective_value"], NOPRICE)

        # 拆掉总部 F7 签字 -> 互换缺双签，拒发
        returns[HQ].cells = [
            c for c in returns[HQ].cells
            if not (c["row"] == "P2|D1" and c["column"] == "S2")
        ]
        # 但该单元仍在总部发放范围内，必须以无建议单元占行，否则属漏交
        returns[HQ].cells.append(
            {"row": "P2|D1", "column": "S2", "value": None, "group": None})
        order2 = build_correction_order(ledger, manifests, returns, 100)
        tr7b = order2.fact_trace["S2|P2|D1"]
        self.assertFalse(tr7b["passed"])
        self.assertTrue(tr7b["needs_dual_signature"])

    def test_resign_base_value_is_real_signature_but_no_diff(self):
        ledger, manifests, returns = _good_inputs()
        order = build_correction_order(ledger, manifests, returns, 100)
        tr4 = order.fact_trace["S1|P2|D2"]
        self.assertTrue(tr4["passed"])
        self.assertFalse(tr4["changed"])
        self.assertFalse(tr4["listed_as_price_diff"])
        self.assertEqual(len(tr4["signatures"]), 2)     # 两方都签回 200
        # 无价底档签回无价同理
        tr8 = order.fact_trace["S2|P2|D2"]
        self.assertTrue(tr8["passed"])
        self.assertEqual(tr8["effective_value"], NOPRICE)
        self.assertFalse(tr8["changed"])

    def test_conflict_blocks_fact_and_component(self):
        ledger, manifests, returns = _good_inputs()
        order = build_correction_order(ledger, manifests, returns, 100)
        tr9 = order.fact_trace["S1|P1|D3"]
        self.assertTrue(tr9["conflict"])
        self.assertFalse(tr9["passed"])
        self.assertIn("CONFLICT", tr9["failure_codes"])

    def test_group_chain_rejects_whole_component(self):
        ledger, manifests, returns = _good_inputs()
        order = build_correction_order(ledger, manifests, returns, 100)
        comp = next(r for r in order.rejected_components
                    if ("S2", "P1", "D2") in r.facts)
        self.assertEqual(comp.failure_facts, [("S2", "P1", "D1")])
        # F6 是连带项，且 links 给出了总部 GY 组两个事实的回件坐标
        kinds = {link["kind"] for link in comp.links}
        self.assertIn("same_group_in_one_return", kinds)
        # 独立组（如 F12 单签）不受牵连，仍然通过
        self.assertTrue(order.fact_trace["S2|P2|D3"]["passed"])

    def test_cross_return_group_joins_via_shared_fact(self):
        # F2 上门店组 GA 与总部组 GX 接合，应合成一个通过关联组
        ledger, manifests, returns = _good_inputs()
        order = build_correction_order(ledger, manifests, returns, 100)
        comp = next(p for p in order.passed_components
                    if ("S1", "P1", "D2") in p.facts)
        role_group_pairs = {(g["role"], g["group"]) for g in comp.groups}
        self.assertIn((STORE, "GA"), role_group_pairs)
        self.assertIn((HQ, "GX"), role_group_pairs)

    def test_uncovered_party_cannot_be_treated_as_signed(self):
        ledger, manifests, returns = _good_inputs()
        order = build_correction_order(ledger, manifests, returns, 100)
        tr3 = order.fact_trace["S1|P2|D1"]          # 总部未发放 F3
        self.assertEqual(tr3["covered_by"], [STORE])
        self.assertFalse(tr3["passed"])
        self.assertTrue(any("未收到该项" in r for r in tr3["failure_reasons"]))

    def test_each_price_diff_listed_once(self):
        ledger, manifests, returns = _good_inputs()
        order = build_correction_order(ledger, manifests, returns, 100)
        facts = [c.fact for c in order.changes]
        self.assertEqual(len(facts), len(set(facts)))

    def test_ledger_is_not_mutated(self):
        ledger, manifests, returns = _good_inputs()
        import json
        before = json.dumps({ "|".join(k): v for k, v in sorted(ledger.items())},
                            ensure_ascii=False)
        build_correction_order(ledger, manifests, returns, 100)
        after = json.dumps({"|".join(k): v for k, v in sorted(ledger.items())},
                           ensure_ascii=False)
        self.assertEqual(before, after)

    def test_invalid_return_variants_are_all_rejected(self):
        for name, (sm, sr, hm, hr) in sd.build_invalid_variants().items():
            with self.subTest(variant=name):
                with self.assertRaises(ValidationError) as ctx:
                    build_correction_order(
                        sd.build_ledger(),
                        {STORE: sm, HQ: hm}, {STORE: sr, HQ: hr}, 100,
                    )
                self.assertTrue(ctx.exception.errors)

    def test_bad_label_error_message(self):
        sm, sr, hm, hr = sd.build_invalid_variants()["bad_label"]
        with self.assertRaises(ValidationError) as ctx:
            build_correction_order(
                sd.build_ledger(),
                {STORE: sm, HQ: hm}, {STORE: sr, HQ: hr}, 100)
        self.assertTrue(any("坏标签" in e for e in ctx.exception.errors))

    def test_out_of_scope_blank_is_allowed_but_signed_is_not(self):
        # 范围外留空（无建议）不违规；范围外签值才违规
        sm, sr, hm, hr = sd.build_invalid_variants()["suggestion_outside_scope"]
        with self.assertRaises(ValidationError) as ctx:
            build_correction_order(
                sd.build_ledger(),
                {STORE: sm, HQ: hm}, {STORE: sr, HQ: hr}, 100)
        self.assertTrue(any("发放范围之外" in e for e in ctx.exception.errors))

    def test_duplicate_axis_id_in_manifest_rejected(self):
        ledger, manifests, returns = _good_inputs()
        sm = copy.deepcopy(manifests[STORE])
        sm.axis_maps["date"]["D3"] = "D1"     # 两个日期标签映到同一稳定 ID
        with self.assertRaises(ValidationError) as ctx:
            build_correction_order(
                ledger,
                {STORE: sm, HQ: manifests[HQ]}, returns, 100)
        self.assertTrue(any("同一稳定 ID" in e for e in ctx.exception.errors))

    def test_illegal_threshold_rejected(self):
        ledger, manifests, returns = _good_inputs()
        for bad in (-1, 1.5, True, "100"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    build_correction_order(ledger, manifests, returns, bad)

    def test_fmt_value(self):
        self.assertEqual(fmt_value(0), "0分")
        self.assertEqual(fmt_value(NOPRICE), "无价")


if __name__ == "__main__":
    unittest.main(verbosity=2)
