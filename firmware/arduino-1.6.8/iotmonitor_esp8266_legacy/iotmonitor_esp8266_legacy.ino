/*
 * IoT Monitor - ESP8266-12F legacy client
 *
 * Target toolchain:
 *   Arduino IDE 1.6.8
 *   ESP8266 Arduino Core 2.3.0
 *   Board: Generic ESP8266 Module
 *
 * The device sends a DL/T 645-2007 read request, captures the complete RS485
 * response, and uploads the response bytes without decoding meter values.
 */

#include <ESP8266WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiUdp.h>

// ---------- Required device configuration ----------

const char WIFI_SSID[] = "REPLACE_WIFI_SSID";
const char WIFI_PASSWORD[] = "REPLACE_WIFI_PASSWORD";
const char DEVICE_TOKEN[] = "REPLACE_DEVICE_TOKEN";

const char SITE_ID[] = "home-pv";
const char DEVICE_ID[] = "esp8266-12f-001";
const char MEASUREMENT_POINT_ID[] = "inverter-ac-output";

/*
 * false: 通过 RS485 查询真实电表，并上传电表返回的原始报文。
 * true:  不访问电表，仅上传 SIMULATED_FRAME_HEX，用于测试服务器接口。
 */
const bool SEND_SIMULATED_FRAME = false;

/*
 * 仅用于测试的 DL/T 645-2007 模拟响应报文，数据标识为 0x02020100（A 相电流）。
 * 该报文使用当前电表地址，其中的数值不是真实电表读数。
 */
const char SIMULATED_FRAME_HEX[] =
    "68200828090000689107333435353333332B16";

const IPAddress API_SERVER_IP(192, 144, 142, 237);
const char API_HOST_HEADER[] = "192.144.142.237:8899";
const uint16_t API_PORT = 8899;
const char API_PATH[] = "/api/v1/dlt645/frame";

/*
 * 电表地址配置：填写电表铭牌上的 12 位数字地址对应的 6 个字节。
 * DL/T 645-2007 通讯时采用低字节在前的顺序。例如电表地址
 * 123456789012 在线路上通常表示为 12 90 78 56 34 12。
 * 当前表号为 0008962363。DL/T 645 地址固定为 12 位数字，因此左侧补零为
 * 000008962363，再按低字节在前填写为 63 23 96 08 00 00。
 * 如更换电表，必须按照新电表实际报文或通信手册修改。
 */
const uint8_t METER_ADDRESS[6] = {
  0x63, 0x23, 0x96, 0x08, 0x00, 0x00
};
/*
 * 真实电表模式开关：
 * false 表示尚未确认 METER_ADDRESS，程序启动后不会查询电表；
 * true 表示已经填写并确认 METER_ADDRESS，程序可以发起抄表请求。
 * 使用模拟报文时该开关不会生效。
 */
const bool METER_ADDRESS_IS_CONFIGURED = true;

/*
 * 本轮要读取的三个数据标识，均按 DL/T 645 线路顺序填写。
 * 发送请求时程序会自动给每个字节加 0x33，此处不要提前加 0x33。
 * 0x02010100：A 相电压，线路顺序 00 01 01 02。
 * 0x02020100：A 相电流，线路顺序 00 01 02 02。
 * 0x02030000：瞬时总有功功率，线路顺序 00 00 03 02。
 * 如果“功耗”指累计电能而不是瞬时有功功率，应按电表手册替换第三项。
 */
/* 每个查询项包含日志名称、服务端测量点标识和 4 字节数据标识。 */
struct MeterQuery {
  const char *name;
  const char *measurementPointId;
  const uint8_t dataIdentifier[4];
};

const MeterQuery METER_QUERIES[] = {
  {"A-phase voltage", "inverter-ac-output:voltage-a", {0x00, 0x01, 0x01, 0x02}},
  {"A-phase current", "inverter-ac-output:current-a", {0x00, 0x01, 0x02, 0x02}},
  {"instantaneous total active power", "inverter-ac-output:instantaneous-active-power", {0x00, 0x00, 0x03, 0x02}}
};
const uint8_t METER_QUERY_COUNT = sizeof(METER_QUERIES) / sizeof(METER_QUERIES[0]);

// ---------- Wiring and timing ----------

