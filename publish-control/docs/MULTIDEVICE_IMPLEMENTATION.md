# 多设备隔离实现报告（2026-10-28）

## 1. 结论与边界

已按“同一 customer 挂多台机器、一机一码、按设备隔离”的要求完成代码补丁、加法迁移、测试和离线证据。

本轮**没有**做以下动作：

- 没有部署 server.py / bot.py / 客户机 agent。
- 没有对生产 `control.db` 执行迁移或写入；只对 `/tmp` 副本执行过迁移。
- 没有重启 `publish-control.service` 或任何客户机进程。
- 没有连接、修改或重启客户机，也没有真实发布。
- 没有更新客户机 `MANIFEST.json` / `VERSION`，因此客户机文件不能直接视为可部署状态。

生产 `control.db` 在本轮前后 SHA256 均为：

```text
77d25b04cac189f1ad6f9f1f67643a26b7e5eb14c57956c6607d47a879b59b54
```

生产库当前仍是迁移前状态；只读校验会明确报缺少多设备列，这是预期结果，也是“未写生产库”的证据。

## 2. 数据模型

### 2.1 客户与设备

- `customers.active_device_id` 现在只表示“默认设备”，不再表示唯一设备。
- `customer_devices` 每行是一台机器：
  - `device_id`：设备唯一标识。
  - `customer_id`：归属客户，多台设备共用同一客户。
  - `token_hash`：该机器专属激活码的哈希；一机一码，唯一索引防止复用。
  - `binding_epoch`：设备凭据代次；换机/重激活时递增。
  - `status`：`active` / `isolated` / `retired`。
  - `label`、`machine_code`、`last_seen`、`created`、`updated`、`isolated_at`、`retired_at` 等运维字段。
- 新增 `device_events`，记录加机、隔离、退休、重激活、默认设备、换机、启用绑定等事件。

### 2.2 任务与素材

- `commands.device_id` / `binding_epoch`：发布任务、状态查询等定向到具体机器。
- `remote_ops.device_id` / `binding_epoch`：素材清单、读计划、改期等远端操作定向到具体机器。
- `commands.material_sha256`：发布命令对应的素材哈希。
- 新增 `device_material_claims`：
  - 主键 `(customer_id, material_sha256)`，即同一客户下同一素材只能有一个归属设备。
  - 字段包含 `device_id`、`command_id`、`material_id`、`status`（claimed/published/released）。
  - A 机占用后 B 机入队会返回 `409 MATERIAL_DEVICE_CONFLICT`。
  - 同一设备再次入队同一素材允许；不同设备即使文件名不同，只要 SHA 相同也拒绝。

### 2.3 素材身份提取

服务端 `_material_hashes` 现在接受：

- `material_sha256`
- `material_sha256s`
- `materials[].sha256` / `materials[].material_sha256`
- `plan_data.piece.sha256`
- `plan.piece.sha256`
- `piece.sha256`
- `item.plan_data.piece.sha256`
- `row.plan_data.piece.sha256`

因此直接调用 API、只带 `plan_data` 的发布命令也无法绕过跨机防重。

## 3. 隔离语义

### 3.1 吊销/隔离一台不影响其他机器

`isolate_device` / `retire_device` 只修改目标设备：

- 目标设备状态改为 `isolated` / `retired`。
- 目标设备上 `queued` / `running` 的 commands、remote_ops 改为 `needs_reconcile`，保留事实，不静默丢弃。
- 若目标是默认设备，自动从其他 active 设备中重新选择默认设备。
- 其他设备的 token、epoch、任务、默认状态不变。
- 只释放目标设备上**明确失败/取消**或没有命令的未发布素材占用；`queued`、`running`、`needs_reconcile`、`done` 关联的占用不会释放，避免把“可能已发布”的素材交给另一台机器重发。

### 3.2 A 机素材不能从 B 机发

三层限制：

1. 控制面：`commands` / `remote_ops` 带目标 `device_id`，绑定启用后只投递给目标设备。
2. 客户机：`agentpoll.py` / `remote_agent.py` 每次 poll/result 都带 `device_id`、`binding_epoch`、设备 token。
3. 客户机执行：`remote_ops.py` 的 materials/prepare/confirm 校验请求设备和计划设备；`confirm` 拒绝 `plan_device_mismatch` / `plan_epoch_mismatch`。

### 3.3 跨机防重

- 入队时由 `device_material_claims` 做原子校验；冲突返回 `409 MATERIAL_DEVICE_CONFLICT`。
- 机器人准备草稿前也做一次预检：
  - 同设备：按文件名判断，避免同机重复准备。
  - 跨设备：按 SHA256 判断；同名但 SHA 不同不阻塞，SHA 相同即使改名也阻塞。
