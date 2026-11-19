# 1.2.222 Windows 真机验证清单（待人工执行）

本清单只列需要在 Windows 11 真机确认的项目；Linux 侧已跑的测试不能替代这些结果。执行前先备份 `data\` 与程序目录，并在维护窗口内进行。

## A. 凭据 ACL

1. 完成一次激活，生成 `%LOCALAPPDATA%\HuolongAgent\publish-worker\control.json`。
2. 运行 `icacls "%LOCALAPPDATA%\HuolongAgent\publish-worker\control.json"`。
3. 确认输出里只有当前 Windows 用户与 `SYSTEM` 两条显式授权，且不再继承父目录 ACE。
4. 人为让 `icacls` 失败（例如临时只读目录），确认激活仍继续，只在日志出现 warning。

## B. hold 字段与分级

1. 触发一次 `publish_unknown`，检查 `data\run\publish_holds.json` 的该记录含 `source=agent`、`reason_code=publish_unknown`。
2. 手工写入一条不含 `source/reason_code` 的旧记录，重读 `publish-hold-status`，确认返回 `source=human`、`reason_code=""`。
3. 写入 `source=human` 的冻结，让机器人重试发布；确认机器人没有调用 `publish-hold-clear`，客户收到「发布被冻结，需人工确认后解除」。
4. 写入 `source=agent, reason_code=risk_control` 的冻结，确认同样拒绝自动解除。

## C. 限次与 dry-run 门禁

1. 同一账号 24 小时内构造 3 条 `result=cleared` 的 `run\auto_unblock.log` 记录，再触发第 4 次自动解冻；确认停止并提示「24 小时内已自动解冻 3 次」。
2. 构造 `source=agent, reason_code=lease_timeout` 的冻结，令 dry-run 失败（例如临时改坏计划标题）；确认冻结被解除但真发布没有执行，审计 `result=dry_run_failed`。
3. 恢复合法计划后再重试，确认 dry-run 通过后才真发布，审计 `result=cleared`。

## D. 旧副本与脚本

1. 确认客户机程序目录与数据目录都不存在 `client_patch` 目录。
2. 在仓库根运行 `python publish-control/scripts/deploy_client_materials.py --dry-run`，确认 `source` 指向 `client/agent/winport/` 且 `baseline_match=false`。
3. 尝试 `--apply`（仅在隔离测试机），确认报 `source baseline drift` 并拒绝，不会写客户机文件。

## E. 回归与界面

1. Windows 11 真机运行：
   - `python -m pytest -q -p no:cacheprovider publish-control/tests publish-control/client/tests`
   - 正序与反序各一次。
2. 运行 `python publish-control/client/tools/static_verify.py`，确认 127/127。
3. 运行 `python publish-control/client/tools/e2e_selfverify.py --out <报告路径>`，确认 19/19。
4. 打开面板首页与页脚，确认文案为「1.2.221 已在 Windows 11 真机跑通 380 tests、e2e 19/19」，不再出现「尚未验收」。

## F. 发布链路（真账号）

1. 用真实账号跑一次 fill（不点发布），确认证据、回读与 hold 状态正常。
2. 是否真发一条普通发布/星图任务，由业务方单独确认；本清单不预设通过。
