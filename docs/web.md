# 东南大学素养讲座预约控制台与 Linux 部署

[README](../README.md) · [English](../README.en.md) · [业务规则与 CLI](usage.md) · [改版需求](console-redesign.md) · [管理员边界](adr/0010-configured-admin-login.md) · [本轮验收](acceptance/2026-10-30-console-redesign.md)

管理台供本人或少数可信管理员集中管理学校账号。使用明文 HTTP，仅部署在可信局域网或 VPN；管理员账号与学校账号相互独立，全部管理员共享同一套学校账号及操作权限。FastAPI / Uvicorn 固定一个管理进程，每学校账号独立工作进程，访问学校仍使用通过证书校验的 HTTPS。

## 配置与启动

在项目目录运行 `uv sync --locked`，复制 `.env.example` 为 `.env`，已有文件保留。样例中的管理员列表是空对象 `'{}'`，会阻止启动；先按下面的格式直接填写账号和密码，无需执行哈希命令：

```dotenv
SEU_WEB_ADMINS='{"admin":"换成你的密码","other":"另一个密码"}'
SEU_WEB_HOST=0.0.0.0
SEU_WEB_PORT=8080
SEU_DATA_DIR=state
```

列表为 JSON 对象，可添加多个用户名与各自密码；普通密码为 1–1024 字符，空密码、非字符串、重复用户名或空列表会阻止启动，不回退为匿名访问。特殊字符先按 JSON 转义，再按 `.env` 外层单引号规则转义（反斜杠写成 `\\`，单引号写成 `\'`）；`${...}` 保留原文，不做变量展开。首次部署须替换示例密码；不提供默认密码、注册或网页管理员管理。

启动时将直接填写的密码用 PBKDF2-HMAC-SHA256、600000 次迭代和随机盐派生为内存哈希，不改写 `.env`；配置文件仍含明文密码，不要提交或分享。已有完整哈希可与普通密码混用，`uv run --locked seu-reserve-web --hash-password` 仍作为可选工具；`pbkdf2_sha256$` 是保留的哈希前缀，以该前缀开头但格式无效的值会阻止启动。

`.env` 在启动前准备，进程环境变量优先；管理员配置只在启动时读取，增删或改密后重启管理服务生效。重启前原配置及登录仍有效。其他公共配置沿用 `SMTP_*` / `MAIL_FROM`、`SEU_VPN_IP`；学校账号在网页维护。相对路径以配置文件目录为准，不要 `source .env`。

```bash
uv run --locked seu-reserve-web
# 或指定配置文件
uv run --locked seu-reserve-web --config /绝对路径/.env
```

本机打开 `http://127.0.0.1:8080`，其他可信机器使用服务器实际 IP。`0.0.0.0` 仅为绑定所有网卡，不是浏览器地址。服务不需要 TLS 证书，不要启用开发热重载或多个 Uvicorn worker。

## 管理员登录

- 管理员会话固定有效 12 小时，允许多浏览器并存，刷新不续期；退出只结束当前会话，管理服务重启使全部会话失效。
- 关闭网页、退出或登录到期不停止后台预约。修改管理员配置后的重启仍按现有任务恢复语义安全停止和恢复学校账号。
- 登录限速只统计**失败**尝试：每来源每分钟最多 5 次失败，全局每分钟最多 100 次失败；成功登录不再占用预算并清除该来源的失败记录，超限响应带 `Retry-After: 60`。并发请求在计算哈希前原子预留校验名额，超出预算的直接 429，不会让多条请求同时通过检查。内存会话上限 1024；学校凭据、管理员密码、会话及短信码不写入日志或响应错误。
- 学校数据接口均要求登录。非 GET `/api/*` 保留 Origin、JSON 类型与请求大小校验：除 `Content-Length` 外还按实际接收的字节数累计（含分块传输），超过 64 KiB 返回 413，不依赖客户端声明的长度；已登录的写操作还需当前会话的 `X-CSRF-Token`。Cookie 使用 HttpOnly、SameSite=Strict；普通 HTTP 不设置 Secure 或 HSTS，仍不能避免传输窃听。
- 自行调用 API 时，登录或 `GET /api/session` 返回 CSRF 值；`PUT /api/accounts/<id>` 需携带读取到的 `revision`。旧版本保存返回 `409 configuration_conflict`，不会停止工作进程或覆盖新配置。账号正由 CLI `--account` 持锁运行时，启动、保存、停止和删除返回 `409 state_in_use`，不会伪造成功或改动配置；管理台自己运行中的账号仍可保存，保存沿用安全停止、写入和重启。保存期间持有该账号检查点锁完成写入，CLI `--account` 取得锁后重新读取配置，两者不会出现“页面新配置、运行旧配置”的分叉。

## 单页管理账号

