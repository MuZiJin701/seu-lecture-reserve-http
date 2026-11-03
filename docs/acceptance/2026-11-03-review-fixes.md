# 2026-11-03 复核修复记录

后续策略更新（2026-11-03）：当前采用[减少无效查询方案](2026-11-03-query-reduction.md)，最高频为开放前 5 秒至后 9 秒，支持无待抢目标时提前降频及普通历史 30 秒缓存。本文保留该次检查或实战当时的参数与验证结果；当前操作以[使用指南](../usage.md#开抢与拥塞恢复)为准。

本记录对应当日项目检查（离线回归 + 真实学校服务器只读复测）发现的问题及修复。本轮**未提交或取消真实预约、未启动真实开抢、未发送邮件**；真实服务器证据见文末。

## 修复内容

### 1. 开抢快速路径的待确认核对请求放大（主要）

**问题**：`status=pending`、开抢窗口内且后台活动列表无新鲜快照时，核对每轮先同步查询“我的活动”，随后列表快速路径返回 `none`，代码重置计数并只等待 0.1 秒。结果在无法完成核对的情况下约每 0.1 秒发出一次历史请求（离线构造 4.2 秒内约 8 次，旧路径约 1 次/8 秒），并因计数被反复重置而重复记录 `reservation_unconfirmed`。

**修复**（`app.py`）：
- 核对先探测活动列表来源，再决定是否支付历史查询：快速路径无快照时 `list_rows()` 返回 `none` 且无请求（直到三轮后的有界同步兜底），此时重置未决计数、保持 0.1 秒节奏并直接进入下一轮，不再查询历史；列表探测真实失败时仍按原逻辑查询历史并把失败交给既有中止/退避路径，保留“历史正证据可确认”的能力。
- `reservation_unconfirmed` 按待确认段去重：同一段待确认只记录一次，不再随计数重置反复记录。
- 探测结果在同一次核对中复用，不重复读取列表。

### 2. 预热查询预算被同步兜底上限压缩（次要）

`query_mine()` 在开抢窗口内统一套用 0.5 秒同步兜底上限，把预热查询文档承诺的 `min(1.5 秒, 距开抢剩余 −0.1 秒)` 实际压到 0.5 秒。现为 `query_mine(bounded=False)` 供预热使用，预热保持自身预算；其他同步历史兜底仍为 0.5 秒。

### 3. DNS 解析槽位在启动失败时泄漏（次要）

`_resolve_within()` 在 `Thread.start()` 抛错时 worker 不会执行，其 `finally` 无法归还槽位。现由启动方在异常路径归还，避免耗尽 8 个解析槽位；线程启动成功的路径行为不变。

### 4. 后台线程写本地日志的并发（次要）

新增 `poller_aligned` 回调在 poller 线程写 `diagnostics.jsonl`，与主循环的追加写入无锁。现 `record()`／`diagnostic()` 的本地追加共用 `_LOG_LOCK`，异常回退路径在锁外调用 `record()`，不会自锁。

### 5. 认证被拒绝兜底重复事件（提示）

客户端明确拒绝时已记录 `authentication_rejected`，`run()` 的兜底又记录一次。现兜底仅在本次进程尚未宣布该告警时补发，事件日志只保留一条；持久化告警仍跨重启去重邮件。

### 6. 文档与实现不一致（主要 + 次要）

- requirements：移除“后台线程至少冷却 5 秒后重启”的过时表述；重大告警清单补“认证被拒绝”；预热预算注明使用自身预算；DNS 超时补充“查询可重试、提交保守进入待确认”；`background_reader_stopped` 节流不再限定快速路径。
- usage：补预热预算口径、`verifyTgt` 在网关入口的调用、`captcha_prefetched` 提前量的估算口径（相对偏早的本地估算）、`trust_silent_login_failed.code` 的 `-1` 哨兵、DNS 超时的提交语义；修复 `last_error_active` 表格行缺失的行尾 `|`。
- 验收记录：2026-11-01 实战的验证码提前量注明估算口径（时间线实际距真实开抢约 1.96 秒）；“约 70 秒／82 秒”统一为约 82 秒；2026-11-03 记录的 `cli.py:268` 更正为 `265-266`，DNS 超时可重试语义限定到查询路径；开抢快速路径记录的 `query_mine` 0.5 秒说明注明预热已例外。

## 回归

新增：

- `test_opening_pending_miss_does_not_query_history_every_round`：无新鲜列表快照时历史请求 ≤3 次/4.2 秒、`reservation_unconfirmed` 只记录一次、任务仍为 pending。
- `test_prewarm_history_keeps_its_budget_when_the_opening_fast_path_is_active`：预热查询使用 >1 秒预算，不被 0.5 秒兜底压缩。
- `test_dns_resolver_slot_is_released_when_thread_start_fails`：线程启动失败后槽位可再次获取。

更新：

- `test_explicit_bad_credentials_stop_without_repeated_login`：事件日志中 `authentication_rejected` 只出现一次（排除 `notification_finished` 行）。
- 既有待确认快速路径测试继续覆盖“无快照不伪造网络错误、保持 0.1 秒节奏”。

## 验证

| 检查 | 结果 |
| --- | --- |
| `uv run --locked pytest -q` | **740 通过、3 跳过**（80.72 秒；跳过为 2 条 Unix 信号、1 条 Windows 文件替换限制） |
| `uv run --locked mypy seu_reserve` | 12 个源文件通过 |
| `node --check seu_reserve/web_assets/app.js` | 通过 |
| `git diff --check` / `git diff --cached --check` | 通过 |
| 真实学校服务器只读复测 | 通过（见下） |

### 真实学校服务器只读复测（2026-11-03 14:09–14:13，Asia/Shanghai）

- 环境：Windows 本机，当前网络经学校网关；使用本机已纳管的一个学校账号；校验 HTTPS 证书。`--check` 按设计在本机终端及本地日志输出逐场标题；除此之外未输出或记录凭据、Cookie、票据或活动 ID，本记录不含标题。
- `uv run --locked main.py --check --account <账号内部ID>`：退出码 0，`event=read_only_complete`，0.282 秒；本次未触发新的认证协商（沿用已有学校会话），请求为 `index.do` → `queryMyActivityList.do` → `queryActivityList.do` → `vcode.do`，均成功；全部修复完成后同一命令再次复测仍为退出码 0、结果一致。
- 筛选：7 场预约尚未开始、1 场类别未启用（沙龙），与当日 13:22、13:35 的只读结果一致。
- 复制 Cookie 的两个独立只读会话并发 15 秒：活动列表 **140/140** 成功（中位 47 ms、P95 78 ms、最大 125 ms）；我的活动 **3/3** 成功（中位 63 ms、P95 110 ms）；无错误、无 429/5xx/超时。
- 有界 DNS 解析器在真实请求中调用 3 次，均命中预算内（0 ms，缓存命中）；DNS 超时分支仍只有离线合成证据。
- 未产生任务写入：账号 `task.json`、`diagnostics.jsonl` 保持 2026-10-31 状态；仅 `trust-cookies.json` 按正常登录流程刷新。未发送邮件。

## 边界与未验证

- 待确认请求放大与预热预算只在离线构造中复现/验证；本轮无真实开抢或真实待确认场景，未在 19:00 开抢中复测请求量。
- DNS 槽位泄漏为代码路径修复与合成测试，未制造真实解析线程启动失败；DNS 超时对提交路径的 `connection_failure` 保守语义未改变，仅文档澄清。
- 未提交或取消预约、未启动真实开抢、未测试多账号并发、长期运行、Linux/systemd 部署或短信会话恢复。
- 本记录不含服务器地址、账号、活动 ID、标题或个人记录；原始日志只在本机 `state/`（未入库）。

同日第二轮复核（429 先等待、按待确认段持久化去重、重启后认证被拒事件去重、DNS 槽位构造归还、文档计数口径）见[二次复核修复记录](2026-11-03-recheck-fixes.md)。
