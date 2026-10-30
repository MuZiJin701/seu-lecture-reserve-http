# 项目约定

本项目是独立的讲座预约工具：访问学校系统使用通过证书校验的 HTTPS，管理台为本地明文 HTTP、需要配置的管理员账号与密码登录。运行方式、当前能力及验收边界见 `docs/usage.md`。

## Agent skills

### Issue tracker

读取、创建或更新任务及任务关系前，先读 `docs/agents/issue-tracker.md`。任务跟踪使用 `MuZiJin701/seu-lecture-reserve-http` 的 GitHub Issues。

### Triage labels

分诊、配置仓库标签或修改任务标签前，先读 `docs/agents/triage-labels.md`，使用其中的五种角色映射。

### Domain docs

探索业务代码、讨论领域概念或迁移旧领域文件前，先读 `docs/agents/domain.md`。采用 single-context：根目录 `GLOSSARY.md` 和 `docs/adr/`。
实现或调整预约行为前，读取 `docs/requirements.md`，区分已确认需求与待验证能力。调整 Web 管理、账号生命周期或部署时同时读取 `docs/web.md`。

## 开发边界

- 认证及业务请求使用通过证书校验的 HTTPS；凭据、会话票据及个人活动记录留在仓库和日志之外。
- 管理台使用明文 HTTP 与管理员登录，只部署在可信局域网或 VPN；管理员通过服务器配置维护、同等权限共享学校账号，配置启动时读取。身份边界见 ADR-0010，传输与文件权限取舍仍见 ADR-0008；Origin／Content-Type／请求体大小校验及 CSRF 防护不能替代登录认证。
- 离线检查使用构造响应或本地服务，与真实凭据和学校系统隔离。
- 预约提交结果不明确时，先按活动 ID 查询状态，再决定下一步；避免盲目重试。
- 运行诊断写在 CLI 状态目录的 `diagnostics.jsonl`，Web 按账号位于数据根目录的 `accounts/<id>/diagnostics.jsonl`，仅本地，不进入终端、邮件或事件日志；旧单账号 CLI 邮件默认只发重大告警（人工认证、持续五分钟查询失败、验证码或提交连续五分钟失败、异常退出），`SEU_MAIL_SUCCESS=true` 时额外发送每次成功通知；已纳管账号填写收信邮箱后自动发送重大告警及每次成功通知，留空不发邮件。两种模式均不发日报。
- 邮件通知可选，关闭后仍记录终端和本地事件；调整邮件配置或通知行为前，读取 `docs/requirements.md` 中的邮件确认规则及 `docs/usage.md`。
- 真实预约链路、开抢命中率、验证码准确率、会话恢复和长期运行分别按 `docs/usage.md` 的验收记录确认；实现完成与真实验证分开记录，产品行为待用户确认后再实现。

## 开发与验证

- 使用 Python 3.12、uv 和锁文件；安装与命令见 `docs/usage.md`。
- CLI 默认加载当前目录 `.env`，登录、筛选、状态目录和邮件使用同一文件；进程环境变量优先，只提交 `.env.example`。离线测试只能加载临时构造的配置文件。
- 修改代码后运行 `uv run --locked mypy seu_reserve` 和相关入口测试；交付前运行 `uv run --locked pytest`。
- `seu_reserve.app.run` 是已确认的主要测试边界；学校响应、时间、人工输入、邮件和临时状态目录可替换，离线测试不访问学校。

- Web 管理接口以 `seu_reserve.web.create_app` 和 `Manager` 为离线边界；进程隔离使用构造工作进程，管理台不需要 TLS 证书，不加载真实 `.env`。
