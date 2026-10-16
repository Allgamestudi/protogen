/*
 * Protogen ESP32-S3 firmware — Waveshare ESP32-S3-LCD-1.46
 *
 * No rangefinder in this build. Only mic + accelerometer.
 *
 * Hardware:
 *   INMP441   (I²S microphone)   SCK→GPIO38   WS→GPIO39   SD→GPIO40   L/R→GND
 *   ADXL345   (I²C accelerometer) SDA→GPIO41  SCL→GPIO42  CS→3V3  SDO→GND
 *   Status LED (optional, simple LED) GPIO21 — leave floating if you don't want a LED
 *
 * Pin choice rationale (Waveshare ESP32-S3 1.46):
 *   The onboard 1.46" LCD, touch controller, and any onboard IMU occupy GPIOs
 *   2–17 on this dev board. We deliberately pick GPIOs 38–42 from the upper
 *   header — they are broken out and free on every Waveshare S3 dev board I've
 *   seen. If your board variant uses one of these for something else, just
 *   change the #defines at the top.
 *
 * Arduino IDE / arduino-cli libraries required:
 *   arduinoFFT                  (kosme)
 *   Adafruit ADXL345
 *   Adafruit Unified Sensor
 *
 * Board settings in Arduino IDE 2:
 *   Board:                ESP32S3 Dev Module
 *   USB CDC On Boot:      ENABLED   ← MUST
 *   USB Mode:             Hardware CDC and JTAG
 *   USB DFU On Boot:      Disabled
 *   CPU Frequency:        240MHz
 *   Flash Mode:           QIO 80MHz
 *   Flash Size:           16MB (or whatever your board has)
 *   PSRAM:                OPI PSRAM (if board has PSRAM, else Disabled)
 *   Partition Scheme:     Default 4MB with spiffs (or 16M with spiffs if 16MB flash)
 *
 * USB-CDC binary protocol — every packet: [0xAA][0x55][type][len][payload][xor]
 *   0x01  16 B  EQ magnitudes per band
 *   0x02  16 B  stats (uptime, heap, notify rate, sample rate, peak, flags)
 *   0x03   8 B  current settings (margin, gain, amp, smoothing, calibrated)
 *   0x05   8 B  motion:  i16 ax_mg, i16 ay_mg, i16 az_mg, u8 tilt°, u8 flags
 *   0xFF   1 B  boot signal:  bit0=i2s_ok  bit1=(reserved, was tof)  bit2=acc_ok
 *
 * Stats flags (packet 0x02 byte 15): bit0=i2s bit1=calibrated bit2=(reserved) bit3=acc_ok
 * Motion flags (packet 0x05 byte 7): bit0=shake bit1=impact bit2=freefall bit3=flat
 *
 * Text commands (terminated by \n):
 *   M<int>  noise margin (0..30 dB)
 *   G<int>  gain         (1..100)
 *   A<int>  raw amplify  (1..50)
 *   S<int>  smoothing    (0..100)
 *   C       recalibrate
 *   R       reset to defaults
 *   ?       request settings packet
 */

#include <Arduino.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <arduinoFFT.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_ADXL345_U.h>
#include <math.h>

// ───────────────────────────  pins  ───────────────────────────
#define I2S_BCK    38
#define I2S_WS     39
#define I2S_DIN    40
#define LED_PIN    21         // simple status LED (optional)

#define I2C_SDA    41
#define I2C_SCL    42

// ───────────────────────────  audio  ──────────────────────────
#define SAMPLES        128
#define SAMPLE_RATE    16000
#define BANDS          16
#define DMA_BUF_COUNT  2
#define DMA_BUF_LEN    SAMPLES

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

bool     i2s_ok        = false;
bool     calibrated    = false;
uint32_t cal_frames    = 0;

uint32_t notify_count       = 0;
uint32_t notify_count_total = 0;
uint32_t last_stats_ms      = 0;
uint32_t last_settings_ms   = 0;
int16_t  peak_rms           = 0;

String inputBuf = "";

// ────────────────────────  accelerometer  ─────────────────────
Adafruit_ADXL345_Unified accel(12345);
bool     acc_ok = false;
uint32_t last_imu_ms = 0;
const uint32_t IMU_PERIOD_MS = 50;

#define SHAKE_N 8
float    shake_win[SHAKE_N] = {0};
uint8_t  shake_idx = 0;
float    freefall_t = 0.0f;
float    flat_t     = 0.0f;
uint32_t last_imu_us = 0;
volatile bool ev_impact = false;

// ───────────────────────  I²S setup  ──────────────────────────
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

// ───────────────────────  bands  ──────────────────────────────
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

// ───────────────────────  packets  ────────────────────────────
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

