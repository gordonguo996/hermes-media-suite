# CONTEXT — publish_control（机器人 → 客户机远端通道）

## 1. 作用域
服务端控制面（server.py，127.0.0.1:18977）+ control.db，把飞书机器人指令下发到客户机 agent。
客户机 agent = 漫剧发布助手（端口 9223），程序目录由客户自行解压（常在 Downloads）。

## 2. 客户口径 / 硬约束
- 客户素材放**桌面「发布素材」文件夹**（不可 junction 到 inbox：同一文件两条路径会让发布拒绝）。
- **不设发布时段（2026-10-29 用户拍板）**：客户给时间就按客户时间，没给就确认后立即发；客户机 `prepare` 的窗口/≥130 分钟提前量只是历史默认值，机器人收到结果后一律重排覆盖。
- 机器人的「发布」= 建计划并排到下一个合法时段，不是立即发。

## 3. 踩过的坑
- **改客户机代码必须同步改 MANIFEST.json 里该文件条目**，否则启动链完整性 ok=False → 新增发布被拒（INTEGRITY_FAILED）。每改一次都要重算 sha256 + bytes。
- 代码文件推送不等于进程已加载。维护变更须在另行批准的窗口核验 VERSION、MANIFEST、实际 PID 与完整性；维护启动只允许静默入口，run_panel/桌面图标仅供客户主动打开。
- 桌面文件夹里的 `先看这里.txt` 曾被当成待发素材（扫描未过滤后缀），已修：只认 .mp4/.mov。
- 显式 files 列表原先只认 inbox，桌面路径会被静默丢弃；已修为在 inbox + 桌面两个根里查找。
- 远端 exec 带中文路径时，`cmd /c dir` 这类调用易报「找不到文件」；改用 `ls`/`read`/`write` op 或 cwd= 参数。
- **客户机升级排查顺序（2026-10-27 定，别再靠猜）**：①`Get-NetTCPConnection -LocalPort 9223 -State Listen` → PID → `(Get-Process -Id PID).Path`，exe 不在 `%LOCALAPPDATA%\Programs\HuolongAgent\publish-assistant-<ver>` 就是旧版在跑（healthz 的 version 会骗人）；②`.venv\pyvenv.cfg` 的 `home` 必须指向当前 program dir 的 `runtime`，否则杀旧进程后新 agent 起不来；③`run\program-path.txt` 和桌面 vbs 必须指向固定目录，不能是 `%TEMP%`/`Downloads`。
- **安装包 step_stop_running 会静默放弃**：旧版 exe 在非产品目录且 `/healthz` 无应答时，日志只打 `refused: image ... outside product folders`，安装照样报成功。别只看「installation complete」。
- **机器人投递 403 的根因（2026-10-27）**：控制面一旦设置 `CONTROL_INBOUND_TOKEN`，`/api/inbound` 只认 inbound token；机器人却拿 admin token 投递 → `403 bad inbound token`，客户看到「指令投递失败」。机器人对 `/api/inbound` 必须带 `X-Inbound-Token`（管理读接口仍用 admin bearer）；两个 token 存在机器人 `.env` 的 `CONTROL_TOKEN`(admin) 与 `CONTROL_INBOUND_TOKEN`(inbound)。
- **排期发布的时间学（2026-10-27 定，之前反复失败的根因）**：客户机 `prepare` 把计划排到下一个合法时段（≥130 分钟），而 `publish` 只在**计划时间 ±10 分钟**内才允许执行，否则返回 `SCHEDULE_OUT_OF_WINDOW`。所以确认后不能立刻下发 confirm；机器人必须把批次挂成 `scheduled`，在**计划时间-3 分钟**签发新凭据再下发（bot.py 的 `mt-scheduled-dispatch` 线程，30 秒轮询，重启后续跑）。
- **账号绑定写死导致发布必失败（2026-10-27 定）**：客户机 `remote_ops.py` 的 prepare 把 `piece.platform_account_id` 填成 `env_x:平台`，而 `publish_exec._verify_account` 拿它跟页面真实 `uid/sec_uid` 比对 → 永远不一致，报「实际平台账号无法确认或与计划不一致」。修法：prepare 完成后把计划重建为 `platform_account_id=""`（发布时按当前登录账号自动绑定）+ 重算 `compliance.content_hash`（material_fingerprint，schema publish-content/3）+ 重新 dry-run 换 token。机器人侧已在 `store_mt_prepared` 里做（`_mt_rebind_prepared`）。
- **publish 不能塞在远程 exec 里跑**：远程 op 约 100 秒会被掐断且回报 rc=0（看起来正常），实际发布进程被杀。必须用 DETACHED_PROCESS 分离启动，再读日志文件判断结果。
- **草稿复用会报 `NO_UPLOAD_BYTES`**：同一文件上一次跑到一半被掐断后，页面会恢复草稿、不再真正上传，上传字节证据为空 → 证据闸拒绝提交。要么先清草稿/关掉残留 post/video 标签页再跑，要么换一份未被缓存的素材。
- **批量标题取文件名导致整批 TITLE_TOO_LONG（2026-10-27 修，1.2.154）**：客户机 `agent/winport/remote_ops.prepare` 原来只在**单条**时用机器人给的标题，多条时一律取 `path.stem`；素材名是 32 位哈希或 45 字长名，客户机 dry-run 直接判 `TITLE_TOO_LONG` 整批失败。已改为：整批统一用机器人标题；无标题时哈希/纯英文/超 30 字的文件名回落到「火龙漫剧免费看」，正常中文短名保留并截到 30 字。该历史修复版本为 1.2.154；后续维护遵守统一静默启动规则。
- **客户随时可改排期（2026-10-27 加，机器人侧）**：机器人支持「改到明天11点」「改到下午5点半」「第2条改到20:30」「全部改到…」。做法是 `_mt_reschedule_item` 在客户机重写 plan.schedule_at + 重跑 dry-run 换新 confirm_token，再存回 mt_batches；已确认(scheduled)的批次也适用，排期线程按新时间下发。客户机本身不接受 schedule 类命令（ALLOWED_KINDS 无 schedule），改期只能走机器人这条路径。
- **桌面 vbs 写不进去不等于安装失败（2026-10-27）**：bootstrap 最后一步重写 `桌面\漫剧发布助手.vbs` 可能 WinError 5（360 锁文件），日志报 `Installation incomplete` 并停在「stopping running agent」之后，但 venv、wheels、授权、自检都已 GREEN。该 vbs 本身是读 `run\program-path.txt` + `.venv` python 的，不重写也能启动；确认 venv 有 playwright + 自检全绿即可放行。
- **面板显示「1号 未登录」先别信（2026-10-27）**：面板若用程序目录的 `runtime\python.exe` 起，读不到 cookie 证据会报 UNVERIFIED/未登录。用 `.venv\Scripts\python.exe -m winport.cli login-check --env env_1 --json`（PYTHONPATH=程序目录\agent）才是准的；本次实测 env_1 抖音 72 条 cookie、星图 25 条，均 ONLINE。
- **远程 op 跑 CLI 的解释器由 `CLI_PY` 决定（2026-10-27）**：agentpoll 用 `CLI_PY`/`WINPORT_PYTHON`/`sys.executable` 起 CLI。缺 playwright 时把 `CLI_PY` 设成 `.venv\Scripts\python.exe`（setx 后要用 schtasks 重启 agent 才会继承新环境，不能直接 start）。
- **维护重启必须静默，别用 run_panel（2026-10-27 定）**：`run_panel.py` 每次启动都会 `webbrowser.open` 开一个助手面板窗口，客户看到的就是「一直弹的窗口」。远程重启/升级后拉起 agent 一律直接 `pythonw -m winport --data <data>`（分离进程），不要再走桌面 vbs / run_panel；只有客户双击桌面图标才允许弹面板。
- **远程 exec 会弹 System32 黑窗（2026-10-27 根因已定位）**：客户机走的是控制面轮询通道 `remote_agent._op_exec`，它直接 `subprocess.run(...)`，没有加隐藏窗口参数 → 每执行一条远程命令就在客户桌面弹一个黑窗。修复：`_op_exec` 里加 `**hidden_window_options(args)`（配合 `subprocess_text` 的 `STARTF_USESHOWWINDOW/SW_HIDE`）。`process_tree.py` 务必只保留 `CREATE_NO_WINDOW`，**不能加 DETACHED_PROCESS**——无控制台的 console 程序会自行 AllocConsole 弹窗。改这三个文件都要同步 MANIFEST 条目。

