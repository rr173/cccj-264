# -*- coding: utf-8 -*-
"""
促销价格清册勘误 —— 离线签审与出单引擎（只读底档，不修改任何输入文件）。

核心概念
========
* 一个“价格事实”的身份是三个稳定 ID：(门店 store_id, 商品 product_id, 日期 date_id)。
* 底档（ledger）中的每个事实取值为：非负整数“分”，或明确“无价”（``NOPRICE``）。
* 两份回件来自两个角色：
    - STORE 门店表：一行是 (门店, 商品)，日期展开成列；
    - HQ    总部表：一行是 (商品, 日期)，门店展开成列。
  每份回件附“发放清单（manifest）”，声明各轴标签 -> 稳定 ID 的映射，
  以及实际发放覆盖的单元（每个单元即一个三 ID 事实）。
* 回件允许重排行/列，但两个单元不得映到同一个事实；坏标签、漏掉发放范围内
  的单元、在范围外填写建议，都属无效回件，整体不出更正单。
* 单元取值三态：
    - 无建议（不在 cells 中或 value 为 None）；
    - 签为无价（value = NOPRICE）；
    - 签为确定的非负整数分（value >= 0 的 int）。
  即使签回底档原值，也是一份真实签字。
* 每方在一个事实上最多一个签值；双方都签且值不同 -> 冲突，该项不通过。
* 出单规则：
    - 提案从有价到有价且 |新-底| > 整数门槛 threshold，或有价/无价互换，
      必须门店、总部两方签同一个值；
    - 其余情形至少一方签即可；
    - 另一方未收到该项（不在其发放范围内）时，不能冒作已签，按缺签处理。
* 成组更正：回件可把若干“已填写单元”声明为必须一起出单；一个签字最多归一个
  组，未归组的签字各自成组。不同表里的组提到同一事实即合并（并查集），
  可经由多个事实连成关联组。组内任一事实未齐签或冲突 -> 整个关联组拒发，
  独立组不受影响。
* 交付：
    - 通过组中“确实变价”的事实才列价差（每条价差全局只列一次），
      零变动的通过组可查到审查状态但不列价差；
    - 拒发组可从任一连带事实追查失败事实、缺签/冲突原因，以及构成连带的
      两份回件坐标。

本模块只依赖标准库，可离线运行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 常量与类型别名
# --------------------------------------------------------------------------- #

NOPRICE = "NOPRICE"          # 明确“无价”
STORE = "store"              # 门店角色
HQ = "hq"                    # 总部角色
ROLES = (STORE, HQ)

# 事实三 ID：(门店 ID, 商品 ID, 日期 ID)
Fact = Tuple[str, str, str]
# 回件坐标：行标签（按角色有不同含义）+ 列标签
Coord = Tuple[str, str]
# 底档取值：int（非负分）或 NOPRICE；事实存在于底档时必有其一
Value = Any


class ValidationError(Exception):
    """输入数据无效（底档/清单/回件），收集到的全部错误见 ``errors``。"""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# --------------------------------------------------------------------------- #
# 输入数据结构（dataclass 只做承载，校验在 validate_* 中完成）
# --------------------------------------------------------------------------- #

@dataclass
class Manifest:
    """
    一份发放清单。

    role               : STORE 或 HQ
    axis_maps          : 轴标签 -> 稳定 ID 的映射，形如
                         {"store": {"标签": "S1", ...}, "product": ..., "date": ...}
                         门店表不会用到 store 之外的全部三轴？实际两表三轴都会用到，
                         只是行/列展开方式不同。
    covered_cells      : 发放范围内的单元，每个单元给出坐标所用的三个标签及三 ID，
                         形如 [{"row": "标签", "column": "标签",
                                "store_id":..,"product_id":..,"date_id":..}]
    """

    role: str
    axis_maps: Dict[str, Dict[str, str]]
    covered_cells: List[Dict[str, str]]


@dataclass
class ReturnDoc:
    """
    一份回件（可重排行列；解析时按标签映射，不依赖排列顺序）。

    cells : 已填写/已声明的单元，每项形如
            {"row": 行标签, "column": 列标签,
             "value": int | NOPRICE | None,     # None/缺省 = 无建议
             "group": 组名 | None}              # 仅已填写单元允许归组
    """

    role: str
    cells: List[Dict[str, Any]]


# --------------------------------------------------------------------------- #
# 校验：标签映射与事实解析
# --------------------------------------------------------------------------- #

def _axis_index(axis_maps: Dict[str, Dict[str, str]], axis: str
                ) -> Dict[str, str]:
    if axis not in axis_maps or not isinstance(axis_maps[axis], dict):
        raise KeyError(axis)
    return axis_maps[axis]


def resolve_fact(role: str, row: str, column: str,
                 axis_maps: Dict[str, Dict[str, str]]) -> Fact:
    """
    按角色的展开方式，把 (行标签, 列标签) 解析成三稳定 ID。

    门店表 STORE：行 = (门店, 商品) 复合标签 "门店标签|商品标签"，列 = 日期标签。
    总部表 HQ   ：行 = (商品, 日期) 复合标签 "商品标签|日期标签"，列 = 门店标签。
    复合标签以 "|" 分隔，与轴映射中的标签配合解析。
    """
    if role == STORE:
        store_lbl, product_lbl = row.split("|", 1)
        date_lbl = column
        sid = _axis_index(axis_maps, "store")[store_lbl]
        pid = _axis_index(axis_maps, "product")[product_lbl]
        did = _axis_index(axis_maps, "date")[date_lbl]
    elif role == HQ:
        product_lbl, date_lbl = row.split("|", 1)
        store_lbl = column
        pid = _axis_index(axis_maps, "product")[product_lbl]
        did = _axis_index(axis_maps, "date")[date_lbl]
        sid = _axis_index(axis_maps, "store")[store_lbl]
    else:  # pragma: no cover - 角色合法性在更外层已校验
        raise KeyError(role)
    return sid, pid, did


def _is_legal_value(v: Any) -> bool:
    return v == NOPRICE or (isinstance(v, int) and not isinstance(v, bool) and v >= 0)


def validate_ledger(ledger: Dict[Fact, Value]) -> List[str]:
    """校验底档：键为三 ID 元组、值为非负整数分或 NOPRICE。"""
    errs: List[str] = []
    for fact, value in ledger.items():
        if not (isinstance(fact, tuple) and len(fact) == 3
                and all(isinstance(x, str) and x for x in fact)):
            errs.append(f"底档事实键非法: {fact!r}")
            continue
        if not _is_legal_value(value):
            errs.append(f"底档事实 {fact} 的值非法（需非负整数分或 {NOPRICE}）: {value!r}")
    return errs


def validate_manifest(manifest: Manifest, ledger: Dict[Fact, Value]) -> List[str]:
    """校验发放清单：角色合法、轴映射无重复稳定 ID、覆盖单元均可解析且在底档内。"""
    errs: List[str] = []
    role = manifest.role
    if role not in ROLES:
        errs.append(f"未知回件角色: {role!r}（应为 {STORE!r} 或 {HQ!r}）")
        return errs

    for axis in ("store", "product", "date"):
        try:
            mapping = _axis_index(manifest.axis_maps, axis)
        except KeyError:
            errs.append(f"[{role}] 发放清单缺少轴映射: {axis}")
            continue
        seen: Dict[str, str] = {}
        for label, stable_id in mapping.items():
            if not isinstance(label, str) or not label:
                errs.append(f"[{role}] 轴 {axis} 存在空标签")
            if not isinstance(stable_id, str) or not stable_id:
                errs.append(f"[{role}] 轴 {axis} 标签 {label!r} 映到非法稳定 ID: {stable_id!r}")
                continue
            if stable_id in seen:
                errs.append(
                    f"[{role}] 轴 {axis} 标签 {label!r} 与 {seen[stable_id]!r} "
                    f"映到同一稳定 ID {stable_id!r}"
                )
            seen[stable_id] = label

    seen_facts: Dict[Fact, Coord] = {}
    for cell in manifest.covered_cells:
        try:
            row, column = cell["row"], cell["column"]
        except (KeyError, TypeError):
            errs.append(f"[{role}] 覆盖单元缺少 row/column: {cell!r}")
            continue
        try:
            fact = resolve_fact(role, row, column, manifest.axis_maps)
        except (KeyError, ValueError) as exc:
            errs.append(f"[{role}] 覆盖单元 ({row!r}, {column!r}) 使用了坏标签: {exc}")
            continue
        if fact in seen_facts:
            errs.append(
                f"[{role}] 覆盖单元 ({row!r}, {column!r}) 与 "
                f"{seen_facts[fact]} 映到同一事实 {fact}"
            )
        seen_facts[fact] = (row, column)
        if fact not in ledger:
            errs.append(f"[{role}] 覆盖单元 {fact} 在底档中不存在")
    return errs


def validate_return(doc: ReturnDoc, manifest: Manifest,
                    ledger: Dict[Fact, Value]) -> List[str]:
    """
    整份回件校验。任一错误都会使该回件无效（连同其他错误一并收集），
    无效回件存在时不出更正单。
    """
    errs: List[str] = []
    role = doc.role
    if role not in ROLES:
        return [f"未知回件角色: {role!r}"]
    if role != manifest.role:
        errs.append(f"[{role}] 回件角色与发放清单角色不一致（清单为 {manifest.role!r}）")

    coverage: Dict[Fact, Coord] = {}
    try:
        for cell in manifest.covered_cells:
            coverage[resolve_fact(role, cell["row"], cell["column"],
                                  manifest.axis_maps)] = (cell["row"], cell["column"])
    except (KeyError, ValueError):
        # 清单本身的错误已在 validate_manifest 中报告，这里无法继续定位。
        return errs

    seen_facts: Dict[Fact, Coord] = {}
    signed_facts = set()

    for cell in doc.cells:
        try:
            row, column = cell["row"], cell["column"]
        except (KeyError, TypeError):
            errs.append(f"[{role}] 回件单元缺少 row/column: {cell!r}")
            continue
        try:
            fact = resolve_fact(role, row, column, manifest.axis_maps)
        except (KeyError, ValueError) as exc:
            errs.append(f"[{role}] 回件单元 ({row!r}, {column!r}) 使用了坏标签: {exc}")
            continue

        if fact in seen_facts:
            errs.append(
                f"[{role}] 回件单元 ({row!r}, {column!r}) 与 {seen_facts[fact]} "
                f"映到同一事实 {fact}：两个单元不得映到同一个事实"
            )
        seen_facts[fact] = (row, column)

        value = cell.get("value")
        has_suggestion = value is not None
        if has_suggestion and not _is_legal_value(value):
            errs.append(f"[{role}] 单元 ({row!r}, {column!r}) 签值非法: {value!r}")
            has_suggestion = False

        if fact not in coverage:
            if has_suggestion:
                # 范围之外填写建议 -> 无效；范围外留空（无建议）不禁止。
                errs.append(
                    f"[{role}] 单元 ({row!r}, {column!r}) -> {fact} "
                    f"在发放范围之外，却填写了建议 {fmt_value(value)}"
                )
            continue

        if has_suggestion:
            signed_facts.add(fact)

        group = cell.get("group")
        # group 允许为组名字符串，或（仅用于暴露违规的）组名列表：
        # 一个签字最多归一个组，列表长度 > 1 即无效。
        if group is not None:
            if isinstance(group, list):
                bad_names = [g for g in group if not isinstance(g, str) or not g]
                if bad_names:
                    errs.append(
                        f"[{role}] 单元 ({row!r}, {column!r}) 的组名非法: {group!r}"
                    )
                if len(group) > 1:
                    errs.append(
                        f"[{role}] 单元 ({row!r}, {column!r}) 的签字被归入多个组 "
                        f"{sorted(group)}：一个签字最多归一个组"
                    )
            elif not isinstance(group, str) or not group:
                errs.append(f"[{role}] 单元 ({row!r}, {column!r}) 的组名非法: {group!r}")
            if not has_suggestion:
                errs.append(
                    f"[{role}] 单元 ({row!r}, {column!r}) 无建议却声明归组 {group!r}；"
                    f"只有已填写单元可以归组"
                )

    # 发放范围内的单元不得漏交（无论有无建议，单元本身必须在回件中出现）。
    for fact, coord in coverage.items():
        if fact not in seen_facts:
            errs.append(
                f"[{role}] 回件漏掉发放范围内的单元 {coord} -> {fact}"
            )

    # 注：“一个签字最多归一个组”已在上面的单元级校验中拦截
    # （group 为列表且长度 > 1 即报错），此处无需重复检查。
    return errs


# --------------------------------------------------------------------------- #
# 并查集：把两份回件里互相牵连的组并成关联组
# --------------------------------------------------------------------------- #

class DSU:
    def __init__(self, items: List[Fact]):
        self.parent = {x: x for x in items}

    def find(self, x: Fact) -> Fact:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: Fact, b: Fact) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# --------------------------------------------------------------------------- #
# 裁定后的输出结构
# --------------------------------------------------------------------------- #

@dataclass
class Signature:
    """一份真实签字（含“签回底档值”的情形）。"""
    role: str
    value: Value                    # int 或 NOPRICE
    coord: Coord                    # 该回件上的实际签字坐标
    group: Optional[str]            # 该签字在该回件中归属的组名


@dataclass
class FactDecision:
    fact: Fact
    base_value: Value
    signatures: List[Signature]
    covered_by: List[str]                       # 哪些角色的发放范围覆盖此事实
    conflict: bool
    proposed_values: List[Value]                # 去重后的签值
    needs_dual: bool                            # 是否需要两方同签同一值
    passed: bool
    effective_value: Optional[Value]           # 通过时的更正后值
    changed: bool                               # 通过且与底档不同
    failure_reasons: List[str]
    failure_codes: List[str]
    group_labels: List[str]                     # 涉及到的组名（含两方）


@dataclass
class PriceChange:
    fact: Fact
    base_value: Value
    new_value: Value
    change_kind: str                            # price_to_price / to_noprice / from_noprice
    signatures: List[Signature]


@dataclass
class RejectedComponent:
    component_id: str
    facts: List[Fact]
    links: List[Dict[str, Any]]                 # 构成连带的两份回件坐标/组信息
    failure_facts: List[Fact]
    reasons: List[str]


@dataclass
class PassedComponent:
    component_id: str
    facts: List[Fact]
    groups: List[Dict[str, Any]]                # 涉及的回件组（角色、组名、坐标）
    has_change: bool


@dataclass
class CorrectionOrder:
    threshold: int
    changes: List[PriceChange]
    passed_components: List[PassedComponent]
    rejected_components: List[RejectedComponent]
    fact_trace: Dict[str, Dict[str, Any]]       # 每个事实的可追查审查记录
    stats: Dict[str, int]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def build_correction_order(ledger: Dict[Fact, Value],
                           manifests: Dict[str, Manifest],
                           returns: Dict[str, ReturnDoc],
                           threshold: int) -> CorrectionOrder:
    """
    离线签审并出单。只读 ledger / manifests / returns，不修改任何入参或文件。
    任一无效回件都抛 ValidationError（错误全部收集），不出更正单。
    """
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0:
        raise ValidationError([f"门槛 threshold 必须是非负整数，实际: {threshold!r}"])

    errors = validate_ledger(ledger)
    for role in ROLES:
        if role not in manifests:
            errors.append(f"缺少 {role} 的发放清单")
        if role not in returns:
            errors.append(f"缺少 {role} 的回件")
    if errors:
        raise ValidationError(errors)
    for role in ROLES:
        errors.extend(validate_manifest(manifests[role], ledger))
    if errors:
        raise ValidationError(errors)
    for role in ROLES:
        errors.extend(validate_return(returns[role], manifests[role], ledger))
    if errors:
        raise ValidationError(errors)

    # ---------------- 解析覆盖范围与签字 ---------------- #
    coverage: Dict[str, Dict[Fact, Coord]] = {role: {} for role in ROLES}
    for role in ROLES:
        m = manifests[role]
        for cell in m.covered_cells:
            f = resolve_fact(role, cell["row"], cell["column"], m.axis_maps)
            coverage[role][f] = (cell["row"], cell["column"])

    # fact -> role -> Signature（每方在一项上最多一个签值，已由校验保证）
    sig_map: Dict[Fact, Dict[str, Signature]] = {}
    for role in ROLES:
        doc = manifests[role]
        for cell in returns[role].cells:
            value = cell.get("value")
            if value is None:
                continue
            f = resolve_fact(role, cell["row"], cell["column"], doc.axis_maps)
            sig_map.setdefault(f, {})[role] = Signature(
                role=role,
                value=value,
                coord=(cell["row"], cell["column"]),
                group=cell.get("group"),
            )

    all_signed_facts = sorted(sig_map.keys())

    # ---------------- 组与并查集 ---------------- #
    dsu = DSU(all_signed_facts)
    # 每个角色内同名组的全部事实联合
    for role in ROLES:
        name_to_facts: Dict[str, List[Fact]] = {}
        for f, per_role in sig_map.items():
            if role in per_role and per_role[role].group is not None:
                name_to_facts.setdefault(per_role[role].group, []).append(f)
        for facts in name_to_facts.values():
            for f in facts[1:]:
                dsu.union(facts[0], f)

    # 关联组：不同表的组提到同一事实 -> 已自然通过共享事实连通；
    # 下面的循环把“同一事实带两个组名”的跨表连带显式记录下来。
    comp_map: Dict[Fact, List[Fact]] = {}
    for f in all_signed_facts:
        comp_map.setdefault(dsu.find(f), []).append(f)
    components = [sorted(v) for v in comp_map.values()]
    components.sort(key=lambda fs: fs[0])

    # ---------------- 逐事实裁定 ---------------- #
    decisions: Dict[Fact, FactDecision] = {}
    for f in all_signed_facts:
        per_role = sig_map[f]
        sigs = [per_role[r] for r in ROLES if r in per_role]
        values = []
        for s in sigs:
            if s.value not in values:
                values.append(s.value)
        base = ledger[f]
        covered_by = sorted(r for r in ROLES if f in coverage[r])
        conflict = len(values) > 1
        # 大改判定：有价->有价且差额超门槛；或有价/无价互换
        if not conflict:
            prop = values[0]
            if base == NOPRICE and prop != NOPRICE:
                needs_dual = True
            elif base != NOPRICE and prop == NOPRICE:
                needs_dual = True
            elif base != NOPRICE and prop != NOPRICE and abs(prop - base) > threshold:
                needs_dual = True
            else:
                needs_dual = False
        else:
            needs_dual = None  # type: ignore[assignment]

        reasons: List[str] = []
        codes: List[str] = []
        if conflict:
            codes.append("CONFLICT")
            detail = " / ".join(
                f"{s.role}签 {fmt_value(s.value)} @ {s.coord[0]}|{s.coord[1]}"
                for s in sigs
            )
            reasons.append(f"两方签值冲突（{detail}）")
        else:
            if needs_dual:
                missing = [r for r in ROLES
                           if r not in per_role and f in coverage[r]]
                uncovered = [r for r in ROLES if f not in coverage[r]]
                if len(per_role) < 2:
                    codes.append("MISSING_DUAL_SIGNATURE")
                    if missing:
                        reasons.append(
                            f"大改/互换须两方同签同一值；缺 {','.join(missing)} 方签字"
                            + (f"（其中 {','.join(uncovered)} 未收到该项，不能冒作已签）"
                               if uncovered else "")
                        )
                    else:
                        reasons.append(
                            f"大改/互换须两方同签同一值；{','.join(uncovered)} "
                            f"未收到该项，不能冒作已签"
                        )
        passed = not conflict and not codes
        effective = values[0] if passed else None
        changed = bool(passed and effective != base)
        group_labels = sorted({s.group for s in sigs if s.group is not None})

        decisions[f] = FactDecision(
            fact=f,
            base_value=base,
            signatures=sigs,
            covered_by=covered_by,
            conflict=conflict,
            proposed_values=values,
            needs_dual=bool(needs_dual) if not conflict else False,
            passed=passed,
            effective_value=effective,
            changed=changed,
            failure_reasons=reasons,
            failure_codes=codes,
            group_labels=group_labels,
        )

    # ---------------- 关联组级裁定 ---------------- #
    seen_change: set = set()
    changes: List[PriceChange] = []
    passed_out: List[PassedComponent] = []
    rejected_out: List[RejectedComponent] = []

    for idx, facts in enumerate(components, start=1):
        cid = f"G{idx:03d}"
        failed = [f for f in facts if not decisions[f].passed]
        # 组内连带关系：跨角色同名/同事实的组、以及同角色同组
        links = _component_links(facts, sig_map)
        groups_meta = _component_groups(facts, sig_map)
        if failed:
            reasons: List[str] = []
            for f in failed:
                d = decisions[f]
                for r in d.failure_reasons:
                    reasons.append(f"事实 {f}: {r}")
            rejected_out.append(RejectedComponent(
                component_id=cid,
                facts=facts,
                links=links,
                failure_facts=failed,
                reasons=reasons,
            ))
        else:
            comp_has_change = any(decisions[f].changed for f in facts)
            passed_out.append(PassedComponent(
                component_id=cid,
                facts=facts,
                groups=groups_meta,
                has_change=comp_has_change,
            ))
            for f in facts:
                d = decisions[f]
                if d.changed:
                    assert f not in seen_change, f"价差重复列出: {f}"
                    seen_change.add(f)
                    changes.append(PriceChange(
                        fact=f,
                        base_value=d.base_value,
                        new_value=d.effective_value,
                        change_kind=_change_kind(d.base_value, d.effective_value),
                        signatures=list(d.signatures),
                    ))

    # ---------------- 事实级追查索引（含零变动通过事实与拒发事实） ---------------- #
    fact_trace: Dict[str, Dict[str, Any]] = {}
    fact_to_component = {f: cid for cid, fs in
                         ([(p.component_id, p.facts) for p in passed_out]
                          + [(r.component_id, r.facts) for r in rejected_out])
                         for f in fs}
    for f in sorted(decisions.keys()):
        d = decisions[f]
        fact_trace["|".join(f)] = {
            "fact": {"store_id": f[0], "product_id": f[1], "date_id": f[2]},
            "base_value": d.base_value,
            "covered_by": d.covered_by,
            "signatures": [
                {"role": s.role, "value": s.value,
                 "coord": {"row": s.coord[0], "column": s.coord[1]},
                 "group": s.group}
                for s in d.signatures
            ],
            "conflict": d.conflict,
            "needs_dual_signature": d.needs_dual,
            "passed": d.passed,
            "effective_value": d.effective_value,
            "changed": d.changed,
            "listed_as_price_diff": d.changed,
            "component_id": fact_to_component.get(f),
            "failure_reasons": d.failure_reasons,
            "failure_codes": d.failure_codes,
        }

    stats = {
        "facts_signed": len(all_signed_facts),
        "components_total": len(components),
        "components_passed": len(passed_out),
        "components_rejected": len(rejected_out),
        "components_passed_zero_change":
            sum(1 for p in passed_out if not p.has_change),
        "price_changes": len(changes),
        "facts_failed": sum(1 for d in decisions.values() if not d.passed),
    }

    order = CorrectionOrder(
        threshold=threshold,
        changes=changes,
        passed_components=passed_out,
        rejected_components=rejected_out,
        fact_trace=fact_trace,
        stats=stats,
    )
    _self_check(order, ledger)
    return order


def _component_links(facts: List[Fact],
                     sig_map: Dict[Fact, Dict[str, Signature]]) -> List[Dict[str, Any]]:
    """
    生成关联组的连带证据：
    * 同一角色内同组名的签字坐标；
    * 同一事实被两方组同时提到（跨表连带的接合点）。
    """
    links: List[Dict[str, Any]] = []
    by_role_group: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for f in facts:
        for role, s in sig_map[f].items():
            if s.group is None:
                continue
            by_role_group.setdefault((role, s.group), []).append(
                {"fact": f, "coord": {"row": s.coord[0], "column": s.coord[1]}}
            )
    for (role, gname), members in sorted(by_role_group.items()):
        links.append({"kind": "same_group_in_one_return", "role": role,
                      "group": gname, "members": members})
    for f in facts:
        per = sig_map[f]
        named = {r: s.group for r, s in per.items() if s.group is not None}
        if len(named) > 1:
            links.append({
                "kind": "shared_fact_across_returns",
                "fact": f,
                "groups": [
                    {"role": r, "group": g,
                     "coord": {"row": per[r].coord[0], "column": per[r].coord[1]}}
                    for r, g in sorted(named.items())
                ],
            })
    return links


def _component_groups(facts: List[Fact],
                      sig_map: Dict[Fact, Dict[str, Signature]]) -> List[Dict[str, Any]]:
    out = []
    seen = set()
    for f in facts:
        for role, s in sig_map[f].items():
            if s.group is None:
                continue
            key = (role, s.group)
            if key in seen:
                continue
            seen.add(key)
            members = [
                {"fact": g, "coord": {"row": sig_map[g][role].coord[0],
                                      "column": sig_map[g][role].coord[1]}}
                for g in facts if role in sig_map[g] and sig_map[g][role].group == s.group
            ]
            out.append({"role": role, "group": s.group, "members": members})
    return sorted(out, key=lambda x: (x["role"], x["group"]))


def _change_kind(base: Value, new: Value) -> str:
    if base == NOPRICE and new != NOPRICE:
        return "from_noprice"
    if base != NOPRICE and new == NOPRICE:
        return "to_noprice"
    return "price_to_price"


def _self_check(order: CorrectionOrder, ledger: Dict[Fact, Value]) -> None:
    """出单前的内部不变量检查（断言，不触碰底档）。"""
    # 1) 每条价差只列一次
    listed = [c.fact for c in order.changes]
    assert len(listed) == len(set(listed)), "价差被重复列出"
    # 2) 价差事实都来自通过组且确实变价
    rejected_facts = {f for r in order.rejected_components for f in r.facts}
    for c in order.changes:
        assert c.fact not in rejected_facts, f"拒发事实出现在价差中: {c.fact}"
        assert c.base_value == ledger[c.fact]
        assert c.new_value != c.base_value, f"零变动事实被列价差: {c.fact}"
    # 3) 事实分组成对互不相交
    all_comp = ([p.facts for p in order.passed_components]
                + [r.facts for r in order.rejected_components])
    flat = [f for fs in all_comp for f in fs]
    assert len(flat) == len(set(flat)), "关联组之间存在重叠事实"


# --------------------------------------------------------------------------- #
# 展示与序列化
# --------------------------------------------------------------------------- #

def fmt_value(v: Value) -> str:
    return "无价" if v == NOPRICE else f"{int(v)}分"


def _sig_to_dict(s: Signature) -> Dict[str, Any]:
    return {"role": s.role, "value": s.value,
            "coord": {"row": s.coord[0], "column": s.coord[1]}, "group": s.group}


def order_to_dict(order: CorrectionOrder) -> Dict[str, Any]:
    return {
        "threshold_fen": order.threshold,
        "stats": order.stats,
        "price_changes": [
            {"fact": {"store_id": c.fact[0], "product_id": c.fact[1],
                      "date_id": c.fact[2]},
             "base_value": c.base_value, "new_value": c.new_value,
             "change_kind": c.change_kind,
             "signatures": [_sig_to_dict(s) for s in c.signatures]}
            for c in order.changes
        ],
        "passed_components": [
            {"component_id": p.component_id, "facts": [list(f) for f in p.facts],
             "has_price_change": p.has_change, "groups": p.groups}
            for p in order.passed_components
        ],
        "rejected_components": [
            {"component_id": r.component_id, "facts": [list(f) for f in r.facts],
             "failure_facts": [list(f) for f in r.failure_facts],
             "reasons": r.reasons, "links": r.links}
            for r in order.rejected_components
        ],
        "fact_trace": order.fact_trace,
    }


def render_text_report(order: CorrectionOrder) -> str:
    """生成中文文字版审批报告。"""
    L: List[str] = []
    L.append("=" * 72)
    L.append("促销价格清册勘误 · 离线签审更正单（底档只读，未做任何修改）")
    L.append("=" * 72)
    s = order.stats
    L.append(
        f"门槛: 有价间差额 > {order.threshold}分 或 有价/无价互换须两方同签；"
        f"其余一方签即可"
    )
    L.append(
        f"统计: 已签事实 {s['facts_signed']} 项 / 关联组 {s['components_total']} 个 "
        f"(通过 {s['components_passed']}，其中零变动 {s['components_passed_zero_change']}；"
        f"拒发 {s['components_rejected']}) / 价差 {s['price_changes']} 条 / "
        f"失败事实 {s['facts_failed']} 项"
    )

    L.append("")
    L.append("一、可审批入仓的价差更正（每条价差仅列一次）")
    L.append("-" * 72)
    if not order.changes:
        L.append("（无）")
    for i, c in enumerate(order.changes, 1):
        f = c.fact
        L.append(
            f"{i:>2}. 门店 {f[0]} / 商品 {f[1]} / 日期 {f[2]}  "
            f"{fmt_value(c.base_value)} -> {fmt_value(c.new_value)}  [{c.change_kind}]"
        )
        for sg in c.signatures:
            L.append(
                f"      签字: {sg.role:<5} {fmt_value(sg.value)}  "
                f"坐标=({sg.coord[0]}, {sg.coord[1]}) 组={sg.group or '-'}"
            )

    L.append("")
    L.append("二、通过组审查状态（零变动组可查状态，不列价差）")
    L.append("-" * 72)
    for p in order.passed_components:
        tag = "含价差" if p.has_change else "零变动（不列价差）"
        L.append(f"{p.component_id} [{tag}] 事实 {len(p.facts)} 项:")
        for f in p.facts:
            tr = order.fact_trace["|".join(f)]
            L.append(
                f"    - {f} 底档={fmt_value(tr['base_value'])} "
                f"更正后={fmt_value(tr['effective_value'])} "
                f"变动={'是' if tr['changed'] else '否'} "
                f"签字方={[x['role'] for x in tr['signatures']]}"
            )

    L.append("")
    L.append("三、拒发关联组（可从任一连带项追查失败项、原因与两份回件坐标）")
    L.append("-" * 72)
    if not order.rejected_components:
        L.append("（无）")
    for r in order.rejected_components:
        L.append(f"{r.component_id} 拒发；连带事实:")
        for f in r.facts:
            tr = order.fact_trace["|".join(f)]
            mark = "失败项" if f in r.failure_facts else "连带项"
            L.append(
                f"    - [{mark}] {f} 底档={fmt_value(tr['base_value'])} "
                f"覆盖发放方={tr['covered_by']}"
            )
            for sg in tr["signatures"]:
                L.append(
                    f"        签字: {sg['role']:<5} {fmt_value(sg['value'])} "
                    f"坐标=({sg['coord']['row']}, {sg['coord']['column']}) "
                    f"组={sg['group'] or '-'}"
                )
        for reason in r.reasons:
            L.append(f"    原因: {reason}")
        L.append("    连带构成（两份回件坐标）:")
        if not r.links:
            L.append("        * （该组为单事实独立组，无跨项连带；失败原因与签字坐标见上）")
        for link in r.links:
            if link["kind"] == "same_group_in_one_return":
                coords = ", ".join(
                    f"{m['fact']}@({m['coord']['row']},{m['coord']['column']})"
                    for m in link["members"]
                )
                L.append(f"        * {link['role']} 回件组 {link['group']!r}: {coords}")
            else:
                parts = ", ".join(
                    f"{g['role']}组{g['group']!r}@({g['coord']['row']},{g['coord']['column']})"
                    for g in link["groups"]
                )
                L.append(f"        * 跨表接合同一事实 {link['fact']}: {parts}")

    L.append("")
    L.append("四、事实级追查索引（每项签字的完整审查轨迹）见 JSON 的 fact_trace")
    L.append("=" * 72)
    return "\n".join(L)


def write_order_json(order: CorrectionOrder, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(order_to_dict(order), fh, ensure_ascii=False, indent=2)
