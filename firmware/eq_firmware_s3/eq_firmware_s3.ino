/*
 * Protogen ESP32-S3 firmware — Waveshare ESP32-S3-Touch-LCD-1.46 variant
 *
 * On-board hardware used:
 *   QMI8658    (6-axis IMU)   I²C: SDA→GPIO11, SCL→GPIO10  (board native)
 *   PDM MEMS microphone       PDM_CLK→see below, PDM_DAT→see below
 *
 * ⚠️ THE MIC PINS BELOW ARE BEST GUESSES — verify against your board's
 *    schematic. If FFT bars stay zero, the pins are wrong. Common Waveshare
 *    PDM mic configurations:
 *      ESP32-S3-LCD-1.85 / 2.1: CLK=42, DAT=41
 *      Some 1.46 revisions    : CLK=38, DAT=39  (may conflict with µSD on some)
 *      Others                 : CLK=2,  DAT=15
 *    Pick the values that match the silkscreen / wiki of YOUR board.
 *
 * The LCD, touch, RTC, microSD on the Waveshare board are NOT used —
 * this device acts as a pure sensor MCU; everything renders on the Pi's
 * HUB75 panel.  The LCD stays blank.
 *
 * Arduino IDE / arduino-cli libraries required:
 *   arduinoFFT  (kosme)
 *   (QMI8658 uses an inline register-based driver — no library needed)
 *
 * Board settings (Arduino IDE 2):
 *   Board:               ESP32S3 Dev Module
 *   USB CDC On Boot:     ENABLED  ← critical
 *   USB Mode:            Hardware CDC and JTAG
 *   CPU Frequency:       240 MHz
 *   Flash Size:          16 MB (128Mb)
 *   PSRAM:               OPI PSRAM
 *   Partition Scheme:    Default 4MB with spiffs
 *
 * USB-CDC binary protocol — frame: [0xAA][0x55][type][len][payload][xor]
 *   0x01  16 B  EQ magnitudes per band
 *   0x02  16 B  stats (uptime, heap, notify rate, sample rate, peak, flags)
 *   0x03   8 B  current settings echo
 *   0x05   8 B  motion: i16 ax_mg, i16 ay_mg, i16 az_mg, u8 tilt°, u8 flags
 *   0xFF   1 B  boot signal: bit0=i2s_ok (mic), bit1=0, bit2=acc_ok
 *
 * Stats flags (packet 0x02 byte 15): bit0=i2s, bit1=cal, bit3=acc_ok
 * Motion flags (packet 0x05 byte 7): bit0=shake bit1=impact bit2=freefall bit3=flat
 *
 * Text commands (terminated by \n): M<int> G<int> A<int> S<int> C R ?
 */

#include <Arduino.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <arduinoFFT.h>
#include <math.h>

// ──────────────────────────── pins ────────────────────────────
// ⚠️ MIC pins — verify against your board's schematic and adjust!
#define PDM_CLK_PIN  42       // PDM clock OUT  (most common: 42 or 38)
#define PDM_DAT_PIN  41       // PDM data  IN   (most common: 41 or 39)

#define I2C_SDA      11       // Waveshare board native I²C — DO NOT change
#define I2C_SCL      10

#define LED_PIN      -1       // no convenient onboard status LED — disabled

// ──────────────────────────── audio ───────────────────────────
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
int16_t  i2s_buf[SAMPLES];               // 16-bit samples from PDM hardware

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

// ──────────────────────── QMI8658 driver ──────────────────────
// Minimal inline register-based driver — no fragile library deps.
#define QMI8658_REG_WHOAMI   0x00
#define QMI8658_REG_CTRL1    0x02
#define QMI8658_REG_CTRL2    0x03
#define QMI8658_REG_CTRL7    0x08
#define QMI8658_REG_AX_L     0x35

uint8_t qmi_addr = 0x6B;
bool acc_ok = false;
uint32_t last_imu_ms = 0;
const uint32_t IMU_PERIOD_MS = 50;

#define SHAKE_N 8
float    shake_win[SHAKE_N] = {0};
uint8_t  shake_idx = 0;
float    freefall_t = 0.0f;
float    flat_t     = 0.0f;
uint32_t last_imu_us = 0;
volatile bool ev_impact = false;

