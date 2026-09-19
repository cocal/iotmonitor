/*
 * ESP8266-12F API-only simulator
 *
 * Target:
 *   Arduino IDE 1.6.8
 *   ESP8266 Arduino Core 2.3.0
 *   Board: Generic ESP8266 Module
 *
 * This sketch does not initialize RS485 and does not read a meter. After each
 * boot it connects to Wi-Fi, sends exactly one fixed DL/T 645 test frame to
 * the API, prints the result on UART0 / GPIO1, and then stays idle.
 */

#include <ESP8266WiFi.h>
#include <WiFiClientSecure.h>

// ---------- Required configuration ----------

const char WIFI_SSID[] = "REPLACE_WIFI_SSID";
const char WIFI_PASSWORD[] = "REPLACE_WIFI_PASSWORD";
const char DEVICE_TOKEN[] = "REPLACE_DEVICE_TOKEN";

// ---------- Fixed API test data ----------

// Replace these values in a private build. Do not commit the real endpoint.
const IPAddress API_SERVER_IP(0, 0, 0, 0);
const char API_HOST_HEADER[] = "REPLACE_API_HOST:REPLACE_API_PORT";
const uint16_t API_PORT = 0;
const char API_PATH[] = "/api/v1/dlt645/frame";

const char SITE_ID[] = "home-pv";
const char DEVICE_ID[] = "esp8266-12f-api-test";
const char MEASUREMENT_POINT_ID[] = "inverter-ac-output";

/*
 * A fixed, checksum-valid DL/T 645-2007 response frame used only for API
 * testing. It is not a real meter reading.
 */
const char SIMULATED_FRAME_HEX[] =
    "6812907856341268910833333635338334330D16";
const char SIMULATED_CAPTURED_AT[] = "2026-08-30T00:00:00Z";
const unsigned long SIMULATED_SEQUENCE = 1;

const unsigned long WIFI_CONNECT_TIMEOUT_MS = 30000UL;
const unsigned long HTTP_RESPONSE_TIMEOUT_MS = 10000UL;
const int HTTP_ACCEPTED_STATUS = 202;

bool containsPlaceholder(const char *value) {
  return strstr(value, "REPLACE_") != NULL;
}

bool configurationIsReady() {
  if (containsPlaceholder(WIFI_SSID) ||
      containsPlaceholder(WIFI_PASSWORD) ||
      containsPlaceholder(DEVICE_TOKEN) ||
      containsPlaceholder(API_HOST_HEADER) || API_PORT == 0) {
    Serial.println("[ERROR] Replace Wi-Fi, token and private API endpoint settings");
    return false;
  }
  return true;
}

bool connectWifi() {
  Serial.print("[WIFI] Connecting to ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long startedAt = millis();

  while (WiFi.status() != WL_CONNECTED &&
         millis() - startedAt < WIFI_CONNECT_TIMEOUT_MS) {
    Serial.print('.');
    delay(500);
    yield();
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[ERROR] Wi-Fi connection timed out");
    return false;
  }

  Serial.print("[WIFI] Connected, IP: ");
  Serial.println(WiFi.localIP());
  return true;
}

String buildPayload() {
  String payload;
  payload.reserve(420);
  payload += "{\"site_id\":\"";
  payload += SITE_ID;
  payload += "\",\"device_id\":\"";
  payload += DEVICE_ID;
  payload += "\",\"measurement_point_id\":\"";
  payload += MEASUREMENT_POINT_ID;
  payload += "\",\"protocol\":\"DL/T 645-2007\",\"frames\":[{";
  payload += "\"sequence\":";
  payload += String(SIMULATED_SEQUENCE);
  payload += ",\"captured_at\":\"";
  payload += SIMULATED_CAPTURED_AT;
  payload += "\",\"direction\":\"rx\",\"frame_hex\":\"";
  payload += SIMULATED_FRAME_HEX;
  payload += "\"}]}";
  return payload;
}

int readHttpStatus(WiFiClientSecure &client, String &responseBody) {
  String statusLine = client.readStringUntil('\n');
  statusLine.trim();
  Serial.print("[HTTP] ");
  Serial.println(statusLine);

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
         millis() - startedAt < HTTP_RESPONSE_TIMEOUT_MS) {
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

bool sendOneSimulatedFrame() {
  String payload = buildPayload();

  Serial.print("[HTTPS] Connecting to ");
  Serial.println(API_HOST_HEADER);

  /*
   * Core 2.3.0 has no setInsecure(). Not calling verify() is its equivalent:
   * TLS encrypts the connection, but server identity is not authenticated.
   * This connects directly to the dedicated TLS 1.0 compatibility endpoint.
   */
  WiFiClientSecure client;
  client.setTimeout(HTTP_RESPONSE_TIMEOUT_MS);
  if (!client.connect(API_SERVER_IP, API_PORT)) {
    Serial.println("[ERROR] HTTPS connection failed");
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
  int status = readHttpStatus(client, responseBody);
  client.stop();

  if (responseBody.length() > 0) {
    Serial.print("[HTTP] Response: ");
    Serial.println(responseBody);
  }

  if (status != HTTP_ACCEPTED_STATUS) {
    Serial.print("[ERROR] API returned status ");
    Serial.println(status);
    return false;
  }

  Serial.println("[OK] One simulated frame was accepted by the API");
  return true;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("=== ESP8266 API-only simulator ===");
  Serial.println("No RS485 or meter access will be performed.");

  if (!configurationIsReady()) {
    return;
  }
  if (!connectWifi()) {
    return;
  }

  sendOneSimulatedFrame();
  Serial.println("[DONE] Test finished. Reset the board to send once again.");
}

void loop() {
  delay(1000);
  yield();
}
