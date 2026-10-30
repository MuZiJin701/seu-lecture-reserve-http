---
status: superseded by ADR-0008
---

# 由应用服务器直接提供 HTTPS（已取代）

本决策已由 [ADR-0008](0008-trusted-environment-http-no-login.md) 取代：管理台改为可信环境中的明文 HTTP，不再由应用服务器提供 TLS；以下仅保留当时公网 HTTPS 方案的背景，不再作为实施依据。

2026-10-30 需求澄清第五轮及补充确认，用户选择 uv＋systemd 的 Linux 常驻服务，不使用反向代理，但仍直接提供公网 HTTPS；证书与私钥路径从服务器 `.env` 读取。相比 Caddy 等代理管理证书，部署方需自行准备和续期证书，并按账号恢复语义安全重启加载新证书；关闭网页不结束后台任务。

用户在最终确认时改选并确认 FastAPI，因此用 ASGI 服务器 Uvicorn 替代原 Flask／Cheroot 候选；Uvicorn 的[HTTPS 配置](https://uvicorn.dev/settings/#https)可直接加载证书与私钥，无需反向代理。固定一个 Web worker 并关闭开发热重载，避免多个 Web 管理器各自恢复同一账号；并发由账号工作进程承担。保持单个 Web 管理进程，通过 Python 3.12 的 [spawn](https://docs.python.org/3.12/library/multiprocessing.html#contexts-and-start-methods) 启动账号进程，各自创建 HTTP 会话与 OCR，避免从多线程 Web 服务直接 fork 后继承连接或锁。

用户已整体确认本方案，现已实现，离线证据见[本轮验收](../acceptance/2026-10-30-web-offline.md)；完整需求与具体默认见[改造澄清记录](../requirements.md#多账号与-web-改造)。
