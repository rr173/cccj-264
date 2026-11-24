# 促销价格清册勘误 · 离线签审与出单工具

纯 Python 标准库实现，**只读底档，不修改任何输入文件**。建议以隔离模式运行：
`python3 -I ...`（避免回件目录中同名模块干扰）。

## 目录布局

```
price_correction/
  engine.py         # 核心引擎：校验、组连通（并查集）、裁定、出单、报告
  sample_data.py    # 双视图合成样例（同一底档，门店形/总部形两套清单与回件）
  run_demo.py       # 端到端演示 + 结果断言 + 无效回件变体
  export_sample.py  # 把合成样例导出为文件式回件包 JSON
  cli.py            # 文件式 CLI：读回件包 JSON，出更正单或拒单错误
  test_engine.py    # 17 项规则级单元测试
samples/
  return_bundle.json          # 有效回件包（导出）
  return_bundle_invalid.json  # 无效回件包（范围外签值）
  samples/output/...          # 更正单 JSON、文字报告、拒单错误
```

## 业务规则到实现的对应

| 规则 | 实现位置（engine.py） |
| --- | --- |
| 价格身份 = 门店/商品/日期三稳定 ID | `Fact` 类型；`resolve_fact` 按轴标签映射解析 |
| 门店表行=门店\|商品、列=日期；总部表行=商品\|日期、列=门店 | `resolve_fact` 按 `STORE`/`HQ` 两种展开方式解析 |
| 回件可重排行列 | 全程按标签解析，不依赖行列位置 |
| 两个单元不得映到同一事实 | `validate_manifest` / `validate_return` 中的 `seen_facts` 检查 |
| 坏标签、漏掉发放单元、范围外签值 => 无效回件，不出单 | `validate_return`，错误全部收集后抛 `ValidationError` |
| 值三态：无建议 / 签为无价 `NOPRICE` / 非负整数分 | `_is_legal_value`；`value=None` 即无建议 |
| 签回底档值也是真实签字 | 无建议与签字严格区分；零变动签字进 `fact_trace` 但不进价差 |
| 每方每项最多一个签值 | 事实去重校验保证 |
| 双方值不一致 => 冲突，不通过 | `FactDecision.conflict`，代码 `CONFLICT` |
| 大额（有价间差额 > 门槛）或有价/无价互换须两方同签同值 | `build_correction_order` 的 `needs_dual` 判定（差额**等于**门槛算小额） |
| 其余一方签即可；未发放方不能冒作已签 | 缺签原因区分“已发放未签”与“未收到该项” |
| 成组更正；一个签字最多归一个组；未归组单签各自成组 | 组校验 + `DSU` 并查集 |
| 跨表组经共享事实连通，可多事实链式牵连 | 同角色同名组联合 + 共享事实自然连通 |
| 关联组内任一事实失败 => 整组拒发；独立组不受影响 | 组件级裁定循环 |
| 只交付通过组中确实变价的事实，含变前变后与实际签字坐标 | `PriceChange` / `price_changes` |
| 零变动通过组可查审查状态但不列价差 | `passed_components` 标 `has_price_change=false`；`fact_trace` 可查 |
| 拒发组可从任一连带项追查失败项、原因、两份回件坐标 | `RejectedComponent`（`failure_facts`/`reasons`/`links`）+ `fact_trace` |
| 每条价差只列一次 | 去重集合 `seen_change` + `_self_check` 断言 |

## 输入 JSON schema（cli.py）

```json
{
  "threshold": 100,
  "ledger": [
    {"store_id": "S1", "product_id": "P1", "date_id": "D1", "value": 500},
    {"store_id": "S1", "product_id": "P2", "date_id": "D1", "value": "NOPRICE"}
  ],
  "manifests": {
    "store": {
      "axis_maps": {"store": {"标签": "S1"}, "product": {"标签": "P1"}, "date": {"标签": "D1"}},
      "covered_cells": [{"row": "S1|P1", "column": "D1",
                         "store_id": "S1", "product_id": "P1", "date_id": "D1"}]
    },
    "hq": {
      "axis_maps": { "...": "..." },
      "covered_cells": [{"row": "P1|D1", "column": "S1",
                         "store_id": "S1", "product_id": "P1", "date_id": "D1"}]
    }
  },
  "returns": {
    "store": {"cells": [{"row": "S1|P1", "column": "D1",
                         "value": 480, "group": null}]},
    "hq":    {"cells": [{"row": "P1|D1", "column": "S1",
                         "value": "NOPRICE", "group": "GX"}]}
  }
}
```

* `value` 缺省或 `null` = 无建议；`"NOPRICE"` = 签为无价；非负整数 = 确定分值。
* `group` 为组名字符串；若给成多元素列表则触发“一个签字归多个组”的无效错误。
* 发放范围内的单元无论有无建议都必须出现在回件中，否则按漏交拒绝。
* 范围外单元允许留空（无建议），范围外签值则整单拒绝。

## 运行

```bash
# 单元测试（17 项）
python3 -I test_engine.py

# 端到端演示（双视图渲染、出单、6 类无效回件拒绝、断言自检）
python3 -I run_demo.py

# 导出文件式样例
python3 -I export_sample.py

# 文件式出单 / 拒单（退出码 0=出单，2=无效回件）
python3 -I cli.py --input ../samples/return_bundle.json \
                  --output-dir ../samples/output/cli_ok
python3 -I cli.py --input ../samples/return_bundle_invalid.json \
                  --output-dir ../samples/output/cli_bad
```

## 合成样例覆盖的情形

12 个事实、10 个关联组：6 条价差（小额单签、大额双签、互换双签、跨表组接合、
组内多事实连带通过、另一方未发放的小额单签）、2 个零变动通过组（签回分值、
签回无价）、3 个拒发组（冲突、未发放方缺双签、组内一项缺签连累同组事实）。
无效变体覆盖坏标签、事实重复、漏交、范围外签值、一签多组、非法签值。
