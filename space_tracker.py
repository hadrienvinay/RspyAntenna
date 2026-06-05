#!/usr/bin/env python3
"""
space_tracker.py
- Position ISS en temps réel
- Prédiction de passages (ISS, NOAA, Galileo) au-dessus d'Arcachon
- Météo spatiale NOAA (Kp, F10.7, alertes)
"""

import json
import logging
import math
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread, Lock

log = logging.getLogger("space")

WORK_DIR     = Path(__file__).parent
TLE_DIR      = WORK_DIR / "tle"
TLE_DIR.mkdir(exist_ok=True)

# Arcachon
OBS_LAT =  44.6611
OBS_LON =  -1.1638
OBS_ALT =   5.0

MIN_ELEV    = 10    # degrés min pour un passage
HOURS_AHEAD = 24

# ── TLE Sources ──────────────────────────────────────────────────────────

TLE_SOURCES = {
    "iss":     "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=tle",
    "noaa":    (
        "https://celestrak.org/NORAD/elements/gp.php?CATNR=25338&FORMAT=tle\n"  # NOAA 15
        "https://celestrak.org/NORAD/elements/gp.php?CATNR=28654&FORMAT=tle\n"  # NOAA 18
        "https://celestrak.org/NORAD/elements/gp.php?CATNR=33591&FORMAT=tle"    # NOAA 19
    ),
    "galileo": "https://celestrak.org/NORAD/elements/gp.php?GROUP=galileo&FORMAT=tle",
}

NOAA_CATNR = {
    "NOAA-15": 25338,
    "NOAA-18": 28654,
    "NOAA-19": 33591,
}

# Satellites Galileo à suivre (les plus représentatifs)
GALILEO_TRACK = [
    "GSAT0101", "GSAT0102", "GSAT0103", "GSAT0104",
    "GSAT0201", "GSAT0202", "GSAT0203", "GSAT0204",
]

# ── Fetch helpers ────────────────────────────────────────────────────────

def fetch_url(url: str, timeout=10) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log.warning(f"fetch_url {url} : {e}")
        return None


def fetch_json(url: str) -> dict | list | None:
    raw = fetch_url(url)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def load_tle(group: str) -> dict:
    """Charge les TLE depuis le cache ou le réseau. Retourne {name: (l1, l2)}."""
    cache = TLE_DIR / f"{group}.tle"
    raw   = None

    # Cache valide < 6h
    if cache.exists() and (time.time() - cache.stat().st_mtime) < 21600:
        raw = cache.read_text()
    else:
        source = TLE_SOURCES.get(group, "")

        # NOAA : plusieurs URLs séparées par \n
        if group == "noaa":
            parts = []
            for url in source.strip().split("\n"):
                url = url.strip()
                if url:
                    data = fetch_url(url)
                    if data:
                        parts.append(data.strip())
            raw = "\n".join(parts) if parts else None
        else:
            raw = fetch_url(source)

        if raw:
            cache.write_text(raw)

    if not raw:
        return {}

    tles = {}
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    for i in range(0, len(lines) - 2, 3):
        name = lines[i].strip()
        l1, l2 = lines[i+1], lines[i+2]
        if l1.startswith("1") and l2.startswith("2"):
            tles[name] = (l1, l2)
    return tles


# ── SGP4 simplifié ───────────────────────────────────────────────────────

