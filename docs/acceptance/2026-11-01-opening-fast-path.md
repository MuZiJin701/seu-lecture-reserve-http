# 2026-11-01 开抢期快速路径与诊断补充

> 后续统一线程架构已取代本文当时的普通期主线程查询、开抢同步兜底及邮件延后策略；本文保留历史参数与证据。当前行为和验证限制见[统一线程验收](2026-11-03-unified-workers.md)及[使用指南](../usage.md#开抢与拥塞恢复)。

后续查询恢复修复见[2026-11-03 开抢恢复验收](2026-11-03-opening-recovery.md)；本文保留当时参数与证据。

后续策略更新（2026-11-03）：当前采用[减少无效查询方案](2026-11-03-query-reduction.md)，最高频为开放前 5 秒至后 9 秒，支持无待抢目标时提前降频及普通历史 30 秒缓存。本文保留该次检查或实战当时的参数与验证结果；当前操作以[使用指南](../usage.md#开抢与拥塞恢复)为准。

本记录对应当天 19:00 服务器实战暴露的问题 1–5。用户确认采用“后台优先 + 0.5 秒兜底”的方案（B）：开抢窗口内主循环不再阻塞等待后台请求，但保留服务端 `Retry-After`、提交串行与安全判定。

## 背景

[19:00 服务器实战](2026-11-01-live-run.md)中，后台活动列表与历史线程因 `network_timeout` 被回收，主循环按旧逻辑先等待在途请求最长 6 秒、再做同步列表查询 5.954 秒，单次扫描阻塞 **11.707 秒**。虽然发生在成功提交之后，但同一开抢窗口若还有目标场次，可能错过。用户明确要求：开抢窗口内每 0.1 秒都宝贵，不能这样等待。

## 变更

### 1. 开抢快速路径（`app.py`）

- 开抢窗口内 `list_rows()` 不再等待在途后台请求：无新鲜快照时返回 `none`，主循环本轮跳过候选处理并保持 0.1 秒节奏。
- 只有**连续 3 轮**没有可用快照时，允许一次预算 **0.5 秒**的同步列表兜底；仍现场取数、不复用旧行，相邻同步兜底至少间隔 `FAST_SYNC_MIN_INTERVAL`（0.25 秒）。
- 后台活动列表线程普通失败后的重建间隔从 5 秒缩短为 `OPENING_POLLER_RETRY`（0.1 秒，等于 `FAST_INTERVAL`）；服务端 `Retry-After` 仍优先。
- 后台列表/历史线程的单次请求预算从 6 秒降为 `OPENING_BACKGROUND_TIMEOUT`（0.5 秒）。
- 开抢期同步历史查询（`query_mine`）兜底同样限制为 0.5 秒（2026-11-03 修复：预热查询使用自身 1.5 秒预算，不再被该上限压缩）。
- `background_poller=False` 仅测试注入使用（CLI 与管理台入口都用默认 `True`）；生产运行都会启用后台轮询，因此都走快速路径。

### 2. 可观测性

- `submission_started` 增加 `yyrs`、`hdzrs`、`remaining`、`remaining_after`（提交时列表行的已约人数、总人数、提交时剩余与按本次占用推算的提交后剩余；缺失为 `null`）。
- 新增本地诊断 `opening_snapshot_missing`（每次失联期首次无快照，恢复后重置）、`opening_sync_fallback`（触发 0.5 秒兜底）、`poller_aligned`（对齐实际采纳时的 `target`/`actual`/`delta_seconds`）。
- `attendance_category_unknown` 本地诊断补充缺失类别的活动 ID（`wid`），公开错误码与暂停新增的行为不变。
- `background_reader_stopped` 在快速重建下按 reader 每 5 秒最多记录一次，避免 0.1 秒重试造成诊断刷屏。
- `--status` 增加 `last_error_active`，区分当前失败与恢复后保留的历史错误。

### 3. 复核修复（2026-11-02）

- **线程消失后的三轮保护**：原条件只在 `poller is not None` 时计数，线程被回收或处于重建冷却时直接走旧同步路径。现增加 `poller_started_once`：后台读取线程启动过一次后，缺失/冷却都计入连续无快照轮次，仍满 3 轮才允许 0.5 秒同步兜底，并保留 `opening_snapshot_missing`／`opening_sync_fallback` 诊断。回归：`test_missing_started_reader_still_counts_toward_the_three_miss_fallback`。
- **当前错误状态**：`last_error_active` 不再由 `failure_count` 推断，而是 health 中的显式字段：任何错误置 `true`，成功查询置 `false`；CLI 对旧状态文件回退到 `last_error_at > last_query_at` 的时间戳判断。快速重试间隔不变。回归：`test_query_failure_mail_failure_and_recovery_are_visible_without_repeats` 同时断言失败时为 `true`、恢复后为 `false`。
- **对齐回调时机**：`poller_aligned` 只在通过停止与共享等待检查、真正即将发起下一次查询前记录；停止唤醒等待时不再产生未发起请求的对齐记录。回归：`test_alignment_is_not_recorded_when_stop_interrupts_the_wait`。
- **Web 端同步（复查补漏）**：管理台快照统一归一化 `last_error_active`（旧状态用 `last_error_at > last_query_at` 回退），页面错误提示和预约清单 `stale` 标记都改用它，不再看 `failure_count`；`app.js` 只读取归一化后的布尔字段，旧状态兼容由管理器与共享助手统一处理。回归更新：`test_reservation_snapshot_freshness_tracks_running_state_and_query_failures`（含旧状态兼容）。

### 4. 第二轮复查修复（2026-11-02）

- **开抢期历史等待**：历史缓存失效时不再调用 6 秒 `wait_update`；触发后台刷新后保持 0.1 秒节奏跨轮重试，连续 3 轮无可靠历史才允许一次 0.5 秒同步历史兜底，并记录 `opening_history_missing`／`opening_history_sync_fallback`。历史线程启动后退出、处于重建冷却，或仍在运行但快照不可用，都计入三轮保护。回归更新：`test_stale_background_history_is_retried_across_rounds_without_blocking`、`test_stale_history_is_retried_without_blocking_before_candidate_selection`、`test_missing_started_history_reader_still_counts_toward_the_fallback`。
- **撤下记录占用冲突**：`select_candidates` 的已预约集合与时间冲突都改用 `confirmed_reservation()`，与进度、展示、成功核对统一；`YYZT='已预约'` 且 `FBZT='-1'` 的记录不再阻挡同时间的新活动。回归：`test_withdrawn_record_does_not_block_a_conflicting_candidate`。

### 5. 第三轮复查修复（2026-11-02）

- **时间占位时钟基准**：新增 `pending_hold_until()`，占位期限统一保存为主机时钟；活动开始时间（学校时间）先按 `server_offset` 换算到主机时钟，再取与“建立后 600 秒”的较早值。尚未校时无法安全换算时按本地 600 秒保留，避免主机时钟快于学校时占位立即过期；校时完成后把既有占位收紧到活动开始的主机时刻。回归：`test_hold_deadline_uses_host_clock_and_school_offset`。
- **认证被拒绝的 CLI 告警**：`MAJOR_MAIL_EVENTS` 加入 `authentication_rejected`；CLI 明确凭据被拒时通过既有 `emit()` 重大告警路径发送并去重（客户端已发时不会重复），Web 工作进程仍由管理器补偿。告警持久化键映射到既有 `task_failed`，`state.py` 同时允许并一次性迁移旧版本残留的 `authentication_rejected` 键，避免一次密码错误后修正凭据仍报 `state_invalid`。回归更新：`test_explicit_bad_credentials_stop_without_repeated_login`（失败→修正凭据→重启）、`test_legacy_authentication_rejected_alert_is_migrated_on_restart`。

### 6. 第四轮复查修复（2026-11-02，两轴审查发现）

- **重建间隔常量**：`OPENING_POLLER_RETRY` 原先误指向 `FAST_RETRY_INTERVAL`（0.25 秒），现改指向 `FAST_INTERVAL`（0.1 秒），与 requirements／usage／README 的“约 0.1 秒重建”一致。
- **移除不可达的旧等待分支**：非开抢时后台线程已被停止，开抢时由快速路径接管，原 `POLLER_FALLBACK_WAIT`／`POLLER_INFLIGHT_WAIT` 等待逻辑（以及文档中的 0.3／6 秒描述）已不可达，删除并在文档中改为当前行为；随之不再使用的 `history_refresh_target`／`history_refresh_pending` 一并移除，刷新节流（2 秒）保留。
- **待确认分支不再伪造网络错误**：快速路径的 `none` 原先被改写成 `RemoteError('network_timeout')`，会产生假事件、把 `last_error_active` 置真并暂停两个后台线程。现在改为清空已保存的连续核对计数、保持 0.1 秒节奏继续扫描，不写假事件、不置当前错误。回归：`test_opening_pending_check_does_not_fabricate_a_network_failure`。
- **缺失诊断按失联期去重**：`OpeningMissGuard` 让 `opening_snapshot_missing`／`opening_history_missing` 只在每次失联期首次记录，恢复后重置；兜底诊断仍按每三轮记录。回归：`test_opening_snapshot_missing_is_logged_once_per_outage`。
- **历史三轮保护补缺口**：后台历史线程仍在运行、但快照过期或格式非法且刷新请求被节流时，原实现会直接同步查询；现统一跨轮重试，连续 3 轮后才兜底。后台刷新刚完成时仍先重读并接受快照，再决定是否等待。回归：`test_running_stale_history_reader_still_gets_the_three_round_guard`；原有 `test_in_flight_history_completing_at_the_refresh_request_is_still_consumed` 保持通过。
- **占位收紧不再受待确认状态阻挡**：活动开始时间换算为主机时钟后的收紧在 pending 期间也执行（到期释放仍只在非待确认时进行），并抽取为 `tighten_hold_deadlines()` 便于验证。回归：`test_hold_deadlines_are_tightened_only_after_calibration`。
- **`last_error_active` 回退规则收敛**：新增 `state.current_error_active()`，CLI 与管理器共用；`app.js` 只读取归一化后的布尔字段。临时超时覆盖抽取为 `bounded_query_budget()`，列表与历史兜底共用。
- **回归更新**：历史同步回退测试改为“三轮后才查询”（`test_invalid_background_history_...`、`test_invalid_salon_history_...`、`test_background_history_failure_...`）；恢复“历史线程不得在列表线程的共享服务端等待内重启”的断言（`test_opening_poller_server_wait_is_honored_without_a_sync_fallback`）。
- **文档同步**：`usage.md` 行为段落与诊断表、`requirements.md`、`AGENTS.md` 告警清单及本记录；同时更正“旧单账号模式不套用快速路径”的说法（`background_poller=False` 仅供测试）。

## 验证

| 检查 | 结果 |
| --- | --- |
| `uv run --locked pytest -q` | **730 通过、3 跳过**（含开抢快速路径、服务端等待、诊断字段、对齐回调、线程缺失、显式错误状态、历史跨轮重试、撤下记录冲突、占位时钟基准、认证拒绝告警、缺失诊断去重、待确认无快照与历史运行中不可用的新增/改写回归） |
| `uv run --locked mypy seu_reserve` | 12 个源文件通过 |
| 前端 `node --check` / 文档链接与锚点 | 通过 |

回归覆盖：

- 开抢无快照时主循环不等待在途请求（`test_opening_stale_snapshot_does_not_wait_for_the_inflight_poll`）。
- 连续 3 轮缺失后仅一次 0.5 秒预算同步兜底（`test_opening_uses_one_bounded_sync_fallback_after_repeated_misses`）。
- 后台线程返回服务端等待时，快速路径不额外发同步请求且按 `Retry-After` 延迟（`test_opening_poller_server_wait_is_honored_without_a_sync_fallback`）。
- 后台读取预算为 0.5 秒且共享门不变（`test_background_readers_use_the_opening_query_budget_and_share_the_gate`）。
- `background_reader_stopped` 在快速重建下不重复写满诊断（`test_stopped_activity_reader_failure_is_diagnosed_once_with_a_fixed_reason`）。
- 对齐回调报告目标/实际/偏差（`test_align_callback_reports_the_honored_target_and_actual_start`）。
- 线程启动后消失仍走三轮计数而不是立即同步查询（`test_missing_started_reader_still_counts_toward_the_three_miss_fallback`）。
- 停止唤醒等待时不记录未发起的对齐（`test_alignment_is_not_recorded_when_stop_interrupts_the_wait`）。
- 当前错误状态在失败时为 `true`、成功后为 `false`（`test_health.py`）。
- Web 端错误提示与预约清单 `stale` 使用 `last_error_active`，旧状态回退时间戳（`test_reservation_snapshot_freshness_tracks_running_state_and_query_failures`）。
- 历史失效时跨轮重试、不阻塞，候选仍可被选中（`test_stale_background_history_is_retried_across_rounds_without_blocking`、`test_stale_history_is_retried_without_blocking_before_candidate_selection`）。
- 历史线程启动后消失仍走三轮计数而不是立即同步查询（`test_missing_started_history_reader_still_counts_toward_the_fallback`）。
- `FBZT='-1'` 的撤下记录不再占用时间冲突（`test_withdrawn_record_does_not_block_a_conflicting_candidate`）。
- 占位期限统一为主机时钟，未校时时不提前按学校活动开始释放（`test_hold_deadline_uses_host_clock_and_school_offset`）。
- 明确凭据被拒时 CLI 发送一次 `authentication_rejected` 告警（`test_explicit_bad_credentials_stop_without_repeated_login`）。
- 认证告警持久化为 `task_failed` 键，旧 `authentication_rejected` 状态加载时迁移，修正凭据后可重启（`test_explicit_bad_credentials_stop_without_repeated_login`、`test_legacy_authentication_rejected_alert_is_migrated_on_restart`）。
- 提交名额字段与未知类别 `wid` 诊断（`test_diagnostics.py`、`test_app.py`）。
- `--status` 的 `last_error_active`（`test_health.py`）。
- 开抢缺失诊断每次失联期只记录一次，不在每个兜底周期重复（`test_opening_snapshot_missing_is_logged_once_per_outage`）。
- 待确认核对遇到快速路径 `none` 时不伪造网络错误、不推进连续计数（`test_opening_pending_check_does_not_fabricate_a_network_failure`）。
- 后台历史线程运行中但快照不可用仍走三轮保护（`test_running_stale_history_reader_still_gets_the_three_round_guard`）。
- 刷新请求在途完成时仍先重读并采用后台历史快照（`test_in_flight_history_completing_at_the_refresh_request_is_still_consumed`）。
- 历史线程不在列表线程的共享服务端等待内重启（`test_opening_poller_server_wait_is_honored_without_a_sync_fallback`）。
- 合法/未校时/已校时的占位期限换算与收紧（`test_hold_deadline_uses_host_clock_and_school_offset`、`test_hold_deadlines_are_tightened_only_after_calibration`）。
- 非法/失败的背景历史快照在连续 3 轮后才同步回退（`test_invalid_background_history_falls_back_to_sync_after_the_guard`、`test_invalid_salon_history_falls_back_to_sync_after_the_guard`、`test_background_history_failure_falls_back_to_the_sync_query`）。

## 边界与未验证

- 本次改动**尚未部署到服务器**，也未在真实开抢验证；下一场真实开抢需观察 `opening_snapshot_missing`／`opening_sync_fallback`／`opening_history_missing`／`opening_history_sync_fallback`／`poller_aligned` 和最长扫描耗时。
- 0.5 秒后台预算与 0.5 秒兜底是按“开抢黄金窗口约 1 秒”选择的：正常响应约 0.03–0.1 秒，19:00 开抢瞬间的一次请求约 0.36 秒；学校响应持续慢于 0.5 秒时可能丢数据并增加重试。这是速度优先的已知取舍，服务端 429/`Retry-After` 不被绕过。
- 历史缓存改为跨轮重试、0.5 秒同步兜底后，开抢期学校历史很慢时可能更多次按“历史不可靠”跳过新增；安全规则不变。历史快照格式非法或后台线程失效时，同一次扫描不再立即同步回退，候选处理最多推迟约 0.3 秒（三轮 0.1 秒节奏）。
- `background_poller=False` 仅供测试注入；CLI 与管理台入口都使用默认 `True`，因此生产运行都会走快速路径，文档不再声称存在“保留旧同步回退”的运行模式。
- **DNS 限制（后续已修复）**：本记录完成时 DNS 解析在建连前阻塞且不受 socket 超时约束，三轮无快照后的同步列表/历史兜底可能超出 0.5 秒预算；离线模拟中 0.05 秒预算在 0.25 秒合成解析延迟下实际等待 0.250 秒。该问题已由[启动清理与 DNS 有界解析记录](2026-11-03-startup-cleanup-and-bounded-dns.md)中的连接层有界解析修复。
- 本次为单账号、离线验证；多账号并发、真实慢网络与长期运行的收益仍需实测。
