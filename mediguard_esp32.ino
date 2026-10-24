#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <DHT.h>
#include <ESP32Servo.h>

// =====================================================
// Wi-Fi and server settings
// =====================================================
const char* WIFI_SSID  = "YOUR_WIFI_NAME";
const char* WIFI_PASS  = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL = "http://192.168.1.50:5000/api/readings"; // printed when you run app.py
const char* API_KEY    = "change-me";                              // must match MEDIGUARD_KEY on the server
const char* DEVICE_ID  = "mediguard-1";
const unsigned long POST_INTERVAL_MS = 10000;                      // send a reading every 10 s
unsigned long lastPostMs = 0;
bool coolingActive = false;   // true while the cooling fan is commanded on

// =====================================================
// PIN DEFINITIONS
// =====================================================

#define DHT_PIN 4
#define DHT_TYPE DHT22


#define OLED_SDA 21
#define OLED_SCL 22
#define OLED_ADDRESS 0x3C

// Cooling flap servo
#define COOL_SERVO_PIN 27

// Warm-air flap servo
#define WARM_SERVO_PIN 14

// Cooling fan
#define COOLING_FAN_PIN 26

// Warm-air fan
#define WARM_FAN_PIN 25

// Buzzer
#define BUZZER_PIN 33


// =====================================================
// OBJECTS
// =====================================================

DHT dht(DHT_PIN, DHT_TYPE);


Servo coolingServo;
Servo warmingServo;

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64

Adafruit_SSD1306 display(
  SCREEN_WIDTH,
  SCREEN_HEIGHT,
  &Wire,
  -1
);


// =====================================================
// TEMPERATURE LIMITS
// =====================================================

const float VERY_COLD = 1.0;
const float LOW_LIMIT = 2.0;
const float HIGH_LIMIT = 8.0;
const float SEVEN_DEGREE = 7.0;


// =====================================================
// VARIABLES FOR CROSSING DETECTION
// =====================================================

float previousTemperature = -100.0;



// =====================================================
// BUZZER FUNCTIONS
// =====================================================

void beepOnce() {

  digitalWrite(BUZZER_PIN, HIGH);
  delay(250);

  digitalWrite(BUZZER_PIN, LOW);
  delay(250);
}


void beepTwoTimes() {

  beepOnce();
  beepOnce();
}


void beepThreeTimes() {

  beepOnce();
  beepOnce();
  beepOnce();
}


// =====================================================
// OLED
// =====================================================

void showDisplay(float temperature,
                 float humidity,
                 String status) {

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // Title
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println("MEDIGUARD");

  // Temperature
  display.setTextSize(2);
  display.setCursor(0, 14);
  display.print(temperature, 1);
  display.println(" C");

  // Humidity
  display.setTextSize(1);
  display.setCursor(0, 38);
  display.print("Humidity: ");
  display.print(humidity, 0);
  display.println("%");

  // Status
  display.setCursor(0, 53);
  display.println(status);

  display.display();
}


// =====================================================
// STARTUP
// =====================================================

void startupScreen() {

  display.clearDisplay();

  display.setTextColor(SSD1306_WHITE);

  display.setTextSize(2);
  display.setCursor(5, 15);
  display.println("MEDIGUARD");

  display.setTextSize(1);
  display.setCursor(25, 42);
  display.println("STARTING...");

  display.display();

  delay(3000);
}


// =====================================================
// Wi-Fi connection
// =====================================================

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  Serial.print("Connecting to Wi-Fi");
  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 30) {
    delay(500);
    Serial.print(".");
    tries++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println();
    Serial.print("Wi-Fi connected. IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println();
    Serial.println("Wi-Fi FAILED to connect. Will keep retrying in the background.");
  }
}

void keepWiFiAlive() {
  static unsigned long lastTry = 0;
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastTry < 15000) return;
  lastTry = millis();
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}


// =====================================================
// Send a reading to the dashboard API
// =====================================================

