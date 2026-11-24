# -*- coding: utf-8 -*-
"""
价格历史台账可复跑合成样例。

覆盖：
  * 多批次乱序到达；
  * 相同单号/相同内容重复投递；
  * 迟到的早期更正触发全量按生效顺序重算；
  * 同一时点 sequence=0/1 的先后；
  * 一个通过组内一项变更前价格过期 -> 整组挡下，同单其他独立组正常入账；
  * 底档无价、更正后无价、缺少历史记录、早于底档起点；
  * 指定时点查询及“底档/更正单”来源链；
  * 硬错误整批不落盘，以及关闭后重新打开台账继续使用。

运行：
    python3 -I run_history_demo.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from history_ledger import (  # noqa: E402
    LedgerInputError, PriceHistoryLedger, render_query_text,
)
from engine import build_correction_order  # noqa: E402
import sample_data as sd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "samples" / "price_history"
OUT_DIR = ROOT / "samples" / "output" / "price_history"
DB_PATH = OUT_DIR / "price_history_ledger.json"

BASE_TIME = "2026-01-01T00:00:00+08:00"
A = ("S1", "P100", "2026-01-15")
B = ("S1", "P100", "2026-01-16")
C = ("S1", "P200", "2026-01-15")
D = ("S2", "P100", "2026-01-15")
E = ("S2", "P200", "2026-01-15")
X = ("S1", "P999", "2026-01-15")

report_lines: List[str] = []


def out(text: str = "") -> None:
    print(text)
    report_lines.append(text)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def fact(sid: str, pid: str, did: str) -> Dict[str, str]:
    return {"store_id": sid, "product_id": pid, "date_id": did}


def line(f: tuple, before: Any, after: Any) -> Dict[str, Any]:
    return {**fact(*f), "before": before, "after": after}


def group(gid: str, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"group_id": gid, "facts": lines}


def order(oid: str, at: str, seq: int, groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "order_id": oid,
        "effective_at": at,
        "sequence": seq,
        "content": {"schema": "native-groups-v1", "groups": groups},
    }


def base_doc() -> Dict[str, Any]:
    return {
        "effective_from": BASE_TIME,
        "prices": [
            {**fact(*A), "value": 1000},
            {**fact(*B), "value": 1200},
            {**fact(*C), "value": "NOPRICE"},
            {**fact(*D), "value": 800},
            {**fact(*E), "value": 500},
        ],
    }


def build_orders() -> Dict[str, Dict[str, Any]]:
    early = order(
        "CO-001-EARLY", "2026-01-05T10:00:00+08:00", 0,
        [group("G-EARLY", [line(A, 1000, 950)])],
    )
    chain = order(
        "CO-002-CHAIN", "2026-01-10T10:00:00+08:00", 0,
        [group("G-CHAIN", [line(A, 950, 900)])],
    )
    b_update = order(
        "CO-003-B-UPDATE", "2026-01-12T10:00:00+08:00", 0,
        [group("G-B-OK", [line(B, 1200, 1150)])],
    )
    mixed = order(
        "CO-004-MIXED-GROUP", "2026-01-15T10:00:00+08:00", 0,
        [
            group("G-STALE-ALL-OR-NOTHING", [
                # 按最终生效顺序，A 的期望值会是 900（先 CO-001 再 CO-002）。
                line(A, 900, 850),
                # B 已被 CO-003 改为 1150；本单仍写 1200，故整组过期。
                line(B, 1200, 1100),
            ]),
            group("G-INDEPENDENT-S2", [line(D, 800, 760)]),
        ],
    )
    noprice = order(
        "CO-005-TO-NOPRICE", "2026-01-18T10:00:00+08:00", 0,
        [group("G-NOPRICE", [line(E, 500, "NOPRICE")])],
    )
    seq0 = order(
        "CO-006-SEQ0", "2026-01-20T15:00:00+08:00", 0,
        [group("G-SEQ0", [line(A, 900, 880)])],
    )
    seq1 = order(
        "CO-007-SEQ1", "2026-01-20T15:00:00+08:00", 1,
        [group("G-SEQ1", [line(A, 880, 870)])],
    )
    return {
        "early": early, "chain": chain, "b_update": b_update, "mixed": mixed,
        "noprice": noprice, "seq0": seq0, "seq1": seq1,
    }


def write_sample_batches(orders: Dict[str, Dict[str, Any]]) -> Dict[str, Path]:
    paths = {
        "base": SAMPLE_DIR / "base.json",
        "batch1": SAMPLE_DIR / "batch_01_out_of_order.json",
        "batch2": SAMPLE_DIR / "batch_02_more_arrivals.json",
        "batch3": SAMPLE_DIR / "batch_03_late_early.json",
        "batch4": SAMPLE_DIR / "batch_04_duplicate.json",
        "bad_id": SAMPLE_DIR / "batch_invalid_order_id_conflict.json",
        "bad_position": SAMPLE_DIR / "batch_invalid_duplicate_position.json",
        "bad_fact": SAMPLE_DIR / "batch_invalid_fact.json",
        "bad_structure": SAMPLE_DIR / "batch_invalid_structure.json",
    }

    write_json(paths["base"], base_doc())

    # 第一批故意打乱：未来的 mixed、同一时点 seq=1、依赖早期单的 chain 先到。
    write_json(paths["batch1"], {"delivery": "batch-01", "orders": [
        orders["mixed"], orders["seq1"], orders["chain"],
    ]})
    # 第二批同样乱序：seq=0、无价单、B 的较早更新。
    write_json(paths["batch2"], {"delivery": "batch-02", "orders": [
        orders["noprice"], orders["b_update"], orders["seq0"],
    ]})
    write_json(paths["batch3"], {"delivery": "batch-03-late", "orders": [
        orders["early"],
    ]})
    write_json(paths["batch4"], {"delivery": "batch-04-duplicate", "orders": [
        orders["early"],
    ]})

    changed_early = json.loads(json.dumps(orders["early"]))
    changed_early["content"]["groups"][0]["facts"][0]["after"] = 959
    write_json(paths["bad_id"], {"orders": [
        order("CO-TX-GOOD-NEW", "2026-01-22T10:00:00+08:00", 0,
              [group("G-TX", [line(D, 760, 740)])]),
        changed_early,
    ]})

    write_json(paths["bad_position"], {"orders": [
        order("CO-DUP-POS-A", "2026-01-23T10:00:00+08:00", 7,
              [group("G-A", [line(D, 760, 755)])]),
        order("CO-DUP-POS-B", "2026-01-23T10:00:00+08:00", 7,
              [group("G-B", [line(E, "NOPRICE", 499)])]),
    ]})

    bad_fact = order("CO-BAD-FACT", "2026-01-24T10:00:00+08:00", 0, [
        group("G-BAD-FACT", [{
            **fact("S1", "P404", "2026-01-15"),
            "before": 1, "after": 2,
        }]),
    ])
    write_json(paths["bad_fact"], {"orders": [bad_fact]})

    write_json(paths["bad_structure"], {"orders": [
        {"order_id": "CO-BAD-STRUCTURE", "sequence": -1},
    ]})
    return paths


def show_query(ledger: PriceHistoryLedger, title: str, f: tuple, at: str,
               expected_value: Any = None, expected_status: str | None = None) -> Dict[str, Any]:
    out()
    out(f"### {title}")
    result = ledger.query_price(*f, at=at)
    out(render_query_text(result))
    if expected_status is not None:
        assert result["status"] == expected_status, (title, result["status"])
    if expected_value is not None:
        assert result.get("value") == expected_value, (title, result.get("value"))
    return result


def expect_hard_reject(ledger: PriceHistoryLedger, path: Path, label: str) -> None:
    out()
    out(f"### 硬错误整批拒绝：{label}")
    before = ledger.stats()["order_count"]
    try:
        ledger.deliver_file(path)
    except LedgerInputError as exc:
        out(f"已拒绝 {path.name}，台账单据数保持 {before}；错误：")
        for err in exc.errors:
            out(f"  - {err}")
    else:
        raise AssertionError(f"{path} 本应硬拒绝")
    reloaded = PriceHistoryLedger(DB_PATH)
    assert reloaded.stats()["order_count"] == before
    assert "CO-TX-GOOD-NEW" not in reloaded.list_orders().__str__()


def main() -> int:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    orders = build_orders()
    paths = write_sample_batches(orders)

    out("# 价格历史台账合成样例")
    out(f"台账文件：{DB_PATH}")
    out(f"样例投递目录：{SAMPLE_DIR}")

    raw_base = json.loads(paths["base"].read_text(encoding="utf-8"))
    ledger = PriceHistoryLedger(DB_PATH)
    receipt = ledger.initialize_base(raw_base["effective_from"], raw_base["prices"])
    out()
    out(f"初始化底档：{json.dumps(receipt, ensure_ascii=False)}")

    # 先到两批“未来/乱序”单据。
    for key in ("batch1", "batch2"):
        receipt = ledger.deliver_file(paths[key])
        out()
        out(f"投递 {paths[key].name}：")
        out(json.dumps({
            "new_order_ids": receipt["new_order_ids"],
            "current_order_status": receipt["current_order_status"],
            "conflict_groups": [
                f"{c['order_id']}/{c['group_id']}" for c in receipt["conflicts"]
            ],
        }, ensure_ascii=False, indent=2))

    # 迟到早期单到达前：2026-01-08 的 A 仍是底档 1000；CO-002 因期望值不符被挡。
    pre_late = show_query(
        ledger, "迟到早期单到达前，回看 1月8日 A（当前台账只知道底档）",
        A, "2026-01-08T12:00:00+08:00", 1000, "OK",
    )
    assert pre_late["source"]["type"] == "base"
    chain_eval = ledger.state["evaluation"]["orders"]["CO-002-CHAIN"]
    assert chain_eval["status"] == "blocked"
    assert chain_eval["groups"][0]["conflicts"][0]["expected_value"] == 1000

    mixed_eval = ledger.state["evaluation"]["orders"]["CO-004-MIXED-GROUP"]
    assert mixed_eval["status"] == "partially_applied"
    group_status = {g["group_id"]: g["status"] for g in mixed_eval["groups"]}
    assert group_status["G-STALE-ALL-OR-NOTHING"] == "blocked"
    assert group_status["G-INDEPENDENT-S2"] == "applied"

    # 迟到的 CO-001 到达；全量重算后，历史查询结果必须改变。
    receipt = ledger.deliver_file(paths["batch3"])
    out()
    out("投递迟到早期单 batch_03_late_early.json 后重算：")
    out(json.dumps({
        "new_order_ids": receipt["new_order_ids"],
        "current_order_status": receipt["current_order_status"],
    }, ensure_ascii=False, indent=2))

    receipt_dup = ledger.deliver_file(paths["batch4"])
    out()
    out("重复投递 batch_04_duplicate.json：")
    out(json.dumps({
        "new_order_ids": receipt_dup["new_order_ids"],
        "duplicate_order_ids": receipt_dup["duplicate_order_ids"],
        "order_count": ledger.stats()["order_count"],
    }, ensure_ascii=False, indent=2))
    assert receipt_dup["new_order_ids"] == []
    assert receipt_dup["duplicate_order_ids"] == ["CO-001-EARLY"]
    assert ledger.stats()["order_count"] == 7

    # 各类时点查询与来源。
    show_query(ledger, "早于底档生效时点：明确尚无历史",
               A, "2025-12-31T23:30:00+08:00", None, "NO_HISTORY_BEFORE_BASE")
    show_query(ledger, "底档刚生效后：A=1000，来源为底档",
               A, "2026-01-03T12:00:00+08:00", 1000, "OK")
    show_query(ledger, "CO-001 生效前一秒：仍为底档 1000",
               A, "2026-01-05T09:59:59+08:00", 1000, "OK")
    q_jan8 = show_query(
        ledger, "迟到 CO-001 重算后，1月8日 A=950，来源变为 CO-001",
        A, "2026-01-08T12:00:00+08:00", 950, "OK",
    )
    assert q_jan8["source"]["order_id"] == "CO-001-EARLY"

    q_chain = show_query(
        ledger, "1月12日 A=900：CO-001 与 CO-002 共同形成",
        A, "2026-01-12T12:00:00+08:00", 900, "OK",
    )
    assert [x.get("order_id") for x in q_chain["sources"][1:]] == [
        "CO-001-EARLY", "CO-002-CHAIN",
    ]

    q_a_blocked = show_query(
        ledger, "1月16日 A 仍为 900：CO-004 同组 900→850 被冲突组挡下",
        A, "2026-01-16T12:00:00+08:00", 900, "OK",
    )
    assert q_a_blocked["source"]["order_id"] == "CO-002-CHAIN"
    assert q_a_blocked["blocked_attempts"], "应能查到被挡下的 CO-004 变更"

    q_b = show_query(
        ledger, "1月16日 B=1150：来源 CO-003；CO-004 的 1200→1100 因过期被挡",
        B, "2026-01-16T12:00:00+08:00", 1150, "OK",
    )
    assert q_b["source"]["order_id"] == "CO-003-B-UPDATE"
    stale_conflicts = q_b["blocked_attempts"][0]["conflicts"]
    b_conflict = next(c for c in stale_conflicts
                      if c["fact"] == fact(*B))
    assert b_conflict["original_value"] == 1200
    assert b_conflict["expected_value"] == 1150

    show_query(
        ledger, "同一更正单的独立组正常入账：D=760，来源 CO-004/G-INDEPENDENT-S2",
        D, "2026-01-16T12:00:00+08:00", 760, "OK",
    )
    show_query(ledger, "底档明确无价：C=NOPRICE，来源为底档",
               C, "2026-01-03T12:00:00+08:00", "NOPRICE", "NO_PRICE")
    show_query(ledger, "更正单造成的无价：E=NOPRICE，来源 CO-005",
               E, "2026-01-19T12:00:00+08:00", "NOPRICE", "NO_PRICE")
    show_query(ledger, "底档和更正中均无 X：缺少历史记录，而非无价",
               X, "2026-01-10T12:00:00+08:00", None, "MISSING_HISTORY")

    show_query(ledger, "同时点顺序号生效前一秒：A=900",
               A, "2026-01-20T14:59:59+08:00", 900, "OK")
    q_seq = show_query(
        ledger, "同时点 seq=0、seq=1 均生效后：A=870，直接来源 CO-007",
        A, "2026-01-20T15:00:00+08:00", 870, "OK",
    )
    assert q_seq["source"]["order_id"] == "CO-007-SEQ1"
    assert [x.get("order_id") for x in q_seq["sources"][-2:]] == [
        "CO-006-SEQ0", "CO-007-SEQ1",
    ]

    # 硬错误批次：均不得部分写入。
    expect_hard_reject(ledger, paths["bad_id"], "单号相同但内容不同，且同批夹带新单")
    expect_hard_reject(ledger, paths["bad_position"], "重复 (生效时间, sequence) 位置")
    expect_hard_reject(ledger, paths["bad_fact"], "底档外无效事实")
    expect_hard_reject(ledger, paths["bad_structure"], "缺少带时区生效时间等结构错误")

    # 模拟进程重启：从落盘文件重新打开。
    restarted = PriceHistoryLedger(DB_PATH)
    q_restart = restarted.query_price(*A, at="2026-01-21T00:00:00+08:00")
    assert q_restart["value"] == 870
    assert restarted.stats()["order_count"] == 7
    out()
    out("### 重启恢复")
    out(f"重新打开 {DB_PATH}：order_count=7，A@2026-01-21 => {q_restart['value_display']}")

    out()
    out("### 现有签审引擎更正单适配")
    engine_order = build_correction_order(
        sd.build_ledger(),
        {"store": sd.build_store_manifest(), "hq": sd.build_hq_manifest()},
        {"store": sd.build_store_return(), "hq": sd.build_hq_return()},
        sd.THRESHOLD,
    )
    engine_db = OUT_DIR / "engine_adapter_ledger.json"
    if engine_db.exists():
        engine_db.unlink()
    engine_ledger = PriceHistoryLedger(engine_db)
    engine_ledger.initialize_base("2026-01-01T00:00:00+08:00", sd.build_ledger())
    engine_ledger.deliver({
        "order_id": "ENGINE-GOLDEN-001",
        "effective_at": "2026-02-01T10:00:00+08:00",
        "sequence": 0,
        "content": engine_order,
    })
    engine_query = engine_ledger.query_price(
        "S1", "P1", "D1", at="2026-02-02T10:00:00+08:00"
    )
    assert engine_query["value"] == 480
    assert engine_query["source"]["order_id"] == "ENGINE-GOLDEN-001"
    assert len(engine_ledger.state["price_events"]) == 6
    write_json(OUT_DIR / "queries" / "engine_golden_s1_p1_d1.json", engine_query)
    out(
        "现有 engine 黄金样例已入账：S1/P1/D1 => "
        f"{engine_query['value_display']}，来源 {engine_query['source']['order_id']}；"
        "拒发组未生成事件，6 条价差生成 6 条价格变化记录。"
    )

    report_path = OUT_DIR / "history_demo_report.txt"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    # 同时导出最终查询 JSON，便于检查机器可读来源链。
    queries_dir = OUT_DIR / "queries"
    queries_dir.mkdir(parents=True, exist_ok=True)
    for name, f, at in [
        ("a_jan8_late_source", A, "2026-01-08T12:00:00+08:00"),
        ("a_jan12_chain", A, "2026-01-12T12:00:00+08:00"),
        ("a_jan16_blocked_group", A, "2026-01-16T12:00:00+08:00"),
        ("b_jan16_conflict", B, "2026-01-16T12:00:00+08:00"),
        ("missing_history", X, "2026-01-10T12:00:00+08:00"),
        ("before_base", A, "2025-12-31T23:30:00+08:00"),
        ("noprice_from_correction", E, "2026-01-19T12:00:00+08:00"),
        ("sequence_final", A, "2026-01-21T00:00:00+08:00"),
    ]:
        write_json(queries_dir / f"{name}.json",
                   restarted.query_price(*f, at=at))

    out()
    out(f"[输出] 文字样例报告：{report_path}")
    out(f"[输出] 机器可读查询：{queries_dir}")
    out("# 全部价格历史台账断言通过。")
    # 最后再写一次报告，包含结尾路径/断言信息。
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
