# Web 操作与 Linux 部署

[README](../README.md) · [配置与排错](usage.md) · [业务规则](requirements.md)

前半部分讲日常账号操作，后半部分讲长期运行、升级和备份。管理台使用明文 HTTP，只放在可信局域网或 VPN 内；管理员登录不能替代传输加密。

## 配置与启动

先按 [README](../README.md#安装与启动)安装依赖、创建 `.env` 并设置 `SEU_WEB_ADMINS`。启动命令：

```bash
uv run --locked seu-reserve-web
# 指定其他配置文件时：
# uv run --locked seu-reserve-web --config /绝对路径/.env
```

本机打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)，服务器使用可信网络内可访问的实际 IP。默认 `0.0.0.0:8080` 是监听设置，不是浏览器访问地址；不要将管理台端口直接暴露到公网。

管理员账号与学校账号不同。所有管理员共用同一组学校账号；增删管理员或改管理员密码后要重启服务。登录会话固定十二小时，退出登录不停止预约。

## 账号操作

### 第一次使用

1. 登录后新增学校账号，填写显示名称、一卡通号和学校密码。
2. 选择讲座类别、是否允许线上／线下，以及允许的线下校区。**没有选择校区就不会预约线下场次。**
3. 按需填收信邮箱；留空不发送邮件。发信还需要管理员配置公共 SMTP，见[邮件配置](usage.md#邮件配置)。
4. 保存，再点击“启动预约”。保存新账号不会自动启动。
5. 出现认证提示时，点击“发送／重发短信”，填写六位验证码并点击“验证并继续”。其他账号继续运行。

### 日常操作

| 操作或显示 | 你需要知道的事 |
| --- | --- |
| 关闭网页／退出登录 | 预约仍会继续；要停止请点“停止” |
| 修改运行中的账号 | 保存时会安全停止并重启该账号，保留任务和待确认。学校账号身份不能修改；编辑时密码留空表示保留原密码 |
| 配置保存冲突 | 其他管理员已先保存；点击重新加载配置，再编辑 |
| 收信邮箱 | 填写后开启重要告警和每次成功通知；公共发信配置无效会阻止该账号启动 |
| 当前预约／参考进度 | 显示最近一次同步的学校结果，不是实时刷新；学校侧手动变更后，可停止再启动该账号来同步 |
| 待核对与时间保护 | 本地保留的待确认或占位，不能当作学校预约成功；处理方法见[入口与恢复](usage.md#入口与恢复) |
| 删除账号 | 先停止任务再删除本地数据；有待确认或占位时会拒绝删除。**不会取消学校已有预约** |

管理服务重启后，会恢复原本希望继续运行的账号。账号进程意外退出时最多连续重启三次，每次间隔五秒；稳定运行十分钟后重置次数。明确凭据错误、状态损坏或 OCR 不可用时，停止自动重启，等你处理。

网页每两秒读取本地状态，不因此额外请求学校。项目以十账号作为验收目标，没有硬性账号数上限；这不等于已经证明多账号长期稳定或开抢成功率。

## CLI 与锁

网页账号可以使用 CLI，示例见[入口与恢复](usage.md#入口与恢复)。CLI 不需要管理员登录，认证和 Ctrl+C 停止都在原终端处理。

同一数据根只允许一个管理服务，同一账号状态只允许一个写入进程。外部 CLI 正占用账号时，网页的启动、保存、停止和删除会返回 `state_in_use`；应先回到原终端停止，勿删锁文件。不同机器或数据目录之间无法协调，请避免同号重复运行。

## systemd 常驻

适用于 Linux。先手动启动，确认网页能登录、学校账号能运行，再按 Ctrl+C 停止前台服务。

部署前在运行管理服务的机器上确认系统时钟已通过 NTP 等方式同步。即使在另一台设备打开管理台，预约排程和时间判断也以运行服务的机器为准。具体时序见[预约查询策略](thread-query-strategy.md#验证码何时准备能用多久)。

下面的命令在**项目根目录**执行。使用已有普通用户，该用户应能运行 uv 并写入数据目录；示例要求项目路径和 uv 路径不含空格，有空格时需按 systemd 规则引用。

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

`KillMode=mixed` 会先让管理服务通知各账号退出，超时后再由 systemd 清理整个进程组。不要同时运行旧单账号服务和这套服务。

常用维护命令：

```bash
sudo systemctl status seu-reserve-web.service
sudo journalctl -u seu-reserve-web.service -n 100
sudo systemctl stop seu-reserve-web.service
sudo systemctl start seu-reserve-web.service
```

启动失败时先查管理员配置、端口和数据目录；分享日志前检查是否含个人信息。

## 本地数据与恢复

管理台数据通常在 `state/`，也可通过 `SEU_DATA_DIR` 更改：

| 内容 | 用途 |
| --- | --- |
| `.env` | 管理员、公共发信和路径配置，位于配置文件所在位置 |
| `<数据根>/accounts.sqlite3` | 学校账号、明文学校密码、筛选和运行意图 |
| `<数据根>/accounts/<id>/` | 各账号的任务、待确认、占位、Cookie 和日志 |
| `<数据根>/manager.lock` | 防止重复启动管理服务 |

**备份应包含 `.env` 和完整数据目录。** 只复制 `task.json` 不足以恢复全部账号配置，也不要复制后另开同号任务。程序不额外强制收紧文件权限，能读取这些文件的人可能取得学校凭据；不要提交到仓库或公开打包。

### 更新已有部署

1. 停止管理服务。
2. 备份 `.env` 和完整数据目录。
3. 更新代码，对照 [.env.example](../.env.example)调整配置，并检查[旧配置兼容限制](usage.md#配置)。
4. 安装锁定依赖，再启动服务。
5. 登录管理台，确认账号运行状态和待确认记录；新查询策略只有重启后才生效。

对应命令如下，备份和更新代码需在停服期间完成：

```bash
sudo systemctl stop seu-reserve-web.service
# 此时备份 .env 和完整数据目录，再更新代码与配置
uv sync --locked
sudo systemctl start seu-reserve-web.service
```

恢复时也先停服，再恢复完整备份并核对配置。若提示状态损坏或账号不符，保留原文件排查，不能通过删除任务来绕过。停止服务、恢复备份和删除本地账号，都不会替你取消学校预约。
