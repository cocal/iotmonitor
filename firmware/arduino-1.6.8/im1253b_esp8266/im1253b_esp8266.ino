/*
 * IoT Monitor - IM1253B Modbus-RTU client for ESP8266-12F
 *
 * Target: Arduino IDE 1.6.8, ESP8266 Arduino Core 2.3.0.
 * UART0 (GPIO1 TX / GPIO3 RX) is connected directly to the IM1253B TTL UART.
 * UART1 TX (GPIO2) is used for 115200-baud diagnostics.
 *
 * The IM1253B manual specifies 4800 8N1 by default and four-byte values for
 * the eight logical data items at 0x0048..0x004F. This sketch reads all eight
 * items with one Modbus request, validates CRC-16, and uploads one raw block.
 */

#include <ESP8266WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiUdp.h>

// ---------- Private deployment configuration ----------

const char WIFI_SSID[] = "REPLACE_WIFI_SSID";
const char WIFI_PASSWORD[] = "REPLACE_WIFI_PASSWORD";
const char DEVICE_TOKEN[] = "REPLACE_DEVICE_TOKEN";
const char SITE_ID[] = "home-pv";
const char DEVICE_ID[] = "esp8266-12f-im1253b-001";
const char MEASUREMENT_POINT_PREFIX[] = "inverter-ac-output";

// Replace these in a private build. Do not commit real endpoints or tokens.
const IPAddress API_SERVER_IP(0, 0, 0, 0);
const char API_HOST_HEADER[] = "REPLACE_API_HOST:REPLACE_API_PORT";
const uint16_t API_PORT = 0;
const char API_PATH[] = "/api/v1/dlt645/frame";

// IM1253B defaults from the V2.0 manual: address 1, 4800 baud, 8N1.
const uint8_t IM1253B_ADDRESS = 1;
const unsigned long METER_BAUD = 4800UL;

// 一次读取 0x0048～0x004F 共 8 个数据项，每项返回 4 字节。
const uint16_t IM1253B_BLOCK_START = 0x0048;
const uint16_t IM1253B_BLOCK_ITEM_COUNT = 0x0008;
const uint8_t IM1253B_BLOCK_DATA_BYTES = 32;
const size_t IM1253B_BLOCK_RESPONSE_BYTES = 37;

const unsigned long POLL_INTERVAL_MS = 5000UL;
const unsigned long RESPONSE_START_TIMEOUT_MS = 800UL;
const unsigned long INTER_BYTE_TIMEOUT_MS = 30UL;
const unsigned long WIFI_CONNECT_TIMEOUT_MS = 30000UL;
const unsigned long NTP_SYNC_INTERVAL_MS = 6UL * 60UL * 60UL * 1000UL;
const size_t MAX_FRAME_BYTES = 64;
const unsigned int NTP_LOCAL_PORT = 2391;
const unsigned int NTP_PACKET_SIZE = 48;
const unsigned long NTP_TO_UNIX_SECONDS = 2208988800UL;
const int HTTP_ACCEPTED_STATUS = 202;

WiFiUDP ntpUdp;
uint8_t ntpPacket[NTP_PACKET_SIZE];
unsigned long baseUnixTime = 0;
unsigned long baseUnixMillis = 0;
unsigned long lastNtpAttemptMillis = 0;
unsigned long lastPollMillis = 0;
unsigned long sequenceNumber = 1;

bool containsPlaceholder(const char *value) {
  return strstr(value, "REPLACE_") != NULL;
}

void logLine(const String &message) {
  Serial1.println(message);
}

void bytesToHex(const uint8_t *input, size_t length, char *output) {
  static const char digits[] = "0123456789ABCDEF";
  for (size_t index = 0; index < length; index++) {
    output[index * 2] = digits[input[index] >> 4];
    output[index * 2 + 1] = digits[input[index] & 0x0F];
  }
  output[length * 2] = '\0';
}

