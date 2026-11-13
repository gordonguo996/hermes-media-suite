# 1.2.203 实现验证记录

本轮聚焦 1.2.199 复核的客户端 P1/P2：先处理 `control_error` 再判断凭据是否变化，只有服务端明确 `rotated=false` 才提示换新码；写盘前校验 `binding_epoch` 单调，并把同码 exchange 与写盘放入同一把锁；停止中的 poller 不写 `error`，连续 2 次 poll 失败才报错，正常/异常退出分别写 `disabled`/`error`；面板失败时刷新状态行，超时时区分接入服务是否已停止；README 与本节同步「同码可重兑并换发新凭据」语义；包内 `tools/static_verify.py` 缺少 devsign/receipt_fixture 时降级跳过。实现与 Linux 定向自检不代表 Windows 实机验收或独立终审。

当前源码分支：VERSION=1.2.203。MANIFEST.json、PACKAGE_BINDING.json、SHA256SUMS.txt 由打包阶段按 1.2.203 统一重算；PACKAGE_BINDING.json 按自绑定设计不计入 SHA256SUMS，其完整性由 MANIFEST 中的规范化自绑定摘要校验（该字段单独定义、非文件原始哈希）。本记录随源码分支交付，打包后清单覆盖本文件自身；任何后续改动都必须重算清单后才能再打包。未上传客户机。

面板仅在实际 PID、owner program/version 与当前包一致时保留 owner，多余 supervisor 不使合法 owner 失效；安装阶段同版本 owner 也先停止并确认退出；旧 owner 先停止并确认退出，新 VBS 在原子 guard 下重试取得锁，覆盖原有 30 秒锁保护期。未知 owner 或异常遗留 guard 仍须人工核实，不自动删除。安装统一失败出口追加原始异常类型、owner 原文与解析、锁状态和日志尾部；面板入口及桌面启动器显式加入包根。已在 Linux /tmp、非包根 cwd 下通过 runpy.run_path 注入接管超时验证弹窗证据，Windows 桌面实测仍待执行。历史 ANSI owner 按 UTF-8→mbcs/GBK→latin-1 回退并记日志；无法证明归属的服务端口占用立即失败。

进程镜像允许当前 program 或 data，仍须通过进程归属、health version、ready PID/instance 与 supervisor owner 校验；停止逻辑额外支持经过验证的历史版本路径。tasklist 调用失败或非零退出使清理状态变为不确定，警告并跳过目录删除及重命名。

report 不再使用内置 demo key；心跳与上传均要求显式非空 key（含 loopback），否则返回明确错误且不发送，离线台账和导出仍可使用。已删除 tools/report_129_sample.py（含 demo 共享密钥与管理员口令的示例脚本）。tools/demo_offline_cycle.py、tools/hot_update_drill.py、tools/hot_update_fake_agent.py、tools/make_fixture.py 为既有离线演练与测试夹具工具，不含真实凭据，按原样保留。

保留历史约束：注册表异常 fail-closed；星图点击与回读绑定同一 DOM 实例，挂载证据限定同一直接容器；旧 v1 试用不自动重签。enrollment token 为既有扫描白名单，扫描不证明服务端绑定与撤销机制。

本轮自检记录见 `review/FIX_REPORT_1.2.203.md`；历史记录见外部 FIX_REPORT_1.2.167_GPT.md。Windows WMI/VBS、Task Scheduler、双入口竞争及真实发布仍待实机验证。

