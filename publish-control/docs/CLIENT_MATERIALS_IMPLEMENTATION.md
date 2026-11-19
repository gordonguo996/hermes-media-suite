# 客户机只读 materials：隔离实现交付

代码和测试已完成，未部署。1.2.222 起，交付源码统一位于 `client/agent/winport/`（agentpoll.py / remote_agent.py / remote_ops.py）；旧的 `client_patch/` 副本已删除。默认 dry-run 的 `scripts/deploy_client_materials.py`、`fix_evidence_client/` 测试与独立审查记录保留。人工终审及维护授权尚未进行。

## 实现

- `remote_ops.materials(config, payload=None, command_id=None)` 返回 `publish-materials/1` 完整快照，默认参数兼容；拒绝未知 schema、客户不符和 layout 客户不符。根只从配置 layout 与现有桌面发现函数取得，请求中的路径不能改变根。
- 直接复用 `_publish_roots/_scan_inbox/_owned_drop_file/_owned_inbox_file`。扫描保持 inbox 优先、桌面其次，非零常规 mp4/mov、链接与临时名排除、路径和 `(name.lower(), size)` 去重，再按 `(mtime, name.lower())` 排序。保留同名同大小但内容不同也合并的历史行为。
- 共用函数新增默认关闭的 `strict` 参数，仅 materials 开启：目录枚举、桌面发现、文件权限或 IO 错误不能伪装为完整空清单；链接/非常规文件仍按原安全规则排除。prepare 默认扫描行为不变。
- SHA256 通过已有 `secure_open` 的无跟随句柄流式读取。读取前、同句柄读取后、路径复查均比较 dev/ino/size/mtime_ns/ctime_ns/mode；返回前重扫集合、顺序并复查每个文件。变化返回 `ok=false, complete=false`，不交付部分列表，调用方需重新查询。
- material_id 绑定客户、规范绝对路径、size、mtime_ns、SHA256；snapshot_id 绑定 schema、客户、根及有序完整列表；有效期不超过开始扫描后的 1800 秒。全量返回，无 500 条上限或分页。
- 查询直接使用 `config.layout`，不调用 ensure、prepare、dry-run、receipt、CLI 或写文件。只读指素材和业务数据；远程任务队列本身仍按原协议记录请求和结果。
- agentpoll 增加 materials kind，并兼容 publish 的 materials op；返回附原有 command_id/kind/status。remote_agent 的 execute 新增可选 config，把远程队列中 materials 请求交给同一实现，旧调用签名仍可用。
- 三个交付源码均为 ASCII，无新增依赖；原源码注释中的中文也转为转义形式。

## 测试与证据

全部测试在本机临时目录运行。开始时仅按任务书授权调用四次 `remote_pc.py read`，取得三个源码和 MANIFEST 到 `fix_evidence_client/baseline/`；这些 read 会写控制面请求记录，不写客户机文件。此后未调用客户机，未执行远程 exec/write、重启或发布。

复现：

```bash
cd /home/ubuntu/workflow/projects/publish_control
PYTHONDONTWRITEBYTECODE=1 python3 fix_evidence_client/reproduce.py
```

实际输出（`fix_evidence_client/pytest.txt`）：

```text
...............                                                          [100%]
15 passed in 0.95s
```

覆盖：

- 同名异路径、两根同名同大小不同内容、零字节、目录名带 mp4、符号链接、lnk 快捷方式后缀、隐藏/临时名、客户隔离、mtime 与名字顺序相反、同 mtime 按名字排序、507 条完整返回。
- materials 查询前后素材树内容和 mtime 不变，ensure 被替换为失败哨兵；空根不创建目录。
- 实际调用 prepare，在本地 stub 掉平台配置、排期与预览外部依赖，读取真实生成的 plan，逐条比对素材顺序和 SHA256；同时验证自动选材及逆序显式输入。
- 内容变化、扫描中新增文件、schema/客户不符、权限错误、桌面发现错误都拒绝成功快照。
- prepare/confirm/_pick_files 和已有远程文件操作 AST 不变；移除新增 materials 分支后，agentpoll 原 dispatch AST 不变；新增 strict 参数的默认扫描结果与客户机基线代码对照一致。
- 部署默认 dry-run 的 main 通过单元测试调用：禁止网络、子进程与写文件；部署 apply 仅在模拟 Windows 的临时安装树中运行，进程/网络/完整性校验全部 mock，验证备份、代码写入、MANIFEST bytes/SHA、VERSION 递增、静默启动参数、PID/healthz/integrity 成功判据及漂移拒绝。未执行部署脚本命令行或真实部署。
- unified diff 在临时基线树应用后，三文件与候选逐字节一致：`patch_check.json`。源码哈希见 `source_hashes.json`。