def propagate(l1: str, l2: str, t: float) -> tuple[float, float, float]:
    """Retourne (lat_deg, lon_deg, alt_km)."""
    try:
        epoch_year = int(l1[18:20])
        epoch_day  = float(l1[20:32])
        year = 2000 + epoch_year if epoch_year < 57 else 1900 + epoch_year
        epoch_unix = (datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()
                      + (epoch_day - 1) * 86400)

        inc  = math.radians(float(l2[8:16]))
        raan = math.radians(float(l2[17:25]))
        ecc  = float("0." + l2[26:33])
        argp = math.radians(float(l2[34:42]))
        M0   = math.radians(float(l2[43:51]))
        n    = float(l2[52:63]) * 2 * math.pi / 86400

        dt = t - epoch_unix
        M  = (M0 + n * dt) % (2 * math.pi)

        E = M
        for _ in range(8):
            E = M + ecc * math.sin(E)

        nu  = 2 * math.atan2(math.sqrt(1+ecc)*math.sin(E/2),
                              math.sqrt(1-ecc)*math.cos(E/2))
        a   = (398600.4418 / (n**2)) ** (1/3)
        r   = a * (1 - ecc * math.cos(E))

        x_o =  r * math.cos(nu)
        y_o =  r * math.sin(nu)

        cos_raan, sin_raan = math.cos(raan), math.sin(raan)
        cos_inc,  sin_inc  = math.cos(inc),  math.sin(inc)
        cos_argp, sin_argp = math.cos(argp), math.sin(argp)

        x = (cos_raan*cos_argp - sin_raan*sin_argp*cos_inc)*x_o \
          + (-cos_raan*sin_argp - sin_raan*cos_argp*cos_inc)*y_o
        y = (sin_raan*cos_argp + cos_raan*sin_argp*cos_inc)*x_o \
          + (-sin_raan*sin_argp + cos_raan*cos_argp*cos_inc)*y_o
        z = (sin_inc*sin_argp)*x_o + (sin_inc*cos_argp)*y_o

        # Rotation sidérale
        gst_deg = (280.46061837 + 360.98564736629*(t - 946728000)/86400) % 360
        gst     = math.radians(gst_deg)
        x2 =  x*math.cos(gst) + y*math.sin(gst)
        y2 = -x*math.sin(gst) + y*math.cos(gst)

        lon = math.degrees(math.atan2(y2, x2))
        lat = math.degrees(math.atan2(z, math.sqrt(x2**2 + y2**2)))
        alt = math.sqrt(x2**2 + y2**2 + z**2) - 6371.0

        return round(lat, 5), round(lon % 360 - 180, 5) if lon > 180 else round(lon, 5), round(alt, 1)
    except Exception:
        return 0.0, 0.0, 400.0


def elevation_azimuth(sat_lat, sat_lon, sat_alt_km) -> tuple[float, float]:
    """Élévation et azimut depuis Arcachon."""
    R = 6371.0
    obs_alt = OBS_ALT / 1000.0

    oLat, oLon = math.radians(OBS_LAT), math.radians(OBS_LON)
    sLat, sLon = math.radians(sat_lat), math.radians(sat_lon)

    ro = R + obs_alt
    rs = R + sat_alt_km

    ox = ro * math.cos(oLat) * math.cos(oLon)
    oy = ro * math.cos(oLat) * math.sin(oLon)
    oz = ro * math.sin(oLat)

    sx = rs * math.cos(sLat) * math.cos(sLon)
    sy = rs * math.cos(sLat) * math.sin(sLon)
    sz = rs * math.sin(sLat)

    dx, dy, dz = sx-ox, sy-oy, sz-oz
    dist = math.sqrt(dx**2 + dy**2 + dz**2)

    # Vecteur local
    nx = math.cos(oLat)*math.cos(oLon)
    ny = math.cos(oLat)*math.sin(oLon)
    nz = math.sin(oLat)

    ex = -math.sin(oLon)
    ey =  math.cos(oLon)
    ez =  0

    nx2 = -math.sin(oLat)*math.cos(oLon)
    ny2 = -math.sin(oLat)*math.sin(oLon)
    nz2 =  math.cos(oLat)

    up    = dx*nx  + dy*ny  + dz*nz
    east  = dx*ex  + dy*ey  + dz*ez
    north = dx*nx2 + dy*ny2 + dz*nz2

    elev = math.degrees(math.asin(max(-1, min(1, up/dist))))
    az   = math.degrees(math.atan2(east, north)) % 360

    return round(elev, 1), round(az, 1)


# ── Prédiction de passages ───────────────────────────────────────────────

def predict_passes_tle(name: str, l1: str, l2: str,
                        hours: int = HOURS_AHEAD) -> list:
    now   = time.time()
    end_t = now + hours * 3600
    step  = 20
    passes, in_pass = [], False
    p_start = p_maxelev = p_az_start = 0

    t = now
    while t < end_t:
        lat, lon, alt = propagate(l1, l2, t)
        elev, az      = elevation_azimuth(lat, lon, alt)

        if elev >= MIN_ELEV and not in_pass:
            in_pass  = True
            p_start  = t
            p_maxelev = elev
            p_az_start = az
        elif elev >= MIN_ELEV and in_pass:
            p_maxelev = max(p_maxelev, elev)
        elif elev < MIN_ELEV and in_pass:
            in_pass   = False
            dur       = int(t - p_start)
            if dur > 30:
                passes.append({
                    "name":      name,
                    "start":     int(p_start),
                    "end":       int(t),
                    "duration":  dur,
                    "max_elev":  round(p_maxelev, 1),
                    "az_start":  round(p_az_start, 1),
                    "start_fmt": datetime.fromtimestamp(p_start).strftime("%d/%m %H:%M"),
                })
            if len(passes) >= 4:
                break
        t += step

    return passes


