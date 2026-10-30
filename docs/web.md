# Web 管理台与 Linux 部署

[README](../README.md) · [English](../README.en.md) · [业务规则与 CLI](usage.md) · [需求](requirements.md) · [ADR-0008](adr/0008-trusted-environment-http-no-login.md) · [ADR-0009](adr/0009-concurrent-sessions-and-locks.md)

管理台供本人或少数可信操作者集中管理学校账号，不开放注册，也不提供管理员登录。它只使用明文 HTTP，须部署在可信局域网或 VPN 内；同网段任何可达机器都能操作管理台并接触明文的学校凭据，这是已确认的取舍，见下文“风险提示”。FastAPI 提供管理接口，Uvicorn 承载服务；每个学校账号使用独立的 spawn 工作进程。预约循环不依赖浏览器连接，关闭页面不会停止账号。访问学校系统的客户端请求仍使用通过证书校验的 HTTPS。

## 配置与启动

在项目目录执行 `uv sync --locked`，复制 `.env.example` 为 `.env`。已有配置保留原文件，按样例补充字段，不要覆盖。

| 字段 | 含义与默认 |
| --- | --- |
| `SEU_WEB_HOST` | 监听地址，默认 `0.0.0.0` |
| `SEU_WEB_PORT` | HTTP 端口，默认 `8080` |
| `SEU_DATA_DIR` | 统一数据根目录；缺省沿用 `SEU_STATE_DIR`，再缺省为 `state` |
| `SMTP_*` / `MAIL_FROM` | 全部账号共用的发信配置，格式见[邮件配置](usage.md#邮件配置) |
| `SEU_VPN_IP` | 学校网关回退地址，沿用原客户端配置 |

相对路径以所选配置文件所在目录为准；默认加载当前目录 `.env`，也可用 `--config /绝对路径/.env` 指定，进程环境变量优先。`.env` 中的学校账号、筛选和 `SEU_MAIL_SUCCESS` 仅用于旧单账号 CLI；Web 的这些选项在网页逐账号设置。Web 不使用公共 `MAIL_TO` 作为收件人。

```bash
uv run --locked seu-reserve-web
```

随后在可信网络内用浏览器打开 `http://<服务器地址>:8080`；本机部署可用 `http://127.0.0.1:8080`。服务器防火墙或安全组需允许所选端口。使用非 root 用户及大于 1024 的端口即可直接访问，不需要证书、反向代理或 TLS 配置。不要使用 `fastapi dev`、热重载或多个 Uvicorn worker；管理服务固定一个进程，账号进程承担并发。同一数据根目录有管理器锁 `manager.lock`，第二个管理服务启动会失败。

管理台没有认证。作为浏览器侧的防误操作与跨站请求防护，非 GET 的 `/api/*` 请求仍校验 Origin（仅接受浏览器来源）与 `Content-Type: application/json`，并要求 `Content-Length` 不超过 64 KiB；响应保留 CSP、`no-store`、`nosniff` 与 Referrer-Policy，不设置 HSTS。这些校验降低误触和跨站请求风险，但不是认证，也不能替代网络边界。

## 管理账号

1. 新增账号：填写显示名、学校一卡通号、密码及所需选项。保存不会自动登录学校，首次需手动点击启动。
2. 一卡通号唯一且不可修改；编辑时密码留空保留原值，网页不读取原密码。标题按行填写并完整匹配；线下校区不选则不约线下。目标与查询频率沿用原引擎，不能在网页修改。
3. 选中账号查看运行阶段、已选听与待参加数量、必需类别缺口、下次检查、筛选原因、安全事件及邮件状态。首次查询前显示未知；网页每两秒刷新本地快照，不因此额外查询学校。
4. 出现短信提示后，主动点击发送／重发，再输入六位验证码。每条认证输入绑定当前账号和当前挑战，过期输入会被拒绝；学校发送冷却仍保留。未知挑战需按原因处理后重新检查。等待一个账号不会暂停其他账号。
5. 运行中保存配置会先停止并重启该账号，保留原任务。停止中的请求需等待安全退出；已停止账号只保存。提交结果未知时保留待确认保护，恢复后先核对，不盲目重试。
6. 删除会先停止账号，再检查待确认提交及时间占位；存在保护时拒绝删除，先启动完成核对，必要时按[人工核对](usage.md#入口与恢复)使用同账号 CLI。删除只清理本地记录，不取消学校预约。

服务重启自动恢复此前仍希望运行的账号；明确停止、目标完成或不可自动恢复错误保持停止。意外退出最多连续自动重启三次、每次间隔五秒，稳定运行十分钟重置预算；超过预算显示异常并按账号通知规则告警。明确密码错误、状态损坏、OCR 不可用不反复重启。人工启动开启新一轮预算。磁盘或目录故障需修复后重启管理服务；页面会显示管理器健康异常。

十个并发账号是验收目标，没有十账号启动硬上限或排队机制。真实容量取决于内存、CPU、网络与学校响应；离线资源测量不能代表生产容量。

Web 只管理已建立在本数据根目录中的账号，不检测也不导入旧的单账号任务文件；新部署从空数据根目录开始。已纳管账号指已建立在管理台中的账号，可用同账号 CLI 查看或核对：

```bash
uv run --locked main.py --account 你的一卡通号 --status
uv run --locked main.py --account 你的一卡通号 --check
uv run --locked main.py --account 你的一卡通号
# 必要时在交互终端核对待确认任务
uv run --locked main.py --account 你的一卡通号 --resolve-pending
```

`--account` 也接受内部账号 ID。旧单账号命令启动已纳管账号时会提示改用 `--account`；这是操作提示，不是并发限制。CLI 启动的任务可在网页查看本地快照，但认证输入和停止仍在原 CLI 终端完成，建议停止它再用网页修改配置。

## 每账号通知

公共 SMTP 发信账号和授权码仍留在 `.env`。每个学校账号填写一个收信邮箱；留空即关闭该账号邮件，不回退到发信人。填写后还需打开邮件开关，可选成功通知。

启用邮件时，发信配置缺失或不符合本地配置校验会阻止该账号启动；保存运行中账号的新配置也先校验，不因非法输入停掉现有任务。本地校验不尝试 SMTP 登录，错误授权码、连接故障等仍可能在真正发信时才发现。可用“测试邮件”检查实际发送。SMTP 接受不等于已进入收件箱。

重大告警与去重策略沿用原引擎；成功通知可选，不增加日报。运行中发信失败记录在账号状态，不改变预约结果。邮件配置修改后需安全重启服务加载；各账号的学校配置可直接在网页更新。

## 锁与并发

- 数据根目录的 `manager.lock` 保证同一数据根只有一个 Web 管理服务。
- 每个账号状态目录的 `task.lock` 保证同一 `task.json` 只有一个写者。
- Web 内同一学校账号仍只允许一个工作进程，避免重复预约与状态互相覆盖。
- 账号身份锁已移除，CLI、Web 或不同机器可对同一学校账号并发登录；单账号、单机、约 4 分钟只读实测未观察到会话互踢或后登录被拒，见[并发登录验证](acceptance/2026-10-30-concurrent-login.md)与 [ADR-0009](adr/0009-concurrent-sessions-and-locks.md)。该结论不能外推开抢期、多 IP、三个以上会话或提交链路。
- 开抢期仍建议避免额外的诊断登录，以免短信冷却或未知会话策略影响运行中的任务。
- 不同数据根目录的两套任务无法互相发现，也不能互相协调；同一台机器的另一个管理服务会被 `manager.lock` 拒绝。

## systemd 常驻

先在 Linux 手动启动并确认 `http://127.0.0.1:8080` 可访问，再 `Ctrl+C` 停止。下面在项目根目录生成 unit，路径不含空格；若含空格需按 systemd 规则引用路径。以已有普通用户运行，确认该用户能写入数据目录并运行 uv：

```bash
task_project_dir="$(pwd -P)"
task_uv_path="$(command -v uv)"
task_service_user="$(id -un)"
cat > /tmp/seu-reserve-web.service <<EOF
[Unit]
Description=SEU lecture HTTP console
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=${task_service_user}
WorkingDirectory=${task_project_dir}
ExecStart=${task_uv_path} run --locked seu-reserve-web --config ${task_project_dir}/.env
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
KillMode=mixed
TimeoutStopSec=150
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 644 /tmp/seu-reserve-web.service /etc/systemd/system/seu-reserve-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now seu-reserve-web.service
sudo systemctl status seu-reserve-web.service
```

`KillMode=mixed` 先让主服务停止所有工作进程，超时后再由 systemd 清理整个组；不要与旧单账号服务同时启用。服务启动失败时先检查 `.env`、端口和数据目录；`journalctl -u seu-reserve-web.service` 查看服务层错误。不要公开个人状态文件。

更新代码前保留 `.env`，先备份数据，再按顺序操作：

```bash
sudo systemctl stop seu-reserve-web.service
# 在停服期间更新代码，保留全部数据根目录
uv sync --locked
sudo systemctl start seu-reserve-web.service
```

更新后账号按原运行意图恢复；没有证书更换步骤。

## 本地数据与恢复

数据根目录包含 `accounts.sqlite3`（账号与明文密码）、`manager.lock` 和 `accounts/<内部 ID>/`，不再有 `locks/`。每账号目录内沿用 `task.json`、`trust-cookies.json`、设备指纹、`events.log`、`runner.log`、`diagnostics.jsonl` 等文件。CLI 诊断在状态目录的 `diagnostics.jsonl`，Web 诊断在 `<数据根>/accounts/<id>/diagnostics.jsonl`。Cookie 仅保存固定字段与账号归属哈希，无归属或归属不符的文件不会用于登录。

学校密码和 Cookie 按用户决定明文保存，不额外设置文件权限或访问控制。备份时先停止整个服务，将 `.env` 和完整数据根目录备份至受控位置，恢复时沿用同一目录与账号身份；不要只恢复账号数据库而遗漏待确认任务。没有网页导出凭据或批量重置功能。

页面只展示筛选摘要和安全事件；完整诊断留在服务器。截图、日志及数据备份需按个人信息处理。实现与离线证据见[本轮 HTTP 简化验收](acceptance/2026-10-30-http-console.md)、[并发登录验证](acceptance/2026-10-30-concurrent-login.md)与 [HTTPS 版本离线验收](acceptance/2026-10-30-web-offline.md)；真实学校多账号、Linux 部署与长期运行尚待分别验收。

## 风险提示

- 管理台只应部署在可信局域网或 VPN 内，不要把 8080 端口暴露到公网或不可信网络。
- 管理台无管理员登录：同网段任何可达机器都能查看和操作管理台，也能读取明文保存的学校账号密码、讲座数据与运行记录。
- 管理台无 TLS：同网段的窃听与篡改无法被本程序发现。
- 以上取舍以“仅本人或少数人和机器使用”为前提；若以后需要跨网段访问、多人协作或更强边界，可重新引入 TLS 与登录并恢复文件权限约束，见 [ADR-0008](adr/0008-trusted-environment-http-no-login.md) 的可逆性说明。

技术依据：[FastAPI 同步路由与线程池](https://fastapi.tiangolo.com/async/#path-operation-functions)、[Uvicorn HTTP 部署](https://uvicorn.dev/)。预约循环独立于这些路由执行。