## 1.2.203 相对 1.2.199 的变更
- `agent/winport/activation.py`：`control_error` 优先于凭据比较；`control_unreachable` 提示「控制面连不上，稍后再点『重新获取接入配置』」，`control_admin_rejected` 提示「服务商侧授权配置异常，请联系服务商」。仅当服务端明确返回 `rotated=false`（或 `rotate_skipped=true`）且三项凭据全同才提示换新码；缺 `rotated` 字段按未知处理，返回「服务端未确认已换发新凭据」，不再误报成「未轮换/需新码」。
- `agent/winport/activation.py`：写 `control.json` 前比较 `binding_epoch`，新值小于盘上已有值即丢弃并返回「已获取更新的接入配置，无需覆盖」；`activate_with_code()` 的 exchange 与写盘在同一把 `_LOCK` 内完成，避免两个标签页乱序覆盖。
- `agent/winport/agentpoll.py`：`stop_event` 已置位时不再写 `error`；首次 poll 失败只记日志，连续 2 次失败才写 `error`，一次成功立即写 `connected` 覆盖旧错误；poller 正常退出写 `disabled`「接入服务已停止」，异常退出写 `error`「接入服务异常退出：原因」，状态原因会脱敏 device token。
- `agent/winport/panel.py`：「用激活码激活」失败后也拉取一次 `/api/control-status` 刷新状态行；60 秒超时时若状态为 `disabled`，文案改为「飞书接入：接入服务已停止，请重启面板」，否则仍为「连接中…（可稍后刷新页面）」。
- `CUSTOMER_README.txt` / `VERIFY.md`：同步「同一激活码可以重兑并换发新凭据，先点『重新获取接入配置』；只有明确提示未签发新凭据/需新码时才向服务商索取新码」。
- `tools/static_verify.py`：`devsign` / `receipt_fixture` 缺失时打印 `[SKIP]` 并跳过依赖检查，包内自检不再抛 `ModuleNotFoundError`；源码树内仍执行全部检查。
- `tests/test_control_bridge_1199.py` / `tests/test_control_bridge_1197.py` / `tests/test_control_bridge_1198.py`：覆盖 `control_error` 优先、`rotated` 缺失兼容、epoch 单调、同码并发、poller 去抖/停止/退出状态、激活失败刷新与停止超时文案。

## 1.2.199 相对 1.2.198 的变更
- `agent/winport/panel.py`：激活、重试自动授权或「重新获取接入配置」成功后启动状态轮询：立即拉取一次，随后每 2 秒一次，最多 60 秒；`connected`/`error`（或 `level=ok/bad`）立即停止，超时显示「飞书接入：连接中…（可稍后刷新页面）」。只更新 `#control-status-line`，不整页刷新、不覆盖 `#license-result`。重复点击会取消上一轮定时器与在途结果。
- `agent/winport/activation.py`：`_bridge_control()` 写盘前读取现有合法 control；若 `customer_token`、`device_token`、`binding_epoch` 全部不变，不覆盖 control.json，写 `error` 状态并返回 `ok=False` 与同一原因；`_install_with_control()` / `_complete_existing_control()` 不再把该情况包装成「接入已补齐」。
- `CUSTOMER_README.txt`：1.2.199 曾删除「输入同一激活码可恢复」并改为直接索取新码；1.2.203 已纠正为同码可重兑、先点「重新获取接入配置」，仅在明确未轮换/需新码时索取新码。
- `tests/test_control_bridge_1199.py`：新增同码同凭据不重写 control 的回归，以及 pending→connected、pending→error、60 秒超时三种 JS DOM 轮询回归。

## 1.2.198 相对 1.2.197 的变更
- `agent/winport/activation.py`：`activate_with_code(..., force=True)` 跳过「已有有效授权」早返回，强制走一次 exchange；`refresh_control` 有码时固定 force，无码时提示输入 8 位激活码。新 control 写入后返回 `control_written=True`，面板据此调用 poller 重启。
- `agent/winport/main.py`：新增运行期 `_restart_agentpoll()`（stop → start），面板重新获取接入配置成功后立即用新 token 重建 poller；停止/启动失败写 `error` 并在结果里说明原因。启动期 `import_error/config_error/startup_error` 一律写 `error`，不再停留在可被误显示为绿色的 pending/disabled。
- `agent/winport/control_status.py`：状态文件路径固定为共享数据根：`Layout.data` 或 `CustomerLayout.base`，不再使用租户 `root`；状态新增 `pending`。
- `agent/winport/panel.py`：control.json 写入后状态为 `pending`，面板显示 warn 色「飞书接入：连接中…」；只有 `connected` 且更新时间不早于 control.json mtime 才显示绿色。历史租户 poller 写共享状态后，面板能读到 401/403 等运行期失败。
- `tests/test_control_bridge_1198.py`：覆盖失效 token + 同一激活码刷新必须 exchange、token 变化、poller 重启、重启失败可见；`single_root=False` 时 poller 写共享根且面板不为 connected；control 写入后 pending，首次成功 poll 后 connected；启动 config_error 写 error。

