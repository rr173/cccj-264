# -*- coding: utf-8 -*-
"""门店价格同步规则测试：python3 -I -m unittest -v test_sync"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import NOPRICE  # noqa: E402
from history_ledger import PriceHistoryLedger  # noqa: E402
from sync import (  # noqa: E402
    PACKAGE_DELTA, PACKAGE_FULL, OP_REMOVE, OP_UPSERT, StoreSyncService,
    SyncError, apply_package, audit_application, digest_json,
    verify_credential_seal, verify_package,
)

HERE = Path(__file__).resolve().parent


def fd(s, p, d):
    return {"store_id": s, "product_id": p, "date_id": d}


def line(s, p, d, before, after):
    return {**fd(s, p, d), "before": before, "after": after}


def group(gid, *lines_):
    return {"group_id": gid, "facts": list(lines_)}


def order(oid, at, seq, *groups_):
    return {
        "order_id": oid,
        "effective_at": at,
        "sequence": seq,
        "content": {"groups": list(groups_)},
    }


class SyncTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "ledger.json"
        self.ledger = PriceHistoryLedger(self.path)
        self.base_prices = [
            {**fd("S1", "P1", "D1"), "value": 100},
            {**fd("S1", "P2", "D1"), "value": 200},
            {**fd("S1", "P3", "D1"), "value": NOPRICE},
            {**fd("S2", "P1", "D1"), "value": 50},
        ]
        self.ledger.initialize_base(
            "2026-01-01T00:00:00+08:00", self.base_prices
        )
        self.service = StoreSyncService(self.ledger)

    def tearDown(self):
        self.tmp.cleanup()

    def deliver(self, *orders_):
        self.ledger.deliver({"orders": list(orders_)})

    def entries_by_key(self, package):
        return {e["fact_key"]: e for e in package["entries"]}

    def apply_chain(self, *packages):
        state = None
        for pkg in packages:
            state = apply_package(pkg, state)
            report = audit_application(self.ledger, state)
            self.assertTrue(
                report["ok"],
                f"应用 {pkg['package_type']} 后与台账完整状态不一致: {report}",
            )
        return state


class FullPackageTests(SyncTestBase):
    def test_full_packages_contain_whole_store_only(self):
        pkg = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        self.assertEqual(pkg["package_type"], PACKAGE_FULL)
        keys = {e["fact_key"] for e in pkg["entries"]}
        self.assertEqual(keys, {"S1|P1|D1", "S1|P2|D1", "S1|P3|D1"})
        by_key = self.entries_by_key(pkg)
        self.assertEqual(by_key["S1|P1|D1"]["value"], 100)
        self.assertEqual(by_key["S1|P3|D1"]["state"], "no_price")
        self.assertEqual(by_key["S1|P3|D1"]["value"], NOPRICE)
        # 其他门店事实不得进入本店首包。
        self.assertNotIn("S2|P1|D1", keys)

        state = apply_package(pkg)
        report = audit_application(self.ledger, state)
        self.assertTrue(report["ok"])
        self.assertEqual(report["fact_count"], 3)
        self.assertEqual(state["mirror"], {
            "S1|P1|D1": 100,
            "S1|P2|D1": 200,
            "S1|P3|D1": NOPRICE,
        })

    def test_cutoff_before_base_rejected(self):
        with self.assertRaises(SyncError):
            self.service.publish_full("S1", "2025-12-31T23:59:59+08:00")

    def test_full_package_tamper_detected(self):
        pkg = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        tampered = copy.deepcopy(pkg)
        tampered["entries"][0]["value"] = 1
        with self.assertRaises(SyncError):
            verify_package(tampered)


class DeltaMechanicsTests(SyncTestBase):
    def test_pure_time_advance_brings_future_event_into_price(self):
        # 事件已接收但生效时间在未来。
        self.deliver(order(
            "CO-FUTURE", "2026-01-15T00:00:00+08:00", 0,
            group("G1", line("S1", "P1", "D1", 100, 90)),
        ))
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        state = apply_package(full)

        # 截止时点在事件生效前：无任何变化事实，发空差量。
        empty_delta = self.service.publish_delta(
            "S1", "2026-01-12T00:00:00+08:00", full["credential"]
        )
        self.assertEqual(empty_delta["entries"], [])
        self.assertEqual(empty_delta["entry_count"], 0)
        state = apply_package(empty_delta, state)

        # 截止时点推进越过生效瞬间：未来事件进入价格。
        delta = self.service.publish_delta(
            "S1", "2026-01-16T00:00:00+08:00", empty_delta["credential"]
        )
        entries = self.entries_by_key(delta)
        self.assertEqual(set(entries), {"S1|P1|D1"})
        self.assertEqual(entries["S1|P1|D1"]["op"], OP_UPSERT)
        self.assertEqual(entries["S1|P1|D1"]["value"], 90)
        self.assertEqual(entries["S1|P1|D1"]["previous_value"], 100)
        state = apply_package(delta, state)
        self.assertEqual(state["mirror"]["S1|P1|D1"], 90)
        self.assertTrue(audit_application(self.ledger, state)["ok"])

    def test_late_correction_unblocks_followup_group(self):
        # CO-CHAIN 先到：期望 950 但台账仍是底档 100，组被挡下。
        self.deliver(order(
            "CO-CHAIN", "2026-01-10T00:00:00+08:00", 0,
            group("G-CHAIN", line("S1", "P1", "D1", 950, 900)),
        ))
        full = self.service.publish_full("S1", "2026-01-08T00:00:00+08:00")
        state = apply_package(full)
        self.assertEqual(state["mirror"]["S1|P1|D1"], 100)

        # 迟到的早期更正到达：100 -> 950，重算后 CO-CHAIN 转为生效。
        self.deliver(order(
            "CO-EARLY", "2026-01-05T00:00:00+08:00", 0,
            group("G-EARLY", line("S1", "P1", "D1", 100, 950)),
        ))
        delta = self.service.publish_delta(
            "S1", "2026-01-12T00:00:00+08:00", full["credential"]
        )
        # 同一事实只出现一条差量，最终值必须是重算后的 900，而不是中间值 950。
        entries = self.entries_by_key(delta)
        self.assertEqual(set(entries), {"S1|P1|D1"})
        self.assertEqual(entries["S1|P1|D1"]["value"], 900)
        self.assertEqual(entries["S1|P1|D1"]["previous_value"], 100)
        state = apply_package(delta, state)
        self.assertEqual(state["mirror"]["S1|P1|D1"], 900)
        self.assertTrue(audit_application(self.ledger, state)["ok"])

    def test_late_correction_can_reblock_applied_group(self):
        # CO-LATER 先到并生效：100 -> 90。
        self.deliver(order(
            "CO-LATER", "2026-01-10T00:00:00+08:00", 0,
            group("G-LATER", line("S1", "P1", "D1", 100, 90)),
        ))
        full = self.service.publish_full("S1", "2026-01-12T00:00:00+08:00")
        state = apply_package(full)
        self.assertEqual(state["mirror"]["S1|P1|D1"], 90)

        # 迟到早期单把 01-05 的价格改成 95；CO-LATER 的 before=100 过期 -> 被挡，
        # 01-12 的有效价格回退为 95。
        self.deliver(order(
            "CO-EARLY", "2026-01-05T00:00:00+08:00", 0,
            group("G-EARLY", line("S1", "P1", "D1", 100, 95)),
        ))
        delta = self.service.publish_delta(
            "S1", "2026-01-12T00:00:00+08:00", full["credential"]
        )
        entries = self.entries_by_key(delta)
        self.assertEqual(entries["S1|P1|D1"]["value"], 95)
        state = apply_package(delta, state)
        self.assertEqual(state["mirror"]["S1|P1|D1"], 95)
        self.assertTrue(audit_application(self.ledger, state)["ok"])

    def test_noprice_emits_remove_and_repricing_emits_upsert(self):
        self.deliver(order(
            "CO-REMOVE", "2026-01-08T00:00:00+08:00", 0,
            group("G-RM", line("S1", "P2", "D1", 200, NOPRICE)),
        ))
        full = self.service.publish_full("S1", "2026-01-03T00:00:00+08:00")
        state = apply_package(full)

        delta_rm = self.service.publish_delta(
            "S1", "2026-01-10T00:00:00+08:00", full["credential"]
        )
        entries = self.entries_by_key(delta_rm)
        self.assertEqual(entries["S1|P2|D1"]["op"], OP_REMOVE)
        self.assertEqual(entries["S1|P2|D1"]["previous_value"], 200)
        state = apply_package(delta_rm, state)
        self.assertEqual(state["mirror"]["S1|P2|D1"], NOPRICE)
        self.assertTrue(audit_application(self.ledger, state)["ok"])

        # 无价 -> 有价：重新 upsert。
        self.deliver(order(
            "CO-REPRICE", "2026-01-15T00:00:00+08:00", 0,
            group("G-RP", line("S1", "P2", "D1", NOPRICE, 180)),
        ))
        delta_up = self.service.publish_delta(
            "S1", "2026-01-16T00:00:00+08:00", delta_rm["credential"]
        )
        entries = self.entries_by_key(delta_up)
        self.assertEqual(entries["S1|P2|D1"]["op"], OP_UPSERT)
        self.assertEqual(entries["S1|P2|D1"]["value"], 180)
        state = apply_package(delta_up, state)
        self.assertEqual(state["mirror"]["S1|P2|D1"], 180)
        self.assertTrue(audit_application(self.ledger, state)["ok"])

    def test_unchanged_facts_are_never_sent(self):
        # 只动 S1|P1；P2 未变价，S2 是其他门店。
        self.deliver(order(
            "CO-1", "2026-01-08T00:00:00+08:00", 0,
            group("G1", line("S1", "P1", "D1", 100, 99)),
        ))
        # 零变动通过组：触及 P2 但价格不变。
        self.deliver(order(
            "CO-ZERO", "2026-01-09T00:00:00+08:00", 0,
            group("G0", line("S1", "P2", "D1", 200, 200)),
        ))
        full = self.service.publish_full("S1", "2026-01-03T00:00:00+08:00")
        delta = self.service.publish_delta(
            "S1", "2026-01-12T00:00:00+08:00", full["credential"]
        )
        self.assertEqual({e["fact_key"] for e in delta["entries"]},
                         {"S1|P1|D1"})

    def test_same_revision_same_cutoff_publishes_empty_delta(self):
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        delta = self.service.publish_delta(
            "S1", "2026-01-10T00:00:00+08:00", full["credential"]
        )
        self.assertEqual(delta["entries"], [])
        self.assertEqual(delta["credential"]["parent_snapshot_id"],
                         full["credential"]["snapshot_id"])
        state = self.apply_chain(full, delta)
        self.assertTrue(audit_application(self.ledger, state)["ok"])

    def test_old_credential_reusable_for_different_cutoffs(self):
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        # 不用最新凭证，而是反复用首包凭证在不同截止时点续传。
        d1 = self.service.publish_delta(
            "S1", "2026-01-11T00:00:00+08:00", full["credential"]
        )
        d2 = self.service.publish_delta(
            "S1", "2026-01-12T00:00:00+08:00", full["credential"]
        )
        self.assertEqual(d1["credential"]["parent_snapshot_id"],
                         full["credential"]["snapshot_id"])
        self.assertEqual(d2["credential"]["parent_snapshot_id"],
                         full["credential"]["snapshot_id"])
        self.assertNotEqual(d1["credential"]["snapshot_id"],
                            d2["credential"]["snapshot_id"])
        self.assertEqual(d1["as_of"], "2026-01-10T16:00:00Z")
        self.assertEqual(d2["as_of"], "2026-01-11T16:00:00Z")


class CredentialSecurityTests(SyncTestBase):
    def test_seal_detects_field_tampering(self):
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        cred = copy.deepcopy(full["credential"])
        cred["as_of"] = "2026-02-01T00:00:00Z"
        with self.assertRaises(SyncError):
            verify_credential_seal(cred)

        cred2 = copy.deepcopy(full["credential"])
        cred2["store_id"] = "S2"
        with self.assertRaises(SyncError):
            verify_credential_seal(cred2)

        cred3 = copy.deepcopy(full["credential"])
        cred3["seal"] = "sha256:" + "0" * 64
        with self.assertRaises(SyncError):
            verify_credential_seal(cred3)

    def test_cross_store_credential_rejected(self):
        full_s1 = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        with self.assertRaises(SyncError) as ctx:
            self.service.publish_delta(
                "S2", "2026-01-11T00:00:00+08:00", full_s1["credential"]
            )
        self.assertIn("跨店", str(ctx.exception))

    def test_cross_ledger_credential_rejected(self):
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        other_path = Path(self.tmp.name) / "other" / "ledger.json"
        other = PriceHistoryLedger(other_path)
        other.initialize_base("2026-01-01T00:00:00+08:00", [
            {**fd("S1", "P1", "D1"), "value": 999},  # 底档不同 -> 不同台账身份
        ])
        other_service = StoreSyncService(other)
        with self.assertRaises(SyncError) as ctx:
            other_service.publish_delta(
                "S1", "2026-01-11T00:00:00+08:00", full["credential"]
            )
        self.assertIn("跨台账", str(ctx.exception))

    def test_unknown_credential_rejected(self):
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        forged = copy.deepcopy(full["credential"])
        forged["snapshot_id"] = "snap_unknown_000000000000000000000000_abcd1234"
        # 重新封印使封印自洽，但登记中不存在该快照。
        from sync import seal_credential
        seal_credential(forged)
        with self.assertRaises(SyncError) as ctx:
            self.service.publish_delta(
                "S1", "2026-01-11T00:00:00+08:00", forged
            )
        self.assertIn("不在本台账已发快照登记", str(ctx.exception))

    def test_cutoff_never_moves_backward(self):
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        with self.assertRaises(SyncError):
            self.service.publish_delta(
                "S1", "2026-01-09T23:59:59+08:00", full["credential"]
            )

    def test_published_credential_is_immutable(self):
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        old_cred_copy = copy.deepcopy(full["credential"])
        self.deliver(order(
            "CO-1", "2026-01-12T00:00:00+08:00", 0,
            group("G1", line("S1", "P1", "D1", 100, 88)),
        ))
        _ = self.service.publish_delta(
            "S1", "2026-01-13T00:00:00+08:00", full["credential"]
        )
        # 新修订发布后，旧凭证仍可验封印，且登记内容逐字节未变。
        verify_credential_seal(old_cred_copy)
        saved = self.ledger.state["sync_snapshots"]["S1"]["snapshots"][
            old_cred_copy["snapshot_id"]
        ]
        self.assertEqual(digest_json(saved["credential"]),
                         digest_json(old_cred_copy))
        self.assertEqual(saved["as_of"], old_cred_copy["as_of"])

    def test_delta_before_full_rejected_on_client(self):
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        self.deliver(order(
            "CO-1", "2026-01-12T00:00:00+08:00", 0,
            group("G1", line("S1", "P1", "D1", 100, 88)),
        ))
        delta = self.service.publish_delta(
            "S1", "2026-01-13T00:00:00+08:00", full["credential"]
        )
        with self.assertRaises(SyncError):
            apply_package(delta, None)

    def test_wrong_parent_chain_rejected_on_client(self):
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        self.deliver(order(
            "CO-1", "2026-01-12T00:00:00+08:00", 0,
            group("G1", line("S1", "P1", "D1", 100, 88)),
        ))
        d1 = self.service.publish_delta(
            "S1", "2026-01-13T00:00:00+08:00", full["credential"]
        )
        d2 = self.service.publish_delta(
            "S1", "2026-01-14T00:00:00+08:00", d1["credential"]
        )
        # 客户端只应用了首包，却收到 d2（父凭证是 d1）。
        state = apply_package(full)
        with self.assertRaises(SyncError):
            apply_package(d2, state)


class IncrementalAccessTests(SyncTestBase):
    """续传不得遍历未变价目/重建整店快照。"""

    def _build_large_base(self, n_facts=3000):
        prices = [
            {**fd("S1", f"P{i:05d}", "D1"), "value": 1000 + i}
            for i in range(n_facts)
        ]
        prices += [
            {**fd("S2", f"P{i:05d}", "D1"), "value": 1000 + i}
            for i in range(n_facts)
        ]
        large_path = Path(self.tmp.name) / "large" / "ledger.json"
        ledger = PriceHistoryLedger(large_path)
        ledger.initialize_base("2026-01-01T00:00:00+08:00", prices)
        return ledger, prices

    def test_delta_reads_only_touched_facts(self):
        ledger, prices = self._build_large_base()
        service = StoreSyncService(ledger)

        query_calls = []
        original_query = ledger.query_price

        def counting_query(*args, **kwargs):
            query_calls.append(args)
            return original_query(*args, **kwargs)

        ledger.query_price = counting_query  # type: ignore[method-assign]

        full = service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        full_queries = len(query_calls)
        self.assertEqual(full_queries, 3000)  # 首包允许全量。
        query_calls.clear()

        # 只投递两条变更，且只涉及 S1 的两个事实。
        ledger.deliver({"orders": [
            order("CO-1", "2026-01-12T00:00:00+08:00", 0,
                  group("G1", line("S1", "P00001", "D1", 1001, 900),
                        line("S1", "P00002", "D1", 1002, 800))),
            order("CO-2-S2", "2026-01-12T00:00:00+08:00", 1,
                  group("G2", line("S2", "P00003", "D1", 1003, 700))),
        ]})

        # 续传准备阶段调用全店枚举必须失败：证明没有把全量扫描搬进续传。
        import sync as sync_mod
        original_scan = sync_mod._store_base_facts

        def forbidden_scan(*_args, **_kwargs):
            raise AssertionError("续传不得枚举整店底档事实")

        sync_mod._store_base_facts = forbidden_scan
        try:
            delta = service.publish_delta(
                "S1", "2026-01-13T00:00:00+08:00", full["credential"]
            )
        finally:
            sync_mod._store_base_facts = original_scan
        # 读取次数与价目总量脱钩，只与候选（变更依据/水位线）规模相关。
        self.assertLess(len(query_calls), 10)
        self.assertEqual(
            {e["fact_key"] for e in delta["entries"]},
            {"S1|P00001|D1", "S1|P00002|D1"},
        )
        # 登记的稀疏状态只含已触及事实，未复制整店 3000 条。
        saved = ledger.state["sync_snapshots"]["S1"]["snapshots"][
            delta["credential"]["snapshot_id"]
        ]
        self.assertLess(len(saved["basis_states"]), 10)

        # 应用 + 审计仍与 3000 事实的完整台账状态逐事实相等。
        state = apply_package(full)
        state = apply_package(delta, state)
        report = audit_application(ledger, state)
        self.assertTrue(report["ok"])
        self.assertEqual(report["fact_count"], 3000)
        self.assertEqual(state["mirror"]["S1|P00001|D1"], 900)
        self.assertEqual(state["mirror"]["S1|P00999|D1"], 1999)


class PersistenceTests(SyncTestBase):
    def test_registry_persists_and_survives_restart(self):
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        self.deliver(order(
            "CO-1", "2026-01-12T00:00:00+08:00", 0,
            group("G1", line("S1", "P1", "D1", 100, 88)),
        ))
        delta = self.service.publish_delta(
            "S1", "2026-01-13T00:00:00+08:00", full["credential"]
        )

        restarted = PriceHistoryLedger(self.path)
        service2 = StoreSyncService(restarted)
        # 在重开的实例上再投递并续传：旧凭证在新进程里仍被承认。
        restarted.deliver(order(
            "CO-2", "2026-01-14T00:00:00+08:00", 0,
            group("G2", line("S1", "P2", "D1", 200, 150)),
        ))
        delta2 = service2.publish_delta(
            "S1", "2026-01-15T00:00:00+08:00", delta["credential"]
        )
        self.assertEqual(
            self.entries_by_key(delta2)["S1|P2|D1"]["value"], 150
        )
        snapshots = service2.list_snapshots("S1")
        self.assertEqual([s["snapshot_id"] for s in snapshots], [
            full["credential"]["snapshot_id"],
            delta["credential"]["snapshot_id"],
            delta2["credential"]["snapshot_id"],
        ])

        # 落盘文件本身包含凭证登记与封印，可被外部审计读取。
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("sync_snapshots", on_disk)
        self.assertEqual(
            len(on_disk["sync_snapshots"]["S1"]["snapshots"]), 3
        )

    def test_failed_publish_leaves_no_partial_package(self):
        full = self.service.publish_full("S1", "2026-01-10T00:00:00+08:00")
        before = len(self.service.list_snapshots("S1"))
        # 倒拨截止时点：发布在落盘前失败，登记数量不变，也不返回任何包。
        with self.assertRaises(SyncError):
            self.service.publish_delta(
                "S1", "2026-01-01T00:00:00+08:00", full["credential"]
            )
        self.assertEqual(len(self.service.list_snapshots("S1")), before)


class CliTests(SyncTestBase):
    def _run_cli(self, *args):
        proc = subprocess.run(
            [sys.executable, "-I", str(HERE / "sync_cli.py"), *args],
            capture_output=True, text=True, check=False,
        )
        return proc

    def test_cli_full_delta_apply_audit_roundtrip(self):
        out_dir = Path(self.tmp.name) / "packages"
        state_path = Path(self.tmp.name) / "client_state.json"

        proc = self._run_cli(
            "full", "--db", str(self.path), "--store", "S1",
            "--as-of", "2026-01-10T00:00:00+08:00",
            "--output-dir", str(out_dir),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        full_path = next(out_dir.glob("sync_full_*.json"))

        self.deliver(order(
            "CO-1", "2026-01-12T00:00:00+08:00", 0,
            group("G1", line("S1", "P1", "D1", 100, 77)),
        ))
        proc = self._run_cli(
            "delta", "--db", str(self.path), "--store", "S1",
            "--as-of", "2026-01-13T00:00:00+08:00",
            "--credential", str(full_path),
            "--output-dir", str(out_dir),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        delta_path = next(out_dir.glob("sync_delta_*.json"))

        for pkg_path in (full_path, delta_path):
            proc = self._run_cli(
                "apply", "--package", str(pkg_path),
                "--state", str(state_path),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = self._run_cli(
            "audit", "--db", str(self.path), "--state", str(state_path)
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["ok"])

    def test_cli_rejects_cross_store_credential(self):
        out_dir = Path(self.tmp.name) / "packages"
        proc = self._run_cli(
            "full", "--db", str(self.path), "--store", "S1",
            "--as-of", "2026-01-10T00:00:00+08:00",
            "--output-dir", str(out_dir),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        full_path = next(out_dir.glob("sync_full_*.json"))
        proc = self._run_cli(
            "delta", "--db", str(self.path), "--store", "S2",
            "--as-of", "2026-01-11T00:00:00+08:00",
            "--credential", str(full_path),
            "--output-dir", str(out_dir),
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("跨店", proc.stderr)
        self.assertEqual(list(out_dir.glob("sync_delta_*.json")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
