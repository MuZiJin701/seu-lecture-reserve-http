# 2026-11-03 启动清理竞态与 DNS 有界解析记录

后续策略更新（2026-11-03）：当前采用[减少无效查询方案](2026-11-03-query-reduction.md)，最高频为开放前 5 秒至后 9 秒，支持无待抢目标时提前降频及普通历史 30 秒缓存。本文保留该次检查或实战当时的参数与验证结果；当前操作以[使用指南](../usage.md#开抢与拥塞恢复)为准。

本记录对应两轮定向复查发现的两项问题：① 启动失败清理存在跨实例删除检查点的竞态（按 P2 修复）；② DNS 解析不受请求预算约束，本轮按方案 B 在连接层加入有界解析。

## 问题 1：启动失败清理可能删除另一实例的检查点

### 原因

原 `cli.discard_checkpoint()` 在 `had_directory`／`had_task` 于**取得检查点锁之前**读取（当时 `cli.py:265-266`），且先 `Task.close()` 释放锁，再删除 `task.json` 或递归删除状态目录。存在两种交错：

1. **加锁前**：进程读到 `had_task=False`，尚未取得锁；另一实例经真实 `run()` 路径保存 `pending` 后退出并释放锁；本进程随后取得锁并加载到 `pending`，但 OCR 加载失败后仍按旧的 `had_task=False` 删除状态目录。
2. **解锁后**：本进程释放锁后、删除之前，另一实例取得锁并写入检查点，随后被本进程删除。

离线构造复现（独立子进程、构造状态，不访问学校）：加锁前交错得到 `parent_loaded=pending`、`checkpoint_exists=false`。

### 修复

- `Task` 在取得锁后记录 `created_checkpoint`：只有 `task.json` 不存在、由本次 `Task` 创建时为真；加载既有检查点（包括 `pending`）时为假。
- `discard_checkpoint()` 只在本进程创建检查点、且仍持有锁时删除 `task.json`；删除后立即关闭锁。不再在解锁后递归删除状态目录。
- 状态目录中可能保留空的 `task.lock`；空目录不是检查点，允许保留。

### 回归

- 加锁前交错：`test_ocr_failure_keeps_a_pending_checkpoint_written_before_the_lock`。
- 解锁后交错：`test_failed_startup_cleanup_does_not_delete_a_checkpoint_written_after_unlock`。
- 既有断言更新：`test_ocr_unavailable_removes_a_checkpoint_created_for_a_new_account`、`test_normal_run_rechecks_the_account_before_entering_the_engine`、`test_configuration_reread_failure_removes_a_checkpoint_created_for_a_new_account` 由“状态目录不存在”改为“没有 `task.json`”。

## 问题 2：DNS 解析不受 socket 预算约束（方案 B 修复）

### 原因

`client.py` 的截止时间连接类在 `connect()` 前收紧 socket 超时、返回后再校验预算，但 urllib3 2.8.0 的 `create_connection()` 先调用 `socket.getaddrinfo()`，该调用不受 socket 超时约束。离线模拟：请求预算 0.05 秒、合成解析延迟 0.25 秒时，`connect()` 实际耗时 0.250 秒后以 `NameResolutionError` 结束，预算未生效。开抢期连续 3 轮无新快照后的同步列表兜底与历史同步兜底在主线程执行，DNS 卡顿时会超出 0.5 秒预算、无法维持 0.1 秒节奏。

### 修复

- `_DeadlineHTTPConnection`／`_DeadlineHTTPSConnection` 改为经 `_bounded_create_connection()` 建连：`socket.getaddrinfo()` 在 daemon 线程中执行，主线程只等待请求的剩余预算；超时以 `EAI_AGAIN` 临时解析失败处理（`NameResolutionError`，映射 `connection_failure`，请求未发出；查询可安全重试，提交路径按 `connection_failure` 保守进入待确认核对，vpn 首次登录仍满足网关回退条件）。
- 解析成功后按剩余预算逐地址建连，保留 `source_address`／`socket_options` 行为；IP 字面地址（含网关备用 IP）不额外解析。
- 成功结果短 TTL 缓存（60 秒）；连接失败时丢弃该主机缓存，避免失效地址在 TTL 内被反复使用。
- 并行解析任务上限 8 个；槽位耗尽时快速失败为 `EAI_AGAIN`，避免 DNS 故障时线程堆积。

### 复核修复：超时后成功的解析结果

原实现只由等待的请求线程在 `Event.wait()` 成功后写缓存；请求因预算耗尽先返回时，后台解析即使随后成功也被丢弃。冷连接下若解析持续慢于开抢兜底预算，后续请求会不断重新解析并再次超时，无法自行恢复。现由解析线程在成功时直接写入缓存（并用按主机的递增代数防止连接失败丢弃缓存后，旧解析结果又被写回），因此本次请求超时、解析稍后完成时，下一次请求可以直接命中缓存。

- 回归：`test_dns_result_is_cached_after_the_requesting_call_times_out`（0.05 秒预算先超时；0.3 秒解析完成后缓存命中，第二次解析不再调用 `getaddrinfo`）。

### 回归

- 0.05 秒预算下 0.3 秒合成解析延迟不会拖过预算：`test_bounded_dns_resolution_stops_at_the_request_deadline`。
- 经 `Client.request` 走完整请求栈时，DNS 超时映射为可重试的 `connection_failure` 且耗时受限：`test_client_request_bounds_dns_with_the_request_budget`。
- 成功解析命中缓存、不重复解析：`test_dns_resolution_cache_avoids_repeated_lookups`。
- 本次请求超时后解析仍写入缓存、下次复用：`test_dns_result_is_cached_after_the_requesting_call_times_out`。
- 解析任务槽位耗尽时快速失败：`test_dns_resolver_slot_limit_fails_fast`。
- 既有网关回退/证书测试改为通过 `client._bounded_create_connection` 的接缝注入，断言不变。

## 验证

| 检查 | 结果 |
| --- | --- |
| `uv run --locked pytest -q` | **737 通过、3 跳过**（含启动清理两种交错与 5 条 DNS 有界解析回归） |
| `uv run --locked mypy seu_reserve` | 12 个源文件通过 |
| `node --check` / 文档链接与锚点 | 通过 / 0 处失效 |

同日复核修复新增 3 条回归（待确认快速路径请求节流与事件去重、预热预算、DNS 槽位归还），更新 2 条断言后全量 **740 通过、3 跳过**，mypy 通过；同时完成真实学校服务器只读复测，详见[复核修复记录](2026-11-03-review-fixes.md)。本记录的 737 项为当时基线。

## 边界与未验证

- 启动清理竞态用离线构造的独立子进程复现；真实多实例并发窗口很窄，但修复后清理只发生在锁内且只针对本进程创建的 `task.json`。修复后未在真实多账号并发启动中再次验证。
- DNS 用合成解析延迟模拟（0.25 秒），不代表真实 resolver 的超时时长；未在真实 DNS 故障网络中验证。60 秒缓存 TTL 与 8 个并行解析上限是离线取舍；纯 CPU 解析仍不可硬中断，因此 0.5/0.5/8 秒仍是目标上限而非绝对保证。方案 A（只读兜底后台化）未采用。
