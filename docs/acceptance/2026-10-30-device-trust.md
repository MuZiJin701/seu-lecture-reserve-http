# 2026-10-30 设备信任（记住此设备）验证记录

本文记录可选“记住此设备”（`SEU_TRUST_DEVICE`）在 2026-10-30 的本机真实验证，供后续排查和验收使用。当前使用步骤见[使用指南](../usage.md)，需求见[业务规则](../requirements.md)，会话有效期观测见[2026-10-29 本机实战](2026-10-29-live-run.md)。

记录已脱敏：不包含账号、手机号、凭据、Cookie 或指纹的值、票据、活动 ID、活动标题或完整个人活动列表。原始文件只保存在本机状态目录，不进入仓库。

## 背景

- 学校统一身份认证在账号密码之后要求短信二次验证（2026-10-29 起多次实测）；短时间重复登录可能被学校拒绝发送短信。
- 浏览器登录页提供“记住此设备，下次免二次验证”选项，并在本地持久保存学校签发的 CAS Cookie（`TGT`、`JSESSIONID`）；认证单页应用通过 `verifyTgt`／`casLoginByCookie` 检查既有会话，从而在 Cookie 仍有效时免密码、免短信完成静默登录。
- 本项目把同一能力做成可选开关 `SEU_TRUST_DEVICE`（默认关闭），不改变默认认证行为。

## 运行条件

- 本机 Windows，同一学校账号，单实例；校园网直连应用 CAS（入口直接跳转 `auth.seu.edu.cn`，CAS 服务目标为 `ehall.seu.edu.cn`）；未修改 Clash/TUN、系统 DNS 或路由。
- 验证方式：前台运行 `uv run --locked main.py --check`，完成登录和数据检查，不预约、不写任务；每次尝试都使用新进程。
- 两次尝试使用的本地代码分别为 `cbad3b2`（仅发送信任字段）与 `ac38ee8`（增加 CAS Cookie 持久化与 `verifyTgt` 静默登录）。
- 相关本地文件：状态目录下的 `device-fingerprint` 与 `trust-cookies.json`；原始文件只在本机，不进入仓库。

## 验证过程

| 顺序 | 版本 | 操作 | 结果 |
| --- | --- | --- | --- |
| 尝试 1 | `cbad3b2` | 开启 `SEU_TRUST_DEVICE=true`；首次 `--check` 发送 `rememberTrustDevice` 与本地 `fingerPrint`，按提示完成短信验证 | 紧接着在新进程运行的第二次 `--check` **仍要求短信** → 仅发送指纹字段不足以免短信 |
| 尝试 2 | `ac38ee8` | 开启 `SEU_TRUST_DEVICE=true`；首次 `--check` 照常通过密码＋短信完成，成功后状态目录出现 `trust-cookies.json`；等待约 60 秒以上，再次在新进程运行 `--check` | **未要求短信**，输出 `event=read_only_complete` → 本账号／本机单样本验证 Cookie 静默登录可用 |

## 机制（当前实现）

- 信任字段：开启时 CAS 登录请求发送 `rememberTrustDevice: true` 与稳定 `fingerPrint`；指纹为状态目录 `device-fingerprint` 中的 `stable_<32 位十六进制>`（`0600`，随机生成，缺失、损坏或不可读时重新生成）。
- Cookie 持久化：登录成功后将域名以 `seu.edu.cn` 结尾的 Cookie 原子写入 `trust-cookies.json`（同目录临时文件 + `os.replace`，`0600`，仅保存 name／value／domain／path／secure／expires；过期项在读取时丢弃）。本机保存的文件中观察到学校签发的 `TRUST_DEVICE`（`auth.seu.edu.cn`、`secure`，单样本有效期约 30 天）以及 `TGT`／`JSESSIONID` 等；有效期与轮换以学校实际为准，本文不记录任何 Cookie 值。
- 静默登录：登录前把保存的 Cookie 装入会话；校园直连路径在进入密码登录前显式调用 `verifyTgt`（`service`＋`loginType=account`），只有返回成功且最终页面确认为 `ehall.seu.edu.cn` 应用页（含 `indexjsp.js`）才视为已登录，否则静默回退到原密码＋短信流程。
- 登录成功后再次保存 Cookie。默认关闭时不发送信任字段、不读写这两个文件，认证行为与之前一致；`--check` 开启时同样读写这两个文件，但仍不创建任务、不写诊断、不预约。
- 安全边界：两个文件仅在本地使用，内容不打印、不写入事件日志／诊断／邮件，也不进入仓库；Cookie 文件等效登录凭据。

## 限制与边界

- 单账号、单机、单样本；未验证多次重复、多账号或多设备。
- 学校 `TRUST_DEVICE` 在本样本中约 30 天有效，但 `TGT` 等会话 Cookie 的有效期及信任的轮换／吊销策略均未测；也未测信任注册后的会话过期行为；会话有效期仍以 2026-10-29 的单样本约 1 小时 54 分为参考，设备信任不延长会话。
- Cookie 被拒绝、过期、损坏，或学校 `verifyTgt` 响应结构变化时，静默尝试失败并回退密码＋短信，不会阻塞登录；但 Cookie 加载与静默尝试目前没有逐次诊断，只能从登录是否要求短信推断。
- 删除 `trust-cookies.json` 只会结束静默登录，下次正常登录可重新保存；删除 `device-fingerprint` 按新设备处理。两者都位于状态目录，共享或复制状态目录会让信任行为跟随文件。
- 学校策略可能变化；若仍要求短信，说明静默登录未被接受，保持默认关闭即可。不要在共享或公用电脑开启。

## 结论

- 2026-10-30 本机同账号单样本验证：仅发送 `rememberTrustDevice`＋`fingerPrint` 未能免短信（尝试 1）；登录成功后持久保存学校 CAS Cookie，并在后续新进程中用 `verifyTgt` 静默登录，第二次 `--check` 未要求短信并输出 `event=read_only_complete`（尝试 2）。
- 该结果只证明该账号、该机器、该时刻可用；不改变默认认证行为（默认关闭），不延长会话有效期，学校拒绝或 Cookie 失效时静默回退。
- 原始文件仅在本机状态目录；本文件不含 Cookie／指纹值、凭据、活动 ID、标题或账号数据。