// ───────────────────────  commands  ───────────────────────────
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

// ───────────────────────  sensor setup  ───────────────────────
void setupSensors() {
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(400000);

  if (accel.begin()) {
    accel.setRange(ADXL345_RANGE_4_G);
    accel.setDataRate(ADXL345_DATARATE_100_HZ);
    acc_ok = true;
  }
}

// ───────────────────────  motion  ────────────────────────────
void processMotion() {
  if (!acc_ok) return;
  uint32_t now_us = micros();
  float dt = (last_imu_us == 0) ? 0.05f : (now_us - last_imu_us) / 1e6f;
  if (dt > 0.25f) dt = 0.25f;
  last_imu_us = now_us;

  sensors_event_t e;
  accel.getEvent(&e);
  float ax = e.acceleration.x;
  float ay = e.acceleration.y;
  float az = e.acceleration.z;
  float mag = sqrtf(ax*ax + ay*ay + az*az);
  float dev = fabsf(mag - 9.81f);

  shake_win[shake_idx] = dev;
  shake_idx = (shake_idx + 1) % SHAKE_N;
  float shake_avg = 0.0f;
  for (int i = 0; i < SHAKE_N; i++) shake_avg += shake_win[i];
  shake_avg /= SHAKE_N;
  bool shake = shake_avg > 4.0f;

  if (mag > 58.8f) ev_impact = true;
  bool impact = ev_impact;
  ev_impact = false;

  if (mag < 4.0f) freefall_t += dt; else freefall_t = 0.0f;
  bool freefall = freefall_t > 0.10f;

  bool oriented = (az > 8.5f) && fabsf(ax) < 2.5f && fabsf(ay) < 2.5f;
  if (oriented && shake_avg < 0.6f) flat_t += dt; else flat_t = 0.0f;
  bool flat = flat_t > 2.0f;

  float tilt = mag > 0.5f ? acosf(constrain(az / mag, -1.0f, 1.0f)) * 180.0f / (float)M_PI : 0.0f;
  if (tilt < 0) tilt = 0; else if (tilt > 180) tilt = 180;

  int16_t ax_mg = (int16_t)constrain((int)(ax / 9.81f * 1000.0f), -32760, 32760);
  int16_t ay_mg = (int16_t)constrain((int)(ay / 9.81f * 1000.0f), -32760, 32760);
  int16_t az_mg = (int16_t)constrain((int)(az / 9.81f * 1000.0f), -32760, 32760);

  uint8_t flags = (shake    ? 0x01 : 0)
                | (impact   ? 0x02 : 0)
                | (freefall ? 0x04 : 0)
                | (flat     ? 0x08 : 0);

  uint8_t pkt[8];
  memcpy(pkt + 0, &ax_mg, 2);
  memcpy(pkt + 2, &ay_mg, 2);
  memcpy(pkt + 4, &az_mg, 2);
  pkt[6] = (uint8_t)tilt;
  pkt[7] = flags;
  sendPacket(0x05, pkt, 8);
}

// ───────────────────────  setup  ─────────────────────────────
void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  delay(400);

  i2s_ok = setupI2S();
  initBands();
  resetCalibration();
  resetDefaults();
  setupSensors();

  // bit0 = i2s_ok, bit1 reserved (was tof), bit2 = acc_ok
  uint8_t hello[1] = { (uint8_t)((i2s_ok ? 1 : 0) | (acc_ok ? 4 : 0)) };
  sendPacket(0xFF, hello, 1);
  sendSettings();
}

// ───────────────────────  loop  ──────────────────────────────
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

  // motion (~20 Hz) — also samples raw between packets to catch impact spikes
  if (acc_ok && now - last_imu_ms >= IMU_PERIOD_MS) {
    processMotion();
    last_imu_ms = now;
  } else if (acc_ok) {
    sensors_event_t e;
    accel.getEvent(&e);
    float mag = sqrtf(e.acceleration.x*e.acceleration.x +
                      e.acceleration.y*e.acceleration.y +
                      e.acceleration.z*e.acceleration.z);
    if (mag > 58.8f) ev_impact = true;
  }

  // stats (1 Hz)
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
    // bit0=i2s, bit1=calibrated, bit2=(reserved), bit3=acc_ok
    pkt[15] = (uint8_t)((i2s_ok ? 1 : 0) | (calibrated ? 2 : 0) | (acc_ok ? 8 : 0));
    sendPacket(0x02, pkt, 16);
    last_stats_ms = now;
    notify_count = 0;
    peak_rms = 0;
  }

  // settings echo (0.2 Hz)
  if (now - last_settings_ms >= 5000) {
    sendSettings();
    last_settings_ms = now;
  }
}
