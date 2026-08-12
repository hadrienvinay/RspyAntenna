# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Ground Station Monitor — a dashboard for a Raspberry Pi 3 + RTL-SDR Blog V4 SDR receiver
located in Arcachon, France (44.6611°N, 1.1638°W). It monitors and controls RTL-SDR-based
reception of ADS-B (aircraft), NOAA weather satellite APT images, and GNSS (GPS/Galileo),
plus tracks ISS position, satellite passes, and space weather. Single-owner hobby project,
not a library — there is no test suite or build step; comments and UI strings are in French.

## Running

There is no build/lint/test tooling in this repo. This runs directly on the Raspberry Pi
(user `suri`, home `/home/suri/monitor`), not typically on the dev machine.

```bash
# Install deps (on the Pi)
sudo apt install -y rtl-sdr sox gnss-sdr python3-pip
pip3 install psutil websockets --break-system-packages

# Run the main dashboard server directly (foreground, for debugging)
python3 monitor.py
# → dashboard on http://0.0.0.0:8888
# → data WebSocket on ws://0.0.0.0:8765
# → spectrum WebSocket on ws://0.0.0.0:8766
# → live-logs WebSocket on ws://0.0.0.0:8767 (tails `journalctl -u monitor -f`)

# Run as a systemd service (production)
sudo cp monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now monitor
sudo systemctl status monitor
journalctl -u monitor -f          # live logs

# ADS-B web server (separate process, reads dump1090's SBS feed)
python3 server.py                 # → http://0.0.0.0:8080, expects ./public_html

# GNSS-SDR (Galileo/GPS) — mutually exclusive with dump1090/rtl_fm on the one RTL-SDR dongle
sudo systemctl stop dump1090-fa
gnss-sdr --config_file=gnss-sdr-rtlsdr.conf
```

Full install steps (system packages, noaa-apt decoder, dump1090-fa) are in `README.txt` (French).

## Architecture

**Single physical constraint drives the whole design**: there is exactly one RTL-SDR dongle,
so only one of {dump1090 (ADS-B), gnss-sdr (GPS/Galileo), rtl_fm (NOAA recording), rtl_power
(spectrum scan)} can hold it at a time. Every start routine (`dump1090_start`, `gnss_start`,
`record_pass`, `get_spectrum_snapshot`) first checks via `pgrep` that no other consumer is
running (ignoring zombie processes) and refuses to start if the dongle is busy.

**Process model**: `monitor.py` is the single entrypoint and orchestrator. It runs four
concurrent things in one asyncio event loop + threads:
1. An `HTTPServer` (in a background `Thread`) that serves `dashboard.html` and a small
   JSON API (`/api/gnss/*`, `/api/dump1090/*`, `/api/noaa/*`, `/api/spectrum`,
   `/api/system/reboot`) for starting/stopping SDR consumers as subprocesses (`gnss-sdr`,
   `dump1090`, `rtl_fm`+`sox`+`noaa-apt`) and for rebooting the host (`sudo /sbin/reboot` —
   requires a NOPASSWD sudoers rule, see `README.txt` §6).
2. A WebSocket server (port 8765) that pushes a full JSON state snapshot (`collect()`) to all
   connected dashboard clients every `INTERVAL` (2s) — system stats, RTL-SDR/dump1090/GNSS
   status, NOAA and space-tracker state, systemd service states, network info.
3. A second WebSocket server (port 8766) dedicated to spectrum (FFT) data, driven by
   `rtl_power`, parameterized per-client by the frequency range the browser requests.
4. A third WebSocket server (port 8767) that tails `journalctl -u monitor -f` in a background
   thread (`_tail_journal`) and broadcasts each new line to connected clients — backs the
   dashboard's "Logs" tab. Only works when running under systemd (see `monitor.service`).

`noaa_tracker.py` and `space_tracker.py` are self-contained background modules, each started
once via their own `start()` (spawns daemon threads) and each exposing `get_state()` which
`monitor.collect()` polls and merges into the broadcast payload — there is no other coupling
between them and `monitor.py`. Both maintain their own thread-safe global `_state` dict guarded
by a `Lock`, refreshed on independent timers (NOAA scheduler every 60s, TLE refresh every 12h;
ISS position every 5s, satellite passes every 10min, space weather every 15min).

Both trackers implement their own minimal SGP4-like orbital propagator from TLE data (no
external orbital mechanics library) to predict satellite passes over Arcachon — see
`tle_to_state`/`predict_passes` in `noaa_tracker.py` and `propagate`/`predict_passes_tle` in
`space_tracker.py`. These are near-duplicate implementations; if fixing a bug in the orbit math
or pass-prediction logic in one, check whether the same bug exists in the other. TLEs are
fetched from Celestrak and cached to disk (`noaa_tle.txt`, `tle/*.tle`) to survive restarts and
avoid hammering the network (NOAA: 12h cache, space_tracker: 6h cache).

`noaa_tracker.py` additionally drives a record→decode pipeline when a satellite pass is
imminent: `rtl_fm` (FM demod) piped into `sox` (raw→WAV) for the pass duration, then `noaa-apt`
decodes the WAV into a PNG under `images/`. This can also be triggered manually via
`/api/noaa/record`. State machine: `idle → recording → decoding → done`, tracked in
`noaa_tracker._state["status"]`.

`server.py` is an independent process (not started by `monitor.py`) serving the ADS-B web UI
on port 8080. It reads dump1090's raw SBS-1 feed on TCP port 30003 in a background thread
(`sbs_reader`/`_parse_sbs`), maintains an in-memory `aircraft` dict, and exposes it as
`/data/aircraft.json` (aircraft.json/dump1090 wire format) for the frontend at `./public_html`
(not present in this repo — expected to be an existing tar1090/dump1090 static UI). dump1090
itself and this web server are started together by `monitor.dump1090_start()`.

`dashboard.html` is a single self-contained file (no build step, no framework) — vanilla JS,
inline `<style>`, Leaflet.js via CDN for maps. It connects to `ws://<host>:8765` for the main
data feed and calls the `/api/*` POST endpoints on the same origin (port 8888) for control
actions.

**Data flow summary**: browser ⇄ `monitor.py` (8888 HTTP / 8765 WS / 8766 WS spectrum /
8767 WS logs) ⇄ subprocesses (`gnss-sdr`, `dump1090`, `rtl_fm`/`sox`/`noaa-apt`, `rtl_power`,
`journalctl`) and separately browser ⇄ `server.py` (8080) ⇄ dump1090 SBS feed (30003).

## Conventions

- All process launches from Python use `subprocess.Popen`/`subprocess.run` with generous
  `try/except Exception` guards — external tools (rtl_fm, gnss-sdr, noaa-apt, systemctl) are
  expected to be flaky or absent on a given machine, and failures degrade to empty/default
  values rather than raising.
- Global mutable state shared between threads (`_gnss_cache`, `noaa_tracker._state`,
  `space_tracker._state`, `DUMP1090_PROC`) is always guarded by a `Lock` and only ever
  read/written through accessor functions (`get_state`, `_update_state`) — don't touch the
  dict directly from new code.
- Hardware paths (`/sys/class/thermal/...`, `/home/suri/monitor/...`, dump1090 stats.json
  locations) are Raspberry Pi/Debian-specific and hardcoded; they intentionally fail soft off-Pi.
