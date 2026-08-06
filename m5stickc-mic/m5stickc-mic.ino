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
// Board: "M5Stick-C" (or generic ESP32 Dev Module if that's not listed)
// in Arduino IDE's board manager.

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

WiFiUDP udp;
int16_t audioBuffer[BUFFER_LEN];

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
  Serial.begin(115200);
  setupWiFi();
  setupMic();
  udp.begin(0);
  Serial.println("Streaming mic audio via UDP...");
}

void loop() {
  size_t bytesRead = 0;
  i2s_read(I2S_PORT, audioBuffer, sizeof(audioBuffer), &bytesRead, portMAX_DELAY);
  if (bytesRead > 0) {
    udp.beginPacket(PI_IP, PI_PORT);
    udp.write((uint8_t*)audioBuffer, bytesRead);
    udp.endPacket();
  }
}
