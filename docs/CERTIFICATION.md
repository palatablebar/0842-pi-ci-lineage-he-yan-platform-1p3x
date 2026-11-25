# 认证原料占比承诺

## 核算口径

对每个批次输出“最低可承诺认证占比”：

```text
认证投入量 = 直接认证量，或按父批次承诺占比 × 投料贡献量逐项继承
最坏认证损耗 = 损耗总量 - 已明确归属为非认证原料的损耗量
最低认证产出 = max(0, 认证投入量 - 最坏认证损耗)
可承诺占比 = 最低认证产出 / qty_out
```

未收到分拣记录时，未归属损耗全部按认证原料损失试算。因此平均投入占比即使达到约定线，只要保守下界低于约定线，也只会进入 `INSUFFICIENT_EVIDENCE / REVIEW`，不会误判为可保证达标。

同一图节点只在拓扑计算中出现一次；同一 `loss_id` 的共用损耗只在拓扑最早的属主批次扣减一次。拆分后再合回不会沿多条路径重复扣减。

晚到的分拣记录使用 `record_id`（或相同内容摘要）幂等去重。重复提交返回 `duplicate=true`，认证损耗/扣减量不会增加；登记后重新计算报告，并通过批次的 `ancestor_lot_ids` 给出受影响下游。

## 状态

| `status` | `verdict` | 含义 |
|---|---|---|
| `GUARANTEED` | `PASS` | 保守下界仍达到约定，可照常签发 |
| `INSUFFICIENT_EVIDENCE` | `REVIEW` | 当前不能保证，但补齐归属后可能达标，留待复核 |
| `NO_CERTIFICATION_EVIDENCE` | `REVIEW` | 缺少认证来源 |
| `BELOW_THRESHOLD` | `REJECT` | 证据完整且实际/保守结果仍低于约定 |
| `BLOCKED` | `REJECT` | 血缘、数量或记录冲突导致无法核算 |

## API

- `POST /api/certification/evaluate`：无状态试算。
- `POST /api/certification/sorting-records`：全局幂等登记分拣记录。
- `GET  /api/certification/workbench`：认证承诺工作台。
- `POST /api/runs/{run_id}/sorting-records`：登记晚到记录并重算该运行。
- `GET  /api/runs/{run_id}/certification`：读取该运行的认证承诺。

试算请求示例：

```json
{
  "threshold": 0.9,
  "lots": [
    {"lot_id": "R", "qty_in": 100, "qty_out": 90, "loss_qty": 10, "certified_qty": 90},
    {"lot_id": "M", "parent_lot": "A:45;B:45", "qty_in": 90, "qty_out": 90, "loss_qty": 0}
  ]
}
```

分拣记录示例：

```json
{
  "record_id": "SORT-001",
  "lot_id": "R",
  "loss_id": "loss:R",
  "uncertified_loss_qty": 10
}
```

工作台主表新增“认证原料承诺占比”面板，显示每批的可承诺占比、投入平均占比、状态和计算依据。
