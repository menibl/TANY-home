// M5StickC PLUS — streams its built-in PDM microphone over WiFi/UDP to
// the Raspberry Pi, as a substitute for a physical mic. Bluetooth (HFP)
// would need a full hands-free protocol stack implemented from scratch —
// this is the same audio, over a much simpler transport, using standard
// ESP32 I2S APIs.
//
// Pi side: pi-tools/udp_mic_bridge.py listens on PI_PORT and pipes what
// it receives into an ALSA loopback device — capture-svc then sees it as
// an ordinary capture device (see README in pi-tools/), no Python
// changes needed there at all.
//
// Screen shows two eyes: colour = connection state (red = no WiFi, blue
// = connected and streaming), and they widen briefly whenever the mic
// picks up sound above a simple threshold — a visual "I hear you". This
// is a local VU-meter reaction, not a confirmation the Pi actually
// received that specific packet (UDP is fire-and-forget, one-way, no
// ack channel back from the Pi) — it tells you the mic itself is alive,
// not that the Pi-side pipeline is healthy end to end.
//
// Board: "M5Stick-C" (or generic ESP32 Dev Module if that's not listed)
// in Arduino IDE's board manager. Library: M5StickCPlus (Library
// Manager -> search "M5StickCPlus" by M5Stack).

#include <M5StickCPlus.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <HTTPClient.h>
#include "driver/i2s.h"

const char* WIFI_SSID = "Meni";
const char* WIFI_PASSWORD = "0543265994";

// Update this if the Pi's IP changes (it has before, twice, this session —
// DHCP reassigned it after each reboot). Check with `hostname -I` on the Pi.
const char* PI_IP = "192.168.68.53";
const int PI_PORT = 5005;
// Same endpoint the dashboard's own manual-trigger button already
// posts to (capture-svc/web_ui.py) — Button A on the M5StickC becomes
// a second physical trigger alongside the double-clap detector, no
// Pi-side code changes needed at all.
const char* PI_TRIGGER_URL = "http://192.168.68.53:8010/api/trigger";
// Polled to animate the eyes while a conversation is active, so it's
// visible at a glance whether the bot is still busy or has gone back
// to idle (needing a fresh clap/button press to start again). Plain
// text ("listening", "session", ...) not JSON — nothing on this board
// to parse JSON with, and we only need to compare the whole string.
const char* PI_STATE_URL = "http://192.168.68.53:8010/api/state";
// Same endpoint the dashboard's "end session" button posts to — Button A
// ends an in-progress conversation instead of starting a new one when
// pressed while conversationActive is true (see checkButton()).
const char* PI_END_SESSION_URL = "http://192.168.68.53:8010/api/end-session";

#define I2S_PORT      I2S_NUM_0
#define SAMPLE_RATE   16000
#define BUFFER_LEN    512   // samples per UDP packet (1024 bytes at 16-bit)

// Built-in PDM microphone pins. DATA was 34 -- confirmed wrong live:
// mic read exactly 0 always (no error), eyes never widened on a close
// loud clap, and raw UDP packets received on the Pi were 100%
// zero-filled. The M5StickCPlus library's own bundled mic example
// (Examples > M5StickCPlus, matching the exact installed library
// version) defines its data pin as 33, not 34 -- more authoritative
// than the "consistent across the family" assumption this used to be
// written on.
#define MIC_CLK_PIN   0
#define MIC_DATA_PIN  33

// Peak amplitude (of int16 full scale 32767) above which we consider
// "sound heard" for the eye-widen reaction. Tune if it feels too
// twitchy or too dead in practice.
#define SOUND_THRESHOLD  1500

WiFiUDP udp;
int16_t audioBuffer[BUFFER_LEN];

// ---- eyes -------------------------------------------------------------

// ~3x the original size (was CY=65, R_NARROW=14, R_WIDE=24, CX=45/90) —
// centers pushed apart and toward the screen edges too, otherwise the
// bigger circles would overlap. Checked against the rotated 240x135
// panel: worst case (both eyes wide, r=55) spans x=[5,235], y=[12,122],
// still inside bounds with a few px of margin.
const int EYE_CY = 67, EYE_R_NARROW = 40, EYE_R_WIDE = 55;
const int EYE_CX1 = 60, EYE_CX2 = 180;
bool eyesWide = false;
uint16_t eyeColor = RED;

void drawEyes(uint16_t color, bool wide) {
  M5.Lcd.fillScreen(BLACK);
  int r = wide ? EYE_R_WIDE : EYE_R_NARROW;
  M5.Lcd.fillCircle(EYE_CX1, EYE_CY, r, color);
  M5.Lcd.fillCircle(EYE_CX2, EYE_CY, r, color);
  M5.Lcd.fillCircle(EYE_CX1, EYE_CY, r / 3, BLACK);
  M5.Lcd.fillCircle(EYE_CX2, EYE_CY, r / 3, BLACK);
}

void setEyeState(uint16_t color, bool wide) {
  if (color == eyeColor && wide == eyesWide) return;  // skip redundant redraws
  eyeColor = color;
  eyesWide = wide;
  drawEyes(color, wide);
}

// -------------------------------------------------------------------------

void setupWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print(".");
  }
  // ESP32's default WiFi power-save (modem sleep) adds real latency to
  // every send — measured on the Pi side: packets were arriving every
  // ~65ms instead of the ~32ms of audio each one actually represents,
  // meaning audio was being generated/sent slower than real-time no
  // matter how much buffering the receiving side had. This is the
  // single biggest fix for that.
  WiFi.setSleep(false);
  Serial.println();
  Serial.print("Connected, IP: ");
  Serial.println(WiFi.localIP());
  // screen doesn't exist yet — M5.begin() runs after this on purpose
  // (see setup()) — the eyes get drawn there instead.
}

void setupMic() {
  i2s_config_t i2s_config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM),
    .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_RIGHT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 4,
    .dma_buf_len = BUFFER_LEN,
    .use_apll = false,
  };
  i2s_pin_config_t pin_config = {
    .bck_io_num = I2S_PIN_NO_CHANGE,
    .ws_io_num = MIC_CLK_PIN,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = MIC_DATA_PIN,
  };
  i2s_driver_install(I2S_PORT, &i2s_config, 0, NULL);
  i2s_set_pin(I2S_PORT, &pin_config);
  // Confirmed live: i2s_read() succeeds (no error, real bytesRead) but
  // every sample comes back exactly 0 -- eyes never widen even on a
  // loud clap right next to the mic, and raw UDP packets received on
  // the Pi are 100% zero-filled too, so this isn't a network/timing
  // issue, it's the I2S capture itself. A known ESP32-Arduino PDM
  // gotcha: i2s_driver_install() alone doesn't always actually clock
  // the PDM decimation filter -- an explicit i2s_set_clk() after pin
  // setup is the documented fix for exactly "reads succeed, data is
  // silently all-zero."
  i2s_set_clk(I2S_PORT, SAMPLE_RATE, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
}

void setup() {
  // WiFi connects BEFORE M5.begin() on purpose — M5.begin() also inits
  // the AXP192 power-management chip and the LCD backlight (LEDC PWM),
  // and doing that first was seen live to break WiFi association
  // entirely (endless "Connecting..." with no error, plus LEDC init
  // errors in the serial log). Connecting to WiFi first, while the
  // chip is still in its clean post-boot power state, then bringing up
  // the screen after, avoids whatever that interaction was.
  Serial.begin(115200);
  setupWiFi();
  M5.begin();
  M5.Lcd.setRotation(3);
  setEyeState(BLUE, false);  // already connected by the time the screen exists
  setupMic();
  udp.begin(0);
  Serial.println("Streaming mic audio via UDP...");
}

// True whenever the Pi reports anything other than "listening" (its
// idle, waiting-for-trigger state) — i.e. a conversation is in
// progress somewhere between clap/button and the reply finishing.
bool conversationActive = false;
unsigned long lastStatusPoll = 0;
const unsigned long STATUS_POLL_INTERVAL_MS = 700;

void checkStatus() {
  unsigned long now = millis();
  if (now - lastStatusPoll < STATUS_POLL_INTERVAL_MS) return;
  lastStatusPoll = now;
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(PI_STATE_URL);
  http.setTimeout(1500);
  int code = http.GET();
  if (code == 200) {
    conversationActive = (http.getString() != "listening");
  }
  http.end();
}

void checkButton() {
  M5.update();  // refreshes button state — must be called every loop
  if (M5.BtnA.wasPressed() && WiFi.status() == WL_CONNECTED) {
    // conversationActive comes from the last checkStatus() poll (up to
    // STATUS_POLL_INTERVAL_MS stale) — good enough for a button press.
    const char* url = conversationActive ? PI_END_SESSION_URL : PI_TRIGGER_URL;
    Serial.printf("Button A pressed -> %s\n", conversationActive ? "ending session" : "triggering");
    HTTPClient http;
    http.begin(url);
    http.setTimeout(3000);
    int code = http.POST("");
    Serial.printf("POST -> HTTP %d\n", code);
    http.end();
  }
}

void loop() {
  size_t bytesRead = 0;
  i2s_read(I2S_PORT, audioBuffer, sizeof(audioBuffer), &bytesRead, portMAX_DELAY);
  checkButton();  // polled once per mic buffer (~32ms) — plenty fast for a button press
  checkStatus();  // internally rate-limited to STATUS_POLL_INTERVAL_MS
  if (bytesRead == 0) return;

  int sampleCount = bytesRead / sizeof(int16_t);
  int16_t peak = 0;
  for (int i = 0; i < sampleCount; i++) {
    int16_t v = audioBuffer[i];
    if (v < 0) v = -v;
    if (v > peak) peak = v;
  }

  // Serial-only mic-level check, independent of WiFi/UDP/the Pi
  // entirely -- confirms whether i2s_set_clk() above actually fixed
  // real capture before chasing anything further downstream.
  static unsigned long lastPeakPrint = 0;
  if (millis() - lastPeakPrint >= 500) {
    lastPeakPrint = millis();
    Serial.printf("mic peak=%d\n", peak);
  }

  if (WiFi.status() == WL_CONNECTED) {
    if (conversationActive) {
      // pulse wide/narrow every 400ms, independent of the mic's own
      // peak level — a steady heartbeat says "bot is busy with you"
      // at a glance, distinct from the idle VU-meter reaction below,
      // so it's obvious when it's done and waiting for a new trigger.
      bool pulse = (millis() / 400) % 2 == 0;
      setEyeState(GREEN, pulse);
    } else {
      setEyeState(BLUE, peak > SOUND_THRESHOLD);
    }
    udp.beginPacket(PI_IP, PI_PORT);
    udp.write((uint8_t*)audioBuffer, bytesRead);
    udp.endPacket();
  } else {
    setEyeState(RED, false);
  }
}
