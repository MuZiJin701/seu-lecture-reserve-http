# 使用与验收

首次使用按 [README](../README.md) 的安装、配置、检查和启动步骤操作。本文集中说明配置、诊断与恢复；业务规则以[需求](requirements.md)为准，统计证据见[接口调查](completion-records.md)。

## 配置

复制 [.env.example](../.env.example) 为 `.env` 后填写。默认只读当前工作目录的 `.env`，不向上查找；其他位置用 `--config /项目目录/.env` 指定，文件不存在即报错。状态目录相对于所选配置文件，进程环境变量优先。

| 配置项 | 含义 |
| --- | --- |
| `SEU_CARD_NUMBER` / `SEU_PASSWORD` | 学校一卡通号与密码 |
| `SEU_PREFERRED_LECTURES` | 完整标题 JSON 数组；`[]` 不限标题，仍受其他条件约束 |
| `SEU_REQUIRED_CATEGORIES` | 类别及顺序，默认心理、法律、艺术、其他与沙龙五类，不能为空；沙龙计入选听总场次 |
| `SEU_PREFERRED_CAMPUS` | 允许的线下校区数组；`[]` 不约线下 |
| `SEU_ENABLE_ONLINE` / `SEU_ENABLE_OFFLINE` | 允许线上／线下，仅接受 `true` 或 `false` |
| `SEU_STATE_DIR` | 状态目录，默认 `state` |
| `SEU_VPN_IP` | 首次登录的备用网关 IP；空值关闭回退 |
| `SEU_MAIL_SUCCESS` | 每次成功通知开关；程序缺省 `false`、样例 `true`，仅邮件已启用时生效 |

列表写成单引号包裹的 JSON，例如 `'["无锡校区"]'`。密码含空格、`#`、`=` 或 `$` 时用引号包裹；`${...}` 原样保留，不展开变量。不要把 `.env` 当 shell 脚本执行。修改后重启生效，样例更新不会覆盖已有配置。

兼容账号变量 `SEU_USERNAME`；同一来源中 `SEU_CARD_NUMBER` 优先，任何进程环境账号都优先于文件。缺少凭据时前台可隐藏输入，非交互运行会报错。旧 TOML 入口和浏览器超时配置不适用，未知 `.env` 字段会报错；迁移时按本项目样例逐项填写，保留旧文件。

`.env`、状态及含讲座详情的终端输出仅供本地使用。Linux 配置文件设为 `600`，状态目录设为 `700`；Windows 也应限制目录访问权限。不要上传真实配置或个人记录。

## 邮件配置

邮件可选，关闭后终端及 `events.log` 仍记录事件。仅配置收件人不足以发信；所有邮件字段为空或缺省时关闭，仅保留 `SMTP_PORT=465` 也关闭，部分配置不完整则停止启动。

### 获取发信密码

| 发件邮箱域名 | 自动使用的主机 | 密码类型 |
| --- | --- | --- |
| `qq.com` | `smtp.qq.com` | SMTP 授权码 |
| `163.com` | `smtp.163.com` | SMTP 授权密码 |
| `126.com` | `smtp.126.com` | SMTP 授权密码 |
| `gmail.com` | `smtp.gmail.com` | 应用专用密码 |