- 已发布（`published`）的占用在换机后仍保留，新机不能再次发布同一素材；只有失败/取消的 `claimed` 占用会在隔离/换机时释放。

## 4. 加机 / 换机流程

> 加机、换机是运维操作，不路由到客户群自然语言，避免客户在群里自助越权。机器人只做“本会话绑定客户的素材/发布”，不提供“选客户”。

### 4.1 加机（新增一台设备）

1. 先备份控制面数据库，并执行加法迁移（见第 7 节）。
2. 调用：

```http
POST /api/customers/<customer_id>/devices
Content-Type: application/json
Authorization: Bearer <admin-token>

{
  "label": "B机",
  "machine_code": "customer-b-2026",
  "enforce_binding": false
}
```

- 返回的 `token` 是该机器的一次性激活码，只显示一次。
- `enforce_binding=false` 用于灰度：先登记，不立即让旧 customer token 失效。
- 已有设备不会被轮换、不会被停用。

3. 在 B 机设置：

```text
CUSTOMER_ID=<customer_id>
DEVICE_ID=<返回的 device_id>
DEVICE_BINDING_EPOCH=<返回的 binding_epoch>
DEVICE_TOKEN=<返回的 token>
CONTROL_URL=<控制面地址>
```

4. 验证 B 机 poll/materials/ping 正常，A 机仍正常。
5. 所有机器都登记并升级完成、且旧的 `queued` / `running` 命令已清空或核对后，再启用强绑定。启用后带设备身份的 poll 不再领取无设备归属的旧命令。

```http
POST /api/customers/<customer_id>/binding
Content-Type: application/json
Authorization: Bearer <admin-token>

{"enabled": true}
```

启用后：

- 每台机器必须使用自己的设备 token + `device_id` + `binding_epoch`。
- 旧 customer token 不再能代替设备身份执行任务。
- 设备离线时，新清单会回退到同客户其他在线设备；已准备的批次不会中途换机。

### 4.2 换机（旧机隔离、新机接管）

1. 先确认旧机没有 `queued` / `running` / `needs_reconcile` 的 commands 或 remote_ops。
2. 新机按“加机”流程登记为 active。
3. 调用：

```http
POST /api/customers/<customer_id>/devices/switch
Content-Type: application/json
Authorization: Bearer <admin-token>

{
  "old_device_id": "d_old...",
  "new_device_id": "d_new..."
}
```

服务端行为：

- 旧机状态改为 `isolated`。
- 新机 `binding_epoch` 递增并成为默认设备。
- `customers.customer_id` 不变，账本继续认同一客户。
- 旧机未发布且已失败/取消的素材占用释放；已发布占用保留。
- 如果旧机仍有未解决任务，换机直接拒绝，要求先人工核对；不会自动清理/重试。

### 4.3 换机后的账本/素材交接

- `stage2-ledger.json` 仍在各客户机本地，不自动复制。
- 控制面通过 `commands.device_id` 和签发的 receipt `device_id` 绑定到具体机器；机器人读取账本证据时只从该命令的目标设备读取。
- 历史账本、素材文件、计划文件如需从旧机转到新机，仍走既有离线交接/核验流程；本轮不自动搬运。
- 同一客户下已 `published` 的素材占用不会因为换机而释放；若业务要求新机重发，需要人工明确处理该占用，不能由机器人自动重试。

## 5. HTTP API 清单

设备管理（管理 token）：

```text
GET  /api/customers/<cid>/devices
GET  /api/customers/<cid>/devices/select?device_id=<did>&require_online=1
POST /api/customers/<cid>/devices
POST /api/customers/<cid>/devices/select
POST /api/customers/<cid>/devices/switch
POST /api/customers/<cid>/devices/<did>/isolate
POST /api/customers/<cid>/devices/<did>/retire
POST /api/customers/<cid>/devices/<did>/activate
POST /api/customers/<cid>/devices/<did>/default
POST /api/customers/<cid>/binding       # 只允许 enabled=true
```

任务/远端操作：

```text
POST /api/inbound
POST /api/customers/<cid>/command
POST /api/remote/exec
POST /api/agent/poll
POST /api/agent/remote-poll
POST /api/agent/result
POST /api/agent/remote-result
```

所有任务/远端写入都接受 `device_id` / `target_device_id`；绑定启用后按目标设备入队、领取、回写和校验。

## 6. 机器人行为

