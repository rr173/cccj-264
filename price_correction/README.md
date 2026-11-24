# 促销价格清册勘误 · 离线签审与出单工具

纯 Python 标准库实现，**只读底档，不修改任何输入文件**。建议以隔离模式运行：
`python3 -I ...`（避免回件目录中同名模块干扰）。

## 目录布局

```
price_correction/
  engine.py              # 核心引擎：校验、组连通（并查集）、裁定、出单、报告
  history_ledger.py      # 价格历史台账：落盘、乱序重算、时点查询、来源/冲突追溯
  history_cli.py         # 台账 CLI：init/deliver/query/history/conflicts/orders/export
  ledger.py              # 兼容别名（转出 PriceHistoryLedger）
  price_history.py       # 兼容别名（转出 PriceHistoryLedger）
  price_history_cli.py   # 兼容别名（调用 history_cli.main）
  run_history_demo.py    # 台账可复跑合成样例与断言
  test_history_ledger.py # 台账规则单元测试
  sample_data.py         # 双视图合成样例（同一底档，门店形/总部形两套清单与回件）
  run_demo.py            # 端到端演示 + 结果断言 + 无效回件变体
  export_sample.py       # 把合成样例导出为文件式回件包 JSON
  cli.py                 # 文件式 CLI：读回件包 JSON，出更正单或拒单错误
  test_engine.py         # 17 项规则级单元测试
samples/
  return_bundle.json          # 有效回件包（导出）
  return_bundle_invalid.json  # 无效回件包（范围外签值）
  price_history/              # 台账样例底档与乱序/重复/无效投递批
  samples/output/...          # 更正单 JSON、文字报告、拒单错误
  output/price_history/      # 台账 JSON、样例报告和查询来源 JSON
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

## 价格历史台账（新增）

`history_ledger.py` 从**底档起始快照**和**已批准更正单**建立可持久化、可重算的价格变化链。
台账文件是原子替换写入的 JSON；重新打开进程后直接继续使用。

### 台账输入 schema

底档：

```json
{
  "effective_from": "2026-01-01T00:00:00+08:00",
  "prices": [
    {"store_id": "S1", "product_id": "P100", "date_id": "2026-01-15", "value": 1000},
    {"store_id": "S1", "product_id": "P200", "date_id": "2026-01-15", "value": "NOPRICE"}
  ]
}
```

投递批（也允许顶层直接放单笔更正单，或用 `orders`/`correction_orders`/`items` 包裹）：

```json
{
  "orders": [
    {
      "order_id": "CO-001",
      "effective_at": "2026-01-05T10:00:00+08:00",
      "sequence": 0,
      "content": {
        "groups": [
          {
            "group_id": "G1",
            "facts": [
              {"store_id": "S1", "product_id": "P100", "date_id": "2026-01-15",
               "before": 1000, "after": 950}
            ]
          }
        ]
      }
    }
  ]
}
```

* `effective_at` 必须是带时区的 ISO 8601 时间；内部统一规范化为 UTC。
* `sequence` 是同一 `effective_at` 内的非负整数。`(effective_at, sequence)` 被不同单号占用时硬拒绝。
* `before` 是更正单写明的变更前价格；`after` 是通过后的价格。`before == after` 的零变动事实只参与组核对，不生成价格事件，也不替换原来源。
* `content` 也可以直接放现有 `cli.py`/`engine.py` 生成的 `correction_order.json`：台账只取 `passed_components`、`fact_trace` 和 `price_changes`，自动忽略 `rejected_components`。

### 一致性与冲突规则

1. 已接受单据先按 `(effective_at, sequence, order_id)` 排序，再从底档重放；因此文件/批次到达顺序不影响最终历史。
2. 迟到的更早生效单据入库后，所有后续组的核对结果会重算。例如后续单初次到达时可能因期望值仍是底档而被挡，早期单补齐后会自动变为生效。
3. 某个通过组中**任一事实**的 `before` 与该生效位置的台账应有价格不同，则该组所有变化均不生效；冲突记录包含冲突事实、单内原值 `original_value`/`before`、台账期望值 `expected_value`/`expected` 及期望来源。
4. 同一更正单中其他互不关联的通过组仍可入账。
5. 相同 `order_id` 且规范化后的时间、顺序、内容完全相同：幂等忽略，不重复记账。相同 `order_id` 但任一字段/内容不同：硬拒绝，不覆盖原单。
6. 单号冲突、无效/底档外事实、重复顺序位置、结构错误属于投递硬错误：整批在内存副本上校验，失败时不写台账文件，因此不会只写入部分记录。
7. 组内过期是业务冲突而非投递结构错误：问题组持久化为 `blocked`，同批独立组照常入账，事后可通过 `conflicts` 或查询结果中的 `blocked_attempts` 追踪。

### 查询状态

`query` 返回的 `status` 明确区分：

| status | 含义 |
| --- | --- |
| `OK` | 查有历史且当前为有价，`value` 为非负整数分 |
| `NO_PRICE` | 查有历史，但生效值是明确的 `NOPRICE` |
| `MISSING_HISTORY` | 底档和已生效更正中都没有该事实；这是缺少记录，不是无价 |
| `NO_HISTORY_BEFORE_BASE` | 查询时点早于底档 `effective_from`，此前尚无历史 |
| `BASE_NOT_INITIALIZED` | 台账尚未初始化 |

查询结果的 `source` 是直接来源（底档或某更正单/组），`sources`/`provenance_chain` 是从底档到直接来源的完整形成链；`blocked_attempts` 列出截至查询时点被冲突组挡下的变更。

### 台账 CLI

以下命令均在 `price_correction/` 目录下执行：

```bash
# 可复跑合成样例（重置样例台账，包含断言、报告和查询 JSON）
python3 -I run_history_demo.py

# 初始化
python3 -I history_cli.py init \
  --db ../samples/output/price_history/ledger.json \
  --base ../samples/price_history/base.json

# 投递乱序批次/重复批次
python3 -I history_cli.py deliver \
  --db ../samples/output/price_history/ledger.json \
  --input ../samples/price_history/batch_03_late_early.json

# 时点查询（文本来源链）
python3 -I history_cli.py query \
  --db ../samples/output/price_history/ledger.json \
  --store S1 --product P100 --date 2026-01-15 \
  --at '2026-01-12T12:00:00+08:00'

# 完整 JSON、事实全历史、冲突组、单据状态
python3 -I history_cli.py query --db LEDGER ... --json
python3 -I history_cli.py history --db LEDGER --store S1 --product P100 --date D
python3 -I history_cli.py conflicts --db LEDGER
python3 -I history_cli.py orders --db LEDGER

# 直接把现有签审引擎输出作为更正单内容入账
python3 -I history_cli.py deliver \
  --db LEDGER \
  --order ../samples/output/cli_ok/correction_order.json \
  --order-id CO-ENGINE-001 \
  --effective-at '2026-02-01T00:00:00+08:00' \
  --sequence 0

# 台账单元测试（14 项）
python3 -I test_history_ledger.py
```

完整样例报告位于
`../samples/output/price_history/history_demo_report.txt`，查询来源 JSON 位于
`../samples/output/price_history/queries/`。

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
