/*
 * ESP8266-12F minimal boot and UART diagnostic.
 *
 * Arduino IDE 1.6.8
 * ESP8266 Arduino Core 2.3.0
 * Board: Generic ESP8266 Module
 *
 * UART0 TX: GPIO1 at 115200 baud
 * UART1 TX: GPIO2 at 115200 baud (TX only)
 */

unsigned long messageNumber = 1;

void setup() {
  Serial.begin(115200);
  Serial1.begin(115200);
  delay(200);

  Serial.println();
  Serial.println("[DIAG] setup reached; UART0 GPIO1 works");
  Serial1.println();
  Serial1.println("[DIAG] setup reached; UART1 GPIO2 works");
}

void loop() {
  Serial.print("[DIAG] running on GPIO1, count=");
  Serial.println(messageNumber);

  Serial1.print("[DIAG] running on GPIO2, count=");
  Serial1.println(messageNumber);

  messageNumber++;
  delay(1000);
}
