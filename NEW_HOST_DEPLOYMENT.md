# TsingPaws 新设备 Agent 部署与故障恢复手册

更新时间：2026-07-31

适用环境：TsingWin / OpenWrt、PicoClaw 1.19.0、TsingPaws Agent 2.3.x（当前 `2.3.0-binding-ui`）

本文用于全新 TsingPaws 设备。产品文案统一使用“TsingPaws”，不要再向用户显示“小主机”。

## 1. 目标架构

新 TsingPaws 设备部署完成后应同时运行三个服务：

| 启动顺序 | 服务 | 作用 | 本机端口 |
| --- | --- | --- | --- |
| 97 | `tsingpaw` | PicoClaw Launcher 和 Gateway | Launcher `18880`、Gateway `18790` |
| 98 | `tsingpaws-agent` | 连接公网 Relay，在 Relay 与 PicoClaw 之间转发消息 | 状态接口 `127.0.0.1:18791` |
| 99 | `tsingpaws-bridge` | 网页频道、状态接口和 WebSocket 安全代理 | `18800` |

公网 Relay 当前为 `193.112.152.197:8787`。公网发布前应升级为 HTTPS/WSS。

## 2. 本次故障的根因

旧脚本将启动序号写成：

```text
tsingpaws-agent  START=100
tsingpaws-bridge START=101
```

该 OpenWrt 启动体系只可靠处理两位启动序号。设备重启后，网页桥接可能已经运行，但 Agent 没有进入有效的 procd instance，最终表现为：

```text
服务未启动
手机连接服务未连接
本机智能助手不可用
```

正确顺序必须是：

```text
S97tsingpaw
S98tsingpaws-agent
S99tsingpaws-bridge
```

项目中的三个 init 文件已经按此规则修正。以后不要再使用 `START=100` 或 `START=101`。

## 3. 关键文件

### 项目内

```text
small-host/agent.py
small-host/file_transfer.py
small-host/launcher_bridge.py
small-host/run.sh
small-host/run-bridge.sh
small-host/tsingpaw.init
small-host/tsingpaws-agent.init
small-host/tsingpaws-bridge.init
small-host/static/
small-host/tools/check_pico_token_alignment.py
```

### TsingPaws 设备内

```text
/opt/tsingpaw/current/                     PicoClaw Launcher
/opt/tsingpaws-agent/agent.py
/opt/tsingpaws-agent/file_transfer.py
/opt/tsingpaws-agent/launcher_bridge.py
/opt/tsingpaws-agent/run.sh
/opt/tsingpaws-agent/run-bridge.sh
/opt/tsingpaws-agent/static/
/opt/tsingpaws-agent/tools/check_pico_token_alignment.py
/etc/init.d/tsingpaw
/etc/init.d/tsingpaws-agent
/etc/init.d/tsingpaws-bridge
/etc/tsingpaws-agent.env                   权限必须为 0600
/etc/tsingpaws-agent/device.json           设备身份，权限必须为 0600
/etc/tsingpaws-agent/mode
```

## 4. 新机器部署顺序

1. 给 TsingPaws 设置稳定的局域网地址，确认 Windows 电脑能 SSH 登录。
2. 安装并启动 TsingPaw/PicoClaw，先确认 `18790` Gateway 可用。
3. 将 `small-host` 中的 Agent、Bridge、静态资源和启动脚本部署到上述路径。不要只复制 `agent.py`；`launcher_bridge.py`、`run-bridge.sh` 和 `static/` 必须使用同一版本。
4. 安装 Python 3 和 `requirements.txt` 中的运行依赖。
5. 创建 `/etc/tsingpaws-agent.env`，只填当前环境需要的 Relay、Pico 和目录配置。
6. 完成一次设备注册或配对，生成 `/etc/tsingpaws-agent/device.json`。
7. 设置权限：

```sh
chmod 755 /opt/tsingpaws-agent/run.sh
chmod 755 /opt/tsingpaws-agent/run-bridge.sh
chmod 644 /opt/tsingpaws-agent/agent.py
chmod 644 /opt/tsingpaws-agent/file_transfer.py
chmod 644 /opt/tsingpaws-agent/launcher_bridge.py
chmod 755 /etc/init.d/tsingpaw
chmod 755 /etc/init.d/tsingpaws-agent
chmod 755 /etc/init.d/tsingpaws-bridge
chmod 600 /etc/tsingpaws-agent.env
chmod 600 /etc/tsingpaws-agent/device.json
```

8. 检查启动序号：

```sh
grep '^START=' /etc/init.d/tsingpaw
grep '^START=' /etc/init.d/tsingpaws-agent
grep '^START=' /etc/init.d/tsingpaws-bridge
```

结果必须依次为 `97`、`98`、`99`。

9. 启用并启动服务：