/*
 * ESP8266 UART0 -> isolated RS485 transceiver:
 *   GPIO1 / TX -> DI
 *   GPIO3 / RX <- RO
 *   GPIO5      -> DE and /RE tied together
 *
 * Disconnect the USB-TTL TX/RX wires after flashing because UART0 is used by
 * the meter. Optional diagnostics are transmitted from GPIO2 via Serial1.
 */
const uint8_t RS485_DIRECTION_PIN = 5;
const unsigned long METER_BAUD = 2400;
const unsigned long POLL_INTERVAL_MS = 5000UL;
const unsigned long INTER_QUERY_DELAY_MS = 1000UL;
const unsigned long RESPONSE_START_TIMEOUT_MS = 1200UL;
const unsigned long INTER_BYTE_TIMEOUT_MS = 40UL;
const unsigned long WIFI_CONNECT_TIMEOUT_MS = 30000UL;
const unsigned long NTP_SYNC_INTERVAL_MS = 6UL * 60UL * 60UL * 1000UL;

const size_t MAX_METER_FRAME_BYTES = 256;
const unsigned int NTP_LOCAL_PORT = 2390;
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
bool lastMeterResponseValid = false;

// ---------- Small helpers ----------

bool containsPlaceholder(const char *value) {
  return strstr(value, "REPLACE_") != NULL;
}

void logLine(const String &message) {
  // Serial1 只有 TX，ESP8266-12F 上固定从 GPIO2 输出日志。
  Serial1.println(message);
}

