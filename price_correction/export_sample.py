# -*- coding: utf-8 -*-
"""把合成双视图样例导出为文件式回件包 JSON，供 cli.py 离线读取。"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sample_data as sd  # noqa: E402


def manifest_to_dict(m) -> dict:
    return {"axis_maps": m.axis_maps, "covered_cells": m.covered_cells}


def main() -> int:
    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples"
    )
    os.makedirs(out_dir, exist_ok=True)

    ledger = sd.build_ledger()
    bundle = {
        "threshold": sd.THRESHOLD,
        "ledger": [
            {"store_id": s, "product_id": p, "date_id": d, "value": v}
            for (s, p, d), v in sorted(ledger.items())
        ],
        "manifests": {
            "store": manifest_to_dict(sd.build_store_manifest()),
            "hq": manifest_to_dict(sd.build_hq_manifest()),
        },
        "returns": {
            "store": {"cells": sd.build_store_return().cells},
            "hq": {"cells": sd.build_hq_return().cells},
        },
    }
    path = os.path.join(out_dir, "return_bundle.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)
    print(path)

    # 同时导出一个无效包（范围外签值），用于演示拒绝路径
    sm, sr, hm, hr = sd.build_invalid_variants()["suggestion_outside_scope"]
    bad_bundle = {
        "threshold": sd.THRESHOLD,
        "ledger": [
            {"store_id": s, "product_id": p, "date_id": d, "value": v}
            for (s, p, d), v in sorted(ledger.items())
        ],
        "manifests": {
            "store": manifest_to_dict(sm),
            "hq": manifest_to_dict(hm),
        },
        "returns": {
            "store": {"cells": sr.cells},
            "hq": {"cells": hr.cells},
        },
    }
    bad_path = os.path.join(out_dir, "return_bundle_invalid.json")
    with open(bad_path, "w", encoding="utf-8") as fh:
        json.dump(bad_bundle, fh, ensure_ascii=False, indent=2)
    print(bad_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