```sh
/etc/init.d/tsingpaw enable
/etc/init.d/tsingpaws-agent enable
/etc/init.d/tsingpaws-bridge enable
/etc/init.d/tsingpaw restart
/etc/init.d/tsingpaws-agent restart
/etc/init.d/tsingpaws-bridge restart
```

10. 检查开机链接：

```sh
ls -l /etc/rc.d/*tsing*
```

必须能看到：

```text
S97tsingpaw
S98tsingpaws-agent
S99tsingpaws-bridge
```

> TsingWin/OpenWrt 的 BusyBox 环境可能没有 `install` 命令。部署文件时使用 `cp` 后再执行 `chmod`，不要把 GNU/Linux 桌面系统中的命令直接照搬过去。

## 5. Pico Token 与网页 401

本次遇到的 `server rejected websocket: HTTP 401` 不是账号绑定问题，而是网页到 Gateway 的 WebSocket 最后一跳没有带上有效 Authorization。页面会一直显示：

```text
正在连接对话服务，当前内容会保留
```

安全配置原则：

- `config.json` 中的 `channels.pico.token` 保持占位值，不保存真实 Token。
- 真实 Token 保存在 PicoClaw 的 `.security.yml`。
- `/etc/tsingpaws-agent.env` 中的 `PICO_TOKEN` 只作为回退值，必须与 `.security.yml` 一致。
- `/etc/tsingpaws-agent.env` 必须设置 `PICO_SECURITY_FILE=/opt/tsingpaw/data/.security.yml`。
- `/etc/tsingpaws-agent.env` 必须设置 `LAUNCHER_API_BASE=http://127.0.0.1:18880`；如果 Launcher 管理口令与 Pico 通道 Token 不同，还必须单独设置 `LAUNCHER_TOKEN`。
- `run-bridge.sh` 必须显式导出 `PICO_SECURITY_FILE`；仅在 env 文件中赋值但不导出，Python Bridge 进程读取不到。
- `/etc/tsingpaw.conf` 不要再设置 `PICOCLAW_CHANNELS_PICO_TOKEN`，防止启动时覆盖正确令牌。
- 不要在日志、截图、Git 或部署记录中打印真实 Token。
- 不要随意重新生成 Token。当前定制版 PicoClaw 1.19.0 存在 Launcher 轮换后 Gateway 仍使用旧运行时令牌的风险。

网页 `/pico/ws` 应由 `launcher_bridge.py` 在验证 Launcher 登录会话后，直接代理到 `127.0.0.1:18790`，并在服务端注入正确的 Authorization。不要再依赖 Launcher 做第二次 WebSocket 转发。未登录网页请求必须继续返回 `401`，不能为了消除 401 而开放匿名访问。

Token 对齐检查：

```sh
python3 /opt/tsingpaws-agent/tools/check_pico_token_alignment.py
```

正确结果应包含：

```text
security_configured=true
effective_configured=true
tokens_match=true
config_is_placeholder=true
```

`config_is_placeholder=true` 是正确的安全状态，不是故障。诊断时必须比较 env 与 `.security.yml`，不能把 `config.json` 的占位值当成真实 Token。

修复或部署 Bridge 后检查：

```sh
logread | grep -E 'websocket proxy attempt|websocket upstream handshake' | tail -n 20
```

登录网页后的目标结果必须是：

```text
HTTP/1.1 101 Switching Protocols
```

如果仍是 `401`：

1. 先运行 Token 对齐工具。
2. 确认 `run-bridge.sh` 导出了 `PICO_SECURITY_FILE`。
3. 只重启 `/etc/init.d/tsingpaws-bridge` 后重试。
4. 若 PicoClaw 的安全配置刚发生变化，再重启 `/etc/init.d/tsingpaw`，等待 `18790/health` 恢复，然后重启 Bridge。
5. 不要删除 `device.json`，不要解绑账号，也不要重复注册设备。

## 6. 新版网页与绑定状态

新版 TsingPaws 页面由以下文件组成：

```text
/opt/tsingpaws-agent/static/cloud-channel.js
/opt/tsingpaws-agent/static/cloud-channel.css
```

部署页面时必须同时更新 JavaScript 和 CSS，并保持 Bridge 注入的版本号一致，避免浏览器继续使用旧缓存。

页面状态规则：

- 主题色固定为 `#5F1E98`。
- 未绑定 APP 账号时显示“等待绑定”，不能显示“服务正常”。
- 已绑定时显示脱敏账号，例如 `m***n`。
- 已绑定时，六位绑定码输入框和“确认绑定”按钮必须灰色禁用。
- 若仍向已绑定的 TsingPaws 提交绑定码，应提示“请使用 m***n 账号在原 APP 中解除绑定”。
- 页面文案统一使用“TsingPaws”，不使用“小主机”。

这部分依赖：

```text
Relay: GET /v1/devices/self/binding
Agent status: binding_known、bound、account_hint
```

如果页面能打开但绑定状态永远未知，优先检查上述 Relay 接口和 Agent `/status`，不要先修改前端显示逻辑。

