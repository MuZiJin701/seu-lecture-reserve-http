# 东南大学讲座预约 · HTTPS 版

[English](README.en.md)

面向 Linux 服务器的讲座预约工具，兼容 Windows。无需浏览器，配置账号和筛选条件后，持续查找讲座、识别验证码并尝试预约。

默认以学校记录中的已打卡 8 场、法律／艺术／心理健康各至少 1 场为目标。预约一场后继续；已有预约覆盖缺口时暂停新增，等实际打卡达标后结束。沙龙不预约、不计数。

已有记录时间重叠时，自动选择互不冲突的场次计数，只调整本地统计，不取消学校预约；最终有效场次以学校认定为准。选择顺序见[使用指南](docs/usage.md#运行和恢复)。

## 使用截图

Linux 服务器后台启动与本地任务状态查询：

![Linux 终端中使用 nohup 后台启动客户端，并查询到本地任务状态 status=scanning](docs/images/使用截图.png)

`status=scanning` 表示本地任务处于扫描状态，不代表进程健康或预约成功。后台进程和日志的核对方法见[使用指南](docs/usage.md#服务器后台运行)；真实预约及长期运行仍待验收。

联合宣传海报：[高清 PNG](docs/promo/seu-lecture-poster.png) · [Python 生成说明](docs/promo/usage.md)，同时介绍 Windows 浏览器版与 Linux HTTPS 版。

## 快速开始

Ubuntu 24.04 LTS 示例：使用已有的 sudo 或 root 账号，通过 SSH 安装并运行；安装和运行使用同一账号。若已是 root，省略下面的 `sudo`。

```bash
sudo apt update
sudo apt install -y git curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"
uv python install 3.12
```

uv 安装方式及 Python 管理见[官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)和 [Python 指南](https://docs.astral.sh/uv/guides/install-python/)。Windows 可按官方 Windows 步骤安装 uv，使用 Git Bash 执行下方项目命令并前台运行；不执行 Ubuntu 的系统安装和后台管理命令。

**1. 下载并安装依赖**

```bash
git clone https://github.com/MuZiJin701/seu-lecture-reserve-http.git
cd seu-lecture-reserve-http
uv sync --locked
```

**2. 填写配置**

首次使用时复制样例；已有 `.env` 时保留原文件。

```bash
cp -n .env.example .env
chmod 600 .env
```

用文本编辑器打开 `.env`，填写账号、密码及需要的线下校区：

```dotenv
SEU_CARD_NUMBER="你的一卡通号"
SEU_PASSWORD='你的密码'
SEU_PREFERRED_CAMPUS='["无锡校区"]'
```

校区名称须与学校系统一致，请按需修改。默认校区列表为空，只预约线上讲座。其他配置可以先保留样例值。程序自动读取当前目录的 `.env`，不用执行 `source`；进程环境变量优先。

其他常用配置如下，修改 `.env` 后重新启动生效。列表使用 JSON 数组，例如 `'["无锡校区"]'`。

| 配置 | 用途 |
| --- | --- |
| `SEU_PREFERRED_CAMPUS` | 允许的线下校区，`'[]'` 表示不约线下 |
| `SEU_ENABLE_ONLINE` / `SEU_ENABLE_OFFLINE` | 是否允许线上／线下，填 `true` 或 `false` |
| `SEU_PREFERRED_LECTURES` | 只匹配完整标题，`'[]'` 表示不限标题 |
| `SEU_REQUIRED_CATEGORIES` | 候选类别及顺序，通常保留样例中的四类 |
| `SEU_VPN_IP` | 首次登录的备用网关 IP；始终先尝试正常 DNS，留空关闭回退 |

指定标题仍须满足其他筛选条件和时间冲突检查。备用 IP 样例为 `121.249.14.2`，仅在网关解析失败、连接关闭／重置或 TLS 意外断开后回退一次；普通超时和证书错误不触发。回退后沿用至进程结束，下次启动重新尝试 DNS。完整边界及 IP 更新方法见[网关 DNS 回退](docs/usage.md#仅在项目内绕过网关-dns-故障)。

邮件通知可选，未配置也能预约。每位用户使用自己的发件邮箱，收件人默认自己；可用 `MAIL_TO` 指定其他收件人。Gmail 配置步骤：

1. 登录自己的 Google 账号，开启[两步验证](https://myaccount.google.com/signinoptions/two-step-verification)。
2. 打开[应用专用密码](https://myaccount.google.com/apppasswords)，为本工具创建一个应用专用密码。
3. 复制生成的 16 字符密码，去掉分组空格，填入 `.env`，不要填写日常 Google 登录密码：

```dotenv
SMTP_USERNAME=your-address@gmail.com
SMTP_PASSWORD='your-app-password'
```

程序自动使用 `smtp.gmail.com:465`，默认自己给自己发邮件。旧配置中的非空 `SMTP_HOST`、`SMTP_PORT`、`MAIL_FROM`、`MAIL_TO` 优先；要用自动默认值，请清空相应旧覆盖项。应用密码入口不可用、密码撤销及其他邮箱配置见[使用指南](docs/usage.md#gmail-示例)和 [Google 官方说明](https://support.google.com/accounts/answer/185833?hl=zh-Hans)。

启用邮件后，重要事件使用中文通知；每天北京时间 **20:00** 汇报最近核对的进度和待处理问题。当天 20:00 后启动会在首次安全等待时汇报当天；同一状态目录每天最多尝试一次，失败不补发。程序停止或目标完成后不继续汇报。

**3. 检查登录和可选邮件**

```bash
uv run main.py --check
```

显示 `event=read_only_complete` 表示登录、当前查询数据和验证码图片检查通过，不代表已验证真实预约。列表非空时也校验讲座字段；列表为空时无法验证其字段。这一步不提交预约、不发邮件；需要短信认证时按终端提示处理。失败时查看 `event=task_failed` 所在行，对照[排错说明](docs/usage.md#先做只读检查)。

如已配置邮件，单独发送一封测试邮件；此入口不登录学校、不启动预约任务：

```bash
uv run main.py --test-mail
```

`event=test_mail_accepted` 仅表示 SMTP 服务器接受，请另查收件箱确认送达。失败时终端给出固定错误分类，不输出密码或服务器响应；排错见[邮件说明](docs/usage.md#邮件通知与每日汇报)。

**4. 开始预约**

```bash
uv run main.py
```

每次启动先读取学校最新进度；目标已完成时核对后退出。目标未完成时，此命令会提交真实预约。没有合适讲座时安静等待，默认每 10 秒查询。前台运行时保持 SSH 终端开启；按 `Ctrl+C` 停止，再次执行原命令会继续原任务，不会取消已有预约。

## 在服务器后台运行

先执行 `uv run main.py --status`，确认 `process_running=false`，且其他机器或浏览器版没有同账号实例，再在项目目录执行：

```bash
mkdir -p -m 700 state
nohup uv run main.py >> state/runner.log 2>&1 < /dev/null &
echo $! > state/runner.pid
tail -n 20 state/runner.log
```

确认 `process_running=true`、`last_query_at` 更新后再断开 SSH；重新连接后先回到本项目目录查看状态和日志。SSH 断开后仍可运行，但进程退出或服务器重启后不会自动启动。后台遇到需要人工认证的情况会退出并保留任务；确认后台已退出后，执行 `uv run main.py` 在前台处理。修改过 `SEU_STATE_DIR` 时，请同步修改上面的日志路径。进程核对及恢复方法见[服务器后台运行](docs/usage.md#服务器后台运行)。

## 查看任务状态

在项目目录查看本地任务记录，不会启动新的预约进程：

```bash
uv run main.py --status
```

| 状态 | 含义 |
| --- | --- |
| `not_started` | 尚无本地任务 |
| `scanning` | 任务尚未完成，仍需扫描或核对进度 |
| `pending` | 预约提交结果待确认 |
| `done` | 已打卡目标完成 |

输出首行仍为 `status=...`，后面增加运行信息：

| 字段 | 怎么看 |
| --- | --- |
| `process_running` | 是否有进程持有本任务的锁；`false` 表示没有正在运行的预约实例 |
| `phase` | `authentication` 为登录中，`waiting` 为扫描后等待，`waiting_for_attendance` 为等待打卡，`backoff` 为异常退避，`manual_required` 为需人工处理 |
| `last_query_at` / `last_query_age_seconds` | 最近成功查询时间及距今秒数；`never` / `unknown` 表示尚无成功记录 |
| `updated_at` / `snapshot_age_seconds` | 运行状态最后更新时间及距今秒数；长时间不更新时结合阶段和 `next_check_at` 排查 |
| `failure_count` / `last_error` / `last_error_at` | 连续失败次数、最近错误及发生时间；恢复后次数清零，旧错误保留供排查 |
| `activity_count` / `candidate_count` | 最近一次讲座列表条数及通过筛选的候选数；`unknown` 表示该轮未查询列表 |
| `mail_status` | `disabled` 未配置、`ready` 已配置、`sending` 发送中、`accepted` 服务器接受、`failed` 发送失败；不代表收件箱送达 |

`activity_details` 展示最近一轮各讲座的标题、首个筛选原因及预约开放／截止时间，结合 `selection_updated_at` 判断是否过期。例如 `reservation_not_started` 表示未开放，`campus_not_selected` 表示线下校区未选，`time_conflict` 表示与已有预约冲突。详情见[运行诊断](docs/usage.md#运行诊断)。这些私有详情不写入事件日志或邮件，请勿公开状态文件或含详情的终端输出。

`last_mail_status`、`last_mail_error`、`last_mail_event`、`last_mail_duration_seconds` 显示最近一次任务通知的结果、错误分类、事件及发送耗时，跨重启保留；未完成或旧记录的耗时显示未知。

判断是否在正常工作，结合 `process_running=true`、最近的成功查询时间及当前阶段。候选数为 0 且成功查询时间持续更新，表示本轮没有可选候选；可能尚未开放预约或不满足筛选条件，不代表预约提交已经验证。正常轮询不刷屏。`phase=unknown` 表示旧任务尚无新运行信息或信息无效，不能当作健康证明。

**`scanning` 本身不代表程序还在运行。** 使用上述后台启动方式时，也可检查 PID 文件对应的进程：

```bash
ps -p "$(cat state/runner.pid)" -o pid,ppid,args
```

没有对应进程行，说明该 PID 已退出；即使进程存在，也不能据此确认最近一次查询成功。查看最近输出和事件：

```bash
tail -n 20 state/runner.log
tail -n 20 state/events.log
```

日志可能保留旧错误，请结合时间判断；尚无日志时可能提示文件不存在。修改过 `SEU_STATE_DIR` 时，请调整这里和下方命令的路径。

## 停止与继续

前台运行时，在原终端按 `Ctrl+C`。后台运行时，先读取 PID 并核对命令和工作目录：

```bash
reserve_pid=$(cat state/runner.pid)
ps -p "$reserve_pid" -o pid,ppid,args
readlink "/proc/$reserve_pid/cwd"
```

确认命令是 `uv run main.py`、工作目录是本项目后，再执行：

```bash
kill -TERM "$reserve_pid"
```

`uv` 会把停止信号转发给 Python 子进程。再次执行上面的 `ps` 命令核对退出；`No such process` 表示该 PID 已结束。PID 文件可能过期，不要跳过核对。

停止不会取消学校已有预约。保留 `state` 目录，再执行 `uv run main.py` 或上述后台启动命令即可继续原任务。每次正常启动都会重新登录并核对学校进度，即使本地曾完成。仍达标时结束且不重复发送完成邮件；不足则继续补足。`--new-task` 仅保留为已完成任务重新核对的兼容入口，不能重置扫描中或待确认任务。

## 使用提醒

- 已核对非空真实讲座列表：按预约时间窗和发布状态筛选，线上线下仅识别标题开头明确的 `【线上】`／`【线下】` 标记；未知字段会报错。名额和资格仍以学校预约前检查为准，服务器时钟须准确。详情见[接口边界](docs/usage.md#当前业务数据契约与限制)。
- 此前版本的真实登录、只读查询及测试邮件送达已有验证记录；本轮新增行为仅做离线回归，真实预约、短信恢复及更新后的长期运行仍待验收。请在 [SEU 门户](https://ehall.seu.edu.cn/)核对预约与打卡结果，统计不代替学院最终认定。
- `.env` 包含凭据，请勿上传；预约结果不明时程序先查询原活动，不盲目重复提交。
- 开发检查、详细配置及验收记录见[使用文档](docs/usage.md)。

维护者：[MuZiJin701](https://github.com/MuZiJin701) · [项目地址](https://github.com/MuZiJin701/seu-lecture-reserve-http) · [浏览器版本](https://github.com/MuZiJin701/SEU_Lecture_Reserve)
