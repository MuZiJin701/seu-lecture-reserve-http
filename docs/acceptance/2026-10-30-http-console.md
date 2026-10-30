# 2026-10-30 HTTP 管理台简化验收

[需求](../requirements.md#多账号与-web-改造) · [Web 指南](../web.md) · [ADR-0008](../adr/0008-trusted-environment-http-no-login.md) · [ADR-0009](../adr/0009-concurrent-sessions-and-locks.md) · [同账号并发登录验证](2026-10-30-concurrent-login.md)

本轮把管理台改为明文 HTTP、移除管理员登录、取消文件权限限定、删除旧账号迁移，并移除账号身份锁；同时修复上一轮只读审查发现的问题。以下证据使用 Windows、Python 3.12、锁定依赖和临时构造的数据；**没有加载真实 `.env`，没有连接学校，没有提交真实预约或发送真实邮件**。

## 变更摘要

- **管理台传输**：删除 TLS 证书/私钥配置、证书加载检查与 HTTPS 往返内容；Uvicorn 直接提供明文 HTTP，默认 `0.0.0.0:8080`（`SEU_WEB_HOST`／`SEU_WEB_PORT` 可配）。访问学校系统的客户端请求仍使用通过证书校验的 HTTPS。
- **管理台认证**：删除登录页、`--hash-password`、scrypt 哈希、会话 Cookie、CSRF token 与登录失败限速；管理台不区分操作者身份。保留非 GET `/api/*` 的 Origin 同源、`Content-Type: application/json` 与 `Content-Length`（≤64 KiB）校验，所有响应保留 CSP、`no-store`、`nosniff`、Referrer-Policy，不设置 HSTS；这些校验不是认证。
- **数据与权限**：删除代码、文档与命令中的 `chmod`、`UMask`、`0600`／`0700` 等文件权限限定；`.env`、数据目录与日志仍不得提交或公开。
- **旧账号迁移**：删除 `--import-config`、`import_legacy`、旧任务检测与「须先导入」提示；新部署从空数据根目录开始，旧的单账号任务文件不再检测或导入。保留不带 `--account` 的旧单账号 CLI。
- **锁**：删除账号身份锁 `AccountLock` 与 `locks/`，取消 CLI／Web 同号互斥；保留每账号状态目录的 `task.lock`（同一 `task.json` 只有一个写者）、数据根的 `manager.lock`（同一数据根只有一个管理服务）和 Web 内同账号单 worker。依据见 [ADR-0009](../adr/0009-concurrent-sessions-and-locks.md)。
- **前端**：删除登录视图、退出按钮与管理员名展示；页面打开即读取元数据并展示管理台。
- **上一轮审计问题**：修复登录失败表全局上限（随登录删除失效）、自动恢复意图被启动异常误清、崩溃告警线程与删除/关闭竞态、早退响应缺安全头、`SEU_DATA_DIR` 空值未校验、CLI 账号类错误统一塌缩、`sqlite3.Error` 未捕获、`load_trust_cookies` 无归属默认加载、请求体依赖 Starlette 私有 `request._body`；同步修正 `verifyTgt` 适用范围、缺失的 `SEU_DATA_DIR` 配置说明、`--resolve-pending` 措辞、诊断路径与中英 README 不对称等文档问题。HSTS、POSIX 权限测试与迁移防护随本轮删除而失效。

## 自动检查

| 检查 | 结果与范围 |
| --- | --- |
| `uv run --locked pytest -q` | **637 通过、3 跳过**，约 1 分钟；跳过的 3 项为两项 Unix 信号测试与一项 Windows 打开文件句柄下的原子替换测试（平台限制） |
| `uv run --locked mypy seu_reserve` | **11 个源文件通过** |
| `uv lock --check` | 锁文件一致（47 个包） |
| `node --check seu_reserve/web_assets/app.js` | JavaScript 语法通过 |
| `uv build --wheel` | 构建通过；wheel 含 Web 模块与 HTML、CSS、JS、SVG 四个资源 |
| 文档链接 | 29 份 Markdown、208 个本地链接及锚点检查通过，0 失效 |

新增/更新的离线回归覆盖：无登录访问与 `/api/meta`；Origin 不同源 403、非 JSON 415、缺 `Content-Length` 411、超限 413；早退响应安全头；账号增删配置与身份不可改、运行中配置安全重启、待确认/占位删除保护；重启预算与十分钟重置、关闭保留运行意图、关闭回收子进程、恢复意图不被启动异常误清；告警线程在账号删除后不发信且不重建状态目录、`close()` 后不发信；`manager.lock` 第二实例拒绝；状态目录非十六进制/符号链接/联接拒绝；十个真实 spawn 进程隔离与第十一个可启动；真实引擎短信桥；真实 Uvicorn 明文 HTTP 往返；`main()` 的 `invalid_port`、`invalid_data_directory`、`unknown_configuration_key`、`manager_already_running` 原因码。CLI 侧新增 `--account` 状态查看、不存在账号、空数据根、状态目录占用事件。

本轮未重新执行浏览器人工检查；README 中的截图是 HTTPS＋管理员登录版本的演示数据，不反映当前无登录界面。旧记录[2026-10-30 HTTPS 版本离线验收](2026-10-30-web-offline.md)保留原数字与当时结论，作为历史证据。

## 尚未验证

真实学校多账号登录/查询/预约、Linux systemd 部署、真实 SMTP 送达、真实浏览器下的无登录界面操作、跨机器明文 HTTP 访问、开抢期表现与长期运行均未验证。既有单账号真实样本不替代这些验收。