1. 在卡片队列末尾点击“新增账号”，填写名称、一卡通号和密码，保存后手动启动。已有一卡通号不可修改，密码留空保留原值。
2. 选中卡片后，桌面右侧、手机卡片下方显示进度、配置与预约。点击保存才生效；运行中沿用安全停止及重启，保留原任务与待确认保护。
3. 自动刷新不覆盖正在输入的内容；切换账号或退出有未保存修改时使用页面内确认框。多人同时编辑时拒绝旧表单覆盖，保留输入并提供重新加载入口。
4. 可见筛选只有类别、校区与线上 / 线下。全部已纳管账号（含旧账号和 `main.py --account`）统一清除标题限制，开启设备信任、预热、验证码预取、后台历史刷新、开抢对齐和成功通知，网页不显示这些开关；旧单账号 CLI 保留原配置能力。
5. 邮箱留空不发邮件；填写后自动开启重大告警和每次预约成功通知，不回退至公共发信邮箱。公共 SMTP 无效会阻止有收信邮箱的账号正常启动；只读 `--check` / `--resolve-pending` 不校验邮件。测试邮件由当前账号收件人接收，SMTP 接受与收件箱送达仍有区别。
6. 人工认证按账号显示。主动发送 / 重发短信并输入六位数字；未知挑战按提示处理。其他学校账号继续运行。
7. 删除先停止账号，存在待确认或时间占位时拒绝删除，先运行核对。删除仅清理本地数据，不取消学校预约。

页面中的“账号”指学校账号，右上角单独标明管理员身份。启动预约、停止、测试邮件和删除账号采用一致尺寸；深色主按钮、描边辅助按钮和红色删除按钮区分操作层级。管理员名称、退出登录、账号名称及关键状态使用较大字号，手机布局按宽度换行。

当前预约展示该学校账号全部仍有效、尚未结束的预约，包括手动预约或其他工具预约的场次；显示标题、类别、举办时间、地点 / 线上方式和状态。清单复用引擎已有查询并保存精简本地快照，不增加学校请求，不保存完整个人历史，不替代预约核对证据。首次未查询显示“尚未同步”；停止、查询失败或快照超过 30 秒时标为上次数据。待确认提交及时间占位单列，不能当作学校预约成功；清单与选听目标的计数计划也不同。

服务重启恢复仍希望运行的账号。意外退出最多连续重启三次、间隔五秒，稳定运行十分钟重置预算；明确密码错误、状态损坏和 OCR 不可用不反复重启。页面每两秒读取本地状态，关闭页面不会影响工作进程。十账号是验收目标，没有启动硬上限。

## CLI 与锁

```bash
uv run --locked main.py --account 你的一卡通号 --status
uv run --locked main.py --account 你的一卡通号 --check
uv run --locked main.py --account 你的一卡通号
uv run --locked main.py --account 你的一卡通号 --resolve-pending
```

`--account` 也接受内部 ID。CLI 不需要管理台登录；CLI 的认证输入和停止须在原终端进行。外部 CLI 运行中的账号在网页禁用编辑、停止和删除，服务端对启动、保存、停止和删除同样返回 `state_in_use`，不会把未生效的停止报成成功，也不会让保存的配置与 CLI 实际运行的配置分叉。旧单账号模式仍保留，已纳管账号应使用 `--account`。

- 数据根 `manager.lock` 限制同一数据根仅一个 Web 管理服务。
- 每账号 `task.lock` 限制同一检查点仅一个写者；删除先加锁再检查是否存在 `task.json`，CLI 正常启动在 OCR 前持锁复查账号，避免删除与首次启动竞争。
- 同一学校账号允许并发会话，保留数据锁而非身份锁，见 [ADR-0009](adr/0009-concurrent-sessions-and-locks.md)。不同数据根不能互相发现或协调。

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

页面展示精简预约清单、进度、筛选摘要和安全事件；`task.json` 的 `reservations` 只保留当前预约展示字段与同步时间，不保存完整个人历史。完整诊断留在服务器。截图、日志及数据备份需按个人信息处理。新版证据见[改版验收](acceptance/2026-10-30-console-redesign.md)，历史 HTTP / HTTPS 版本的记录保留原范围；真实学校多账号、Linux 部署与长期运行尚待分别验收。

## 风险提示

- 管理台只应部署在可信局域网或 VPN 内，不要把 8080 端口暴露到公网或不可信网络。
- 管理台要求管理员登录；所有管理员拥有相同权限。HTTP 不加密登录信息、会话和学校凭据，能访问服务器数据目录的人也能读取本地明文凭据。
- 管理台无 TLS：同网段的窃听与篡改无法被本程序发现。
- 以上取舍以“仅本人或少数人和机器使用”为前提；若以后需要跨网段访问或更强边界，可引入 TLS、按需细分权限并恢复文件权限约束，见 [ADR-0008](adr/0008-trusted-environment-http-no-login.md) 与 [ADR-0010](adr/0010-configured-admin-login.md)。

技术依据：[FastAPI 同步路由与线程池](https://fastapi.tiangolo.com/async/#path-operation-functions)、[Uvicorn HTTP 部署](https://uvicorn.dev/)。预约循环独立于这些路由执行。
