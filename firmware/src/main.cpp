/*
 * Protogen LED-EQ firmware for ESP32-C3 Super Mini
 *   INMP441 → I²S → FFT → per-band gating → USB CDC packets
 *
 * Wiring INMP441 → ESP32-C3:
 *   VDD→3V3  GND→GND  L/R→GND
 *   SCK→GPIO4  WS→GPIO5  SD→GPIO6
 *
 * Onboard LED indicator: GPIO 8 (active-low on most SuperMini boards)
 *
 * Outgoing packet frame: [0xAA][0x55][type][len][payload...][xor]
 *   0x01 (len 16) = EQ magnitudes per band (uint8)
 *   0x02 (len 16) = stats: uptime, heap, notify_rate, sample_rate, peak, calibrated
 *   0x03 (len  8) = current settings (margin*10, gain*10, amp, smoothing, calibrated, reserved)
 *   0xFF (len  1) = boot signal (i2s_ok flag)
 *
 * Incoming commands (text + \n):
 *   M<int>   set noise margin in dB        (0..30)
 *   G<int>   set gain multiplier            (1..100)
 *   A<int>   set raw amplification          (1..50)
 *   S<int>   set smoothing percent          (0..100)
 *   C        recalibrate now
 *   R        reset all settings to defaults
 *   ?        request current settings
 */

#include <Arduino.h>
#include <driver/i2s.h>
#include <arduinoFFT.h>
#include <math.h>

// ============== PINS ==============
#define I2S_BCK   4
#define I2S_WS    5
#define I2S_DIN   6
#define LED_PIN   8

// ============== AUDIO (low latency) ==============
#define SAMPLES        128
#define SAMPLE_RATE    16000
#define BANDS          16
#define DMA_BUF_COUNT  2
#define DMA_BUF_LEN    SAMPLES

// ============== DEFAULTS ==============
#define DEFAULT_NOISE_MARGIN  2.0
#define DEFAULT_DB_GAIN       20.0
#define DEFAULT_AMP           1
#define DEFAULT_SMOOTHING     10
#define CAL_FRAMES_NEEDED     60

double vReal[SAMPLES], vImag[SAMPLES];
ArduinoFFT<double> FFT(vReal, vImag, SAMPLES, SAMPLE_RATE);

int      band_edges[BANDS + 1];
double   band_noise[BANDS]    = {0};
double   band_smoothed[BANDS] = {0};
uint8_t  bands_out[BANDS];
int32_t  i2s_buf[SAMPLES];

volatile double noise_margin = DEFAULT_NOISE_MARGIN;
volatile double db_gain      = DEFAULT_DB_GAIN;
volatile int    amp_mult     = DEFAULT_AMP;
volatile int    smoothing    = DEFAULT_SMOOTHING;

bool     i2s_ok     = false;
bool     calibrated = false;
uint32_t cal_frames = 0;

uint32_t notify_count       = 0;
uint32_t notify_count_total = 0;
uint32_t last_stats_ms      = 0;
uint32_t last_settings_ms   = 0;
int16_t  peak_rms           = 0;

String inputBuf = "";

// ============== I²S ==============
bool setupI2S() {
  i2s_config_t cfg = {
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate          = SAMPLE_RATE,
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = DMA_BUF_COUNT,
    .dma_buf_len          = DMA_BUF_LEN,
    .use_apll             = false,
    .tx_desc_auto_clear   = false,
    .fixed_mclk           = 0,
  };
  i2s_pin_config_t pins = {
    .bck_io_num   = I2S_BCK,
    .ws_io_num    = I2S_WS,
    .data_out_num = -1,
    .data_in_num  = I2S_DIN,
  };
  if (i2s_driver_install(I2S_NUM_0, &cfg, 0, nullptr) != ESP_OK) return false;
  return i2s_set_pin(I2S_NUM_0, &pins) == ESP_OK;
}

// ============== BANDS ==============
void initBands() {
  double lo = log(2.0), hi = log(60.0);
  for (int i = 0; i <= BANDS; i++) {
    band_edges[i] = (int)round(exp(lo + (hi - lo) * i / BANDS));
    if (i > 0 && band_edges[i] <= band_edges[i-1])
      band_edges[i] = band_edges[i-1] + 1;
  }
}

void resetCalibration() {
  for (int i = 0; i < BANDS; i++) {
    band_noise[i] = 0;
    band_smoothed[i] = 0;
  }
  cal_frames = 0;
  calibrated = false;
}

void resetDefaults() {
  noise_margin = DEFAULT_NOISE_MARGIN;
  db_gain      = DEFAULT_DB_GAIN;
  amp_mult     = DEFAULT_AMP;
  smoothing    = DEFAULT_SMOOTHING;
}

// ============== PACKETS ==============
static inline uint8_t xor_cs(const uint8_t* p, size_t n) {
  uint8_t c = 0;
  for (size_t i = 0; i < n; i++) c ^= p[i];
  return c;
}

void sendPacket(uint8_t type, const uint8_t* data, uint8_t len) {
  uint8_t hdr[4] = { 0xAA, 0x55, type, len };
  uint8_t cs = xor_cs(hdr, 4) ^ xor_cs(data, len);
  Serial.write(hdr, 4);
  Serial.write(data, len);
  Serial.write(&cs, 1);
}

void sendSettings() {
  uint8_t pkt[8];
  uint16_t m = (uint16_t)(noise_margin * 10);
  uint16_t g = (uint16_t)(db_gain * 10);
  uint8_t  a = (uint8_t)amp_mult;
  uint8_t  s = (uint8_t)smoothing;
  uint8_t  c = calibrated ? 1 : 0;
  uint8_t  r = 0;
  memcpy(pkt + 0, &m, 2);
  memcpy(pkt + 2, &g, 2);
  pkt[4] = a;
  pkt[5] = s;
  pkt[6] = c;
  pkt[7] = r;
  sendPacket(0x03, pkt, 8);
}

