# -*- coding: utf-8 -*-
"""
文件式离线签审 CLI。

用法（建议以隔离模式运行，避免回件目录中的同名模块干扰）：
    python3 -I cli.py --input 回件包.json --output-dir 输出目录 [--threshold 100]

输入 JSON 结构（轴标签 -> 稳定 ID 的映射在 manifests 内显式声明）：
{
  "threshold": 100,
  "ledger": [
    {"store_id": "S1", "product_id": "P1", "date_id": "D1", "value": 500},
    {"store_id": "S1", "product_id": "P2", "date_id": "D1", "value": "NOPRICE"}
  ],
  "manifests": {
    "store": {
      "axis_maps": {"store": {"S1": "S1"}, "product": {...}, "date": {...}},
      "covered_cells": [{"row": "S1|P1", "column": "D1",
                         "store_id": "S1", "product_id": "P1", "date_id": "D1"}]
    },
    "hq": { ... 行=商品|日期，列=门店 ... }
  },
  "returns": {
    "store": {"cells": [{"row": "S1|P1", "column": "D1",
                         "value": 480, "group": null}]},
    "hq":    {"cells": [{"row": "P1|D1", "column": "S1",
                         "value": "NOPRICE", "group": "GX"}]}
  }
}

取值约定：value 缺省/null=无建议；"NOPRICE"=签为无价；非负整数=签为确定分值。
无效回件时进程以退出码 2 结束，并把全部校验错误写入输出目录的 errors.txt。
本工具不修改输入文件与底档。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (  # noqa: E402
    HQ, NOPRICE, ROLES, STORE, Fact, Manifest, ReturnDoc, ValidationError,
    build_correction_order, render_text_report, write_order_json,
)


def _load_bundle(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _parse_ledger(raw_items) -> dict:
    ledger = {}
    for item in raw_items:
        fact: Fact = (item["store_id"], item["product_id"], item["date_id"])
        value = item["value"]
        if isinstance(value, str) and value == NOPRICE:
            ledger[fact] = NOPRICE
        else:
            ledger[fact] = value      # 合法性由引擎统一校验
    return ledger


def _parse_manifest(role: str, raw: dict) -> Manifest:
    return Manifest(role=role, axis_maps=raw["axis_maps"],
                    covered_cells=raw["covered_cells"])


def _parse_return(role: str, raw: dict) -> ReturnDoc:
    cells = []
    for cell in raw["cells"]:
        parsed = {"row": cell["row"], "column": cell["column"]}
        if "value" in cell:
            v = cell["value"]
            parsed["value"] = NOPRICE if v == NOPRICE else v
        if "group" in cell:
            parsed["group"] = cell["group"]
        cells.append(parsed)
    return ReturnDoc(role=role, cells=cells)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="促销价格清册勘误离线签审出单工具")
    ap.add_argument("--input", required=True, help="回件包 JSON 路径")
    ap.add_argument("--output-dir", required=True, help="更正单输出目录")
    ap.add_argument("--threshold", type=int, default=None,
                    help="大额改单门槛（分）；缺省读取输入 JSON 的 threshold")
    args = ap.parse_args(argv)

    bundle = _load_bundle(os.path.abspath(args.input))
    threshold = args.threshold if args.threshold is not None else bundle.get("threshold")
    if threshold is None:
        print("错误：未提供门槛（--threshold 或 JSON 内 threshold）", file=sys.stderr)
        return 2

    os.makedirs(args.output_dir, exist_ok=True)
    try:
        ledger = _parse_ledger(bundle["ledger"])
        manifests = {
            role: _parse_manifest(role, bundle["manifests"][role]) for role in ROLES
        }
        returns = {
            role: _parse_return(role, bundle["returns"][role]) for role in ROLES
        }
        order = build_correction_order(ledger, manifests, returns, threshold)
    except ValidationError as exc:
        err_path = os.path.join(args.output_dir, "errors.txt")
        with open(err_path, "w", encoding="utf-8") as fh:
            fh.write("回件无效，不出更正单。全部校验错误：\n")
            for e in exc.errors:
                fh.write(f"- {e}\n")
        print(f"回件无效，不出更正单；共 {len(exc.errors)} 条错误，详见 {err_path}",
              file=sys.stderr)
        for e in exc.errors:
            print(f"  - {e}", file=sys.stderr)
        return 2
    except (KeyError, TypeError) as exc:
        print(f"输入 JSON 结构错误: {exc!r}", file=sys.stderr)
        return 2

    json_path = os.path.join(args.output_dir, "correction_order.json")
    txt_path = os.path.join(args.output_dir, "correction_order.txt")
    write_order_json(order, json_path)
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(render_text_report(order) + "\n")
    print(f"更正单已生成: {json_path}")
    print(f"文字报告已生成: {txt_path}")
    print(f"统计: {json.dumps(order.stats, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
