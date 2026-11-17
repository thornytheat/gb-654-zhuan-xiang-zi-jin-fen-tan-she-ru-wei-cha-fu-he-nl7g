# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。

## M7 支付对账字段

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `pool_id` / `batch_id` / `instruction_id` | string | 否 | 资金池、分摊批次、待付指令的稳定业务键 |
| `state` | string | 否 | 指令状态：`PENDING` 待付 / `SUBMITTED` 已受理 / `PAID` 实付 / `FAILED` 失败 / `VOIDED` 撤回作废 / `RECOVERY_PENDING` 待追回 / `RECOVERED` 已追回 |
| `withdraw_flag` | integer | 否 | 已受理指令的撤回标记（1=撤回中，成功回执到达即转待追回） |
| `bank_state` | string | 否 | 模拟支付端请求状态：`ACCEPTED` / `SUCCESS` / `FAILED` |
| `available` | string | 否 | 可用资金 = 预算 - 占用 - 实付 + 已追回 |
| `occupied` | string | 否 | 占用资金：`PENDING` 与 `SUBMITTED` 指令金额合计 |
| `paid` | string | 否 | 累计实付（含待追回与已追回），撤回不抹除 |
| `recovery_pending` | string | 否 | 撤回后实付、尚未全额追回的金额，不释放给其他批次 |
| `recovered` | string | 否 | 已全额追回并释放回可用的累计金额 |
| `scenario` | string | 否 | 模拟支付场景：`ok` / `reject` / `timeout` / `interrupt` |

所有金额以分为最小单位（`Decimal` 量化到 `0.01`）；M7 仅写 `m7_*` 表，不修改 M2 资格评分。