- **2026-10-28 实机修复约束**：维护重启以静默 `runtime/pythonw -m winport --data` 为准，覆盖旧条目中的 run_panel 建议；修改代码同步 MANIFEST 后需核对新 agent PID 与客户目录 integrity.ok=true。
- **冻结定位**：发布使用 `customers/<customer_id>/run/publish_holds.json`；公共根的 CLI clear 返回 false 不代表客户冻结已解除。用正确绝对路径实例化 PublishHoldStore.clear_manual，保留 resolved 审计。get/set 会 reload，并非必然是内存副本问题。
- **发布成功回读**：remote_ops.confirm 要解析完整多行 JSON；显式 approved_publish 双开关且 exit=0 的空输出仍按成功处理。机器人账本兜底按 content_hash + receipt_id 绑定；账本不保证有 plan_id/command_id。video_id 是上传视频标识，不能冒充平台作品号。
- **机器人进程**：使用 feishu-bot.service 管理单实例；手工再启动会重复轮询。bot.main 增加进程生命周期文件锁，重启/维护先检查服务和实际进程。

- **隔离实施约束（2026-10-28 P0-1）**：`remote_pc.py read` 虽不改客户文件，仍经 `/api/remote/exec` 写控制面请求记录；任务禁止生产数据库写入时只能使用已有只读快照，不以“read”名称视为无副作用。
- **P0-1 契约（2026-10-28 任务书声明已上线，本轮未运行核验）**：显式 false 与 exit=0/强成功证据冲突须进入 needs_reconcile；通知重试只重发消息。控制面命令列表固定最近 50 条且不支持分页参数，机器人可按本地批次 command_id 补查，不能据此声称所有历史命令已遍历。实施及验收缺口以 P0_1_IMPLEMENTATION.md 为准。

