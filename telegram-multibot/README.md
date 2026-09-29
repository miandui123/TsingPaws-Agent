# TsingPaws Telegram Multibot

在一台 TsingPaws 设备上运行一个 PicoClaw Gateway，同时接入多个独立的 Telegram Bot。

安装后仍然只使用一个管理入口：

- `http://设备IP:18800`：TsingPaws 管理页面
- `http://设备IP:18800/telegram-bots`：Telegram 机器人管理
- `18790`：原 PicoClaw Gateway
- `18792`：多 Bot 适配器，仅本机访问
- `18880`：原生 Launcher，仅本机访问并由防火墙保护

每个 Bot Token 对应一个独立 Telegram 机器人；每个 Bot/Chat 使用独立 Pico 会话，但共用同一个 Gateway。Token 只保存到设备上的 root 可读配置，不通过管理 API 回显。

## 一条命令安装

以 `root` 登录已安装 TsingPaws 的 OpenWrt 设备，然后执行：

```sh
curl -fsSL https://raw.githubusercontent.com/miandui123/TsingPaws-Telegram-Multibot/main/install.sh | sh
```

如果设备只有 `wget`：

```sh
wget -qO- https://raw.githubusercontent.com/miandui123/TsingPaws-Telegram-Multibot/main/install.sh | sh
```

引导脚本固定安装 `v0.1.2`，下载前校验清单 SHA-256，下载后逐个校验程序文件。安装失败会自动恢复安装前的文件和服务状态。

## 前提

- OpenWrt，且以 `root` 执行
- 已安装并能正常启动 TsingPaws/PicoClaw
- `/usr/bin/python3`
- `curl` 或 `wget`
- `sha256sum`
- 设备能够访问 `raw.githubusercontent.com`

当前版本基于 TsingPaws 私有 PicoClaw `1.26.4` 验证。安装器会检查关键目录、服务和 Launcher 启动方式；不兼容时会停止并回滚。

## 使用

安装成功后打开：

```text
http://设备IP:18800/telegram-bots
```

可以添加、编辑、启停、删除和测试多个 Telegram Bot。编辑机器人时 Token 留空不会覆盖已有 Token。

设备端验证：

```sh
/opt/tsingpaw/extensions/telegram-multibot/verify.sh
```

回滚：

```sh
/opt/tsingpaw/extensions/telegram-multibot/rollback.sh
```

回滚会恢复安装前的程序与服务状态，但会保留 `telegram-bots.json` 和备份目录，避免删除用户后来添加的机器人配置。

## 数据与安全

- Bot 配置：`/opt/tsingpaw/data/telegram-bots.json`，权限 `0600`
- 安装备份：`/opt/tsingpaw/backups/telegram-multibot-*`
- 适配器和原生 Launcher 不直接暴露到局域网
- 仓库不包含 Bot Token、SSH 密码、设备配置、日志或运行数据

## 开发测试

```sh
python3 -m unittest discover -s backend/tests -v
python3 -m unittest discover -s integration/tests -v
```

`D:\\TsingPaws` 中的 `small-host/telegram-multibot` 是源码源头；本仓库是面向安装的公开分发镜像。
