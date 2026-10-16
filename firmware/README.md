# ESP32-C3 firmware

I²S microphone sampling + FFT + USB-CDC packet stream.

## Build & flash

### Via VS Code (recommended)

1. Install **PlatformIO IDE** extension in VS Code
2. `File → Open Folder` → select this `firmware/` directory
3. Click the **✓** icon (Build) in the PlatformIO toolbar at the bottom
4. Click the **→** icon (Upload) — flashes via USB
5. Click the **🔌** icon (Serial Monitor) — see live ESP logs

### Via arduino-cli (alternative)

```bash
arduino-cli compile --fqbn esp32:esp32:esp32c3 \
  --build-property "build.cdc_on_boot=1" src/
arduino-cli upload -p /dev/ttyACM0 --fqbn esp32:esp32:esp32c3 src/
```

## Settings to verify

- Board: ESP32C3 Dev Module / esp32-c3-devkitm-1
- USB CDC On Boot: **Enabled** (set via `ARDUINO_USB_CDC_ON_BOOT=1` build flag)
- USB Mode: Hardware CDC and JTAG
- Flash Size: 4MB
- CPU Frequency: 160 MHz

## Wiring

See `../docs/wiring.md`.

## Recovery (if firmware bricks USB)

1. Disconnect USB
2. Hold the **BOOT** button on the ESP
3. Plug USB while holding BOOT
4. Wait 3 seconds, release BOOT
5. Re-upload firmware

If even that fails, short GPIO9 to GND while plugging USB.

## Protocol

See `../docs/api.md`.
