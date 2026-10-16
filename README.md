# 东南大学讲座预约 · HTTPS 版

[English](README.en.md)

面向 Linux 服务器的讲座预约工具，兼容 Windows。无需浏览器，配置账号和筛选条件后，持续查找讲座、识别验证码并尝试预约。

默认以学校记录中的已打卡 8 场、法律／艺术／心理健康各至少 1 场为目标。预约一场后继续；已有预约覆盖缺口时暂停新增，等实际打卡达标后结束。沙龙不预约、不计数。

已有记录时间重叠时，自动选择互不冲突的场次计数，只调整本地统计，不取消学校预约；最终有效场次以学校认定为准。选择顺序见[使用指南](docs/usage.md#运行和恢复)。

## 使用截图

Linux 服务器后台启动与本地任务状态查询：

![Linux 终端中使用 nohup 后台启动客户端，并查询到本地任务状态 status=scanning](docs/images/使用截图.png)

`status=scanning` 表示本地任务处于扫描状态，不代表进程健康或预约成功。后台进程和日志的核对方法见[使用指南](docs/usage.md#服务器后台运行)；真实预约及长期运行仍待验收。

## 快速开始

先准备 Python 3.12 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

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

**3. 检查登录和查询**

```bash
uv run main.py --check
```

显示 `event=read_only_complete` 表示检查通过。这一步不提交预约、不发邮件；需要短信认证时按终端提示处理。失败时查看 `event=task_failed` 所在行，对照[排错说明](docs/usage.md#先做只读检查)。

**4. 开始预约**

```bash
uv run main.py
```

此命令会提交真实预约。没有合适讲座时安静等待，默认每 10 秒查询。前台运行时保持 SSH 终端开启；按 `Ctrl+C` 停止，再次执行原命令会继续原任务，不会取消已有预约。

## 在服务器后台运行

确认没有其他实例后，在项目目录执行：

```bash
mkdir -p -m 700 state
nohup uv run main.py >> state/runner.log 2>&1 < /dev/null &
echo $! > state/runner.pid
tail -n 20 state/runner.log
```

SSH 断开后仍可运行，但进程退出或服务器重启后不会自动启动。后台遇到需要人工认证的情况会退出并保留任务；确认后台已退出后，执行 `uv run main.py` 在前台处理。修改过 `SEU_STATE_DIR` 时，请同步修改上面的日志路径。进程核对及恢复方法见[服务器后台运行](docs/usage.md#服务器后台运行)。

查看本地任务状态：

```bash
uv run main.py --status
```

`scanning`、`pending`、`done` 分别表示扫描、预约结果待确认、已打卡目标完成。它不检查进程是否存活。不要删除状态文件来强行重试；已完成目标需要重新核对时使用 `uv run main.py --new-task`，升级前已完成的任务也需用此命令按重叠选择的新口径核对。

## 常用配置

修改 `.env` 后重新启动生效。列表使用 JSON 数组，例如 `'["无锡校区"]'`。

| 配置 | 用途 |
| --- | --- |
| `SEU_PREFERRED_CAMPUS` | 允许的线下校区，`'[]'` 表示不约线下 |
| `SEU_ENABLE_ONLINE` / `SEU_ENABLE_OFFLINE` | 是否允许线上／线下，填 `true` 或 `false` |
| `SEU_PREFERRED_LECTURES` | 只匹配完整标题，`'[]'` 表示不限标题 |
| `SEU_REQUIRED_CATEGORIES` | 候选类别及顺序，通常保留样例中的四类 |
| `SEU_VPN_IP` | 首次登录的备用网关 IP；始终先尝试正常 DNS，留空关闭回退 |

指定标题仍须满足其他筛选条件和时间冲突检查。备用 IP 样例为 `121.249.14.2`，仅在网关解析失败、连接关闭／重置或 TLS 意外断开后回退一次；普通超时和证书错误不触发。回退后沿用至进程结束，下次启动重新尝试 DNS。完整边界及 IP 更新方法见[网关 DNS 回退](docs/usage.md#仅在项目内绕过网关-dns-故障)。

邮件通知可选。不配置也能运行；使用 Gmail 时只需补充：

```dotenv
SMTP_USERNAME=your-address@gmail.com
SMTP_PASSWORD='your-app-password'
```

默认自己给自己发邮件，使用应用专用密码。配置方法、其他邮箱及关闭通知的步骤见[使用指南](docs/usage.md#gmail-示例)。

## 使用提醒

- 真实登录和只读查询已验证；真实预约、邮件送达、短信恢复及长期运行仍待验收。请在 [SEU 门户](https://ehall.seu.edu.cn/)核对预约与打卡结果，统计不代替学院最终认定。
- `.env` 包含凭据，请勿上传；预约结果不明时程序先查询原活动，不盲目重复提交。
- 开发检查、详细配置及验收记录见[使用文档](docs/usage.md)。

维护者：[MuZiJin701](https://github.com/MuZiJin701) · [项目地址](https://github.com/MuZiJin701/seu-lecture-reserve-http) · [浏览器版本](https://github.com/MuZiJin701/SEU_Lecture_Reserve)
