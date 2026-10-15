# 东南大学讲座预约 · HTTPS 版

[English](README.en.md)

在服务器上持续查找符合条件的讲座，识别图形验证码并尝试预约。直接使用 HTTPS，不需要浏览器；确认成功一场后自动结束。

目前已通过离线测试，真实预约和长期运行仍待验证。首次使用请先运行只读检查。

## 快速开始

适用于 Linux，使用 Python 3.12 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

**1. 下载并安装依赖**

```bash
git clone https://github.com/MuZiJin701/seu-lecture-reserve-http.git
cd seu-lecture-reserve-http
uv sync --locked
```

已有项目目录时，直接进入该目录即可。

**2. 填写 `.env`**

首次使用时复制样例；如果已经填好 `.env`，请保留原文件。

```bash
cp -n .env.example .env
chmod 600 .env
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
uv run --locked seu-reserve --check
```

这一步只登录、查询和获取验证码，不会提交预约，也不需要邮件配置。成功时显示 `event=read_only_complete`。

**4. 启动预约**

先在 `.env` 中填写 `SMTP_HOST`、`MAIL_FROM`、`MAIL_TO` 及所需的 SMTP 账号和密码。邮件使用隐式 TLS，默认端口为 `465`；密码或授权码按邮箱服务商的要求填写。

```bash
uv run --locked seu-reserve
```

此命令会提交真实预约。程序默认每 10 秒查询一次，直到确认预约成功一场或你主动停止。

## 常用配置

修改 `.env` 后，重新启动生效。列表使用 JSON 数组，写法可参照上面的校区示例。

| 配置项 | 作用 |
| --- | --- |
| `SEU_PREFERRED_CAMPUS` | 允许的线下校区，可填多个；默认空列表，不预约线下 |
| `SEU_REQUIRED_CATEGORIES` | 允许的讲座类别，可保留样例值 |
| `SEU_ENABLE_ONLINE` / `SEU_ENABLE_OFFLINE` | 是否允许线上 / 线下，填 `true` 或 `false` |
| `SEU_PREFERRED_LECTURES` | 只匹配指定的完整标题；`'[]'` 表示不限标题 |
| `SMTP_*` / `MAIL_*` | 成功、需要人工认证或持续失败时的邮件通知 |

指定标题仍须满足其他筛选和时间冲突条件。进程环境变量优先于 `.env` 中的同名配置。
全部配置见 [.env.example](.env.example)，格式和排查方法见[使用指南](docs/usage.md)。

## 停止与继续

- 保持 SSH 交互终端开启。需要短信时，按提示输入 `send` 发送短信，再输入六位验证码。
- 按 `Ctrl+C` 停止，不会取消已有预约。再次使用原启动命令会恢复同一个任务。
- 提交结果不明时，程序先核对原预约，不会盲目重复提交；不要删除状态文件来强行重试。
- 已成功的任务再次启动会直接结束。需要再约一场时，明确启动新任务：

```bash
uv run --locked seu-reserve --new-task
```

查看本地任务状态：

```bash
uv run --locked seu-reserve --status
```

## 当前进度

命令行和离线回归测试已完成，实际活动数据适配、真实预约、短信恢复和跨天运行尚待验收。只读检查通过不代表预约已验证；请在 [SEU 门户](https://ehall.seu.edu.cn/)核对最终结果。

真实 `.env` 不纳入 Git，请勿上传凭据或个人活动记录。开发检查命令为 `uv run --locked pytest` 和 `uv run --locked mypy seu_reserve`。

相关项目：[浏览器版本](https://github.com/MuZiJin701/SEU_Lecture_Reserve)；本项目使用独立的 HTTPS 实现。