static bool qmi_write(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(qmi_addr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool qmi_read(uint8_t reg, uint8_t *buf, size_t n) {
  Wire.beginTransmission(qmi_addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  uint8_t got = Wire.requestFrom((int)qmi_addr, (int)n);
  if (got < n) return false;
  for (size_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

static bool qmi_probe(uint8_t addr) {
  qmi_addr = addr;
  uint8_t id = 0;
  if (!qmi_read(QMI8658_REG_WHOAMI, &id, 1)) return false;
  return id == 0x05;
}

bool qmi_init() {
  if (!qmi_probe(0x6B) && !qmi_probe(0x6A)) return false;
  qmi_write(QMI8658_REG_CTRL1, 0x40);     // address auto-increment
  qmi_write(QMI8658_REG_CTRL2, 0x15);     // accel ±4 g, ODR 125 Hz
  qmi_write(QMI8658_REG_CTRL7, 0x01);     // enable accelerometer
  delay(20);
  return true;
}

bool qmi_read_accel(float *ax, float *ay, float *az) {
  uint8_t b[6];
  if (!qmi_read(QMI8658_REG_AX_L, b, 6)) return false;
  int16_t rax = (int16_t)((b[1] << 8) | b[0]);
  int16_t ray = (int16_t)((b[3] << 8) | b[2]);
  int16_t raz = (int16_t)((b[5] << 8) | b[4]);
  const float scale = 4.0f * 9.81f / 32768.0f;
  *ax = rax * scale;
  *ay = ray * scale;
  *az = raz * scale;
  return true;
}

// ──────────────────────── PDM mic setup ───────────────────────
bool setupPDMmic() {
  i2s_config_t cfg = {
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM),
    .sample_rate          = SAMPLE_RATE,
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,    // ignored in PDM, must be set
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = DMA_BUF_COUNT,
    .dma_buf_len          = DMA_BUF_LEN,
    .use_apll             = false,
    .tx_desc_auto_clear   = false,
    .fixed_mclk           = 0,
  };
  i2s_pin_config_t pins = {
    .mck_io_num   = I2S_PIN_NO_CHANGE,
    .bck_io_num   = I2S_PIN_NO_CHANGE,    // not used in PDM
    .ws_io_num    = PDM_CLK_PIN,           // PDM CLK output
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num  = PDM_DAT_PIN,           // PDM data input
  };
  if (i2s_driver_install(I2S_NUM_0, &cfg, 0, nullptr) != ESP_OK) return false;
  return i2s_set_pin(I2S_NUM_0, &pins) == ESP_OK;
}

// ──────────────────────────── bands ────────────────────────────
void initBands() {
  double lo = log(2.0), hi = log(60.0);
  for (int i = 0; i <= BANDS; i++) {
    band_edges[i] = (int)round(exp(lo + (hi - lo) * i / BANDS));
    if (i > 0 && band_edges[i] <= band_edges[i-1])
      band_edges[i] = band_edges[i-1] + 1;
  }
}

void resetCalibration() {
  for (int i = 0; i < BANDS; i++) { band_noise[i] = 0; band_smoothed[i] = 0; }
  cal_frames = 0;
  calibrated = false;
}

void resetDefaults() {
  noise_margin = DEFAULT_NOISE_MARGIN;
  db_gain      = DEFAULT_DB_GAIN;
  amp_mult     = DEFAULT_AMP;
  smoothing    = DEFAULT_SMOOTHING;
}

// ──────────────────────────── packets ──────────────────────────
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
  pkt[4] = a; pkt[5] = s; pkt[6] = c; pkt[7] = r;
  sendPacket(0x03, pkt, 8);
}

// ──────────────────────────── commands ─────────────────────────
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

// ──────────────────────────── motion ───────────────────────────
void processMotion() {
  if (!acc_ok) return;
  uint32_t now_us = micros();
  float dt = (last_imu_us == 0) ? 0.05f : (now_us - last_imu_us) / 1e6f;
  if (dt > 0.25f) dt = 0.25f;
  last_imu_us = now_us;

  float ax = 0, ay = 0, az = 0;
  if (!qmi_read_accel(&ax, &ay, &az)) return;

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

// ──────────────────────────── setup ────────────────────────────
void setup() {
  Serial.begin(115200);
  if (LED_PIN >= 0) { pinMode(LED_PIN, OUTPUT); digitalWrite(LED_PIN, HIGH); }
  delay(400);

  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(400000);

  i2s_ok = setupPDMmic();
  initBands();
  resetCalibration();
  resetDefaults();
  acc_ok = qmi_init();

  uint8_t hello[1] = { (uint8_t)((i2s_ok ? 1 : 0) | (acc_ok ? 4 : 0)) };
  sendPacket(0xFF, hello, 1);
  sendSettings();
}

// ──────────────────────────── loop ─────────────────────────────
void loop() {
  readSerialCommands();

  if (!i2s_ok) {
    if (LED_PIN >= 0) digitalWrite(LED_PIN, (millis() / 100) & 1);
    delay(50);
    return;
  }

  size_t br = 0;
  if (i2s_read(I2S_NUM_0, i2s_buf, sizeof(i2s_buf), &br, portMAX_DELAY) != ESP_OK) return;
  // PDM mic returns 16-bit samples → 2 bytes each
  if (br / 2 != SAMPLES) return;

  // DC removal
  long sum = 0;
  for (int i = 0; i < SAMPLES; i++) sum += i2s_buf[i];
  int16_t dc = (int16_t)(sum / SAMPLES);

  long long sq = 0;
  for (int i = 0; i < SAMPLES; i++) {
    int32_t s = ((int32_t)i2s_buf[i] - dc) * amp_mult;
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
    for (int k = lo; k < hi && k < SAMPLES / 2; k++) if (vReal[k] > mx) mx = vReal[k];
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
      if (LED_PIN >= 0) {
        for (int k = 0; k < 4; k++) {
          digitalWrite(LED_PIN, LOW); delay(60);
          digitalWrite(LED_PIN, HIGH); delay(60);
        }
      }
      sendSettings();
    }
  }

  sendPacket(0x01, bands_out, BANDS);
  notify_count++;
  notify_count_total++;
  if (LED_PIN >= 0) digitalWrite(LED_PIN, (notify_count_total / 50) & 1 ? LOW : HIGH);

  uint32_t now = millis();

  if (acc_ok && now - last_imu_ms >= IMU_PERIOD_MS) {
    processMotion();
    last_imu_ms = now;
  } else if (acc_ok) {
    float ax, ay, az;
    if (qmi_read_accel(&ax, &ay, &az)) {
      float mag = sqrtf(ax*ax + ay*ay + az*az);
      if (mag > 58.8f) ev_impact = true;
    }
  }

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
    pkt[15] = (uint8_t)((i2s_ok ? 1 : 0) | (calibrated ? 2 : 0) | (acc_ok ? 8 : 0));
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
