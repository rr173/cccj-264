# -*- coding: utf-8 -*-
"""
门店促销价格同步：完整首包与续传差量。

设计要点
========
* 同步建立在 :class:`history_ledger.PriceHistoryLedger` 之上。台账接收顺序与价格
  生效时间是两条独立的轴：
    1. 接收修订（新更正单投递，或迟到的早期更正引发重算）会改变事实在各时点的
       有效价格，甚至让原先被冲突组挡下的后续组转为生效；
    2. 截止时点（``as_of``）推进会让“已接收但当时尚未生效”的未来事件进入价格。
  两类变化都必须完整反映。
* 每个门店的每次发布产出一个**包**（``full`` 首包或 ``delta`` 续传）和一个固定
  门店、台账身份、接收修订与截止时点的**快照凭证**。凭证是带封印（seal）的
  不可变结构；凭证登记随台账原子落盘，后续更正只产生新凭证，绝不反改已发凭证。
* 续传必须出示上次已生成快照的凭证。服务端比对凭证封印、登记、门店/台账身份、
  接收修订先后关系与截止时点；损坏、跨店、跨台账、倒拨截止时点的凭证一律拒绝。
* 续传只读取以下事实，不遍历未变价目、不重建整店快照：
    - 台账 ``change_basis`` 中本门店被事件/冲突组触及的事实（索引读取）；
    - 新旧凭证水位线之间新生效的事件对应事实；
    - 前后修订之间新接收单据中本门店的事实；
    - 前快照自身记录的稀疏状态（用于状态比对）。
* 无变化的事实不发送；事实在新时点的有效状态变为明确无价（NOPRICE）时发送
  ``remove``。对同一次发布，任一事实最多一条差量条目。
* 应用差量后的门店状态，必须等于当前已接收台账修订在该截止时点对该门店的完整
  价格状态（见 :func:`audit_application`）。

本模块只用标准库。
"""

from __future__ import annotations

import copy
import hashlib
import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from engine import NOPRICE
    from history_ledger import (
        LedgerInputError, canonical_json, fact_key, normalize_fact,
        parse_aware_datetime,
    )
except ImportError:  # pragma: no cover - 包方式导入分支
    from .engine import NOPRICE
    from .history_ledger import (
        LedgerInputError, canonical_json, fact_key, normalize_fact,
        parse_aware_datetime,
    )

Fact = Tuple[str, str, str]

SYNC_SCHEMA = "store-price-sync-v1"
CREDENTIAL_KIND = "snapshot-credential-v1"
PACKAGE_FULL = "full"
PACKAGE_DELTA = "delta"
OP_UPSERT = "upsert"
OP_REMOVE = "remove"

# 凭证封印/摘要使用的算法；集中成常量，便于审计口径一致。
SEAL_ALGO = "sha256"