bool configurationIsReady() {
  if (containsPlaceholder(WIFI_SSID) ||
      containsPlaceholder(WIFI_PASSWORD) ||
      containsPlaceholder(DEVICE_TOKEN)) {
    logLine("CONFIG ERROR: replace Wi-Fi and token");
    return false;
  }
  if (!SEND_SIMULATED_FRAME && !METER_ADDRESS_IS_CONFIGURED) {
    logLine("CONFIG ERROR: configure the real meter address");
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

  while (WiFi.status() != WL_CONNECTED &&
         millis() - startedAt < WIFI_CONNECT_TIMEOUT_MS) {
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

  while (ntpUdp.parsePacket() > 0) {
    ntpUdp.read(ntpPacket, NTP_PACKET_SIZE);
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
      unsigned long secondsSince1900 =
          ((unsigned long)ntpPacket[40] << 24) |
          ((unsigned long)ntpPacket[41] << 16) |
          ((unsigned long)ntpPacket[42] << 8) |
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

bool isLeapYear(unsigned int year) {
  return (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
}

void unixToUtc(unsigned long epoch, unsigned int &year, uint8_t &month,
               uint8_t &day, uint8_t &hour, uint8_t &minute,
               uint8_t &second) {
  unsigned long days = epoch / 86400UL;
  unsigned long daySeconds = epoch % 86400UL;

  hour = daySeconds / 3600UL;
  minute = (daySeconds % 3600UL) / 60UL;
  second = daySeconds % 60UL;

  year = 1970;
  while (true) {
    unsigned int daysInYear = isLeapYear(year) ? 366 : 365;
    if (days < daysInYear) {
      break;
    }
    days -= daysInYear;
    year++;
  }

  static const uint8_t DAYS_IN_MONTH[] = {
    31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31
  };

  month = 1;
  for (uint8_t index = 0; index < 12; index++) {
    uint8_t daysInMonth = DAYS_IN_MONTH[index];
    if (index == 1 && isLeapYear(year)) {
      daysInMonth = 29;
    }
    if (days < daysInMonth) {
      month = index + 1;
      break;
    }
    days -= daysInMonth;
  }
  day = (uint8_t)days + 1;
}

bool makeUtcTimestamp(char *output, size_t outputSize) {
  if (baseUnixTime == 0) {
    return false;
  }

  unsigned long epoch = baseUnixTime + (millis() - baseUnixMillis) / 1000UL;
  unsigned int year;
  uint8_t month;
  uint8_t day;
  uint8_t hour;
  uint8_t minute;
  uint8_t second;
  unixToUtc(epoch, year, month, day, hour, minute, second);

  snprintf(output, outputSize, "%04u-%02u-%02uT%02u:%02u:%02uZ",
           year, month, day, hour, minute, second);
  return true;
}

// ---------- DL/T 645 transport ----------

size_t buildReadRequest(const uint8_t *dataIdentifier,
                        uint8_t *output, size_t outputSize) {
  const size_t requestLength = 16;
  if (outputSize < requestLength) {
    return 0;
  }

  output[0] = 0x68;
  for (uint8_t index = 0; index < 6; index++) {
    output[1 + index] = METER_ADDRESS[index];
  }
  output[7] = 0x68;
  output[8] = 0x11;
  output[9] = 0x04;
  for (uint8_t index = 0; index < 4; index++) {
    output[10 + index] = dataIdentifier[index] + 0x33;
  }

  uint8_t checksum = 0;
  for (uint8_t index = 0; index < 14; index++) {
    checksum += output[index];
  }
  output[14] = checksum;
  output[15] = 0x16;
  return requestLength;
}

/*
 * 等待电表返回一帧数据，并把校验通过的原始字节复制到 output。
 * 电表可能先发送 0xFE 前导字节，因此不能把收到的第一个字节直接当成完整响应。
 * 本方法会寻找 0x68 ... 0x16 帧边界，并验证长度和 CS；只收到 FE、半帧或乱码时返回 0。
 */
size_t readMeterResponse(uint8_t *output, size_t outputSize) {
  lastMeterResponseValid = false;
  unsigned long waitStartedAt = millis();
  while (Serial.available() == 0 &&
         millis() - waitStartedAt < RESPONSE_START_TIMEOUT_MS) {
    delay(1);
    yield();
  }

  if (Serial.available() == 0) {
    return 0;
  }

  uint8_t raw[MAX_METER_FRAME_BYTES];
  size_t rawLength = 0;
  unsigned long lastByteAt = millis();
  while (millis() - lastByteAt < INTER_BYTE_TIMEOUT_MS) {
    while (Serial.available() > 0) {
      int value = Serial.read();
      if (value >= 0 && rawLength < sizeof(raw)) {
        raw[rawLength++] = (uint8_t)value;
      }
      lastByteAt = millis();
    }
    delay(1);
    yield();
  }
  for (size_t start = 0; start + 12 <= rawLength; start++) {
    if (raw[start] != 0x68 || raw[start + 7] != 0x68) {
      continue;
    }

    size_t frameLength = 12 + raw[start + 9];
    if (start + frameLength > rawLength || frameLength > outputSize ||
        raw[start + frameLength - 1] != 0x16) {
      continue;
    }

    uint8_t checksum = 0;
    for (size_t index = start; index < start + frameLength - 2; index++) {
      checksum += raw[index];
    }
    if (checksum != raw[start + frameLength - 2]) {
      continue;
    }

    memcpy(output, &raw[start], frameLength);
    lastMeterResponseValid = true;
    return frameLength;
  }

  if (rawLength > 0) {
    logLine(String("RS485 invalid response bytes: ") + rawLength);
    size_t copyLength = rawLength < outputSize ? rawLength : outputSize;
    memcpy(output, raw, copyLength);
    return copyLength;
  }
  return 0;
}

/* 在 pollMeter() 前声明，供查询日志把二进制帧转换为十六进制文本。 */
void bytesToHex(const uint8_t *input, size_t inputLength, char *output);

/*
 * 完成一次 RS485 抄表：生成 DL/T 645 查询指令、清除串口残留数据、
 * 切换到发送模式并发出指令，再切回接收模式并取得电表原始回复。
 * 这里不解析功率值。
 */
size_t pollMeter(const uint8_t *dataIdentifier,
                 uint8_t *response, size_t responseSize) {
  uint8_t request[16];
  size_t requestLength = buildReadRequest(dataIdentifier, request, sizeof(request));
  if (requestLength == 0) {
    return 0;
  }

  char requestHex[33];
  char requestChecksumHex[3];
  bytesToHex(request, requestLength, requestHex);
  bytesToHex(&request[requestLength - 2], 1, requestChecksumHex);
  logLine(String("RS485 request: ") + requestHex +
          " CS=0x" + requestChecksumHex);

  while (Serial.available() > 0) {
    Serial.read();
  }

  digitalWrite(RS485_DIRECTION_PIN, HIGH);
  delayMicroseconds(200);
  Serial.write(request, requestLength);
  Serial.flush();
  delayMicroseconds(500);
  digitalWrite(RS485_DIRECTION_PIN, LOW);

  return readMeterResponse(response, responseSize);
}

/*
 * 把二进制字节转换成大写十六进制文本，以便放入 JSON 的 frame_hex 字段。
 * 每个输入字节需要两个输出字符，末尾还需要一个 '\0' 字符。
 * 示例：{ 0x68, 0x11 } 转换为 "6811"。
 */
void bytesToHex(const uint8_t *input, size_t inputLength, char *output) {
  static const char HEX_DIGITS[] = "0123456789ABCDEF";
  for (size_t index = 0; index < inputLength; index++) {
    output[index * 2] = HEX_DIGITS[input[index] >> 4];
    output[index * 2 + 1] = HEX_DIGITS[input[index] & 0x0F];
  }
  output[inputLength * 2] = '\0';
}

// ---------- HTTPS API 上传 ----------

/*
 * 读取 API 的 HTTP 响应状态和响应正文。
 *
 * 参数：
 *   client       已完成 TLS 握手的 HTTPS 客户端。
 *   responseBody 用于接收响应正文的 String 缓冲区。
 * 返回：HTTP 状态码；连接或响应格式异常时返回 -1。
 *
 * 这里只解析 HTTP 状态和 Content-Length，不解析服务端 JSON 业务字段。
 */
int readHttpResponse(WiFiClientSecure &client, String &responseBody) {
  String statusLine = client.readStringUntil('\n');
  statusLine.trim();
  int firstSpace = statusLine.indexOf(' ');
  int status = -1;
  if (firstSpace >= 0 && statusLine.length() >= firstSpace + 4) {
    status = statusLine.substring(firstSpace + 1, firstSpace + 4).toInt();
  }

  int contentLength = 0;
  while (client.connected()) {
    String headerLine = client.readStringUntil('\n');
    if (headerLine == "\r" || headerLine.length() == 0) {
      break;
    }
    headerLine.trim();
    if (headerLine.startsWith("Content-Length:")) {
      contentLength = headerLine.substring(15).toInt();
    }
  }

  unsigned long startedAt = millis();
  while ((int)responseBody.length() < contentLength &&
         millis() - startedAt < 10000UL) {
    while (client.available() > 0) {
      responseBody += (char)client.read();
    }
    if (!client.connected() && client.available() == 0) {
      break;
    }
    delay(1);
    yield();
  }
  return status;
}

/*
 * 封装一次原始电表报文上报。
 *
 * 参数：
 *   frameHex   电表收到或发出的完整原始字节，已转换为十六进制文本。
 *   capturedAt 采集时间，格式为 UTC 的 ISO-8601 字符串。
 *   sequence   本设备递增的报文序号，用于服务端日志关联。
 * 返回：服务端返回 202 Accepted 时为 true，否则为 false。
 *
 * 本方法负责组装 JSON、建立 TLS 1.0 连接、添加 Bearer Token、发送 HTTP
 * 请求并读取状态码。ESP8266 Core 2.3.0 不调用 verify()，所以证书不校验。
 */
bool uploadRawFrame(const char *measurementPointId, const char *frameHex,
                    const char *capturedAt, unsigned long sequence) {
  String payload;
  payload.reserve(strlen(frameHex) + 320);
  payload += "{\"site_id\":\"";
  payload += SITE_ID;
  payload += "\",\"device_id\":\"";
  payload += DEVICE_ID;
  payload += "\",\"measurement_point_id\":\"";
  payload += measurementPointId;
  payload += "\",\"protocol\":\"DL/T 645-2007\",\"frames\":[{";
  payload += "\"sequence\":";
  payload += String(sequence);
  payload += ",\"captured_at\":\"";
  payload += capturedAt;
  payload += "\",\"direction\":\"rx\",\"frame_hex\":\"";
  payload += frameHex;
  payload += "\"}]}";

  /*
   * ESP8266 Core 2.3.0 has no setInsecure() method. WiFiClientSecure still
   * creates an encrypted TLS connection, but server identity is not checked
   * unless verify() is called. We intentionally do not call verify() here and
   * connect to the dedicated TLS 1.0 endpoint by IP, so certificate renewal
   * does not require reflashing this legacy device.
   */
  WiFiClientSecure client;
  client.setTimeout(10000);
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

  String responseBody;
  responseBody.reserve(320);
  int status = readHttpResponse(client, responseBody);
  client.stop();

  logLine(String("HTTP status: ") + status);
  return status == HTTP_ACCEPTED_STATUS;
}

/*
 * 直接向 API 上传一条固定的模拟报文。
 * 本方法不读取串口，也不与 RS485 电表通信，只用于验证 Wi-Fi、TLS、
 * Token 鉴权和服务器接口是否正常；实际请求仍由 uploadRawFrame() 统一发送。
 * 返回 true 表示服务器返回 202，false 表示网络或接口请求失败。
 */
bool sendSimulatedFrame(const char *capturedAt, unsigned long sequence) {
  logLine("Uploading simulated DL/T 645 frame");
  return uploadRawFrame(MEASUREMENT_POINT_ID, SIMULATED_FRAME_HEX,
                        capturedAt, sequence);
}

// ---------- Arduino lifecycle ----------

void setup() {
  // UART0 专用于隔离 RS485 电表通信，日志固定从 GPIO2/Serial1 输出。
  Serial.begin(METER_BAUD, SERIAL_8E1);

  // Optional TX-only diagnostics on GPIO2. Do not connect this to RS485.
  Serial1.begin(115200);
  Serial1.println();
  logLine("IoT Monitor legacy client starting");

  pinMode(RS485_DIRECTION_PIN, OUTPUT);
  digitalWrite(RS485_DIRECTION_PIN, LOW);

  if (!configurationIsReady()) {
    return;
  }

  if (connectWifi()) {
    ntpUdp.begin(NTP_LOCAL_PORT);
    lastNtpAttemptMillis = millis();
    syncClockFromNtp();
  }
}

void loop() {
  if (!configurationIsReady()) {
    delay(5000);
    return;
  }

  if (!connectWifi()) {
    delay(5000);
    return;
  }

  if (baseUnixTime == 0 ||
      millis() - lastNtpAttemptMillis >= NTP_SYNC_INTERVAL_MS) {
    lastNtpAttemptMillis = millis();
    if (!syncClockFromNtp() && baseUnixTime == 0) {
      delay(5000);
      return;
    }
  }

  if (millis() - lastPollMillis < POLL_INTERVAL_MS) {
    delay(20);
    return;
  }
  lastPollMillis = millis();

  char capturedAt[21];
  if (!makeUtcTimestamp(capturedAt, sizeof(capturedAt))) {
    logLine("Clock is not synchronized");
    return;
  }

  const unsigned long frameSequence = sequenceNumber;
  if (SEND_SIMULATED_FRAME) {
    if (sendSimulatedFrame(capturedAt, frameSequence)) {
      sequenceNumber++;
      logLine("Uploaded simulated frame");
    } else {
      logLine("Simulated frame upload failed; sequence was not advanced");
    }
    return;
  }

  for (uint8_t queryIndex = 0; queryIndex < METER_QUERY_COUNT; queryIndex++) {
    const MeterQuery &query = METER_QUERIES[queryIndex];
    logLine(String("Reading ") + query.name);

    uint8_t response[MAX_METER_FRAME_BYTES];
    size_t responseLength = pollMeter(query.dataIdentifier, response, sizeof(response));
    if (responseLength == 0) {
      logLine(String("Meter response missing or invalid: ") + query.name);
      continue;
    }

    char responseChecksumHex[3];
    if (responseLength >= 2) {
      bytesToHex(&response[responseLength - 2], 1, responseChecksumHex);
      logLine(String("RS485 response bytes: ") + responseLength +
              " CS=0x" + responseChecksumHex);
    } else {
      logLine(String("RS485 response bytes: ") + responseLength);
    }

    char frameHex[MAX_METER_FRAME_BYTES * 2 + 1];
    bytesToHex(response, responseLength, frameHex);

    String uploadMeasurementPointId = query.measurementPointId;
    if (!lastMeterResponseValid) {
      uploadMeasurementPointId += ":invalid-response";
    }

    if (uploadRawFrame(uploadMeasurementPointId.c_str(), frameHex, capturedAt,
                       sequenceNumber)) {
      logLine(String("Uploaded ") + query.name +
              (lastMeterResponseValid ? " valid" : " invalid") +
              " bytes: " + responseLength);
      sequenceNumber++;
    } else {
      logLine(String("Upload failed: ") + query.name);
    }

    if (queryIndex + 1 < METER_QUERY_COUNT) {
      delay(INTER_QUERY_DELAY_MS);
      yield();
    }
  }
}
