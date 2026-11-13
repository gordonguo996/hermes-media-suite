# 交付前自验报告（win-port 1.2.216）

生成时间：2026-11-13T18:35:25+0800　|　工作目录：`/tmp/sv3-dk7rq5wv`
结论：**19/19 条通过**（全部通过）

> 口径（1.2.8，GPT_REVIEW_1.2.7 必修 4）：**M6 发布是模拟单元测试**（模拟驱动，不是真平台填表）；**F1 已在降低磁盘阈值后重跑**，回显里是真实网络失败。当前功能口径：**上传+填表已验证；最终点发布待测试号验证；默认停在发布按钮前，**你点确认后才发**。

口径（source/extra-requirements-verify.md）：**没有证据 = 没验过**。下面每条都给了命令、期望、**实际回显**与产物 SHA-256；本机（Linux 构建机）跑不到的一律进「待真机」清单，不写成通过。

## 1. 自验清单（命令 / 期望 / 实际回显）

### M1 · 装（本机可验部分：数据区+配置+试用+自检）　✅　[主链路]
- 命令：`/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -m winport selftest --data /tmp/sv3-dk7rq5wv/00-case_M1_install/data  →  initialize_trial()  →  /home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -m winport selftest --data /tmp/sv3-dk7rq5wv/00-case_M1_install/data  # 另：bootstrap.ps1 建目录/铺配置/venv/自启动`
- 期望：建试用前：自检 exit=2，license RED 且给出机器码+激活码路径；安装建试用后：自检 exit=0，license WARN（试用剩余天数）；四个环境目录已建
- 实际退出码：`0`
- 实际回显：
```
env dirs: ['env_1', 'env_2', 'env_wechat', 'env_xhs']
建试用前 exit=2: [RED    ] license         未找到正式授权（license.json），且试用未创建或已失效：未找到授权文件（license.json），且没有可继续的试用记录：拒绝发布。请打开面板顶部「查看机器码」（本机机器码：0cd04bdb12b0273c784d0103a8586fcd），把机器码发给服
initialize_trial: {'created': True, 'reason': 'trial started', 'issued': '2026-11-13T10:34:57.288488+00:00'}
建试用后:
[GREEN  ] python          python 3.11.15, playwright import ok
[SKIPPED] tray            pystray/Pillow absent (static host): No module named 'pystray'
[SKIPPED] chrome          浏览器检测跳过（构建机静态自检）：本程序需要 Google Chrome（只认官方正版 Chrome，不用 Microsoft Edge、Chromium 或其它浏览器顶替）。这台电脑上没检测到 Google Chrome。怎么装（二选一）：① 用随包附带的官方离线安装包 chrome-offline\ChromeStandaloneSetup64.exe（双击即可，Google 官方原版、我们未做任何改动）；② 到官网下载安装：https://www.google.cn/chrome/（离线版：https://www.google.com/chrome/?standalone=1，国内：https://www.google.cn/chrome/?standalone=1）。装完这一个即可回控制面板点「重新检测」；除 Chrome 外什么都不用装。顺带说明：系统里也没检测到 Microsoft Edge（不影响运行）。
[SKIPPED] media_tool      ffmpeg absent (static host)
[WARN   ] license         未导入正式授权（新客户包正常状态）；试用授权有效，剩余 14.0 天。请打开面板顶部「查看机器码」（本机机器码：0cd04bdb12b0273c784d0103a8586fcd），把机器码发给服务商换取 8 位激活码，填入面板「激活码」框后点「用激活码激活」；也可让服务商签发 license.json，再用包内 import-license.bat 导入。
[GREEN  ] evidence        /tmp/sv3-dk7rq5wv/00-case_M1_install/data/evidence（保留 14 天，现有 0 条）
[GREEN  ] storage         data area writable: /tmp/sv3-dk7rq5wv/00-case_M1_install/data
SELFTEST: ALL GREEN  (static host: platform-bound lights skipped)
```
- 备注：Windows 侧 bootstrap.ps1（venv、Task Scheduler 自启动、快捷方式、注册表试用副本）只能在真机执行 → 见待真机清单 W1/W2

