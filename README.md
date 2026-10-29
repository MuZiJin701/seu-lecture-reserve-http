# 东南大学讲座预约 · HTTPS 版

[English](README.en.md)

无需浏览器的讲座预约工具，主要用于 Linux 服务器，也可在 Windows 前台运行。它持续查询讲座、识别验证码、提交预约并核对结果。

目标固定为实际选听 **8 场人文与科学素养系列讲座，法律、艺术、心理健康各至少 1 场**。正式讲座预约够了会等待参加，打卡达标后结束；“SEU咖啡间”沙龙可按配置预约，但不计入目标；测试活动与购票公告不预约、不计数，最终认定以学校为准。

升级后会按新口径重算全部历史，7 场正式讲座加 1 场沙龙不算达标；沙龙不占正式讲座的待参加缺口，已有沙龙预约仍参与时间冲突检查。沙龙默认启用，仍受原有筛选和任务暂停／结束规则约束，配置说明见[使用指南](docs/usage.md#配置)。

## 1. 安装

Ubuntu 24.04：通过 SSH 登录，安装工具。已有工具可跳过；root 用户省略 `sudo`。

```bash
sudo apt update
sudo apt install -y git curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"
```

Windows：安装 [Git](https://git-scm.com/) 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，打开 Git Bash，执行下面的项目命令。Ubuntu 系统安装和 systemd 命令只用于 Linux。

下载项目并安装锁定的依赖，uv 会准备 Python 3.12：

```bash
git clone https://github.com/MuZiJin701/seu-lecture-reserve-http.git
cd seu-lecture-reserve-http
uv sync --locked
```

后续命令都在项目目录执行；安装和运行使用同一账号。已有项目保留原文件夹和配置。

## 2. 填写配置

首次使用时复制样例；已有 `.env` 就跳过复制，直接编辑。

```bash
cp -n .env.example .env
chmod 600 .env
```

用编辑器打开 `.env`，填写学校账号和密码：

```dotenv
SEU_CARD_NUMBER="你的一卡通号"
SEU_PASSWORD='你的密码'
SEU_PREFERRED_CAMPUS='["九龙湖校区","四牌楼校区","丁家桥校区","无锡校区","苏州校区"]'
```

默认允许学校全部校区的线下讲座；校区为 `[]` 时只约线上，也可按需只保留部分校区。线上仍可预约。其他筛选见[完整配置](docs/usage.md#配置)。

邮件可选，不需要就保留空值。需要时填写自己的邮箱和发信授权码：

```dotenv
SMTP_USERNAME=your-address@qq.com
SMTP_PASSWORD='your-authorization-code'
```

QQ、163、126 使用开启 SMTP 后生成的授权码，Gmail 使用应用专用密码；默认发给自己。先按[获取发信密码](docs/usage.md#获取发信密码)完成设置，再继续。更换邮箱或指定收件人见[邮件配置](docs/usage.md#邮件配置)。

保存 `.env`，重启程序后生效。程序直接读取文件，不要执行 `source .env`；进程环境变量优先。凭据和状态文件只留在自己的设备上。

## 3. 先检查

```bash
uv run --locked main.py --check
```

看到 `event=read_only_complete` 表示登录、当前数据和验证码图片检查完成；不会预约、修改任务或发邮件。失败时看[排错说明](docs/usage.md#排错)。

配置了邮件，再执行下面的命令，并确认收件箱收到；不用邮件就跳过。

```bash
uv run --locked main.py --test-mail
```

## 4. 启动预约

以下命令会提交真实预约。**同账号在所有机器及两个项目之间只运行一个实例。** 旧 `nohup` 用户先按[迁移步骤](docs/usage.md#从-nohup-迁移)停止旧实例，保留状态。

本地前台运行，按 `Ctrl+C` 停止：

```bash
uv run --locked main.py
```

Ubuntu 后台运行：先执行 `uv run --locked main.py --status`，确认 `process_running=false`，并确认其他机器没有同账号实例，再启动服务。

```bash
mkdir -p state
chmod 700 state
touch state/runner.log
chmod 600 state/runner.log
sudo systemd-run --unit=seu-lecture-reserve-http --collect --service-type=exec \
  --uid="$(id -un)" --working-directory="$PWD" --expand-environment=no \
  --property=Restart=no --property=UMask=0077 --property=TimeoutStopSec=60 \
  --property="StandardOutput=append:$PWD/state/runner.log" \
  --property=StandardError=inherit \
  "$(command -v uv)" run --locked main.py
```

SSH 断开后继续运行；程序退出或服务器重启后，需手动执行这段启动命令。服务使用项目 `.env`，不自动继承 SSH 临时配置或代理变量；需要代理时先看[服务环境](docs/usage.md#服务环境)。自定义状态目录时同步调整日志路径。

## 5. 查看、停止与恢复

Windows 或 Ubuntu 都可另开终端，进入项目目录查看：

```bash
uv run --locked main.py --status
```

结合 `process_running`、`phase` 和 `last_query_at` 判断进展。`status=scanning` 只表示任务状态，不能证明进程健康或预约成功。筛选原因和更多字段见[状态与日志](docs/usage.md#状态与日志)。

Ubuntu 查看服务及最近输出：

```bash
sudo systemctl status seu-lecture-reserve-http.service --no-pager
tail -n 20 state/runner.log
```

停止后台服务，再用项目状态命令确认 `process_running=false`：

```bash
sudo systemctl stop seu-lecture-reserve-http.service
uv run --locked main.py --status
```

停止后服务可能被回收，显示“未找到”；任务和日志仍保留。重新执行启动命令会接着原任务，不取消已有预约。遇短信认证时后台会保留任务并退出；确认后台已退出后，在前台按提示处理，再恢复后台运行。

预约结果待确认时，先通过“我的活动”和活动列表核对原活动，暂停其他提交。查询失败、记录矛盾或结构不可读的核对不算有效查询，不推进自动解除。连续 3 次有效查询仍未显示且能建立时间占位时，保留每笔预约的独立占位再恢复扫描：跳过冲突候选，原活动最多再提交 2 次；明确成功证据立即计入冲突检查和计划进度。占位约十分钟或到活动开始前失效；限流要求优先等待。旧状态缺少时间或类别时保留保护并暂停新增。`uv run --locked main.py --resolve-pending` 是人工兜底，只清理对应活动，不提交预约。详见[恢复说明](docs/usage.md#入口与恢复)和[开抢与拥塞恢复](docs/usage.md#开抢与拥塞恢复)。

更新代码前先停止旧实例，保留 `.env` 和状态目录；更新后执行 `uv sync --locked`，再按原方式启动。运行中的旧进程不会自动加载修复。

截至 2026-10-29，完整离线测试 **531 项通过、3 项因平台限制跳过**，mypy 通过。本轮为开抢提速：开抢等待期改为后台轮询完成事件唤醒，只对已消费版本之后的新轮询生效、旧通知不空转，版本读取与清除之间完成的轮询也不丢失，快照与版本号在同一把锁内读取；另提供可选开抢对齐（`SEU_POLLER_ALIGN`，默认关闭，需真实开抢对比误差）。开抢前约 2 秒用主会话预热“我的活动”（`SEU_PREWARM`）、约 0.6 秒预取并识别验证码（`SEU_CAPTCHA_PREFETCH`），预热与预取默认开启、可分别关闭，均有开抢前超时上界且剩余时间不足时跳过；准备动作按学校开抢时间戳去重、同一场各最多一次，本地开抢估算取“开抢时间戳 − 学校时钟偏移 − 余量”（余量 `1.1 秒 + 该次列表快照年龄 + 该次列表请求耗时`，余量超过 2 秒时跳过预热、预取与可选对齐并记录 `preparation_skipped`，重算只允许向更早移动），准备与对齐目标取下一场尚未开放的场次，仍在快轮询窗口的旧场次不会挡住后续准备。开抢期另有后台“我的活动”刷新（`SEU_HISTORY_BACKGROUND`，默认开启，约 20 秒一次；主线程只用 30 秒内且无错误的快照，过期、出错或格式非法时回退同步查询，其 429／`Retry-After` 与活动列表线程共用同一服务端等待截止点：每轮扫描先检查共同截止时间再决定是否重启两个后台线程，线程内部未到期也直接停止返回；退避时两个查询线程一起暂停、冷却都钳到共同截止时间（取最长等待），恢复后各自按冷却重启；净请求约每窗口增加 20 次，关闭后回到 60 秒缓存＋同步刷新），活动列表快照过期但后台轮询正在请求时最多等 0.3 秒复用（等待该过期快照之后的新版本）。候选到提交之间只保留两次落盘（验证码阶段首次进入时一次、待确认与 `submitting` 合并一次），并补齐 `rows_fetch_seconds`、`rows_data_age`、`precommit_save_seconds` 与后台历史诊断。事件唤醒与可选对齐不增加请求且不绕过 429／`Retry-After` 等服务端等待，预取超时、判错或超过 5 秒未用会回退现场取图；待确认核对优先使用轮询快照的正向证据，健康快照使普通等待下 `--status` 最多滞后约 1 秒，关键阶段进入前会立即写入 phase。本轮只有离线证据，尚未真实开抢复验。此前完成沙龙计数口径修正（沙龙可按配置预约但不计入目标）与 19:00 真实开抢监控修复（重复确认诊断、Windows 状态写入告警刷屏），并修复后台轮询限流等待未传导主循环、提交前未复查后台等待，以及失败、429、历史不可读等各类中止核对未统一清零计数的问题，补齐相应回归；普通开抢及 503/504/429 组合拥塞回放的请求与等待保持原有行为。真实提交/取消已在测试活动上验证（2026-10-25）；2026-10-26 本机成功预约一场线下艺术讲座并按活动 ID 确认，2026-10-28 19:00 再次成功预约一场线下前沿科技讲座。抢约命中率、短信恢复、至少 24 小时运行及本轮提速收益仍需分别验收；离线通过不保证必抢成功。2026-10-29 修复三处离线复现的核对与等待缺陷：后台线程学到的服务端等待现在会立即停止两个查询线程并拦截同步历史请求入口；活动列表缺少活动 ID 的行不再计为有效缺席、不能据此解除待确认；后台历史快照在接受前按扫描实际使用的结构校验，非法时同轮回退同步查询。补三条回归；后续又补齐线程间共享等待截止点、后台历史含沙龙冲突时间校验与后台查询 6 秒预算，再补五条回归，离线测试 527 项通过、3 项平台限制跳过，mypy 通过；尚未部署或真实验证。同日继续提速：开抢期历史缓存失效时按 2 秒节流主动请求后台刷新，候选路径优先等待这一份后台快照而不是立刻重复同步查询；只读线程停止改为只发信号、由线程退出后关闭会话，主线程不再固定等待在途请求；活动列表快照过期但轮询仍在请求时先等待该单个在途请求，再决定是否同步回退；`submission_reply` 诊断补充提交连接耗时。再补四条回归，离线测试 531 项通过、3 项平台限制跳过，mypy 通过；尚未部署或真实验证。见[验收边界](docs/usage.md#验收记录)、[本机实战日志（2026-10-26）](docs/acceptance/2026-10-26-live-run.md)、[2026-10-28](docs/acceptance/2026-10-28-live-run.md)、[业务规则](docs/requirements.md)和[使用指南](docs/usage.md)。

维护者：[MuZiJin701](https://github.com/MuZiJin701) · [浏览器版](https://github.com/MuZiJin701/seu_lecture_reserve) · [历史宣传素材](docs/promo/usage.md)
