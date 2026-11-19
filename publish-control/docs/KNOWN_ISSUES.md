# 发布助手（publish-control / 火龙发布机器人）问题清单

更新：2026-11-19（对应 1.2.222；1.2.219 交付时首版）。只列能复现、能指到文件的问题；每条给证据。

## 一、已修（1.2.216 → 1.2.219）

| 版本 | 问题 | 修法 |
|---|---|---|
| 1.2.217 | 发布浏览器卡死后被当成"标签复用"继续往下走，实际页面没起来 | `launch_environment` 加真实 CDP 握手判定（`alive_probe` 两次探测 + 退避），握手不通就杀进程重启；`reason` 不再报 `TAB_REUSED` |
| 1.2.218 | OTA 补丁不完整 → 客户「升级→起不来→回滚」死循环（1.2.188–1.2.191 报 `ModuleNotFoundError: winport.process_identity`） | 补丁补齐 1.2.188→目标版本所有改动文件（23→34 个），`min_version` 改为 1.2.192 重签上线；新增 `tools/patch_coverage_check.py` 与 `tools/ota_patch.py check/gate` |
| 1.2.219 | 非管理员客户机上旧计划任务删不掉 → 安装中断 | `bootstrap_py.py` 改 `schtasks /Change` 复用旧任务并直接拉起，接管是否成功交给 handover 判定（任务不存在但 HKCU Run 已注册 → WARNING 继续；旧任务确实接管失败 → 仍 FATAL） |
| 1.2.220 | 1.2.219 的回退路径留下"活的旧任务"：删不掉也改不了的 ONLOGON 任务仍指向旧解压目录（程序目录随版本变）→ 旧目录被清掉的客户每次登录弹"找不到脚本文件"；没清掉的重启后旧版抢先启动 | 读 `schtasks /Query /XML` 拿任务目标（不受系统语言影响），把那个旧 `run-agent-task.vbs` 原地换成转发桩：读 `data\run\program-path.txt` 启动当前版本（`//B` 无弹窗）；桩所在目录没有 bootstrap/MANIFEST，以后的清理不会删它，以后每次升级自动跟随。Windows 真机验证：真实任务（中文+空格路径）目标解析正确、桩确实拉起当前版本、无 program-path 时静默退出 |
| 1.2.220 | Windows 上 11 条测试红（CI 报 7 条，真机 11 条） | 6 条 0600：Windows 无此权限位，改为按平台断言；1 条卡死自愈：注入存活/CDP 探测，Windows 分支真正被覆盖；2 条试用：测试读写了开发机真实 HKCU 注册表（装过正式授权的机器有 tombstone → 全红，还会污染开发机）→ conftest 在 Windows 上隔离注册表（产品代码不变，不能变成客户侧重置试用开关）；2 条就绪 PID：测试经 venv 重定向器多套了一层，产品只有一层 → 测试改用真实解释器起子进程。真机 Windows 11（中文系统、py3.14）：336 passed |
| 1.2.220 | e2e 在 Windows 上 14/19 | 子进程按 UTF-8 输出、e2e 按 GBK 解码 → 中文全乱码（F4 关键字匹配失败、M8 日报路径乱码"找不到文件"）→ 改 UTF-8 解码；M1/F2/C1 改为适配真机（已授权机器无试用是正确行为；Windows 用 ACL 拒写模拟杀软/IT 锁；有 Chrome 时报出路径）。真机 19/19；windows CI 改为必过并加跑 e2e |
| 1.2.221 | 远程执行通道默认全开（原 P0-1）：relay 与控制面两条通道都能跑任意命令、读写任意文件、截桌面；机器人 12 个日常功能靠往客户机发 `python -c` 脚本实现 | 新增 `remote_policy`：默认只放行本产品 CLI 白名单子命令（工作目录强制为程序目录，禁 `--program`，`--data` 只能在数据区内）和数据区/程序目录内的读/列目录；其余（任意 argv、`-c`、cmd/powershell、写文件、数据区外路径、截屏）需客户在面板开"远程协助"（默认 4 小时、最长 24 小时、到期自动关），每次放行/拒绝写 `logs/remote-audit.jsonl`。新增 `named_ops`（inbox-scan / plan-make / plan-set-time / plan-reschedule / ledger-unblock / export-files / agent-health），两条通道都加 `named` 操作在进程内执行；机器人按 /info 的 `named-ops-1` 能力切换，旧客户端走旧脚本。顺带修掉：PLAN_MAKER 调用只在机器人里定义的 `title_from_file`，在客户机上必报 NameError；_MT_RESCHED 自己复刻 content_hash 并猜桌面目录、往 `C:\ProgramData` 写 py/bat 再 `cmd /c` 执行 → 改为调用客户端自己的 `material_fingerprint` 与本机程序目录。Windows 真机：380 passed、e2e 19/19、真实 HTTP 往返验证（任意代码/PowerShell/截屏/读用户目录 403 且提示开远程协助；具名操作与白名单 CLI 正常；开启后放行、关闭后立即拒绝） |

## 一之二、1.2.222（发布冻结分级解除）