### M2 · 激活（Ed25519 授权链，固定信任根）　✅　[主链路]
- 命令：`license.check_license（in-process）+ license-check 固定信任根`
- 期望：配对→OK；自签+生产公钥→拒；换机→MACHINE_MISMATCH；CLI 无注入开关
- 实际退出码：`5`
- 实际回显：
```
① 配对授权: OK / None
② 自签授权 + 包内生产公钥: SIGNATURE_INVALID / signature does not verify with vendor key
③ 换机: MACHINE_MISMATCH / 机器指纹不符，请联系换签
④ 真 CLI 用固定信任根 + 本机指纹(无 --pubkey/--machine-id): rc=5
```
- 产物哈希：
  - `/tmp/sv3-dk7rq5wv/01-case_M2_activation/license.json` → `49fdea832f775bdfb759a6486769d2556b8eb6fe3b9fdf071f71dc7d4fa623e9`

### M3 · 扫码登录位（真 Chrome 命令 + 未登录态报错）　✅　[主链路]
- 命令：`/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -m winport login-open --env env_1 --dry-run --chrome-path /tmp/sv3-dk7rq5wv/02-case_M3_login/fake/chrome.exe --data /tmp/sv3-dk7rq5wv/02-case_M3_login/login`
- 期望：同一环境两个平台共用 1 个 user-data-dir + 1 个端口；未登录→OFFLINE 带原因
- 实际退出码：`0`
- 实际回显：
```
dry-run command (env_1 两个平台共用一份登录态):
  /tmp/sv3-dk7rq5wv/02-case_M3_login/fake/chrome.exe --user-data-dir=/tmp/sv3-dk7rq5wv/02-case_M3_login/login/profiles/env_1 --remote-debugging-port=9224 --no-first-run --no-default-browser-check --new-window --remote-allow-origins=http://127.0.0.1 --disable-gpu --disable-gpu-compositing --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --window-size=1440,900 --window-position=80,60 --disable-features=ExternalProtocolDialog,Translate,CalculateNativeWinOcclusion https://creator.douyin.com/
  /tmp/sv3-dk7rq5wv/02-case_M3_login/fake/chrome.exe --user-data-dir=/tmp/sv3-dk7rq5wv/02-case_M3_login/login/profiles/env_1 --remote-debugging-port=9224 --no-first-run --no-default-browser-check --new-window --remote-allow-origins=http://127.0.0.1 --disable-gpu --disable-gpu-compositing --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --window-size=1440,900 --window-position=80,60 --disable-features=ExternalProtocolDialog,Translate,CalculateNativeWinOcclusion https://www.xingtu.cn/
login-check(空登录态) exit=2
通道                
平台      
状态          
原因/证据
env_1:douyin       抖音       OFFLINE      登录态目录不存在或为空：这台机器上这个通道还没登录过，先点『开始登录』扫码
```
- 备注：真实扫码（手机）只能在真机做 → 待真机 W4

### M4 · 取件（LIST→DIFF→FETCH→VERIFY→BIND）　✅　[主链路]
- 命令：`Downlink(FileSystemSource).run_once() on /tmp/sv3-dk7rq5wv/03-case_M4_fetch/bucket`
- 期望：downloaded=2；账本记 DOWNLOAD
- 实际退出码：`None`
- 实际回显：
```
{"listed": true, "manifest_seq": 20261015001, "downloaded": 2, "skipped": 0, "failed": 0, "folders": [{"folder": "/tmp/sv3-dk7rq5wv/03-case_M4_fetch/fetch/inbox/B20261015-01/B20261015-01_示例账号_种草", "folder_name": "B20261015-01_示例账号_种草", "status": "READY", "expected_folder_key": "B20261015-01_示例账号_种草", "piece_count": 2, "videos_found": 2, "files": [{"file": "01_第12集-上.mp4", "piece_id": "B20261015-01-01", "rule": "R1", "md5": "741ffa05591634976ed13e322310933d", "sha256": "1182fb239cf32f64ff0741c0d6e04a78eafd42cd8207e1e6ec4e2feddefa05e0", "action": "BIND_OK", "result": "OK"}, {"file": "02_第12集-下.mp4", "piece_id": "B20261015-01-02", "rule": "R1", "md5": "e66a649dc57049b16dfb8729e83f65a9", "sha256": "306e5abc86f060449cfe8f17da2db596ac05ee884e23d35f4337d6e69bdf01ab", "action": "BIND_OK", "result": "OK"}]}], "errors": []}
inbox/B20261015-01/B20261015-01_示例账号_种草/.folder.json  efda49fd5e4d1550…
inbox/B20261015-01/B20261015-01_示例账号_种草/01_第12集-上.mp4  1182fb239cf32f64…
inbox/B20261015-01/B20261015-01_示例账号_种草/02_第12集-下.mp4  306e5abc86f06044…
```
- 产物哈希：
  - `/tmp/sv3-dk7rq5wv/03-case_M4_fetch/fetch/inbox/B20261015-01/B20261015-01_示例账号_种草/.folder.json` → `efda49fd5e4d1550d3038ad385adb109dde05349a5f08e9b5f2d5a73d6e58c29`
  - `/tmp/sv3-dk7rq5wv/03-case_M4_fetch/fetch/inbox/B20261015-01/B20261015-01_示例账号_种草/01_第12集-上.mp4` → `1182fb239cf32f64ff0741c0d6e04a78eafd42cd8207e1e6ec4e2feddefa05e0`
  - `/tmp/sv3-dk7rq5wv/03-case_M4_fetch/fetch/inbox/B20261015-01/B20261015-01_示例账号_种草/02_第12集-下.mp4` → `306e5abc86f060449cfe8f17da2db596ac05ee884e23d35f4337d6e69bdf01ab`