uint16_t modbusCrc16(const uint8_t *data, size_t length) {
  uint16_t crc = 0xFFFF;
  for (size_t index = 0; index < length; index++) {
    crc ^= data[index];
    for (uint8_t bit = 0; bit < 8; bit++) {
      crc = (crc & 1) ? (crc >> 1) ^ 0xA001 : crc >> 1;
    }
  }
  return crc;
}

// 生成 Modbus 03 功能码查询，并自动追加低字节在前的 CRC-16。
size_t buildReadRequest(uint16_t registerAddress, uint16_t itemCount,
                        uint8_t *output, size_t capacity) {
  if (capacity < 8) {
    return 0;
  }
  output[0] = IM1253B_ADDRESS;
  output[1] = 0x03;
  output[2] = registerAddress >> 8;
  output[3] = registerAddress & 0xFF;
  output[4] = itemCount >> 8;
  output[5] = itemCount & 0xFF;
  uint16_t crc = modbusCrc16(output, 6);
  output[6] = crc & 0xFF;
  output[7] = crc >> 8;
  return 8;
}

size_t readModbusResponse(uint8_t *output, size_t capacity) {
  unsigned long startedAt = millis();
  while (Serial.available() == 0 && millis() - startedAt < RESPONSE_START_TIMEOUT_MS) {
    delay(1);
    yield();
  }
  if (Serial.available() == 0) {
    return 0;
  }

  size_t length = 0;
  unsigned long lastByteAt = millis();
  while (millis() - lastByteAt < INTER_BYTE_TIMEOUT_MS) {
    while (Serial.available() > 0) {
      int value = Serial.read();
      if (value >= 0 && length < capacity) {
        output[length++] = (uint8_t)value;
      }
      lastByteAt = millis();
    }
    delay(1);
    yield();
  }
  return length;
}

// 校验一次性读取响应的地址、功能码、32 字节数据长度和 CRC。
bool validModbusResponse(const uint8_t *frame, size_t length) {
  if (length != IM1253B_BLOCK_RESPONSE_BYTES || frame[0] != IM1253B_ADDRESS ||
      frame[1] != 0x03 || frame[2] != IM1253B_BLOCK_DATA_BYTES) {
    return false;
  }
  uint16_t expected = modbusCrc16(frame, length - 2);
  uint16_t received = frame[length - 2] | ((uint16_t)frame[length - 1] << 8);
  return expected == received;
}

// 向 IM1253B 发送一次整块查询，返回收到的完整原始响应。
size_t pollMeter(uint8_t *response, size_t responseCapacity, bool &valid) {
  valid = false;
  uint8_t request[8];
  size_t requestLength = buildReadRequest(
      IM1253B_BLOCK_START, IM1253B_BLOCK_ITEM_COUNT, request, sizeof(request));
  char requestHex[17];
  bytesToHex(request, requestLength, requestHex);
  uint16_t requestCrc = request[6] | ((uint16_t)request[7] << 8);
  logLine(String("Modbus TX: ") + requestHex + " CRC=0x" + String(requestCrc, HEX));

  while (Serial.available() > 0) {
    Serial.read();
  }
  Serial.write(request, requestLength);
  Serial.flush();

  size_t length = readModbusResponse(response, responseCapacity);
  if (length > 0) {
    char responseHex[MAX_FRAME_BYTES * 2 + 1];
    bytesToHex(response, length, responseHex);
    logLine(String("Modbus RX bytes=") + length + " " + responseHex);
    valid = validModbusResponse(response, length);
    if (length >= 2) {
      uint16_t received = response[length - 2] | ((uint16_t)response[length - 1] << 8);
      uint16_t expected = modbusCrc16(response, length - 2);
      logLine(String("Modbus RX CRC expected=0x") + String(expected, HEX) +
              " received=0x" + String(received, HEX) + (valid ? " valid" : " invalid"));
    }
  } else {
    logLine("Modbus RX timeout");
  }
  return length;
}

