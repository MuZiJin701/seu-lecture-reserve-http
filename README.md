# 东南大学讲座预约 · HTTPS 版

[English](README.en.md)

在服务器上持续查找符合条件的讲座，识别图形验证码并尝试预约。直接使用 HTTPS，不需要浏览器；以学校已打卡记录达到 8 次、法律/艺术/心理健康各至少 1 次为目标。

已通过离线测试及真实登录、只读查询验证，真实预约和长期运行仍待验证。首次使用请先运行只读检查。

## 快速开始

使用 Python 3.12 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，以下命令以 Linux shell 为例。Windows 兼容路径已做离线模拟验证，仍待真实 Windows 验收。

**1. 下载并安装依赖**

```bash
git clone https://github.com/MuZiJin701/seu-lecture-reserve-http.git
cd seu-lecture-reserve-http
uv sync --locked
```

已有项目目录时，直接进入该目录即可。

依赖包含 `tzdata`，用于在 Windows 等缺少系统 IANA 时区库的环境中提供 `Asia/Shanghai`。更新依赖及锁文件后，重新运行 `uv sync --locked` 即可安装。

**2. 填写 `.env`**

首次使用时复制样例；如果已经填好 `.env`，请保留原文件。

```bash
cp -n .env.example .env
chmod 600 .env
```

Windows PowerShell 使用以下命令保留已有配置或复制样例；`.env` 和状态目录的访问权限由 Windows ACL 控制，请放在仅自己可访问的目录中。

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

用文本编辑器打开 `.env`，填写账号、密码和筛选条件，例如：

```dotenv
SEU_CARD_NUMBER="你的一卡通号"
SEU_PASSWORD='你的密码'
SEU_PREFERRED_CAMPUS='["无锡校区"]'
```

校区名称须与学校系统一致，按自己的需要修改。账号、筛选和邮件都在这一个文件里配置，程序会自动读取，不需要执行 `export` 或 `source`。

**3. 检查登录和查询**

```bash
uv run main.py --check
```

这一步只登录、查询和获取验证码，不会提交预约，也不需要邮件配置。成功时显示 `event=read_only_complete`。

失败时，`event=task_failed` 会附带 `stage`（失败阶段）、`reason`（安全错误码），HTTP 请求失败还会显示 `http_status`。TLS 失败会显示学校域名 `host`、细分类别 `tls_reason`，以及可确定时的 `proxy_used`；排查时请提供这一整行。