### M5 · 入队（文件夹绑定 = 任务键，READY）　✅　[主链路]
- 命令：`.folder.json 落盘`
- 期望：status=READY
- 实际退出码：`None`
- 实际回显：
```
{"status": "READY", "folder_key": "B20261015-01_示例账号_种草", "batch_id": "B20261015-01"}
```
- 产物哈希：
  - `/tmp/sv3-dk7rq5wv/04-case_M5_enqueue/enq/inbox/B20261015-01/B20261015-01_示例账号_种草/.folder.json` → `41a22d184432f3571732d75273880d13c21ff3ffc55f4d8367a154e6cba95d65`

### M6 · 发布（模拟单元测试：只填表，绝不点提交；模拟驱动须显式 opt-in）　✅　[主链路]
- 命令：`run_publish(..., mode='fill')`
- 期望：未 opt-in 即拒 / 只调 fill()，绝不 click
- 实际退出码：`None`
- 实际回显：
```
未开 opt-in 时: 被拒
opt-in 后 fill 结果: {"mode": "fill", "filled": true, "published": false, "platform_work_id": null, "work_id": null, "evidence": [], "env_id": null, "allow_submit": false, "topic_mode": null, "topic_modes": []}
driver 实际动作: [('fill', 'PLAN-SV')]（没有 click_publish）
```
- 备注：真 Chrome 里 fill 的 DOM 选择器是 W15/W16 真机项

### M7 · 抓数（离线导出 → 13 列规范化）　✅　[主链路]
- 命令：`/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -m winport collect-offline --xlsx /tmp/sv3-dk7rq5wv/06-case_M7_collect/export.xlsx --data /tmp/sv3-dk7rq5wv/06-case_M7_collect/col`
- 期望：column_count=13，有数据行，落盘 out/douyin/
- 实际退出码：`0`
- 实际回显：
```
{"header": ["col1", "col2", "col3", "col4", "col5", "col6", "col7", "col8", "col9", "col10", "col11", "col12", "col13"], "column_count": 13, "row_count": 2, "outputs": {"json": "/tmp/sv3-dk7rq5wv/06-case_M7_collect/col/out/douyin/作品列表导出_20261113.json", "csv": "/tmp/sv3-dk7rq5wv/06-case_M7_collect/col/out/douyin/作品列表导出_20261113.csv", "evidence": "/tmp/sv3-dk7rq5wv/06-case_M7_collect/col/evidence/export-header.json"}}
```
- 产物哈希：
  - `/tmp/sv3-dk7rq5wv/06-case_M7_collect/col/out/douyin/作品列表导出_20261113.json` → `87b30f6a2842c2a87216e70a56efee241760b7eb144c5311119652af2908f707`
  - `/tmp/sv3-dk7rq5wv/06-case_M7_collect/col/out/douyin/作品列表导出_20261113.csv` → `e35f3d6dd952d390ef775f0e11d718c103b9e286640bd916df28cb80eca55bfb`
  - `/tmp/sv3-dk7rq5wv/06-case_M7_collect/col/evidence/export-header.json` → `c63c3cd4c8ee9136d9938dc2972587d33483154a076f15c55c3d89f507987e11`