- **P0-2/P1-1 契约（2026-10-28 任务书声明已上线，本轮未运行核验）**：取消意图必须先独立持久化再等批次锁，文本停作用当前绑定会话、取消卡只作用指定批次；调度与旧状态写回均回读取消意图。清单刷新清旧选择/话题，草稿修改使用revision/status CAS，禁止旧请求递增追平取消版本。确认摘要须绑定素材SHA、标题、话题、账号和排期，刷新preview后重新复验，不能只看新token自洽。
- **客户机准备排序**：prepare会按mtime重排显式files，逐条话题不得只按原列表序号盲传；按素材身份绑定并核对重建后的plan/topics/hash。当前累计补丁与测试结论见 `P0_2_P1_1_IMPLEMENTATION.md`，全量仍非绿，不能视为部署或人工终审通过。

- **唯一恢复口径（2026-10-28）**：禁止自动清 holds、清账本或自动重试。明确客户、通道和目标记录，核实无在途后，人工恢复仅通过 Stage2Ledger.recycle_zombie 回收满足其条件且无 publish_confirmed 的 RESERVED，保留审计；UNKNOWN / SUBMIT_INTENT / CONSUMED / publish_confirmed 一律不删改。冻结需人工核实后在客户专属目录 clear_manual，不能用公共根替代。成功证据绑定 content_hash + receipt_id；video_id 不是平台作品号。
- **剩余项已部署（2026-10-28 13:0x，运维已上线，独立终审未做）**：server.py 补丁 797d4137、bot.py 补丁 66afa070 已打到生产并重启；`control.db` 已执行 `scripts/verify_device_binding.py control.db --apply`（加法迁移，device_binding_enabled 仍全为 0，旧 agent 继续兼容）。客户机 agent 由 1.2.154 升到 1.2.155（备份 `C:\Users\<客户机用户名>\AppData\Local\HuolongAgent\backups\materials-20261028-1310`）。原「未部署」描述作废：P1-2 清单仅接受 publish-materials/1 完整快照，绑定 snapshot_id/material_id/路径/SHA256，旧客户端清单暂不可用；客户配置不得串用。P1-3 采用显式加法迁移和按客户设备强校验，默认兼容旧 agent；启用后设备与 epoch 绑定任务，过期运行任务待核实。换机只提供离线清单及摘要校验。P2-2 自动清理入口已在补丁中禁用。补丁、验证证据和限制见 publish_control/REMAINING_IMPLEMENTATION.md；文档更新不代表运行代码已变更。
- **materials 约定（2026-10-28 已部署 1.2.155）**：纯查询不可调用会创建目录的 `_layout().ensure()`；复用发布扫描时严格读取失败必须拒绝完整快照，不能把权限/桌面发现异常当空根。完全相同 `(mtime,name.lower())` 键保留输入顺序，显式 files 必须沿用快照顺序。部署基线须同时比对 MANIFEST 顶层版本、VERSION 内容及其条目哈希，任一不一致先重取一致基线，禁止顺手修生产清单。实现与证据见 `CLIENT_MATERIALS_IMPLEMENTATION.md`。
- **客户机部署的两个坑（2026-10-28 实测）**：① 部署前 MANIFEST 顶层 `version` 是 1.2.155 而 `VERSION` 文件是 1.2.154，部署脚本会按「基线不一致」阻断；正确做法是先按 VERSION 文件把 MANIFEST 顶层标签改回 1.2.154（只改标签、保留 CRLF，另存 `MANIFEST.json.pre-rebase-20261028`），再让脚本自增到 1.2.155，不要改 VERSION 文件（会让 MANIFEST 条目哈希失配）。② 部署脚本经控制面 `remote exec` 启动时，脚本会停掉 agent 自身，op 往往 90 秒超时无回执；**超时不等于失败**，必须另起一次 ping/healthz 核对版本、PID、verify_program 和备份 deployment.json，不能凭 op 状态判定。
- **materials 生产实测（2026-10-28）**：控制面 op `materials` 返回 `publish-materials/1` 完整快照，客户机 14 条素材，`complete=true`；bot 侧 `_mt_validate_material_snapshot` 对同一快照校验通过。
- **旧控制面已下线（2026-10-28）**：用户级 `publish-control.service`（0.0.0.0:8898，Oct-20 遗留、老代码）已移出 unit 目录到 `~/.config/systemd/user/disabled_20261028/` 并终止进程，8898 已关闭；唯一控制面是 system 级 `publish-control.service`（127.0.0.1:18977）。不要恢复 8898 实例，它会与主库并发写。

