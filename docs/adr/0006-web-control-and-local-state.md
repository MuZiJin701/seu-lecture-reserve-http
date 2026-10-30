---
status: accepted
---

# 轻量 Web 控制台与本地状态分工

2026-10-30 需求澄清第五轮曾选择 Flask，最终确认时用户改选并确认 FastAPI，技术方案调整为 FastAPI、普通 HTML/CSS 和原生 JavaScript，以定时读取状态满足单管理员控制台；SQLite 保存学校账号目录与配置，原有每账号任务 JSON、`task.lock` 及恢复逻辑继续复用。FastAPI 提供请求模型校验，页面继续使用普通 HTML 与原生 JavaScript，无需改成前后端分离应用。相比新增前端构建体系或把全部预约状态迁进数据库，此方案复用现有引擎，并用数据库唯一约束维护学校账号身份；SQLite 与文件不属于同一事务，删除须支持中断恢复，不能先清除待确认保护再处理文件。

采用单个 Web 管理进程调度账号工作进程，各请求使用独立短事务，避免跨请求线程或子进程共享 SQLite 连接；相关约束见 [Python sqlite3 文档](https://docs.python.org/3.12/library/sqlite3.html)。FastAPI 的 ASGI 接入不替代账号进程管理，长时间预约循环仍在独立进程执行；前端保持定时读取状态，不因框架更换引入 SSE 或 WebSocket。用户已整体确认本方案，现已实现，离线证据见 [HTTPS 版本离线验收](../acceptance/2026-10-30-web-offline.md)；完整需求与具体默认见[改造澄清记录](../requirements.md#多账号与-web-改造)。
