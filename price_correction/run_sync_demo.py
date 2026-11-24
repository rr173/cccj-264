# -*- coding: utf-8 -*-
"""
门店价格同步可复跑合成样例。

一条时间线上覆盖题目要求的三类变化，并在每次发布后审计
“应用差量后的状态 == 台账在该时点对该门店的完整价格状态”：

  1. 完整首包（含首包凭证）；
  2. 纯时点推进：无新修订时的空差量，以及已接收的未来事件随截止推进进入价格；
  3. 迟到的早期更正连带后续组：被挡组转为生效（及反向：已生效组被重新挡下）；
  4. 明确无价：有价 -> NOPRICE 发 remove，NOPRICE -> 有价再发 upsert；
  5. 凭证安全：损坏、跨店、跨台账、倒拨截止时点一律拒绝，且失败不留半成品。

运行：
    python3 -I run_sync_demo.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import NOPRICE  # noqa: E402
from history_ledger import PriceHistoryLedger  # noqa: E402
from sync import (  # noqa: E402
    OP_REMOVE, OP_UPSERT, StoreSyncService, SyncError, apply_package,
    audit_application, digest_json, seal_credential, verify_credential_seal,
)

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "samples" / "output" / "sync"
DB_PATH = OUT_DIR / "sync_ledger.json"
PKG_DIR = OUT_DIR / "packages"
STATE_PATH = OUT_DIR / "client_state_S1.json"

BASE_TIME = "2026-01-01T00:00:00+08:00"
A = ("S1", "P100", "2026-01-15")
B = ("S1", "P200", "2026-01-15")
C = ("S1", "P300", "2026-01-15")
D = ("S2", "P100", "2026-01-15")

report_lines: List[str] = []


def out(text: str = "") -> None:
    print(text)
    report_lines.append(text)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def fact(t):
    return {"store_id": t[0], "product_id": t[1], "date_id": t[2]}


def line(t, before, after):
    return {**fact(t), "before": before, "after": after}


def group(gid, *lines_):
    return {"group_id": gid, "facts": list(lines_)}


def order(oid, at, seq, *groups_):
    return {
        "order_id": oid,
        "effective_at": at,
        "sequence": seq,
        "content": {"groups": list(groups_)},
    }


def brief(package: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "package_type": package["package_type"],
        "snapshot_id": package["credential"]["snapshot_id"],
        "parent_snapshot_id": package["credential"].get("parent_snapshot_id"),
        "as_of": package["as_of"],
        "revision": package["revision"],
        "entry_count": package["entry_count"],
        "entries": [
            {k: v for k, v in e.items() if k in (
                "op", "fact_key", "value", "previous_value", "state", "reason"
            )}
            for e in package["entries"]
        ],
    }


def expect_reject(label: str, fn) -> None:
    out()
    out(f"### 预期拒绝：{label}")
    try:
        fn()
    except SyncError as exc:
        for err in exc.errors:
            out(f"  拒绝原因 - {err}")
    else:
        raise AssertionError(f"{label} 本应被拒绝")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PKG_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    for old in PKG_DIR.glob("sync_*.json"):
        old.unlink()
    if STATE_PATH.exists():
        STATE_PATH.unlink()

    # ------------------------------------------------------------------ #
    # 底档：S1 三个事实（含一个明确无价），S2 一个对照门店事实。
    # ------------------------------------------------------------------ #
    ledger = PriceHistoryLedger(DB_PATH)
    ledger.initialize_base(BASE_TIME, [
        {**fact(A), "value": 1000},
        {**fact(B), "value": 2000},
        {**fact(C), "value": NOPRICE},
        {**fact(D), "value": 800},
    ])
    svc = StoreSyncService(ledger)
    client_state: Dict[str, Any] | None = None

    def publish_and_apply(title: str, package: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal client_state
        out()
        out(f"### {title}")
        out(json.dumps(brief(package), ensure_ascii=False, indent=2))
        write_json(
            PKG_DIR / f"{package['package_type']}_{package['credential']['snapshot_id']}.json",
            package,
        )
        client_state = apply_package(package, client_state)
        report = audit_application(ledger, client_state)
        assert report["ok"], f"审计失败: {report}"
        out(f"  审计：应用后与台账完整状态逐事实一致（{report['fact_count']} 个事实）")
        return package

    # 1) 完整首包：截止 2026-01-11 12:00。此时尚无任何更正入账。
    full = publish_and_apply(
        "1. 完整首包（S1，截止 2026-01-11 12:00）",
        svc.publish_full("S1", "2026-01-11T12:00:00+08:00"),
    )
    assert full["package_type"] == "full"
    assert {e["fact_key"] for e in full["entries"]} == {
        "S1|P100|2026-01-15", "S1|P200|2026-01-15", "S1|P300|2026-01-15",
    }
    assert client_state["mirror"]["S1|P300|2026-01-15"] == NOPRICE

    # 2) 纯时点推进（无新修订）：空差量，但产生新凭证。
    empty_delta = publish_and_apply(
        "2. 纯时点推进、无修订变化：空差量（截止推进到 2026-01-12 12:00）",
        svc.publish_delta("S1", "2026-01-12T12:00:00+08:00",
                          full["credential"]),
    )
    assert empty_delta["entries"] == []

    # ------------------------------------------------------------------ #
    # 乱序接收：后续链单 CO-CHAIN 先到。它声明 A: 950 -> 900，
    # 但当前台账 A 仍是底档 1000，组被冲突挡下，不生效。
    # ------------------------------------------------------------------ #
    ledger.deliver({"orders": [order(
        "CO-CHAIN", "2026-01-10T10:00:00+08:00", 0,
        group("G-CHAIN", line(A, 950, 900)),
    )]})
    out()
    out("### 接收 CO-CHAIN（A 950->900）：期望 950 与台账 1000 不符，组被挡下")
    assert ledger.state["evaluation"]["orders"]["CO-CHAIN"]["status"] == "blocked"

    blocked_delta = publish_and_apply(
        "3. 被挡组不影响价格：续传仍无价格条目（截止 2026-01-12 18:00）",
        svc.publish_delta("S1", "2026-01-12T18:00:00+08:00",
                          empty_delta["credential"]),
    )
    assert blocked_delta["entries"] == []
    assert client_state["mirror"]["S1|P100|2026-01-15"] == 1000

    # 3) 迟到的早期更正到达：A 1000 -> 950；重算后 CO-CHAIN 转为生效，
    #    同一事实在截止时点的最终值是 900，差量只发一条。
    ledger.deliver({"orders": [order(
        "CO-EARLY", "2026-01-05T10:00:00+08:00", 0,
        group("G-EARLY", line(A, 1000, 950)),
    )]})
    out()
    out("### 迟到早期更正 CO-EARLY 到达：重算后 CO-CHAIN 连带转为生效")
    assert ledger.state["evaluation"]["orders"]["CO-CHAIN"]["status"] == "applied"

    late_delta = publish_and_apply(
        "4. 迟到更正连带后续组：A 直接呈现重算后的最终值 900（截止 2026-01-13 12:00）",
        svc.publish_delta("S1", "2026-01-13T12:00:00+08:00",
                          blocked_delta["credential"]),
    )
    entries = {e["fact_key"]: e for e in late_delta["entries"]}
    assert set(entries) == {"S1|P100|2026-01-15"}
    assert entries["S1|P100|2026-01-15"]["op"] == OP_UPSERT
    assert entries["S1|P100|2026-01-15"]["value"] == 900
    assert entries["S1|P100|2026-01-15"]["previous_value"] == 1000

    # 4) 反向连带：另一条迟到更正让已生效组的 before 过期，组被重新挡下，
    #    价格回退。构造：B 先有 2000->1900 生效；再迟到一条 2000->1950，
    #    使 1900 单的 before=2000 过期。
    ledger.deliver({"orders": [order(
        "CO-B-LATER", "2026-01-09T10:00:00+08:00", 0,
        group("G-B-LATER", line(B, 2000, 1900)),
    )]})
    b_applied = publish_and_apply(
        "5a. B 的更正生效：2000 -> 1900",
        svc.publish_delta("S1", "2026-01-13T18:00:00+08:00",
                          late_delta["credential"]),
    )
    assert b_applied["entries"][0]["value"] == 1900

    ledger.deliver({"orders": [order(
        "CO-B-EARLY-LATE", "2026-01-06T10:00:00+08:00", 0,
        group("G-B-EARLY", line(B, 2000, 1950)),
    )]})
    out()
    out("### 迟到更正 CO-B-EARLY-LATE 到达：CO-B-LATER 的 before=2000 过期，被重新挡下")
    assert ledger.state["evaluation"]["orders"]["CO-B-LATER"]["status"] == "blocked"

    reblock_delta = publish_and_apply(
        "5b. 已生效组被重新挡下：B 回退到 1950（截止 2026-01-14 12:00）",
        svc.publish_delta("S1", "2026-01-14T12:00:00+08:00",
                          b_applied["credential"]),
    )
    entries = {e["fact_key"]: e for e in reblock_delta["entries"]}
    assert entries["S1|P200|2026-01-15"]["value"] == 1950
    assert client_state["mirror"]["S1|P200|2026-01-15"] == 1950

    # 5) 纯时点推进第二形态：事件已接收但生效时间在未来。
    ledger.deliver({"orders": [order(
        "CO-FUTURE", "2026-01-20T10:00:00+08:00", 0,
        group("G-FUTURE", line(A, 900, 870)),
    )]})
    before_future = publish_and_apply(
        "6a. 未来事件已接收、尚未生效：截止 2026-01-18 无价格变化",
        svc.publish_delta("S1", "2026-01-18T12:00:00+08:00",
                          reblock_delta["credential"]),
    )
    assert before_future["entries"] == []

    after_future = publish_and_apply(
        "6b. 截止时点推进越过生效瞬间：未来事件进入价格，A 870（2026-01-21 12:00）",
        svc.publish_delta("S1", "2026-01-21T12:00:00+08:00",
                          before_future["credential"]),
    )
    entries = {e["fact_key"]: e for e in after_future["entries"]}
    assert entries["S1|P100|2026-01-15"]["value"] == 870

    # 6) 明确无价：B 1950 -> NOPRICE 发 remove；随后 NOPRICE -> 1880 再 upsert。
    ledger.deliver({"orders": [order(
        "CO-TO-NOPRICE", "2026-01-22T10:00:00+08:00", 0,
        group("G-RM", line(B, 1950, NOPRICE)),
    )]})
    remove_delta = publish_and_apply(
        "7a. 有价变明确无价：B 发 remove（2026-01-23 12:00）",
        svc.publish_delta("S1", "2026-01-23T12:00:00+08:00",
                          after_future["credential"]),
    )
    entries = {e["fact_key"]: e for e in remove_delta["entries"]}
    assert entries["S1|P200|2026-01-15"]["op"] == OP_REMOVE
    assert entries["S1|P200|2026-01-15"]["previous_value"] == 1950
    assert client_state["mirror"]["S1|P200|2026-01-15"] == NOPRICE

    ledger.deliver({"orders": [order(
        "CO-REPRICE", "2026-01-25T10:00:00+08:00", 0,
        group("G-RP", line(B, NOPRICE, 1880)),
    )]})
    reprice_delta = publish_and_apply(
        "7b. 无价重新定价：B 发 upsert 1880（2026-01-26 12:00）",
        svc.publish_delta("S1", "2026-01-26T12:00:00+08:00",
                          remove_delta["credential"]),
    )
    entries = {e["fact_key"]: e for e in reprice_delta["entries"]}
    assert entries["S1|P200|2026-01-15"]["op"] == OP_UPSERT
    assert entries["S1|P200|2026-01-15"]["value"] == 1880

    # 7) 旧凭证可用于不同截止时点的重复续传（不要求沿最新凭证链）。
    #    这是另开的一条续传链：从首包重新应用，再应用复用旧凭证得到的差量。
    reused = svc.publish_delta(
        "S1", "2026-01-27T12:00:00+08:00", before_future["credential"]
    )
    out()
    out("### 8. 复用更早凭证（6a 的凭证）在更晚截止时点重复续传")
    out(json.dumps(brief(reused), ensure_ascii=False, indent=2))
    write_json(PKG_DIR / f"delta_{reused['credential']['snapshot_id']}.json",
               reused)
    reused_state = apply_package(full)
    reused_state = apply_package(empty_delta, reused_state)
    reused_state = apply_package(blocked_delta, reused_state)
    reused_state = apply_package(late_delta, reused_state)
    reused_state = apply_package(b_applied, reused_state)
    reused_state = apply_package(reblock_delta, reused_state)
    reused_state = apply_package(before_future, reused_state)
    reused_state = apply_package(reused, reused_state)
    reused_report = audit_application(ledger, reused_state)
    assert reused_report["ok"], reused_report
    assert reused["credential"]["parent_snapshot_id"] == \
        before_future["credential"]["snapshot_id"]
    out("  审计：另开续传链应用后同样等于台账完整状态")

    # ------------------------------------------------------------------ #
    # 凭证安全：损坏 / 跨店 / 跨台账 / 倒拨截止时点。
    # ------------------------------------------------------------------ #
    last_cred = reprice_delta["credential"]

    tampered = copy.deepcopy(last_cred)
    tampered["as_of"] = "2026-02-01T00:00:00Z"
    expect_reject("封印损坏的凭证（改截止时点后未重封印）",
                  lambda: svc.publish_delta(
                      "S1", "2026-01-28T12:00:00+08:00", tampered))

    expect_reject("跨店凭证（S1 凭证用于 S2）",
                  lambda: svc.publish_delta(
                      "S2", "2026-01-28T12:00:00+08:00", last_cred))

    other_db = OUT_DIR / "other_ledger.json"
    if other_db.exists():
        other_db.unlink()
    other = PriceHistoryLedger(other_db)
    other.initialize_base(BASE_TIME, [{**fact(A), "value": 4242}])
    other_svc = StoreSyncService(other)
    expect_reject("跨台账凭证（底档指纹不同）",
                  lambda: other_svc.publish_delta(
                      "S1", "2026-01-28T12:00:00+08:00", last_cred))

    expect_reject("截止时点倒退",
                  lambda: svc.publish_delta(
                      "S1", "2026-01-01T00:00:00+08:00", last_cred))

    forged = copy.deepcopy(last_cred)
    forged["snapshot_id"] = "snap_never_published_0000000000000000000000_ab"
    seal_credential(forged)
    expect_reject("封印自洽但未登记的伪造凭证",
                  lambda: svc.publish_delta(
                      "S1", "2026-01-28T12:00:00+08:00", forged))

    # 包内容被篡改：条目摘要与凭证不符。
    bad_pkg = copy.deepcopy(reprice_delta)
    bad_pkg["entries"][0]["value"] = 1
    expect_reject("条目被篡改的同步包",
                  lambda: apply_package(bad_pkg, client_state))

    # 失败后登记数量不变，磁盘上也没有对应新包文件。
    snapshot_count_before = len(svc.list_snapshots("S1"))
    pkg_files_before = set(PKG_DIR.glob("sync_*.json"))
    try:
        svc.publish_delta("S1", "2025-12-31T00:00:00+08:00", last_cred)
    except SyncError:
        pass
    assert len(svc.list_snapshots("S1")) == snapshot_count_before
    assert set(PKG_DIR.glob("sync_*.json")) == pkg_files_before
    out()
    out("### 失败原子性：被拒绝的发布不新增凭证登记，也不在包目录留下文件")

    # 已发凭证不可反改：用首包凭证的登记内容与最初封印再校验。
    saved_full = ledger.state["sync_snapshots"]["S1"]["snapshots"][
        full["credential"]["snapshot_id"]
    ]
    verify_credential_seal(saved_full["credential"])
    assert digest_json(saved_full["credential"]) == \
        digest_json(full["credential"])
    out()
    out("### 凭证不可变：历经多轮更正后，首包凭证封印与登记内容仍逐字节一致")

    # 最终客户端镜像落盘并再审计一次。
    write_json(STATE_PATH, client_state)
    final_report = audit_application(ledger, client_state)
    assert final_report["ok"]
    out()
    out("### 最终门店镜像（S1）")
    out(json.dumps(client_state["mirror"], ensure_ascii=False, indent=2))
    out(f"应用包序列: {[p['snapshot_id'] for p in client_state['applied_packages']]}")

    # S2 对照门店独立首包，证明门店间状态互不串扰。
    s2_full = svc.publish_full("S2", "2026-01-27T12:00:00+08:00")
    s2_state = apply_package(s2_full)
    assert audit_application(ledger, s2_state)["ok"]
    assert s2_state["mirror"] == {"S2|P100|2026-01-15": 800}
    write_json(PKG_DIR / f"full_{s2_full['credential']['snapshot_id']}.json",
               s2_full)
    out()
    out("### 对照门店 S2 独立首包：只含 S2 事实，与 S1 凭证体系隔离")

    report_path = OUT_DIR / "sync_demo_report.txt"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    out()
    out(f"[输出] 台账：{DB_PATH}")
    out(f"[输出] 同步包目录：{PKG_DIR}")
    out(f"[输出] 客户端镜像：{STATE_PATH}")
    out(f"[输出] 文字报告：{report_path}")
    out("# 全部门店同步合成样例断言通过。")
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
