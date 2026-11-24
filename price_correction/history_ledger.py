# -*- coding: utf-8 -*-
"""
可持续维护的价格历史台账。

设计要点
========
* 底档是 ``effective_from`` 时点的起始快照；明确 ``NOPRICE`` 与“没有历史记录”
  是两种不同状态。
* 每笔已批准更正单有唯一 ``order_id``、带时区的 ``effective_at``、同一生效时点内
  的非负整数 ``sequence``。``(effective_at, sequence)`` 是唯一入账位置。
* 投递可以乱序、重复。落盘的是已接受投递的规范化原始单据；所有生效判定都从底档
  按生效时间/顺序重新计算，因此迟到的早期更正会自动重算后续组的冲突与来源。
* 单号相同但内容/时间/顺序不同、重复位置、无效事实或结构错误属于硬错误：整批
  投递不落任何新记录。组内“变更前价格”过期属于业务冲突：该通过组整体不入账并
  记录冲突，其他互不关联的通过组不受影响。
* 状态通过临时文件 + ``os.replace`` 原子替换落盘，重启后从同一 JSON 文件继续。

本模块只用标准库。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

try:  # Linux/macOS 可用；不可用时退化为进程内原子替换保证。
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:  # 兼容 `python3 history_cli.py` 与 `from price_correction import ...`
    from engine import NOPRICE, _is_legal_value
except ImportError:  # pragma: no cover - 包方式导入分支
    from .engine import NOPRICE, _is_legal_value

Fact = Tuple[str, str, str]
MISSING = object()

STATUS_PRICED = "OK"
STATUS_NO_PRICE = "NO_PRICE"
STATUS_MISSING = "MISSING_HISTORY"
STATUS_BEFORE_BASE = "NO_HISTORY_BEFORE_BASE"
STATUS_NO_BASE = "BASE_NOT_INITIALIZED"


class LedgerInputError(ValueError):
    """投递/底档存在硬错误；一次投递中的全部错误见 ``errors``。"""

    def __init__(self, errors: Union[str, List[str]]):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class LedgerPersistenceError(RuntimeError):
    """台账文件无法安全读取或写入。"""


# --------------------------------------------------------------------------- #
# 基础规范化工具
# --------------------------------------------------------------------------- #

def parse_aware_datetime(value: Any, *, allow_naive: bool = False,
                        field: str = "时间") -> datetime:
    """把 ISO 8601 字符串/datetime 转成 UTC datetime。

    更正单生效时间必须显式带时区；查询时点若意外为 naive，则按 UTC 解释，以保证
    样例和测试可复现。
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise LedgerInputError(f"{field}为空")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        if text.endswith("T"):
            text = text[:-1]
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise LedgerInputError(f"{field}不是合法 ISO 8601 时间: {value!r} ({exc})")
    else:
        raise LedgerInputError(f"{field}必须是 ISO 8601 字符串或 datetime: {value!r}")

    if dt.tzinfo is None or dt.utcoffset() is None:
        if not allow_naive:
            raise LedgerInputError(f"{field}必须带时区偏移，实际为无时区时间: {value!r}")
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canonical_datetime(value: Any, *, field: str = "生效时间") -> Tuple[str, datetime, str]:
    """返回（规范 UTC 字符串, UTC datetime, 输入原文）。"""
    raw = value if isinstance(value, str) else (
        value.isoformat() if isinstance(value, datetime) else repr(value)
    )
    dt = parse_aware_datetime(value, field=field)
    return dt.isoformat().replace("+00:00", "Z"), dt, raw


def normalize_value(value: Any, *, field: str = "价格") -> Any:
    """规范化价格：非负 int 或 NOPRICE。显式 null/NO_PRICE/无价也视为 NOPRICE。"""
    if value is None:
        return NOPRICE
    if isinstance(value, bool):
        raise LedgerInputError(f"{field}不能是布尔值: {value!r}")
    if _is_legal_value(value):
        return value
    if isinstance(value, str) and value.strip().upper() in {"NO_PRICE", "NOPRICE"}:
        return NOPRICE
    if value == "无价":
        return NOPRICE
    raise LedgerInputError(
        f"{field}必须是非负整数分、{NOPRICE} 或显式 null（无价），实际: {value!r}"
    )


def normalize_fact(raw: Any) -> Fact:
    """把 dict / 三元组 / ``S|P|D`` 字符串规范化为字符串三元组。"""
    if isinstance(raw, dict):
        def pick(*keys: str) -> Any:
            for key in keys:
                if key in raw:
                    return raw[key]
            raise KeyError(keys[0])

        try:
            sid = pick("store_id", "storeId", "store", "sid")
            pid = pick("product_id", "productId", "product", "pid")
            did = pick("date_id", "dateId", "date", "did")
        except KeyError as exc:
            raise LedgerInputError(f"事实缺少字段 {exc}: {raw!r}")
    elif isinstance(raw, (list, tuple)):
        if len(raw) != 3:
            raise LedgerInputError(f"事实必须含门店/商品/日期三个 ID: {raw!r}")
        sid, pid, did = raw
    elif isinstance(raw, str):
        parts = raw.split("|")
        if len(parts) != 3:
            raise LedgerInputError(f"字符串事实必须形如 门店|商品|日期: {raw!r}")
        sid, pid, did = parts
    else:
        raise LedgerInputError(f"无效事实结构: {raw!r}")

    out: List[str] = []
    for axis, item in (("store_id", sid), ("product_id", pid), ("date_id", did)):
        if isinstance(item, bool) or item is None:
            raise LedgerInputError(f"事实 {axis} 必须是非空字符串/整数 ID: {raw!r}")
        text = str(item).strip()
        if not text:
            raise LedgerInputError(f"事实 {axis} 不能为空: {raw!r}")
        out.append(text)
    return out[0], out[1], out[2]


def fact_to_dict(fact: Fact) -> Dict[str, str]:
    return {"store_id": fact[0], "product_id": fact[1], "date_id": fact[2]}


def fact_key(fact: Fact) -> str:
    """与现有 engine.fact_trace 相同的可读键。"""
    return "|".join(fact)


def change_kind(before: Any, after: Any) -> str:
    if before == NOPRICE and after != NOPRICE:
        return "from_noprice"
    if before != NOPRICE and after == NOPRICE:
        return "to_noprice"
    return "price_to_price"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def content_fingerprint(content: Any) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def format_value(value: Any) -> str:
    if value == NOPRICE:
        return "无价"
    return f"{value}分"


# --------------------------------------------------------------------------- #
# 底档规范化
# --------------------------------------------------------------------------- #

