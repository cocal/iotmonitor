# 电力表原始帧接收 API

这是 iotmonitor 的电力表原始帧接收服务。服务接收 DL/T 645-2007 和 IM1253B `MODBUS-RTU` 原始帧，并在日志中原样记录；单片机上传原始报文，服务端按已知协议解析可识别的数据项。

## 启动

需要 Python 3.6 或更新版本（服务使用标准库，已兼容 104 节点的 Python 3.6）：

~~~bash
cd /root/vibe/iotmonitor/api
export POWER_MONITOR_TOKEN="$(openssl rand -hex 32)"
export POWER_MONITOR_LOG_ONLY=true
python3 app.py
~~~

默认监听 127.0.0.1:8090。可使用以下环境变量修改：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| POWER_MONITOR_TOKEN | 无，必填 | 设备使用的 Bearer Token |
| POWER_MONITOR_HOST | 127.0.0.1 | 监听地址 |
| POWER_MONITOR_PORT | 8090 | 监听端口 |
| POWER_MONITOR_DATABASE | data/power-monitor.db | 旧解析接口兼容用 SQLite 路径；生产趋势数据应同步到 Monitor Center |
| POWER_MONITOR_RAW_ARCHIVE | 与数据库同目录的 dlt645-frames.jsonl | 原始帧本地持久化 JSONL 路径；收到报文后先同步刷盘再写数据库 |
| POWER_MONITOR_DATABASE_URL | 无 | 192 本机 PostgreSQL 连接串；设置后接收服务写入本机 `iot_dlt645_frames` |
| POWER_MONITOR_CENTER_API_URL | 无 | 页面/API 查询代理地址，例如 `http://MONITOR_CENTER_API_HOST:MONITOR_CENTER_API_PORT/api/dlt645/frames` |
| POWER_MONITOR_CENTER_API_KEY | 无 | 查询代理调用中心端点的共享密钥 |
| POWER_MONITOR_RAW_FORWARD_URL | 无 | 可选的上游原始帧 POST 地址，用于跨服务器同步 |
| POWER_MONITOR_LOG_ONLY | false | true 时只启用原始帧日志入口，不创建或写入 SQLite |

## 部署边界

外部有标准入口和旧设备兼容入口：

- 域名入口： https://iot.ohmyskills.top/api/v1/dlt645/frame，由域名证书保护，ESP8266 正式运行应校验证书。
- Arduino 1.6.8 / ESP8266 Core 2.3.0 兼容入口：`https://LEGACY_API_HOST:LEGACY_API_PORT/api/v1/dlt645/frame`，使用自签证书、TLS 1.0 和 AES128-SHA，仅供旧版 axTLS 客户端。真实地址和端口只保存在部署配置中。
- 两台服务器运行同一个 app.py 和同一个 API 路径，分别把原始帧归档到本地 JSONL，并同步到各自配置的 SQLite 数据库；systemd 日志仍保留接收审计事件。
- Python 服务只绑定服务器本机内部端口，外部请求由 Nginx 标准入口或私有兼容入口转发。

旧版兼容入口不验证服务器身份，存在中间人窃取 Token 的风险。它只解决旧版 axTLS 的连接兼容问题，现代客户端仍必须使用域名标准 HTTPS 和受信任证书。

## 原始帧上报接口

`protocol` 支持 `DL/T 645-2007` 和 `MODBUS-RTU`。IM1253B 的 ESP8266 固件第一版
使用 `MODBUS-RTU`，上传每项的完整响应帧；服务端只解析功能码 `03`、4 字节数值且
CRC 正确的响应。协议假设、接线和程序见
[`docs/im1253b-esp8266-design.md`](../docs/im1253b-esp8266-design.md) 和
[`im1253b_esp8266.ino`](../firmware/arduino-1.6.8/im1253b_esp8266/im1253b_esp8266.ino)。

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

生产链路为：接收节点验证请求后，先把每个原始帧追加到本机 JSONL，执行 `flush` 和 `fsync`，再打印 journald 结构化日志并写入本机 PostgreSQL。数据库不可用时接口返回 503，但已经落盘的原始帧仍可恢复。源 PostgreSQL 通过原生逻辑复制同步业务表到 Monitor Center PostgreSQL；展示节点只查询中心数据，不直接写中心数据库。旧版 journald/NATS 链路仅用于兼容迁移，不再作为新的数据库同步方案。

### 数据库故障后的恢复

恢复脚本会重新解析 JSONL 并补写原始帧、指标、帧统计和每日用电量。脚本以
`request_id + sequence + direction` 生成稳定事件 ID；已存在的报文会跳过，因此可以
安全地重放整个文件，重复运行不会把统计值累加两次。

先只校验归档格式，不连接数据库：

~~~bash
cd /opt/iotmonitor/api
python3 replay_raw_archive.py --archive /var/lib/iotmonitor/dlt645-frames.jsonl --dry-run
~~~

传入当前 JSONL 路径时，脚本会自动发现同目录下按日期命名的轮转文件，按照从旧到新的
顺序读取 `.gz`、尚未压缩的最近轮转文件以及当前 JSONL。也可以把 `--archive` 直接指向
某一个 `.gz` 文件，只校验或恢复该文件。

数据库恢复后，加载与 API 服务相同的环境变量并执行重放：

~~~bash
set -a
. /etc/iotmonitor/api.env
set +a
python3 /opt/iotmonitor/api/replay_raw_archive.py
~~~

输出中的 `files` 是本次读取的归档文件数，`inserted` 是本次补写数量，`duplicates` 是数据库中已经存在而跳过的数量。
文件很大时可使用 `--start-line N` 从指定行开始，但完整重放最稳妥。JSONL 是恢复依据，
不能在尚未备份或确认数据库完整前删除。

### 原始报文日志轮转

生产环境使用 `deploy/iotmonitor-raw-archive.logrotate`：每天轮转一次，保留 90 天，历史
文件使用 gzip 压缩。最近一次轮转文件延迟到下一天压缩，确保已经打开旧文件的并发请求
完成写入；API 每次追加都会重新打开文件，因此采用重命名加创建新文件的方式，不使用
`copytruncate`，不会产生复制和截断之间的丢帧窗口。

实时查看接收到的结构化报文日志：

~~~bash
journalctl -u power-monitor-api.service -f -o cat
~~~

`dlt645_raw_frame` 日志在数据库操作之前输出；真正用于恢复的是经过 `fsync` 的 JSONL，
因为 journald 会受系统保留空间和轮转策略影响。

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