- **星图任务匹配必须是包含匹配（2026-10-29 定，已加不变量）**：机器人下发的是类别词「APP种草/免费-新码/激励-新码」，发布页卡片标题是全名「火龙漫剧星广-260916-APP种草-1-优矩互动」，全等比较永远命中不了，表现为视频传完后报 `XINGTU_TASK_NOT_ATTACHED`。正确做法：精确优先→包含兜底→无结果时清空搜索框全量再匹配，并把页面上实际卡片名写进日志。`scripts/check_publish_invariants.py` 第 3 条已固化该约束，旧包实测 FAIL、新包 PASS，上线前必跑。
- **单文件热修会被整包替换吞掉（2026-10-29 事故根因）**：10-29 10:55 的 publish_exec 热修在 11:1x 被 1.2.157 整包覆盖，文件回到旧逻辑且无人察觉。规则：①任何整包/补丁上线后，比对现网 `agent/winport/publish_exec.py` 的 sha256 与 `fix_evidence_client/runtime/winport/publish_exec.py` 模板是否一致；②跑 `check_publish_invariants.py`（现已含封面顺序、无封面跳过、星图包含匹配三条）；③验收标准是「真发出一条带星图任务的」，不是「代码改完」。

- **星图弹窗真实 DOM（2026-10-29 实测，别再猜选择器）**：发布页布局本身带 `card-container-creator-layout`，用 `div[class*=card-container]` 全文档查会把整个表单当成一张卡，匹配必然失败（日志里 `card_texts` 是「基础信息/作品描述…」就是此症）。真实结构：弹窗在 `.semi-portal` 内，任务卡是 `div.card-container-<hash>`，卡内文本以 `任务: 全名` 开头；已挂载后表单显示 `任务: 全名 … 更换/删除`，**没有选中态 radio**，所以校验不能只认 checked，要认「任务:行 + 更换/删除 + 无『请选择星图任务』占位」。点击控制用 `data-xt-idx` 句柄，不能用全文档 nth()。
- **星图校验层也要包含匹配**：机器人传类别词、页面渲染全名，`_xingtu_page_shows_task` 的全等比较同样会误判「未挂上」，已在选中校验和挂载校验两处都改为「全等优先 + 包含兜底」。
- **「Windows 发成功过」≠「带星图任务发成功过」（2026-10-29 核账）**：控制面记录里 Windows 客户机真实发布成功 6 条（10-28 10:28/10:35/10:43/10:50、21:30/21:43；work_id 7701540774973803818、7701544598799174939、7701712649750449471、7701715936008555818，另 2 条 published=true 但无作品号），**全部未挂星图任务**。带星图任务只做到真机挂载成功（fill、不点发布：回读「火龙漫剧星广-260916-APP种草-1-优矩互动」），从未带任务真发出去。验收标准仍是「真发出一条带星图任务的」，禁止拿普通发布成功当星图链路已通。

## 4. 多设备补丁部署记录（2026-10-28）

- 历史记录：server.py 43a06083…、bot.py c1eab2ce…、旧 client_patch 三件（agentpoll c1b3d5f9…、remote_agent eb287022…、remote_ops bc0aec0e…）。**1.2.222 起 `publish-control/client_patch/` 已删除**，客户机源码以 `client/agent/winport/` 为准，升级只走完整安装包或 `client/tools/ota_patch.py`。
- 迁移已应用：新增 device_material_claims / device_events 两表，customer_devices 加 9 列，删除 one_active_customer_device 唯一索引（一客户可多台 active）。行数未变（customers 2 / commands 80 / remote_ops 2054），integrity ok。
- 服务：publish-control.service（新建 user unit，Restart=always，127.0.0.1:18977）与 feishu-bot.service 均已重启并 active。
- 回滚：备份在 backups/deploy_multidevice_20261028_154831/（含 control.db 一致性快照与 BASELINE_SHA256.txt）。
- 兼容：device_binding_enabled=false 且未带 device_id 的旧客户机仍走 legacy 路径；客户机文件只是模板、未推送，现网机器仍跑旧 agent。
- 残留风险：素材占用在命令失败/取消时释放，若实际已发布但被记成失败，另一台机可再发一次；需「成功回执优先于失败覆盖」兜底。

- **星图「同名任务」去重不能用类别词（2026-10-29 根因+修复）**：机器人下发的 `task_name` 是类别词（如 `APP种草`），本地账本只按 task_name/order_id 判重。第 1 条发成功后，第 2 条同类别不同视频会被误判 `XINGTU_DUPLICATE_LOCAL`，表现为「本批第 2 条起全被拦」。修复：账本增加 `media_sha256`，重复判定改为「同一任务身份 + 同一视频指纹」才算重复；历史无指纹条目只在 order_id 精确相等时拦截。模板 `fix_evidence_client/runtime/winport/xingtu_tasks.py` + `publish_exec.py`，已随 1.2.158 上客户机，MANIFEST 同步重算、VERSION 递增、integrity 141 文件 ok。
- **控制面可能被手起实例占端口（2026-10-29 发现）**：`publish-control.service` 长时间 crash-loop（restart counter >1.2 万），根因是手工起的 `server.py`（PID 594261）占着 127.0.0.1:18977，systemd 每次 bind 失败。远程 op 仍可用但 systemd 永不 active；清理需人工批准（停服务/结束进程受门禁），不要自行停服。修改客户机代码后的标准顺序：写文件 → 重算 MANIFEST bytes/sha256 → VERSION 递增并同步清单 → 静默重启 `runtime/pythonw -m winport --data` → 读 `run/integrity.json` 必须 ok=true。单文件热修会被整包覆盖，部署后必须比对现网 sha 与模板 sha。