void sendReading(float temperature, float humidity) {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.setTimeout(4000);
  if (!http.begin(SERVER_URL)) return;
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", API_KEY);

  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["temperature"] = temperature;
  doc["humidity"] = humidity;
  doc["cooling"] = coolingActive;
  doc["servo_angle"] = coolingServo.read();

  String body;
  serializeJson(doc, body);

  int code = http.POST(body);
  if (code == 200) {
    Serial.println("Reading sent to server.");
  } else {
    Serial.printf("Send to server failed (code %d)\n", code);
  }
  http.end();
}


// =====================================================
// SETUP
// =====================================================

void setup() {

  Serial.begin(115200);

  // Fan and buzzer outputs
  pinMode(COOLING_FAN_PIN, OUTPUT);
  pinMode(WARM_FAN_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);

  digitalWrite(COOLING_FAN_PIN, LOW);
  digitalWrite(WARM_FAN_PIN, LOW);
  digitalWrite(BUZZER_PIN, LOW);


  // OLED
  Wire.begin(OLED_SDA, OLED_SCL);

  if (!display.begin(
        SSD1306_SWITCHCAPVCC,
        OLED_ADDRESS)) {

    Serial.println("OLED NOT FOUND!");

    while (1);
  }


  // Sensors
  dht.begin();


  // Servos
  coolingServo.attach(COOL_SERVO_PIN);
  warmingServo.attach(WARM_SERVO_PIN);


  // Initial positions
  // Cooling flap CLOSED
  coolingServo.write(180);

  // Warm flap CLOSED
  warmingServo.write(0);


  // Startup message
  startupScreen();

  // Wi-Fi
  connectWiFi();

  Serial.println("MediGuard Started");
}


// =====================================================
// MAIN LOOP
// =====================================================

