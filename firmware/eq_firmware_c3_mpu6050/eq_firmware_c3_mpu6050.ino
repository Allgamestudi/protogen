/*
 * Protogen ESP32-C3 firmware — INMP441 mic + MPU6050 IMU variant.
 *
 * Hardware (C3 Super Mini):
 *   INMP441   (I²S microphone)   SCK→GPIO4   WS→GPIO5   SD→GPIO6   L/R→GND
 *   MPU6050   (I²C IMU, GY-521)  VCC→3V3  GND→GND  SDA→GPIO8  SCL→GPIO9
 *                                AD0 floating → address 0x68
 *   VL53L1X   (I²C ToF, 400C)    VCC→3V3  GND→GND  SDA→GPIO8  SCL→GPIO9
 *                                XSHUT,INT floating → address 0x29
 *   Status LED                   GPIO10 (built-in LED on GPIO8 is now I²C SDA)
 *   WS2812 left ear ring  (13 LEDs)   DIN→GPIO2    VCC→external 5V    GND→common
 *   WS2812 right ear ring (13 LEDs)   DIN→GPIO3    VCC→external 5V    GND→common
 *
 *   ⚠️ WS2812 power: do NOT power from C3's 3V3 rail — at 60 mA per LED full
 *      white, the 26-LED total draws up to 1.5 A. Use an external 5 V / 2-3 A
 *      PSU. Tie that PSU's GND to C3's GND (data signal needs common ground).
 *
 *      WS2812 data threshold is ~3.5 V — C3's 3.3 V output works on short
 *      wires (<30 cm), but if you see colour glitches add a 1N4148 diode in
 *      series with the +5 V of the FIRST LED to drop its VCC to ~4.4 V (then
 *      3.3 V data clears its threshold cleanly).
 *
 * Arduino IDE / arduino-cli libraries required:
 *   arduinoFFT                  (kosme)
 *   Adafruit NeoPixel           (for the ear rings — kept on NeoPixel because
 *                                FastLED 3.7+ initialises the new I²S driver
 *                                which conflicts with our legacy I²S mic)
 *   (MPU6050 uses an inline register-based driver — no library needed)
 *
 * Board settings in Arduino IDE 2:
 *   Board:                ESP32C3 Dev Module
 *   USB CDC On Boot:      ENABLED   ← MUST
 *   USB Mode:             Hardware CDC and JTAG
 *   CPU Frequency:        160 MHz
 *   Flash Size:           4 MB
 *   Partition Scheme:     Default 4MB with spiffs
 *
 * USB-CDC binary protocol — frame: [0xAA][0x55][type][len][payload][xor]
 *   ESP → Pi:
 *   0x01  16 B  EQ magnitudes per band
 *   0x02  16 B  stats (uptime, heap, notify rate, sample rate, peak, flags)
 *   0x03   8 B  current settings echo
 *   0x04   4 B  distance: u16 mm, u8 valid, u8 reserved  (VL53L1X)
 *   0x05  14 B  motion: i16 ax_mg, ay_mg, az_mg, gx_dps, gy_dps, gz_dps, u8 tilt°, u8 flags
 *                (gyro values are integer deg/s; pi handler accepts both 8 B and 14 B)
 *   0xFF   1 B  boot signal: bit0=i2s_ok, bit1=tof_ok, bit2=acc_ok
 *   Pi → ESP:
 *   0x06   1 + 3·N B  ear colours: [side(0=L,1=R)] [R G B] × N (N ≤ 13)
 *
 * Stats flags (packet 0x02 byte 15): bit0=i2s, bit1=cal, bit3=acc_ok
 * Motion flags (packet 0x05 byte 7): bit0=shake bit1=impact bit2=freefall bit3=flat
 *
 * Text commands (terminated by \n):
 *   M<int> G<int> A<int> S<int>  — mic settings (margin, gain, amp, smoothing)
 *   C                             — recalibrate mic noise floor
 *   R                             — reset mic settings to defaults
 *   ?                             — request settings echo
 *   B                             — run ear-ring boot animation (self-test +
 *                                   spinner + ready flash). Runs in setup() ONLY
 *                                   when commanded — gives Pi control of timing.
 *   O                             — turn ear rings off
 *
 * Binary commands Pi→ESP — same framing as ESP→Pi:
 *   AA 55 type len payload[len] xor
 *
 *   type=0x10  len=78  payload = 13 RGB bytes for left ear, then 13 RGB for right.
 *                      ESP applies and shows immediately. ~60-100 Hz capable.
 */