- **360「PowerShell 命令执行攻击」弹窗（2026-10-29 根因+修复）**：客户机实际是 **venv pythonw 父进程(1 线程、不监听) + runtime pythonw 子进程(监听 9223、真正干活)**；runtime 自带的 python **没有 psutil、没有 Pillow**，于是 `proc.list_processes()` 每次都有 psutil ImportError → 回落到 PowerShell CIM，360 每次都拦并弹窗。修复三处，全部上线并核对 sha：①`fingerprint._bios_uuid` 改读 SMBIOS 固件表（ctypes，无子进程），实测 UUID `AF2DC58D-...` 与旧 PowerShell 结果一致、machine_id `75ef...1dcb` 不变；②`remote_channel` 截图改 Pillow ImageGrab；③`proc.list_processes` 在无 psutil 时改用数据目录 `.venv\Scripts\python.exe` 子进程导出 JSON（隐藏窗口），PowerShell 只留最后兜底。验收：integrity 141 项 ok、powershell 进程 0、runtime python 下 list_processes 返回 315 条且带 `--user-data-dir` 命令行。
- **整包重出流程（2026-10-29 固化）**：`tools/build_bundle.py build --root . --version 1.2.159` 第一次常因 PACKAGE_BINDING.json 非规范格式报 `self-binding size changed`，**重跑一次即过**；打包前先删 `__pycache__`（否则 verify 报 extras）；zip 命名 `<目录名>-offline.zip`，条目=目录内全部文件（含 SHA256SUMS.txt）。改代码后必须同时推 `MANIFEST.json`+`PACKAGE_BINDING.json`+`SHA256SUMS.txt`，再重启。
- **重启客户机 agent 的正确做法**：杀 `-m winport` 全部进程会让远程 op 自己断线（op 无回执、ping 排队），必须用分离进程的 restarter（数据目录 `run/psfix-restart.py` 模式：等 3 秒→kill→`venv\pythonw -m winport --data`）。杀子进程(runtime)不会让父进程接管，只能整组重启。

- **星图任务「选不中」≠任务下架（2026-10-29 根因+修复）**：现象是弹窗里明明有 4 张任务卡，程序却报 `star task card did not become selected`；同一天 13:31 用同一份代码能成功、18:42 就失败，属时序问题。两个真因：①`click_picker_element` 用 `dispatch_event("click")` 是不可信事件，label 的默认激活不执行、`semi-radio` 永远不 checked；②搜索框过滤生效后弹窗重渲染、`data-xt-idx` 重新编号，点击前捕获的 index 指向别的卡或空节点。修复：先真鼠标 `item.click()` 再退回 dispatch；点前等 1.5s 用 `matching_rows()` 重取 index，失败再重取重试一次。同一坑在 Mac 执行器 10-13 已踩过并写进 skill，但没移植到 Windows 客户端，属于「修了没带过去」。防回归：`scripts/check_publish_invariants.py` 新增 2 条断言（真点击优先、点击前重取 index + 重试），打包前必须跑；实测旧 1.2.155 树 10 条 FAIL、修后的 1.2.159 PASS。

- **多选批次不回写星图任务（2026-10-30 根因+修复）**：客户回「星图」但没点名任务＝按素材名匹配，旧 `_mt_apply_auto_xingtu` 只支持「整批同一个标签」，遇到 免费/激励/种草 混选的批次算不出唯一标签就**静默 return ""**，计划里根本没有 `xingtu_task`，发出去是普通作品且日志无异常。这就是「星图任务从没真发成」的真根因（不是选择器）。修法：按素材名逐条写入 `publish_mode=xingtu` + `xingtu_task.task_name`（免费→免费-新码、激励→激励-新码、种草→APP种草、剧集→剧集剪辑），有素材识别不出就让整批停下并报错，禁止静默降级成普通发布。已改 bot.py 并重启（10:28）。
- **「确认重发」被自己的闸挡回（2026-10-30 根因+修复）**：`set_republish_candidate` 只存 plan/preview/token，不存设备；`_plan_device_identity(plan, candidate)` 拿不到 device_id，多设备绑定客户一律回「重发信息缺少目标机器」。修法：候选里持久化 device_id/binding_epoch，`_mt_store_result` 从失败指令 payload（device_id / expected_device_id、expected_binding_epoch）带入。客户端 `reserve_republish` 本来就支持「CONSUMED 之后显式重发+签名 claim」，无需改客户机。

- **群机器人取数通道（2026-10-30 定）**：机器人 `run_cli` 走的 12224 隧道经常是断的（现象：客户问数据，机器人回「无法确认发布助手当前运行时版本」或取数超时）。取数一律走控制面 `_cp_cli`（`/api/remote/exec` op=exec，WINPORT_DATA 指向 `customers/<cid>`），读结果用 `_cp_read_text`。星图数据 JSON 结构是 `tasks[].items[]`（不是 `records`），作品级字段：`item_id/item_name/publish_time/valid_play/share_amount`，消耗在任务级 `consume_amount`。`spend_data` 已改为输出摘要 + 扁平 Excel（任务/作品/发布时间/有效播放/分成/消耗/作品ID）。
- **机器人分流口径（2026-10-30）**：含「数据/取数/出表」→ `analyze_data`（抖音作品表）；含「消耗/收益/分成/星图」→ `spend_data`（星图表）；上一条是星图取数失败后客户回「登录了/已登录」→ 自动重试星图取数。