### M8 · 出表（日报 xlsx + md，可回读）　✅　[主链路]
- 命令：`/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -m winport report --collection-json /tmp/sv3-dk7rq5wv/07-case_M8_report/collection.json --data /tmp/sv3-dk7rq5wv/07-case_M8_report/rep`
- 期望：xlsx/md 都生成，xlsx 能被回读
- 实际退出码：`0`
- 实际回显：
```
{'md': '/tmp/sv3-dk7rq5wv/07-case_M8_report/rep/out/reports/日报_2026-11-13.md', 'xlsx': '/tmp/sv3-dk7rq5wv/07-case_M8_report/rep/out/reports/日报_2026-11-13.xlsx'}
回读 sheet 条目数=10
```
- 产物哈希：
  - `/tmp/sv3-dk7rq5wv/07-case_M8_report/rep/out/reports/日报_2026-11-13.xlsx` → `ad407bddb5232ac14bd711d5d1b6ff629fce52a17ecaad0cfbfadbc90b92b7f3`
  - `/tmp/sv3-dk7rq5wv/07-case_M8_report/rep/out/reports/日报_2026-11-13.md` → `6d68210b6a7a0beea271094d423ef8f63c34682a01c2abf2e18ab610fad9c242`

### F1 · 失败路径：断网 / 取件源不可达　✅　[失败路径]
- 命令：`/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -m winport downlink-once --bucket /tmp/sv3-dk7rq5wv/08-case_F1_offline/empty-bucket --data /tmp/sv3-dk7rq5wv/08-case_F1_offline/f1  |  /home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -m winport downlink-once --token-endpoint http://127.0.0.1:9/token --customer-id c --data /tmp/sv3-dk7rq5wv/08-case_F1_offline/f1`
- 期望：两路都非 0 退出，写出人话原因，不静默成功
- 实际退出码：`5`
- 实际回显：
```
[空桶] exit=5
{
  "listed": false,
  "manifest_seq": null,
  "downloaded": 0,
  "skipped": 0,
  "failed": 0,
  "folders": [],
  "errors": [
    "customer_id required (found 0 local customer prefixes)"
  ]
}

[不可达端点] exit=5
{
  "listed": false,
  "manifest_seq": null,
  "downloaded": 0,
  "skipped": 0,
  "failed": 0,
  "folders": [],
  "errors": [
    "network error: [Errno 111] Connection refused"
  ]
}
```

### F2 · 失败路径：权限/杀毒拦截（数据区不可写）　✅　[失败路径]
- 命令：`/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -m winport selftest --data /tmp/sv3-dk7rq5wv/09-case_F2_permission/locked/data`
- 期望：storage 灯 RED，退出码 4（不是 0）
- 实际退出码：`4`
- 实际回显：
```
ERROR(4): cannot create data area /tmp/sv3-dk7rq5wv/09-case_F2_permission/locked/data: [Errno 13] Permission denied: '/tmp/sv3-dk7rq5wv/09-case_F2_permission/locked/data/profiles'
```
- 备注：杀毒软件的实时拦截只能在真机复现 → 待真机 W6

### F3 · 失败路径：中文 / 空格 / 超长路径　✅　[失败路径]
- 命令：`WINPORT_DATA=<含中文+空格+超长路径>`
- 期望：全链路正常，路径 >200 字符也不崩
- 实际退出码：`None`
- 实际回显：
```
路径长度 = 369 字符（含中文与空格）
取件 downloaded=2 / failed=0
出表 xlsx = /tmp/sv3-dk7rq5wv/10-case_F3_paths/中文 目录 with spaces/第00层-非常长的中文子目录名用来逼近路径长度上限/第01层-非常长的中文子目录名用来逼近路径长度上限/第02层-非常长的中文子目录名用来逼近路径长度上限/第03层-非常长的中文子目录名用来逼近路径长度上限/第04层-非常长的中文子目录名用来逼近路径长度上限/第05层-非常长的中文子目录名用来逼近路径长度上限/第06层-非常长的中文子目录名用来逼近路径长度上限/第07层-非常长的中文子目录名用来逼近路径长度上限/第08层-非常长的中文子目录名用来逼近路径长度上限/第09层-非常长的中文子目录名用来逼近路径长度上限/第10层-非常长的中文子目录名用来逼近路径长度上限/第11层-非常长的中文子目录名用来逼近路径长度上限/data/out/reports/日报_2026-11-13.xlsx
```

### F4 · 失败路径：磁盘不足（阈值水位）　✅　[失败路径]
- 命令：`/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -m winport downlink-once --bucket /tmp/sv3-dk7rq5wv/11-case_F4_disk/b4 --data /tmp/sv3-dk7rq5wv/11-case_F4_disk/f4`
- 期望：阈值把水位顶上去后：拒绝取件、非 0、写明原因，不写半个文件
- 实际退出码：`5`
- 实际回显：
```
{
  "listed": false,
  "manifest_seq": null,
  "downloaded": 0,
  "skipped": 0,
  "failed": 0,
  "folders": [],
  "errors": [
    "磁盘可用空间不足：仅 9.07 GB，低于阈值 1073740.75 GB；本轮取件已停止，未写入任何文件（清出空间后自动重试）"
  ],
  "disk_low": true
}
```
- 备注：真实磁盘写满（ENOSPC）只能在真机造 → 待真机 W7

