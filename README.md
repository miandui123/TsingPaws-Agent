# TsingPaws OpenWrt Agent

当前默认以 `/etc/tsingpaws-agent/mode` 为准。内部测试版切换后为 `internal_test`。

Agent `2.3.0-binding-ui` 在保持 Relay↔PicoClaw 文本桥接的同时，拦截 APP `file.*` 做本地落盘与 Pico 通知，并把 Pico 附件/批准目录下的输出文件以 `file.*` 推回 APP；按 `session_id` 维护 typing / cancel，避免会话永久堵塞。

## Python 依赖

运行环境使用 Python 3.10+，安装锁定的运行依赖：

```bash
python3 -m pip install -r requirements.txt
```

开发测试额外安装：

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

## 关键文件

| 路径 | 说明 |
| --- | --- |
| `/opt/tsingpaws-agent/agent.py` | Agent（双模式 + 文件传输 / 会话任务） |
| `/opt/tsingpaws-agent/file_transfer.py` | 文件分片校验、临时目录、MIME、出站帧构造 |
| `/opt/tsingpaws-agent/launcher_bridge.py` | Launcher 反代 + 云频道 API（鉴权/CSRF/限体/环回固定） |
| `/opt/tsingpaws-agent/static/cloud-channel.js` | 频道 → TsingPaws 页面 |
| `/opt/tsingpaws-agent/tools/relay_probe_file_events.py` | Relay 对 `file.*` / `typing.*` 透明转发探针 |
| `/etc/tsingpaws-agent/mode` | `single_node` 或 `internal_test` |
| `/etc/tsingpaws-agent/device.json` | 内部测试版设备身份（0600） |
| `/etc/tsingpaws-agent/enrollment.env` | 一次性注册凭证（0600，用后删除） |
| `/etc/tsingpaws-agent.env` | 运行配置（0600） |

## 本地管理 API（仅 127.0.0.1:18791）

- `GET /health`
- `GET /status`
- `POST /reconnect`
- `POST /pairing/claim`  body: `{"pairing_code":"六位数字"}`

## Launcher Bridge 安全

- `/api/tsingpaws/*` 必须携带已登录 Launcher Cookie
- 写操作（reconnect / claim / restart）额外校验 Origin/Referer 同源
- JSON 请求体上限 2KB；反代请求体上限 2MB
- `LAUNCHER_UPSTREAM` / `AGENT_STATUS_BASE` 必须是环回地址，否则拒绝启动
- `single_node` 模式下绑定输入框禁用，claim API 返回 503

## 公网与 OpenWrt 联动回滚

**原则：先恢复公网单机版，再恢复 OpenWrt 单机版；两边都要验证 health/status。**

### A. 仅公网切换失败（OpenWrt 仍是 single_node）

1. 公网执行：`/opt/tsingpaws-relay/backups/single-node-20260727-200131/rollback.sh`
2. 验证：`curl http://127.0.0.1:8787/health` 无 `version`/`pairing_flow`（单机版）
3. OpenWrt 无需操作；确认 `mode=single_node` 且 `relay_connected=true`

### B. 公网已切内部测试，OpenWrt 注册失败（尚无 device.json）

1. 保存脱敏错误（不含 Token）
2. **不要反复注册**
3. 公网执行单机版 `rollback.sh`
4. OpenWrt 执行：`/opt/tsingpaws-agent/switch-to-single-node.sh`
5. 验证公网 agents≥1、OpenWrt `mode=single_node` + `relay_connected=true`

### C. OpenWrt 已注册成功（已有 device.json）

- **不要轻易回滚或删除 device.json**，避免丢失独立 DEVICE_TOKEN
- 先报告 `device_id`（可完整）与 status；需要回滚时再人工决策

### D. 完整灾难恢复（两边都回到切换前）

1. 公网：`.../single-node-20260727-200131/rollback.sh`
2. OpenWrt：`/opt/tsingpaws-backup/internal-agent-before-20260727-143732/rollback.sh`
3. 验证消息链路恢复后再处理后续

## 切换到内部测试版

1. 公网 `/health`：`version` 为 `internal-test-2` 或 `internal-test-auth-1`，且 `pairing_flow=app_first_invitation`
2. SSH 写入 `/etc/tsingpaws-agent/enrollment.env`（0600 root:root）
3. `/opt/tsingpaws-agent/switch-to-internal-test.sh`
4. 失败自动回滚 single_node；成功后 enrollment.env 被销毁

回滚模式：`/opt/tsingpaws-agent/switch-to-single-node.sh`

## Relay 文件事件探针

在不改公网的前提下，确认 Relay 能透明转发 `file.start` / `file.chunk` / `file.end` / `typing.*` / `response.*`（仅微型无害 payload，不打印 Token）：

```bash
DEVICE_TOKEN='…' RELAY_WS_URL='ws://HOST:8787/v1/agent/connect' \
  python3 /opt/tsingpaws-agent/tools/relay_probe_file_events.py
```

若某类消息被拒绝或连接被关闭，暂停正式文件联调并记录失败类型，不要擅自改公网 Relay。

## 文件传输要点

- APP→设备：`file.*` 由 Agent 拦截；落盘 `/tmp/tsingpaws-agent/transfers/<id>/`，校验后移入 `<PICO_WORKSPACE>/inbox/tsingpaws/`，再向 Pico `message.send`（图片可附 `data:image/...;base64,...`，PDF 等只走路径说明）
- 设备→APP：Pico `message.create` 的 `/pico/media/<ref>` 附件，以及内容中 `[image: path]` / `[file: path]`（仅 `inbox/**` 与 `skills/**/output/**`）经 `file.*` 推送
- 单文件 ≤20MiB，分片 ≤48KiB，并发传输 ≤3，临时总量 ≤100MiB；任务超时 20 分钟；`response.cancel` 释放该 session