- **\u661f\u56fe\u4efb\u52a1\u5217\u8868\u53ea\u8ba4 POST\uff082026-10-30 \u6839\u56e0\uff09**\uff1a`author_get_performance_list` \u7528 GET \u4f1a\u8fd4\u56de\u7a7a\u5217\u8868\uff1b\u6b63\u786e\u8bf7\u6c42\u662f POST\uff0cbody `{"search_condition":{"exist_valid_brand_ad_order":false,"only_light_cooperate_task":false},"page":"1","limit":"10","search_source":1}`\uff0c\u5e76\u5e26 `x-secsdk-csrf-token`\u3001`agw-js-conv: str`\u3001`x-login-source: 1`\u3002agent \u7684 collect_xingtu \u5728 1.8 \u79d2\u5185\u62ff\u4e0d\u5230\u9875\u9762\u81ea\u5df1\u7684 POST \u54cd\u5e94\u65f6\u5c31\u56de\u9000 GET\uff0c\u7ed3\u679c task_count=0\uff0c\u673a\u5668\u4eba\u62a5\u300c\u661f\u56fe\u6570\u636e\u8bfb\u5230\u4e86\uff0c\u4f46\u91cc\u9762\u6ca1\u6709\u4f5c\u54c1\u8bb0\u5f55\u300d\u3002\u5df2\u5728\u5ba2\u6237\u673a\u6570\u636e\u76ee\u5f55\u843d `tools\\xingtu_fetch.py`\uff08\u6570\u636e\u76ee\u5f55\u4e0d\u5728 MANIFEST \u8303\u56f4\uff0c\u4e0d\u52a8\u53d1\u5e03\u5b8c\u6574\u6027\uff09\uff0c\u673a\u5668\u4eba `_cp_xingtu_collect_fix` \u5728 rows \u4e3a\u7a7a\u65f6\u81ea\u52a8\u91cd\u53d6\uff1b\u6a21\u677f collect_xingtu.py \u5df2\u6539\u6210 POST \u4e14\u7b49\u5f85\u9875\u9762 POST\u3002\n- **\u661f\u56fe\u4f5c\u54c1\u7ea7\u91d1\u989d\u4e0d\u5728\u63a5\u53e3\u91cc\uff082026-10-30 \u5b9e\u6d4b\uff09**\uff1aitem_info \u7684 reward_amount/author_total_share \u5168\u4e3a 0\uff0c\u4efb\u52a1\u7ea7 author_reward_amount \u624d\u6709\u503c\uff08\u5355\u4f4d\u5398\uff09\uff1b\u4f5c\u54c1\u7ea7\u5206\u6210\u53ea\u80fd\u4ece\u8be6\u60c5\u9875\u5361\u7247\u6293\uff08collect_cards.py\uff0c\u300c\u5df2\u7ed3\u7b97\u6536\u76ca\u300d\uff09\u3002\u4e0d\u6293\u5361\u65f6\u5c31\u4e0d\u8981\u865a\u62a5\u5206\u6210\u3002\u6d88\u8017\u4e5f\u53ea\u6709\u4efb\u52a1\u7ea7 consume_amount\u3002\n
- **1.2.160 \u51fa\u5305\uff082026-10-30\uff09**\uff1a\u76ee\u5f55 `build_1.2.160/manju-publish-assistant-1.2.160-win64`\uff0cVERSION=1.2.160\uff0cMANIFEST 3205 \u9879\u3001verify ok\uff1bzip `publish_pkg/manju-publish-assistant-1.2.160-win64-offline.zip`\uff083207 \u6761\u76ee\uff0c299MB\uff0csha256 70e7beb1a940d577605ce52370e8a4d6dd86e664ed88317d2e4070e3b3e016e6\uff09\u3002\u5305\u5185 `agent/winport/collect_xingtu.py` \u5df2\u542b\u661f\u56fe POST \u4fee\u590d\uff08\u7b49\u9875\u9762 POST + POST \u515c\u5e95\uff09\u3002\u672c\u673a `/dev/shm` \u4e5f\u7559\u4e86\u4e00\u4efd\u3002\u76d8\u5269\u4f59\u7a7a\u95f4\u4ec5 ~188MB\uff0c`rebuild_151/\u2026-1.2.155`\uff08491MB\uff09\u548c backups \u91cc 330MB \u65e7\u5e93\u53ef\u6e05\uff08\u9700\u4eba\u5de5\u6279\uff09\u3002\n- **\u51fa\u5305\u6d41\u7a0b\u8865\u5145**\uff1a`tools/build_bundle.py build --root . --version X --out .` \u4f1a\u91cd\u5199 MANIFEST/SHA256SUMS/PACKAGE_BINDING\uff0c**\u4e0d\u5199 VERSION**\uff0cVERSION \u8981\u624b\u5de5 `printf 'X\\r\\n'`\uff1b`zip` \u547d\u4ee4\u672c\u673a\u6ca1\u88c5\uff0c\u7528 python zipfile \u6253\u5305\uff1b\u672c\u673a\u6ca1\u6709 windows_compat_check.py\uff0c\u7528\u81ea\u5199\u68c0\u67e5\uff08ps1 BOM\u3001bat/vbs GBK\u3001bat CRLF\uff09\u3002\n
- **同路径覆盖升级的 supervisor 归属**：路径相同不代表进程已加载新版本；仅当实际 PID、owner program/version 与当前包一致才保留。同路径旧 owner 必须先停止并确认退出，新 supervisor 通过原子 guard 重试跨过 30 秒锁保护期；不得直接改写存活 owner 或删未知 guard。程序与数据目录可跨盘，镜像白名单检查须逐根处理不同盘符。构建任务若明确禁止重算清单，应保留原件并记录实际版本与哈希状态，不得照任务描述假定清单已更新。

