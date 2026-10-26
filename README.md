# 东南大学讲座预约 · HTTPS 版

[English](README.en.md)

无需浏览器的讲座预约工具，主要用于 Linux 服务器，也可在 Windows 前台运行。它持续查询讲座、识别验证码、提交预约并核对结果。

目标固定为实际选听 **8 场正式系列讲座或沙龙，法律、艺术、心理健康各至少 1 场**。预约够了会等待参加，打卡达标后结束；测试活动与购票公告不预约、不计数，最终认定以学校为准。

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

停止后服务可能被回收，显示“未找到”；任务和日志仍保留。重新执行启动命令会接着原任务，不取消已有预约。遇短信认证时后台会保留任务并退出；确认后台已退出后，在前台按提示处理，再恢复后台运行。预约结果待确认时程序只核对原活动并暂停其他提交；连续 3 次有效查询仍未出现时自动解除待确认并继续扫描，无需人工（学校记录延迟时可能重复请求同一活动，重复预约由学校业务拒绝）；`uv run --locked main.py --resolve-pending` 仅作人工兜底，该命令不提交预约。

当前版本已通过离线检查；真实提交/取消已在测试活动上验证（2026-10-25）；2026-10-26 已在本机真实预约一场艺术讲座并按活动 ID 确认（线上版本首发已满，预约的是线下版本）。目标系列讲座命中率、短信恢复及至少 24 小时运行等仍需分别验收，见[验收边界](docs/usage.md#验收记录)和[本机实战日志](docs/acceptance/2026-10-26-live-run.md)。[业务规则](docs/requirements.md)与[使用指南](docs/usage.md)提供详细说明。

维护者：[MuZiJin701](https://github.com/MuZiJin701) · [浏览器版](https://github.com/MuZiJin701/seu_lecture_reserve) · [历史宣传素材](docs/promo/usage.md)
