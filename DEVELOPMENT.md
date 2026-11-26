> 更新：2026-11-26 ｜ 范围：仓库全部核心源码与运维脚本

## 目录

- [1 架构](#1-架构)
- [2 关键参数与非默认配置](#2-关键参数与非默认配置)
- [3 脚本](#3-脚本)
- [4 命令清单](#4-命令清单)
- [5 非直观逻辑](#5-非直观逻辑)
- [6 坑与教训](#6-坑与教训)
- [7 当前状态](#7-当前状态)

快速入口：了解设计与踩坑看 5、6；常用调试与验收命令看 4；理解结构看 1。

## 1 架构

| 模块 | 路径 | 职责 |
|---|---|---|
| 纯 HTTP 协议核心 | `switched_core.py` | 封装面板快照解析、每日连签、会话/VPS 保活、`adfocus` 与 `linkvertise` 纯 HTTP/GraphQL 直刷 |
| Web 控制台与调度器 | `web_app.py` | 提供 `127.0.0.1:50914` 前后端一体免密控制台、单总开关守护调度、快照双层缓存、每日随机偏移定时与后台保活线程 |
| 配置模板 | `config.example.yaml` | 端口、面板 Cookie、任务开关、保活与定时默认配置模板，首次启动自动复制为 `config.yaml` |
| Python 依赖清单 | `requirements.txt` | 声明 `requests`、`PyYAML`、`beautifulsoup4`、`pycryptodome` 最低版本 |
| 环境初始化脚本 | `setup.sh` | 创建 `--system-site-packages` 虚拟环境 `.venv`、安装预编译 `pycryptodome` 与依赖并自检导入 |
| 服务重启脚本 | `restart.sh` | 并发锁、内存 AST 语法预检、`/healthz` 任务占用保护、`/proc` 精准停服与 `nohup -B -u` 拉起探活 |
| Cookie 复制示例图 | `assets/cookie_guide.png` | 静态图示资源，通过 `/assets/cookie_guide.png` 内嵌展示于「Cookie与设置」页底部 |

- 启动与缓存流：`web_app.py::_bootstrap` 复制 `config.example.yaml` → 读取 `.snap_cache.json` 预热 `STATE["snap"]` → 启动 `sched_loop` 与 `keepalive_loop` 后台线程 → `ThreadingHTTPServer` 仅监听 `127.0.0.1:50914`（无密码登录，远程通过 SSH 端口转发访问）。
- 单总开关守护流：`POST /api/run`（手动）或 `sched_loop`（定时）打开总开关 → 持续循环：刷新 `snapshot()` 并更新缓存 → 自动领取每日连签 → 检查所有启用项目是否今日全部完成（完成则自动关闭总开关）→ 按 `quote` 降序挑选未满额且未冷却渠道执行；若未满额渠道均在冷却中，保持总开关开启并可中断休眠至 `min(cool_until) + 3s` 后自动继续。
- 首屏秒开与异步刷新流：前端加载/F5 时先从 `localStorage('sw_snap_cache')` 零等待渲染上次缓存 → `poll()` 同步服务端 `.snap_cache.json` 与运行态 → 若已配置 Cookie，自动发一次 `POST /api/refresh {"async": true}`（带 `refreshing` 并发去重）在后台拉取最新面板快照并落盘更新。
- `adfocus` 直刷流：`snapshot()` 刷新 `_token` → `POST /tokens/earn`（`allow_redirects=False` 取 `Location`，避免污染面板 Session）→ 独立 `ad_sess` 拉取页面并提取 `my_key`/接口前缀（`/serve/nx` 或 `/serve`）→ 依次 `POST /credit` → `/state`(a) → 补足 8.5s 后 `/state`(b) → `/skip` → 补足 11s 后经 `urljoin` 回跳 `back_url` 或 `/next?t=` 入账。
- `linkvertise` 直刷流：`snapshot()` 获取 `anchor` 与 `var userId` → `lv_encode` 加密目标地址 → 后台异步触发 `TABOOLA_SYNC` 获取 `user_id` → 独立 `lv_sess` 循环 `getContent` 处理 `WaitTask`/`AdTask`（固定取 `ads[0]`，若含 Taboola 负载则异步上报 `available`/`visible` 后停留 `15.6~17.6s`，携带 `cqreferrer` 头提交 `completeTask`，失败时自动兜底无 `completion_token` 重试一次）→ 拿到 `DetailPageTargetData.url` 后 `_hit_back` 入账。
- 保活与 Cookie 回写流：`keepalive_loop` 每 15s 巡检，按间隔调用 `ping_session()`（`GET /tokens`）与 `ping_vps()`（缓存 `/vps` UUID 列表后 `GET /vps/<uuid>`）→ 每次面板请求后 `_sync_cookies()` 将新 `Set-Cookie` 原子回写 `config.yaml`。
- 重启部署流：`restart.sh` 获取 `.restart.lock` → `python -B` 内存 `ast.parse` 检查 → `GET /healthz` 确认 `running=false` → 按 `/proc/<pid>/cwd` 与 `cmdline` 终止旧 `web_app.py` → 等 50914 端口释放 → `nohup .venv/bin/python -B -u web_app.py >> web_app.log 2>&1 < /dev/null 9>&- &` → 轮询 `/healthz` 确认就绪。

## 2 关键参数与非默认配置

| 名称 | 值或位置 | 用途 | 锚点 |
|---|---|---|---|
| 目标运行环境 | Linux / Debian 12（含 arm64） | 定时基准按宿主机本地时区 | `setup.sh`、`web_app.py` |
| 监听地址与端口 | `BIND_HOST="127.0.0.1"`，`server.port=50914` | 无密码设计，强制仅本机回环监听；远程通过 SSH 端口转发访问 | `web_app.py::BIND_HOST`、`config.example.yaml:4-6` |
| 探活端点 | `GET /healthz` → `{"ok": true, "running": bool}` | 供 `restart.sh` 判断服务存活与总开关状态 | `web_app.py::Handler.do_GET` |
| 快照磁盘缓存 | `.snap_cache.json`（已入 `.gitignore`） | 原子缓存最近一次有效快照，服务重启后首屏直接秒出 | `web_app.py::SNAP_CACHE_PATH`、`web_app.py::_set_snap` |
| 面板 Cookie 与自动回写 | `config.yaml::panel.cookie` | 长期免登录依赖 `remember_web_*` + `XSRF-TOKEN`，请求后服务端自动补发/刷新会话 Cookie 并原子回写 | `config.example.yaml:9-12`、`web_app.py::_persist_cookie` |
| 服务端渠道能力边界 | 自动：`adfocus`、`linkvertise`；仅展示：`cuty`、`vylo`、`cpx_research` | 服务端不做 Turnstile/PoW/问卷绕过 | `switched_core.py::SUPPORTED_PROVIDERS`、`switched_core.py::MANUAL_ONLY_PROVIDERS` |
| 渠道轮间隔 | `gap_min=25`（下限 5），`gap_max=60` | 正常入账后轮间随机等待；某渠道失败切到下一可用渠道时走 `3~5s` 快速切换 | `config.example.yaml:20-21`、`web_app.py::start_task` |
| 失败冷却阶梯与跨天重置 | 1 次 `120s`、2 次 `300s`、3 次及以上 `3600s`；风控 `cool_s` 优先 | 不再永久熔断，到期自动重试；跨自然日自动清空历史失败记录 | `web_app.py::_bump_fail`、`web_app.py::_reset_stale_fails` |
| 每日定时与随机偏移 | `schedule.time="08:30"`，`jitter_min=30`（范围 `0~120`），触发窗 `30m` | 偏移种子 `sw\|<YYYY-MM-DD>\|<HH:MM>`，按日固定；到点自动打开总开关并写 `last_run_date` | `web_app.py::sched_target_for_day`、`web_app.py::sched_loop` |
| 会话与 VPS 保活 | `session_interval_min=10`（下限 2），`server_interval_sec=60`（下限 20） | `keepalive_loop` 每 15s 巡检，分别请求 `/tokens` 与 `/vps/<uuid>` | `config.example.yaml:25-29`、`web_app.py::keepalive_loop` |
| `adfocus` 节拍参数 | `state b` 补足 `8.5s`，`back_url` 补足 `11.0s` | 模拟浏览器停留时长，避免服务端过快拒付 | `switched_core.py::SwitchedClient.run_adfocus` |
| `linkvertise` 端点与加密 | `LV_GRAPHQL`、`LV_JS_URL`、`TABOOLA_SYNC`；头部 `>66B` RSA-OAEP(SHA-256) | 匿名可跑，`tasks.lv_auth`/`lv_ut` 为可选增强凭据 | `switched_core.py::LV_GRAPHQL`、`switched_core.py::SwitchedClient.lv_encode` |
| `linkvertise` 风控与超时 | 最多 `26` 轮、墙钟 `240s`；`remainingWaitingTime > 90s` 冷却 `max(left+60, 300)`；广告停留 `15.6~17.6s` | 匹配官方前端 `15s` 倒计时（`va=15e3`）；遇长等待闸门或连续 2 次同 `WaitTask` 未推进立即止损 | `switched_core.py::SwitchedClient.run_linkvertise` |
| 虚拟环境安装策略 | `python3 -m venv --system-site-packages .venv` + 清华源 `--only-binary=:all: pycryptodome` | 避免 arm64 源码编译；优先安装预编译 wheel | `setup.sh:5-11` |

## 3 脚本

| 路径 | 输入 | 触发方式 | 产物位置 |
|---|---|---|---|
| `setup.sh` | `requirements.txt`、系统 `python3` | 手动 `bash setup.sh` | `.venv/` |
| `restart.sh` | `.venv/bin/python`、`switched_core.py`、`web_app.py` | 手动 `bash restart.sh` | `.restart.lock`、`web_app.pid`、`web_app.log` |

## 4 命令清单

### 构建

```bash
# 初始化虚拟环境并安装预编译 pycryptodome 与依赖
bash setup.sh
```

### 运行

```bash
# 安全重启后台服务（含 AST 预检、任务运行保护、精准停服与探活）
bash restart.sh

# 前台调试启动（Ctrl+C 停止；-B 禁用 __pycache__，-u 无缓冲实时输出）
.venv/bin/python -B -u web_app.py
```

### 测试

```bash
# 内存 AST 语法检查（不落盘 __pycache__）
.venv/bin/python -B -c "import ast, pathlib; [ast.parse(pathlib.Path(f).read_text(encoding='utf-8')) for f in ('switched_core.py', 'web_app.py')]"
```

### 验证

```bash
# 检查监听地址（仅 127.0.0.1:50914）、健康状态与示例图静态资源
ss -lntp | grep 50914
curl -fsS http://127.0.0.1:50914/healthz
curl -fsSI http://127.0.0.1:50914/assets/cookie_guide.png | head -n 5

# 实时查看日志
tail -f web_app.log
```

## 5 非直观逻辑

- `switched_core.py::SwitchedClient.snapshot` — 渠道今日满额后，面板 HTML 会撤掉 `input[name="provider"]` 与 `/tokens/return/` 锚点，必须保留按 `.pct-way-name` 正则兜底识别，否则满额渠道会从快照与 UI 消失。
- `switched_core.py::SwitchedClient.lv_encode` — 复刻 `linkvertise.js` 编码逻辑：对 `unquote(target_url)` 逐字符累加 UTF-8 字节直到 `len(head) > 66` 截断，仅头部用 `linkvertise.js` 内的 RSA 公钥做 PKCS1-OAEP(SHA-256) 加密并 Base64，剩余尾部原样拼接。
- `switched_core.py::_op_id` — Linkvertise GraphQL 强制校验 100 字符 `action_id`（3 个 UUIDv4 按 `[8,4,4,4,20,4,4,4,20,4,4,4,4]` 拼接截断）及 `task_args.additional_data.taboola`，缺失时服务端只返回 `PremiumTask` 无法推进。
- `switched_core.py::SwitchedClient._lv_page_url` 与 `completeTask` — Linkvertise 后端在 `completeTask` 强校验自定义请求头 `cqreferrer`（值同 `taboola.url`），且 `r=` 参数必须使用 `quote(enc_hash, safe="")` 对 `/` 编码（与浏览器 `encodeURIComponent` 一致）；缺 `cqreferrer` 或 `/` 未编码会导致 `completeTask` 返回通用错误 `some errors occurred!`。
- `switched_core.py::SwitchedClient.run_linkvertise` — 官方前端 `AdTask` 固定渲染并校验 `ads[0]`（即使 `ads[0]` 为 `SUPPLY_FEED`、`ads[1]` 为 `TABOOLA`，提交 `ads[1]` 的 `completion_token` 会被服务端拒掉）；需固定使用 `ads[0]`，并在带 `completion_token` 报错时兜底无 `completion_token` 重试一次（对齐官方 `completeAdWithoutConversion`）。Taboola `user.sync`/`available`/`visible` 采用后台线程异步上报，不阻塞主倒计时。
- `switched_core.py::SwitchedClient.run_adfocus` — `POST /tokens/earn` 必须用 `allow_redirects=False` 提取 `Location` 后再交给独立 `ad_sess` 请求 `adfoc.us`，防止 `adfoc.us` Cookie 种进面板 `self._session` 或导致后续 `/credit`、`/state`、`/skip` 缺失会话 Cookie；同时按 HTML 是否含 `/serve/nx/` 动态切换新旧接口。
- `web_app.py::start_task` — 唯一总开关守护线程：不设固定轮次上限，未满额渠道均在冷却时不退出，而是可中断等待至最早冷却到期自动续跑；监控到所有启用渠道今日满额且连签已完成时自动关闭总开关。
- `web_app.py::sched_target_for_day` — 每日随机偏移用固定种子 `sw|<YYYY-MM-DD>|<HH:MM>` 生成，确保服务重启或前端轮询时展示的当天目标时间稳定不变；`sched_loop` 允许 `target` 起 30 分钟内触发以容忍短暂重启。
- `restart.sh:13`、`restart.sh:50` — 语法预检使用 `python -B -c "import ast..."` 内存解析且启动加 `-B -u`（禁用 `__pycache__` 落盘并启用 stdout 无缓冲实时写 `web_app.log`）；后台 `nohup` 行必须追加 `< /dev/null 9>&-` 显式关闭子进程 FD 9，防止子进程继承 `exec 9>"$LOCK"` 导致 `.restart.lock` 永久死锁。
- `switched_core.py::parse_cookie_string`、`web_app.py::do_update_cfg` — 自动剥离浏览器 DevTools 粘贴带入的前导 `Cookie:`、换行与 Tab，拦截含 `…`/`...` 的截断串；更新面板 Cookie 时清空 `_VPS_CACHE = None`，强制下次 VPS 保活重拉 `/vps` UUID 列表。

## 6 坑与教训

- 已否决方案：在服务端自动绕过 `cuty`、`vylo`、`cpx_research` → 分别涉及 Cloudflare Turnstile + 行为质检、PoW + 人机验证、人工问卷，纯 HTTP 无法稳定通过，改为仅展示进度、浏览器人工处理。
- 已否决方案：在 arm64 Linux 源码编译 `pycryptodome` → 耗时且依赖编译链，改为 `setup.sh` 指定 `--only-binary=:all:` 安装预编译轮子。
- 踩过的坑：Linkvertise `completeTask` 持续报 `{"message": "some errors occurred!"}` → 两层叠加导致：① 漏发 `cqreferrer` 请求头且 `quote()` 默认未编码 `/`；② 尝试跳过 `ads[0]` 选 `ads[1]` 触发服务端校验失败。补齐 `cqreferrer` + `quote(..., safe="")` 并固定取 `ads[0]`（加无 token 兜底）后恢复 `status: "DONE"`（`switched_core.py::SwitchedClient.run_linkvertise`）。
- 踩过的坑：会话保活报 `HTTP 404: .../dashboard` → 面板实际无 `/dashboard` 路由，已改为 `GET /tokens`（`switched_core.py::SwitchedClient.ping_session`）。
- 踩过的坑：Android / 移动设备运行 Linux 容器时熄屏导致 `sleep(16s)` 被拉长到十几甚至四十分钟 → 无 WakeLock 时 Android 内核进入 Suspend 冻结用户态定时器，且 Wi-Fi 开启 Suspend Optimization 导致熄屏丢包；需在宿主机关闭 Doze（`dumpsys deviceidle disable`）、关闭 Wi-Fi 熄屏优化（`wifi_suspend_optimizations_enabled 0`）并保持充电不休眠或持有 WakeLock。
- 踩过的坑：`web_app.log` 长期为 0 字节 → `log_line()` 仅写内存且 `restart.sh` 未加 `-u`；已改为 `log_line()` 同步 `print(..., flush=True)` 且启动加 `-B -u`（`web_app.py::log_line`、`restart.sh:50`）。
- 踩过的坑：单次运行遇渠道冷却直接收工 + 失败 `n >= 4` 永久锁死 → 已重构为单总开关守护模式，冷却期间保持总开关开启并定时续跑，解除永久锁死并支持跨天自动重置（`web_app.py::start_task`、`web_app.py::_reset_stale_fails`）。
- 踩过的坑：跨端共享目录导入测试后产生不同属主 `__pycache__/` 导致 `Permission denied` → 全程使用 `python -B` 与内存 `ast.parse` 预检规避（`restart.sh:13`、`restart.sh:50`）。
- 踩过的坑：再次运行 `bash restart.sh` 误报并发锁冲突 → `nohup` 子进程继承了 `flock` 的 FD 9，追加 `9>&-` 解决（`restart.sh:50`）。
- 踩过的坑：从浏览器 DevTools Headers 拖选复制 Cookie 被截断为 `…` → 需切换「原始 / Raw」视图复制整行；界面「Cookie与设置」底部已内置示例图与提示，后端亦拦截含省略号的截断串。
- 脆弱点：`panel.switched.cloud` 若改动 `.pct-way` / `.pct-balance` / `Daily streak` / `var userId` 页面结构或 `/tokens/earn` 路径，`snapshot()` 与直刷会失效。
- 脆弱点：`STATE["fails"]` 冷却计数与 `_VPS_CACHE` 仅驻留内存，重启后冷却状态清零；频繁重启可能加剧渠道风控。
- 脆弱点：若粘贴的面板 Cookie 缺少 `remember_web_*`，面板会话可能较快过期导致保活与定时任务报 `SessionExpiredError`。

## 7 当前状态

### 已完成

- 纯 HTTP 协议核心 `switched_core.py` 已完成真机实测与协议对齐修复（`/tokens` 保活、`adfocus` Cookie 隔离与直刷入账、`linkvertise` `cqreferrer` + `ads[0]` + 无 token 兜底 + 异步 Taboola）。
- `web_app.py` 已完成本地无密码改造（强制 `127.0.0.1:50914`）、单总开关守护模式（冷却自动续跑、全满自动关闭、定时自动开开关）、双层快照缓存（`.snap_cache.json` + `localStorage` 首屏秒开 + 刷新网页自动异步拉新）以及「Cookie与设置」页底部示例图展示。
- 运维脚本与开源发布文件（`setup.sh`、`restart.sh`、`config.example.yaml`、`requirements.txt`、`.gitignore`、`README.md`、`DEVELOPMENT.md`、`assets/cookie_guide.png`）均已完成脱敏检查、AST 语法检查与本地隔离 HTTP 冒烟测试。
