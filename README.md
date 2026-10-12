# 3D Printer Enclosure Monitor

MicroPython firmware for an M5Stack stick that sits inside (or next to) a 3D-printer
enclosure. It shows temperature and humidity, controls a cooling fan, and talks to a
**Bambu Lab** printer over the LAN so you can pause/resume and toggle the chamber light
from the device, or get alerts on Telegram.

## Features

- Live temperature / humidity from a **BME680** sensor on a colour ST7789 LCD
- Automatic **PWM fan control** (ramps between 28 and 38 °C, with tach reading) plus manual mode
- **Bambu Lab LAN integration** (MQTT over TLS, port 8883): job state, nozzle/bed temps,
  progress, time remaining, pause / resume, chamber light
- Printer auto-discovery (SSDP, then subnet scan, with the IP cached on the device)
- **Auto-pause** the print if the enclosure exceeds `PAUSE_TEMP` (44 °C)
- **Telegram bot** alerts and command polling
- Buzzer when temperature exceeds `BUZZ_TEMP` (40 °C)
- Hardware watchdog and auto-restart/reset on crash

## Files

| File | Purpose |
|------|---------|
| `env_display_s3.py` | Main firmware for **M5StickS3** (ESP32-S3, M5PM1 power chip) - current version |
| `env_display.py` | Original firmware for the older **M5StickC-style** board (ESP32, AXP192) |
| `main_s3.py`, `main.py` | Boot loaders: run the firmware, print exceptions, reset on failure |
| `monitor.py` | PC-side serial logger: counts crashes/brownouts and watches free RAM for leaks over 3 h |
| `config.example.py` | Template for your credentials |

## Setup

1. Flash MicroPython onto the device.
2. Copy `config.example.py` to `config.py` and fill in:
   - Wi-Fi SSID/password
   - Telegram bot token (from [@BotFather](https://t.me/BotFather)) and your chat ID
   - Printer LAN access code and serial number (enable **LAN mode** on the printer)
3. Upload to the device (e.g. with `mpremote` or `ampy`): the firmware file, `config.py`,
   and the boot loader. The loaders import a module named `env_display`, so upload
   `env_display_s3.py` as `env_display.py` (or edit the loader), and the loader as `main.py`.

```sh
mpremote cp config.py :config.py
mpremote cp env_display_s3.py :env_display.py
mpremote cp main_s3.py :main.py
```

`config.py` is gitignored - never commit it.

## Controls (S3 build)

- **Button A, short press**: pause / resume the print
- **Button A, long press**: open the menu (fan mode, fan +/-10 %, Wi-Fi, find printer, backlight, reset)
- **Button B, short press**: toggle the printer chamber light
- **Button B, long press**: toggle the device backlight

## Pins (M5StickS3 build)

| Function | GPIO |
|----------|------|
| BME680 I2C (SDA / SCL) | 9 / 10 |
| M5PM1 I2C (SDA / SCL) | 47 / 48 |
| Buttons A / B | 11 / 12 |
| Fan PWM / tach | 5 / 6 |
| LCD SPI (SCK / MOSI / CS / DC / RST) | 40 / 39 / 41 / 45 / 21 |

Notes:
- LCD power on the S3 is controlled by M5PM1 `gpio2` (reg `0x11`, bit 2).
- Be careful wiring a 12 V fan near the stick: wiring mistakes can destroy GPIO pins.
  Use a transistor/MOSFET driver and a flyback diode, and keep fan power off the logic pins.

## Monitoring

`monitor.py` (needs `pyserial`) reads `/dev/ttyUSB0` at 115200 baud without toggling
DTR/RTS (so it won't reset the board) and prints periodic health reports. Adjust `PORT`.

## Disclaimer

Automatically pausing or controlling a printer is a convenience, not a safety system.
Do not rely on this project to prevent fires or damage.