测试运行时依赖来自本机现有 1.2.155 安装包，保存在 `fix_evidence_client/runtime/winport/`；85 个 Python 文件中 80 个匹配读取的 MANIFEST。差异为 remote_ops/agentpoll（测试时覆盖为本次候选）及 main/publish_exec/activation（不作为本轮实机行为验证依据）。逐项哈希见 `dependency_hashes.json`；secure_io、customer_paths、desktop 匹配远程 MANIFEST。离线测试不能替代 Windows 实机验证。

首次测试暴露同名且同 mtime 的稳定排序细节：旧 prepare 在完全相同排序键下保留显式输入顺序。因此调用方必须按快照顺序提交 files，不能任意反转同键项。此行为保留，首次失败输出留在 `pytest-first.txt`。

独立审查：`fix_evidence_client/INDEPENDENT_REVIEW.md`。审查发现的读取失败吞错与 PID 路径子串匹配问题已修复；该审查不构成人工门批准。

## 部署脚本与部署前检查

脚本面向未来在客户 Windows 本机维护窗口运行；无需把整个证据运行时复制到产品目录。脚本现在直接读取 `client/agent/winport/`，并保留 `fix_evidence_client/baseline/` 作为漂移对照。默认或 `--dry-run` 只检查本地交付源码并输出计划，不联网、不备份、不写目标、不重启。实际执行必须显式 `--apply` 并提供维护批准标记、精确 program/data 路径、现有 PID 和新的外部 backup 路径。

**1.2.222 起该脚本不再承担日常升级。** 当前源码相对 1.2.155 基线已漂移，`--apply` 会以 `source baseline drift` 拒绝；客户机更新请使用签名的完整安装包，或 `client/tools/ota_patch.py check/gate` 产出的 OTA 补丁。不要再从任何 `client_patch` 旧目录推文件。

实际 apply 会核验当前源码及整个 MANIFEST 与快照一致、VERSION 条目哈希及顶层版本一致、PID 的真实解释器路径和精确 `-m winport --data` 参数、维护前完整性；之后备份三源码/VERSION/MANIFEST，停止精确 PID，原子替换各文件，按内容重算 bytes/SHA256、递增补丁版本，再直接 `runtime/pythonw -B -m winport --data` 静默启动。验证新进程存活、9223 监听 PID、新版本 healthz、运行时 integrity.json 及独立 verify_program，结果写入外部备份目录。部署并非多文件事务；中途失败会停止并保留备份，禁止继续发布，需人工回滚。

**当前基线有版本不一致信号**：只读 MANIFEST 顶层为 `1.2.155`，但 VERSION 条目 SHA256 为 `99f6d81ee51e1dd4a47dfd8bf8aa16672b0aa084e9bf90d9bfc9e3e8ca58d841`，对应 `1.2.154\n`。本轮未读取实际 VERSION、未修改 MANIFEST。维护前须读取实际 VERSION 并解决基线一致性、重新取得审查认可的基线；当前脚本会对实际 VERSION/hash/顶层版本任一不一致明确阻断，不自动修复这一历史问题。

维护前还须人工确认无在途发布、已暂停新派发、实际 data 目录与 PID/解释器/CLI 环境正确、runtime Python 依赖可用。脚本不会自动暂停控制面、清账本/holds、替人批准维护或发送发布命令。Windows reparse/junction 行为、真实 CommandLineToArgvW/进程关闭/静默启动、healthz 与 integrity 需在另行授权窗口验证。本轮只验证 POSIX 无跟随访问和模拟 Windows 部署流程。大视频全量哈希耗时需实测；超时仍应按清单暂不可用处理，不回落 ls 或返回部分清单。

## 回滚步骤（仅另行授权维护时执行）

1. 保持派发暂停，确认无在途发布，定位 backup/deployment.json 中 program/data 及备份清单；检查 backup_sha256，拒绝缺失或损坏备份。
2. 若新进程已启动，核验监听 PID 的真实解释器和精确 data 参数后停止该 PID；不得按进程名批量杀进程。
3. 将备份的三源码、VERSION、MANIFEST 原样恢复到对应路径；不重算为候选版本，不碰素材、计划、账本、holds。若存在失败写入的 `.materials-tmp`，先核实再仅清除本次临时文件。
4. 用原 runtime 与原环境执行完整性检查；直接 pythonw -B -m winport --data 静默启动，核验旧版本、监听 PID、healthz 与运行时 integrity。
5. 保留失败与回滚证据，只有人工核验后恢复派发。旧代码不支持 materials，机器人按契约显示清单暂不可用。
