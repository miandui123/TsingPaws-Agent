# Android PicoClient → 公网 Relay 改造说明

当前部署环境无法访问 `D:\TsingPaws`，请在 Windows 工程中按下列改动应用。

## 1. local.properties（勿提交 Git）

```
RELAY_WS_URL=ws://193.112.152.197:8787/v1/app/connect?device_id=home-001
RELAY_TOKEN=<从公网 /etc/tsingpaws-relay/relay.env 读取，勿发到聊天>
```

## 2. 注入 BuildConfig

```kotlin
android {
  defaultConfig {
    val props = java.util.Properties().apply {
      val f = rootProject.file("local.properties")
      if (f.exists()) f.inputStream().use { load(it) }
    }
    buildConfigField("String", "RELAY_WS_URL", "\"${props.getProperty("RELAY_WS_URL", "")}\"")
    buildConfigField("String", "RELAY_TOKEN", "\"${props.getProperty("RELAY_TOKEN", "")}\"")
  }
  buildFeatures { buildConfig = true }
}
```

## 3. PicoClient

- URL 改为 `BuildConfig.RELAY_WS_URL`
- Header：`Authorization: Bearer ${BuildConfig.RELAY_TOKEN}`
- 保留消息 JSON 的 `session_id` 与 Pico 协议（如 `message.send`）
- 收到 `{"type":"relay.peer_offline"}` 时显示「设备离线」
- 不再直连 `192.168.100.180:18790`

## 4. .gitignore 保留 local.properties

## 5. 验证

App 连接后 health 应为 `apps:1,agents:1,devices:1`，并可收发消息；再用 4G/5G 测一次。