## 1.2.197 相对 1.2.196 的变更
- `agent/winport/main.py`：历史分租户布局改为按 `<data>/customers/<id>` 内的稳定目录/文件（`config|run|inbox|out|system|accounts.json`）判定，不再依赖 `Layout.ensure()` 之前是否存在共享根 `inbox`；标准专用机仍使用 `<data>` 根。
- `agent/winport/panel.py`：`control-status.json` 中更新时间不早于 `control.json` mtime 的 `error/disabled` 优先于合法 control 文件，面板显示「未连接（原因）」；激活成功与「重新获取接入配置」返回后原地刷新 `#control-status-line` 文本和 `st-ok/st-warn/st-bad` class，不刷新整页、不覆盖结果文本。
- `agent/winport/agentpoll.py`：首次成功 poll 回写 `connected`；401/403 回写 `error` +「接入凭据失效」；连接/超时回写 `error` +「控制面连不上」；写盘失败只记日志，不影响主循环。
- `agent/winport/control_status.py`：状态路径同时接受 `Layout.data` 与 `CustomerLayout.root`，继续原子写、`0600`、不落 token。

## 1.2.196 相对 1.2.195 的变更
- `agent/winport/main.py`：专用单客户机默认 `single_root=True`，poller/worker 的 `--data` 等于面板与素材所在的 `<data>` 根；仅当历史 `<data>/customers/<customer_id>` 存在且 `<data>/inbox` 不存在时保留租户根。
- `agent/winport/activation.py`：已有正式授权但 `control.json` 缺失/无效时，继续执行一次 exchange 补齐接入；补接入失败不回退已有授权，并区分「授权已生效，接入已补齐」与「授权已生效，但接入未完成：原因」。
- `agent/winport/control_status.py`（新增）：原子写入 `0600` 的 `<data>/control-status.json`，记录 `connected|missing|error|disabled`、原因、时间、customer_id、device_id，不写 token。
- `agent/winport/panel.py`：授权区块常驻显示「飞书接入：已连接 / 未连接（原因）」，新增「重新获取接入配置」按钮（走一次 exchange，无激活码时提示输入）。
- `agent/winport/main.py`：复查线程只在 401/403 等显式鉴权失败时停止；连接失败/超时按 30 秒继续重试，最多 24 小时。
- `CUSTOMER_README.txt`：明确试用期内飞书发布/抓取不可用，需要激活后自动接入，并说明「飞书接入：已连接」才算开通完成。

## 1.2.176 相对 1.2.175 的变更
- `agent/winport/publish_exec.py`：pre-submit 窗口不再无条件抑制 hold。验证码、短信验证、风控、登录失效四类故障即使发生在 fill 阶段（点发布之前）也写入通道冻结，避免账号异常后队列继续撞墙；其余 fill 前置失败仍只回收账本占位、不冻结，可安全重试。异常文本与 `extra` 中的页面证据都会参与故障分类。
- `agent/winport/publish_exec.py`：fill 阶段抛出未经过 `_fail` 的账号级异常时，`run_publish` 在回收占位后按具体故障类型补写冻结，不再只依赖驱动内部调用点。
- `tests/test_presubmit_hold.py`：新增 7 条用例覆盖上述规则（登录/短信/风控/验证码冻结，普通前置失败不冻结，提交后失败仍冻结，显式 kind 优先）。
- `tools/devsign.py`：补回仓库（DEV/TEST-ONLY，不含生产私钥，按调用方传入的测试种子签名）；`tests/receipt_fixture.py`、`config/` 模板与入口脚本一并补齐，使 `static_verify.py` / `e2e_selfverify.py` 可在源码树内直接运行。

## 1.2.175 相对 1.2.174-r2 的变更
- `agent/winport/publish_exec.py`：fill 阶段的前置失败（话题回读、封面、上传等，未进入发布点击）改为回收账本 v2 占位并保持通道未冻结；`_click_failed` 仍按不确定结果处理（UNKNOWN + 冻结）。回收失败时回退为冻结，避免静默反复撞 RESERVED。
- `agent/winport/publish_exec.py`：`_apply_hold` 在 pre-submit 窗口内不写 hold。
- `agent/winport/cli.py`：外层通用异常出口在 pre-submit 失败时不再写 PUBLISH_UNKNOWN。
- `tools/build_bundle.py`：重建时把包版本同步写进 `PACKAGE_BINDING.json`（此前停留在旧版本号）。
- 其余为 VERSION / MANIFEST / SHA256SUMS / PACKAGE_BINDING 重算，无新增或删除源码文件。
