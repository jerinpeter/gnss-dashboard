# RTK Base Station Monitor

A real-time web dashboard and support toolchain for a u-blox ZED-F9P RTK base
station on a Raspberry Pi — built for a setup feeding RTCM corrections to a
rover's own ZED-F9P (which does all RTK fusion on-chip). Runs entirely as a
background service and is viewed from any browser on the local network.

![status](https://img.shields.io/badge/status-active-brightgreen)

## What this does

- **Live dashboard** — fix status, sky plot, per-satellite signal strength,
  survey-in progress, RTCM3 correction output, receiver RF health (per-band
  jamming/AGC, live spectrum), internal chip resources (CPU/memory/firmware
  log counters), navigation configuration, station map, and a scrolling
  message console — all updating several times a second over a WebSocket.
- **Raw observation logging** — continuously captures `RXM-RAWX` at a precise,
  clock-aligned 30-second interval (not polled — the receiver auto-streams it
  off its own hardware clock, immune to software/serial timing jitter), ready
  to convert to RINEX for post-processing.
- **OPUS / PPP workflow** — converts the raw log to RINEX (via RTKLIB's
  `convbin`), validates it (interval, grid alignment, clock-jump detection),
  and prepares it for submission to [NOAA's OPUS](https://geodesy.noaa.gov/OPUS/),
  which computes a precise (cm-level) absolute position using the CORS
  network and precise satellite orbits — far better than the receiver's own
  survey-in self-convergence.
- **Fixed-mode lock-in** — once a precise OPUS coordinate is confirmed (ideally
  cross-validated across multiple independent sessions), switches the
  receiver from Survey-In to Fixed mode at that exact position, so it stops
  re-estimating its own location on every power cycle.
- **RTCM-over-WiFi relay** — replaces an XBee radio link with a WiFi path: the
  Pi taps the RTCM3 stream it's already reading (zero impact on the
  receiver's other output ports) and relays it over TCP to a small bridge
  process on the rover's companion computer, which writes it straight into
  the rover board's correction-input UART.

Everything the dashboard/logging does is **read-only polling** against the
receiver — it never issues a config `SET` except for the two deliberate,
explicitly-confirmed actions described below, and both are scoped to avoid
touching the base station's actual job of streaming corrections.

## Hardware

```
┌─────────────────┐  USB   ┌──────────────┐  RTCM3 (UART2 / WiFi)  ┌──────────────────┐
│ u-blox ZED-F9P   ├────────┤ Raspberry Pi │────────────────────────┤ Rover ZED-F9P     │
│ (base station)   │        │ (this repo)  │                        │ (RTK fusion       │
└─────────────────┘        └──────────────┘                        │  on-chip)         │
                                                                     └─────────┬─────────┘
                                                                               │ USB (position)
                                                                     ┌─────────▼─────────┐
                                                                     │ Companion computer │
                                                                     │ (ROS2 / robot stack)│
                                                                     └────────────────────┘
```

- Base station module connected to the Pi over USB only — the dashboard reads
  everything (UBX, NMEA, RTCM3) off this single connection.
- RTCM3 corrections reach the rover either via a UART-connected radio link
  (e.g. XBee) or via the WiFi relay in this repo, which delivers the exact
  same bytes over the network instead.

## Repository layout

```
server.py              Flask + WebSocket backend: serial reader, UBX/RTCM3
                        parsing, raw logger, RTCM relay server, REST/WS API
static/                Dashboard frontend (HTML/CSS/JS, no build step)
rtcm_bridge_thor.py     Rover-side bridge: WiFi → rover's correction-input UART
rtcm-bridge.service     systemd unit for the bridge (runs on the rover's
                        companion computer, not the base station Pi)
requirements.txt        Python dependencies
```

`raw_logs/` and `opus_submissions/` are created at runtime and gitignored —
the former holds large raw observation binaries, the latter holds RINEX
files that encode your station's precise real-world coordinates.

## Setup

### Base station (Raspberry Pi)

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python3 server.py        # or install as a systemd service, see below
```

Open `http://<pi-ip>:5000` from any browser on the same network.

**As a systemd service** (recommended — auto-starts on boot, restarts on
crash):

```ini
# /etc/systemd/system/gnss-dashboard.service
[Unit]
Description=RTK Base Station Monitor Dashboard
After=network.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/path/to/this/repo
ExecStart=/path/to/this/repo/venv/bin/python3 /path/to/this/repo/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now gnss-dashboard.service
```

Needs `convbin` (from RTKLIB) on the `PATH` if you intend to convert raw logs
to RINEX yourself: `sudo apt install rtklib`.

### RTCM-over-WiFi bridge (rover's companion computer)

1. Edit the constants at the top of `rtcm_bridge_thor.py`:
   `PI_HOST` (the base station's IP), `DEVICE` (the serial device wired into
   the rover's correction-input UART), `BAUD` (must match that UART's
   configured baud rate).
2. Copy the script and the systemd unit over, then:

```bash
sudo cp rtcm-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rtcm-bridge.service
```

The relay server on the Pi side listens on port `5015` and needs no separate
setup — it starts automatically with `server.py`.

## The OPUS / PPP workflow

1. Let the raw logger accumulate several hours of data (more is better, up to
   OPUS's 48-hour cap per submission).
2. Convert to RINEX, GPS-only (OPUS's classic engine wants this, not
   multi-GNSS RINEX 3), at a clean 30-second interval:
   ```bash
   convbin -r ubx -od -os -v 2.11 -y R -y E -y J -y S -y C -y I \
     -ts <start> -te <end> \
     -hp <approx_X>/<approx_Y>/<approx_Z> \
     -o output.obs raw_log.ubx
   ```
   The `-hp` approximate ECEF position seeds OPUS's solver (standard
   practice — it doesn't bias the final answer, just helps convergence).
3. Upload the `.obs` file at https://geodesy.noaa.gov/OPUS/ (antenna type
   "NONE" if your antenna isn't in their list, height 0 if there's no
   separate survey mark).
4. Once you have a precise coordinate — ideally confirmed by **two or more
   independent sessions agreeing closely** — switch the receiver to Fixed
   mode with it (`UBX-CFG-TMODE3`, `rcvrMode=2`, using the **ITRF2020**
   column from the OPUS report, not NAD83 — ITRF matches the receiver's
   native reference frame; NAD83 drifts from it over time due to tectonic
   plate motion).

## Lessons learned (the hard way)

A few non-obvious things this project ran into, in case they save you time:

- **OPUS wants plain GPS-only RINEX 2.11**, not multi-GNSS RINEX 3 — its
  classic processing engine rejects the latter outright.
- **OPUS requires the observation interval to be exactly one of
  1/2/3/5/10/15/30 seconds, with each epoch's seconds field landing exactly
  on a matching grid point** (e.g. `:00`/`:30` for a 30s rate) — not just
  "spaced 30s apart starting from an arbitrary phase." *Polling* the receiver
  for raw measurements drifts under software/serial timing jitter and won't
  satisfy this; the fix is to have the receiver **auto-stream** the message
  at a configured rate divisor instead.
- **The receiver's internal timebase is GPS time, not UTC** — GPS time runs a
  fixed ~18 seconds ahead of UTC (no leap seconds). Computing the alignment
  boundary in raw UTC reliably shifts every epoch by that offset.
- **Don't blindly re-toggle the output rate on every process restart.** The
  receiver keeps running regardless of the monitoring software restarting;
  unconditionally re-enabling the rate on every restart only risks
  introducing a fresh misalignment where a working one already existed.
  Check whether it's already enabled and aligned first.
- **A seed position for OPUS (`-hp`) is required** — without it, some OPUS
  processing paths abort with "zero coordinates for initial point."
- **Avoid submitting data from the last several hours** — IGS precise/rapid
  orbit products aren't published yet for very recent dates, which can cause
  processing to fail outright rather than just degrade accuracy. A several-hour
  buffer between the data's end and the submission time avoided this
  consistently in practice.
- **Receiver clock-steering jumps** (a normal, periodic event on u-blox
  receivers) shift every tracked satellite's pseudorange by the same amount
  simultaneously — detectable by cross-checking each pseudorange delta
  against the Doppler-predicted range-rate between epochs, and distinct from
  genuine multipath/noise (which affects satellites independently, not in
  lockstep). OPUS's own format checker is sensitive to these; trimming the
  session to a single jump-free segment resolves it.

## License

Not yet specified.