- **监督器诊断与重装时序约定**：面板先以 owner PID/program/version 与实际进程证明归属，额外登录入口不能使合法 owner 失效；只停非 owner。安装重装先确认 supervisor 退出再停止 agent，避免触发其崩溃退避。锁失败诊断读取 data/run/supervisor-exit.json、owner、guard/staging 年龄及日志尾部；诊断只作证据，不作锁回收授权。py_compile 必须另设 PYTHONPYCACHEPREFIX 到包外临时目录，仅 PYTHONDONTWRITEBYTECODE=1 不会阻止显式 py_compile 生成 pyc。

- **桌面入口与安装失败证据约定（1.2.167）**：runpy.run_path 不自动加入脚本所在目录；面板及生成的桌面启动命令必须显式加入包根，测试从 /tmp 且不预置包根执行。安装所有步骤及构造失败统一在 main 捕获后追加只读诊断，保留异常类型与信息并写控制台、安装日志、桌面副本。历史 FSO ANSI owner 读取统一按 UTF-8→mbcs/GBK→latin-1 回退并记录编码；中文路径必须覆盖归属匹配用例。无法证明归属的服务端口占用立即拒绝，空闲端口正常继续。Linux 注入验证不代表 Windows 实机或独立终审。

- **星图任务「点卡片不选中」根因（2026-11-03，1.2.170 修）**：发布页弹窗里点 `div.card-container-*` 真实点击成功，但里面的 `label.semi-radio > input[type=radio]` 不会变 checked、弹窗不关，表单永远是「请选择星图任务」。fill 模式更糟：`_xingtu_select_fill_row(wait_for_selection=False)` 点完卡片不看 radio 就 `return fresh or current`，直接把失败当成功。修法：按 `label.semi-radio` → `input[type=radio]` → 卡片 顺序点，点完必须读到 checked（或弹窗已关）才返回，否则继续下一个目标；选完要点弹窗「确定」。publish 模式原本就有 radio 兜底，所以只有 fill 暴雷。
- **星图挂载回读契约与真实 DOM 不符（2026-11-03，1.2.170 修）**：旧契约要求 `任务:` 标签与 `更换/删除` 是直接兄弟，实际 DOM 是 `card-container-*` 里放 `任务:`，`更换`(`span.reload-*`)/`删除`(`span.delete-*`) 在卡片外面一层 wrapper，结果 blocks=0 → 永远 `form_block_ambiguous`。新契约：可见、非 popup、非 creator-layout 的 `card-container`，文本里 `任务:` 恰好一次，名字取 `[class*="task-name-"]`，向上最多 3 层找同时含更换+删除的 wrapper，并要求页面上「请选择星图任务」占位消失；名字比较改为「全等 / 互相包含」（表单显示的是截短名，不带「奖金无上限落地页…」后缀）。`readback_via` 现在返回 `form_card`。
- **验证口径**：fill 跑通 = `filled=true` 且 `xingtu.readback ok=true`；`stopped_at=publish_button` 是 fill 的正常终点。真机验收仍是「真发出一条带星图任务的」，fill 成功不等于发布链路验收。
- **`login-check` 的星图 ONLINE 会骗人（2026-11-03 实测）**：它只看 xingtu.cn 首页 HTTP 200 + cookie 条数，登录态过期时仍报 `env_1:xingtu ONLINE`（22 条匿名 cookie）。真判断要看任务页：登录过期会跳成 `https://www.xingtu.cn/?redirect_uri=/sup/creator/user/task` 并显示首页「登录」按钮，任务表 0 行；此时 `xingtu-tasks` 的最终报错只是兜底的 `XingtuTaskListEmpty: the publish-page star task picker has no writable task`，容易被误读成「账号没任务」。排查顺序：先看任务页 URL/是否有登录按钮 → 再谈任务是否存在。
- **1.2.173 真机验收通过（2026-11-03）**：fill 与 approved_publish 双通。fill 回读 `xingtu.readback ok=true / form_card`；真发一条 `work_id=7703854321254927658`、`publish_mode=xingtu`、`xingtu_task=火龙漫剧星广-260915-免费-新码-1-优矩互动 (order 7686009187993747494)`，发布时间 2026-11-03T16:10:22+08:00，账本 result=成功；星图侧剩余配额 64→63 独立印证任务确实扣上。
- **远程手工跑 CLI 的 license 路径坑（2026-11-03）**：`WINPORT_SYSTEM` 必须指向 `publish-worker\system`（正式 license.json 在这），指到 `customers/<cid>\system` 会报 `NOT_ACTIVATED / 未找到授权文件`。agent 自身命令通道用的就是前者，手工 exec 时别漏。