#include <Arduino.h>
#include <Wire.h>
#include <driver/i2s.h>
#include <arduinoFFT.h>
#include <Adafruit_NeoPixel.h>
#include "Adafruit_VL53L1X.h"
#include <math.h>

// ─────────────────────────  DEBUG MODE ─────────────────────────
// Set to 1 to make USB-CDC carry human-readable monitoring output for the
// Arduino IDE Serial Monitor instead of the binary packets.  The Pi will
// not understand this mode — switch back to 0 before deploying.
#define DEBUG_MODE  0

#if DEBUG_MODE
  #define DBG(...)    Serial.printf(__VA_ARGS__)
  #define DBGLN(s)    Serial.println(s)
  #define BIN_TX(call)  ((void)0)        // suppress binary frames
#else
  #define DBG(...)    ((void)0)
  #define DBGLN(s)    ((void)0)
  #define BIN_TX(call)  (call)
#endif

// ──────────────────────────── pins ────────────────────────────
#define I2S_BCK    4         // INMP441 SCK
#define I2S_WS     5         // INMP441 WS  (LRCLK)
#define I2S_DIN    6         // INMP441 SD
#define LED_PIN    10        // simple status LED (optional)

#define I2C_SDA    8
#define I2C_SCL    9

// ──────────────────────────── ear rings ────────────────────────
#define EAR_L_PIN       2
#define EAR_R_PIN       3
#define EAR_COUNT       13       // LEDs per ear
#define EAR_BRIGHT_BOOT 80       // 0..255, kept low during boot to spare current

