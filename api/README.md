# 电力表原始帧接收 API

这是 iotmonitor 的电力表原始帧接收服务。服务仅使用 Python 标准库，接收 ESP8266/ESPHome 节点上传的 DL/T 645-2007 原始帧，并在日志中原样记录；单片机不解析电表业务数据，服务端后续再解析。

## 启动

需要 Python 3.6 或更新版本（服务使用标准库，已兼容 104 节点的 Python 3.6）：

~~~bash
cd /root/vibe/iotmonitor/api
export POWER_MONITOR_TOKEN="$(openssl rand -hex 32)"
export POWER_MONITOR_LOG_ONLY=true
python3 app.py
~~~

默认监听 127.0.0.1:MONITOR_API_PORT。可使用以下环境变量修改：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| POWER_MONITOR_TOKEN | 无，必填 | 设备使用的 Bearer Token |
| POWER_MONITOR_HOST | 127.0.0.1 | 监听地址 |
| POWER_MONITOR_PORT | 8090 | 监听端口 |
| POWER_MONITOR_DATABASE | data/power-monitor.db | 旧解析接口兼容用 SQLite 路径；生产趋势数据应同步到 Monitor Center |
| POWER_MONITOR_RAW_ARCHIVE | 与数据库同目录的 dlt645-frames.jsonl | 原始帧本地 JSONL 归档路径 |
| POWER_MONITOR_CENTER_DATABASE_URL | 无 | Monitor Center PostgreSQL 连接串；设置后使用 `iot_dlt645_frames` 独立表 |
| POWER_MONITOR_CENTER_API_URL | 无 | 页面/API 查询代理地址，例如 `http://MONITOR_CENTER_API_HOST:MONITOR_CENTER_API_PORT/api/dlt645/frames` |
| POWER_MONITOR_CENTER_API_KEY | 无 | 查询代理调用中心端点的共享密钥 |
| POWER_MONITOR_RAW_FORWARD_URL | 无 | 可选的上游原始帧 POST 地址，用于跨服务器同步 |
| POWER_MONITOR_LOG_ONLY | false | true 时只启用原始帧日志入口，不创建或写入 SQLite |

## 部署边界

外部有标准入口和旧设备兼容入口：

- 域名入口： https://iot.ohmyskills.top/api/v1/dlt645/frame，由域名证书保护，ESP8266 正式运行应校验证书。
- Arduino 1.6.8 / ESP8266 Core 2.3.0 兼容入口： https://LEGACY_API_HOST:LEGACY_API_PORT/api/v1/dlt645/frame，使用自签证书、TLS 1.0 和 AES128-SHA，仅供旧版 axTLS 客户端。
- 两台服务器运行同一个 app.py 和同一个 API 路径，分别把原始帧归档到本地 JSONL，并同步到各自配置的 SQLite 数据库；systemd 日志仍保留接收审计事件。
- Python 服务只绑定服务器本机 127.0.0.1:MONITOR_API_PORT，外部请求由 Nginx 443 或 旧版兼容入口转发。

旧版兼容入口不验证服务器身份，存在中间人窃取 Token 的风险。它只解决旧版 axTLS 的连接兼容问题，现代客户端仍必须使用域名 443 和受信任证书。

## 原始帧上报接口

POST /api/v1/dlt645/frame

请求头：

~~~text
Authorization: Bearer <POWER_MONITOR_TOKEN>
Content-Type: application/json
~~~

请求示例。服务端不解码 frame_hex：

~~~json
{
  "site_id": "home-pv",
  "device_id": "esp8266-12f-001",
  "measurement_point_id": "inverter-ac-output",
  "protocol": "DL/T 645-2007",
  "frames": [
    {
      "sequence": 1042,
      "captured_at": "2026-08-16T04:01:05+08:00",
      "direction": "rx",
      "frame_hex": "68010203040568910433333333333316"
    }
  ]
}
~~~

site_id、device_id、measurement_point_id、protocol 和 frames 是必填字段。每个帧只要求 sequence、captured_at、direction 和 frame_hex；direction 为 tx 或 rx，默认 rx。服务只验证十六进制传输格式，不验证 DL/T 645 地址、控制码、数据标识或校验和。单次最多上报 100 帧，请求体最大 256 KiB。

成功响应为 202 Accepted：

~~~json
{
  "request_id": "06310875-9c5c-450f-a4a9-3f339aa44379",
  "status": "logged",
  "logged_frames": 1,
  "received_at": "2026-08-16T08:20:30.123456Z"
}
~~~

生产链路为：192 接收并解析原始帧，先异步追加本地 JSONL，再由 `monitor-center-agent` 从 `power-monitor-api.service` 的 journald 读取结构化日志，经 WireGuard/NATS JetStream 流式送到 Monitor Center；中心端归档并同步写入 PostgreSQL 独立表 `iot_dlt645_frames`。104 只通过 `POWER_MONITOR_CENTER_API_URL` 查询中心数据并展示趋势，不直接写中心数据库。

192 上的 agent 环境必须设置 `MONITOR_CENTER_JOURNAL_UNIT=power-monitor-api.service`。中心端也兼容旧 agent 发送的普通 `raw` 日志，会从 `MESSAGE` 中兜底识别 `dlt645_raw_frame`。

监控页面使用 `GET /api/v1/dlt645/frame?limit=100&device_id=...&direction=rx` 读取最近 100 条记录。该查询接口最多返回最近 100 条，返回 `frames`、当前结果 `count` 和筛选前总数 `total`，不需要设备 Bearer Token。

调用示例：

~~~bash
curl --fail-with-body https://iot.ohmyskills.top/api/v1/dlt645/frame \
  -H "Authorization: Bearer $POWER_MONITOR_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @example-raw-frame.json
~~~

Arduino 1.6.8 兼容入口联调：

~~~bash
curl --fail-with-body --insecure --tlsv1.0 --tls-max 1.0 \
  --ciphers 'AES128-SHA:@SECLEVEL=0' \
  https://LEGACY_API_HOST:LEGACY_API_PORT/api/v1/dlt645/frame \
  -H "Authorization: Bearer $POWER_MONITOR_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @example-raw-frame.json
~~~

健康检查无需鉴权：GET /api/v1/health。

旧的 POST /api/v1/iotreport 解析测量接口仍保留给已有原型测试；在 POWER_MONITOR_LOG_ONLY=true 的部署中会明确返回 parsed_ingest_disabled，不会写数据库。

## 测试

~~~bash
cd /root/vibe/iotmonitor/api
python3 -m unittest discover -s tests -v
~~~