class SyncError(LedgerInputError):
    """同步发布/应用/凭证校验错误。"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_json(obj: Any) -> str:
    """对任意可 JSON 化对象做规范化 SHA-256 摘要。"""
    return _sha256_text(canonical_json(obj))


def _as_utc(value: Any, *, field: str = "时点") -> Tuple[datetime, str]:
    """返回 (UTC datetime, 规范 UTC 字符串)。"""
    dt = parse_aware_datetime(value, allow_naive=True, field=field)
    return dt, dt.isoformat().replace("+00:00", "Z")


def fact_to_entry_dict(fact: Fact) -> Dict[str, str]:
    return {"store_id": fact[0], "product_id": fact[1], "date_id": fact[2]}


# --------------------------------------------------------------------------- #
# 台账身份与接收修订
# --------------------------------------------------------------------------- #

def ledger_identity(ledger: Any) -> Dict[str, str]:
    """台账身份由底档（起点+全部事实起始值）的规范化指纹决定。

    底档不变则身份稳定；另一个台账即使事实集合相同，只要底档内容不同就是
    不同的 ``ledger_id``，跨台账凭证因此可被识别并拒绝。
    """
    base = ledger.state.get("base")
    if not base:
        raise SyncError("台账尚未初始化底档，无法建立同步身份")
    base_fp = digest_json(base)
    return {
        "ledger_id": "ldg_" + base_fp[:32],
        "base_fingerprint": base_fp,
        "base_effective_from": base["effective_from"],
    }


def current_revision(ledger: Any, base_fp: str) -> Dict[str, Any]:
    """当前接收修订：已接受单据数量 + 基于底档与全部单据身份的状态摘要。

    单据内容/生效位置变化（含迟到早期更正引发的重算结果变化）都会改变
    ``state_digest``；只追加完全相同的重复投递不会改变修订。
    """
    orders = ledger.state.get("orders", {})
    order_digest = digest_json([
        {
            "order_id": rec["order_id"],
            "effective_at": rec["effective_at"],
            "sequence": rec["sequence"],
            "content_sha256": rec["content_sha256"],
        }
        for oid, rec in sorted(orders.items())
    ])
    state_digest = digest_json({
        "base_fingerprint": base_fp,
        "order_digest": order_digest,
    })
    return {
        "revision_no": len(orders),
        "state_digest": state_digest,
        "order_digest": order_digest,
        "order_ids": sorted(orders.keys()),
    }


# --------------------------------------------------------------------------- #
# 取价辅助：全部走索引/单事实查询，不做整店全量扫描
# --------------------------------------------------------------------------- #

def _effective_value(ledger: Any, fact: Fact, at_dt: datetime) -> Any:
    """事实在截止时点的有效价格；底档外事实返回 None（缺少历史）。"""
    result = ledger.query_price(*fact, at=at_dt, refresh=False)
    if not result.get("found"):
        return None
    return result["value"]


def _change_basis_index(ledger: Any) -> Dict[str, Dict[str, Any]]:
    basis = ledger.state.get("change_basis") or {"facts": []}
    return {item["fact_key"]: item for item in basis.get("facts", [])}


def _store_base_facts(ledger: Any, store_id: str) -> List[Fact]:
    """仅首包使用：枚举该门店在底档中的事实（首包允许全量）。"""
    base = ledger.state.get("base") or {}
    return [
        tuple(item["fact"])  # type: ignore[return-value]
        for item in base.get("prices", [])
        if str(item["fact"][0]) == store_id
    ]


def _events_for_store(ledger: Any, store_id: str) -> List[Dict[str, Any]]:
    return [
        e for e in ledger.state.get("price_events", [])
        if e.get("store_id") == store_id
    ]


def _event_fact_key(event_id: str) -> str:
    """event_id 形如 ``order_id:group_id:store|product|date``。"""
    return event_id.split(":", 2)[2]


# --------------------------------------------------------------------------- #
# 凭证封印与登记
# --------------------------------------------------------------------------- #

def _credential_seal_material(cred: Dict[str, Any]) -> Dict[str, Any]:
    """封印覆盖的固定字段：门店、台账身份、接收修订、截止时点、包与条目摘要。"""
    return {
        "kind": cred["kind"],
        "snapshot_id": cred["snapshot_id"],
        "store_id": cred["store_id"],
        "ledger_id": cred["ledger_id"],
        "base_fingerprint": cred["base_fingerprint"],
        "revision": cred["revision"],
        "as_of": cred["as_of"],
        "package_type": cred["package_type"],
        "entries_digest": cred["entries_digest"],
        "schema": cred.get("schema", SYNC_SCHEMA),
        "seal_algo": SEAL_ALGO,
    }


def seal_credential(cred: Dict[str, Any]) -> Dict[str, Any]:
    material = _credential_seal_material(cred)
    cred["seal"] = SEAL_ALGO + ":" + digest_json(material)
    return cred


def verify_credential_seal(cred: Any) -> None:
    if not isinstance(cred, dict):
        raise SyncError("快照凭证必须是 JSON 对象")
    required = (
        "kind", "snapshot_id", "store_id", "ledger_id", "base_fingerprint",
        "revision", "as_of", "package_type", "entries_digest", "seal",
    )
    missing = [key for key in required if key not in cred]
    if missing:
        raise SyncError(f"快照凭证缺少字段: {', '.join(missing)}")
    if cred.get("kind") != CREDENTIAL_KIND:
        raise SyncError(f"不支持的凭证类型: {cred.get('kind')!r}")
    expected = SEAL_ALGO + ":" + digest_json(_credential_seal_material(cred))
    actual = cred.get("seal")
    if not isinstance(actual, str) or not secrets.compare_digest(expected, actual):
        raise SyncError(
            f"快照凭证封印不匹配（凭证已损坏或被篡改）: {cred.get('snapshot_id')!r}"
        )


# --------------------------------------------------------------------------- #
# 同步服务
# --------------------------------------------------------------------------- #

class StoreSyncService:
    """在一个已落盘台账上发布门店首包/差量，并登记不可变快照凭证。"""

    def __init__(self, ledger: Any):
        self.ledger = ledger
        if "change_basis" not in ledger.state:
            # 兼容由旧版本写入、尚未带 change_basis 的台账文件。
            from history_ledger import recompute_state
            recompute_state(ledger.state)
        ledger.state.setdefault("sync_snapshots", {})

    # ---------------- 凭证登记（随台账原子落盘） ---------------- #

    def _registry(self) -> Dict[str, Any]:
        return self.ledger.state.setdefault("sync_snapshots", {})

    def _persist_registry(self) -> None:
        # 复用台账自身的原子替换：临时文件 + os.replace，包与凭证一次发布，
        # 失败时调用方拿不到包，磁盘上也不会出现半份结果。
        self.ledger._commit(self.ledger.state)  # noqa: SLF001

    def _store_record(self, store_id: str) -> Dict[str, Any]:
        registry = self._registry()
        record = registry.get(store_id)
        if record is None:
            record = {"store_id": store_id, "snapshots": {}}
            registry[store_id] = record
        record.setdefault("snapshots", {})
        return record

    def _save_snapshot(self, store_id: str, snapshot_id: str,
                       entry: Dict[str, Any]) -> None:
        record = self._store_record(store_id)
        if snapshot_id in record["snapshots"]:
            # 凭证 id 带随机后缀；碰撞在实践中不可能，遇到则直接拒绝以防覆盖。
            raise SyncError(f"快照标识冲突，拒绝覆盖已发凭证: {snapshot_id}")
        record["snapshots"][snapshot_id] = entry
        self._persist_registry()

    # ---------------- 通用校验 ---------------- #

    def _prepare(self, store_id: str, as_of: Any) -> Dict[str, Any]:
        if not isinstance(store_id, str) or not store_id.strip():
            raise SyncError("store_id 必须是非空字符串")
        store_id = store_id.strip()
        at_dt, at_canon = _as_utc(as_of, field="截止时点 as_of")
        identity = ledger_identity(self.ledger)
        base_dt = parse_aware_datetime(identity["base_effective_from"])
        if at_dt < base_dt:
            raise SyncError(
                f"截止时点 {at_canon} 早于底档起点 {identity['base_effective_from']}，"
                "此前尚无价格状态可同步"
            )
        revision = current_revision(self.ledger, identity["base_fingerprint"])
        return {
            "store_id": store_id,
            "at_dt": at_dt,
            "as_of": at_canon,
            "identity": identity,
            "revision": revision,
        }

    def _make_credential(self, *, store_id: str, identity: Dict[str, str],
                         revision: Dict[str, Any], as_of: str,
                         package_type: str, entries_digest: str,
                         parent_snapshot_id: Optional[str]) -> Dict[str, Any]:
        snapshot_material = {
            "kind": CREDENTIAL_KIND,
            "store_id": store_id,
            "ledger_id": identity["ledger_id"],
            "base_fingerprint": identity["base_fingerprint"],
            "revision": {
                "revision_no": revision["revision_no"],
                "state_digest": revision["state_digest"],
            },
            "as_of": as_of,
            "package_type": package_type,
            "entries_digest": entries_digest,
            "parent_snapshot_id": parent_snapshot_id,
        }
        snapshot_id = "snap_" + digest_json(snapshot_material)[:24] + \
            "_" + secrets.token_hex(4)
        cred = {
            "kind": CREDENTIAL_KIND,
            "schema": SYNC_SCHEMA,
            "snapshot_id": snapshot_id,
            "store_id": store_id,
            "ledger_id": identity["ledger_id"],
            "base_fingerprint": identity["base_fingerprint"],
            "revision": {
                "revision_no": revision["revision_no"],
                "state_digest": revision["state_digest"],
            },
            "as_of": as_of,
            "package_type": package_type,
            "entries_digest": entries_digest,
            "parent_snapshot_id": parent_snapshot_id,
            "issued_at_utc": datetime.now(timezone.utc)
                .isoformat().replace("+00:00", "Z"),
        }
        return seal_credential(cred)

    def _envelope(self, *, package_type: str, store_id: str,
                  identity: Dict[str, str], revision: Dict[str, Any],
                  as_of: str, entries: List[Dict[str, Any]],
                  credential: Dict[str, Any],
                  parent_credential: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "schema": SYNC_SCHEMA,
            "package_type": package_type,
            "store_id": store_id,
            "ledger_id": identity["ledger_id"],
            "base_fingerprint": identity["base_fingerprint"],
            "as_of": as_of,
            "revision": {
                "revision_no": revision["revision_no"],
                "state_digest": revision["state_digest"],
            },
            "entry_count": len(entries),
            "entries_digest": credential["entries_digest"],
            "parent_snapshot_id": credential["parent_snapshot_id"],
            "entries": entries,
            "credential": credential,
            "parent_credential": parent_credential,
        }

    # ---------------- 完整首包 ---------------- #

    def publish_full(self, store_id: str, as_of: Any) -> Dict[str, Any]:
        """发布完整首包：枚举该门店底档中的全部事实及其截止时点有效状态。"""
        ctx = self._prepare(store_id, as_of)
        store_id = ctx["store_id"]

        entries: List[Dict[str, Any]] = []
        for fact in sorted(_store_base_facts(self.ledger, store_id)):
            value = _effective_value(self.ledger, fact, ctx["at_dt"])
            if value is None:
                # 底档事实在该时点必已有状态（at >= 底档起点）；防御性分支。
                continue
            fkey = fact_key(fact)
            entries.append({
                "fact": fact_to_entry_dict(fact),
                "fact_key": fkey,
                "value": value,
                "state": "no_price" if value == NOPRICE else "priced",
            })

        entries_digest = digest_json(entries)
        cred = self._make_credential(
            store_id=store_id, identity=ctx["identity"],
            revision=ctx["revision"], as_of=ctx["as_of"],
            package_type=PACKAGE_FULL, entries_digest=entries_digest,
            parent_snapshot_id=None,
        )
        watermark = self._watermark(store_id, ctx["at_dt"], ctx["revision"])
        package = self._envelope(
            package_type=PACKAGE_FULL, store_id=store_id,
            identity=ctx["identity"], revision=ctx["revision"],
            as_of=ctx["as_of"], entries=entries, credential=cred,
            parent_credential=None,
        )
        # 首包固定底档基线（后续快照回退比对用，不可变）；首包本身不维护
        # 稀疏差量状态，避免后续每次续传都继承整店副本。
        base_values = {
            fact_key(tuple(item["fact"])): item["value"]
            for item in self.ledger.state["base"]["prices"]
            if str(item["fact"][0]) == store_id
        }
        # 先在内存中组好包与凭证，再原子落盘登记；落盘失败则不返回包。
        self._save_snapshot(store_id, cred["snapshot_id"], {
            "snapshot_id": cred["snapshot_id"],
            "store_id": store_id,
            "ledger_id": ctx["identity"]["ledger_id"],
            "base_fingerprint": ctx["identity"]["base_fingerprint"],
            "package_type": PACKAGE_FULL,
            "as_of": ctx["as_of"],
            "revision": copy.deepcopy(ctx["revision"]),
            "entries_digest": entries_digest,
            "credential": cred,
            "watermark": watermark,
            "base_values": base_values,
            "basis_states": {},
        })
        return package

    # ---------------- 续传差量 ---------------- #

    def _watermark(self, store_id: str, at_dt: datetime,
                   revision: Dict[str, Any]) -> Dict[str, Any]:
        """发布水位线：截至本快照、本门店已生效事件与全部已接收单号。"""
        effective_event_ids = sorted(
            e["event_id"] for e in _events_for_store(self.ledger, store_id)
            if parse_aware_datetime(e["effective_at"]) <= at_dt
        )
        return {
            "effective_event_ids": effective_event_ids,
            "order_ids": list(revision["order_ids"]),
        }

    def _validate_continuation(self, credential: Any,
                               ctx: Dict[str, Any]) -> Dict[str, Any]:
        """校验续传凭证：封印、登记、跨店/跨台账、修订先后、截止时点不倒退。"""
        verify_credential_seal(credential)
        store_id = ctx["store_id"]
        identity = ctx["identity"]

        if credential["store_id"] != store_id:
            raise SyncError(
                f"跨店凭证被拒绝：凭证固定门店 {credential['store_id']!r}，"
                f"本次请求门店 {store_id!r}"
            )
        if credential["ledger_id"] != identity["ledger_id"] \
                or credential["base_fingerprint"] != identity["base_fingerprint"]:
            raise SyncError(
                "跨台账凭证被拒绝：凭证台账身份 "
                f"{credential['ledger_id']!r} 与当前台账 "
                f"{identity['ledger_id']!r} 不一致"
            )

        record = self._store_record(store_id)
        saved = record["snapshots"].get(credential["snapshot_id"])
        if saved is None:
            raise SyncError(
                f"凭证 {credential['snapshot_id']!r} 不在本台账已发快照登记中；"
                "拒绝凭未知凭证续传"
            )
        saved_cred = saved["credential"]
        if digest_json(saved_cred) != digest_json(credential):
            raise SyncError(
                f"凭证 {credential['snapshot_id']!r} 与已登记内容不一致；"
                "已发快照不可反改"
            )

        prev_rev = credential["revision"]
        cur_rev = ctx["revision"]
        if prev_rev["state_digest"] == cur_rev["state_digest"] \
                and prev_rev["revision_no"] == cur_rev["revision_no"]:
            rev_relation = "same"
        elif cur_rev["revision_no"] >= prev_rev["revision_no"]:
            rev_relation = "advanced"
        else:
            raise SyncError(
                "接收修订倒退：凭证修订号 "
                f"{prev_rev['revision_no']} 晚于当前修订号 "
                f"{cur_rev['revision_no']}；台账历史不允许回退"
            )

        prev_at = parse_aware_datetime(credential["as_of"])
        if ctx["at_dt"] < prev_at:
            raise SyncError(
                f"截止时点不得倒退：凭证截止 {credential['as_of']}，"
                f"本次请求 {ctx['as_of']}"
            )
        return {"saved": saved, "rev_relation": rev_relation,
                "prev_at": prev_at}

    def publish_delta(self, store_id: str, as_of: Any,
                      previous_credential: Any) -> Dict[str, Any]:
        """凭上次快照凭证发布续传差量。"""
        ctx = self._prepare(store_id, as_of)
        store_id = ctx["store_id"]
        cont = self._validate_continuation(previous_credential, ctx)
        saved = cont["saved"]
        prev_watermark: Dict[str, Any] = saved["watermark"]
        prev_basis_states: Dict[str, Any] = saved.get("basis_states", {})

        # ---- 只收集“可能变化”的候选事实，绝不遍历未变价目 ---- #
        candidate_keys: Set[str] = set()

        # 1) 变更依据索引：本门店被价格事件/冲突组触及的事实。
        basis_index = _change_basis_index(self.ledger)
        for fkey, ref in basis_index.items():
            if ref.get("store_id") == store_id:
                candidate_keys.add(fkey)

        # 2) 水位线之间新生效的事件（纯时点推进：未来事件进入价格）。
        prev_events = set(prev_watermark.get("effective_event_ids", []))
        cur_events = set(self._watermark(
            store_id, ctx["at_dt"], ctx["revision"])["effective_event_ids"])
        for event_id in cur_events - prev_events:
            candidate_keys.add(_event_fact_key(event_id))

        # 3) 前快照发布之后新接收的单据中本门店的事实（含迟到早期更正）。
        #    orders 按 order_id 而非接收序号存储，必须逐单据取身份并与水位线
        #    中的已接收单号比对，不能假定字典顺序。
        prev_orders = set(prev_watermark.get("order_ids", []))
        for oid, rec in self.ledger.state.get("orders", {}).items():
            if oid in prev_orders:
                continue
            for group in rec.get("bookable", {}).get("groups", []):
                for line_item in group["facts"]:
                    fact = tuple(line_item["fact"])
                    if fact[0] == store_id:
                        candidate_keys.add(fact_key(fact))

        # ---- 逐候选事实比对前后状态；无变化不发送，变无价发 remove ---- #
        entries: List[Dict[str, Any]] = []
        prev_base_values: Dict[str, Any] = saved.get("base_values", {})
        # 差量登记只记录本快照触及事实的稀疏状态；从未变价的事实不复制。
        new_basis_states: Dict[str, Any] = {}
        for fkey in sorted(candidate_keys):
            fact = normalize_fact(fkey)
            new_value = _effective_value(self.ledger, fact, ctx["at_dt"])
            if new_value is None:
                # 底档外事实本不该进入候选；防御性跳过。
                continue
            # 前状态优先取链上已记录的变更后状态；未被任何差量触及的事实
            # 回退到底档基线值（首包已固定，不可变）。
            old_value = prev_basis_states.get(fkey, prev_base_values.get(fkey))
            new_basis_states[fkey] = new_value
            if old_value == new_value:
                continue  # 无变化事实不发送
            if new_value == NOPRICE:
                entries.append({
                    "op": OP_REMOVE,
                    "fact": fact_to_entry_dict(fact),
                    "fact_key": fkey,
                    "previous_value": old_value,
                    "reason": "effective_state_is_noprice",
                })
            else:
                entries.append({
                    "op": OP_UPSERT,
                    "fact": fact_to_entry_dict(fact),
                    "fact_key": fkey,
                    "value": new_value,
                    "previous_value": old_value,
                    "state": "priced",
                })

        entries_digest = digest_json(entries)
        cred = self._make_credential(
            store_id=store_id, identity=ctx["identity"],
            revision=ctx["revision"], as_of=ctx["as_of"],
            package_type=PACKAGE_DELTA, entries_digest=entries_digest,
            parent_snapshot_id=previous_credential["snapshot_id"],
        )
        watermark = self._watermark(store_id, ctx["at_dt"], ctx["revision"])
        package = self._envelope(
            package_type=PACKAGE_DELTA, store_id=store_id,
            identity=ctx["identity"], revision=ctx["revision"],
            as_of=ctx["as_of"], entries=entries, credential=cred,
            parent_credential=copy.deepcopy(previous_credential),
        )
        self._save_snapshot(store_id, cred["snapshot_id"], {
            "snapshot_id": cred["snapshot_id"],
            "store_id": store_id,
            "ledger_id": ctx["identity"]["ledger_id"],
            "base_fingerprint": ctx["identity"]["base_fingerprint"],
            "package_type": PACKAGE_DELTA,
            "as_of": ctx["as_of"],
            "revision": copy.deepcopy(ctx["revision"]),
            "entries_digest": entries_digest,
            "credential": cred,
            "parent_snapshot_id": previous_credential["snapshot_id"],
            "watermark": watermark,
            # 沿链传递不可变的底档基线；本快照只保存候选事实的稀疏状态。
            "base_values": saved.get("base_values", {}),
            "basis_states": new_basis_states,
        })
        return package

    # ---------------- 查询 ---------------- #

    def list_snapshots(self, store_id: str) -> List[Dict[str, Any]]:
        record = self._registry().get(store_id)
        if record is None:
            return []
        return [
            copy.deepcopy(entry["credential"])
            for entry in sorted(
                record["snapshots"].values(),
                key=lambda item: (item["as_of"], item["snapshot_id"]),
            )
        ]


# --------------------------------------------------------------------------- #
# 门店侧：应用包 + 完整性审计
# --------------------------------------------------------------------------- #

def verify_package(package: Any) -> None:
    """校验包与凭证的结构、封印及条目摘要的一致性。"""
    if not isinstance(package, dict):
        raise SyncError("同步包必须是 JSON 对象")
    for key in ("schema", "package_type", "store_id", "ledger_id",
                "base_fingerprint", "as_of", "entries_digest",
                "entries", "credential"):
        if key not in package:
            raise SyncError(f"同步包缺少字段: {key}")
    if package["schema"] != SYNC_SCHEMA:
        raise SyncError(f"不支持的同步包 schema: {package['schema']!r}")
    if package["package_type"] not in (PACKAGE_FULL, PACKAGE_DELTA):
        raise SyncError(f"未知包类型: {package['package_type']!r}")
    if not isinstance(package["entries"], list):
        raise SyncError("同步包 entries 必须是列表")

    cred = package["credential"]
    verify_credential_seal(cred)
    if cred["store_id"] != package["store_id"]:
        raise SyncError("包与凭证的门店不一致")
    if cred["ledger_id"] != package["ledger_id"] \
            or cred["base_fingerprint"] != package["base_fingerprint"]:
        raise SyncError("包与凭证的台账身份不一致")
    if cred["package_type"] != package["package_type"]:
        raise SyncError("包与凭证的包类型不一致")
    if cred["as_of"] != package["as_of"]:
        raise SyncError("包与凭证的截止时点不一致")

    actual_entries_digest = digest_json(package["entries"])
    if actual_entries_digest != package["entries_digest"] \
            or actual_entries_digest != cred["entries_digest"]:
        raise SyncError("包条目摘要与凭证封印不一致（包内容可能被篡改）")


def apply_package(package: Any, client_state: Optional[Dict[str, Any]] = None,
                  *, expected_store_id: Optional[str] = None) -> Dict[str, Any]:
    """门店侧应用首包/差量，返回更新后的客户端状态。

    ``client_state`` 结构：
      ``{"store_id", "ledger_id", "base_fingerprint", "as_of", "revision",
         "mirror": {fact_key: 价格或 NOPRICE}, "last_credential"}``。

    首包必须先于差量；差量必须接续同一门店、同一台账上一次应用的凭证。
    """
    verify_package(package)
    store_id = package["store_id"]
    if expected_store_id is not None and store_id != expected_store_id:
        raise SyncError(
            f"包门店 {store_id!r} 与客户端登记门店 {expected_store_id!r} 不一致"
        )

    ptype = package["package_type"]
    cred = package["credential"]

    if client_state is None:
        client_state = {}
    if client_state:
        if client_state.get("store_id") != store_id:
            raise SyncError("跨店包被拒绝：客户端状态属于其他门店")
        if client_state.get("ledger_id") != package["ledger_id"] \
                or client_state.get("base_fingerprint") != package["base_fingerprint"]:
            raise SyncError("跨台账包被拒绝：客户端状态属于其他台账")

    mirror: Dict[str, Any] = dict(client_state.get("mirror", {}))

    if ptype == PACKAGE_FULL:
        mirror = {}
        for entry in package["entries"]:
            fkey = entry["fact_key"]
            mirror[fkey] = entry["value"]
    else:
        if not client_state or "last_credential" not in client_state:
            raise SyncError("差量包之前必须先应用同一门店/台账的完整首包")
        last_cred = client_state["last_credential"]
        if cred.get("parent_snapshot_id") != last_cred.get("snapshot_id"):
            raise SyncError(
                "差量接续凭证不匹配：父凭证 "
                f"{cred.get('parent_snapshot_id')!r} != 客户端上次凭证 "
                f"{last_cred.get('snapshot_id')!r}"
            )
        if package.get("parent_credential") is not None:
            verify_credential_seal(package["parent_credential"])
            if digest_json(package["parent_credential"]) != digest_json(last_cred):
                raise SyncError("包内父凭证与客户端上次应用的凭证不一致")
        # 截止时点不倒退（服务端已校验，客户端再守一道）。
        prev_at = parse_aware_datetime(client_state["as_of"])
        if parse_aware_datetime(package["as_of"]) < prev_at:
            raise SyncError("截止时点不得倒退")
        for entry in package["entries"]:
            fkey = entry["fact_key"]
            if entry["op"] == OP_UPSERT:
                mirror[fkey] = entry["value"]
            elif entry["op"] == OP_REMOVE:
                # 明确无价：与普通缺值区分，mirror 中保留 NOPRICE 标记。
                mirror[fkey] = NOPRICE
            else:
                raise SyncError(f"未知差量操作: {entry.get('op')!r}")

    return {
        "schema": SYNC_SCHEMA,
        "store_id": store_id,
        "ledger_id": package["ledger_id"],
        "base_fingerprint": package["base_fingerprint"],
        "as_of": package["as_of"],
        "revision": copy.deepcopy(package["revision"]),
        "mirror": mirror,
        "last_credential": copy.deepcopy(cred),
        "applied_packages": list(client_state.get("applied_packages", [])) + [
            {"package_type": ptype, "snapshot_id": cred["snapshot_id"],
             "as_of": package["as_of"], "entry_count": package["entry_count"]}
        ],
    }


def audit_application(ledger: Any, client_state: Dict[str, Any]) -> Dict[str, Any]:
    """审计：应用结果必须等于台账在该时点对该门店的完整价格状态。

    比对门店在底档中的**全部**事实（审计口径允许全量；同步发布口径不允许）。
    """
    store_id = client_state["store_id"]
    at_dt = parse_aware_datetime(client_state["as_of"])
    identity = ledger_identity(ledger)
    if identity["ledger_id"] != client_state["ledger_id"] \
            or identity["base_fingerprint"] != client_state["base_fingerprint"]:
        raise SyncError("审计失败：客户端状态属于其他台账")

    mirror = client_state.get("mirror", {})
    expected: Dict[str, Any] = {}
    for fact in _store_base_facts(ledger, store_id):
        value = _effective_value(ledger, fact, at_dt)
        if value is not None:
            expected[fact_key(fact)] = value

    mismatches: List[Dict[str, Any]] = []
    for fkey in sorted(set(expected) | set(mirror)):
        if expected.get(fkey) != mirror.get(fkey):
            mismatches.append({
                "fact_key": fkey,
                "expected": expected.get(fkey),
                "applied": mirror.get(fkey),
            })

    revision = current_revision(ledger, identity["base_fingerprint"])
    revision_matches = (
        revision["revision_no"] == client_state["revision"]["revision_no"]
        and revision["state_digest"] == client_state["revision"]["state_digest"]
    )
    return {
        "ok": not mismatches and revision_matches,
        "store_id": store_id,
        "as_of": client_state["as_of"],
        "fact_count": len(expected),
        "mismatches": mismatches,
        "revision_matches": revision_matches,
        "expected_revision": {
            "revision_no": revision["revision_no"],
            "state_digest": revision["state_digest"],
        },
    }
