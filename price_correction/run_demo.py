# -*- coding: utf-8 -*-
"""
离线签审出单演示入口（标准库，可直接 `python3 -I run_demo.py` 运行）。

流程：
  1. 构造合成底档与双视图（门店形/总部形）发放清单、回件；
  2. 打印双视图，证明同一份事实在两种形状下都能按稳定 ID 对齐；
  3. 调用引擎出更正单，落 JSON 与文字报告（写在独立输出目录，底档只读）；
  4. 跑全部无效回件变体，确认整单拒绝且错误被完整收集；
  5. 对裁定结果做断言自检。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (  # noqa: E402
    HQ, NOPRICE, STORE, ValidationError, build_correction_order,
    render_text_report, write_order_json,
)
import sample_data as sd  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "samples", "output")


def _expected_snapshot():
    """样例的预期裁定结果（与 sample_data 文档字符串保持一致）。"""
    return {
        "price_change_facts": {
            ("S1", "P1", "D1"),   # F1 小额门店单签
            ("S1", "P1", "D2"),   # F2 大额双签
            ("S2", "P2", "D1"),   # F7 互换双签
            ("S2", "P1", "D3"),   # F10 大额双签
            ("S1", "P2", "D3"),   # F11 小额单签（与 F10 同组）
            ("S2", "P2", "D3"),   # F12 总部小额单签
        },
        "rejected_facts": {
            ("S1", "P2", "D1"),   # F3 缺双签（总部未发放）
            ("S2", "P1", "D1"),   # F5 缺门店签
            ("S2", "P1", "D2"),   # F6 被 F5 牵连
            ("S1", "P1", "D3"),   # F9 冲突
        },
        "zero_change_passed_facts": {
            ("S1", "P2", "D2"),   # F4 签回 200
            ("S2", "P2", "D2"),   # F8 签回无价
        },
    }


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)

    ledger = sd.build_ledger()
    ledger_before = json.dumps(
        {"|".join(k): v for k, v in sorted(ledger.items())}, ensure_ascii=False
    )
    store_manifest, hq_manifest = sd.build_store_manifest(), sd.build_hq_manifest()
    store_return, hq_return = sd.build_store_return(), sd.build_hq_return()

    print("#" * 72)
    print("# 第一步：双视图回件（行列已重排；引擎按标签->稳定ID映射解析）")
    print("#" * 72)
    print(sd.render_store_view(store_manifest, store_return))
    print()
    print(sd.render_hq_view(hq_manifest, hq_return))
    print()

    print("#" * 72)
    print("# 第二步：离线签审并出单（底档只读）")
    print("#" * 72)
    order = build_correction_order(
        ledger,
        {STORE: store_manifest, HQ: hq_manifest},
        {STORE: store_return, HQ: hq_return},
        threshold=sd.THRESHOLD,
    )
    report = render_text_report(order)
    print(report)

    json_path = os.path.join(OUT_DIR, "correction_order.json")
    txt_path = os.path.join(OUT_DIR, "correction_order.txt")
    write_order_json(order, json_path)
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(report + "\n")
    print(f"[输出] JSON 更正单: {json_path}")
    print(f"[输出] 文字报告   : {txt_path}")

    # 底档未被修改的留痕检查
    ledger_after = json.dumps(
        {"|".join(k): v for k, v in sorted(ledger.items())}, ensure_ascii=False
    )
    assert ledger_before == ledger_after, "底档在处理过程中被修改！"
    print("[校验] 底档前后序列化一致，确认未被修改。")

    print()
    print("#" * 72)
    print("# 第三步：无效回件变体（必须整单拒绝，并收集全部错误）")
    print("#" * 72)
    for name, (sm, sr, hm, hr) in sd.build_invalid_variants().items():
        try:
            build_correction_order(
                sd.build_ledger(),
                {STORE: sm, HQ: hm}, {STORE: sr, HQ: hr},
                threshold=sd.THRESHOLD,
            )
        except ValidationError as exc:
            print(f"[拒绝] 变体 {name}: 共 {len(exc.errors)} 条错误")
            for e in exc.errors:
                print(f"        - {e}")
        else:  # pragma: no cover
            raise AssertionError(f"变体 {name} 本应被拒绝却出单了")
        print()

    # ---------------- 结果断言 ---------------- #
    snap = _expected_snapshot()
    got_changes = {c.fact for c in order.changes}
    got_rejected = {f for r in order.rejected_components for f in r.facts}
    got_zero = {
        tuple(tr["fact"].values())
        for tr in order.fact_trace.values()
        if tr["passed"] and not tr["changed"]
    }
    assert got_changes == snap["price_change_facts"], \
        f"价差事实不符: {got_changes ^ snap['price_change_facts']}"
    assert got_rejected == snap["rejected_facts"], \
        f"拒发事实不符: {got_rejected ^ snap['rejected_facts']}"
    assert got_zero == snap["zero_change_passed_facts"], \
        f"零变动通过事实不符: {got_zero ^ snap['zero_change_passed_facts']}"
    # 每条价差只列一次
    assert len(order.changes) == len(got_changes)
    # 拒发组可从任一连带事实追查：F6 本身通过条件具备，但须在拒发组中且 links 非空
    f6 = ("S2", "P1", "D2")
    rej = next(r for r in order.rejected_components if f6 in r.facts)
    assert ("S2", "P1", "D1") in rej.failure_facts
    assert rej.links, "拒发组缺少连带坐标证据"
    # 零变动通过组可查状态但不在价差中
    for f in snap["zero_change_passed_facts"]:
        tr = order.fact_trace["|".join(f)]
        assert tr["passed"] and not tr["listed_as_price_diff"]
    # F3 的失败原因必须点明总部未发放、不能冒作已签
    f3_trace = order.fact_trace["S1|P2|D1"]
    assert any("未收到该项" in x for x in f3_trace["failure_reasons"])
    # F9 冲突两方坐标都在
    f9_trace = order.fact_trace["S1|P1|D3"]
    assert f9_trace["conflict"] and len(f9_trace["signatures"]) == 2

    print("#" * 72)
    print("# 全部断言通过：裁定结果、追查索引、价差唯一性与只读保证均符合预期。")
    print("#" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
