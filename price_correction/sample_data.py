# -*- coding: utf-8 -*-
"""
双视图合成样例：同一套底档事实，分别以
  * 门店视图（行=门店|商品，列=日期）
  * 总部视图（行=商品|日期，列=门店）
生成发放清单与回件；回件故意重排行列，验证引擎按标签映射而非按位置解析。

样例事实（12 项，门槛 100 分；底档单位：分，NOPRICE=明确无价）
================================================================
  F1  S1 P1 D1  500       F2  S1 P1 D2  500
  F3  S1 P2 D1  NOPRICE   F4  S1 P2 D2  200
  F5  S2 P1 D1  800       F6  S2 P1 D2  800
  F7  S2 P2 D1  300       F8  S2 P2 D2  NOPRICE
  F9  S1 P1 D3  500       F10 S2 P1 D3  800
  F11 S1 P2 D3  200       F12 S2 P2 D3  300

裁定预期（详见 run_demo 输出对照）
  F1  小额 500->480，门店单签 -> 通过，价差
  F2  大额 500->700，两方同签 700（门店组 GA、总部组 GX）-> 通过，价差
  F3  无价->有价 150，仅门店签（总部未发放 F3，不能冒签）-> 拒发（缺双签）
  F4  签回底档 200，零变动真实签字 -> 通过，零变动组，不列价差
  F5  大额 800->950，仅总部签且门店已发放 F5 -> 拒发（缺门店签）；
      与 F6 同属总部组 GY，整关联组拒发
  F6  小额 800->820 两方同签本可通过，但被 F5 牵连 -> 整组拒发
  F7  无价/有价互换（300->NOPRICE）两方同签 NOPRICE（门店、总部均发放）-> 通过，价差
  F8  签回无价 NOPRICE（仅总部发放并签），零变动 -> 通过，零变动组
  F9  两方签值冲突 470 vs 460 -> 拒发
  F10 大额 800->600 两方同签 -> 通过，价差；门店组 GB 同时含 F10 与 F11
  F11 小额 200->190 门店单签 -> 通过，价差；与 F10 同组（门店组 GB）
  F12 仅总部签 300->290 小额 -> 通过，价差（独立组）
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Tuple

from engine import (
    HQ, NOPRICE, STORE, Fact, Manifest, ReturnDoc,
)

THRESHOLD = 100

# 稳定 ID 与（仅为可读而存在的）轴标签恰好同名；映射关系仍显式声明。
STORES = ["S1", "S2"]
PRODUCTS = ["P1", "P2"]
DATES = ["D1", "D2", "D3"]

AXIS_MAPS: Dict[str, Dict[str, str]] = {
    "store": {s: s for s in STORES},
    "product": {p: p for p in PRODUCTS},
    "date": {d: d for d in DATES},
}


def build_ledger() -> Dict[Fact, Any]:
    raw = {
        ("S1", "P1", "D1"): 500,
        ("S1", "P1", "D2"): 500,
        ("S1", "P2", "D1"): NOPRICE,
        ("S1", "P2", "D2"): 200,
        ("S2", "P1", "D1"): 800,
        ("S2", "P1", "D2"): 800,
        ("S2", "P2", "D1"): 300,
        ("S2", "P2", "D2"): NOPRICE,
        ("S1", "P1", "D3"): 500,
        ("S2", "P1", "D3"): 800,
        ("S1", "P2", "D3"): 200,
        ("S2", "P2", "D3"): 300,
    }
    return dict(raw)


# --------------------------------------------------------------------------- #
# 发放清单：显式声明各角色覆盖的单元（即“实际覆盖的单元”）
# --------------------------------------------------------------------------- #

def store_row(sid: str, pid: str) -> str:
    return f"{sid}|{pid}"


def hq_row(pid: str, did: str) -> str:
    return f"{pid}|{did}"


def build_store_manifest() -> Manifest:
    # 门店发放范围：不含 F3 之外的……门店覆盖 12 项中的 8 项（F3 在店内，门店能收到）
    covered_facts = [
        ("S1", "P1", "D1"), ("S1", "P1", "D2"),
        ("S1", "P2", "D1"), ("S1", "P2", "D2"),
        ("S2", "P1", "D1"), ("S2", "P1", "D2"),
        ("S2", "P2", "D1"),
        ("S1", "P1", "D3"), ("S2", "P1", "D3"),
        ("S1", "P2", "D3"),
    ]
    cells = [
        {"row": store_row(s, p), "column": d,
         "store_id": s, "product_id": p, "date_id": d}
        for (s, p, d) in covered_facts
    ]
    return Manifest(role=STORE, axis_maps=copy.deepcopy(AXIS_MAPS), covered_cells=cells)


def build_hq_manifest() -> Manifest:
    # 总部发放范围：不含 F3（S1 P2 D1）——用于演示“另一方未收到该项，不能冒作已签”
    covered_facts = [
        ("S1", "P1", "D1"), ("S1", "P1", "D2"),
        ("S1", "P2", "D2"),
        ("S2", "P1", "D1"), ("S2", "P1", "D2"),
        ("S2", "P2", "D1"), ("S2", "P2", "D2"),
        ("S1", "P1", "D3"), ("S2", "P1", "D3"),
        ("S2", "P2", "D3"),
    ]
    cells = [
        {"row": hq_row(p, d), "column": s,
         "store_id": s, "product_id": p, "date_id": d}
        for (s, p, d) in covered_facts
    ]
    return Manifest(role=HQ, axis_maps=copy.deepcopy(AXIS_MAPS), covered_cells=cells)


# --------------------------------------------------------------------------- #
# 回件：行列相对发放清单故意重排；无建议单元用 value=None 显式占行
# --------------------------------------------------------------------------- #

def build_store_return() -> ReturnDoc:
    # 列顺序重排：D3, D1, D2；行顺序也打乱。
    cells = [
        # F1 小额单签
        {"row": "S1|P1", "column": "D1", "value": 480, "group": None},
        # F2 大额双签（门店侧组 GA）
        {"row": "S1|P1", "column": "D2", "value": 700, "group": "GA"},
        # F3 无价->有价，仅门店签（总部未发放 F3）
        {"row": "S1|P2", "column": "D1", "value": 150, "group": None},
        # F4 签回底档值（真实签字，零变动）
        {"row": "S1|P2", "column": "D2", "value": 200, "group": None},
        # F5 门店无建议（总部单方签大额 -> 缺签），单元仍须交回
        {"row": "S2|P1", "column": "D1", "value": None, "group": None},
        # F6 门店签 820，与总部组 GY 牵连
        {"row": "S2|P1", "column": "D2", "value": 820, "group": None},
        # F7 有价->无价，门店与总部两方同签 NOPRICE
        {"row": "S2|P2", "column": "D1", "value": NOPRICE, "group": None},
        # F9 门店签 470，与总部 460 冲突
        {"row": "S1|P1", "column": "D3", "value": 470, "group": None},
        # F10 大额双签 600；门店组 GB 同时含 F10、F11
        {"row": "S2|P1", "column": "D3", "value": 600, "group": "GB"},
        # F11 小额单签，同属组 GB
        {"row": "S1|P2", "column": "D3", "value": 190, "group": "GB"},
    ]
    return ReturnDoc(role=STORE, cells=cells)


def build_hq_return() -> ReturnDoc:
    # 列顺序重排：S2, S1；行顺序打乱。
    cells = [
        # F1 总部无建议（小额门店单签即可通过）
        {"row": "P1|D1", "column": "S1", "value": None, "group": None},
        # F2 总部签 700，组 GX 与门店组 GA 在事实 F2 上接合
        {"row": "P1|D2", "column": "S1", "value": 700, "group": "GX"},
        # F4 总部同样签回底档 200
        {"row": "P2|D2", "column": "S1", "value": 200, "group": None},
        # F5 总部单方签大额 950（门店已发放但无建议 -> 缺双签）
        {"row": "P1|D1", "column": "S2", "value": 950, "group": "GY"},
        # F6 总部签 820，与 F5 同组 GY -> 关联组整体拒发
        {"row": "P1|D2", "column": "S2", "value": 820, "group": "GY"},
        # F7 有价->无价，两方同签 NOPRICE
        {"row": "P2|D1", "column": "S2", "value": NOPRICE, "group": None},
        # F8 签回无价（零变动真实签字）
        {"row": "P2|D2", "column": "S2", "value": NOPRICE, "group": None},
        # F9 总部签 460 -> 与门店 470 冲突
        {"row": "P1|D3", "column": "S1", "value": 460, "group": None},
        # F10 总部签 600
        {"row": "P1|D3", "column": "S2", "value": 600, "group": None},
        # F12 总部小额单签 290（门店未发放 F12）
        {"row": "P2|D3", "column": "S2", "value": 290, "group": None},
    ]
    return ReturnDoc(role=HQ, cells=cells)


# --------------------------------------------------------------------------- #
# 无效回件变体（在独立深拷贝上变异，绝不改动有效样例）
# --------------------------------------------------------------------------- #

def build_invalid_variants() -> Dict[str, Tuple[Manifest, ReturnDoc, Manifest, ReturnDoc]]:
    """
    返回 {变体名: (门店清单, 门店回件, 总部清单, 总部回件)}；
    每个变体只破坏一方回件，引擎应拒绝并报告全部错误。
    """
    out: Dict[str, Tuple[Manifest, ReturnDoc, Manifest, ReturnDoc]] = {}

    # 变体 1：坏标签（门店回件引用了轴映射中不存在的日期标签 D9）
    sm, sr = build_store_manifest(), build_store_return()
    sr_bad = ReturnDoc(role=STORE, cells=copy.deepcopy(sr.cells))
    sr_bad.cells[0]["column"] = "D9"
    out["bad_label"] = (sm, sr_bad, build_hq_manifest(), build_hq_return())

    # 变体 2：两个单元映到同一事实（门店回件把 F1 的坐标再写一份，标签合法但事实重复）
    sm, sr = build_store_manifest(), build_store_return()
    sr_dup = ReturnDoc(role=STORE, cells=copy.deepcopy(sr.cells))
    sr_dup.cells.append({"row": "S1|P1", "column": "D1", "value": 479, "group": None})
    out["duplicate_fact"] = (sm, sr_dup, build_hq_manifest(), build_hq_return())

    # 变体 3：漏掉发放范围内的单元（门店删掉 F4 对应单元）
    sm, sr = build_store_manifest(), build_store_return()
    sr_miss = ReturnDoc(
        role=STORE,
        cells=[c for c in copy.deepcopy(sr.cells)
               if not (c["row"] == "S1|P2" and c["column"] == "D2")],
    )
    out["missing_covered_cell"] = (sm, sr_miss, build_hq_manifest(), build_hq_return())

    # 变体 4：在发放范围之外填写建议（总部就未发放的 F3 填值）
    hm, hr = build_hq_manifest(), build_hq_return()
    hr_out = ReturnDoc(role=HQ, cells=copy.deepcopy(hr.cells))
    hr_out.cells.append({"row": "P2|D1", "column": "S1", "value": 160, "group": None})
    out["suggestion_outside_scope"] = (
        build_store_manifest(), build_store_return(), hm, hr_out
    )

    # 变体 5：一个签字归两个组（门店 F1 的同一签字同时标 GA 与 GB）
    sm, sr = build_store_manifest(), build_store_return()
    sr_multi = ReturnDoc(role=STORE, cells=copy.deepcopy(sr.cells))
    for c in sr_multi.cells:
        if c["row"] == "S1|P1" and c["column"] == "D1":
            c["group"] = ["GA", "GB"]
    out["signature_in_two_groups"] = (sm, sr_multi, build_hq_manifest(), build_hq_return())

    # 变体 6：非法签值（负数与字符串）
    hm, hr = build_hq_manifest(), build_hq_return()
    hr_val = ReturnDoc(role=HQ, cells=copy.deepcopy(hr.cells))
    hr_val.cells[0]["value"] = -5
    hr_val.cells[1]["value"] = "cheap"
    out["illegal_value"] = (build_store_manifest(), build_store_return(), hm, hr_val)

    return out


# --------------------------------------------------------------------------- #
# 双视图人类可读渲染（展示“同一份数据，两种形状”）
# --------------------------------------------------------------------------- #

def _fmt(v: Any) -> str:
    if v is None:
        return "  .  "
    if v == NOPRICE:
        return " 无价 "
    return f"{v:>5}"


def render_store_view(manifest: Manifest, doc: ReturnDoc) -> str:
    """门店表：行=门店|商品，列=日期（按回件实际列顺序展示重排效果）。"""
    cols = sorted({c["column"] for c in doc.cells})
    rows = sorted({c["row"] for c in doc.cells})
    index = {(c["row"], c["column"]): c for c in doc.cells}
    lines = ["[门店视图] 行=门店|商品，列=日期（回件可重排行列）",
             "  标注: 数值=签字分 / 无价 / .=无建议 / (组名)=成组更正"]
    header = "行\\列      " + "".join(f"{c:>10}" for c in cols)
    lines.append(header)
    for r in rows:
        parts = [f"{r:<10}"]
        for c in cols:
            cell = index.get((r, c))
            if cell is None:
                parts.append(f"{'--':>10}")
                continue
            txt = _fmt(cell.get("value"))
            if cell.get("group"):
                txt += f"({cell['group']})"
            parts.append(f"{txt:>10}")
        lines.append("".join(parts))
    covered = {f"({c['row']},{c['column']})" for c in manifest.covered_cells}
    lines.append(f"  发放覆盖单元 {len(covered)} 个；回件单元 {len(doc.cells)} 个")
    return "\n".join(lines)


def render_hq_view(manifest: Manifest, doc: ReturnDoc) -> str:
    """总部表：行=商品|日期，列=门店。"""
    cols = sorted({c["column"] for c in doc.cells})
    rows = sorted({c["row"] for c in doc.cells})
    index = {(c["row"], c["column"]): c for c in doc.cells}
    lines = ["[总部视图] 行=商品|日期，列=门店（回件可重排行列）",
             "  标注: 数值=签字分 / 无价 / .=无建议 / (组名)=成组更正"]
    header = "行\\列      " + "".join(f"{c:>10}" for c in cols)
    lines.append(header)
    for r in rows:
        parts = [f"{r:<10}"]
        for c in cols:
            cell = index.get((r, c))
            if cell is None:
                parts.append(f"{'--':>10}")
                continue
            txt = _fmt(cell.get("value"))
            if cell.get("group"):
                txt += f"({cell['group']})"
            parts.append(f"{txt:>10}")
        lines.append("".join(parts))
    covered = {f"({c['row']},{c['column']})" for c in manifest.covered_cells}
    lines.append(f"  发放覆盖单元 {len(covered)} 个；回件单元 {len(doc.cells)} 个")
    return "\n".join(lines)