# ── ISS live ────────────────────────────────────────────────────────────

def get_sat_positions() -> list:
    """
    Calcule les positions actuelles de tous les satellites trackés.
    Retourne une liste de dicts {name, type, lat, lon, alt, elev, az, visible}
    """
    now  = time.time()
    sats = []

    # ISS
    iss_tles = load_tle("iss")
    for name, (l1, l2) in iss_tles.items():
        lat, lon, alt = propagate(l1, l2, now)
        elev, az      = elevation_azimuth(lat, lon, alt)
        sats.append({
            "name": "ISS", "type": "iss",
            "lat": lat, "lon": lon, "alt_km": round(alt, 1),
            "elev": elev, "az": az,
            "visible": elev > 0,
        })

    # NOAA
    noaa_tles = load_tle("noaa")
    noaa_names = {"NOAA 15": "NOAA-15", "NOAA 18": "NOAA-18", "NOAA 19": "NOAA-19"}
    for name, (l1, l2) in noaa_tles.items():
        short = noaa_names.get(name, name)
        lat, lon, alt = propagate(l1, l2, now)
        elev, az      = elevation_azimuth(lat, lon, alt)
        sats.append({
            "name": short, "type": "noaa",
            "lat": lat, "lon": lon, "alt_km": round(alt, 1),
            "elev": elev, "az": az,
            "visible": elev > 0,
        })

    # Galileo — tous les satellites disponibles
    galileo_tles = load_tle("galileo")
    for name, (l1, l2) in galileo_tles.items():
        short = name.split()[0] if name else name
        lat, lon, alt = propagate(l1, l2, now)
        elev, az      = elevation_azimuth(lat, lon, alt)
        sats.append({
            "name": short, "type": "galileo",
            "lat": lat, "lon": lon, "alt_km": round(alt, 1),
            "elev": elev, "az": az,
            "visible": elev > 0,
        })

    return sats


# ── ISS live ────────────────────────────────────────────────────────────

def get_iss_live() -> dict:
    """Position ISS depuis l'API wheretheiss.at."""
    data = fetch_json("https://api.wheretheiss.at/v1/satellites/25544")
    if data:
        elev, az = elevation_azimuth(
            data["latitude"], data["longitude"], data["altitude"]
        )
        return {
            "lat":      round(data["latitude"],  4),
            "lon":      round(data["longitude"], 4),
            "alt_km":   round(data["altitude"],  1),
            "velocity": round(data["velocity"],  1),
            "elev":     elev,
            "az":       az,
            "visible":  elev > 0,
            "ts":       int(time.time()),
        }
    # Fallback TLE
    tles = load_tle("iss")
    if tles:
        l1, l2 = next(iter(tles.values()))
        lat, lon, alt = propagate(l1, l2, time.time())
        elev, az = elevation_azimuth(lat, lon, alt)
        return {"lat": lat, "lon": lon, "alt_km": alt,
                "velocity": 7.66, "elev": elev, "az": az,
                "visible": elev > 0, "ts": int(time.time())}
    return {}


# ── Météo spatiale ───────────────────────────────────────────────────────

def get_space_weather() -> dict:
    result = {
        "kp_current":  None,
        "kp_24h":      [],
        "f107":        None,
        "alerts":      [],
        "storm_level": "None",
        "ts":          int(time.time()),
    }

    # Kp index actuel (planetary K-index, 1 min)
    kp_data = fetch_json(
        "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"
    )
    if kp_data and isinstance(kp_data, list) and len(kp_data) > 0:
        latest = kp_data[-1]
        try:
            result["kp_current"] = float(latest.get("kp_index", latest.get("Kp", 0)))
        except Exception:
            pass
        # Historique 24h
        result["kp_24h"] = [
            {"t": row[0] if isinstance(row, list) else row.get("time_tag", ""),
             "kp": float(row[1] if isinstance(row, list) else row.get("kp_index", 0))}
            for row in kp_data[-48:]
        ]

    # F10.7 flux solaire
    f107_data = fetch_json(
        "https://services.swpc.noaa.gov/json/solar-cycle/observed-solar-cycle-indices.json"
    )
    if f107_data and isinstance(f107_data, list) and len(f107_data) > 0:
        try:
            latest = f107_data[-1]
            result["f107"] = float(latest.get("f10.7", latest.get("ssn", 0)))
        except Exception:
            pass

    # Alertes actives
    alerts_data = fetch_json(
        "https://services.swpc.noaa.gov/products/alerts.json"
    )
    if alerts_data and isinstance(alerts_data, list):
        result["alerts"] = [
            {
                "issue_time": a.get("issue_datetime", ""),
                "message":    a.get("message", "")[:120],
                "severity":   _parse_severity(a.get("message", "")),
            }
            for a in alerts_data[:5]
        ]

    # Niveau de tempête géomagnétique
    kp = result["kp_current"] or 0
    if kp >= 8:
        result["storm_level"] = "G4-G5 Sévère"
    elif kp >= 6:
        result["storm_level"] = "G2-G3 Modérée"
    elif kp >= 5:
        result["storm_level"] = "G1 Mineure"
    elif kp >= 4:
        result["storm_level"] = "Active"
    else:
        result["storm_level"] = "Calme"

    return result


