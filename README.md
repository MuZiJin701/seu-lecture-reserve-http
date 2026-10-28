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

截至 2026-10-28，完整离线测试 **473 项通过、3 项因平台限制跳过**，mypy 通过；本轮只排除沙龙目标计数，保留按配置预约，覆盖类别开关、历史重算、冲突保护及任务停止条件；此前修复了后台轮询限流等待未传导主循环、提交前未复查后台等待，以及失败、429、历史不可读等各类中止核对未统一清零计数的问题，并补齐相应回归；普通开抢及 503/504/429 组合拥塞回放的请求与等待保持原有行为。真实提交/取消已在测试活动上验证（2026-10-25）；2026-10-26 本机成功预约一场线下艺术讲座并按活动 ID 确认，首次记录开抢后筛选结果时线上版本已满。抢约命中率、短信恢复及至少 24 小时运行等仍需分别验收；离线通过不保证必抢成功。见[验收边界](docs/usage.md#验收记录)、[本机实战日志](docs/acceptance/2026-10-26-live-run.md)、[业务规则](docs/requirements.md)和[使用指南](docs/usage.md)。

维护者：[MuZiJin701](https://github.com/MuZiJin701) · [浏览器版](https://github.com/MuZiJin701/seu_lecture_reserve) · [历史宣传素材](docs/promo/usage.md)
