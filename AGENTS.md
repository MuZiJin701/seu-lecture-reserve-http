# 项目约定

本项目是独立的 Python HTTPS 讲座预约客户端。运行方式、当前能力及验收边界见 `docs/usage.md`。

## Agent skills

### Issue tracker

读取、创建或更新任务前，先读 `docs/agents/issue-tracker.md`。任务跟踪使用 GitHub Issues。

### Triage labels

分诊或修改任务标签前，先读 `docs/agents/triage-labels.md`，使用其中的五种角色映射。

### Domain docs

探索业务代码或讨论领域概念前，先读 `docs/agents/domain.md`。采用 single-context：根目录 `GLOSSARY.md` 和 `docs/adr/`。
实现或调整预约行为前，读取 `docs/requirements.md`，区分已确认需求与待验证能力。

## 开发边界

- 认证及业务请求使用通过证书校验的 HTTPS；凭据、会话票据及个人活动记录留在仓库和日志之外。
- 离线检查使用构造响应或本地服务，与真实凭据和学校系统隔离。
- 预约提交结果不明确时，先按活动 ID 查询状态，再决定下一步；避免盲目重试。
- 邮件通知可选，关闭后仍记录终端和本地事件；调整邮件配置或通知行为前，读取 `docs/requirements.md` 中的邮件确认规则及 `docs/usage.md`。
- 真实预约、验证码准确率、会话恢复和长期运行分别按 `docs/usage.md` 验收；产品行为待用户确认后再实现。

## 开发与验证

- 使用 Python 3.12、uv 和锁文件；安装与命令见 `docs/usage.md`。
- CLI 默认加载当前目录 `.env`，登录、筛选、状态目录和邮件使用同一文件；进程环境变量优先，只提交 `.env.example`。离线测试只能加载临时构造的配置文件。
- 修改代码后运行 `uv run --locked mypy seu_reserve` 和相关入口测试；交付前运行 `uv run --locked pytest`。
- `seu_reserve.app.run` 是已确认的主要测试边界；学校响应、时间、人工输入、邮件和临时状态目录可替换，离线测试不访问学校。
