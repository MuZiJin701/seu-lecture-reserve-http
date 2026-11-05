# 项目约定

本项目是多账号讲座预约工具。当前产品规则见 [docs/requirements.md](docs/requirements.md)，配置与排错见 [docs/usage.md](docs/usage.md)，Web 与部署见 [docs/web.md](docs/web.md)。

## 工作重点

- 优先解决实战中暴露的取图、提交、确认和恢复问题；先核对最近实战证据，再决定改动。
- 同一行为保留最小有效测试，优先使用已有入口和构造工具；删除重复、过时及只绑定内部实现的断言。凭据保护、提交结果不明时不盲目重提、状态持久化和账号隔离继续验证。
- 文档只维护当前规则、操作及少量实战证据；历史方案和离线验收通过 Git 追溯。变更直接更新归属文档，不逐轮新建验收报告，不在 README、需求和使用指南重复追加经过。
- 实现与真实验证分开；开抢验收关注验证码就绪、提交时间、结果确认和故障恢复。产品行为改变先取得用户确认。

## 开发边界

- 学校请求使用校验证书的 HTTPS。管理台为需要管理员登录的明文 HTTP，仅部署在可信局域网或 VPN；配置启动时读取。
- 学校密码和 Cookie 本地明文保存；凭据、会话票据不进入仓库、终端、邮件或日志。个人活动详情仅在授权界面、本地状态和私有诊断使用，不进入公共事件或邮件；诊断位于本地状态目录的 `diagnostics.jsonl`。
- 提交结果不明确时先按活动 ID 核对；保留待确认、时间占位和任务锁。停止或删除本地数据不取消学校预约。
- 邮件可关闭；调整通知前读取需求中的邮件规则和使用指南，不增加日报或未确认通知类型。

## 阅读入口

预约行为先读[需求](docs/requirements.md)；Web、账号生命周期或部署同时读 [Web 指南](docs/web.md)。

## Agent skills

### Issue tracker

任务与规格使用本仓库的 GitHub Issues；读取、创建、更新 Issue 或关系前读 [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md)。

### Triage labels

采用默认五种分诊角色；分诊或维护标签前读 [docs/agents/triage-labels.md](docs/agents/triage-labels.md)。

### Domain docs

采用 single-context：根目录术语表和 `docs/adr/`。探索业务前读 [GLOSSARY.md](GLOSSARY.md) 和 [docs/agents/domain.md](docs/agents/domain.md)。

## 验证

使用 Python 3.12、uv 和锁文件。修改代码后运行 `uv run --locked mypy seu_reserve` 和相关入口测试，交付前运行 `uv run --locked pytest`。

离线边界为 `seu_reserve.app.run`、`seu_reserve.web.create_app` 和 `Manager`；使用构造响应或本地服务、临时配置和状态，不加载真实 `.env`，不访问学校或发邮件。真实链路验证另行授权。