bool configurationIsReady() {
  if (containsPlaceholder(WIFI_SSID) || containsPlaceholder(WIFI_PASSWORD) ||
      containsPlaceholder(DEVICE_TOKEN) || containsPlaceholder(API_HOST_HEADER) || API_PORT == 0) {
    logLine("CONFIG ERROR: replace Wi-Fi, token and private API endpoint");
    return false;
  }
  return true;
}

bool connectWifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long startedAt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startedAt < WIFI_CONNECT_TIMEOUT_MS) {
    delay(250);
    yield();
  }
  if (WiFi.status() != WL_CONNECTED) {
    logLine("Wi-Fi connection failed");
    return false;
  }
  logLine(String("Wi-Fi connected: ") + WiFi.localIP().toString());
  return true;
}

bool syncClockFromNtp() {
  IPAddress serverAddress;
  if (WiFi.hostByName("pool.ntp.org", serverAddress) != 1) {
    logLine("NTP DNS lookup failed");
    return false;
  }
  memset(ntpPacket, 0, NTP_PACKET_SIZE);
  ntpPacket[0] = 0xE3;
  ntpPacket[1] = 0;
  ntpPacket[2] = 6;
  ntpPacket[3] = 0xEC;
  ntpPacket[12] = 49;
  ntpPacket[13] = 0x4E;
  ntpPacket[14] = 49;
  ntpPacket[15] = 52;
  ntpUdp.beginPacket(serverAddress, 123);
  ntpUdp.write(ntpPacket, NTP_PACKET_SIZE);
  ntpUdp.endPacket();
  unsigned long startedAt = millis();
  while (millis() - startedAt < 1800UL) {
    int packetLength = ntpUdp.parsePacket();
    if (packetLength >= (int)NTP_PACKET_SIZE) {
      ntpUdp.read(ntpPacket, NTP_PACKET_SIZE);
      unsigned long secondsSince1900 = ((unsigned long)ntpPacket[40] << 24) |
          ((unsigned long)ntpPacket[41] << 16) | ((unsigned long)ntpPacket[42] << 8) |
          (unsigned long)ntpPacket[43];
      if (secondsSince1900 > NTP_TO_UNIX_SECONDS) {
        baseUnixTime = secondsSince1900 - NTP_TO_UNIX_SECONDS;
        baseUnixMillis = millis();
        logLine("NTP clock synchronized");
        return true;
      }
    }
    delay(20);
    yield();
  }
  logLine("NTP request timed out");
  return false;
}

bool makeUtcTimestamp(char *output, size_t capacity) {
  if (baseUnixTime == 0 || capacity < 21) {
    return false;
  }
  unsigned long epoch = baseUnixTime + (millis() - baseUnixMillis) / 1000UL;
  unsigned int year = 1970;
  unsigned long days = epoch / 86400UL;
  unsigned long seconds = epoch % 86400UL;
  while (days >= 365UL + (((year % 4 == 0) && (year % 100 != 0 || year % 400 == 0)) ? 1UL : 0UL)) {
    days -= 365UL + (((year % 4 == 0) && (year % 100 != 0 || year % 400 == 0)) ? 1UL : 0UL);
    year++;
  }
  static const uint8_t monthDays[] = {31,28,31,30,31,30,31,31,30,31,30,31};
  uint8_t month = 1;
  for (; month <= 12; month++) {
    uint8_t length = monthDays[month - 1];
    if (month == 2 && year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)) length++;
    if (days < length) break;
    days -= length;
  }
  snprintf(output, capacity, "%04u-%02u-%02luT%02lu:%02lu:%02luZ", year, month,
           days + 1, seconds / 3600UL, (seconds / 60UL) % 60UL, seconds % 60UL);
  return true;
}

