# -*- coding: utf-8 -*-
"""
成交价核对：用价格历史台账按“成交发生时刻”核对收银成交明细。

设计要点
========
* 每条成交只按 ``transaction_at`` 这个带时区的瞬时去查台账；经营日期 ``date_id``
  只是价格事实的维度键，不拿来当查询时间，也不使用“当前价格”。更正单生效时间
  与成交时间相等时，按已生效处理（台账查询使用 ``effective_at <= at``）。
* 逐笔状态严格区分六类：
    - ``MATCH``                    价格一致
    - ``OVERCHARGED``              多收（实际单价 > 当时有效单价）
    - ``UNDERCHARGED``             少收（实际单价 < 当时有效单价）
    - ``NO_PRICE_AT_TIME``         当时明确无价（NOPRICE）
    - ``MISSING_HISTORY``          底档和已生效更正中都没有该事实
    - ``BEFORE_BASE_EFFECTIVE``    成交时点早于底档生效时点
  后三类没有“期望单价”，绝不计算、也绝不归入多收或少收。
* 有价成交同时给出单件分差和含件数分差，并保留成交时点（输入原文与 UTC）、
  期望价格、直接来源与从底档到直接来源的完整来源链。
* 成交明细是 JSONL。任何一行结构/字段/值不合法、成交行编号重复，都在内存中
  收集全部错误后整份拒绝；调用方不会拿到部分报告（CLI 也不会写任何输出文件）。
* 汇总先逐笔裁定，再分别累计多收与少收的金额和行数，最后才给出净分差；因此
  多收与少收不会在逐笔核对之前互相抵消。报告不含当前时间等易变字段，相同输入
  与相同台账产生逐字节稳定的文字/JSON 报告。

本模块只使用标准库。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

try:  # 兼容 `python3 price_check.py` 与 `from price_correction import ...`
    from history_ledger import (
        STATUS_BEFORE_BASE, STATUS_MISSING, STATUS_NO_BASE, STATUS_NO_PRICE,
        STATUS_PRICED, LedgerInputError, LedgerPersistenceError,
        PriceHistoryLedger, canonical_datetime, format_value,
    )
except ImportError:  # pragma: no cover - 包方式导入分支
    from .history_ledger import (
        STATUS_BEFORE_BASE, STATUS_MISSING, STATUS_NO_BASE, STATUS_NO_PRICE,
        STATUS_PRICED, LedgerInputError, LedgerPersistenceError,
        PriceHistoryLedger, canonical_datetime, format_value,
    )

# --------------------------------------------------------------------------- #
# 逐笔状态
# --------------------------------------------------------------------------- #

STATUS_MATCH = "MATCH"
STATUS_OVERCHARGED = "OVERCHARGED"
STATUS_UNDERCHARGED = "UNDERCHARGED"
STATUS_NO_PRICE_AT_TIME = "NO_PRICE_AT_TIME"
STATUS_MISSING_HISTORY = "MISSING_HISTORY"
STATUS_BEFORE_BASE_EFFECTIVE = "BEFORE_BASE_EFFECTIVE"

CATEGORY_ORDER: Tuple[str, ...] = (
    STATUS_MATCH,
    STATUS_OVERCHARGED,
    STATUS_UNDERCHARGED,
    STATUS_NO_PRICE_AT_TIME,
    STATUS_MISSING_HISTORY,
    STATUS_BEFORE_BASE_EFFECTIVE,
)

CATEGORY_LABELS: Dict[str, str] = {
    STATUS_MATCH: "价格一致",
    STATUS_OVERCHARGED: "多收",
    STATUS_UNDERCHARGED: "少收",
    STATUS_NO_PRICE_AT_TIME: "当时明确无价",
    STATUS_MISSING_HISTORY: "缺少历史",
    STATUS_BEFORE_BASE_EFFECTIVE: "底档尚未生效",
}

PRICED_CATEGORIES = (STATUS_MATCH, STATUS_OVERCHARGED, STATUS_UNDERCHARGED)

REQUIRED_FIELDS: Tuple[str, ...] = (
    "line_id", "store_id", "product_id", "date_id",
    "transaction_at", "quantity", "unit_price_fen",
)


class TransactionInputError(LedgerInputError):
    """成交明细整份无效；``errors`` 含全部行级错误。"""


class PriceCheckLedgerError(LedgerPersistenceError):
    """台账缺失、无法读取或尚未初始化，不能进行成交价核对。"""


# --------------------------------------------------------------------------- #
# 成交明细解析与严格校验
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TransactionLine:
    """一条已通过严格校验的成交明细。时间均以 UTC 规范化字符串保存。"""

    line_id: str
    input_line: int
    store_id: str
    product_id: str
    date_id: str
    transaction_at: str          # 规范 UTC，如 2026-01-09T16:00:00Z
    transaction_at_input: str    # 输入原文
    quantity: int
    unit_price_fen: int


class _StrictJSONError(ValueError):
    """JSON 文本本身有重复字段/非法常量等问题。"""


def _strict_pairs_hook(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    obj: Dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise _StrictJSONError(f"同一 JSON 对象内字段重复: {key!r}")
        obj[key] = value
    return obj


def _reject_json_constant(text: str) -> Any:
    raise _StrictJSONError(f"JSON 中不允许非标准常量 {text}")


def _normalize_id(value: Any, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} 必须是非空字符串或整数 ID，实际: {value!r}")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError(f"{field} 必须是非空字符串或整数 ID，实际: {value!r}")
    if not text:
        raise ValueError(f"{field} 不能为空")
    return text


def _parse_line_object(obj: Any, line_no: int) -> Tuple[Optional[TransactionLine],
                                                        List[str]]:
    errors: List[str] = []
    prefix = f"第 {line_no} 行"
    if not isinstance(obj, dict):
        return None, [f"{prefix}: 每行必须是 JSON 对象，实际为 {type(obj).__name__}"]

    missing = [name for name in REQUIRED_FIELDS if name not in obj]
    if missing:
        errors.append(f"{prefix}: 缺少字段 {', '.join(missing)}")

    line_id: Optional[str] = None
    store_id: Optional[str] = None
    product_id: Optional[str] = None
    date_id: Optional[str] = None

    for field, target in (("line_id", "line"), ("store_id", "store"),
                          ("product_id", "product"), ("date_id", "date")):
        if field not in obj:
            continue
        try:
            normalized = _normalize_id(obj[field], field)
        except ValueError as exc:
            errors.append(f"{prefix}: {exc}")
            continue
        if target == "line":
            line_id = normalized
        elif target == "store":
            store_id = normalized
        elif target == "product":
            product_id = normalized
        else:
            date_id = normalized

    transaction_at: Optional[str] = None
    transaction_at_input: Optional[str] = None
    if "transaction_at" in obj:
        raw_time = obj["transaction_at"]
        if not isinstance(raw_time, str):
            errors.append(
                f"{prefix}: transaction_at 必须是带时区的 ISO 8601 字符串，"
                f"实际: {raw_time!r}"
            )
        else:
            try:
                canonical, _dt, original = canonical_datetime(
                    raw_time, field="transaction_at"
                )
                transaction_at, transaction_at_input = canonical, original
            except LedgerInputError as exc:
                errors.extend(f"{prefix}: {e}" for e in exc.errors)

    quantity: Optional[int] = None
    if "quantity" in obj:
        raw_qty = obj["quantity"]
        if isinstance(raw_qty, bool) or not isinstance(raw_qty, int):
            errors.append(
                f"{prefix}: quantity 必须是正整数，实际: {raw_qty!r}"
            )
        elif raw_qty <= 0:
            errors.append(
                f"{prefix}: quantity 必须是正整数（件数 > 0），实际: {raw_qty!r}"
            )
        else:
            quantity = raw_qty

    unit_price: Optional[int] = None
    if "unit_price_fen" in obj:
        raw_price = obj["unit_price_fen"]
        if isinstance(raw_price, bool) or not isinstance(raw_price, int):
            errors.append(
                f"{prefix}: unit_price_fen 必须是非负整数分，实际: {raw_price!r}"
            )
        elif raw_price < 0:
            errors.append(
                f"{prefix}: unit_price_fen 必须是非负整数分，实际: {raw_price!r}"
            )
        else:
            unit_price = raw_price

    if errors:
        return None, errors

    # 走到这里所有字段都合法且存在。
    assert all(x is not None for x in (
        line_id, store_id, product_id, date_id,
        transaction_at, transaction_at_input, quantity, unit_price,
    ))
    return TransactionLine(
        line_id=line_id,  # type: ignore[arg-type]
        input_line=line_no,
        store_id=store_id,  # type: ignore[arg-type]
        product_id=product_id,  # type: ignore[arg-type]
        date_id=date_id,  # type: ignore[arg-type]
        transaction_at=transaction_at,  # type: ignore[arg-type]
        transaction_at_input=transaction_at_input,  # type: ignore[arg-type]
        quantity=quantity,  # type: ignore[arg-type]
        unit_price_fen=unit_price,  # type: ignore[arg-type]
    ), []


def parse_transactions(text: str, *, source: str = "成交明细") -> List[TransactionLine]:
    """把 JSONL 文本解析为已校验的成交行；任何错误都整份拒绝。"""
    if not isinstance(text, str):
        raise TransactionInputError(f"{source} 必须是文本")
    physical_lines = text.splitlines()
    if not physical_lines:
        raise TransactionInputError(f"{source} 为空，没有任何成交行")

    errors: List[str] = []
    transactions: List[TransactionLine] = []
    # 即使该行其他字段不合法，只要所在行编号本身可解析，也参与“编号全局唯一”检查。
    seen_ids: Dict[str, List[int]] = {}

    for line_no, raw_line in enumerate(physical_lines, 1):
        if not raw_line.strip():
            errors.append(f"第 {line_no} 行: 空行不是合法的成交明细")
            continue
        try:
            obj = json.loads(
                raw_line,
                object_pairs_hook=_strict_pairs_hook,
                parse_constant=_reject_json_constant,
            )
        except json.JSONDecodeError as exc:
            errors.append(f"第 {line_no} 行: JSON 格式错误: {exc.msg}")
            continue
        except _StrictJSONError as exc:
            errors.append(f"第 {line_no} 行: {exc}")
            continue
        if isinstance(obj, dict) and "line_id" in obj:
            try:
                raw_id = _normalize_id(obj["line_id"], "line_id")
            except ValueError:
                pass
            else:
                seen_ids.setdefault(raw_id, []).append(line_no)
        tx, line_errors = _parse_line_object(obj, line_no)
        errors.extend(line_errors)
        if tx is not None:
            transactions.append(tx)

    # 成交行编号必须全局唯一；重复时列出全部行号。
    for line_id, line_nos in sorted(seen_ids.items()):
        if len(line_nos) > 1:
            errors.append(
                f"成交行编号重复: {line_id} 出现在第 "
                f"{', '.join(str(n) for n in line_nos)} 行；整份输入拒绝"
            )

    if errors:
        raise TransactionInputError(errors)
    return transactions


def load_transactions(path: Union[str, Path]) -> Tuple[List[TransactionLine],
                                                        Dict[str, Any]]:
    """读取并校验 JSONL 文件。返回（成交行, 输入元数据）。"""
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise TransactionInputError(f"无法读取成交明细 {path}: {exc}")
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TransactionInputError(f"成交明细 {path} 不是合法 UTF-8: {exc}")
    transactions = parse_transactions(text, source=str(path))
    meta = {
        "path": str(path),
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "line_count": len(transactions),
    }
    return transactions, meta


# --------------------------------------------------------------------------- #
# 逐笔核对
# --------------------------------------------------------------------------- #

def _fmt_fen(value: Optional[int]) -> str:
    if value is None:
        return "—"
    return f"{value}分"


def _signed_fen(value: int) -> str:
    return f"+{value}分" if value > 0 else f"{value}分"


def _expected_state_for_ledger_status(status: str) -> str:
    if status == STATUS_PRICED:
        return "priced"
    if status == STATUS_NO_PRICE:
        return "explicit_no_price"
    if status == STATUS_MISSING:
        return "missing_history"
    if status == STATUS_BEFORE_BASE:
        return "no_history_before_base"
    return status.lower()


def _check_one(ledger: PriceHistoryLedger,
               tx: TransactionLine) -> Dict[str, Any]:
    query = ledger.query_price(
        tx.store_id, tx.product_id, tx.date_id,
        at=tx.transaction_at, refresh=False,
    )
    ledger_status = query["status"]
    if ledger_status == STATUS_NO_BASE:
        # 理论上 reconcile() 已在入口拦截；保留防线，绝不当作“无价”。
        raise PriceCheckLedgerError("台账尚未初始化底档，不能核对成交价")

    base_result: Dict[str, Any] = {
        "line_id": tx.line_id,
        "input_line": tx.input_line,
        "store_id": tx.store_id,
        "product_id": tx.product_id,
        "date_id": tx.date_id,
        "transaction_at": {
            "input": tx.transaction_at_input,
            "utc": tx.transaction_at,
        },
        "quantity": tx.quantity,
        "actual_unit_price_fen": tx.unit_price_fen,
        "actual_total_fen": tx.unit_price_fen * tx.quantity,
        "ledger_query_status": ledger_status,
        "expected_price_state": _expected_state_for_ledger_status(ledger_status),
        "expected_unit_price_fen": None,
        "expected_total_fen": None,
        "unit_difference_fen": None,
        "total_difference_fen": None,
        "difference_direction": None,
        "source": copy.deepcopy(query.get("source")),
        "provenance_chain": copy.deepcopy(query.get("sources", [])),
    }

    if ledger_status == STATUS_PRICED:
        expected = query["value"]
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise PriceCheckLedgerError(
                f"台账对 {tx.store_id}/{tx.product_id}/{tx.date_id} 返回了非整数有价 "
                f"{expected!r}"
            )
        unit_diff = tx.unit_price_fen - expected
        total_diff = unit_diff * tx.quantity
        base_result.update({
            "expected_unit_price_fen": expected,
            "expected_total_fen": expected * tx.quantity,
            "unit_difference_fen": unit_diff,
            "total_difference_fen": total_diff,
        })
        if unit_diff > 0:
            status = STATUS_OVERCHARGED
            summary = f"实际成交单价高于交易时点有效价格 {unit_diff}分/件"
        elif unit_diff < 0:
            status = STATUS_UNDERCHARGED
            summary = f"实际成交单价低于交易时点有效价格 {-unit_diff}分/件"
        else:
            status = STATUS_MATCH
            summary = "实际成交单价与交易时点有效价格一致"
        base_result.update({
            "status": status,
            "status_label": CATEGORY_LABELS[status],
            "difference_direction": (
                "overcharge" if unit_diff > 0 else
                "undercharge" if unit_diff < 0 else "equal"
            ),
            "expected_price_display": _fmt_fen(expected),
            "message": summary,
        })
    elif ledger_status == STATUS_NO_PRICE:
        base_result.update({
            "status": STATUS_NO_PRICE_AT_TIME,
            "status_label": CATEGORY_LABELS[STATUS_NO_PRICE_AT_TIME],
            "expected_price_display": "无价（NOPRICE）",
            "message": (
                "交易时点该门店/商品/经营日期在台账中为明确无价（NOPRICE）；"
                "不是缺少历史，也不计算多收或少收"
            ),
        })
    elif ledger_status == STATUS_MISSING:
        base_result.update({
            "status": STATUS_MISSING_HISTORY,
            "status_label": CATEGORY_LABELS[STATUS_MISSING_HISTORY],
            "expected_price_display": "无（缺少历史）",
            "message": (
                "底档和交易时点前已生效的更正中均无该事实；缺少历史记录，"
                "不是明确无价，也不计算多收或少收"
            ),
        })
    elif ledger_status == STATUS_BEFORE_BASE:
        base_effective_input = query.get("base_effective_from_input")
        base_effective = query.get("base_effective_from")
        base_result.update({
            "status": STATUS_BEFORE_BASE_EFFECTIVE,
            "status_label": CATEGORY_LABELS[STATUS_BEFORE_BASE_EFFECTIVE],
            "expected_price_display": "无（底档尚未生效）",
            # 底档本身晚于成交时点，不能当作成交时点的直接价格来源。
            "source": None,
            "provenance_chain": [],
            "message": (
                f"成交时点 {tx.transaction_at_input}（UTC {tx.transaction_at}）"
                f"早于底档生效时点 {base_effective_input or base_effective}；"
                "此前尚无价格历史，不计算多收或少收"
            ),
        })
    else:  # pragma: no cover - 台账只定义有限状态
        raise PriceCheckLedgerError(f"未知台账查询状态: {ledger_status!r}")

    return base_result


# --------------------------------------------------------------------------- #
# 汇总与报告
# --------------------------------------------------------------------------- #

def _empty_counts() -> Dict[str, int]:
    return {status: 0 for status in CATEGORY_ORDER}


def _bucket_accumulator() -> Dict[str, Any]:
    return {
        "transaction_count": 0,
        "category_counts": _empty_counts(),
        "priced_transaction_count": 0,
        "overcharge_count": 0,
        "undercharge_count": 0,
        "overcharge_amount_fen": 0,
        "undercharge_amount_fen": 0,
        "net_difference_fen": 0,
        "gross_difference_fen": 0,
    }


def _accumulate(bucket: Dict[str, Any], line: Dict[str, Any]) -> None:
    status = line["status"]
    bucket["transaction_count"] += 1
    bucket["category_counts"][status] += 1
    if status not in PRICED_CATEGORIES:
        return
    bucket["priced_transaction_count"] += 1
    total_diff = line["total_difference_fen"]
    assert isinstance(total_diff, int)
    bucket["net_difference_fen"] += total_diff
    bucket["gross_difference_fen"] += abs(total_diff)
    if status == STATUS_OVERCHARGED:
        bucket["overcharge_count"] += 1
        bucket["overcharge_amount_fen"] += total_diff
    elif status == STATUS_UNDERCHARGED:
        bucket["undercharge_count"] += 1
        bucket["undercharge_amount_fen"] += -total_diff


def _finalize_bucket(bucket: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(bucket)
    out["category_counts"] = dict(bucket["category_counts"])
    out["category_labels"] = {
        status: CATEGORY_LABELS[status] for status in CATEGORY_ORDER
    }
    out["has_difference"] = bool(
        bucket["overcharge_count"] or bucket["undercharge_count"]
    )
    return out


def _ledger_meta(ledger: PriceHistoryLedger) -> Dict[str, Any]:
    base = ledger.state["base"]
    return {
        "path": str(ledger.path),
        "base_effective_from": base["effective_from"],
        "base_effective_from_input": base.get("effective_from_input"),
        "order_count": len(ledger.state.get("orders", {})),
        "price_event_count": len(ledger.state.get("price_events", [])),
    }


def check_transactions(ledger: PriceHistoryLedger,
                       transactions: Sequence[TransactionLine],
                       *, input_meta: Optional[Dict[str, Any]] = None
                       ) -> Dict[str, Any]:
    """对已校验的成交行做逐笔核对并汇总；台账必须已初始化。"""
    if not ledger.path.exists():
        raise PriceCheckLedgerError(
            f"价格历史台账文件不存在：{ledger.path}；请先初始化底档"
        )
    # 一次刷新后固定快照；逐笔查询不再反复读盘，保证整份报告对应同一台账状态。
    ledger.reload()
    if not ledger.state.get("base"):
        raise PriceCheckLedgerError(
            f"价格历史台账尚未初始化底档：{ledger.path}；请先执行 init"
        )

    lines = [_check_one(ledger, tx) for tx in transactions]

    summary_bucket = _bucket_accumulator()
    group_buckets: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for line in lines:
        _accumulate(summary_bucket, line)
        key = (line["store_id"], line["product_id"], line["date_id"])
        group_buckets.setdefault(key, _bucket_accumulator())
        _accumulate(group_buckets[key], line)

    summary = _finalize_bucket(summary_bucket)
    summary["categories"] = [
        {
            "status": status,
            "label": CATEGORY_LABELS[status],
            "count": summary_bucket["category_counts"][status],
        }
        for status in CATEGORY_ORDER
    ]
    summary["non_priced_transaction_count"] = (
        summary_bucket["transaction_count"]
        - summary_bucket["priced_transaction_count"]
    )
    summary["net_difference_formula"] = (
        "净分差 = 多收合计 - 少收合计；正数表示净多收，负数表示净少收"
    )
    summary["per_line_first_note"] = (
        "先逐笔判定多收/少收，再分别汇总；多收与少收不会在逐笔核对前抵消"
    )

    by_fact: List[Dict[str, Any]] = []
    for key in sorted(group_buckets):
        item = _finalize_bucket(group_buckets[key])
        item.update({
            "store_id": key[0],
            "product_id": key[1],
            "date_id": key[2],
        })
        by_fact.append({
            "store_id": item.pop("store_id"),
            "product_id": item.pop("product_id"),
            "date_id": item.pop("date_id"),
            **item,
        })

    meta = {
        "path": None,
        "sha256": None,
        "line_count": len(lines),
    }
    if input_meta:
        meta.update(input_meta)
    meta["line_count"] = len(lines)

    return {
        "schema": "transaction-price-check-v1",
        "input": meta,
        "ledger": _ledger_meta(ledger),
        "lines": lines,
        "summary": summary,
        "by_fact": by_fact,
    }


def check_transactions_file(ledger: PriceHistoryLedger,
                            path: Union[str, Path]) -> Dict[str, Any]:
    """读取 JSONL、严格校验、逐笔核对；任一行无效则不会返回报告。"""
    transactions, meta = load_transactions(path)
    return check_transactions(ledger, transactions, input_meta=meta)


# --------------------------------------------------------------------------- #
# 文字报告
# --------------------------------------------------------------------------- #

def _render_source(source: Optional[Dict[str, Any]]) -> str:
    if not source:
        return "（无）"
    if source.get("type") == "base":
        return (
            "初始底档（生效 "
            f"{source.get('effective_from_input') or source.get('effective_from')}）"
        )
    return (
        f"更正单 {source.get('order_id')} / 通过组 {source.get('group_id')} "
        f"@ {source.get('effective_at')} seq={source.get('sequence')}"
    )


def _render_chain(chain: Sequence[Dict[str, Any]]) -> str:
    if not chain:
        return "（无价格来源链）"
    parts: List[str] = []
    for index, item in enumerate(chain):
        if item.get("kind") == "base":
            parts.append(
                f"{index}. 底档起始（生效 {item.get('effective_from')}）"
                f"：{format_value(item.get('value'))}"
            )
        else:
            parts.append(
                f"{index}. {item.get('order_id')} [{item.get('group_id')}] "
                f"{format_value(item.get('before_value'))} -> "
                f"{format_value(item.get('after_value'))} "
                f"@ {item.get('effective_at')} seq={item.get('sequence')}"
            )
    return "；".join(parts)


def _signed_amount(value: int) -> str:
    return f"+{value}分" if value > 0 else f"{value}分"


def _amount_with_sign(value: int, sign: str) -> str:
    """按指定方向展示金额；0 仍显示 0分，不显示 -0分。"""
    if value == 0:
        return "0分"
    return f"{sign}{value}分"


def render_report_text(report: Dict[str, Any]) -> str:
    """稳定、可读的中文成交价核对报告。"""
    summary = report["summary"]
    ledger = report["ledger"]
    source_input = report["input"]
    lines: List[Dict[str, Any]] = report["lines"]

    L: List[str] = []
    L.append("=" * 88)
    L.append("成交价核对报告（按成交发生时刻的有效价格；台账只读）")
    L.append("=" * 88)
    L.append(
        f"成交明细：{source_input.get('path') or '（内存输入）'}  "
        f"sha256={source_input.get('sha256') or '—'}"
    )
    L.append(f"价格台账：{ledger['path']}")
    L.append(
        "底档生效："
        f"{ledger.get('base_effective_from_input') or ledger['base_effective_from']}"
        f"；已接受更正单 {ledger['order_count']} 笔，价格事件 "
        f"{ledger['price_event_count']} 条"
    )
    L.append(
        f"成交 {summary['transaction_count']} 行；有价 "
        f"{summary['priced_transaction_count']} 行；非有价 "
        f"{summary['non_priced_transaction_count']} 行"
    )
    L.append(
        "分类计数："
        + "；".join(
            f"{CATEGORY_LABELS[status]} "
            f"{summary['category_counts'][status]}"
            for status in CATEGORY_ORDER
        )
    )
    L.append("")

    L.append("一、逐笔核对（按成交明细原始顺序）")
    L.append("-" * 88)
    if not lines:
        L.append("（无成交行）")
    for line in lines:
        L.append(
            f"[{line['input_line']:>3}] 成交行 {line['line_id']}  "
            f"{line['store_id']} / {line['product_id']} / {line['date_id']}"
        )
        L.append(
            f"      成交时点：{line['transaction_at']['input']}"
            f"（UTC {line['transaction_at']['utc']}）"
        )
        L.append(
            f"      成交：{line['quantity']} 件 × "
            f"{line['actual_unit_price_fen']}分 = {line['actual_total_fen']}分"
        )
        L.append(f"      核对结果：{line['status_label']}（{line['status']}）")
        if line["status"] in PRICED_CATEGORIES:
            L.append(
                f"      期望单价：{line['expected_unit_price_fen']}分；"
                f"期望含件数金额：{line['expected_total_fen']}分"
            )
            L.append(
                f"      单件分差：{_signed_fen(line['unit_difference_fen'])}；"
                f"含件数分差：{_signed_fen(line['total_difference_fen'])}"
            )
        else:
            L.append(f"      期望价格：{line['expected_price_display']}；不计算分差")
        L.append(f"      说明：{line['message']}")
        L.append(f"      直接来源：{_render_source(line.get('source'))}")
        L.append(f"      来源链：{_render_chain(line.get('provenance_chain', []))}")
        L.append("")

    L.append("二、汇总（先逐笔裁定，多收与少收分别累计）")
    L.append("-" * 88)
    L.append(
        f"价格一致 {summary['category_counts'][STATUS_MATCH]} 行；"
        f"多收 {summary['overcharge_count']} 行，合计 "
        f"{_amount_with_sign(summary['overcharge_amount_fen'], '+')}；"
        f"少收 {summary['undercharge_count']} 行，合计 "
        f"{_amount_with_sign(summary['undercharge_amount_fen'], '-')}"
    )
    L.append(
        f"多收与少收绝对额合计：{summary['gross_difference_fen']}分；"
        f"净分差：{_signed_amount(summary['net_difference_fen'])}"
        f"（= 多收合计 - 少收合计）"
    )
    L.append(
        f"当时明确无价 {summary['category_counts'][STATUS_NO_PRICE_AT_TIME]} 行；"
        f"缺少历史 {summary['category_counts'][STATUS_MISSING_HISTORY]} 行；"
        f"底档尚未生效 "
        f"{summary['category_counts'][STATUS_BEFORE_BASE_EFFECTIVE]} 行"
    )
    L.append(
        "非有价成交不参与分差计算，也不会被误报为多收或少收。"
    )
    L.append("")

    L.append("三、按门店 / 商品 / 经营日期汇总")
    L.append("-" * 88)
    if not report["by_fact"]:
        L.append("（无分组）")
    for group in report["by_fact"]:
        counts = group["category_counts"]
        L.append(
            f"{group['store_id']} / {group['product_id']} / {group['date_id']}"
        )
        L.append(
            f"  成交 {group['transaction_count']} 行；有价 "
            f"{group['priced_transaction_count']} 行；"
            f"一致 {counts[STATUS_MATCH]}、多收 {counts[STATUS_OVERCHARGED]}、"
            f"少收 {counts[STATUS_UNDERCHARGED]}、"
            f"明确无价 {counts[STATUS_NO_PRICE_AT_TIME]}、"
            f"缺少历史 {counts[STATUS_MISSING_HISTORY]}、"
            f"底档前 {counts[STATUS_BEFORE_BASE_EFFECTIVE]}"
        )
        L.append(
            f"  多收 {_amount_with_sign(group['overcharge_amount_fen'], '+')}"
            f"（{group['overcharge_count']} 行）；"
            f"少收 {_amount_with_sign(group['undercharge_amount_fen'], '-')}"
            f"（{group['undercharge_count']} 行）；"
            f"净分差 {_signed_amount(group['net_difference_fen'])}"
        )
        L.append("")

    L.append("-" * 88)
    L.append(
        "口径：分差 =（实际成交单价 - 交易时点有效单价）× 件数；"
        "正数为多收，负数为少收。"
    )
    L.append(
        "成交时点优先于经营日期和当前价格；更正生效时间等于成交时点按已生效处理。"
    )
    L.append("=" * 88)
    return "\n".join(L)


def report_to_json(report: Dict[str, Any]) -> str:
    """稳定 JSON 序列化（不排序键，保留固定写入顺序）。"""
    return json.dumps(report, ensure_ascii=False, indent=2)


__all__ = [
    "CATEGORY_LABELS", "CATEGORY_ORDER", "PRICED_CATEGORIES",
    "PriceCheckLedgerError", "TransactionInputError", "TransactionLine",
    "check_transactions", "check_transactions_file", "load_transactions",
    "parse_transactions", "render_report_text", "report_to_json",
    "STATUS_BEFORE_BASE_EFFECTIVE", "STATUS_MATCH", "STATUS_MISSING_HISTORY",
    "STATUS_NO_PRICE_AT_TIME", "STATUS_OVERCHARGED", "STATUS_UNDERCHARGED",
]