def normalize_base(effective_from: Any, prices: Any) -> Dict[str, Any]:
    canon, _dt, raw_time = canonical_datetime(effective_from, field="底档生效时间")
    if prices is None:
        raise LedgerInputError("底档缺少 prices/ledger 价格列表")

    items: List[Dict[str, Any]] = []
    errors: List[str] = []

    if isinstance(prices, dict):
        iterable: Iterable[Tuple[Any, Any]] = []
        for key, value in prices.items():
            if isinstance(key, tuple):
                iterable = list(iterable) + [(key, value)]
            elif isinstance(key, str) and "|" in key:
                iterable = list(iterable) + [(key.split("|"), value)]
            else:
                errors.append(f"不支持的底档字典键: {key!r}")
    else:
        iterable = prices

    seen = set()
    try:
        iterator = list(iterable)
    except TypeError:
        raise LedgerInputError("底档 prices 必须是列表或 {事实: 价格} 字典")

    for idx, raw_item in enumerate(iterator):
        prefix = f"底档第 {idx + 1} 项"
        try:
            if isinstance(raw_item, dict):
                raw_fact = raw_item.get("fact", raw_item)
                fact = normalize_fact(raw_fact)
                if "value" in raw_item:
                    value = normalize_value(raw_item["value"], field=f"{prefix}.value")
                elif "price" in raw_item:
                    value = normalize_value(raw_item["price"], field=f"{prefix}.price")
                elif "base_value" in raw_item:
                    value = normalize_value(raw_item["base_value"],
                                            field=f"{prefix}.base_value")
                else:
                    raise LedgerInputError(f"{prefix} 缺少 value/price/base_value")
            elif isinstance(raw_item, (list, tuple)) and len(raw_item) == 2:
                fact = normalize_fact(raw_item[0])
                value = normalize_value(raw_item[1], field=f"{prefix}价格")
            else:
                raise LedgerInputError(f"{prefix} 结构错误: {raw_item!r}")

            if fact in seen:
                raise LedgerInputError(f"{prefix} 事实重复: {fact}")
            seen.add(fact)
            items.append({"fact": list(fact), "value": value})
        except LedgerInputError as exc:
            errors.extend(exc.errors if hasattr(exc, "errors") else [str(exc)])

    if errors:
        raise LedgerInputError(errors)

    items.sort(key=lambda x: tuple(x["fact"]))
    return {
        "effective_from": canon,
        "effective_from_input": raw_time,
        "prices": items,
    }