int readHttpStatus(WiFiClientSecure &client) {
  String line = client.readStringUntil('\n');
  line.trim();
  int space = line.indexOf(' ');
  int status = space >= 0 ? line.substring(space + 1, space + 4).toInt() : -1;
  while (client.connected()) {
    String header = client.readStringUntil('\n');
    if (header == "\r" || header.length() == 0) break;
  }
  return status;
}

bool uploadRawFrame(const char *metric, const char *frameHex, const char *capturedAt,
                    unsigned long sequence) {
  String payload;
  payload.reserve(strlen(frameHex) + 360);
  payload += "{\"site_id\":\"";
  payload += SITE_ID;
  payload += "\",\"device_id\":\"";
  payload += DEVICE_ID;
  payload += "\",\"measurement_point_id\":\"";
  payload += MEASUREMENT_POINT_PREFIX;
  payload += ":";
  payload += metric;
  payload += "\",\"protocol\":\"MODBUS-RTU\",\"frames\":[{\"sequence\":";
  payload += String(sequence);
  payload += ",\"captured_at\":\"";
  payload += capturedAt;
  payload += "\",\"direction\":\"rx\",\"frame_hex\":\"";
  payload += frameHex;
  payload += "\"}]}";

  WiFiClientSecure client;
  client.setTimeout(10000);
  // Arduino Core 2.3.0 has no setInsecure(); no verify() means TLS is encrypted
  // but the legacy endpoint certificate is intentionally not authenticated.
  if (!client.connect(API_SERVER_IP, API_PORT)) {
    logLine("HTTPS connection failed");
    return false;
  }
  client.print("POST ");
  client.print(API_PATH);
  client.println(" HTTP/1.1");
  client.print("Host: ");
  client.println(API_HOST_HEADER);
  client.print("Authorization: Bearer ");
  client.println(DEVICE_TOKEN);
  client.println("Content-Type: application/json");
  client.println("Connection: close");
  client.print("Content-Length: ");
  client.println(payload.length());
  client.println();
  client.print(payload);
  int status = readHttpStatus(client);
  client.stop();
  logLine(String("HTTP status: ") + status);
  return status == HTTP_ACCEPTED_STATUS;
}

void setup() {
  Serial.begin(METER_BAUD, SERIAL_8N1);
  Serial1.begin(115200);
  logLine("IM1253B Modbus client starting");
  if (!configurationIsReady()) return;
  if (connectWifi()) {
    ntpUdp.begin(NTP_LOCAL_PORT);
    lastNtpAttemptMillis = millis();
    syncClockFromNtp();
  }
}

void loop() {
  if (!configurationIsReady() || !connectWifi()) {
    delay(5000);
    return;
  }
  if (baseUnixTime == 0 || millis() - lastNtpAttemptMillis >= NTP_SYNC_INTERVAL_MS) {
    lastNtpAttemptMillis = millis();
    if (!syncClockFromNtp() && baseUnixTime == 0) return;
  }
  if (millis() - lastPollMillis < POLL_INTERVAL_MS) {
    delay(20);
    return;
  }
  lastPollMillis = millis();
  char capturedAt[21];
  if (!makeUtcTimestamp(capturedAt, sizeof(capturedAt))) return;

  uint8_t response[MAX_FRAME_BYTES];
  bool valid;
  logLine("Reading IM1253B block 0x0048-0x004F");
  size_t length = pollMeter(response, sizeof(response), valid);
  if (length == 0) {
    // 超时没有原始字节可上传，日志仍会记录本轮失败。
    logLine("IM1253B block response missing");
  } else {
    char frameHex[MAX_FRAME_BYTES * 2 + 1];
    bytesToHex(response, length, frameHex);
    const char *measurement = valid ? "im1253b-block" : "im1253b-block:invalid-response";
    if (uploadRawFrame(measurement, frameHex, capturedAt, sequenceNumber)) {
      sequenceNumber++;
      logLine(valid ? "Uploaded IM1253B block valid" : "Uploaded IM1253B block invalid");
    } else {
      logLine("Upload failed: IM1253B block");
    }
  }
}