void loop() {

  keepWiFiAlive();

  // ---------------------------------------------------
  // READ DHT22 (temperature + humidity)
  // ---------------------------------------------------
  float temperature = dht.readTemperature();
  float humidity = dht.readHumidity();

  // ---------------------------------------------------
  // SENSOR ERROR: do not run automatic control on invalid data
  // ---------------------------------------------------
  if (isnan(temperature) || isnan(humidity)) {
    digitalWrite(COOLING_FAN_PIN, LOW);
    digitalWrite(WARM_FAN_PIN, LOW);
    digitalWrite(BUZZER_PIN, HIGH);  // sensor fault needs user attention
    coolingActive = false;
    coolingServo.write(180);  // close the cooling path on a sensor fault
    warmingServo.write(0);    // close the warm-air path too

    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(0, 10);
    display.println("MEDIGUARD");
    display.setCursor(0, 30);
    display.println("DHT22 SENSOR ERROR");
    display.setCursor(0, 45);
    display.println("Check sensor/wiring");
    display.display();

    Serial.println("DHT22 read failed; fans disabled.");
    delay(2000);
    return;
  }

  // ---------------------------------------------------
  // SERIAL MONITOR
  // ---------------------------------------------------

  Serial.print("Temperature: ");
  Serial.print(temperature, 2);

  Serial.print(" C   Humidity: ");
  Serial.print(humidity, 0);

  Serial.println(" %");


  // ===================================================
  // DETECT CROSSING OF 2°C DOWNWARD
  // ===================================================

  if (previousTemperature > LOW_LIMIT &&
      temperature <= LOW_LIMIT) {

    Serial.println("Temperature crossed DOWN through 2 C");

    beepTwoTimes();

  }


  // ===================================================
  // DETECT CROSSING OF 7°C UPWARD
  // ===================================================

  if (previousTemperature < SEVEN_DEGREE &&
      temperature >= SEVEN_DEGREE) {

    Serial.println("Temperature crossed UP through 7 C");

    beepThreeTimes();

  }


  // ===================================================
  // VERY COLD: 1°C OR BELOW
  // ===================================================

  if (temperature <= VERY_COLD) {

    Serial.println("HIGH RISK - TOO COLD; COOLING STOPPED");

    // Cooling OFF
    digitalWrite(COOLING_FAN_PIN, LOW);
    coolingActive = false;

    // Room air is not a heater; keep the warm-air fan off.
    digitalWrite(WARM_FAN_PIN, LOW);

    // Cooling flap CLOSED
    coolingServo.write(180);

    // Keep the warm-air flap closed.
    warmingServo.write(0);

    // Continuous buzzer
    digitalWrite(BUZZER_PIN, HIGH);

    showDisplay(
      temperature,
      humidity,
      "TOO COLD - CHECK"
    );
  }


  // ===================================================
  // BELOW 2°C: stop cooling and report a low-temperature condition.
  // The second fan cannot guarantee warming: there is no ambient sensor,
  // and room air may be colder than the medicine chamber.
  // ===================================================

  else if (temperature < LOW_LIMIT) {
    Serial.println("LOW TEMPERATURE - COOLING STOPPED");
    digitalWrite(COOLING_FAN_PIN, LOW);
    digitalWrite(WARM_FAN_PIN, LOW);
    coolingActive = false;
    coolingServo.write(180);  // cooling flap closed
    warmingServo.write(0);    // warm-air flap closed
    digitalWrite(BUZZER_PIN, HIGH);  // keep a clear warning active below the lower limit
    showDisplay(temperature, humidity, "TOO COLD - CHECK");
  }


  // ===================================================
  // NORMAL: 2°C TO 7°C
  // ===================================================

  else if (temperature >= LOW_LIMIT &&
           temperature < SEVEN_DEGREE) {

    Serial.println("NORMAL");

    // Both fans OFF
    digitalWrite(COOLING_FAN_PIN, LOW);
    digitalWrite(WARM_FAN_PIN, LOW);
    coolingActive = false;

    // Both flaps CLOSED
    coolingServo.write(180);
    warmingServo.write(0);

    // Buzzer OFF
    digitalWrite(BUZZER_PIN, LOW);

    showDisplay(
      temperature,
      humidity,
      "NORMAL"
    );
  }


  // ===================================================
  // 7°C TO 8°C = COOLING
  // ===================================================

  else if (temperature >= SEVEN_DEGREE &&
           temperature <= HIGH_LIMIT) {

    Serial.println("COOLING");

    // Cooling flap OPEN
    coolingServo.write(0);

    // Cooling fan ON
    digitalWrite(COOLING_FAN_PIN, HIGH);
    coolingActive = true; 

    // Warm system OFF
    digitalWrite(WARM_FAN_PIN, LOW);
    warmingServo.write(0);

    // Buzzer OFF
    digitalWrite(BUZZER_PIN, LOW);

    showDisplay(
      temperature,
      humidity,
      "COOLING"
    );
  }


  // ===================================================
  // ABOVE 8°C = HIGH RISK
  // ===================================================

  else {

    Serial.println("HIGH RISK - TOO HOT");

    // Cooling flap OPEN
    coolingServo.write(0);

    // Cooling fan ON
    digitalWrite(COOLING_FAN_PIN, HIGH);
    coolingActive = true; 

    // Warm system OFF
    digitalWrite(WARM_FAN_PIN, LOW);
    warmingServo.write(0);

    // Continuous buzzer
    digitalWrite(BUZZER_PIN, HIGH);

    showDisplay(
      temperature,
      humidity,
      "HIGH RISK HOT"
    );
  }


  // Save current temperature
  previousTemperature = temperature;

  // ---------------------------------------------------
  // Send readings to the server at a slower interval than the sensor loop.
  // ---------------------------------------------------
  if (millis() - lastPostMs >= POST_INTERVAL_MS) {
    lastPostMs = millis();
    sendReading(temperature, humidity);
  }

  delay(2000);  // DHT22 should not be sampled more frequently than about every 2 seconds
}