QQ：在电脑浏览器登录 [QQ 邮箱](https://mail.qq.com/)，进入“设置 → 账号与安全 → 安全设置”（旧版为“设置 → 帐户”），开启包含 SMTP 的服务，按提示验证并生成授权码。已有服务时保留原设置，从授权码管理生成新码；页面入口可能变化，见[操作说明](https://consumer.huawei.com/cn/support/content/zh-cn00696857/)。

163／126：登录对应邮箱，进入“设置 → POP3/SMTP/IMAP”，开启 SMTP 并选择“新增授权密码”，按提示验证。生成的密码通常只显示一次，见[网易说明](https://help.mail.126.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac286624f309a1a7089)。

Gmail：在 [Google 账号](https://myaccount.google.com/security)开启两步验证，再到[应用专用密码](https://myaccount.google.com/apppasswords)为本工具创建密码，复制十六个字符并去掉分组空格。找不到入口时检查组织限制、验证方式及高级保护设置，见 [Google 说明](https://support.google.com/accounts/answer/185833?hl=zh-Hans)。

### 填写并测试

不要填写邮箱网页登录密码。只填以下两项即可默认发给自己：

```dotenv
SMTP_USERNAME=your-address@qq.com
SMTP_PASSWORD='your-authorization-code'
```

主机按上表域名精确匹配，不区分大小写；别名、子域及其他邮箱不自动识别。切换邮箱时清空或更新旧的显式主机。高级配置如下，非空显式值优先：

| 配置项 | 默认值及用途 |
| --- | --- |
| `SMTP_HOST` | 根据发件账号补全；其他邮箱手填 TLS 主机，不含 URL 或端口 |
| `SMTP_PORT` | 隐式 TLS 端口，默认 `465` |
| `MAIL_FROM` | 默认 `SMTP_USERNAME`；自定义须获服务商允许 |
| `MAIL_TO` | 默认发件人，可指定另一个完整邮箱地址 |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | 成对提供；也兼容无需认证的 TLS 中继 |

当前使用校验证书的 `SMTP_SSL`，不支持明文、587 STARTTLS 或 OAuth。SMTP 不自动使用 HTTP 代理。关闭邮件需清空账号、密码、主机、发件人和收件人，端口留空或保持 `465`；同名进程环境变量也须清除。

保存配置后运行：

```bash
uv run --locked main.py --test-mail
```

此命令只发一封邮件，不要求学校凭据、不访问学校、不修改任务。`event=test_mail_accepted`、退出码 0 只代表 SMTP 接受，还需核对收件箱及垃圾邮件。`mail_disabled`、`mail_configuration_invalid` 或 `notification_failed` 返回 1，测试结果不写入任务通知状态。

### 通知规则

只提醒重大问题：需要人工认证、连续五分钟无法完成查询、连续五分钟无法完成验证码或提交请求、任务异常退出。预约成功邮件由 `SEU_MAIL_SUCCESS=true` 开启（样例默认开启），每次按活动 ID 确认的预约成功发一封，由后台线程发送、不阻塞扫描；关闭时只发重大告警，不发目标完成、待确认、恢复通知和日报。开抢窗口内出现的持续查询失败、验证码／提交失败告警延后到窗口结束再发送，期间恢复则不再发送。同一未解决告警跨重启只尝试一次，失败不自动补发，恢复只记录不发送；关闭程序后不再发信。具体时机见[邮件与告警](requirements.md#邮件与告警)。邮件只有中文说明、安全汇总与数据时间，没有讲座详情或凭据。

重大告警在主流程中使用同步 SMTP；成功通知使用后台守护线程，不阻塞扫描。每次 SMTP 网络操作超时为二十秒，并非整封邮件总时限。任务快照的最近邮件结果和耗时只反映重大告警；成功通知不更新这些字段，进程退出时尚未发完的成功邮件可能中止，不自动补发。通知失败不改变预约结果；SMTP 接受与真实送达分别验收。

## 开抢与拥塞恢复

已知预约开放时间前三十秒至开放后十分钟内（不超过预约截止时间）进入快轮询，目标间隔为 0.25 秒；请求本身较慢时，实际一轮会更长。合法空列表或暂时缺少目标时仍保留已知且未结束的窗口，提交只依据最新有效列表。

| 路径 | 超时或等待策略 |
| --- | --- |
| 普通查询、验证码与提交 | 请求预算 10 秒；普通扫描结束后等 10 秒 |
| 开抢期活动列表、历史查询 | 请求预算 6 秒 |
| 开抢期验证码、提交 | 请求预算 8 秒；不增加提交前列表复核 |
| 可恢复的网络或响应异常 | 已认证且处于开抢窗口时约 0.25 秒后重试 |
| 502 / 503 / 504 | 无明确等待值时按 1、2、4、5 秒短退避，上限 5 秒 |
| 429 或带有效 `Retry-After` 的过载响应 | 先按要求等待，再请求其他来源；不增加待确认核对次数 |

活动列表优先于历史刷新。开抢期可复用 60 秒内的历史缓存；缺失或过期时仍须刷新，历史不可靠时暂停新增。缓存降低同步刷新撞上开放瞬间的概率，不保证每次都避开。验证码最多尝试四次，明确失败后该活动冷却五分钟；提交结果不明进入待确认核对，恢复规则见下文。

响应首部、TLS 读取及响应体共用截止时间，连接后再次校验预算；DNS 与纯 CPU 处理不能硬中断，因此 6/8 秒是目标预算而非绝对上限。同步请求、服务器排队及名额竞争仍会影响结果；离线回放不能证明真实拥塞下的命中率。

## 入口与恢复

| 命令 | 用途 |
| --- | --- |
| `uv run --locked main.py` | 启动或恢复真实预约；Ctrl+C 停止 |
| `uv run --locked main.py --check` | 登录、校验数据、展示筛选原因并取得验证码图片，不预约或写任务 |
| `uv run --locked main.py --status` | 离线查看状态与私有筛选快照，不要求登录 |
| `uv run --locked main.py --test-mail` | 只发一封真实测试邮件 |
| `uv run --locked main.py --new-task` | 兼容入口：重核已完成目标；普通启动也会重核 |
| `uv run --locked main.py --resolve-pending` | 待确认提交的人工兜底入口：只读核对学校记录，显式确认后才清除待确认与占位，不提交任何请求；正常运行会在核对后自动保留/释放占位 |

五个模式参数互斥，均可配合 `--config`；`--help` 查看帮助。原 `uv run --locked seu-reserve` 与 `uv run main.py` 仍可使用，`--locked` 会阻止锁文件被自动更新。业务错误通常返回 1，Ctrl+C 或 SIGTERM 返回 130。

每次正常启动都核对学校目标，本地完成不代表学校当前仍达标。状态损坏、账号不符或恢复时间非法会停止并保留原文件，不要删除任务来绕过待确认、冷却或限流。版本 1 单场状态自动迁移，旧程序不能读取新版状态，不要混用。

待确认不需要人工解除：程序先只核对原活动；连续 3 次有效查询仍未出现时，为每笔未确认预约保留独立时间占位并恢复扫描，所有占位共同参与冲突检查，其他候选与之重叠时跳过，每个原活动最多重试 2 次（学校记录延迟时可能重复提交同一活动）；“我的活动”出现原活动、活动列表显示已预约或回复明确已预约时立即确认，并把该时间加入本地冲突集合后重算候选；已确认但历史未追上的占位继续保留，并按活动 ID 计入本地计划进度，避免超过目标继续预约。占位在建立后 600 秒或原活动开始前到期，到期记录 `reservation_hold_expired`。待确认核对同时使用“我的活动”和活动列表，一个接口失败不会阻止另一个来源的成功证据；但 429 等带等待要求的响应必须先等待，不会先查另一来源或增加核对次数。两者都查不到仍不能当作失败。查询失败或记录矛盾时不自动解除，仍可停止后台服务、确认 `process_running=false` 后运行 `uv run --locked main.py --resolve-pending` 人工兜底：只读查询“我的活动”，原场已出现则按成功确认；未出现时输入 `confirm` 才清除待确认与对应占位（含旧格式占位），其他输入取消且状态不变。该命令不提交预约。

升级时，旧版已确认占位缺少类别会优先用本轮已有响应补齐，不额外查询学校；仍缺类别时以 `attendance_category_unknown` 暂停新增并保留占位，等待学校记录同步。旧待确认记录缺少活动起止时间时保持待确认，可用上述人工兜底入口核对。人工解除只清理对应活动，其他预约的新旧格式占位继续保留。

前台需要短信时，输入 `send` 主动发送，再输入六位验证码；错误或过期可主动重发，未知挑战等待处理。会话恢复成功前不开展业务。后台没有终端而需要人工输入时保留任务并退出，先确认后台停止，再前台运行；处理后停止前台并按原命令恢复后台。

## 服务器后台运行

Ubuntu 24.04 使用 [README 的启动命令](../README.md#4-启动预约)创建系统级临时服务，不需要浏览器或永久服务文件；安装和运行使用同一账号。默认日志为 `state/runner.log`，自定义状态目录时同步修改命令中的目录及日志路径。

服务名固定为 `seu-lecture-reserve-http.service`，与浏览器版区分。它指定工作目录、当前账号和绝对 `uv` 路径，`UMask=0077` 限制新文件权限；已有目录及日志分别用 `chmod 700`、`chmod 600` 收紧权限。输出持续追加，未配置自动轮转，请留意磁盘空间。

### 查看与停止

```bash
sudo systemctl status seu-lecture-reserve-http.service --no-pager
uv run --locked main.py --status
tail -n 20 state/runner.log
```

启动命令成功只证明程序已启动，仍需检查最近成功查询。停止时执行：

```bash
sudo systemctl stop seu-lecture-reserve-http.service
uv run --locked main.py --status
```

确认 `process_running=false` 后才可前台处理或再次启动。停止等待最多六十秒，超时后可能强制终止；保留状态，重启时先核对待确认提交。`--collect` 会回收已退出服务，`systemctl status` 可能显示未找到，需结合项目状态及日志判断；root 用户省略 `sudo`。

`Restart=no` 表示退出后不自动重启；服务器重启后也不会自启。处理故障后重新执行 README 的启动命令，保留原状态和日志，停止不取消学校预约。此处只提供操作指南，不会替用户部署或启动真实预约。

### 服务环境

系统级服务不自动继承 SSH 的临时 `export`，程序仍直接加载项目 `.env`，保留服务进程环境优先的规则。不要用 systemd 的 `EnvironmentFile` 解析本项目 `.env`，两者语法并不相同。

Requests 的环境代理仍可用，但代理变量不能写进本项目 `.env`，否则报未知配置项。需要沿用 SSH 中已设置的 `HTTPS_PROXY` 和 `NO_PROXY` 时：把 README 启动命令中的 `sudo systemd-run` 改为 `sudo --preserve-env=HTTPS_PROXY,NO_PROXY systemd-run`，并在程序路径之前加上 `--setenv=HTTPS_PROXY --setenv=NO_PROXY`。只列出实际设置的变量；root 直接运行 `systemd-run` 并加同样的 `--setenv` 参数。其他代理变量可按名称显式传入，不自动转发整个环境。

该方式只在命令中列变量名，不展开代理地址或密码；sudo 策略须允许保留指定变量。SMTP 不走这些 HTTP 代理。参考 [systemd-run](https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html) 的独立服务环境和 `--setenv` 说明。

### 从 nohup 迁移

只需迁移旧后台实例，不要重建 `.env` 或状态目录。先进入原项目目录，读取旧 PID 并核对：

```bash
reserve_pid=$(cat state/runner.pid)
ps -p "$reserve_pid" -o pid,ppid,args
readlink "/proc/$reserve_pid/cwd"
```

确认命令确为本项目的 `uv run main.py`（可能含 `--locked`），工作目录也正确后再执行：

```bash
kill -TERM "$reserve_pid"
uv run --locked main.py --status
```

PID 可能过期或被复用，不能只凭文件杀进程；没有 PID 文件也不代表没有实例，需自行核对原进程。`uv` 会[转发终止信号](https://docs.astral.sh/uv/concepts/projects/run/#signal-handling)给 Python 子进程，确认 `process_running=false` 后做只读检查，再按 README 启动 systemd 服务。旧 PID 文件不再用于新服务管理，保留它不会影响任务恢复。

## 状态与日志

`--status` 首行 `status=...` 为本地任务状态：`not_started` 无任务、`scanning` 扫描、`pending` 提交待确认、`done` 上次核对已完成、`legacy_completed` 旧单场成功。它们不能单独证明进程健康或预约成功。

| 字段 | 怎么看 |
| --- | --- |
| `process_running` | 是否占用本地任务锁，不读取旧 PID 判活；权限或路径错误会报错 |
| `phase` / `last_phase` | 当前／退出前阶段；旧任务或坏快照为 `unknown`，停止时 `phase=stopped` |
| `updated_at` / `snapshot_age_seconds` | 快照时间与距今秒数 |
| `last_query_at` / `last_query_age_seconds` | 当前进程最近完整查询及校验成功时间；未知不代表零次 |
| `next_check_at` | 正常等待或退避的下一次检查计划 |
| `last_error` / `last_error_at` / `failure_count` | 最近错误及时间、连续失败数；恢复清零次数，旧错误保留 |
| `activity_count` / `candidate_count` | 上次列表条数及候选数；未查询列表时为 `unknown`，不是剩余名额 |
| `mail_status` / `last_mail_at` | 邮件配置与最近重大告警状态／时间；`accepted` 只是 SMTP 接受 |
| `last_mail_status` / `last_mail_error` | 跨重启保留的最近重大告警结果和安全错误分类 |
| `last_mail_event` / `last_mail_duration_seconds` | 重大告警事件及实际耗时；旧记录或未结束尝试显示未知耗时 |
| `selection_updated_at` / `activity_details` | 最近私有筛选快照及时间，损坏详情显示 `unknown` |

常见阶段为 `authentication`、`my_activities`、`activities`、`captcha`、`submitting`、`confirming`、`waiting`、`waiting_for_attendance`、`backoff`、`manual_required` 和 `failed`。

结合进程、阶段、成功查询及 `next_check_at` 判断进展：零候选且成功时间在更新，可能是未开放或被筛选，不代表提交链路通过。退避可受学校要求延长，邮件或网络操作也需等待，不能只按固定时长无更新判断退出。快照写入失败会记录 `health_write_failed`，此后旧时间不可作为新证据。

正常轮询安静，启动和认证成功会记录事件；查看最近事件：

```bash
tail -n 20 state/events.log
```

日志每行带 Unix 时间戳，旧错误仍保留；未记录过事件时文件可能不存在。启动署名不代表登录成功。后台启动、`--check` 和 `--test-mail` 展示一次维护者及项目链接，`--status` 和 `--help` 保持简洁。

私有诊断另写在 `state/diagnostics.jsonl`：扫描节奏、筛选变化、验证码、提交与核对结果；字段仅含活动 ID、标题、固定原因、计数与耗时。诊断不进入终端、邮件或事件日志；`--check` 不写诊断，按需在本地排查时读取。

### 筛选原因

`--check` 展示当前结果；`--status` 展示最近成功快照，可能因等待打卡、待确认或查询失败而陈旧。每场仅保留标题、首个原因及学校预约时间，时间以北京时间显示，未知为 `null`。不为诊断额外查询学校，逐场详情不进入邮件或事件日志。

| 原因 | 含义 |
| --- | --- |
| `candidate` | 通过筛选与名额判定，提交后按活动 ID 确认 |
| `category_not_allowed` / `category_not_needed` | 类别未启用／当前目标不需要 |
| `reservation_not_started` / `reservation_ended` | 预约尚未开始／已经结束 |
| `capacity_full` / `not_reservable` | 已约人数达到总人数或不可约；以学校接口响应为准 |
| `already_reserved` | 列表或我的活动已有预约 |
| `notice_test_only` | 系统测试、请勿预约或购票公告，不预约 |
| `title_not_selected` | 完整标题不匹配 |
| `online_disabled` / `offline_disabled` | 对应授课方式关闭 |
| `campus_not_selected` | 线下校区未选择 |
| `time_conflict` | 与有效预约重叠、首尾相接，或与待确认原活动的本地时间占位重叠 |
| `cooldown` | 验证码失败的本地冷却；只读检查不采用此项 |
| `invalid_activity_time` | 候选时间损坏，跳过该场 |
| `retry_limit` | 待确认原活动已达到重试次数上限，暂不重试 |

未启用类别提前排除，允许缺少其他字段；缺标题显示空字符串，损坏的预约时间显示 `null`。目标或类别未知的记录仍严格校验，矛盾候选及已有记录未知会阻止新增。

## 排错

先看事件码、发生时间及下一次检查计划，公开排错只提供脱敏信息。

| 事件或错误 | 处理 |
| --- | --- |
| `credentials_require_terminal` / `authentication_rejected` | 补齐凭据或核对账号状态，避免反复登录 |
| `authentication_required` / `interactive_terminal_required` | 确认后台退出后前台处理短信或其他挑战 |
| `activity_schema_unknown` / `business_response_invalid` | 目标数据或活动列表响应格式变化，暂停提交并核实接口，不能当作零候选 |
| `invalid_activity_data` / `attendance_category_unknown` / `attendance_status_unknown` / `attendance_record_conflict` | 已有记录不可靠，不删除状态或绕过校验；尚未结束的新预约缺少打卡/违约字段时按未参加处理，结束后仍未知才暂停 |
| `attendance_result_pending` | 已结束记录尚无打卡或违约结论，等学校更新 |
| `reservation_unconfirmed` / `reservation_unconfirmed_released` | 提交结果待核对／核对后保留原活动时间占位并恢复扫描；占位期间冲突候选跳过，解除与占位事件不发邮件 |
| `reservation_hold_confirmed` / `reservation_hold_expired` | 占位期间出现明确成功证据并确认／占位到期释放；到期后可能重新允许冲突候选，证据在本地事件与诊断 |
| `query_failure` / `query_recovered` | 持续故障／完整查询恢复；恢复不等于预约成功 |
| `reservation_step_failure` / `reservation_step_recovered` | 验证码或提交请求连续五分钟失败／恢复；开抢窗口内延后并去重，恢复不发信 |
| `notification_deferred` / `notification_skipped_recovered` | 开抢窗口内查询或关键步骤失败告警延后；窗口结束前恢复则跳过，不重复发送 |
| `connect_timeout` | 连接阶段超时、请求未发出；程序恢复扫描并重试，不进入待确认 |
| `server_backoff` / `service_unavailable` | 429 按 `Retry-After` 等待；502/503/504 为临时过载，按短退避恢复，持续失败再升级 |
| `pending_confirmed` / `pending_released_by_operator` / `pending_resolution_cancelled` | `--resolve-pending` 的人工核对结果；取消不改变任务 |
| `reservation_succeeded` / `goal_completed` | 已按 ID 确认一场／实际选听目标达标 |
| `task_failed` / `unexpected_failure` | 可捕获异常退出，检查本地错误和服务日志后恢复 |
| `mail_configuration_invalid` / `notification_failed` | 核对邮件字段或下表错误分类，业务结果不受发信失败影响 |

只读检查会输出 `stage` 和安全原因。网络原因包括 `dns_failure`、`tls_failure`、`proxy_failure`、`network_timeout`、`connect_timeout`、`connection_failure`、`network_failure`、`http_failure`、`server_backoff` 和 `service_unavailable`；HTTP 错误附状态码。`connect_timeout` 表示连接未建立、请求未发出，可安全重试；`network_timeout` 表示已发送但结果未知或读取超时，提交按待确认处理。`server_backoff` 表示 429 限流，按 `Retry-After` 等待；`service_unavailable` 表示 502/503/504 临时过载，按短退避恢复。TLS 诊断只显示白名单域名、固定 `tls_reason` 及可确定时的 `proxy_used`，不打印异常原文或代理地址。证书失败不能通过关闭校验解决。

邮件失败分类为 `dns_failed`、`tls_failed`、`timeout`、`authentication_failed`、`sender_refused`、`recipient_refused`、`message_rejected`、`connection_failed`、`smtp_failed`、`network_failed` 或 `unknown`。分别检查网络、授权码、服务器和发件／收件设置；不自动重发，SMTP 接受后仍需确认收件箱。

### 网关回退

`.env.example` 提供 `SEU_VPN_IP=121.249.14.2`，程序内部没有默认 IP，空值或缺省关闭。该地址来自历史排查，可能失效，须按可信 DNS 核对更新；仅接受 IPv4／IPv6 字面地址，不接受域名、URL 或端口。

始终先正常 DNS；仅首次登录访问 `vpn.seu.edu.cn` 遇解析失败、连接关闭／重置或可识别的 TLS 意外断开时，切换备用 IP 并重走认证一次。普通超时、连接拒绝、证书失败、凭据错误、代理或 HTTP 错误、其他域名故障不触发。切换后本进程继续使用备用 IP，下次启动重新尝试 DNS，首次登录完成后不再切换。

回退只替换网关连接地址，URL、Host、TLS SNI、域名证书及 Cookie 仍使用原域名；不改变系统 DNS、TUN 或代理，不直接重放业务请求。它不能保证绕过所有代理干预，真实故障恢复仍待验收。

## 当前业务数据契约与限制

真实学校列表使用 `JZMC`、`JZXL_DISPLAY`、`JZDD`、`JZSJ`／`HDJSSJ`、`FBZT`、`YYKSSJ`／`YYJSSJ`。活动 ID 使用非空 `HD_WID` 或 `WID`，我的预约优先使用 `HD_WID`，不能拿预约记录自身 ID 替代。

`FBZT='1'` 且处于预约起止时刻之间才进入可用性判断，时间边界包含；`FBZT='-1'` 或已有 `YY_WID` 时排除。已约人数（`hdrsList` 的 `YYRS`）和总人数（`HDZRS`）都可解析且前者不小于后者时判满；缺少人数不是已有名额的证明，最终以提交和核对为准。开放与结束按学校返回的服务器时间 `xtdqsj` 判定。线上线下只接受标题开头的 `【线上】`／`【线下】`，不猜测 `SFKSYY`。学校前端会合并人数和预约信息，本项目同样按 `HD_WID` 合并，并保留独立“我的活动”防重与冲突检查；资格和实时名额不依赖单独预检，以提交响应及最终确认裁定。

兼容构造响应的 `HDMC`、`HDLX_DISPLAY`、布尔 `IS_ONLINE`、`YYZT`，时间兼容 `KSSJ`／`JSSJ`、`HDKSSJ`／`HDJSSJ`。时间格式为 `YYYY-MM-DD HH:MM:SS`、`YYYY-MM-DD HH:MM` 或 `YYYY/MM/DD/HH:MM:SS`，按 Asia/Shanghai 解释，主机应准确校时。

有效预约 `YYZT` 可为 `已预约`、`预约成功`、`取消预约`；非有效为 `已取消`、`取消成功`、`未预约`。真实我的活动可用 `HD_WID` 与 `FBZT` 识别，已打卡仍有预约身份；统计打卡与违约规则见[接口调查](completion-records.md)。

列表接受完整数组，拒绝分页对象或已提供总数不符的响应。空列表不能证明字段兼容，未开放列表不能证明开放后的预约成功；全历史完整性和新格式仍需单独验证。

学校时间暂时缺失时沿用已知时钟偏差，不为校时增加提交前查询；重启后尚未校准时，已有冷却继续生效，取得有效学校时间后才判断是否到期。

## 开发检查

```bash
uv sync --locked
uv run --locked mypy seu_reserve
uv run --locked pytest
uv run --locked main.py --help
```

Python 3.12、uv 和锁文件是开发入口；OCR 使用 ddddocr 自带模型，首次安装包含 ONNX 依赖。`tzdata` 为无系统 IANA 数据的平台提供北京时间，更新锁文件后先同步环境。

离线测试用构造学校响应、临时配置／状态、替代时间／输入／邮件，不读取真实 `.env` 或访问学校。Windows 使用 `msvcrt` 文件锁，Linux 使用 `fcntl`，保留原子替换及持久化写入；Windows 不执行 Unix 目录 `fsync`。

## 验收记录

截至 2026-10-27，本地完整离线测试为 437 项通过、3 项平台限制跳过，mypy 通过。下表按验证发生时间保留历史；旧行中的“未推送、未部署”描述当时状态，提交状态以 Git 为准。2026-10-27 的恢复保护修正仅有离线证据，尚未部署到真实预约实例验证。

| 项目 | 当前证据及边界 |
| --- | --- |
| 2026-10-19 本轮改造 | 完整离线测试 331 项通过、3 项因 Windows 平台限制跳过（两项 Unix 信号、一项打开文件替换）；mypy、锁文件及依赖检查通过。全部 11 份 Markdown、49 个本地链接及锚点、28 段 shell 语法、双语命令与启动顺序、隔离样例配置检查通过 |
| 2026-10-25 真实预约链路 | 用 HTTP 版对购票公告（非目标、测试活动）真实提交成功（`addReservation.do` 返回 `code=0`，按 `HD_WID` 在“我的活动”确认），验证码 OCR 单次识别成功；随后 `deleteReservation.do` 取消并在“我的活动”复核消失。本机与服务器均完成该验证；另实测连续 5 次/秒、10 次/秒的活动列表只读查询未触发 429。目标系列讲座的真实预约与命中率仍待验证 |
| 2026-10-25 第二批改造 | 诊断文件、A 版重大告警策略（无日报/无冷却）、健康快照、沙龙计入目标及测试/购票公告排除完成；离线测试 353 项通过、3 项平台限制跳过，mypy 通过；已提交（`89e9558`），并于当日 22:34 在服务器启动系统级后台服务 |
| 2026-10-26 服务器运行观察 | 后台服务自 2026-10-25 22:34 起连续运行约 16 小时 37 分（截至 15:11）：`--status` 为 scanning／waiting，最近查询无错误、失败计数 0、候选 0（6 条记录中 5 场未开放、1 场测试公告），journal 无错误；不足二十四小时，长期运行、重启恢复与目标讲座真实预约仍待验收 |
| 2026-10-26 复查修复 | 待确认不再因查不到而自动释放或重试，保持 pending 并只核对原场；新增 `--resolve-pending` 人工核对与显式确认恢复；冷却清理延后到学校时钟校准完成后，重启不再误删有效冷却；429 按 `Retry-After` 等待，502/503/504 改走 1/2/4/5 秒短退避；活动列表失败保留有效历史缓存；验证码冷却统一使用学校校准时间；离线测试 369 项通过、3 项平台限制跳过，mypy 通过；未推送、未部署（待确认规则已由 2026-10-26 无人值守修正取代） |
| 2026-10-26 开抢前修复 | 连接阶段超时不再进入 pending，可按“请求未发送”安全恢复；开抢窗口沿用开抢前 60 秒内的历史缓存，避免开放瞬间同步刷新阻塞；移除提交前额外列表复核；离线测试 374 项通过、3 项平台限制跳过，mypy 通过；未推送、未部署 |
| 2026-10-26 预约后修复 | 新预约尚未结束且缺少 `SFDK`/`SFWY` 时按“未参加、未违约”计入待参加，不再误报 `attendance_status_unknown` 阻塞后续；已结束记录仍缺字段时才暂停；离线测试 377 项通过、3 项平台限制跳过，mypy 通过；未推送、未部署 |
| 2026-10-26 开抢延迟与邮件修复 | 历史缓存有效期改为 60 秒，避免开放瞬间同步刷新“我的活动”；预约成功邮件改为后台线程发送，不阻塞扫描；离线测试 378 项通过、3 项平台限制跳过，mypy 通过；未推送、未部署 |
| 2026-10-26 本机真实预约 | 19:00 艺术讲座线下预约成功并按活动 ID 确认，线上版本首发已满；完整时间线、脱敏诊断与修复记录见[本机实战日志](acceptance/2026-10-26-live-run.md) |
| 2026-10-26 请求总时限 | HTTP 响应体读取按单请求总预算限制，慢速持续返回数据不再无限拖住同步扫描；当时用逐字节读取，且未限制单次阻塞读取的时长；离线测试 379 项通过、3 项平台限制跳过，mypy 通过；未推送、未部署 |
| 2026-10-26 响应读取修正 | 429/502/503/504 改为在读取响应体前按状态码与响应头判定，慢速或损坏的错误响应不再把它们变成 `network_timeout` 并丢掉 `Retry-After`；响应体改用 64 KiB 分块 `read1`（按 Requests 同款解码压缩响应），单次阻塞读取以剩余总预算作为 socket 超时；body 读取超时映射为 `network_timeout`，其他读取失败映射为 `connection_failure`；当时 gzip 解码和 chunked 分帧的内部连续读取仍可越过预算，见下一行；离线测试 388 项通过、3 项平台限制跳过，mypy 通过；未推送、未部署 |
| 2026-10-26 响应读取修正二 | 响应体读取期间底层 socket 的每次 recv 改为经 `select` 等待剩余预算，gzip 解码、chunked 分帧等一次性 `read1` 内部的多轮读取同样受总预算约束；预算耗尽仍按 `network_timeout` 中断并恢复 socket 状态；用真实本地服务验证慢 gzip、慢 chunked 分块头（含 TLS 路径）和正常快响应的连接复用；离线测试 392 项通过、3 项平台限制跳过，mypy 通过；未推送、未部署 |
| 2026-10-26 无人值守修正 | 保留已知未结束的开抢窗口，合法空列表不再让轮询退回 10 秒；429 等待前不再发历史探测；新增验证码／提交连续五分钟失败的 `reservation_step_failure` 告警（开抢窗口内延后、去重、恢复清除）；待确认连续 3 次有效查询缺席后自动解除并继续扫描，`--resolve-pending` 保留为人工兜底；响应首部与响应体共用请求总预算，慢首部按 `network_timeout` 中断；离线测试 400 项通过、3 项平台限制跳过，mypy 通过；未推送、未部署（待确认策略已由下一行取代） |
| 2026-10-26 复查修正三 | 待确认核对后保留原活动时间占位恢复扫描，冲突候选跳过、原活动最多重试 2 次、明确成功证据立即确认、占位 600 秒或原活动开始前到期；补齐 `reservation_step_failure` 邮件定义并验证告警后重启；默认会话代理连接池也使用 deadline 连接类，代理慢首部受总预算约束；离线测试 412 项通过、3 项平台限制跳过，mypy 通过；未推送、未部署 |
| 2026-10-27 复查修正四 | 待确认占位改为按活动 ID 独立维护，多笔超时各有占位；确认一笔只清理该笔，已确认未进历史时保留冲突保护；成功证据先进入本地冲突集合并重算候选、停止旧候选循环（活动列表与提交回复两条路径）；pending 核对同时使用“我的活动”和活动列表；select 后按剩余预算收紧 socket 超时、连接/TLS 使用剩余时间并事后校验；DNS/CPU 解析仍为已知边界；离线测试 420 项通过、3 项平台限制跳过，mypy 通过；未推送、未部署 |
| 2026-10-27 复查修正五 | pending 双来源核对遇限流等待时立即等待、不请求另一来源、不增加核对次数；成功回复的 pending 清除与 confirmed hold 合并为一次原子保存；confirmed hold 按活动 ID 计入本地计划进度；`--resolve-pending` 同时清理旧格式占位；离线测试 425 项通过、3 项平台限制跳过，mypy 通过；未推送、未部署 |
| 2026-10-27 恢复保护修正 | 创建占位后才清除 pending，覆盖 Ctrl+C 中断和缺少起止时间；人工恢复及迁移保留其他活动的新旧占位；旧确认记录用已有响应补类别，仍未知则暂停新增；空占位集合正确初始化。离线测试 437 项通过、3 项平台限制跳过，mypy 通过；普通开抢与 503/504/429 组合拥塞回放的请求及模拟时序与修复前一致；未推送、未部署 |
| 既有离线基线 | 2026-10-18 的 `d3e2b7b` 记录完整 307 项测试及类型检查通过，覆盖提交确认、状态损坏、恢复通知与异常退避 |
| 学校只读接入 | 历史版本已验证认证、活动和我的活动查询、验证码图片；2026-10-18 核对六条非空列表的真实字段与开放时间解析，没有据此确认预约成功 |
| 邮件送达 | 2026-10-16 一封真实测试邮件被 SMTP 接受，用户确认收到；新增服务商和真实业务通知仍需各自验收 |
| 待验证 | 多次开抢的真实命中率、验证码批量准确率、短信端到端恢复、修复后跨天会话、Windows 故障恢复、无人值守自动解除与验证码连续失败告警的真实链路、多笔占位及升级／中断恢复、代理路径总时限。2026-10-26 的单场真实预约和 Windows 接入已经通过，不再列为待验证 |
| 服务器长期运行 | 2026-10-26 15:11 的历史观测累计约 16 小时 37 分，不能作为当前运行状态或满二十四小时的证据；最新修复后的至少二十四小时运行、SSH 断开、网络恢复及服务器重启后手动恢复仍待验证 |

本次文档核对及恢复保护修正未访问学校、提交预约、发送真实邮件或重启部署。Windows 下的静态检查不作为 Ubuntu systemd 实机证据，参考项目的记录也不能代替本项目。历史 [Linux 截图](images/使用截图.png)和[宣传素材](promo/usage.md)保留旧 `nohup` 等行为，仅供追溯。