- **机器人全流程实测（2026-11-03 16:2x）**：模拟客户在飞书说「发布」→ 清单 43 条；「1」→ 问星图/普通；「星图」→ 拉到 3 条真任务（约 70s）；env_1 抖音 60 cookie、星图 25 cookie 在线。测到 prepare 为止，未点确认、未发布。测试产生的 run/remote/draft-1c563265… 已删。
- **星图列表未回就抢答序号会绑错任务（2026-11-03 实测）**：`_mt_start_xingtu_menu` 异步拉任务（约 70s），期间 `status=awaiting_xingtu_task` 且 `auto_xingtu_tasks=[]`；客户若先回「1」，`_mt_pick_xingtu_task` 走「无列表就接受客户输入的名字」分支，把「1」当任务名。修法：任务列表为空且在拉取中时回「还在拉取，稍等」，不要把纯数字当任务名。
- **prepare 命令会把未发布素材标成 published（2026-11-03 实测，控制面缺陷）**：`server.py` 在命令 status=done 时无条件 `UPDATE device_material_claims SET status='published'`，op=create（prepare）同样命中。库内已有 4 条此类误标（含测试产生的一条，已回滚为 released，证据 fix_evidence_test_cleanup_20261103.json）。修法：只有 op=confirm 且回执 published=true 才标 published。
- **2026-11-03 已修上述两条**：①`server.py` 新增 `_published_evidence()`，只有 `payload.op=confirm` 且有 published=true / platform_work_id 才把素材标 published；用隔离库做 record_result 集成测试：prepare→claimed、真发布→published、confirm 无证据→claimed，三条全过。②`bot.py` 星图拉取期间（`xingtu_fetch_started_at` 起 300 秒内、列表仍空）收到纯数字回「还在拉取，稍等」，不再把数字当任务名；离线用例通过。两个服务已重启（publish-control 16:28:29 / feishu-publish-bot 16:28:34），备份 server.py.bak_20261103_claims、bot.py.bak_20261103_xingtu_wait。历史另有 3 条 create 命令留下的 published 记录未动（无法判定是否早前真发过，且不影响单机发布）。

## 增量包出包强制检查（2026-11-17 定，物理检查）

**出任何 `patch-1.2.NNN.zip` 之前必须跑，RESULT: PASS 才允许发布：**

```bash
cd /home/ubuntu/build/hermes-media-suite/publish-control/client
python3 tools/patch_coverage_check.py --to-tag v1.2.NNN \
    --patch-zip /home/ubuntu/workflow/projects/publish_control/updates/patch-1.2.NNN.zip \
    --manifest /home/ubuntu/workflow/projects/publish_control/updates/manifest-1.2.NNN.json
```

规则两条：
1. `min_version` 到目标版本之间**每个版本**改过的程序文件必须全部在补丁里。程序文件 = `agent/`、`config/`、`scripts/` 下全部 + 客户端根目录的 `*.py`/`*.bat`（`run_panel.py`、`bootstrap.bat` 这类）；不含 `tests/`、`tools/`、`VERSION`、`MANIFEST.json`、`SHA256SUMS.txt`。
2. `min_version` 必须等于**第一个被完整覆盖的版本**——不是随手填的旧版本号，也不是「大部分文件在就够」。

**背景（1.2.218 事故）**：清单写 `min_version=1.2.188`，补丁只从 1.2.192 起完整。1.2.188–1.2.191 客户 OTA 后 `python -m winport` 报 `ModuleNotFoundError: winport.process_identity`，updater 健康检查失败回滚，形成「升级→起不来→回滚」死循环；1.2.193 另缺 `process_identity.py`/`updater.py`，根目录 `run_panel.py` 也漏打。修法：把 1.2.188→1.2.218 之间改过的全部程序文件打进补丁（23 → 34 个文件），重签清单，`min_version` 保持 1.2.188 且每个版本均 0 缺失。

**发布顺序**：先替换 `patch-1.2.NNN.zip`，再替换 `manifest-1.2.NNN.json` 并覆盖 `manifest.json`（服务器每次请求现读，不用重启）；旧包旧清单先 `cp` 到 `updates/backups/superseded_<ver>_<ts>/`。发布后从广州机 `curl -sk https://xingturgzn.cn/ctl/update/manifest.json` 核对 version / min_version / files 数量 / patch_zip_sha256，并下载 zip 核 sha256 与大小。