Adafruit_NeoPixel earL(EAR_COUNT, EAR_L_PIN, NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel earR(EAR_COUNT, EAR_R_PIN, NEO_GRB + NEO_KHZ800);

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
int32_t  i2s_buf[SAMPLES];               // INMP441 = 32-bit I²S samples

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

// ──────────────────────── MPU6050 driver ──────────────────────
// Inline register-based driver — no library dependency.
#define MPU_REG_SMPRT_DIV    0x19
#define MPU_REG_CONFIG       0x1A
#define MPU_REG_GYRO_CFG     0x1B
#define MPU_REG_ACCEL_CFG    0x1C
#define MPU_REG_ACCEL_XOUT_H 0x3B
#define MPU_REG_PWR_MGMT_1   0x6B
#define MPU_REG_WHOAMI       0x75

uint8_t mpu_addr = 0x68;
bool acc_ok = false;
uint32_t last_imu_ms = 0;
const uint32_t IMU_PERIOD_MS = 50;

// ──────────────────────── VL53L1X ─────────────────────────────
Adafruit_VL53L1X tof = Adafruit_VL53L1X();
bool tof_ok = false;
uint32_t last_tof_ms = 0;
const uint32_t TOF_PERIOD_MS = 80;     // ~12 Hz, plenty for proximity sensing

// ──────────────────────── DEBUG-only globals ─────────────────
// Need forward declarations so functions defined above setup() can store
// the most-recent sensor sample for the 1 Hz monitoring dump.
#if DEBUG_MODE
static uint32_t dbg_last_print_ms = 0;
static int16_t  dbg_last_dist_mm  = -1;
static float    dbg_last_ax = 0, dbg_last_ay = 0, dbg_last_az = 0;
static float    dbg_last_gx = 0, dbg_last_gy = 0, dbg_last_gz = 0;
static uint16_t dbg_peak_bands  = 0;
#endif

// Accelerometer scale at ±4 g: 8192 LSB/g  →  m/s² per LSB
const float MPU_ACC_SCALE = 4.0f * 9.81f / 32768.0f;
// Gyro scale at ±500 dps: 65.5 LSB/dps  →  dps per LSB
const float MPU_GYRO_SCALE = 500.0f / 32768.0f;

#define SHAKE_N 8
float    shake_win[SHAKE_N] = {0};
uint8_t  shake_idx = 0;
float    freefall_t = 0.0f;
float    flat_t     = 0.0f;
uint32_t last_imu_us = 0;
volatile bool ev_impact = false;

static bool mpu_write(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(mpu_addr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool mpu_read(uint8_t reg, uint8_t *buf, size_t n) {
  Wire.beginTransmission(mpu_addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  uint8_t got = Wire.requestFrom((int)mpu_addr, (int)n);
  if (got < n) return false;
  for (size_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

static bool mpu_probe(uint8_t addr) {
  mpu_addr = addr;
  uint8_t id = 0;
  if (!mpu_read(MPU_REG_WHOAMI, &id, 1)) return false;
  // MPU6050 → 0x68, MPU6500/9250 clones often → 0x70..0x73
  return id == 0x68 || id == 0x70 || id == 0x71 || id == 0x72 || id == 0x73 || id == 0x98;
}

bool mpu_init() {
  if (!mpu_probe(0x68) && !mpu_probe(0x69)) return false;
  mpu_write(MPU_REG_PWR_MGMT_1, 0x00);   // wake (clear sleep), clock=internal 8 MHz
  delay(10);
  mpu_write(MPU_REG_PWR_MGMT_1, 0x01);   // clock=PLL from gyro X (more stable)
  mpu_write(MPU_REG_SMPRT_DIV,  0x09);   // sample rate = 1 kHz / (1+9) = 100 Hz
  mpu_write(MPU_REG_CONFIG,     0x03);   // DLPF ~44 Hz
  mpu_write(MPU_REG_ACCEL_CFG,  0x08);   // ±4 g  →  8192 LSB/g
  mpu_write(MPU_REG_GYRO_CFG,   0x08);   // ±500 °/s →  65.5 LSB/dps
  delay(20);
  return true;
}

bool mpu_read_accel(float *ax, float *ay, float *az) {
  uint8_t b[6];
  if (!mpu_read(MPU_REG_ACCEL_XOUT_H, b, 6)) return false;
  // MPU6050 is big-endian: high byte first
  int16_t rax = (int16_t)((b[0] << 8) | b[1]);
  int16_t ray = (int16_t)((b[2] << 8) | b[3]);
  int16_t raz = (int16_t)((b[4] << 8) | b[5]);
  *ax = rax * MPU_ACC_SCALE;
  *ay = ray * MPU_ACC_SCALE;
  *az = raz * MPU_ACC_SCALE;
  return true;
}

// Read accel (6B) + temp (2B) + gyro (6B) in one I²C transaction.
bool mpu_read_all(float *ax, float *ay, float *az,
                  float *gx, float *gy, float *gz) {
  uint8_t b[14];
  if (!mpu_read(MPU_REG_ACCEL_XOUT_H, b, 14)) return false;
  int16_t rax = (int16_t)((b[0]  << 8) | b[1]);
  int16_t ray = (int16_t)((b[2]  << 8) | b[3]);
  int16_t raz = (int16_t)((b[4]  << 8) | b[5]);
  // b[6..7] = temperature, ignored
  int16_t rgx = (int16_t)((b[8]  << 8) | b[9]);
  int16_t rgy = (int16_t)((b[10] << 8) | b[11]);
  int16_t rgz = (int16_t)((b[12] << 8) | b[13]);
  *ax = rax * MPU_ACC_SCALE;
  *ay = ray * MPU_ACC_SCALE;
  *az = raz * MPU_ACC_SCALE;
  *gx = rgx * MPU_GYRO_SCALE;
  *gy = rgy * MPU_GYRO_SCALE;
  *gz = rgz * MPU_GYRO_SCALE;
  return true;
}

// ──────────────────────── VL53L1X helpers ─────────────────────
// Quick non-blocking I²C ping. Returns true if a device ACKs the address.
static bool i2c_ping(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

bool tof_init() {
  // ── presence check FIRST ──
  // Adafruit_VL53L1X.begin() blocks indefinitely on missing hardware (it
  // polls device-ready status forever). We ping the I²C bus ourselves to
  // catch that case before it can wedge setup().
  if (!i2c_ping(0x29)) {
    return false;       // no sensor on the bus — skip cleanly
  }
  // Some 400C clones use 0x52 (8-bit form). Adafruit library expects 0x29.
  if (!tof.begin(0x29, &Wire)) return false;
  tof.setTimingBudget(50);            // ~10 cm accuracy at 2 m
  if (!tof.startRanging()) return false;
  return true;
}

void readAndSendTof() {
  if (!tof_ok) return;
  if (!tof.dataReady()) return;

  int16_t mm = tof.distance();
  tof.clearInterrupt();

#if DEBUG_MODE
  dbg_last_dist_mm = mm;
#endif

  uint16_t out_mm;
  uint8_t  valid;
  // VL53L1X official minimum range is ~40 mm. Below that, readings become
  // unreliable (noisy / wrapped / fake near-zero values), so we drop them.
  // Above 4000 mm (4 m) the sensor is also out of its long-mode range.
  if (mm < 30 || mm > 4000) {
    out_mm = 0;
    valid  = 0;
  } else {
    out_mm = (uint16_t)mm;
    valid  = 1;
  }
  uint8_t pkt[4];
  memcpy(pkt + 0, &out_mm, 2);
  pkt[2] = valid;
  pkt[3] = 0;       // reserved
  sendPacket(0x04, pkt, 4);
}

// ──────────────────────────── I²S setup ───────────────────────
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

// ──────────────────────────── bands ───────────────────────────
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

// ──────────────────────────── packets ─────────────────────────
static inline uint8_t xor_cs(const uint8_t* p, size_t n) {
  uint8_t c = 0;
  for (size_t i = 0; i < n; i++) c ^= p[i];
  return c;
}

void sendPacket(uint8_t type, const uint8_t* data, uint8_t len) {
#if DEBUG_MODE
  (void)type; (void)data; (void)len;            // swallowed in debug mode
#else
  uint8_t hdr[4] = { 0xAA, 0x55, type, len };
  uint8_t cs = xor_cs(hdr, 4) ^ xor_cs(data, len);
  Serial.write(hdr, 4);
  Serial.write(data, len);
  Serial.write(&cs, 1);
#endif
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

// ──────────────────────────── commands ────────────────────────
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
  else if (c == 'B') {                                  // ear-ring boot animation
    earsBootSelfTest();
    earsBootSpinner(2, 80, 160, 255);
    if (i2s_ok && acc_ok) earsBootReady(40, 200, 60);
    else                  earsBootReady(220, 110, 0);
    return;                                             // don't echo settings for B/O
  }
  else if (c == 'O') { earsClear(); return; }           // ears off
  sendSettings();
}

// ───────────────────────── binary command handler ────────────────────────
// Pi→ESP packet types
#define IN_PKT_EARS  0x10        // 78 B payload — 13 left RGB + 13 right RGB

static void handleBinaryCommand(uint8_t type, const uint8_t* data, uint8_t len) {
  if (type == IN_PKT_EARS && len == EAR_COUNT * 6) {
    for (int i = 0; i < EAR_COUNT; i++) {
      earL.setPixelColor(i, data[i*3], data[i*3 + 1], data[i*3 + 2]);
    }
    for (int i = 0; i < EAR_COUNT; i++) {
      int off = (EAR_COUNT + i) * 3;
      earR.setPixelColor(i, data[off], data[off + 1], data[off + 2]);
    }
    earsShow();
  }
  // future binary commands handled here
}

// State-machine reader: handles text commands AND binary AA-55 frames.
enum InState {
  IN_TEXT,
  IN_BIN_SYNC2,
  IN_BIN_TYPE,
  IN_BIN_LEN,
  IN_BIN_PAYLOAD,
  IN_BIN_XOR,
};
static InState inState = IN_TEXT;
static uint8_t inType, inLen, inIdx, inCalcXor;
static uint8_t inBuf[128];

void readSerialCommands() {
  while (Serial.available()) {
    uint8_t ch = (uint8_t)Serial.read();
    switch (inState) {
      case IN_TEXT:
        if (ch == 0xAA) {
          inState = IN_BIN_SYNC2;
        } else if (ch == '\n' || ch == '\r') {
          if (inputBuf.length() > 0) { processCommand(inputBuf); inputBuf = ""; }
        } else if (inputBuf.length() < 32) {
          inputBuf += (char)ch;
        }
        break;
      case IN_BIN_SYNC2:
        if (ch == 0x55) {
          inState = IN_BIN_TYPE;
          inCalcXor = 0xAA ^ 0x55;
        } else {
          inState = IN_TEXT;
        }
        break;
      case IN_BIN_TYPE:
        inType = ch;
        inCalcXor ^= ch;
        inState = IN_BIN_LEN;
        break;
      case IN_BIN_LEN:
        inLen = ch;
        inCalcXor ^= ch;
        if (inLen > sizeof(inBuf)) { inState = IN_TEXT; break; }
        inIdx = 0;
        inState = (inLen > 0) ? IN_BIN_PAYLOAD : IN_BIN_XOR;
        break;
      case IN_BIN_PAYLOAD:
        inBuf[inIdx++] = ch;
        inCalcXor ^= ch;
        if (inIdx >= inLen) inState = IN_BIN_XOR;
        break;
      case IN_BIN_XOR:
        if (ch == inCalcXor) {
          handleBinaryCommand(inType, inBuf, inLen);
        }
        inState = IN_TEXT;
        break;
    }
  }
}

// ──────────────────────────── motion ──────────────────────────
void processMotion() {
  if (!acc_ok) return;
  uint32_t now_us = micros();
  float dt = (last_imu_us == 0) ? 0.05f : (now_us - last_imu_us) / 1e6f;
  if (dt > 0.25f) dt = 0.25f;
  last_imu_us = now_us;

  float ax = 0, ay = 0, az = 0, gx = 0, gy = 0, gz = 0;
  if (!mpu_read_all(&ax, &ay, &az, &gx, &gy, &gz)) return;

#if DEBUG_MODE
  dbg_last_ax = ax; dbg_last_ay = ay; dbg_last_az = az;
  dbg_last_gx = gx; dbg_last_gy = gy; dbg_last_gz = gz;
#endif

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

  // Gyro: clamp to int16 deg/s range
  int16_t gx_dps = (int16_t)constrain((int)gx, -32760, 32760);
  int16_t gy_dps = (int16_t)constrain((int)gy, -32760, 32760);
  int16_t gz_dps = (int16_t)constrain((int)gz, -32760, 32760);

  uint8_t pkt[14];
  memcpy(pkt + 0,  &ax_mg, 2);
  memcpy(pkt + 2,  &ay_mg, 2);
  memcpy(pkt + 4,  &az_mg, 2);
  memcpy(pkt + 6,  &gx_dps, 2);
  memcpy(pkt + 8,  &gy_dps, 2);
  memcpy(pkt + 10, &gz_dps, 2);
  pkt[12] = (uint8_t)tilt;
  pkt[13] = flags;
  sendPacket(0x05, pkt, 14);
}

// ──────────────────────────── ear ring animations ─────────────
static inline void earsShow() { earL.show(); earR.show(); }

static void earsClear() {
  earL.clear(); earR.clear();
  earsShow();
}

// Apply EAR_BRIGHT_BOOT to a raw 0..255 channel — saves repetitive scaling.
static inline uint8_t scaled(uint8_t v) {
  return (uint16_t)v * EAR_BRIGHT_BOOT / 255;
}

// Phase 1: light each LED in sequence (R, then G, then B passes) to verify
// every pixel is wired and the colour channels work.  ~1.0 s total.
static void earsBootSelfTest() {
  const uint8_t channels[3][3] = {
    {255, 0,   0},
    {0,   255, 0},
    {0,   0,   255},
  };
  for (uint8_t ch = 0; ch < 3; ch++) {
    earL.clear(); earR.clear(); earsShow(); delay(40);
    uint32_t c = earL.Color(scaled(channels[ch][0]),
                            scaled(channels[ch][1]),
                            scaled(channels[ch][2]));
    for (int i = 0; i < EAR_COUNT; i++) {
      earL.setPixelColor(i, c);
      earR.setPixelColor(EAR_COUNT - 1 - i, c);   // mirror sweep on right ear
      earsShow();
      delay(28);
    }
  }
  earsClear();
  delay(40);
}

// Phase 2: spinner — a bright "comet" rotates around each ring, leaving a
// fading trail.  Right ear spins the opposite way for visual symmetry.
static void earsBootSpinner(uint8_t cycles, uint8_t r, uint8_t g, uint8_t b) {
  const int TRAIL = 5;
  uint8_t br = scaled(r), bg = scaled(g), bb = scaled(b);
  for (uint8_t c = 0; c < cycles; c++) {
    for (int head = 0; head < EAR_COUNT; head++) {
      for (int i = 0; i < EAR_COUNT; i++) {
        int dist = (head - i + EAR_COUNT) % EAR_COUNT;
        uint8_t v = (dist < TRAIL) ? (255 >> (dist + 1)) : 0;
        uint8_t pr = (uint16_t)br * v / 255;
        uint8_t pg = (uint16_t)bg * v / 255;
        uint8_t pb = (uint16_t)bb * v / 255;
        earL.setPixelColor(i, pr, pg, pb);
        earR.setPixelColor((EAR_COUNT - 1 - i) % EAR_COUNT, pr, pg, pb);
      }
      earsShow();
      delay(38);
    }
  }
}

// Phase 3: brief "ready" flash — full rings, then a smooth fade out.
static void earsBootReady(uint8_t r, uint8_t g, uint8_t b) {
  uint8_t br = scaled(r), bg = scaled(g), bb = scaled(b);
  for (int i = 0; i < EAR_COUNT; i++) {
    earL.setPixelColor(i, br, bg, bb);
    earR.setPixelColor(i, br, bg, bb);
  }
  earsShow();
  delay(160);
  for (int step = 0; step < 18; step++) {
    uint16_t k = 255 - step * 14;
    for (int i = 0; i < EAR_COUNT; i++) {
      earL.setPixelColor(i, (uint16_t)br * k / 255,
                            (uint16_t)bg * k / 255,
                            (uint16_t)bb * k / 255);
      earR.setPixelColor(i, (uint16_t)br * k / 255,
                            (uint16_t)bg * k / 255,
                            (uint16_t)bb * k / 255);
    }
    earsShow();
    delay(22);
  }
  earsClear();
}

// ──────────────────────────── setup ───────────────────────────
void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);
  delay(400);

#if DEBUG_MODE
  delay(800);  // give Serial Monitor time to connect after reset
  Serial.println();
  Serial.println("════════════════════════════════════════════════════");
  Serial.println("  Protogen ESP32-C3  —  DEBUG / monitoring mode");
  Serial.println("════════════════════════════════════════════════════");
#endif

  // ── ear rings: init hardware but leave dark until Pi sends 'B' ──
  earL.begin();   earR.begin();
  earsClear();
  DBGLN("[init] NeoPixel ear rings ready");

  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(400000);
  DBGLN("[init] I2C 400 kHz on SDA=8 SCL=9");

#if DEBUG_MODE
  // I²C scan — list every device on the bus
  Serial.println("[init] scanning I2C bus...");
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      const char *name = "?";
      if (a == 0x29) name = "VL53L1X (ToF)";
      else if (a == 0x3C || a == 0x3D) name = "OLED SSD1306";
      else if (a == 0x68 || a == 0x69) name = "MPU6050 (IMU)";
      Serial.printf("  found 0x%02X — %s\n", a, name);
    }
  }
#endif

  i2s_ok = setupI2S();
  DBG("[init] I2S microphone (INMP441): %s\n", i2s_ok ? "OK" : "FAIL");

  initBands();
  resetCalibration();
  resetDefaults();

  acc_ok = mpu_init();
  DBG("[init] MPU6050 IMU:               %s (addr 0x%02X)\n",
      acc_ok ? "OK" : "FAIL", mpu_addr);

  tof_ok = tof_init();
  DBG("[init] VL53L1X ToF rangefinder:   %s\n", tof_ok ? "OK" : "FAIL");

#if DEBUG_MODE
  Serial.println("════════════════════════════════════════════════════");
  Serial.println("  Streaming live sensor readings (1 Hz summary)");
  Serial.println("════════════════════════════════════════════════════");
#endif

  uint8_t hello[1] = { (uint8_t)((i2s_ok ? 1 : 0)   |
                                 (tof_ok ? 2 : 0)   |
                                 (acc_ok ? 4 : 0)) };
  sendPacket(0xFF, hello, 1);
  sendSettings();
}

// (DEBUG-only globals are declared at top of file so the functions defined
// above setup() can reference them. Don't redeclare here.)

// ──────────────────────────── loop ────────────────────────────
void loop() {
  // Process serial first — keeps response latency low even when audio stalls.
  readSerialCommands();
  // Yield to USB-CDC / WDT housekeeping
  yield();

  if (!i2s_ok) {
    digitalWrite(LED_PIN, (millis() / 100) & 1);
    // Still process incoming binary ear-ring packets if mic failed
    processMotion();
    delay(20);
    return;
  }

  size_t br = 0;
  // Bounded wait — 30 ms is plenty at 16 kHz, 128 samples = 8 ms period.
  // portMAX_DELAY was dangerous: if I²S DMA stalls, the chip locks forever
  // and USB-CDC eventually disconnects (host watchdog).
  if (i2s_read(I2S_NUM_0, i2s_buf, sizeof(i2s_buf), &br,
               30 / portTICK_PERIOD_MS) != ESP_OK || br == 0) {
    // No audio this tick — still serve other tasks so we don't starve them.
    if (acc_ok && millis() - last_imu_ms >= IMU_PERIOD_MS) {
      processMotion();
      last_imu_ms = millis();
    }
    readSerialCommands();
    return;
  }
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

  // Motion update at IMU_PERIOD_MS (50 ms → 20 Hz). Skipping the extra
  // per-loop "impact watchdog" — it added another I²C transaction every
  // audio tick (≈60-100 per second), starving the USB-CDC TX queue and
  // contributing to the brownout-like resets we were seeing.
  if (acc_ok && now - last_imu_ms >= IMU_PERIOD_MS) {
    processMotion();
    last_imu_ms = now;
  }

  if (tof_ok && now - last_tof_ms >= TOF_PERIOD_MS) {
    readAndSendTof();
    last_tof_ms = now;
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
    pkt[15] = (uint8_t)((i2s_ok ? 1 : 0) | (calibrated ? 2 : 0) |
                       (tof_ok ? 4 : 0) | (acc_ok ? 8 : 0));
    sendPacket(0x02, pkt, 16);

#if DEBUG_MODE
    // ── Human-readable monitoring dump, once per second ──
    Serial.printf("\n─── t=%5.1fs  heap=%lu  notifyHz=%u  cal=%c ────────────\n",
                  now / 1000.0f,
                  (unsigned long)heap,
                  (unsigned)ncnt,
                  calibrated ? '✓' : '·');

    // Audio: peak RMS + top 8 bands as bar chart
    Serial.printf("  MIC  peak=%-5d  bands=", (int)pk);
    for (int b = 0; b < BANDS; b++) {
      uint8_t v = bands_out[b];
      char c = (v < 32) ? '.' : (v < 96) ? '-' : (v < 160) ? '=' : (v < 224) ? '#' : '█';
      Serial.print(c);
    }
    Serial.println();

    // IMU
    if (acc_ok) {
      float mag = sqrtf(dbg_last_ax*dbg_last_ax + dbg_last_ay*dbg_last_ay + dbg_last_az*dbg_last_az);
      Serial.printf("  IMU  a=(%+6.2f %+6.2f %+6.2f) m/s²  |a|=%.2fg  g=(%+6.1f %+6.1f %+6.1f) °/s\n",
                    dbg_last_ax, dbg_last_ay, dbg_last_az, mag / 9.81f,
                    dbg_last_gx, dbg_last_gy, dbg_last_gz);
    } else {
      Serial.println("  IMU  (offline)");
    }

    // ToF
    if (tof_ok) {
      if (dbg_last_dist_mm < 0) {
        Serial.println("  ToF  out of range / no echo");
      } else {
        const char *zone = "far";
        if (dbg_last_dist_mm < 100)       zone = "NEAR";
        else if (dbg_last_dist_mm < 400)  zone = "close";
        else if (dbg_last_dist_mm < 1500) zone = "mid";
        Serial.printf("  ToF  %4d mm  [%s]\n", dbg_last_dist_mm, zone);
      }
    } else {
      Serial.println("  ToF  (offline)");
    }
#endif

    last_stats_ms = now;
    notify_count = 0;
    peak_rms = 0;
  }

  if (now - last_settings_ms >= 5000) {
    sendSettings();
    last_settings_ms = now;
  }
}
