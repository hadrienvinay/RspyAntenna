#!/usr/bin/env python3
"""
dump1090 Web Server
Lit le flux SBS depuis dump1090 (port 30003),
construit aircraft.json et sert l'interface web sur le port 8080.
"""

import json
import socket
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PUBLIC = Path(__file__).parent / "public_html"
PORT   = 8080

# ── Cache avions ─────────────────────────────────────────────────────────

aircraft = {}
lock     = threading.Lock()

CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "js":   "application/javascript; charset=utf-8",
    "css":  "text/css; charset=utf-8",
    "json": "application/json",
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "ico":  "image/x-icon",
    "svg":  "image/svg+xml",
    "gif":  "image/gif",
    "woff": "font/woff",
    "woff2":"font/woff2",
}


# ── Lecture SBS (port 30003) ─────────────────────────────────────────────

def sbs_reader():
    """Lit le flux SBS de dump1090 et met à jour le cache avions."""
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect(("127.0.0.1", 30003))
            buf = ""
            while True:
                chunk = sock.recv(4096).decode("utf-8", errors="ignore")
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    _parse_sbs(line.strip())
        except Exception:
            pass
        finally:
            try:
                sock.close()
            except Exception:
                pass
        time.sleep(2)


def _parse_sbs(line: str):
    parts = line.split(",")
    if len(parts) < 22:
        return
    msg_type = parts[1].strip()
    icao     = parts[4].strip().lower()
    if not icao:
        return

    with lock:
        if icao not in aircraft:
            aircraft[icao] = {
                "hex":      icao,
                "flight":   "",
                "alt_baro": "",
                "gs":       "",
                "track":    "",
                "lat":      None,
                "lon":      None,
                "squawk":   "",
                "seen":     time.time(),
                "messages": 0,
            }
        a = aircraft[icao]
        a["messages"] += 1
        a["seen"]      = time.time()

        def safe_int(v):
            try: return int(v) if v.strip() else None
            except: return None

        def safe_float(v):
            try: return float(v) if v.strip() else None
            except: return None

        if msg_type == "1":
            if parts[10].strip():
                a["flight"] = parts[10].strip()
        elif msg_type == "2":
            v = safe_int(parts[11]);   a["alt_baro"] = v if v is not None else a["alt_baro"]
            v = safe_float(parts[12]); a["gs"]       = v if v is not None else a["gs"]
            v = safe_float(parts[13]); a["track"]    = v if v is not None else a["track"]
            v = safe_float(parts[14]); a["lat"]      = v if v is not None else a["lat"]
            v = safe_float(parts[15]); a["lon"]      = v if v is not None else a["lon"]
        elif msg_type == "3":
            v = safe_int(parts[11]);   a["alt_baro"] = v if v is not None else a["alt_baro"]
            v = safe_float(parts[14]); a["lat"]      = v if v is not None else a["lat"]
            v = safe_float(parts[15]); a["lon"]      = v if v is not None else a["lon"]
        elif msg_type == "4":
            v = safe_float(parts[12]); a["gs"]    = v if v is not None else a["gs"]
            v = safe_float(parts[13]); a["track"] = v if v is not None else a["track"]
        elif msg_type == "6":
            if parts[17].strip():
                a["squawk"] = parts[17].strip()


def build_aircraft_json() -> bytes:
    now = time.time()
    with lock:
        # Expirer les avions non vus depuis 60 secondes
        expired = [k for k, v in aircraft.items() if now - v["seen"] > 60]
        for k in expired:
            del aircraft[k]

        result = []
        for a in aircraft.values():
            entry = {k: v for k, v in a.items() if v is not None and v != ""}
            # "seen" doit être le nombre de secondes écoulées, pas un timestamp Unix
            entry["seen"] = round(now - a["seen"], 1)
            # "seen_pos" : secondes depuis la dernière position connue
            if a.get("lat") is not None:
                entry["seen_pos"] = entry["seen"]
            result.append(entry)

        data = {
            "now":      now,
            "messages": sum(a["messages"] for a in aircraft.values()),
            "aircraft": result,
        }
    return json.dumps(data).encode()


def build_receiver_json() -> bytes:
    return json.dumps({
        "version": "1.0",
        "refresh": 1000,
        "lat":     44.6611,   # Arcachon
        "lon":     -1.1638,
    }).encode()


# ── Serveur HTTP ─────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # silencieux

    def send_json(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type",  "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]  # ignorer les query strings

        # ── API données dump1090
        if path == "/data/aircraft.json":
            self.send_json(build_aircraft_json())
            return

        if path == "/data/receiver.json":
            self.send_json(build_receiver_json())
            return

        # ── Fichiers statiques
        if path == "/":
            path = "/index.html"

        file_path = PUBLIC / path.lstrip("/")

        # Sécurité : empêcher la traversée de dossier
        try:
            file_path.resolve().relative_to(PUBLIC.resolve())
        except ValueError:
            self.send_error(403)
            return

        if not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return

        try:
            ext  = file_path.suffix.lstrip(".").lower()
            ct   = CONTENT_TYPES.get(ext, "application/octet-stream")
            data = file_path.read_bytes()

            self.send_response(200)
            self.send_header("Content-Type",   ct)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, str(e))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()


# ── Main ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Démarrer le lecteur SBS en arrière-plan
    threading.Thread(target=sbs_reader, daemon=True, name="sbs-reader").start()
    print(f"dump1090 web server → http://0.0.0.0:{PORT}")
    print(f"Public dir          → {PUBLIC}")

    # Permettre la réutilisation du port immédiatement après un restart
    HTTPServer.allow_reuse_address = True
    try:
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("Arrêt du serveur.")
