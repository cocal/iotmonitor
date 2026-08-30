#!/usr/bin/env python3
"""DL/T 645-2007 电表模拟器。

通过 USB-RS485 适配器连接 ESP8266 的 RS485 总线。模拟器收到符合格式的
读请求后，返回一帧带正确校验和的固定响应，不连接真实电表，也不解析功率。
"""

import argparse
import sys
import time
from typing import Optional

import serial


DEFAULT_ADDRESS = bytes.fromhex("63 23 96 08 00 00")
DEFAULT_DATA = bytes.fromhex("00 00 03 02 00 05 00 10")


def checksum(frame_without_checksum: bytes) -> int:
    """计算 DL/T 645 从首字节到数据域末尾的 8 位累加校验和。"""
    return sum(frame_without_checksum) & 0xFF


def build_response(address: bytes, data: bytes) -> bytes:
    """生成一帧 DL/T 645 正常响应，数据域按协议加 0x33 传输。"""
    if len(address) != 6:
        raise ValueError("电表地址必须是 6 个字节")
    if not data or len(data) > 255:
        raise ValueError("响应数据长度必须在 1 到 255 字节之间")

    encoded_data = bytes((value + 0x33) & 0xFF for value in data)
    frame = bytearray((0x68,))
    frame.extend(address)
    frame.extend((0x68, 0x91, len(encoded_data)))
    frame.extend(encoded_data)
    frame.append(checksum(frame))
    frame.append(0x16)
    return bytes(frame)


def extract_frame(buffer: bytearray) -> Optional[bytes]:
    """从串口缓冲区取出一帧，以 0x68 地址 0x68 ... 0x16 为边界。"""
    while buffer:
        start = buffer.find(b"\x68")
        if start < 0:
            buffer.clear()
            return None
        if start:
            del buffer[:start]
        if len(buffer) < 12:
            return None
        if buffer[7] != 0x68:
            del buffer[0]
            continue
        data_length = buffer[9]
        frame_length = 12 + data_length
        if len(buffer) < frame_length:
            return None
        candidate = bytes(buffer[:frame_length])
        del buffer[:frame_length]
        if candidate[-1] != 0x16:
            continue
        if checksum(candidate[:-2]) != candidate[-2]:
            print("[模拟电表] 忽略校验和错误的请求:", candidate.hex(" ").upper(), flush=True)
            continue
        return candidate
    return None


def run(port: str, baud: int, address: bytes, data: bytes) -> None:
    """打开串口并持续响应 ESP8266 的查询请求。"""
    response = build_response(address, data)
    print(f"[模拟电表] 串口={port} 波特率={baud} 8E1", flush=True)
    print(f"[模拟电表] 地址={address.hex(' ').upper()}", flush=True)
    print(f"[模拟电表] 响应={response.hex(' ').upper()}", flush=True)

    try:
        with serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
            write_timeout=1,
        ) as connection:
            buffer = bytearray()
            while True:
                chunk = connection.read(64)
                if chunk:
                    buffer.extend(chunk)
                request = extract_frame(buffer)
                if request is None:
                    continue
                if request[1:7] != address:
                    print(
                        "[模拟电表] 地址不匹配，忽略:",
                        request[1:7].hex(" ").upper(),
                        flush=True,
                    )
                    continue
                if request[8] != 0x11:
                    print(f"[模拟电表] 控制码 0x{request[8]:02X} 暂不响应", flush=True)
                    continue
                print(
                    "[模拟电表] 收到请求:", request.hex(" ").upper(),
                    " -> 返回响应",
                    flush=True,
                )
                time.sleep(0.02)
                connection.write(response)
                connection.flush()
    except serial.SerialException as error:
        print(f"[模拟电表] 串口错误: {error}", file=sys.stderr, flush=True)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\n[模拟电表] 已停止", flush=True)


def parse_hex(value: str, expected_length: Optional[int] = None) -> bytes:
    """解析无空格或带空格的十六进制命令行参数。"""
    try:
        result = bytes.fromhex(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"不是有效十六进制: {value}") from error
    if expected_length is not None and len(result) != expected_length:
        raise argparse.ArgumentTypeError(
            f"需要 {expected_length} 个字节，实际为 {len(result)} 个"
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="DL/T 645-2007 USB-RS485 电表模拟器")
    parser.add_argument("--port", required=True, help="串口，例如 COM3 或 /dev/ttyUSB1")
    parser.add_argument("--baud", type=int, default=2400, help="波特率，默认 2400")
    parser.add_argument(
        "--address",
        type=lambda value: parse_hex(value, 6),
        default=DEFAULT_ADDRESS,
        help="线路字节顺序的 6 字节地址，默认 129078563412",
    )
    parser.add_argument(
        "--data",
        type=lambda value: parse_hex(value),
        default=DEFAULT_DATA,
        help="未加 0x33 的模拟数据域，默认 0000030200050010",
    )
    args = parser.parse_args()
    run(args.port, args.baud, args.address, args.data)


if __name__ == "__main__":
    main()
