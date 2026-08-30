# 电表模拟器

`dlt645_meter_simulator.py` 是一个运行在电脑上的 DL/T 645-2007 电表模拟器。
它通过 USB-RS485 适配器接入 ESP8266 所连接的同一条 RS485 总线，收到查询请求后返回
一帧固定的、校验和正确的模拟响应。

## 接线

需要第二个 USB-RS485 适配器：

| ESP8266 一侧 | 电脑模拟电表一侧 |
| --- | --- |
| RS485 A | A / D+ |
| RS485 B | B / D- |
| GND | GND（适配器支持时连接） |

不要把 USB-TTL 的 TX/RX 接到 ESP8266 的 GPIO1/GPIO3；这两个引脚已经连接 RS485。

## 安装和运行

```bash
python3 -m pip install pyserial
python3 dlt645_meter_simulator.py --port /dev/ttyUSB1
```

Windows 示例：

```text
python dlt645_meter_simulator.py --port COM3
```

默认地址与当前完整采集程序一致。若已修改 `METER_ADDRESS`，模拟器也要使用相同的线路
字节顺序，例如：

```bash
python3 dlt645_meter_simulator.py --port /dev/ttyUSB1 \
  --address 129078563412
```

看到“收到请求 -> 返回响应”后，ESP8266 应记录收到的原始帧并向 API 返回 `HTTP status: 202`。
该模拟器只验证 RS485 抄表链路，不代表真实 DDSU666 的功率读数。