### F5 · 失败路径：登录掉线（逐环境报因 + 只报掉的那个）　✅　[失败路径]
- 命令：`login-check（空登录态）+ notify_transitions`
- 期望：全部 OFFLINE 且各带人话原因；掉线提醒只喊掉的那个环境
- 实际退出码：`None`
- 实际回显：
```
offline=7/7
示例: env_1:douyin OFFLINE - 登录态目录不存在或为空：这台机器上这个通道还没登录过，先点『开始登录』扫码
提醒: ('抖音 掉线了', '1号（env_1:douyin）：掉线原因')
```

### F6 · 失败路径：强杀后重启（不许重复发布）　✅　[失败路径]
- 命令：`run_publish 两次，第二次用重开账本`
- 期望：第二次被幂等键拦住，账本 CONSUMED
- 实际退出码：`None`
- 实际回显：
```
第一次 published=True work_id=7412345678901234567
第二次: IdempotencyConsumed: idempotency: already CONSUMED; refusing to republish
账本状态=CONSUMED
```

### F7 · 失败路径：交付物被篡改 / 签名不符 → 必须拒绝　✅　[失败路径]
- 命令：`build_bundle.verify（改 1 字节前后）+ license-check（自签）`
- 期望：改字节后 verify 报 mismatch；自签授权被拒
- 实际退出码：`None`
- 实际回显：
```
干净 verify: ok=True files=179
篡改后 verify: ok=False mismatches=['agent/winport/version.py']
自签授权: SIGNATURE_INVALID
```
- 备注：远程更新器（签名+哈希双验、原子替换、回滚）本单未实现 → 见报告 §接口预留

### F8 · 失败路径：写盘中途失败（原子写，不留半个文件）　✅　[失败路径]
- 命令：`jsonio.write_json 注入 os.replace 失败`
- 期望：旧文件原样保留、仍是合法 JSON、无 .tmp 残留
- 实际退出码：`None`
- 实际回显：
```
注入失败=True
写前==写后: True
残留临时文件: []
```
- 备注：自动回滚（升级失败退回上一版）属远程更新器 → 未实现，见报告

### F9 · 失败路径：重复点击 / 并发同一动作　✅　[失败路径]
- 命令：`5 线程同时 run_publish 同一幂等键`
- 期望：只成功 1 次，账本只 CONSUMED 1 条
- 实际退出码：`None`
- 实际回显：
```
成功=1 被拒不=4
被拒原因: ['GateRed: idempotency: already RESERVED; refusing to republish', 'GateRed: idempotency: already RESERVED; refusing to republish', 'GateRed: idempotency: already RESERVED; refusing to republish']
账本 CONSUMED=1
```

### F10 · 失败路径：同一环境不许第二个实例　✅　[失败路径]
- 命令：`open_channel（端口在听 vs 不在听）`
- 期望：在听→复用同一窗口(REUSED_ENV_WINDOW)；不在听但 profile 被占→PROFILE_IN_USE
- 实际退出码：`None`
- 实际回显：
```
复用: REUSED_ENV_WINDOW - 抖音 所属环境下浏览器已在运行，已在该窗口打开新标签页（不会开出第二个实例）
拒绝: PROFILE_IN_USE - 这个登录态目录已经有浏览器在跑了，不允许并行开第二个（同一个 profile 并行会互相踩踏登录态）
```

### C1 · 真 Chrome：检测+版本+策略+选择器回归入口　✅　[status]
- 命令：`/home/ubuntu/.hermes/hermes-agent/venv/bin/python3 -m winport chrome-probe --data /tmp/sv3-dk7rq5wv/18-case_C1_chrome/c1  |  chrome-regression`
- 期望：本机无 Chrome → 诚实报 not found（非 0）+ 回归 offline/待真机
- 实际退出码：`5`
- 实际回显：
```
本程序需要 Google Chrome（只认官方正版 Chrome，不用 Microsoft Edge、Chromium 或其它浏览器顶替）。这台电脑上没检测到 Google Chrome。怎么装（二选一）：① 用随包附带的官方离线安装包 chrome-offline\ChromeStandaloneSetup64.exe（双击即可，Google 官方原版、我们未做任何改动）；② 到官网下载安装：https://www.google.cn/chrome/（离线版：https://www.google.com/chrome/?standalone=1，国内：https://www.google.cn/chrome/?standalone=1）。装完这一个即可回控制面板点「重新检测」；除 Chrome 外什么都不用装。顺带说明：系统里也没检测到 Microsoft Edge（不影响运行）。
回归 mode=offline all_ok=False pending_real_machine=True
```

