# 2026-11-03 二次复核修复记录

后续查询恢复修复见[2026-11-03 开抢恢复验收](2026-11-03-opening-recovery.md)；本文保留当时参数与证据。

后续统一线程架构已取代本文当时的三轮同步兜底及相关测试（见[统一线程验收](2026-11-03-unified-workers.md)与[检查小修记录](2026-11-04-audit-minor-fixes.md)）；本文保留当时状态与验证结果。

后续策略更新（2026-11-03）：当前采用[减少无效查询方案](2026-11-03-query-reduction.md)，最高频为开放前 5 秒至后 9 秒，支持无待抢目标时提前降频及普通历史 30 秒缓存。本文保留该次检查或实战当时的参数与验证结果；当前操作以[使用指南](../usage.md#开抢与拥塞恢复)为准。

本记录对应当日第二轮项目检查（离线代码审查、文档一致性核对与真实学校服务器只读复测）发现的问题及修复。本轮**未提交或取消真实预约、未启动真实开抢、未发送邮件**；真实服务器证据见文末。

## 复核发现

1. **主要**：开抢快速路径的待确认核对新增“先探测活动列表”后，列表探测拿到 429／服务端等待时异常只被暂存，随后仍立即发出历史查询；与 `docs/requirements.md` 的“收到 429 或带等待值的过载响应后先等待，再发起历史探测或其他业务请求”冲突。已用离线构造复现“列表 429 → 紧接历史请求”的顺序。
2. **次要**：`reservation_unconfirmed` 的去重标记是进程内变量，不是按待确认段持久化：同一进程第二段待确认不再记录；重启后计数被快速路径清 0 时同一段可能重复记录。
3. **次要**：认证被拒兜底去重只在首次运行成立：重启且 `task_failed` 已持久化时，客户端与兜底各写一行 `authentication_rejected`。
4. **提示**：DNS 槽位归还只覆盖 `Thread.start()` 失败，未覆盖 `Thread(...)` 构造异常。
5. **提示**：有界 DNS 连接替换了 urllib3 的 `_new_conn`，未复刻其 `sys.audit("http.client.connect", ...)` 审计事件。
6. **文档**：① 复核修复记录称复测“未输出标题”，与自身 `--check` 证据矛盾；② 2026-11-01 实战记录的开抢期扫描/轮询绝对计数与 0.1 秒目标间隔、线程重建清零不一致；③ `requirements.md`／`usage.md` 残留“DNS 不受硬中断”的旧推断，与同段“DNS 已纳入请求预算”矛盾。
7. **测试覆盖**：被删除的动态共享等待测试没有等价替代；预热预算断言未校验上界；上述 1–3 项无回归。

## 修复内容

### 1. 服务端等待先于历史查询（`app.py`）

列表探测捕获的 `RemoteError` 若为服务端要求的等待（429／`Retry-After`），先走 `abandon_check` 进入既有等待/退避路径，不再紧接着调用 `query_mine()`。探测结果仍在同一次核对中复用，其余探测失败路径（历史正证据可确认）不变。

### 2. 待确认事件按段持久化去重（`app.py`）

新增 `task.data['pending_unconfirmed_notified']`：新待确认段创建时清除；首次完成核对时与 `pending_checks` 同一次原子保存；确认、自动释放、提交回复“已预约”、连接超时回退与人工 `--resolve-pending` 清理时一并清除；快速路径 miss 只清 `pending_checks`、保留本段标记。这样同一段在进程内和重启后都只记录一次，新段仍会记录。

### 3. 认证被拒事件按进程去重（`app.py`）

`announced_here.add(alert)` 移到持久化告警去重的提前返回之前：重启后已持久化 `task_failed` 时，客户端已记录的事件仍只写一行，邮件继续由持久化键去重；首次运行与修正凭据后恢复的行为不变。

### 4. DNS 槽位在构造异常时归还（`client.py`）

`Thread(...)` 构造与 `start()` 放进同一异常路径，任一失败都归还 8 个解析槽位之一。

### 5. 连接审计事件（`client.py`）

有界连接建立成功后调用 `sys.audit('http.client.connect', self, self.host, self.port)`，与 urllib3 默认建连路径的可观察行为一致。

### 6. 文档更正

- `docs/acceptance/2026-11-03-review-fixes.md`：复测环境说明改为“`--check` 按设计在本机输出逐场标题；除此之外未输出或记录凭据/Cookie/票据/活动 ID，本记录不含标题”。
- `docs/acceptance/2026-11-01-live-run.md`：扫描/轮询行改为“原始诊断行数／最后一代线程计数”，新增计数口径更正说明，同步修正“其余 8030 次使用 poller 快照”的表述；可靠结论以主循环列表请求、同步回退次数与“无 429/5xx、无待确认/重复提交”为准。
- `docs/requirements.md`／`docs/usage.md`：DNS 硬中断表述主语改为“纯 CPU 解析”，并注明 DNS 主线程等待受同一请求预算约束；`requirements.md` 的 2026-11-01 摘要删除不可靠绝对计数并指向实战记录说明。

### 7. 测试补强

- `test_opening_pending_probe_server_wait_is_honored_before_history`：待确认快速路径探测到 429 后不再发历史请求，等待使用完整 `Retry-After`。
- `test_new_pending_episode_clears_a_stale_unconfirmed_notice`、`test_persisted_unconfirmed_notice_suppresses_a_restart_duplicate`：新段清标记、重启不重复；既有快速路径测试补充标记持久化断言。
- `test_explicit_bad_credentials_stop_without_repeated_login`：补充“重启后仍被拒”只写一行事件、不重发邮件。
- `test_dns_resolver_slot_is_released_when_thread_construction_fails`：构造失败归还槽位。
- `test_list_reader_wait_learned_mid_scan_stops_the_history_reader`：恢复“列表线程中途学到 120 秒等待，历史线程不得在剩余等待内重启”的动态覆盖；预热预算测试补充 `≤ PREWARM_TIMEOUT` 上界与 `prewarm_history` 诊断断言。
- **测试复核补强**：429 回归的历史请求计数最初写成“限流标志仍为真时计数”，而标志在返回 429 时已清零，移除保护时该测试会漏报；现改为在 429 之后计数，并已做负向验证——临时移除保护后该测试失败（实际多出 1 次历史请求），恢复保护后通过。同类误报防护：新待确认段测试断言确有两笔提交、重启测试断言确实完成过核对（`reservation_checked`）。

## 验证

| 检查 | 结果 |
| --- | --- |
| `uv run --locked pytest -q` | **745 通过、3 跳过**（80.14 秒；跳过为 2 条 Unix 信号、1 条 Windows 文件替换限制） |
| `uv run --locked mypy seu_reserve` | 12 个源文件通过 |
| `node --check seu_reserve/web_assets/app.js` | 通过 |
| `git diff --check` | 通过（仅 LF/CRLF 提示） |
| 真实学校服务器只读复测（修复后） | 通过（见下） |

### 真实学校服务器只读复测

修复后复测（2026-11-03 14:42–14:43，Asia/Shanghai）：Windows 本机、当前网络经学校网关、TLS 证书校验、使用本机已纳管的一个学校账号；未输出或记录凭据、Cookie、票据、活动 ID；标题按 `--check` 设计仅在本机终端/本地日志出现。

- `run(check_only=True)`：退出码 0，`event=read_only_complete`，0.28 秒；请求 `index.do` → `queryMyActivityList.do` → `queryActivityList.do` → `vcode.do` 全部成功；筛选 7 场预约未开始、1 场类别未启用。
- 两个复制 Cookie 的独立只读会话并发 15 秒：活动列表 **141/141** 成功（中位 47 ms、P95 79 ms、最大 125 ms）；我的活动 **3/3** 成功（中位 63 ms、P95 110 ms）；无错误、无 429/5xx/超时。
- 有界 DNS 解析器在真实请求中调用 3 次，均在预算内（0 ms，缓存命中）。
- 未产生任务写入：账号 `task.json`、`diagnostics.jsonl`、`events.log` 保持 2026-10-31 状态；仅 `trust-cookies.json` 按正常登录流程刷新。未提交或取消预约，未发送邮件。

同日较早一轮（修复前基线，14:22–14:26）另完成的真实服务器只读证据：CLI `--check --account` 退出码 0；15 秒双只读会话活动列表 140/140、我的活动 3/3；在隔离数据根副本中运行完整预约引擎约 50 秒（静默登录成功、活动 8 条、候选 0、无提交/邮件）；Web 管理台真实登录后经管理进程启动账号 worker（认证→等待约 1.6 秒、活动 8 条、候选 0）、停止并优雅关闭，真实数据根未被写入。两轮结果一致。

## 边界与未验证

- 本轮修复的 429 先等待、待确认事件去重、认证被拒去重与 DNS 构造失败均为**离线构造验证**；真实学校服务器无法安全制造 429、待确认或重启后被拒场景，未在真实开抢中复测。
- 真实复测仍为只读：未提交或取消预约、未启动真实开抢、未验证待确认核对、未测试真实 DNS 故障、多账号并发、长期运行、Linux/systemd 或浏览器视觉。
- 2026-11-01 实战的原始 `diagnostics.jsonl` 只在服务器保留，本文只能标注计数口径冲突，未重算绝对值；需以服务器原始文件逐行复核。
- 测试跳过项与平台限制不变；测试时长以本轮最终运行 80.14 秒为准，当日早先记录的 82.97、80.72 秒分别为不同次运行。