- 客户始终由 `chat_id` 解析，沿用 `lookup_customer(chat_id)`；没有新增“选客户”入口。
- 素材清单：
  - 强绑定客户：按默认/在线设备取该客户自己的设备清单，标题显示设备标签。
  - 新清单时，如果会话原设备离线，回退到同客户其他在线设备；已准备批次不换机。
  - 未迁移客户：走受控 legacy 通道，显示“旧机/兼容模式”；若客户已登记多台设备则拒绝，要求先启用绑定。
- 选条：
  - 快照记录 `device_id`；跨设备选择拒绝。
  - 同名不同设备不误判；同 SHA 跨设备阻塞。
- 准备/确认/改期/读计划：
  - 全部带目标设备。
  - 确认卡显示设备；contract digest 和 command id 纳入设备身份。
- 旧确认/旧重发流程：
  - 客户启用设备绑定后，旧 `approve` 卡、旧 `do_confirm`、旧 `do_republish` 直接拒绝，避免签发无设备回执或落到默认机器。
  - 多设备重发走 MT 重发路径，receipt 和发布命令都带原计划设备与 epoch。
- 发布回执：
  - `receipt_issue.issue_receipt` 支持 `device_id` / `binding_epoch`，并在签名前写入签名体。
  - 旧客户不写设备字段，保持兼容。

## 7. 迁移与发布顺序

迁移文件：

```text
migrations/20261028_multidevice.sql
scripts/migrate_multidevice.py
```

特点：

- 默认只读校验，不写库。
- 只有显式 `--apply` 才写；可重复执行。
- 删除旧的单活索引 `one_active_customer_device`（因为一个客户允许多台 active 设备）。
- `customer_devices` / `commands` 只加列；新增 `device_material_claims`、`device_events`；不删除、不改写既有行。
- 迁移前，设备相关写操作会 fail-closed，返回明确的 `multi-device migration required`；旧客户素材查询仍可走 legacy 路径。
- 多设备迁移后，旧的 `scripts/verify_device_binding.py` 单活校验不再适用（它会认为“多 active 设备”是错误）；多设备校验请使用 `scripts/migrate_multidevice.py` 的只读模式。

建议发布顺序：

1. 备份 `control.db`。
2. 在批准窗口执行 `python3 scripts/migrate_multidevice.py <db> --apply`，再跑只读校验。
3. 部署新 server.py。
4. 部署新 bot.py（必须基于真实生产 bot 重新基线，见第 10 节）。
5. 逐台登记设备，设置设备身份，验证 poll/materials/ping。
6. 全部设备就绪后调用 `/binding` 启用强绑定。
7. 观察旧 token 失效、设备定向投递、跨机冲突返回 409。
8. 再做一次人工发布验收；本轮没有做真实发布。

## 8. 测试证据

本轮测试只覆盖离线/模拟路径：

```text
python3 -m unittest discover -s tests -v
Ran 39 tests
OK
```

覆盖点：

- 双设备独立 active / 独立吊销；吊销 A 不影响 B。
- 同客户双设备 token 不同、默认设备在线优先。
- 跨设备同 SHA 入队返回 `MATERIAL_DEVICE_CONFLICT`；同设备复用允许。
- `plan_data.piece.sha256` / `plan.piece.sha256` 嵌套哈希不能绕过防重。
- 准备期间素材 SHA 变化（计划 SHA 与快照 SHA 不一致）时阻止确认。
- 隔离只释放失败/取消的未发布占用；unknown/running 占用保留。
- 换机隔离旧机、新机接管、customer_id 不变；旧机有在途任务时拒绝换机。
- remote_ops 只投递给目标设备。
- agent 身份透传；remote_ops confirm 拒绝另一台设备的计划。
- bot 不传 `customer_id`，只用 chat 绑定客户。
- bot 素材清单标记设备；跨设备选择拒绝；新清单离线回退。
- 同名不同设备不阻塞；同 SHA 跨设备阻塞；同设备同名阻塞；优先使用计划内完整 SHA，避免旧 12 位展示哈希漏判。
- 账本证据匹配拒绝“命令设备 A、回执设备 B”的错配。
- legacy 路径不带设备身份；已登记单设备必须带设备身份；多设备必须启用绑定。
- 启用绑定后旧确认/旧重发流程被拒绝；MT 重发必须带原设备身份并定向入队。
- 灰度阶段显式指定设备也会定向入队/领取；两台以上设备时，带设备身份的 poll 不再领取无设备归属的旧命令；单设备灰度仍可领取 legacy 无设备任务。
- 设备绑定状态无法核对时旧流程 fail-closed，不签发无设备回执。
- signed receipt 的 `device_id` / `binding_epoch` 在签名体内；非法设备 ID 拒绝。

