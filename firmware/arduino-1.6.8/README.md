# ESP8266-12F / Arduino IDE 1.6.8 草稿

这是供 ESP8266-12F 与 Arduino IDE 1.6.8 使用的兼容版本，线上 HTML 文档同步提供同一份程序。

## 固定环境

- Arduino IDE 1.6.8
- ESP8266 Arduino Core 2.3.0
- Board: Generic ESP8266 Module
- Flash Mode: DIO
- Flash Size: 4M (1M SPIFFS)
- CPU Frequency: 80 MHz
- Upload Speed: 115200

## 接线

| ESP8266-12F | 隔离 RS485 模块 |
| --- | --- |
| GPIO1 / TX | DI |
| GPIO3 / RX | RO |
| GPIO5 | DE 与 /RE 并联 |
| GND | 低压侧 GND |

GPIO2 是可选的 TX-only 调试日志口。烧录结束后应断开 USB-TTL 的 TX/RX，避免它和 RS485 共用 UART0。

## 烧录前必须修改

1. `WIFI_SSID` 和 `WIFI_PASSWORD`。
2. 服务器当前分配的 `DEVICE_TOKEN`。服务端完成每设备独立鉴权后，每台设备必须使用不同 Token。
3. `METER_ADDRESS`，按现场电表通信地址填写，并使用 DL/T 645 总线字节顺序；真实表号和通信地址不要写入公开文档或仓库。
4. 核对实际 DDSU666 的电流/功率数据标识；当前默认是 A 相电流 `0x02020100`，线路顺序为
   `00 01 02 02`。确认电表地址和数据标识后，将 `METER_ADDRESS_IS_CONFIGURED` 改为 `true`。

## 模拟报文模式

把 `SEND_SIMULATED_FRAME` 改为 `true` 后，程序不访问 RS485 电表，而是每 5 秒调用
`sendSimulatedFrame()`，向同一个 API 上传固定的 DL/T 645 测试帧。此模式只用于检查
Wi-Fi、HTTPS、Token 和服务端接口；测试帧不是真实电表读数。

使用模拟模式时不需要配置电表地址。完成接口测试后，应将
`SEND_SIMULATED_FRAME` 恢复为 `false`。

## 日志串口

完整程序固定使用 UART0/Serial 连接 RS485 电表，日志固定从 UART1/Serial1 的 TX 输出。
ESP8266-12F 上该日志引脚是 GPIO2；USB-TTL 的 RX 接 GPIO2、GND 接 GND，使用 115200/8N1，
TX 线不要连接。`SEND_SIMULATED_FRAME=true` 只会跳过 RS485 查询并发送模拟报文，日志串口
分工不变。

## 兼容性边界

ESP8266 Core 2.3.0 没有 `setInsecure()` 方法。本程序直接使用 `WiFiClientSecure`
连接私有配置中的 TLS 1.0 兼容入口，但不调用 `verify()`；效果等同于
新版本的 `setInsecure()`：传输仍加密，但设备不验证服务器身份，因此证书续期后
不需要重新烧录。该地址绕过域名和 Cloudflare，仅供这套旧固件使用。

该兼容方式存在中间人攻击风险，攻击者可能冒充服务器并取得设备 Token。服务端应为
每台设备分配独立、可吊销、仅允许写入该设备数据的 Token；发现异常时只轮换受影响设备。

程序只构造查询帧和识别串口静默边界，不解析响应中的地址、控制码、数据标识、数据域或功率值；收到的全部字节原样转成 `frame_hex` 上传。

## 现场验证记录

2026-08-30 现场联调确认：电压、电流、瞬时总有功功率三个查询之间必须保留
`1000 ms` 间隔。此前使用 `300 ms` 时，电表响应会出现截断、错位或只收到零散字节；
调整为 1 秒后读取稳定。代码中的 `INTER_QUERY_DELAY_MS = 1000UL` 是已验证参数，
不要在没有重新做连续读表验证的情况下调小。

每轮仍依次查询电压、电流和瞬时总有功功率；一轮完成后再按
`POLL_INTERVAL_MS` 进入下一轮。