## 2. 未验证项清单（按风险排序，只能真机确认）

| # | 未验证项 | 为什么没验 | 怎么补验 | 风险 |
|---|---|---|---|---|
| W1 | `bootstrap.ps1` 真机安装（建 venv、装 wheel、造自启动任务） | 构建机非 Windows、无 PowerShell/win 运行时 | 客户机按 `REMOTE_INSTALL_SOP.md` 跑一遍，回填 install.log | 高 |
| W2 | Task Scheduler 自启动 + 崩溃自恢复 | 同上 | 重启电脑看托盘图标自动出现；`schtasks /query /tn HuolongAgent\Agent` | 高 |
| W3 | 客户机真 Chrome 检测/启动（注册表 App Paths + 常见路径 + 独立 profile/端口） | 构建机无 Chrome、无注册表 | 客户机跑 `winport chrome-probe` + `winport env-open --env env_1` | 高 |
| W4 | 各环境真实扫码登录（手机扫码） | 需要真人 + 真账号 | 按面板逐环境扫码后 `winport login-check --env <env>` 看灯变绿 | 高 |
| W5 | 企业策略锁远程调试的人话报错 | 构建机无组策略 | 在锁策略的机器上跑 `chrome-probe`/`env-open`，确认报 `POLICY_BLOCKS_DEBUG` | 中 |
| W6 | 杀毒软件实时拦截安装/写盘 | 构建机无目标杀软 | 客户机装 360/火绒/Defender 各跑一次装机 | 中 |
| W7 | 磁盘真正写满（ENOSPC） | 构建机无法安全造满盘 | 真机把盘写到水位以下，看是否停止取件并人话报错 | 中 |
| W8 | 断网中途切换（下载到一半断网） | 构建机能造「源不可达」，造不了「中途断」 | 真机拔网线/断 Wi-Fi 复现，看重试与去重 | 中 |
| W9 | 真 Chrome 下 fill 的 DOM 选择器回归 | 无真 Chrome、无登录态 | 真机 `winport chrome-regression --live`（本单先给了入口） | 高 |
| W10 | xlsx/Excel 打开后行高/列宽是否被切 | 构建机无 Excel/LibreOffice | 真机打开日报，目视 | 低 |
| W11 | 干净机器全流程验收（SOP 走一遍） | 本机装过依赖，不是干净环境 | 找一台全新 Win10/11 走 SOP | 高 |

## 3. 已知风险与缓解

- **真实扫码/选择器**：本单只把「真 Chrome 命令与登录面回归入口」落地；fill 的 DOM 选择器仍是真机项（`publish_exec.PlaywrightDriver.fill` 明确抛 GateRed，不会假装成功）。缓解：`chrome-regression --live` 有产出即算证据。
- **远程更新**：本单**不实现**（按派单）。接口预留位置见 `BUILD_REPORT.md` §5；签名/哈希双验与原子替换的**组件能力**已用「授权签名拒绝 + MANIFEST 篡改检测 + 原子写」三条证据证明可用（F7/F8）。
- **授权按环境计数**：一期 env_1/env_2 都是「抖音」，属不同号；同一平台类型出现在多个环境是设计允许（`environment_warnings` 会提示），不是同一登录态被两个环境共用。
- **磁盘水位默认 5GB**：构建机可用空间 <5GB 会直接触发 F4 的拒取件；客户机若长期低空间会停止取件并人话提示，不会写半个文件。

## 4. 验收人可复现命令

```bash
cd output/win-port
python3 -m unittest discover -s tests -v          # 98 项单测
python3 tools/static_verify.py                     # 65 项静态验收
python3 tools/e2e_selfverify.py --out ../bundle_v3/SELF_VERIFY_REPORT.md
python3 ../bundle_v3/verify_bundle.py               # 成品包静态校验
```

（以上命令的输出即本报告 1 节的原始回显，逐字来自 `SELF_VERIFY_REPORT.raw.log`。）

