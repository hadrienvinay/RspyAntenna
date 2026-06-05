#!/usr/bin/env python3
"""
Flight Monitor — RPi Ground Station Dashboard
Collecte les métriques système, RTL-SDR, dump1090, GNSS-SDR et les diffuse via WebSocket.
Sert aussi le dashboard HTML sur http://0.0.0.0:8888
"""

import asyncio
import json
import math
import os
import re
import socket
import subprocess
import time
from collections import defaultdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Thread, Lock

import psutil
from websockets.server import serve

import noaa_tracker
import space_tracker

WS_PORT      = 8765
WS_SPEC_PORT = 8766   # WebSocket dédié spectre
HTTP_PORT    = 8888
INTERVAL     = 2

clients      = set()
spec_clients = set()   # clients connectés au spectre


# ── Helpers ─────────────────────────────────────────────────────────────

def run(cmd: list[str], timeout=3) -> str:
    try:
        return subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=timeout
        ).decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def read_file(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return ""


# ── Métriques système ────────────────────────────────────────────────────

def get_system() -> dict:
    cpu     = psutil.cpu_percent(interval=None)
    ram     = psutil.virtual_memory()
    disk    = psutil.disk_usage("/")
    uptime  = int(time.time() - psutil.boot_time())
    hours   = uptime // 3600
    minutes = (uptime % 3600) // 60
    seconds = uptime % 60

    # Température CPU RPi
    raw_temp = read_file("/sys/class/thermal/thermal_zone0/temp")
    temp = round(int(raw_temp) / 1000, 1) if raw_temp.isdigit() else None

    # IP locale
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "N/A"

    return {
        "hostname":  socket.gethostname(),
        "ip":        ip,
        "cpu_pct":   cpu,
        "ram_pct":   round(ram.percent, 1),
        "ram_used":  round(ram.used  / 1024**3, 2),
        "ram_total": round(ram.total / 1024**3, 2),
        "disk_pct":  round(disk.percent, 1),
        "disk_used": round(disk.used  / 1024**3, 1),
        "disk_total":round(disk.total / 1024**3, 1),
        "temp_c":    temp,
        "uptime":    f"{hours:02d}:{minutes:02d}:{seconds:02d}",
        "uptime_s":  uptime,
    }


# ── RTL-SDR ─────────────────────────────────────────────────────────────

def get_rtlsdr() -> dict:
    # Détecter via lsusb (Realtek 0bda:2838 ou 0bda:2832)
    lsusb = run(["lsusb"])
    connected = bool(re.search(r"0bda:(2838|2832|2837)", lsusb))

    device_name = "RTL2838UHIDIR"
    if connected:
        # Essayer de lire le nom précis
        match = re.search(r"ID 0bda:\w+ (.+)", lsusb)
        if match:
            device_name = match.group(1).strip()

    # Vérifier si un process utilise le dongle
    in_use = bool(run(["pgrep", "-x", "rtl_fm"]) or
                  run(["pgrep", "-x", "dump1090"]) or
                  run(["pgrep", "-x", "dump1090-fa"]) or
                  run(["pgrep", "-f",  "gnss-sdr"]))

    # Bias-T (V4 uniquement) — chercher dans /sys
    biast = False
    try:
        biast = "1" in read_file("/sys/bus/usb/drivers/rtl2832u/*/power/control")
    except Exception:
        pass

    return {
        "connected":   connected,
        "device":      device_name if connected else "Non détecté",
        "in_use":      in_use,
        "biast":       biast,
        "freq_range":  "25 MHz – 1.7 GHz",
        "chip":        "RTL2838U / R820T2",
    }


# ── dump1090 ─────────────────────────────────────────────────────────────

def get_dump1090() -> dict:
    # Vérifier si le service tourne
    running = False
    service = ""
    for svc in ["dump1090-fa", "dump1090"]:
        status = run(["systemctl", "is-active", svc])
        if status == "active":
            running = True
            service = svc
            break

    # Si pas en service, vérifier le process directement
    if not running:
        pid = run(["pgrep", "-f", "dump1090"])
        running = bool(pid)
        service = "dump1090 (process)"

    planes  = 0
    msgs_30s = 0

    # Lire les stats depuis le fichier JSON de dump1090-fa
    stats_paths = [
        "/run/dump1090-fa/stats.json",
        "/run/dump1090/stats.json",
        "/tmp/dump1090/stats.json",
    ]
    for path in stats_paths:
        raw = read_file(path)
        if raw:
            try:
                data = json.loads(raw)
                last = data.get("last1min", data.get("total", {}))
                msgs_30s = last.get("messages", 0)
                planes   = data.get("aircraft_with_pos", 0)
            except Exception:
                pass
            break

    # Fallback : compter via le port SBS (connexion rapide)
    if running and planes == 0:
        try:
            import socket as _s
            sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
            sock.settimeout(0.5)
            sock.connect(("127.0.0.1", 30003))
            data = sock.recv(4096).decode("utf-8", errors="ignore")
            sock.close()
            planes = len(set(
                line.split(",")[4]
                for line in data.strip().split("\n")
                if len(line.split(",")) > 4 and line.split(",")[4]
            ))
        except Exception:
            pass

    return {
        "running":    running,
        "service":    service,
        "planes":     planes,
        "msgs_30s":   msgs_30s,
        "port_http":  8080,
        "port_sbs":   30003,
    }


# ── Services systemd ─────────────────────────────────────────────────────

def get_services() -> list:
    services = [
        ("gnss-sdr",    "GNSS-SDR"),
        ("dump1090-fa", "ADS-B Decoder"),
        ("dump1090",    "ADS-B Decoder"),
        ("bridge",      "WS Bridge"),
        ("ssh",         "SSH Server"),
    ]
    result = []
    seen   = set()
    for svc, label in services:
        if label in seen:
            continue
        status = run(["systemctl", "is-active", svc])
        if status in ("active", "inactive", "failed"):
            result.append({
                "name":   svc,
                "label":  label,
                "status": status,
            })
            seen.add(label)
    return result


# ── Réseau ───────────────────────────────────────────────────────────────

def get_network() -> dict:
    stats = psutil.net_io_counters()
    ifaces = []
    for name, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET and not addr.address.startswith("127"):
                ifaces.append({"name": name, "ip": addr.address})
    return {
        "interfaces": ifaces,
        "bytes_sent": round(stats.bytes_sent / 1024**2, 1),
        "bytes_recv": round(stats.bytes_recv / 1024**2, 1),
    }


# ── GNSS / Galileo ──────────────────────────────────────────────────────

# Cache thread-safe pour les données GNSS (mises à jour en arrière-plan)
_gnss_cache: dict = {}
_gnss_lock  = Lock()

CONSTELLATION_MAP = {
    "GP": "GPS",
    "GA": "Galileo",
    "GL": "GLONASS",
    "GB": "BeiDou",
    "GN": "Multi",
}


def parse_nmea_gsv(sentence: str, sats: dict):
    """
    Parse $GxGSV — satellites en vue.
    Format : $GPGSV,nMsg,msgN,nSats,prn,elev,az,snr,...*CS
    Constellations : GP=GPS, GA=Galileo, GL=GLONASS, GB=BeiDou
    """
    try:
        parts = sentence.split("*")[0].split(",")
        prefix = parts[0][1:3]  # GP, GA, GL, GB...
        constellation = CONSTELLATION_MAP.get(prefix, prefix)

        # Chaque message contient jusqu'à 4 satellites (4 champs par sat)
        i = 4
        while i + 3 < len(parts):
            prn_raw = parts[i].strip()
            elev    = parts[i+1].strip()
            az      = parts[i+2].strip()
            snr_raw = parts[i+3].split("*")[0].strip()

            if prn_raw:
                prn = f"{constellation[:2]}{prn_raw.zfill(2)}"
                sats[prn] = {
                    "prn":           prn_raw,
                    "constellation": constellation,
                    "elevation":     int(elev)  if elev  else 0,
                    "azimuth":       int(az)    if az    else 0,
                    "snr":           int(snr_raw) if snr_raw else 0,
                    "used":          False,
                }
            i += 4
    except Exception:
        pass


def parse_nmea_gsa(sentence: str, sats: dict):
    """
    Parse $GxGSA — satellites utilisés pour le fix.
    Marque les PRN actifs dans le dict satellites.
    """
    try:
        parts = sentence.split("*")[0].split(",")
        prefix = parts[0][1:3]
        constellation = CONSTELLATION_MAP.get(prefix, "GPS")
        used_prns = {p.strip() for p in parts[3:15] if p.strip()}
        for key, sat in sats.items():
            if sat["prn"] in used_prns and sat["constellation"] == constellation:
                sat["used"] = True
    except Exception:
        pass


def parse_nmea_gga(sentence: str) -> dict | None:
    """
    Parse $GNGGA / $GPGGA — position et qualité du fix.
    """
    try:
        parts = sentence.split("*")[0].split(",")
        if len(parts) < 10:
            return None
        fix_q = int(parts[6]) if parts[6] else 0
        if fix_q == 0:
            return None

        lat_raw, lat_dir = parts[2], parts[3]
        lon_raw, lon_dir = parts[4], parts[5]

        def dms_to_dd(raw, direction):
            if not raw:
                return None
            dot = raw.index(".")
            deg = float(raw[:dot-2])
            mn  = float(raw[dot-2:])
            dd  = deg + mn / 60.0
            if direction in ("S", "W"):
                dd = -dd
            return round(dd, 7)

        return {
            "lat":      dms_to_dd(lat_raw, lat_dir),
            "lon":      dms_to_dd(lon_raw, lon_dir),
            "alt":      float(parts[9]) if parts[9] else None,
            "fix":      fix_q,
            "fix_label": {1: "GPS", 2: "DGPS", 4: "RTK", 5: "Float RTK"}.get(fix_q, f"Fix {fix_q}"),
            "n_sats":   int(parts[7]) if parts[7] else 0,
            "hdop":     float(parts[8]) if parts[8] else None,
        }
    except Exception:
        return None


def read_gnss_nmea() -> dict:
    """
    Lit les trames NMEA depuis plusieurs sources possibles :
    1. Socket GNSS-SDR port 2101 (NMEA out)
    2. Fichier NMEA de GNSS-SDR (/tmp/gnss-sdr/gnss-sdr_pvt.nmea)
    3. gpsd socket port 2947
    """
    lines = []

    # Source 1 : GNSS-SDR socket NMEA (port 2101)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(("127.0.0.1", 2101))
        data = b""
        for _ in range(5):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        lines = data.decode("utf-8", errors="ignore").strip().split("\n")
    except Exception:
        pass

    # Source 2 : fichier NMEA GNSS-SDR
    if not lines:
        for path in ["/home/suri/monitor/pvt.nmea",
                     "/tmp/gnss-sdr/gnss-sdr_pvt.nmea",
                     "/var/run/gnss-sdr/nmea"]:
            raw = read_file(path)
            if raw:
                lines = raw.strip().split("\n")[-50:]  # 50 dernières lignes
                break

    # Source 3 : gpsd (JSON protocol)
    if not lines:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(("127.0.0.1", 2947))
            sock.sendall(b'?WATCH={"enable":true,"nmea":true}\n')
            data = sock.recv(8192)
            sock.close()
            # gpsd retourne du JSON, on extrait le champ nmea si présent
            for jline in data.decode("utf-8", errors="ignore").split("\n"):
                jline = jline.strip()
                if jline.startswith("{"):
                    try:
                        obj = json.loads(jline)
                        if "nmea" in obj:
                            lines.append(obj["nmea"])
                    except Exception:
                        pass
        except Exception:
            pass

    return lines


def get_gnss() -> dict:
    """Collecte et parse toutes les données GNSS disponibles."""

    # Vérifier si GNSS-SDR tourne
    gnss_running = bool(
        run(["pgrep", "-x", "gnss-sdr"]) or
        run(["systemctl", "is-active", "gnss-sdr"]) == "active"
    )

    lines  = read_gnss_nmea()
    sats   = {}
    fix    = None

    for line in lines:
        line = line.strip()
        if not line.startswith("$"):
            continue
        tag = line[3:6] if len(line) > 5 else ""

        if tag == "GSV":
            parse_nmea_gsv(line, sats)
        elif tag == "GSA":
            parse_nmea_gsa(line, sats)
        elif tag in ("GGA", "GFA"):
            result = parse_nmea_gga(line)
            if result:
                fix = result

    # Résumé par constellation
    by_const: dict[str, list] = defaultdict(list)
    for sat in sats.values():
        by_const[sat["constellation"]].append(sat)

    summary = {
        k: {
            "total":  len(v),
            "used":   sum(1 for s in v if s["used"]),
            "avg_snr": round(
                sum(s["snr"] for s in v if s["snr"] > 0) /
                max(1, sum(1 for s in v if s["snr"] > 0)), 1
            ),
        }
        for k, v in by_const.items()
    }

    return {
        "running":     gnss_running,
        "satellites":  list(sats.values()),
        "fix":         fix,
        "summary":     summary,
        "total_sats":  len(sats),
        "sats_used":   sum(1 for s in sats.values() if s["used"]),
        "has_fix":     fix is not None,
    }


# ── Spectre FFT (rtl_power) ──────────────────────────────────────────────

def get_spectrum_snapshot(freq_start="80M", freq_end="110M",
                           step="100k") -> dict | None:
    """
    Lance rtl_power pour un scan rapide et retourne les données FFT.
    Retourne None si le SDR est occupé.
    """
    # Vérifier que le SDR est libre
    busy = run(["pgrep", "-x", "gnss-sdr"]) or \
           run(["pgrep", "-x", "rtl_fm"])   or \
           run(["pgrep", "-x", "dump1090"])
    if busy:
        return {"error": "SDR occupé — arrête GNSS-SDR ou dump1090 d'abord"}

    try:
        result = subprocess.run(
            ["rtl_power", "-f", f"{freq_start}:{freq_end}:{step}",
             "-i", "1", "-1", "-g", "42", "-"],
            capture_output=True, timeout=8
        )
        lines = result.stdout.decode("utf-8", errors="ignore").strip().split("\n")
        freqs, powers = [], []

        for line in lines:
            if not line.strip():
                continue
            parts = line.split(",")
            if len(parts) < 7:
                continue
            try:
                f_low  = float(parts[2])
                f_step = float(parts[4])
                dbvals = [float(x) for x in parts[6:] if x.strip()]
                for i, db in enumerate(dbvals):
                    freqs.append(round((f_low + i * f_step) / 1e6, 3))
                    powers.append(round(db, 1))
            except Exception:
                continue

        return {"freqs": freqs, "powers": powers,
                "f_start": freq_start, "f_end": freq_end}
    except Exception as e:
        return {"error": str(e)}


# ── WebSocket spectre ────────────────────────────────────────────────────

async def spectrum_ws_handler(ws):
    """WebSocket dédié au spectre — envoie un scan FFT toutes les secondes."""
    spec_clients.add(ws)
    # Lire les paramètres depuis le premier message client
    freq_start, freq_end, step = "80M", "110M", "200k"
    try:
        async for msg in ws:
            try:
                params = json.loads(msg)
                freq_start = params.get("f_start", freq_start)
                freq_end   = params.get("f_end",   freq_end)
                step       = params.get("step",    step)
                # Envoyer immédiatement un scan
                data = await asyncio.get_event_loop().run_in_executor(
                    None, get_spectrum_snapshot, freq_start, freq_end, step
                )
                if data:
                    await ws.send(json.dumps(data))
            except Exception:
                break
    finally:
        spec_clients.discard(ws)


# ── Payload complet ──────────────────────────────────────────────────────

def collect() -> dict:
    return {
        "ts":       int(time.time()),
        "system":   get_system(),
        "rtlsdr":   get_rtlsdr(),
        "dump1090": get_dump1090(),
        "gnss":     get_gnss(),
        "noaa":     noaa_tracker.get_state(),
        "space":    space_tracker.get_state(),
        "services": get_services(),
        "network":  get_network(),
    }


# ── WebSocket server ─────────────────────────────────────────────────────

async def ws_handler(ws):
    clients.add(ws)
    try:
        # Envoyer les données immédiatement à la connexion
        await ws.send(json.dumps(collect()))
        await ws.wait_closed()
    finally:
        clients.remove(ws)


async def broadcaster():
    while True:
        await asyncio.sleep(INTERVAL)
        if clients:
            data = json.dumps(collect())
            await asyncio.gather(*[c.send(data) for c in clients],
                                 return_exceptions=True)


# ── HTTP server (sert dashboard.html + API) ─────────────────────────────

DUMP1090_PROC     = None
DUMP1090_WEB_PROC = None
DUMP1090_LOCK     = Lock()

DUMP1090_SERVER = "/home/suri/dump1090/server.py"


def dump1090_start() -> dict:
    global DUMP1090_PROC, DUMP1090_WEB_PROC
    with DUMP1090_LOCK:
        if DUMP1090_PROC and DUMP1090_PROC.poll() is None:
            return {"ok": False, "msg": "dump1090 déjà en cours"}
        # Vérifier que le SDR est libre (ignorer les zombies <defunct>)
        def sdr_busy(name):
            out = run(["pgrep", "-x", name])
            if not out:
                return False
            # Vérifier que ce n'est pas un zombie
            for pid in out.split():
                status = read_file(f"/proc/{pid}/status")
                if "State:\tZ" not in status:
                    return True
            return False

        if sdr_busy("gnss-sdr") or sdr_busy("rtl_fm"):
            return {"ok": False, "msg": "SDR occupé — arrête GNSS-SDR d'abord"}
        # Chercher l'exécutable dump1090
        exe = None
        for path in ["/usr/local/bin/dump1090",
                     "/home/suri/dump1090/dump1090",
                     "/usr/bin/dump1090-fa",
                     "/usr/bin/dump1090"]:
            if Path(path).exists():
                exe = path
                break
        if not exe:
            return {"ok": False, "msg": "dump1090 introuvable"}
        try:
            # Lancer dump1090
            DUMP1090_PROC = subprocess.Popen(
                [exe, "--net", "--quiet"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Lancer le serveur web après 1 seconde
            time.sleep(1)
            if Path(DUMP1090_SERVER).exists():
                DUMP1090_WEB_PROC = subprocess.Popen(
                    ["python3", DUMP1090_SERVER],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            return {"ok": True, "msg": f"dump1090 démarré (PID {DUMP1090_PROC.pid})"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}


def dump1090_stop() -> dict:
    global DUMP1090_PROC, DUMP1090_WEB_PROC
    with DUMP1090_LOCK:
        for proc in [DUMP1090_PROC, DUMP1090_WEB_PROC]:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        DUMP1090_PROC     = None
        DUMP1090_WEB_PROC = None
        run(["pkill", "-x", "dump1090"])
        run(["pkill", "-x", "dump1090-fa"])
        run(["pkill", "-f", "server.py"])
        return {"ok": True, "msg": "dump1090 arrêté"}


def gnss_start() -> dict:
    global GNSS_PROC
    with GNSS_LOCK:
        if GNSS_PROC and GNSS_PROC.poll() is None:
            return {"ok": False, "msg": "GNSS-SDR déjà en cours"}
        config = Path(__file__).parent / "gnss-sdr-rtlsdr.conf"
        if not config.exists():
            return {"ok": False, "msg": f"Config introuvable : {config}"}
        try:
            GNSS_PROC = subprocess.Popen(
                ["gnss-sdr", f"--config_file={config}"],
                cwd=str(Path(__file__).parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {"ok": True, "msg": f"GNSS-SDR démarré (PID {GNSS_PROC.pid})"}
        except Exception as e:
            return {"ok": False, "msg": str(e)}


def gnss_stop() -> dict:
    global GNSS_PROC
    with GNSS_LOCK:
        # Tuer le process géré par nous
        if GNSS_PROC and GNSS_PROC.poll() is None:
            GNSS_PROC.terminate()
            try:
                GNSS_PROC.wait(timeout=5)
            except subprocess.TimeoutExpired:
                GNSS_PROC.kill()
            GNSS_PROC = None
        # Tuer aussi tout process gnss-sdr externe
        run(["pkill", "-x", "gnss-sdr"])
        return {"ok": True, "msg": "GNSS-SDR arrêté"}


class ApiHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/gnss/start":
            result = gnss_start()
        elif self.path == "/api/gnss/stop":
            result = gnss_stop()
        elif self.path == "/api/dump1090/start":
            result = dump1090_start()
        elif self.path == "/api/dump1090/stop":
            result = dump1090_stop()
        elif self.path == "/api/noaa/enable":
            noaa_tracker.enable()
            result = {"ok": True, "msg": "Scheduler NOAA activé"}
        elif self.path == "/api/noaa/disable":
            noaa_tracker.disable()
            result = {"ok": True, "msg": "Scheduler NOAA désactivé"}
        elif self.path == "/api/noaa/record":
            state  = noaa_tracker.get_state()
            passes = state.get("passes", [])
            if passes:
                p    = passes[0]
                sat  = p["satellite"]
                freq = int(p["freq_mhz"] * 1e6)
                dur  = p["duration"] + 60
                Thread(target=noaa_tracker.record_pass,
                       args=(sat, freq, dur), daemon=True).start()
                result = {"ok": True, "msg": f"Enregistrement {sat} lancé"}
            else:
                result = {"ok": False, "msg": "Aucun passage prévu"}
        elif self.path == "/api/noaa/reset":
            run(["pkill", "-x", "rtl_fm"])
            run(["pkill", "-x", "sox"])
            noaa_tracker._update_state(
                recording=False, decoding=False,
                recording_sat=None,
                status="idle", status_msg="Reset manuel"
            )
            result = {"ok": True, "msg": "État remis à zéro"}
        elif self.path == "/api/spectrum":
            # Scan unique — paramètres dans le body JSON
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length) if length else b"{}"
            try:
                params = json.loads(body)
            except Exception:
                params = {}
            data = get_spectrum_snapshot(
                params.get("f_start", "80M"),
                params.get("f_end",   "110M"),
                params.get("step",    "200k"),
            )
            result = data or {"error": "Scan impossible"}
        else:
            self.send_response(404)
            self.end_headers()
            return

        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/dashboard.html"):
            try:
                content = (Path(__file__).parent / "dashboard.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(content))
                self.end_headers()
                self.wfile.write(content)
            except Exception as e:
                self.send_error(500, str(e))
            return
        # Autres fichiers statiques (images NOAA, etc.)
        return super().do_GET()


def run_http():
    os.chdir(Path(__file__).parent)
    server = HTTPServer(("0.0.0.0", HTTP_PORT), ApiHandler)
    print(f"Dashboard → http://0.0.0.0:{HTTP_PORT}")
    server.serve_forever()


# ── Main ─────────────────────────────────────────────────────────────────

async def main():
    noaa_tracker.start()
    space_tracker.start()
    print(f"WebSocket données → ws://0.0.0.0:{WS_PORT}")
    print(f"WebSocket spectre → ws://0.0.0.0:{WS_SPEC_PORT}")
    Thread(target=run_http, daemon=True).start()
    async with serve(ws_handler, "0.0.0.0", WS_PORT), \
               serve(spectrum_ws_handler, "0.0.0.0", WS_SPEC_PORT):
        await broadcaster()


if __name__ == "__main__":
    asyncio.run(main())
