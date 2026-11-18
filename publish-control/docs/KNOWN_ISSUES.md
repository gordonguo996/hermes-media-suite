# 发布助手（publish-control / 火龙发布机器人）问题清单

更新：2026-11-18（对应 1.2.220；1.2.219 交付时首版）。只列能复现、能指到文件的问题；每条给证据。

## 一、已修（1.2.216 → 1.2.219）

| 版本 | 问题 | 修法 |
|---|---|---|
| 1.2.217 | 发布浏览器卡死后被当成"标签复用"继续往下走，实际页面没起来 | `launch_environment` 加真实 CDP 握手判定（`alive_probe` 两次探测 + 退避），握手不通就杀进程重启；`reason` 不再报 `TAB_REUSED` |
| 1.2.218 | OTA 补丁不完整 → 客户「升级→起不来→回滚」死循环（1.2.188–1.2.191 报 `ModuleNotFoundError: winport.process_identity`） | 补丁补齐 1.2.188→目标版本所有改动文件（23→34 个），`min_version` 改为 1.2.192 重签上线；新增 `tools/patch_coverage_check.py` 与 `tools/ota_patch.py check/gate` |
| 1.2.219 | 非管理员客户机上旧计划任务删不掉 → 安装中断 | `bootstrap_py.py` 改 `schtasks /Change` 复用旧任务并直接拉起，接管是否成功交给 handover 判定（任务不存在但 HKCU Run 已注册 → WARNING 继续；旧任务确实接管失败 → 仍 FATAL） |
| 1.2.220 | 1.2.219 的回退路径留下"活的旧任务"：删不掉也改不了的 ONLOGON 任务仍指向旧解压目录（程序目录随版本变）→ 旧目录被清掉的客户每次登录弹"找不到脚本文件"；没清掉的重启后旧版抢先启动 | 读 `schtasks /Query /XML` 拿任务目标（不受系统语言影响），把那个旧 `run-agent-task.vbs` 原地换成转发桩：读 `data\run\program-path.txt` 启动当前版本（`//B` 无弹窗）；桩所在目录没有 bootstrap/MANIFEST，以后的清理不会删它，以后每次升级自动跟随。Windows 真机验证：真实任务（中文+空格路径）目标解析正确、桩确实拉起当前版本、无 program-path 时静默退出 |
| 1.2.220 | Windows 上 11 条测试红（CI 报 7 条，真机 11 条） | 6 条 0600：Windows 无此权限位，改为按平台断言；1 条卡死自愈：注入存活/CDP 探测，Windows 分支真正被覆盖；2 条试用：测试读写了开发机真实 HKCU 注册表（装过正式授权的机器有 tombstone → 全红，还会污染开发机）→ conftest 在 Windows 上隔离注册表（产品代码不变，不能变成客户侧重置试用开关）；2 条就绪 PID：测试经 venv 重定向器多套了一层，产品只有一层 → 测试改用真实解释器起子进程。真机 Windows 11（中文系统、py3.14）：336 passed |
| 1.2.220 | e2e 在 Windows 上 14/19 | 子进程按 UTF-8 输出、e2e 按 GBK 解码 → 中文全乱码（F4 关键字匹配失败、M8 日报路径乱码"找不到文件"）→ 改 UTF-8 解码；M1/F2/C1 改为适配真机（已授权机器无试用是正确行为；Windows 用 ACL 拒写模拟杀软/IT 锁；有 Chrome 时报出路径）。真机 19/19；windows CI 改为必过并加跑 e2e |

## 二、未解问题（按优先级）

### P0-1 远程执行通道默认开启，且机器人业务依赖它（不能只改默认值）
- 两条通道：`remote_channel`（本机 relay，`settings.py:15` 默认 enabled=True，面板默认勾）；`remote_agent`（经控制面轮询的 exec/read/write/ls，`main.py:321` 无条件启动，**没有任何开关**）。
- 机器人有 12 个功能靠"往客户机发一段 Python/脚本执行"实现：`scan_inbox`、`make_batch_plans`、`do_publish`、`_update_plan_time`、`clear_client_ledger_blocks`、`collect_and_report`（取文件）、`ensure_runtime`/`_runtime_from_agent_pid`、`_mt_reschedule_item`（write+exec）、`_mt_refresh_plan_data`/`_mt_read_client_json`/`_mt_send_now_locked`（read）。直接关通道这些全坏。
- 正确改法：把这 12 段脚本做成客户端内置的"具名操作"（白名单，参数校验，路径限定在数据区），机器人只能按名字调；任意 exec/write 默认关，仅客户在面板里开"远程协助"（限时、留痕）才可用。旧客户端不认识具名操作时机器人走旧路径并提示升级。
- 状态：设计已定，待做（需对方控制面/机器人一起改和联调）。

### P2-2（原 P0-2，1.2.220 降级）Windows 上凭据文件没有 0600 等价保护
- 证据：`activation.py:85-98` 写 `control.json`（含 `customer_token`、`device_token`）用 0600；Windows 上 `os.chmod` 只切只读位。
- 实际风险：文件在 `%LOCALAPPDATA%\HuolongAgent\publish-worker`，用户配置文件的默认 ACL 已经挡住同机其他普通账户；管理员本来就能读。所以不是"任意本地账户可读"。
- 需确认：`activation.py` 还会读程序目录下的 `control.json`；若机器级安装到 Program Files（所有用户可读），不能往那里写凭据。
- 建议：Windows 上用 `icacls` 只授当前用户 + SYSTEM 作纵深防御。测试已改为按平台断言（1.2.220）。

### P1-3（原 P0-3）「卡死自愈」Windows 真实场景未测
- 1.2.220：测试已注入存活/CDP 探测，`survival_probe` 的 Windows 分支被单测覆盖，真机 Windows 11 通过。
- 仍缺：真机冻结一个 Chrome 实例 → 触发发布 → 看是否自动重启且页面真的打开。

### P1-4 没有源码的版本无法证明 OTA 可用
- 证据：`python3 publish-control/client/tools/ota_patch.py gate --simulate` 输出 `WARN versions in range with no source in git: 1.2.193, 1.2.194, 1.2.195, 1.2.207–1.2.214`。
- 后果：这些版本的客户热更新路径证不了，只能走完整安装包；根因是历史上有非 main/tag 出包。
- 建议：这些客户一律改发完整安装包；以后出包只认 main 上的 tag（`release_guard.py` 已卡）。

### P1-5 旧版 supervisor / 接管逻辑不在升级范围
- 已发布 tag 里的 supervision 代码不会被补丁替换，回滚路径行为以旧版为准。1.2.188–1.2.191 客户尤其要按完整安装包处理，别指望 OTA 修好自启。

### ~~P2-6 Windows CI 仍是信息位~~（1.2.220 已改为必过，并加跑 e2e；需在 Settings → Branches 把 "windows (py3.11)" 加进必过检查）

### P2-7 客户面板自述「Windows 真机与真实账号尚未验收」
- `panel.py` 首页提示文字里就写着这句（本地测试完成、真机待验）。对外交付前应改成与实测状态一致的说法，或者补完真机验收再删。

## 三、复现命令

```bash
# 仓库根目录，一个会话跑全部（Linux 与 Windows 同一条命令）
python -m pytest -q -p no:cacheprovider publish-control/tests publish-control/client/tests
cd publish-control/client
python tools/static_verify.py                 # 127/127
python tools/e2e_selfverify.py --out r.md     # 19/19（Linux 需非 root；Windows 真机亦 19/19）
python tools/ota_patch.py gate --simulate     # PASS + 上面那条 WARN
```
