# 2026-10-31 锁、告警与待确认口径修复

本记录对应当前项目检查发现的问题。两项口径先经用户确认：重试上限**维持现状**（随占位到期重置，只把口径写进文档）；`FBZT='-1'` 的“我的活动”记录**与进度统计统一**，不再作为待确认成功证据。业务规则见[需求](../requirements.md)，运行说明见[使用指南](../usage.md)与 [Web 指南](../web.md)。

## 1. 告警邮件不再持有账号操作门

**问题**：`Manager._alert` 在 `with self.operation(...)` 内同步发送 SMTP 告警；`Mailer` 每次网络操作超时 20 秒，DNS 失败更久。账号启用邮件且发生 `authentication_rejected`/`restart_exhausted` 告警时，停止、保存、删除及关停都会被一并阻塞。

**修复**：领取跨重启告警（写 `alerts` 标记与 `sending` 健康状态）和发布结果（`accepted`/`failed`）各用一次短临界区；SMTP 发送在门外进行。发送期间账号被删除时不再重建状态目录，也不补写健康状态。

**回归**：`test_slow_alert_mail_does_not_block_stop` 用 2 秒慢邮件发送，`stop()` 在 1 秒内返回，随后健康状态仍为 `accepted`。

## 2. 保存中断后不再留下“想运行但没有进程”

**问题**：运行中账号保存时先安全停止 worker，再取检查点锁写入配置；外部 CLI 恰在两步之间取锁时保存被拒绝且未写入，但 `desired` 仍为 True、worker 已消失、重试表已清，账号停在需手工停止才能恢复的状态。

**修复**：写入阶段抛 `state_in_use` 时调用 `_note_start_failure`，持久化 `desired=False`、`error=state_in_use` 并清理重试计划；页面可继续显示“CLI 正在运行”。

**回归**：`test_saving_when_the_cli_takes_the_checkpoint_drops_the_start_intent`（真实子进程持锁的工作进程 + 构造的取锁交错）。

## 3. 单个账号目录异常不再拖垮账号列表

**问题**：账号状态目录被替换为符号链接/junction 后，`Accounts.state_dir` 抛 `ValueError`；`Manager.snapshot` 只捕获 `OSError`，导致 `GET /api/accounts` 整体返回 409，页面无法单独处理该账号。

**修复**：`snapshot` 对 `task_running` 与事件日志读取同时捕获 `OSError`/`ValueError`；该账号显示为 `status=invalid`，其余账号正常。

**回归**：`test_snapshot_reports_an_unsafe_account_directory_as_invalid`（Windows 用 junction，Unix 用目录符号链接）。

## 4. 停止意图的检查与写入同锁

**问题**：无本地 worker 的停止路径先检查 `task_running` 再写 `desired=False`，CLI 可在两步之间取锁，接口返回成功但停止意图未生效。

**修复**：账号存在性检查后，在 `hold_checkpoint` 临界区内完成重试清理与 `desired=False` 写入；外部 CLI 持锁时仍返回 409 `state_in_use`。

## 5. 连接超时不再消耗原活动重试次数

**问题**：重试提交在发送前先把占位 `attempts` 加一；连接阶段超时（请求未发出）只回退 pending，不回退计数，与[使用指南](../usage.md#排错)“`connect_timeout` 可安全重试”的说明不符。

**修复**：`connect_timeout` 分支同时把该次提交的计数减回。

**回归**：`test_connect_timeout_does_not_consume_a_retry_of_the_original_activity`（占位 `attempts=1`，连接超时后仍为 1）。

## 6. `FBZT='-1'` 与进度统计统一

**问题**：进度统计与预约展示会跳过 `FBZT='-1'` 的行，但待确认核对只调用 `reserved()`，会把同时带 `YYZT=已预约` 的下架/未发布记录当作成功证据，提前清除待确认并发出成功通知。

**修复**：新增 `confirmed_reservation()`，与进度统计采用同一口径；待确认核对（轮询快照以外的“我的活动”来源）、`--resolve-pending` 与占位同步确认都只接受该口径。占位仍参与冲突保护，候选与已预约判定不变。

**回归**：`test_withdrawn_history_row_does_not_confirm_a_pending_submission`、`test_resolve_pending_ignores_a_withdrawn_history_row`。

**口径**：重试上限仍为“每个原活动在占位存续期间最多重试 2 次”，占位到期后计数随占位释放、按新占位重新计算；已在需求与使用指南写明。

## 7. 其他低风险修复

- 删除账号后清理 `operations`/`retries`/`retry_at` 中的条目，避免长期运行缓慢增长（`test_delete_drops_the_account_bookkeeping`）。
- CLI `--account` 在取得检查点锁后重读账号配置失败时，复用统一清理逻辑移除本次新建的 `task.json`/状态目录（`test_configuration_reread_failure_removes_a_checkpoint_created_for_a_new_account`）。
- 管理员密码校验不再依赖 `assert`（避免 `python -O` 下退化为 `TypeError`）。
- 账号名称保存前去首尾空白并拒绝纯空白。
- [Web 指南](../web.md) 对应的安全头注释写明：Starlette 外层 `ServerErrorMiddleware` 产生的 500 是唯一例外。

## 验证

| 检查 | 结果 |
| --- | --- |
| `uv run --locked pytest -q` | **719 通过、3 跳过**（新增 8 条回归） |
| `uv run --locked mypy seu_reserve` | 12 个源文件通过 |
| `node --check seu_reserve/web_assets/app.js` | 通过（前端未改动） |
| 文档链接与锚点 | 0 失效 |

## 边界与未验证

- 告警邮件回归只验证“慢邮件不阻塞操作”，未在真实 SMTP 不可达/DNS 故障网络中验证；发送期间账号被删除或停止只按状态清理处理。
- 保存中断与 stop 竞态用构造的取锁交错与真实文件锁验证，未在真实浏览器与 CLI 同时操作场景复现。
- `FBZT='-1'` 的口径来自用户确认；真实学校“我的活动”是否会出现该组合仍属待验证语义，回归只覆盖构造响应。
- 慢速分块（slowloris）、连接层超时、多 worker 共享限速仍不属于本轮范围。
- 本轮未重启运行中的管理服务，也未访问学校或发送真实邮件。