若确认 `vpn.seu.edu.cn` 的本机 DNS 解析有误，可在项目 `.env` 中设置可选的 `SEU_VPN_IP`，保留域名证书校验，且不修改系统 DNS 或 TUN。配置及限制见[使用文档](docs/usage.md#仅在项目内绕过网关-dns-故障)。正常网络无需设置。

**4. 启动预约**

邮件通知可选；不配置邮件也能正常预约，成功、需要人工认证或持续异常时仍在终端和本地日志记录。

需要 Gmail 通知时，只需在 `.env` 中填写完整邮箱地址和应用专用密码，默认自己给自己发信：

```dotenv
SMTP_USERNAME=your-address@gmail.com
SMTP_PASSWORD='your-app-password'
```

程序自动使用 `smtp.gmail.com:465`，通过隐式 TLS 发信。应用专用密码设置及其他邮箱的高级配置见[使用指南](docs/usage.md#gmail-示例)。已有完整 SMTP 配置仍可用；配置不完整或格式错误时会停止启动。

从旧版本升级时，非空的 `SMTP_HOST`、`MAIL_FROM`、`MAIL_TO` 会继续覆盖自动补全。需要关闭邮件时，清空所有邮件字段；`SMTP_PORT` 可以留空、删除或保留默认 `465`。同时检查进程环境变量中的同名配置。

```bash
uv run main.py
```

`main.py` 调用现有命令行入口，检查、状态查询和新任务参数均可照常使用。需要校验锁文件一致性时，可写成 `uv run --locked main.py`；原 `seu-reserve` 命令仍兼容。

此命令会提交真实预约。程序默认每 10 秒查询，优先补齐法律、艺术、心理健康；成功一场后继续。已打卡加有效待参加预约覆盖目标后暂停新增、继续核对，实际已打卡达标后结束。取消或学校标记违约后补足；已结束但打卡/违约尚未确认时暂停新增，避免重复补约。“SEU咖啡间”沙龙不预约、不计数，不设考核截止日期。

次数来自学校“我的活动”，包括手动及其他工具的预约记录，按活动 ID 去重；不需要手工填写完成次数。这是按学校已打卡记录统计，不代替学院最终考核认定。接口证据及历史完整性限制见[接口调查](docs/completion-records.md)。

**终端没有输出不一定是卡住。** 正常查询但没有合适讲座时，程序安静等待，不算失败。异常时会输出事件并延长重试间隔；重启也会等完原有退避时间。详细说明见[状态与日志](docs/usage.md#状态与日志)。

## 常用配置

修改 `.env` 后，重新启动生效。列表使用 JSON 数组，写法可参照上面的校区示例。

| 配置项 | 作用 |
| --- | --- |
| `SEU_PREFERRED_CAMPUS` | 允许的线下校区，可填多个；默认空列表，不预约线下 |
| `SEU_REQUIRED_CATEGORIES` | 候选类别及同等条件下的顺序；保留心理、法律、艺术及其他四类，目标计数独立于筛选 |
| `SEU_ENABLE_ONLINE` / `SEU_ENABLE_OFFLINE` | 是否允许线上 / 线下，填 `true` 或 `false` |
| `SEU_PREFERRED_LECTURES` | 只匹配指定的完整标题；`'[]'` 表示不限标题 |
| `SMTP_*` / `MAIL_*` | 可选的邮件通知；Gmail 只需 `SMTP_USERNAME` 和 `SMTP_PASSWORD` |

指定标题仍须满足其他筛选和时间冲突条件。进程环境变量优先于 `.env` 中的同名配置。
全部配置见 [.env.example](.env.example)，格式和排查方法见[使用指南](docs/usage.md)。

## 停止与继续

- 前台运行时保持 SSH 交互终端开启。需要短信时，按提示输入 `send` 发送短信，再输入六位验证码。断开 SSH 后继续运行的方案见[服务器后台运行](docs/usage.md#服务器后台运行)。
- 按 `Ctrl+C` 停止，不会取消已有预约。再次使用原启动命令会恢复同一个任务。
- 提交结果不明时，程序先核对原预约，不会盲目重复提交；不要删除状态文件来强行重试。
- 新版 `done` 表示整个选听目标已完成，再次启动直接结束；需按学校最新记录重新核对时使用以下命令，已达标不会多约。旧版单场成功状态首次启动会自动迁移、重新核对目标，保留待确认提交、冷却和退避时间：

```bash
uv run main.py --new-task
```

查看本地任务状态：

```bash
uv run main.py --status
```

这是本地任务记录，不是进程健康检查；`status=scanning` 不保证进程仍在运行或最近一次查询成功。查看状态时无需再次启动预约命令。

## 当前进度

已验证真实登录、活动列表查询和验证码获取，并适配“我的活动”的状态、活动 ID 和时间字段。当前验证时可约列表为空；非空候选数据、真实预约、邮件送达、短信恢复和跨天运行尚待验收。只读检查通过不代表预约已验证；请在 [SEU 门户](https://ehall.seu.edu.cn/)核对最终结果。

真实 `.env` 不纳入 Git，请勿上传凭据或个人活动记录。开发检查命令为 `uv run --locked pytest` 和 `uv run --locked mypy seu_reserve`。

相关项目：[浏览器版本](https://github.com/MuZiJin701/SEU_Lecture_Reserve)；本项目使用独立的 HTTPS 实现。