| 项 | 实现 |
|---|---|
| holds 字段 | 新增 `source`（agent/bot/human）与 `reason_code`；旧记录缺字段时按 `source=human`、`reason_code=""` 处理 |
| 机器人自动解冻 | 仅允许 `source=agent` 且 `reason_code ∈ {lease_timeout, publish_unknown, channel_unbound}`；客户端 `publish-hold-clear --actor bot` 再做一次同样的策略校验 |
| 拒绝提示 | 人工冻结、风控冻结、账号处罚类冻结不调用 clear；客户收到「发布被冻结，需人工确认后解除」 |
| 24 小时限次 | 同一 channel/账号最多自动解冻 3 次；超限停止并提示人工确认 |
| 解冻后校验 | 自动解冻后必须跑 `dry-run --no-write`，`ok=true` 且有 `confirm_token` 才继续真发布；校验失败则本次不发布 |
| 审计 | `run/auto_unblock.log` 记录 source/reason_code/count_24h/result；客户端另写 `run/publish_hold_audit.jsonl`，`resolved` 记录 actor/source/reason_code |
| 旧副本 | 删除 `publish-control/client_patch/`；`deploy_client_materials.py` 改从 `client/agent/winport/` 取件，源哈希与 `fix_evidence_client/baseline` 不一致时 apply 直接拒绝 |
| Windows 凭据 | `control.json` 写入后 `icacls /reset` + `/inheritance:r /grant:r <当前用户>:(F) SYSTEM:(F)`；失败只告警不阻断 |
| 面板文案 | 首页改为「1.2.221 已在 Windows 11 真机跑通 380 tests、e2e 19/19」 |

**上线顺序（不变）：先部署机器人，再推 1.2.222 客户端。** 新机器人兼容新旧客户端；反过来先升客户端，旧机器人的旧调用会被远程策略拒绝。运维脚本仍需客户先开「远程协助」，此约束不变。

**旧部署脚本的替代路径：** `deploy_client_materials.py --apply` 只适用于与 `fix_evidence_client/baseline` 完全一致的历史基线；1.2.222 的 `client/agent/winport/` 源码已相对该基线漂移，apply 会明确拒绝。客户机代码更新请走签名的完整安装包，或 `client/tools/ota_patch.py check/gate` 生成/校验的 OTA 补丁；不要再手工推送 `client_patch` 旧副本。

## 二、1.2.221 遗留项处置

### ~~P0-1 远程执行通道默认开启~~ —— 已关闭（1.2.221）
- `remote_policy` 默认只放行产品 CLI 白名单与数据区/程序目录内读列；其余需客户在面板开「远程协助」，每次放行/拒绝写 `logs/remote-audit.jsonl`。
- 遗留的远程调用改走 `named_ops` 或白名单 CLI；旧 `python -c` 路径仅对新客户端拒绝。

### client_patch 旧副本与自动解冻 —— 已关闭（1.2.222）
- `publish-control/client_patch/` 已删除；`deploy_client_materials.py` 改为读取 `client/agent/winport/`，并在基线漂移时拒绝 apply。
- 机器人自动解冻改为 source/reason 分级 + 24 小时 3 次上限 + 解冻后 dry-run 门禁 + 审计。

### P2-2（原 P0-2）Windows 凭据文件没有 0600 等价保护 —— 已关闭（1.2.222）
- `activation.py` 在 Windows 写入 `control.json` 后调用 `icacls`：清除继承、只授当前用户 + SYSTEM；失败只写 warning，不阻断激活。
- POSIX 平台仍保留 0600；Windows 侧需真机确认 ACL 实际输出（见交付清单的 Windows 验证项）。

### P1-3 「卡死自愈」Windows 真实场景 —— 保留待办（附原因）
- 1.2.220 的存活/CDP 探测已有单测，Windows 11 真机全量测试通过；但「真机冻结一个 Chrome 实例 → 触发发布 → 自动重启且页面真的打开」仍需人工按清单执行。

### P1-4 没有源码的版本无法证明 OTA 可用 —— 保留待办（附原因）
- 历史版本源码缺失不可补造；这些客户改发完整安装包。以后出包只认带源码的 tag（`release_guard.py` 已卡）。

### P1-5 旧版 supervisor / 接管逻辑不在升级范围 —— 保留待办（附原因）
- 已发布 tag 内的 supervision 代码不会被补丁替换；1.2.188–1.2.191 客户按完整安装包处理，不指望 OTA 修自启。

### P2-6 Windows CI 必过检查 —— 保留待办（附原因）
- 代码与 workflow 已加 Windows pytest + e2e；仍需仓库管理员在 Settings → Branches 把对应 Windows job 加进 required checks，代码无法自证仓库设置已完成。

### ~~P2-7 客户面板自述「Windows 真机与真实账号尚未验收」~~ —— 已关闭（1.2.222）
- 首页与页脚改为「1.2.221 已在 Windows 11 真机跑通 380 tests、e2e 19/19」；真实账号的发布结果仍以每次实际操作回执为准。

## 三、复现命令

```bash
# 仓库根目录，一个会话跑全部（Linux 与 Windows 同一条命令）
python -m pytest -q -p no:cacheprovider publish-control/tests publish-control/client/tests
cd publish-control/client
python tools/static_verify.py                 # 127/127
python tools/e2e_selfverify.py --out r.md     # 19/19（Linux 需非 root；Windows 真机亦 19/19）
python tools/ota_patch.py gate --simulate     # PASS + 上面那条 WARN
```

> OTA gate 依赖真实 git 历史（`git show <release>:publish-control/client/VERSION`）。
> 在没有 `.git` 对象或 worktree 元数据的导出目录里会直接以 `ERROR: git rev-parse --show-toplevel` 退出 2；这不是代码失败，需在恢复仓库历史后重跑。
