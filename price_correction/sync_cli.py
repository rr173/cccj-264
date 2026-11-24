# -*- coding: utf-8 -*-
"""
门店价格同步 CLI：完整首包、续传差量、门店侧应用与完整性审计。

示例：
  # 服务端：发布完整首包（包内含凭证，单文件原子写入）
  python3 -I sync_cli.py full --db LEDGER --store S1 \
      --as-of '2026-01-11T12:00:00+08:00' --output-dir DIR

  # 服务端：凭上次凭证续传差量（--credential 可指凭证 JSON 或上次的包文件）
  python3 -I sync_cli.py delta --db LEDGER --store S1 \
      --as-of '2026-01-13T12:00:00+08:00' \
      --credential DIR/sync_full_*.json --output-dir DIR

  # 门店侧：应用包，维护本地镜像状态
  python3 -I sync_cli.py apply --package PKG.json --state DIR/client_state.json

  # 审计：应用结果必须等于台账该时点完整价格状态
  python3 -I sync_cli.py audit --db LEDGER --state DIR/client_state.json

退出码：0 成功；2 凭证/包/输入错误（拒绝，不产生结果）；3 台账读写失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from history_ledger import (  # noqa: E402
    LedgerInputError, LedgerPersistenceError, PriceHistoryLedger,
)
from sync import (  # noqa: E402
    CREDENTIAL_KIND, PACKAGE_DELTA, PACKAGE_FULL, StoreSyncService,
    SyncError, apply_package, audit_application,
)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _load_credential(path: str) -> Dict[str, Any]:
    raw = _load_json(os.path.abspath(path))
    if isinstance(raw, dict) and raw.get("kind") == CREDENTIAL_KIND:
        return raw
    if isinstance(raw, dict) and "credential" in raw:
        # 允许直接指向上次发布的包文件。
        return raw["credential"]
    raise SyncError(f"{path} 既不是快照凭证，也不是含凭证的同步包")


def _service(args: argparse.Namespace) -> StoreSyncService:
    ledger = PriceHistoryLedger(os.path.abspath(args.db))
    return StoreSyncService(ledger)


def _write_package(output_dir: Path, package: Dict[str, Any]) -> Path:
    # 包与凭证在同一个 JSON 内一次原子发布：磁盘上不会出现只有凭证或只有包的
    # 半成品；失败时输出目录里没有任何新文件可被误认为完整结果。
    cred = package["credential"]
    name = (
        f"sync_{package['package_type']}_{package['store_id']}_"
        f"{cred['snapshot_id']}.json"
    )
    out_path = output_dir / name
    _atomic_write_json(out_path, package)
    return out_path


def cmd_full(args: argparse.Namespace) -> int:
    service = _service(args)
    package = service.publish_full(args.store, args.as_of)
    out_path = _write_package(Path(args.output_dir), package)
    _print_json({"written": str(out_path), "summary": _summary(package)})
    return 0


def cmd_delta(args: argparse.Namespace) -> int:
    credential = _load_credential(args.credential)
    service = _service(args)
    package = service.publish_delta(args.store, args.as_of, credential)
    out_path = _write_package(Path(args.output_dir), package)
    _print_json({"written": str(out_path), "summary": _summary(package)})
    return 0


def _summary(package: Dict[str, Any]) -> Dict[str, Any]:
    entries = package["entries"]
    return {
        "package_type": package["package_type"],
        "store_id": package["store_id"],
        "as_of": package["as_of"],
        "revision": package["revision"],
        "entry_count": package["entry_count"],
        "upsert_count": sum(1 for e in entries if e.get("op") == "upsert")
        if package["package_type"] == PACKAGE_DELTA else len(entries),
        "remove_count": sum(1 for e in entries if e.get("op") == "remove"),
        "snapshot_id": package["credential"]["snapshot_id"],
        "parent_snapshot_id": package["credential"].get("parent_snapshot_id"),
    }


def cmd_apply(args: argparse.Namespace) -> int:
    package = _load_json(os.path.abspath(args.package))
    state_path = Path(os.path.abspath(args.state))
    client_state = _load_json(str(state_path)) if state_path.exists() else None
    new_state = apply_package(package, client_state,
                              expected_store_id=args.expected_store)
    _atomic_write_json(state_path, new_state)
    _print_json({
        "state_path": str(state_path),
        "store_id": new_state["store_id"],
        "as_of": new_state["as_of"],
        "mirror_fact_count": len(new_state["mirror"]),
        "applied_packages": new_state["applied_packages"],
    })
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    ledger = PriceHistoryLedger(os.path.abspath(args.db))
    client_state = _load_json(os.path.abspath(args.state))
    report = audit_application(ledger, client_state)
    _print_json(report)
    return 0 if report["ok"] else 4


def cmd_list(args: argparse.Namespace) -> int:
    service = _service(args)
    _print_json({
        "store_id": args.store,
        "snapshots": service.list_snapshots(args.store),
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", required=True, help="价格历史台账 JSON 文件")

    ap = argparse.ArgumentParser(
        description="门店价格同步：完整首包、续传差量、凭证校验与应用审计"
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_full = sub.add_parser("full", parents=[common],
                            help="发布门店完整首包（含首包凭证）")
    p_full.add_argument("--store", required=True)
    p_full.add_argument("--as-of", required=True, help="截止时点 ISO 8601")
    p_full.add_argument("--output-dir", required=True)
    p_full.set_defaults(func=cmd_full)

    p_delta = sub.add_parser("delta", parents=[common],
                             help="凭上次快照凭证发布续传差量")
    p_delta.add_argument("--store", required=True)
    p_delta.add_argument("--as-of", required=True, help="截止时点，不得早于凭证时点")
    p_delta.add_argument("--credential", required=True,
                         help="上次凭证 JSON，或含凭证的上次同步包文件")
    p_delta.add_argument("--output-dir", required=True)
    p_delta.set_defaults(func=cmd_delta)

    p_apply = sub.add_parser("apply", help="门店侧应用同步包，更新本地镜像状态")
    p_apply.add_argument("--package", required=True)
    p_apply.add_argument("--state", required=True, help="客户端镜像状态 JSON（自动创建）")
    p_apply.add_argument("--expected-store", default=None,
                         help="可选：强制包门店必须等于该门店")
    p_apply.set_defaults(func=cmd_apply)

    p_audit = sub.add_parser("audit", parents=[common],
                             help="审计客户端镜像是否等于台账该时点完整状态")
    p_audit.add_argument("--state", required=True)
    p_audit.set_defaults(func=cmd_audit)

    p_list = sub.add_parser("list", parents=[common],
                            help="列出某门店已发布的全部快照凭证")
    p_list.add_argument("--store", required=True)
    p_list.set_defaults(func=cmd_list)
    return ap


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SyncError as exc:
        print("同步请求被拒绝：", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err}", file=sys.stderr)
        return 2
    except LedgerInputError as exc:
        print("输入无效：", file=sys.stderr)
        for err in exc.errors:
            print(f"  - {err}", file=sys.stderr)
        return 2
    except LedgerPersistenceError as exc:
        print(f"台账读写失败，未发布任何结果：{exc}", file=sys.stderr)
        return 3
    except (KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
        print(f"文件/JSON 错误: {exc!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