// ============== COMMANDS ==============
void processCommand(const String& cmd) {
  if (cmd.length() < 1) return;
  char c = cmd.charAt(0);
  String arg = cmd.substring(1); arg.trim();
  int v = arg.toInt();

  if      (c == 'M' && v >= 0 && v <= 30)  noise_margin = (double)v;
  else if (c == 'G' && v >= 1 && v <= 100) db_gain      = (double)v;
  else if (c == 'A' && v >= 1 && v <= 50)  amp_mult     = v;
  else if (c == 'S' && v >= 0 && v <= 100) smoothing    = v;
  else if (c == 'C') resetCalibration();
  else if (c == 'R') { resetDefaults(); resetCalibration(); }

  sendSettings();
}

void readSerialCommands() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (inputBuf.length() > 0) { processCommand(inputBuf); inputBuf = ""; }
    } else if (inputBuf.length() < 32) {
      inputBuf += ch;
    }
  }
}

// ============== SETUP ==============
void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  delay(400);

  i2s_ok = setupI2S();
  initBands();
  resetCalibration();
  resetDefaults();

  uint8_t hello[1] = { (uint8_t)(i2s_ok ? 1 : 0) };
  sendPacket(0xFF, hello, 1);
  sendSettings();
}

// ============== LOOP ==============
void loop() {
  readSerialCommands();

  if (!i2s_ok) {
    digitalWrite(LED_PIN, (millis() / 100) & 1);
    delay(50);
    return;
  }

  size_t br = 0;
  if (i2s_read(I2S_NUM_0, i2s_buf, sizeof(i2s_buf), &br, portMAX_DELAY) != ESP_OK) return;
  if (br / 4 != SAMPLES) return;

  // DC removal + amplification
  long long sum = 0;
  for (int i = 0; i < SAMPLES; i++) sum += (i2s_buf[i] >> 14);
  int32_t dc = (int32_t)(sum / SAMPLES);

  long long sq = 0;
  for (int i = 0; i < SAMPLES; i++) {
    int32_t s = ((i2s_buf[i] >> 14) - dc) * amp_mult;
    vReal[i] = (double)s;
    vImag[i] = 0.0;
    sq += (long long)s * s;
  }
  int32_t rms = (int32_t)sqrt((double)sq / SAMPLES);
  if (rms > peak_rms) peak_rms = (int16_t)min<int32_t>(rms, 32767);

  FFT.windowing(FFTWindow::Hamming, FFTDirection::Forward);
  FFT.compute(FFTDirection::Forward);
  FFT.complexToMagnitude();

  double smooth_a = smoothing / 100.0;

  for (int b = 0; b < BANDS; b++) {
    int lo = band_edges[b], hi = band_edges[b + 1];
    double mx = 0;
    for (int k = lo; k < hi && k < SAMPLES / 2; k++) {
      if (vReal[k] > mx) mx = vReal[k];
    }
    double db = 20.0 * log10(mx + 1.0);

    if (!calibrated) {
      if (db > band_noise[b]) band_noise[b] = db;
      bands_out[b] = 0;
    } else {
      double floor_db = band_noise[b] + noise_margin;
      double norm = (db - floor_db) * db_gain;
      if (norm < 0) norm = 0;
      if (norm > 255) norm = 255;
      band_smoothed[b] = band_smoothed[b] * smooth_a + norm * (1.0 - smooth_a);
      bands_out[b] = (uint8_t)band_smoothed[b];
    }
  }

  if (!calibrated) {
    cal_frames++;
    uint8_t pos = (uint8_t)((cal_frames * BANDS) / CAL_FRAMES_NEEDED);
    if (pos > BANDS - 1) pos = BANDS - 1;
    for (int i = 0; i < BANDS; i++) bands_out[i] = (i == pos) ? 200 : 0;
    if (cal_frames >= CAL_FRAMES_NEEDED) {
      calibrated = true;
      for (int k = 0; k < 4; k++) {
        digitalWrite(LED_PIN, LOW); delay(60);
        digitalWrite(LED_PIN, HIGH); delay(60);
      }
      sendSettings();
    }
  }

  sendPacket(0x01, bands_out, BANDS);
  notify_count++;
  notify_count_total++;
  digitalWrite(LED_PIN, (notify_count_total / 50) & 1 ? LOW : HIGH);

  uint32_t now = millis();
  if (now - last_stats_ms >= 1000) {
    uint8_t pkt[16];
    uint32_t up    = now;
    uint32_t heap  = ESP.getFreeHeap();
    uint16_t ncnt  = (uint16_t)notify_count;
    uint16_t srate = SAMPLE_RATE;
    int16_t  pk    = peak_rms;
    memcpy(pkt + 0,  &up,    4);
    memcpy(pkt + 4,  &heap,  4);
    memcpy(pkt + 8,  &ncnt,  2);
    memcpy(pkt + 10, &srate, 2);
    memcpy(pkt + 12, &pk,    2);
    pkt[14] = calibrated ? 1 : 0;
    pkt[15] = (uint8_t)((i2s_ok ? 1 : 0) | (calibrated ? 2 : 0));
    sendPacket(0x02, pkt, 16);
    last_stats_ms = now;
    notify_count = 0;
    peak_rms = 0;
  }

  if (now - last_settings_ms >= 5000) {
    sendSettings();
    last_settings_ms = now;
  }
}