## 7. 设备身份保护

以下文件代表这台 TsingPaws 在 Relay 上的身份：

```text
/etc/tsingpaws-agent/device.json
```

注意：

- 已经注册成功后，不要删除、覆盖或随意重新生成。
- 不要因为连接失败就重复注册或解绑。
- 换全新 TsingPaws 时使用正常配对流程生成新身份。
- 迁移或恢复旧主机身份前，必须先确认是否会造成两台设备使用同一个 `device_id`。
- `device.json`、Token、Cookie 和账号密码都不能提交到 Git。

## 8. 每次部署后的验收

### 8.1 服务状态

```sh
for s in tsingpaw tsingpaws-agent tsingpaws-bridge; do
    echo "===== $s ====="
    ubus call service list "{\"name\":\"$s\"}"
done
```

三个服务都必须出现：

```json
"running": true
```

`tsingpaws-agent` 不能只是：

```json
{"tsingpaws-agent": {}}
```

后者表示服务名存在，但没有有效 instance。

### 8.2 Agent 状态

```sh
curl -fsS http://127.0.0.1:18791/status
```

必须确认：

```text
status=ok
registered=true
relay_connected=true
pico_reachable=true
last_error=null
credential_invalid=false
autostart=true
```

### 8.3 进程与端口

```sh
pgrep -af 'picoclaw|agent.py|launcher_bridge.py'
netstat -lntp 2>/dev/null | grep -E '18790|18791|18800|18880'
```

### 8.4 自动拉起

只在维护窗口执行：

```sh
old_pid="$(pgrep -f '/opt/tsingpaws-agent/agent.py' | head -1)"
kill -9 "$old_pid"
sleep 7
pgrep -af '/opt/tsingpaws-agent/agent.py'
curl -fsS http://127.0.0.1:18791/status
```

必须出现新的 PID，并重新达到 `relay_connected=true`。

### 8.5 真实重启

最终必须执行一次：

```sh
sync
reboot
```

重启后重新检查第 8.1～8.3 节。仅仅手动 `start` 成功，不能证明开机自启已经修好。

## 9. 常见故障速查

| 表现 | 优先检查 | 处理 |
| --- | --- | --- |
| 网页显示“服务未启动” | `ubus service list`、`18791/status` | 检查 init 序号是否为 97/98/99，再重新 enable |
| `tsingpaws-agent` 显示空对象 | procd 没有有效 instance | 检查脚本、环境文件、执行权限和开机链接 |
| Agent 进程退出 | procd respawn | 确认 `respawn 3600 5 5` 存在，并做一次自动拉起测试 |
| 网页一直显示“正在连接对话服务” | `/pico/ws` 握手结果 | 要求返回 101；检查 Bridge 直连 Gateway、Authorization 和安全 Token |
| WebSocket HTTP 401 | Pico Token 或 Bridge 转发 | 对齐 `.security.yml` 与 Agent 环境，检查 `run-bridge.sh` 导出；不要重新注册设备 |
| Relay 已连、Pico 不可达 | `127.0.0.1:18790` | 检查 Pico Gateway 和 Authorization |
| 网页能打开但状态不更新 | Bridge 与 `18791` | 检查 `tsingpaws-bridge` 和 `AGENT_STATUS_BASE` |
| 手机/Windows 都离线 | Relay 连接和设备凭证 | 查看 `last_error`，不要先删除 `device.json` |

## 10. 参考设备 192.168.100.211 的最终验证

修复后已完成两项真实测试：

1. 强制结束 Agent，procd 在约 7 秒内生成新 PID，Agent 自动重新连接 Relay。
2. 重启整台 TsingPaws 后，三个服务按 97/98/99 自动启动。
3. 网页 WebSocket 经 Bridge 直连 Gateway 后返回 `101 Switching Protocols`，持续连接不再重复出现 401。

最终状态：

```text
tsingpaw=running
tsingpaws-agent=running
tsingpaws-bridge=running
relay_connected=true
pico_reachable=true
last_error=null
```

## 11. 下次部署时的最短检查清单

```text
[ ] 稳定局域网 IP
[ ] PicoClaw Gateway 的 18790 可用
[ ] Agent/Bridge 文件部署完整
[ ] cloud-channel.js 与 cloud-channel.css 同版本
[ ] env 与 device.json 权限为 0600
[ ] PICO_SECURITY_FILE 已配置并由 run-bridge.sh 导出
[ ] Pico Token 不进入 Git/日志，Token 对齐工具检查通过
[ ] START 顺序为 97/98/99
[ ] 三个 procd 服务均 running
[ ] 18791/status 全部正常
[ ] 网页 /pico/ws 握手为 101
[ ] 未绑定/已绑定页面状态和按钮正确
[ ] 自动拉起测试通过
[ ] 整机重启测试通过
[ ] 手机、Windows、网页各发一条消息验收
```
