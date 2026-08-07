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
#include "driver/i2s.h"

const char* WIFI_SSID = "Meni";
const char* WIFI_PASSWORD = "0543265994";

// Update this if the Pi's IP changes (it has before, twice, this session —
// DHCP reassigned it after each reboot). Check with `hostname -I` on the Pi.
const char* PI_IP = "192.168.68.76";
const int PI_PORT = 5005;

#define I2S_PORT      I2S_NUM_0
#define SAMPLE_RATE   16000
#define BUFFER_LEN    512   // samples per UDP packet (1024 bytes at 16-bit)

// Built-in PDM microphone pins, consistent across the M5StickC family
// (M5StickC, PLUS, PLUS2 all wire the SPM1423 mic the same way).
#define MIC_CLK_PIN   0
#define MIC_DATA_PIN  34

// Peak amplitude (of int16 full scale 32767) above which we consider
// "sound heard" for the eye-widen reaction. Tune if it feels too
// twitchy or too dead in practice.
#define SOUND_THRESHOLD  1500

WiFiUDP udp;
int16_t audioBuffer[BUFFER_LEN];

// ---- eyes -------------------------------------------------------------

const int EYE_CY = 65, EYE_R_NARROW = 14, EYE_R_WIDE = 24;
const int EYE_CX1 = 45, EYE_CX2 = 90;
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

void loop() {
  size_t bytesRead = 0;
  i2s_read(I2S_PORT, audioBuffer, sizeof(audioBuffer), &bytesRead, portMAX_DELAY);
  if (bytesRead == 0) return;

  int sampleCount = bytesRead / sizeof(int16_t);
  int16_t peak = 0;
  for (int i = 0; i < sampleCount; i++) {
    int16_t v = audioBuffer[i];
    if (v < 0) v = -v;
    if (v > peak) peak = v;
  }

  if (WiFi.status() == WL_CONNECTED) {
    setEyeState(BLUE, peak > SOUND_THRESHOLD);
    udp.beginPacket(PI_IP, PI_PORT);
    udp.write((uint8_t*)audioBuffer, bytesRead);
    udp.endPacket();
  } else {
    setEyeState(RED, false);
  }
}