def base_from_object(raw: Any) -> Optional[Dict[str, Any]]:
    """从 {"effective_from": ..., "prices"/"ledger": ...} 结构解析底档。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LedgerInputError("base 必须是对象")
    effective = (raw.get("effective_from") or raw.get("effectiveFrom")
                 or raw.get("effective_at") or raw.get("effectiveAt")
                 or raw.get("start_at") or raw.get("as_of"))
    prices = None
    for key in ("prices", "initial_prices", "base_prices", "ledger"):
        if key in raw:
            prices = raw[key]
            break
    return normalize_base(effective, prices)


# --------------------------------------------------------------------------- #
# 更正单内容规范化：原生台账格式 + 现有 engine 更正单 JSON
# --------------------------------------------------------------------------- #

_BEFORE_KEYS = ("before_value", "before", "before_price", "old_value",
                "old_price", "base_value", "original_value", "claimed_before",
                "stated_before", "stated_before_value")
_AFTER_KEYS = ("after_value", "after", "after_price", "new_value", "new_price",
               "effective_value", "corrected_value")


def _first_present(raw: Dict[str, Any], keys: Tuple[str, ...]) -> Tuple[bool, Any]:
    for key in keys:
        if key in raw:
            return True, raw[key]
    return False, None


def _normalize_native_line(raw: Any, path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise LedgerInputError(f"{path} 必须是对象: {raw!r}")
    fact_raw = raw.get("fact", raw)
    fact = normalize_fact(fact_raw)

    has_before, before_raw = _first_present(raw, _BEFORE_KEYS)
    has_after, after_raw = _first_present(raw, _AFTER_KEYS)
    if not has_before:
        raise LedgerInputError(f"{path} {fact} 缺少变更前价格 before/base_value")
    if not has_after:
        raise LedgerInputError(f"{path} {fact} 缺少变更后价格 after/new_value")

    before = normalize_value(before_raw, field=f"{path}.before")
    after = normalize_value(after_raw, field=f"{path}.after")
    changed = before != after
    if "changed" in raw and raw["changed"] is not None:
        claimed = raw["changed"]
        if not isinstance(claimed, bool):
            raise LedgerInputError(f"{path} {fact} 的 changed 必须是布尔值")
        if claimed != changed:
            raise LedgerInputError(
                f"{path} {fact} 的 changed={claimed} 与 {before!r}->{after!r} 不一致"
            )
    return {"fact": list(fact), "before": before, "after": after, "changed": changed}


def _normalize_native_groups(raw_groups: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_groups, list) or not raw_groups:
        raise LedgerInputError("内容 groups 必须是非空列表")
    groups: List[Dict[str, Any]] = []
    all_facts = set()
    for gi, raw_group in enumerate(raw_groups, 1):
        path = f"groups[{gi - 1}]"
        if not isinstance(raw_group, dict):
            raise LedgerInputError(f"{path} 必须是对象")
        group_id = (raw_group.get("group_id") or raw_group.get("id")
                    or raw_group.get("component_id") or raw_group.get("name")
                    or raw_group.get("group") or f"GROUP-{gi:03d}")
        if not isinstance(group_id, str) or not group_id.strip():
            raise LedgerInputError(f"{path}.group_id 非法: {group_id!r}")

        raw_facts = raw_group.get("facts", raw_group.get("items",
                                  raw_group.get("changes", raw_group.get("lines"))))
        if not isinstance(raw_facts, list) or not raw_facts:
            raise LedgerInputError(f"{path}({group_id}) 必须含非空 facts/items/changes")

        lines: List[Dict[str, Any]] = []
        in_group = set()
        for li, line in enumerate(raw_facts, 1):
            line_norm = _normalize_native_line(line, f"{path}.facts[{li - 1}]")
            fact = tuple(line_norm["fact"])
            if fact in in_group:
                raise LedgerInputError(f"{path}({group_id}) 内事实重复: {fact}")
            in_group.add(fact)
            if fact in all_facts:
                raise LedgerInputError(
                    f"事实 {fact} 同时出现在多个通过组中；通过关联组必须互不相交"
                )
            all_facts.add(fact)
            lines.append(line_norm)
        groups.append({"group_id": group_id, "facts": lines})
    return groups


def _trace_key(fact: Fact) -> str:
    return fact_key(fact)


def bookable_from_approved_order(content: Dict[str, Any]) -> Dict[str, Any]:
    """把现有 engine 生成的 CorrectionOrder JSON 转成可入账通过组。"""
    if not isinstance(content, dict):
        raise LedgerInputError("更正单内容必须是 JSON 对象")

    # 极简格式：只有 price_changes 时，把每条价差视为独立通过组。
    if "passed_components" not in content and "groups" not in content:
        changes = content.get("price_changes")
        if not isinstance(changes, list) or not changes:
            raise LedgerInputError(
                "无法识别的更正单内容：需要 groups、passed_components 或非空 price_changes"
            )
        native_groups = []
        for idx, change in enumerate(changes, 1):
            if not isinstance(change, dict):
                raise LedgerInputError(f"price_changes[{idx - 1}] 必须是对象")
            line = _normalize_native_line({
                **change,
                "before": change.get("base_value", change.get("before")),
                "after": change.get("new_value", change.get("after")),
            }, f"price_changes[{idx - 1}]")
            if not line["changed"]:
                continue
            native_groups.append({
                "group_id": f"CHANGE-{idx:03d}",
                "facts": [line],
            })
        return {"schema": "price-changes-v1", "groups": native_groups}

    raw_components = content.get("passed_components")
    if not isinstance(raw_components, list):
        raise LedgerInputError("更正单 passed_components 必须是列表")
    trace = content.get("fact_trace", {})
    if trace is None:
        trace = {}
    if not isinstance(trace, dict):
        raise LedgerInputError("更正单 fact_trace 必须是对象")

    change_map: Dict[Fact, Dict[str, Any]] = {}
    for idx, change in enumerate(content.get("price_changes", []) or [], 1):
        try:
            fact = normalize_fact(change.get("fact", change))
            base = normalize_value(change.get("base_value"),
                                   field=f"price_changes[{idx - 1}].base_value")
            new = normalize_value(change.get("new_value"),
                                  field=f"price_changes[{idx - 1}].new_value")
        except LedgerInputError as exc:
            raise LedgerInputError([f"已批准更正单价差结构错误: {e}" for e in exc.errors])
        if fact in change_map:
            raise LedgerInputError(f"已批准更正单价差重复: {fact}")
        change_map[fact] = {"base_value": base, "new_value": new}

    groups: List[Dict[str, Any]] = []
    passed_facts = set()
    all_facts = set()
    for ci, comp in enumerate(raw_components, 1):
        if not isinstance(comp, dict):
            raise LedgerInputError(f"passed_components[{ci - 1}] 必须是对象")
        group_id = comp.get("component_id") or comp.get("id") or f"PASSED-{ci:03d}"
        raw_facts = comp.get("facts")
        if not isinstance(raw_facts, list) or not raw_facts:
            raise LedgerInputError(f"通过组 {group_id} 缺少非空 facts")
        lines = []
        group_facts = set()
        for fi, raw_fact in enumerate(raw_facts):
            fact = normalize_fact(raw_fact)
            if fact in group_facts:
                raise LedgerInputError(f"通过组 {group_id} 内事实重复: {fact}")
            if fact in all_facts:
                raise LedgerInputError(f"事实 {fact} 出现在多个通过组件中")
            group_facts.add(fact)
            all_facts.add(fact)
            passed_facts.add(fact)

            tr = trace.get(_trace_key(fact))
            if tr is None:
                ch = change_map.get(fact)
                if ch is None:
                    raise LedgerInputError(
                        f"通过组 {group_id} 的事实 {fact} 既无 fact_trace 也无价差记录，"
                        "无法确定变更前价格"
                    )
                before, after = ch["base_value"], ch["new_value"]
                changed = True
            else:
                if not isinstance(tr, dict):
                    raise LedgerInputError(f"fact_trace[{fact_key(fact)}] 必须是对象")
                if tr.get("passed") is False:
                    raise LedgerInputError(
                        f"事实 {fact} 位于 passed_components，但 fact_trace 标记为未通过"
                    )
                before = normalize_value(tr.get("base_value"),
                                         field=f"fact_trace.{fact}.base_value")
                if "effective_value" not in tr:
                    raise LedgerInputError(
                        f"fact_trace.{fact} 缺少 effective_value"
                    )
                after = normalize_value(tr.get("effective_value"),
                                        field=f"fact_trace.{fact}.effective_value")
                changed = before != after
                if bool(tr.get("changed", changed)) != changed:
                    raise LedgerInputError(
                        f"fact_trace.{fact} 的 changed 与价格不一致"
                    )

            ch = change_map.get(fact)
            if ch is not None:
                if not changed:
                    raise LedgerInputError(
                        f"零变动事实 {fact} 不应出现在 price_changes 中"
                    )
                if ch["base_value"] != before or ch["new_value"] != after:
                    raise LedgerInputError(
                        f"事实 {fact} 的 fact_trace 与 price_changes 价格不一致"
                    )
            elif changed:
                raise LedgerInputError(
                    f"变动事实 {fact} 缺少对应的 price_changes 记录"
                )

            lines.append({
                "fact": list(fact), "before": before, "after": after,
                "changed": changed,
            })
        groups.append({"group_id": group_id, "facts": lines})

    for fact in change_map:
        if fact not in passed_facts:
            raise LedgerInputError(
                f"价差事实 {fact} 不属于任何 passed_components；拒发组不得入账"
            )

    return {"schema": "correction-order-json-v1", "groups": groups}


def _groups_from_flat_lines(raw_lines: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_lines, list) or not raw_lines:
        raise LedgerInputError("扁平 changes/items/facts 必须是非空列表")
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for idx, raw_line in enumerate(raw_lines, 1):
        if not isinstance(raw_line, dict):
            raise LedgerInputError(f"changes[{idx - 1}] 必须是对象")
        gid = (raw_line.get("group_id") or raw_line.get("groupId")
               or raw_line.get("component_id") or raw_line.get("group")
               or f"CHANGE-{idx:03d}")
        grouped.setdefault(str(gid), []).append(raw_line)
    return _normalize_native_groups([
        {"group_id": gid, "facts": lines} for gid, lines in grouped.items()
    ])


def bookable_from_content(content: Any) -> Dict[str, Any]:
    """识别内容形状并返回规范化通过组。"""
    if isinstance(content, list):
        # 既允许 [组对象...]，也允许 [带 group_id 的扁平变更...]。
        if content and all(isinstance(x, dict) and (
            "facts" in x or "items" in x or "changes" in x
        ) for x in content):
            groups = _normalize_native_groups(content)
        else:
            groups = _groups_from_flat_lines(content)
        return {"schema": "list-v1", "groups": groups}
    if not isinstance(content, dict):
        raise LedgerInputError("更正单内容必须是 JSON 对象或列表")
    if "groups" in content:
        groups = _normalize_native_groups(content["groups"])
        schema = content.get("schema", "native-groups-v1")
        return {"schema": schema, "groups": groups}
    if "passed_components" in content or "price_changes" in content:
        return bookable_from_approved_order(content)
    for key in ("changes", "items", "facts", "lines"):
        if key in content:
            return {"schema": "flat-changes-v1",
                    "groups": _groups_from_flat_lines(content[key])}
    raise LedgerInputError(
        "更正单内容无法识别：需要 groups（台账原生格式）、changes/items/facts，或 "
        "passed_components/price_changes（现有签审引擎输出）"
    )


# --------------------------------------------------------------------------- #
# 投递信封解析
# --------------------------------------------------------------------------- #

_METADATA_KEYS = {
    "order_id", "id", "order_no", "correction_order_id",
    "effective_at", "effective_time", "effective_from",
    "sequence", "seq", "order_in_instant",
    "content", "correction_order", "approved_order", "order_payload", "payload",
}


def _first_key(raw: Dict[str, Any], keys: Tuple[str, ...]) -> Tuple[bool, Any]:
    for key in keys:
        if key in raw:
            return True, raw[key]
    return False, None


def parse_order_envelope(raw: Any, index: Optional[int] = None) -> Dict[str, Any]:
    prefix = f"orders[{index}]" if index is not None else "更正单"
    if not isinstance(raw, dict):
        raise LedgerInputError(f"{prefix} 结构错误：必须是 JSON 对象")

    has_id, order_id = _first_key(raw, (
        "order_id", "orderId", "order_no", "orderNo", "correction_order_id",
        "correctionOrderId", "id",
    ))
    if not has_id or not isinstance(order_id, str) or not order_id.strip():
        raise LedgerInputError(f"{prefix} 缺少唯一单号 order_id/order_no")
    order_id = order_id.strip()

    has_time, time_value = _first_key(raw, (
        "effective_at", "effectiveAt", "effective_time", "effectiveTime",
        "effective_from", "effectiveFrom", "valid_at", "valid_from",
    ))
    if not has_time:
        raise LedgerInputError(f"{prefix}({order_id}) 缺少带时区的 effective_at")
    effective, _dt, time_raw = canonical_datetime(
        time_value, field=f"{prefix}({order_id}).effective_at"
    )

    has_seq, seq = _first_key(raw, (
        "sequence", "sequence_no", "sequenceNumber", "seq", "seq_no",
        "order_in_instant", "position",
    ))
    if not has_seq:
        raise LedgerInputError(f"{prefix}({order_id}) 缺少同一时点内顺序号 sequence")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise LedgerInputError(
            f"{prefix}({order_id}) sequence 必须是非负整数，实际: {seq!r}"
        )

    content: Any = None
    for key in ("content", "correction_order", "approved_order",
                "order_payload", "payload"):
        if key in raw:
            content = raw[key]
            break
    if content is None:
        if any(k in raw for k in (
            "groups", "passed_components", "price_changes",
            "changes", "items", "facts", "lines",
        )):
            # 允许元数据与内容字段平铺；复制后去掉明显的元数据别名。
            content = {k: v for k, v in raw.items()
                       if k not in _METADATA_KEYS | {
                           "orderId", "effectiveAt", "sequenceNumber",
                           "delivery", "delivery_id", "batch_id",
                       }}
        else:
            raise LedgerInputError(
                f"{prefix}({order_id}) 缺少 content/correction_order 或可识别的更正单内容"
            )

    # 允许 Python 调用方直接传入 engine.CorrectionOrder dataclass。
    if not isinstance(content, (dict, list, str, int, float, bool)) and content is not None \
            and hasattr(content, "fact_trace") and hasattr(content, "changes"):
        try:
            from engine import order_to_dict
        except ImportError:  # pragma: no cover - 包方式导入分支
            from .engine import order_to_dict
        content = order_to_dict(content)
    # 提前做内容结构校验；错误加上单号前缀。
    try:
        bookable = bookable_from_content(content)
    except LedgerInputError as exc:
        raise LedgerInputError([f"{prefix}({order_id}): {e}" for e in exc.errors])

    return {
        "order_id": order_id,
        "effective_at": effective,
        "effective_at_input": time_raw,
        "sequence": seq,
        "content": content,
        "content_sha256": content_fingerprint(content),
        "bookable": bookable,
    }


def parse_delivery(raw: Any) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """解析一次投递。返回 (候选底档, 信封列表, 硬错误)。"""
    errors: List[str] = []
    base_candidate: Optional[Dict[str, Any]] = None

    if isinstance(raw, dict) and isinstance(raw.get("base"), dict):
        try:
            base_candidate = base_from_object(raw["base"])
        except LedgerInputError as exc:
            errors.extend(f"base: {e}" for e in exc.errors)

    raw_orders: Any = None
    if isinstance(raw, list):
        raw_orders = raw
    elif isinstance(raw, dict):
        for key in ("orders", "correction_orders", "correctionOrders",
                    "items", "deliveries", "batch"):
            if key in raw:
                raw_orders = raw[key]
                break
        if raw_orders is None and (
            "order_id" in raw or "order_no" in raw or "effective_at" in raw
        ):
            raw_orders = [raw]
        elif raw_orders is None and "base" in raw:
            raw_orders = []
        elif raw_orders is None:
            errors.append(
                "投递结构错误：需要 orders/correction_orders/items 列表，或单笔更正单对象"
            )
            raw_orders = []
    else:
        return None, [], ["投递结构错误：顶层 JSON 必须是对象或列表"]

    if not isinstance(raw_orders, list):
        errors.append("投递结构错误：orders 必须是列表")
        raw_orders = []

    records: List[Dict[str, Any]] = []
    for idx, item in enumerate(raw_orders):
        try:
            records.append(parse_order_envelope(item, idx))
        except LedgerInputError as exc:
            errors.extend(exc.errors)

    # 顶层也可能直接给 base 字段（effective_from + prices）。
    if base_candidate is None and isinstance(raw, dict) and (
        "effective_from" in raw and ("prices" in raw or "ledger" in raw)
    ):
        try:
            base_candidate = base_from_object(raw)
        except LedgerInputError as exc:
            errors.extend(f"base: {e}" for e in exc.errors)

    return base_candidate, records, errors


# --------------------------------------------------------------------------- #
# 重算引擎：始终按生效先后，而不是到达先后
# --------------------------------------------------------------------------- #

def _base_value_map(base: Dict[str, Any]) -> Dict[Fact, Any]:
    return {tuple(item["fact"]): item["value"] for item in base["prices"]}


def recompute_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """从底档和全部已接受单据重建事件、组裁定与冲突。"""
    base = state.get("base")
    orders = state.get("orders", {})

    events: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    evaluations: Dict[str, Any] = {}

    if not base:
        state["price_events"] = events
        state["conflicts"] = conflicts
        state["evaluation"] = {"orders": evaluations}
        return state

    values = _base_value_map(base)
    base_source = {
        "type": "base",
        "kind": "base",
        "effective_from": base["effective_from"],
        "description": "初始底档起始状态",
    }
    sources: Dict[Fact, Dict[str, Any]] = {fact: dict(base_source) for fact in values}

    ordered = sorted(
        orders.values(),
        key=lambda r: (parse_aware_datetime(r["effective_at"]),
                       int(r["sequence"]), r["order_id"]),
    )

    for record in ordered:
        oid = record["order_id"]
        effective = record["effective_at"]
        seq = record["sequence"]
        oeval: Dict[str, Any] = {
            "order_id": oid,
            "effective_at": effective,
            "sequence": seq,
            "arrival_independent_note": "生效判定由全量单据按时间+顺序重算",
            "groups": [],
            "status": "applied",
        }

        for group in record.get("bookable", {}).get("groups", []):
            gid = group["group_id"]
            geval: Dict[str, Any] = {
                "order_id": oid,
                "group_id": gid,
                "status": "applied",
                "conflicts": [],
                "applied_changes": [],
                "zero_change_facts": [],
                "blocked_changes": [],
                "facts": [line["fact"] for line in group["facts"]],
            }

            group_conflicts: List[Dict[str, Any]] = []
            for line in group["facts"]:
                fact = tuple(line["fact"])
                expected = values.get(fact, MISSING)
                if expected is MISSING:
                    # 正常入库校验会拒绝未知事实；走到这里说明持久化状态被外部破坏。
                    conflict = {
                        "fact": fact_to_dict(fact),
                        "fact_key": fact_key(fact),
                        "original_value": line["before"],
                        "before": line["before"],
                        "stated_before_value": line["before"],
                        "expected_value": None,
                        "expected": None,
                        "actual_value": None,
                        "current_value": None,
                        "ledger_value": None,
                        "missing_expected_history": True,
                        "expected_source": None,
                        "message": "台账中缺少该事实的底档/历史，无法核对变更前价格",
                    }
                    group_conflicts.append(conflict)
                    continue
                if line["before"] != expected:
                    group_conflicts.append({
                        "fact": fact_to_dict(fact),
                        "fact_key": fact_key(fact),
                        "original_value": line["before"],
                        "before": line["before"],
                        "stated_before_value": line["before"],
                        "expected_value": expected,
                        "expected": expected,
                        "actual_value": expected,
                        "current_value": expected,
                        "ledger_value": expected,
                        "expected_source": copy.deepcopy(sources.get(fact)),
                        "message": (
                            f"变更前价格 {format_value(line['before'])} 与该时点应有价格 "
                            f"{format_value(expected)} 不符"
                        ),
                    })

            if group_conflicts:
                geval["status"] = "blocked"
                geval["conflicts"] = group_conflicts
                for line in group["facts"]:
                    fact = tuple(line["fact"])
                    if line["changed"]:
                        blocked = {
                            "fact": fact_to_dict(fact),
                            "from_value": line["before"],
                            "to_value": line["after"],
                            "change_kind": change_kind(line["before"], line["after"]),
                            "order_id": oid,
                            "effective_at": effective,
                            "sequence": seq,
                            "group_id": gid,
                        }
                        geval["blocked_changes"].append(blocked)
                conflicts.append({
                    "order_id": oid,
                    "effective_at": effective,
                    "sequence": seq,
                    "group_id": gid,
                    "facts": [line["fact"] for line in group["facts"]],
                    "conflict_facts": [c["fact"] for c in group_conflicts],
                    "conflicts": group_conflicts,
                    "blocked_changes": copy.deepcopy(geval["blocked_changes"]),
                    "status": "blocked",
                })
            else:
                for line in group["facts"]:
                    fact = tuple(line["fact"])
                    before = values[fact]
                    after = line["after"]
                    if after == before:
                        geval["zero_change_facts"].append({
                            "fact": fact_to_dict(fact),
                            "value": after,
                        })
                        continue
                    event_id = f"{oid}:{gid}:{fact_key(fact)}"
                    source = {
                        "type": "correction_order",
                        "kind": "correction_order",
                        "order_id": oid,
                        "effective_at": effective,
                        "sequence": seq,
                        "group_id": gid,
                        "event_id": event_id,
                        "description": f"已批准更正单 {oid} 通过组 {gid}",
                    }
                    event = {
                        "event_id": event_id,
                        "fact": fact_to_dict(fact),
                        "store_id": fact[0],
                        "product_id": fact[1],
                        "date_id": fact[2],
                        "effective_at": effective,
                        "sequence": seq,
                        "order_id": oid,
                        "group_id": gid,
                        "before_value": before,
                        "after_value": after,
                        "change_kind": change_kind(before, after),
                        "source": source,
                    }
                    events.append(event)
                    geval["applied_changes"].append(copy.deepcopy(event))
                    values[fact] = after
                    sources[fact] = source

            oeval["groups"].append(geval)

        statuses = [g["status"] for g in oeval["groups"]]
        if statuses and all(s == "blocked" for s in statuses):
            oeval["status"] = "blocked"
        elif any(s == "blocked" for s in statuses):
            oeval["status"] = "partially_applied"
        else:
            oeval["status"] = "applied"
        evaluations[oid] = oeval

    events.sort(key=lambda e: (
        parse_aware_datetime(e["effective_at"]), e["sequence"],
        e["order_id"], e["group_id"], fact_key(normalize_fact(e["fact"])),
    ))
    state["price_events"] = events
    state["conflicts"] = conflicts
    state["evaluation"] = {"orders": evaluations}
    return state


# --------------------------------------------------------------------------- #
# 台账持久化与对外 API
# --------------------------------------------------------------------------- #

class PriceHistoryLedger:
    """价格历史台账。一个实例对应一个原子落盘的 JSON 台账文件。"""
    def __init__(self, path: Union[str, os.PathLike[str]]):
        raw_path = Path(path)
        if raw_path.exists() and raw_path.is_dir():
            raw_path = raw_path / "price_history_ledger.json"
        self.path = raw_path
        self.state: Dict[str, Any] = self._load()
        # 每次打开都从已接受单据重算派生结果，避免信任旧快照。
        recompute_state(self.state)

    # ---------------- 持久化 ---------------- #

    def _default_state(self) -> Dict[str, Any]:
        return {"version": 1, "base": None, "orders": {}, "delivery_log": []}

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return self._default_state()
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise LedgerPersistenceError(f"无法读取台账 {self.path}: {exc}")
        if not isinstance(state, dict) or not isinstance(state.get("orders", {}), dict):
            raise LedgerPersistenceError(f"台账文件结构已损坏: {self.path}")
        state.setdefault("version", 1)
        state.setdefault("base", None)
        state.setdefault("orders", {})
        state.setdefault("delivery_log", [])
        return state

    def _commit(self, new_state: Dict[str, Any]) -> None:
        """原子替换台账文件；调用方必须先在副本上完成全部校验。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fh.fileno(), 0o644)
                json.dump(new_state, fh, ensure_ascii=False, indent=2, sort_keys=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
            try:
                dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                # 某些文件系统不支持目录 fsync；os.replace 已完成原子替换。
                pass
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def reload(self) -> "PriceHistoryLedger":
        self.state = self._load()
        recompute_state(self.state)
        return self

    @contextmanager
    def _write_lock(self):
        """串行化跨进程 read-modify-commit，避免两个投递互相覆盖。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_fh:
            if fcntl is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    # ---------------- 底档 ---------------- #

    def initialize_base(self, effective_from: Any, prices: Any) -> Dict[str, Any]:
        candidate = normalize_base(effective_from, prices)
        with self._write_lock():
            current_state = self._load()
            new_state = copy.deepcopy(current_state)
            action = "initialized"
            if new_state["base"] is not None:
                if canonical_json(new_state["base"]) == canonical_json(candidate):
                    self.state = new_state
                    return {"accepted": True, "base_action": "duplicate",
                            "path": str(self.path)}
                raise LedgerInputError(
                    "底档已以不同起始时点/价格初始化；不能静默覆盖既有历史起点"
                )
            new_state["base"] = candidate
            recompute_state(new_state)
            self._commit(new_state)
            self.state = new_state
        return {"accepted": True, "base_action": action, "path": str(self.path)}

    # ---------------- 投递 ---------------- #

    def deliver(self, raw_delivery: Any) -> Dict[str, Any]:
        parsed = parse_delivery(raw_delivery)
        with self._write_lock():
            return self._deliver_with_lock(parsed)

    def _deliver_with_lock(
        self,
        parsed: Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[str]],
    ) -> Dict[str, Any]:
        base_candidate, records, parse_errors = parsed
        errors = list(parse_errors)

        # 锁内重新读盘，避免持有旧实例时覆盖其他进程/实例刚提交的单据。
        new_state = copy.deepcopy(self._load())

        # 底档候选先做幂等/冲突检查。
        base_action = None
        if base_candidate is not None:
            if new_state["base"] is None:
                new_state["base"] = base_candidate
                base_action = "initialized"
            elif canonical_json(new_state["base"]) == canonical_json(base_candidate):
                base_action = "duplicate"
            else:
                errors.append("base 冲突：台账已存在不同的底档起始状态，不能覆盖")
        if new_state["base"] is None:
            errors.append("台账尚未初始化底档；请先提供 base 或调用 init-base")

        # 校验所有单据引用的事实都在底档中（无效事实硬拒绝）。
        base_facts = _base_value_map(new_state["base"]) if new_state["base"] else {}
        base_dt = (parse_aware_datetime(new_state["base"]["effective_from"])
                   if new_state["base"] else None)
        for rec in records:
            oid = rec["order_id"]
            if base_dt is not None and parse_aware_datetime(rec["effective_at"]) < base_dt:
                errors.append(
                    f"更正单 {oid} 的生效时间 {rec['effective_at']} 早于底档起点 "
                    f"{new_state['base']['effective_from']}；起点之前不能建立更正历史"
                )
            for group in rec["bookable"]["groups"]:
                for line in group["facts"]:
                    fact = tuple(line["fact"])
                    if new_state["base"] is not None and fact not in base_facts:
                        errors.append(
                            f"更正单 {oid} 含无效/底档外事实 {fact_to_dict(fact)}；"
                            "不能建立无起点历史"
                        )

        # 同批内单号/位置检查，精确重复投递去重。
        identity_seen: Dict[str, Tuple[str, int, str]] = {}
        batch_positions: Dict[Tuple[str, int], str] = {}
        unique_records: List[Dict[str, Any]] = []
        intra_duplicates: List[str] = []

        for rec in records:
            oid = rec["order_id"]
            identity = (rec["effective_at"], rec["sequence"], rec["content_sha256"])
            pos = (rec["effective_at"], rec["sequence"])

            if oid in identity_seen:
                if identity_seen[oid] == identity:
                    intra_duplicates.append(oid)
                    continue
                errors.append(
                    f"单号冲突：同一投递中 {oid} 出现多次且时间/顺序/内容不同；"
                    "不能静默覆盖"
                )
                continue

            identity_seen[oid] = identity
            existing = new_state["orders"].get(oid)
            if existing is not None:
                existing_identity = (
                    existing["effective_at"], existing["sequence"],
                    existing["content_sha256"],
                )
                if existing_identity == identity:
                    # 与已落盘单据完全相同：幂等忽略。
                    continue
                errors.append(
                    f"单号冲突：{oid} 已存在，但生效时间/顺序/内容与本次投递不同；"
                    "拒绝覆盖原记录"
                )
                continue

            existing_pos_oid = self._position_owner(new_state, pos)
            if existing_pos_oid is not None:
                errors.append(
                    f"重复顺序位置：{pos[0]} sequence={pos[1]} 已属于更正单 "
                    f"{existing_pos_oid}，不能再由 {oid} 占用"
                )
                continue
            if pos in batch_positions:
                errors.append(
                    f"重复顺序位置：{pos[0]} sequence={pos[1]} 在同一投递中被 "
                    f"{batch_positions[pos]} 和 {oid} 同时占用"
                )
                continue

            batch_positions[pos] = oid
            unique_records.append(rec)

        if errors:
            # 关键：不调用 _commit，self.state 也不替换，因此整批零部分写入。
            raise LedgerInputError(errors)

        duplicate_ids = self._duplicate_ids(records) + intra_duplicates
        for rec in unique_records:
            # 不把内部规范化辅助结构与原始内容重复嵌套过多；record 已可序列化。
            new_state["orders"][rec["order_id"]] = rec

        recompute_state(new_state)
        eval_orders = new_state["evaluation"]["orders"]
        delivery_log = list(new_state.get("delivery_log", []))
        delivery_index = len(delivery_log) + 1
        delivery_log.append({
            "delivery_index": delivery_index,
            "order_ids_received": [r["order_id"] for r in records],
            "new_order_ids": [r["order_id"] for r in unique_records],
            "duplicate_order_ids": sorted(set(duplicate_ids)),
            "base_action": base_action,
            "status_after_recompute": {
                oid: eval_orders[oid]["status"] for oid in sorted(eval_orders)
            },
            "conflict_groups_after_recompute": [
                {"order_id": c["order_id"], "group_id": c["group_id"],
                 "conflict_facts": c["conflict_facts"]}
                for c in new_state["conflicts"]
            ],
        })
        new_state["delivery_log"] = delivery_log
        self._commit(new_state)
        self.state = new_state

        return {
            "accepted": True,
            "delivery_index": delivery_index,
            "base_action": base_action,
            "received_record_count": len(records),
            "new_order_ids": [r["order_id"] for r in unique_records],
            "duplicate_order_ids": sorted(set(duplicate_ids)),
            "intra_batch_duplicate_ids": sorted(set(intra_duplicates)),
            "evaluation_summary": {
                oid: eval_orders[oid]["status"]
                for oid in eval_orders
                if oid in {r["order_id"] for r in unique_records}
            },
            "current_order_status": {
                oid: eval_orders[oid]["status"] for oid in sorted(eval_orders)
            },
            "conflicts": copy.deepcopy(new_state["conflicts"]),
            "path": str(self.path),
        }

    @staticmethod
    def _position_owner(state: Dict[str, Any], pos: Tuple[str, int]) -> Optional[str]:
        effective, seq = pos
        for oid, rec in state.get("orders", {}).items():
            if rec["effective_at"] == effective and rec["sequence"] == seq:
                return oid
        return None

    def _duplicate_ids(self, records: List[Dict[str, Any]]) -> List[str]:
        out = []
        for rec in records:
            existing = self.state["orders"].get(rec["order_id"])
            if existing is None:
                continue
            same = (
                existing["effective_at"] == rec["effective_at"]
                and existing["sequence"] == rec["sequence"]
                and existing["content_sha256"] == rec["content_sha256"]
            )
            if same:
                out.append(rec["order_id"])
        return out

    def deliver_file(self, path: Union[str, os.PathLike[str]]) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except json.JSONDecodeError as exc:
            raise LedgerInputError(f"投递 JSON 结构错误: {exc}")
        except OSError as exc:
            raise LedgerInputError(f"无法读取投递文件: {exc}")
        return self.deliver(raw)

    # ---------------- 查询 ---------------- #

    def query_price(self, store_id: str, product_id: str, date_id: str,
                    at: Any = None) -> Dict[str, Any]:
        # 查询前刷新，使长生命周期实例也能看到其他 CLI 进程提交的台账。
        if self.path.exists():
            self.reload()
        fact = normalize_fact({"store_id": store_id, "product_id": product_id,
                              "date_id": date_id})
        if at is None:
            at_dt = datetime.now(timezone.utc)
            at_input = at_dt.isoformat().replace("+00:00", "Z")
        else:
            at_dt = parse_aware_datetime(at, allow_naive=True, field="查询时点")
            at_input = at if isinstance(at, str) else at_dt.isoformat()
        at_canon = at_dt.isoformat().replace("+00:00", "Z")

        base = self.state.get("base")
        query_meta = {
            "fact": fact_to_dict(fact),
            "at_input": at_input,
            "at_utc": at_canon,
            "naive_time_assumed_timezone": "UTC" if (
                isinstance(at, str) and _string_is_naive(at)
            ) else None,
        }

        if not base:
            return {
                "query": query_meta,
                "status": STATUS_NO_BASE,
                "found": False,
                "has_history": False,
                "value": None,
                "price_status": "base_not_initialized",
                "source": None,
                "sources": [],
                "applied_corrections": [],
                "blocked_attempts": [],
            }

        base_dt = parse_aware_datetime(base["effective_from"])
        if at_dt < base_dt:
            return {
                "query": query_meta,
                "status": STATUS_BEFORE_BASE,
                "found": False,
                "has_history": False,
                "value": None,
                "price_status": "no_history",
                "base_effective_from": base["effective_from"],
                "base_effective_from_input": base.get("effective_from_input"),
                "message": (
                    f"查询时点 {at_canon} 早于初始底档生效时点 "
                    f"{base['effective_from']}，此前尚无价格历史"
                ),
                "source": {"type": "base", "effective_from": base["effective_from"]},
                "sources": [],
                "applied_corrections": [],
                "blocked_attempts": [],
            }

        base_values = _base_value_map(base)
        fact_in_base = fact in base_values
        applicable = [
            e for e in self.state.get("price_events", [])
            if tuple(normalize_fact(e["fact"])) == fact
            and parse_aware_datetime(e["effective_at"]) <= at_dt
        ]

        if not fact_in_base and not applicable:
            return {
                "query": query_meta,
                "status": STATUS_MISSING,
                "found": False,
                "has_history": True,
                "value": None,
                "price_status": "missing_record",
                "base_effective_from": base["effective_from"],
                "message": "底档和已生效更正中均无此事实；缺少历史记录，而非明确无价",
                "source": None,
                "sources": [],
                "applied_corrections": [],
                "blocked_attempts": self._blocked_attempts_for_fact(fact, at_dt),
            }

        if applicable:
            last = applicable[-1]
            value = last["after_value"]
            source = copy.deepcopy(last["source"])
        else:
            value = base_values[fact]
            source = {
                "type": "base",
                "kind": "base",
                "effective_from": base["effective_from"],
                "effective_from_input": base.get("effective_from_input"),
                "description": "初始底档起始状态",
            }

        chain: List[Dict[str, Any]] = [{
            "kind": "base",
            "type": "base",
            "effective_from": base["effective_from"],
            "value": base_values.get(fact),
        }] if fact_in_base else []
        for event in applicable:
            chain.append({
                "kind": "correction_order",
                "type": "correction_order",
                "order_id": event["order_id"],
                "group_id": event["group_id"],
                "effective_at": event["effective_at"],
                "sequence": event["sequence"],
                "before_value": event["before_value"],
                "after_value": event["after_value"],
                "change_kind": event["change_kind"],
                "source": copy.deepcopy(event["source"]),
            })

        is_no_price = value == NOPRICE
        return {
            "query": query_meta,
            "status": STATUS_NO_PRICE if is_no_price else STATUS_PRICED,
            "found": True,
            "has_history": True,
            "price_status": "no_price" if is_no_price else "priced",
            "value": value,
            "effective_value": value,
            "value_display": format_value(value),
            "effective_price_fen": None if is_no_price else value,
            "price_fen": None if is_no_price else value,
            "is_no_price": is_no_price,
            "is_missing_history": False,
            "source": source,
            "sources": chain,
            "provenance_chain": chain,
            "applied_corrections": chain[1:],
            "blocked_attempts": self._blocked_attempts_for_fact(fact, at_dt),
        }

    def query(self, store: Any = None, product: Any = None,
              date_id: Any = None, at: Any = None, **kwargs: Any) -> Dict[str, Any]:
        """更灵活的查询别名：支持 query(fact_tuple, at) 或关键字 fact=/as_of=。"""
        at = at if at is not None else kwargs.get("as_of", kwargs.get("point_in_time"))
        fact_raw = kwargs.get("fact")
        if fact_raw is not None:
            fact = normalize_fact(fact_raw)
            return self.query_price(*fact, at=at)
        if isinstance(store, (tuple, list, dict, str)) and (
            isinstance(store, (tuple, list, dict)) or "|" in str(store)
        ):
            fact = normalize_fact(store)
            # 兼容 query(("S", "P", "D"), "2026-...") 的位置传参。
            if product is not None and at is None:
                at = product
            return self.query_price(*fact, at=at)
        return self.query_price(store, product, date_id, at=at)  # type: ignore[arg-type]

    def price_at(self, at: Any, fact: Fact) -> Dict[str, Any]:
        return self.query_price(*fact, at=at)

    def _blocked_attempts_for_fact(self, fact: Fact, at_dt: datetime) -> List[Dict[str, Any]]:
        out = []
        for conflict in self.state.get("conflicts", []):
            effective_dt = parse_aware_datetime(conflict["effective_at"])
            if effective_dt > at_dt:
                continue
            facts = {tuple(normalize_fact(f)) for f in conflict.get("facts", [])}
            if fact not in facts:
                continue
            item = copy.deepcopy(conflict)
            item["matched_conflict_fact"] = any(
                tuple(normalize_fact(f)) == fact
                for f in conflict.get("conflict_facts", [])
            )
            item["blocked_changes_for_fact"] = [
                ch for ch in item.get("blocked_changes", [])
                if tuple(normalize_fact(ch["fact"])) == fact
            ]
            out.append(item)
        return out

    # ---------------- 历史/冲突/导出 ---------------- #

    def history_for_fact(self, store_id: str, product_id: str,
                         date_id: str) -> Dict[str, Any]:
        fact = normalize_fact({"store_id": store_id, "product_id": product_id,
                              "date_id": date_id})
        base = self.state.get("base")
        events = [
            copy.deepcopy(e) for e in self.state.get("price_events", [])
            if tuple(normalize_fact(e["fact"])) == fact
        ]
        return {
            "fact": fact_to_dict(fact),
            "base": (None if not base else {
                "effective_from": base["effective_from"],
                "price": _base_value_map(base).get(fact, MISSING)
                if fact in _base_value_map(base) else None,
                "in_base": fact in _base_value_map(base),
            }),
            "events": events,
            "blocked_attempts": [
                copy.deepcopy(c) for c in self.state.get("conflicts", [])
                if fact in {tuple(normalize_fact(f)) for f in c.get("facts", [])}
            ],
        }

    def list_conflicts(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self.state.get("conflicts", []))

    def list_orders(self) -> List[Dict[str, Any]]:
        out = []
        evals = self.state.get("evaluation", {}).get("orders", {})
        for oid, rec in sorted(self.state.get("orders", {}).items()):
            out.append({
                "order_id": oid,
                "effective_at": rec["effective_at"],
                "effective_at_input": rec.get("effective_at_input"),
                "sequence": rec["sequence"],
                "content_sha256": rec["content_sha256"],
                "evaluation_status": evals.get(oid, {}).get("status"),
            })
        return out

    def stats(self) -> Dict[str, Any]:
        orders = self.state.get("orders", {})
        evals = self.state.get("evaluation", {}).get("orders", {})
        return {
            "base_initialized": self.state.get("base") is not None,
            "order_count": len(orders),
            "applied_order_count": sum(
                1 for oid in orders if evals.get(oid, {}).get("status") == "applied"
            ),
            "partially_applied_order_count": sum(
                1 for oid in orders
                if evals.get(oid, {}).get("status") == "partially_applied"
            ),
            "blocked_order_count": sum(
                1 for oid in orders if evals.get(oid, {}).get("status") == "blocked"
            ),
            "price_event_count": len(self.state.get("price_events", [])),
            "conflicted_group_count": len(self.state.get("conflicts", [])),
            "accepted_delivery_count": len(self.state.get("delivery_log", [])),
            "path": str(self.path),
        }

    def export_state(self) -> Dict[str, Any]:
        return copy.deepcopy(self.state)


def _string_is_naive(value: str) -> bool:
    try:
        text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        if text.endswith("T"):
            text = text[:-1]
        parsed = datetime.fromisoformat(text)
        return parsed.tzinfo is None
    except Exception:
        return False


def open_ledger(path: Union[str, os.PathLike[str]]) -> "PriceHistoryLedger":
    """打开（必要时延迟创建）一个价格历史台账。"""
    return PriceHistoryLedger(path)


# 常见命名别名，方便不同调用习惯。
HistoryLedger = PriceHistoryLedger
PriceHistory = PriceHistoryLedger
PriceHistoryDB = PriceHistoryLedger


def render_query_text(result: Dict[str, Any]) -> str:
    """中文渲染单次时点查询、价格状态与来源链。"""
    q = result["query"]
    f = q["fact"]
    lines = [
        "=" * 78,
        f"时点价格查询：{f['store_id']} / {f['product_id']} / {f['date_id']}",
        f"查询时点：输入={q['at_input']}  UTC={q['at_utc']}",
        "-" * 78,
    ]
    status = result["status"]
    if status == STATUS_BEFORE_BASE:
        lines += [
            "结果：尚无历史（查询时点早于初始底档生效时点）",
            f"底档起点：{result.get('base_effective_from_input') or result.get('base_effective_from')}",
            result.get("message", ""),
        ]
    elif status == STATUS_MISSING:
        lines += [
            "结果：缺少历史记录（不是明确无价 NOPRICE）",
            result.get("message", ""),
        ]
    elif status == STATUS_NO_BASE:
        lines.append("结果：台账尚未初始化底档")
    else:
        if result["is_no_price"]:
            lines.append("有效价格：无价（NOPRICE；这是明确状态，不是缺少记录）")
        else:
            lines.append(f"有效价格：{result['value']} 分")
        src = result["source"]
        if src["type"] == "base":
            lines.append(
                "直接来源：初始底档 "
                f"（生效 {src.get('effective_from_input') or src['effective_from']}）"
            )
        else:
            lines.append(
                f"直接来源：更正单 {src['order_id']} / 通过组 {src['group_id']} "
                f"@ {src['effective_at']} seq={src['sequence']}"
            )
        lines.append("形成链（底档 + 截至查询时点已生效的更正）：")
        for item in result["sources"]:
            if item["kind"] == "base":
                lines.append(f"  0. 底档起始：{format_value(item['value'])}")
            else:
                lines.append(
                    f"  - {item['order_id']} [{item['group_id']}] "
                    f"{format_value(item['before_value'])} -> "
                    f"{format_value(item['after_value'])} "
                    f"@ {item['effective_at']} seq={item['sequence']}"
                )

    blocked = result.get("blocked_attempts", [])
    if blocked:
        lines.append("被冲突组挡下、未影响上述结果的变更：")
        for cg in blocked:
            lines.append(
                f"  ! {cg['order_id']} [{cg['group_id']}] "
                f"@ {cg['effective_at']} seq={cg['sequence']}"
            )
            for c in cg.get("conflicts", []):
                cf = c.get("fact", {})
                is_this_fact = (
                    isinstance(cf, dict)
                    and cf.get("store_id") == f["store_id"]
                    and cf.get("product_id") == f["product_id"]
                    and cf.get("date_id") == f["date_id"]
                )
                mark = "（本事实原值过期）" if is_this_fact else "（同组其他事实冲突，本事实变更一并挡下）"
                lines.append(
                    f"      冲突事实 {c['fact']}：单内原值="
                    f"{format_value(c['original_value'])}，台账期望值="
                    f"{format_value(c['expected_value'])}{mark}"
                )
            for ch in cg.get("blocked_changes_for_fact", []):
                lines.append(
                    f"      挡下变更：{format_value(ch['from_value'])} -> "
                    f"{format_value(ch['to_value'])}"
                )
    lines.append("=" * 78)
    return "\n".join(lines)