def _parse_severity(msg: str) -> str:
    msg = msg.upper()
    if "EXTREME" in msg or "X-CLASS" in msg or "G5" in msg:
        return "extreme"
    if "SEVERE" in msg or "G4" in msg or "M-CLASS" in msg:
        return "severe"
    if "STRONG" in msg or "G3" in msg:
        return "strong"
    if "MODERATE" in msg or "G2" in msg:
        return "moderate"
    return "minor"


# ── État global ──────────────────────────────────────────────────────────

_state = {
    "iss":           {},
    "passes":        [],
    "space_weather": {},
    "iss_track":     [],
    "sat_positions": [],  # positions temps réel ISS + NOAA + Galileo
}
_lock = Lock()


def get_state() -> dict:
    with _lock:
        return {
            "iss":           dict(_state["iss"]),
            "passes":        list(_state["passes"]),
            "space_weather": dict(_state["space_weather"]),
            "iss_track":     list(_state["iss_track"][-120:]),
            "sat_positions": list(_state["sat_positions"]),
        }


# ── Boucles d'update ────────────────────────────────────────────────────

def _iss_loop():
    """ISS toutes les 5 secondes, positions satellites toutes les 15s."""
    tick = 0
    while True:
        try:
            pos = get_iss_live()
            with _lock:
                _state["iss"] = pos
                if pos:
                    _state["iss_track"].append([pos["lat"], pos["lon"]])
                    if len(_state["iss_track"]) > 200:
                        _state["iss_track"].pop(0)

            # Mettre à jour les positions toutes les 15s
            if tick % 3 == 0:
                positions = get_sat_positions()
                with _lock:
                    _state["sat_positions"] = positions
            tick += 1
        except Exception as e:
            log.error(f"ISS loop : {e}")
        time.sleep(5)


def _passes_loop():
    """Passes toutes les 10 minutes."""
    while True:
        try:
            all_passes = []

            # ISS
            iss_tles = load_tle("iss")
            for name, (l1, l2) in iss_tles.items():
                for p in predict_passes_tle("ISS", l1, l2, hours=12):
                    p["type"] = "iss"
                    all_passes.append(p)

            # NOAA
            noaa_tles = load_tle("noaa")
            for name, (l1, l2) in noaa_tles.items():
                short = name.replace("NOAA ", "NOAA-")
                for p in predict_passes_tle(short, l1, l2, hours=12):
                    p["type"] = "noaa"
                    all_passes.append(p)

            # Galileo (sélection)
            galileo_tles = load_tle("galileo")
            for name, (l1, l2) in galileo_tles.items():
                if not any(g in name for g in GALILEO_TRACK):
                    continue
                short = name.split()[0] if name else name
                for p in predict_passes_tle(short, l1, l2, hours=12):
                    p["type"] = "galileo"
                    all_passes.append(p)

            all_passes.sort(key=lambda x: x["start"])

            with _lock:
                _state["passes"] = all_passes[:20]

            log.info(f"Passes calculées : {len(all_passes)} passages")
        except Exception as e:
            log.error(f"Passes loop : {e}")
        time.sleep(600)


def _weather_loop():
    """Météo spatiale toutes les 15 minutes."""
    while True:
        try:
            sw = get_space_weather()
            with _lock:
                _state["space_weather"] = sw
            log.info(f"Météo spatiale : Kp={sw.get('kp_current')} F10.7={sw.get('f107')}")
        except Exception as e:
            log.error(f"Weather loop : {e}")
        time.sleep(900)


def start():
    Thread(target=_iss_loop,     daemon=True, name="iss-tracker").start()
    Thread(target=_passes_loop,  daemon=True, name="pass-predictor").start()
    Thread(target=_weather_loop, daemon=True, name="space-weather").start()
    log.info("Space tracker démarré")
