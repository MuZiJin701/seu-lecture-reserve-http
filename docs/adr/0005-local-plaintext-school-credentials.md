---
status: accepted
---

# 学校凭据本地明文保存

2026-10-30 需求澄清第四轮，用户明确撤回加密保存选择，改为在服务器本地明文保存学校密码和登录 Cookie，不使用应用层加密及解密密钥；本决策取代 [ADR-0002](0002-encrypted-school-credentials.md)。文件权限边界后来由 [ADR-0008](0008-trusted-environment-http-no-login.md) 取消；凭据仍不得进入仓库、日志或网页响应，网页仅替换密码而不返回原文。能读取服务器凭据文件的人也能取得其中凭据，这是此取舍的实际后果。

不因此新增网页凭据导出功能，也不把文件权限或管理台登录当作凭据保护手段；管理台采用可信环境中的明文 HTTP 且无管理员登录，见 [ADR-0008](0008-trusted-environment-http-no-login.md)。2026-10-30 后续改版由 [ADR-0010](0010-configured-admin-login.md) 恢复管理员登录，HTTP 与文件权限取舍不变；以下保留原决策背景。本决策已实现，离线证据见[HTTPS 版验收](../acceptance/2026-10-30-web-offline.md)与[HTTP 简化验收](../acceptance/2026-10-30-http-console.md)，完整需求见[改造澄清记录](../requirements.md#多账号与-web-改造)。