## 9. 已知限制与风险

1. **bot 基线不是真实生产文件。** 仓库原先没有 `feishu_publish_bot/bot.py`；本补丁基于 `fix_evidence_p0_2_p1_1/bot.py`（与 `/tmp` 基线 SHA256 一致）。部署前必须把补丁重新落到真实生产 bot 上做三方合并，不能直接覆盖。
2. **`feishu_publish_bot/receipt_issue.py`、`ed25519_pure.py` 是随 bot 一起补入的。** `receipt_issue.py` 本次新增设备字段签名；`ed25519_pure.py` 未改。
3. **客户机代码未部署。** 本轮交付时用的是 `client_patch/agentpoll.py`、`remote_agent.py`、`remote_ops.py`；**1.2.222 起 `client_patch/` 已删除，客户机源码以 `client/agent/winport/` 为准**（升级走签名完整安装包或 `client/tools/ota_patch.py`，见 `KNOWN_ISSUES.md`）。仍需按客户机 MANIFEST 规则同步哈希并走维护窗口；本轮没有改 MANIFEST/VERSION。
4. **没有真实 Windows/飞书/网络联调。** 所有客户机、bot、remote op 证据均为 mock/离线测试；不能据此声称生产可用。
5. **stage2 ledger 顶层未新增设备列。** 本轮没有修改客户机 `agent/winport/publish_exec.py` / `stage2_ledger.py`。实际可区分方式：
   - `CONSUMED` 条目的 `publish_evidence.receipt` 内含签名后的 `device_id` / `binding_epoch`；
   - `RESERVED` / `SUBMIT_INTENT` / `UNKNOWN` 条目可通过 `receipt_id` 与控制面命令的 `device_id` 关联；
   - 机器人只从命令目标设备读取 ledger，并拒绝“命令设备 A、回执设备 B”的证据。
   若要求 ledger 每条顶层直接出现 `device_id`，需在真实 runtime 基线上另做补丁（`reserve(..., device_id=plan["device_id"], device_binding_epoch=...)`），并更新 MANIFEST。
6. **强绑定启用是破坏性切换。** 对已有客户，必须先用 `enforce_binding=false` 登记并升级所有机器，再调用 `/binding`；否则旧 agent/旧 customer token 会立即失效。启用前还要清空/核对无设备归属的 `queued` / `running` 命令，否则它们不会被设备定向 poll 领取。
7. **禁用绑定没有 API。** 为避免误操作，`/binding` 只允许 `enabled=true`。回滚需要代码回退或数据库备份/人工 SQL。
8. **客户群不提供加机/换机自助指令。** 这是运维 API 流程，避免客户越权。
9. **默认设备与在线设备不是同一概念。** 默认设备离线时，新清单会回退到同客户在线设备；已准备批次仍固定在原设备，不会自动换机。
10. **生产库未迁移。** 当前生产只读校验明确失败，说明多设备表/列不存在；新代码在设备写路径上会 fail-closed，旧客户素材路径保留兼容。
11. **启用绑定后旧确认卡/旧重发路径不可继续。** 这是有意隔离：旧路径无法证明目标机器，可能落到默认机或签出无设备回执。已有旧批次需要重新发「发布」生成设备感知批次。

## 10. 回滚原则

- 代码回退：按补丁反向应用到原基线；不要在生产库上直接 `DROP COLUMN`。
- 数据回退：优先恢复迁移前备份；加法表/列可保留，旧代码不会使用。
- 绑定回滚：本轮没有提供“关闭绑定”API，避免误操作导致设备身份失效；如必须回滚，需在维护窗口用备份或人工 SQL，并先核实所有在途任务。
- 不要自动清理 `needs_reconcile`、材料占用或 stage2 ledger；沿用既有唯一恢复口径，人工核实后再处理。

## 11. 证据文件

```text
fix_evidence_multidevice/compile.txt
fix_evidence_multidevice/unittest.txt
fix_evidence_multidevice/patch_replay.txt
fix_evidence_multidevice/migration_validate_before.txt
fix_evidence_multidevice/migration_apply.txt
fix_evidence_multidevice/migration_validate_after.txt
fix_evidence_multidevice/migration_reapply.txt
fix_evidence_multidevice/production_validate_readonly.txt
fix_evidence_multidevice/production_db_after.sha
fix_evidence_multidevice/multidevice_server.patch
fix_evidence_multidevice/multidevice_client.patch
fix_evidence_multidevice/multidevice_bot.patch
fix_evidence_multidevice/multidevice_migration_tests.patch
fix_evidence_multidevice/patch_check.json
fix_evidence_multidevice/MANIFEST.json
```
