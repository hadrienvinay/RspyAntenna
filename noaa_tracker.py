#!/usr/bin/env python3
"""
noaa_tracker.py
Prédit les passages NOAA, enregistre automatiquement le signal APT,
décode l'image et la sauvegarde dans /home/suri/monitor/images/
"""

import asyncio
import json
import logging
import math
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread, Lock, Event

log = logging.getLogger("noaa")

# ── Config ───────────────────────────────────────────────────────────────

WORK_DIR   = Path(__file__).parent
IMAGES_DIR = WORK_DIR / "images"
TLE_CACHE  = WORK_DIR / "noaa_tle.txt"
IMAGES_DIR.mkdir(exist_ok=True)

# Arcachon
OBSERVER_LAT =  44.6611
OBSERVER_LON =  -1.1638
OBSERVER_ALT =   5.0     # mètres

# Satellites NOAA APT (fréquences en Hz)
NOAA_SATS = {
    "NOAA-15": {"freq": 137_620_000, "tle_name": "NOAA 15"},
    "NOAA-18": {"freq": 137_912_500, "tle_name": "NOAA 18"},
    "NOAA-19": {"freq": 137_100_000, "tle_name": "NOAA 19"},
}

MIN_ELEVATION = 20   # degrés — ignorer les passages trop bas
REC_MARGIN    = 30   # secondes avant/après le passage

# ── TLE ─────────────────────────────────────────────────────────────────

def fetch_tle() -> dict:
    """Télécharge les TLE NOAA depuis Celestrak et retourne un dict name→(l1,l2)."""
    catnrs = {"NOAA 15": 25338, "NOAA 18": 28654, "NOAA 19": 33591}
    all_raw = []

    for name, catnr in catnrs.items():
        url = f"https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=tle"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                all_raw.append(r.read().decode("utf-8").strip())
        except Exception as e:
            log.warning(f"TLE {name} : {e}")

    if all_raw:
        raw = "\n".join(all_raw)
        TLE_CACHE.write_text(raw)
        log.info("TLE NOAA mis à jour")
    elif TLE_CACHE.exists():
        raw = TLE_CACHE.read_text()
    else:
        return {}

    tles = {}
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    for i in range(0, len(lines) - 2, 3):
        name = lines[i].strip()
        l1   = lines[i+1]
        l2   = lines[i+2]
        if l1.startswith("1") and l2.startswith("2"):
            tles[name] = (l1, l2)
    return tles


# ── Orbital mechanics (sans dépendance externe) ──────────────────────────

def tle_to_state(l1: str, l2: str, t: float) -> tuple:
    """
    Propagation SGP4 simplifiée — retourne (lat_deg, lon_deg, alt_km).
    Précision suffisante pour la planification de passes.
    """
    try:
        # Epoch depuis l2
        epoch_year = int(l1[18:20])
        epoch_day  = float(l1[20:32])
        year = 2000 + epoch_year if epoch_year < 57 else 1900 + epoch_year
        epoch_unix = (datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()
                      + (epoch_day - 1) * 86400)

        # Éléments orbitaux
        inc  = math.radians(float(l2[8:16]))
        raan = math.radians(float(l2[17:25]))
        ecc  = float("0." + l2[26:33])
        argp = math.radians(float(l2[34:42]))
        M0   = math.radians(float(l2[43:51]))
        n    = float(l2[52:63]) * 2 * math.pi / 86400  # rad/s

        dt = t - epoch_unix
        M  = M0 + n * dt

        # Équation de Kepler (2 itérations)
        E = M
        for _ in range(5):
            E = M + ecc * math.sin(E)

        nu  = 2 * math.atan2(math.sqrt(1+ecc)*math.sin(E/2),
                              math.sqrt(1-ecc)*math.cos(E/2))
        r   = 6378.137 * (1 - ecc*math.cos(E)) * (1 + ecc) / (1 - ecc**2) * 0.001
        # Approximation r en km
        a_km = 6378.137 / math.sqrt(1 - ecc**2) * (1 + 0.001) * 7.0
        r_km = a_km * (1 - ecc * math.cos(E))

        # Position dans le plan orbital
        x_o = r_km * math.cos(nu)
        y_o = r_km * math.sin(nu)

        # Rotation vers ECEF
        cos_raan, sin_raan = math.cos(raan), math.sin(raan)
        cos_argp, sin_argp = math.cos(argp), math.sin(argp)
        cos_inc,  sin_inc  = math.cos(inc),  math.sin(inc)

        x = (cos_raan*cos_argp - sin_raan*sin_argp*cos_inc)*x_o + \
            (-cos_raan*sin_argp - sin_raan*cos_argp*cos_inc)*y_o
        y = (sin_raan*cos_argp + cos_raan*sin_argp*cos_inc)*x_o + \
            (-sin_raan*sin_argp + cos_raan*cos_argp*cos_inc)*y_o
        z = (sin_argp*sin_inc)*x_o + (cos_argp*sin_inc)*y_o

        # ECEF → géodésique (rotation Terre)
        gst = (280.46061837 + 360.98564736629 *
               (t - 946728000) / 86400) % 360
        gst_rad = math.radians(gst)
        x2 =  x*math.cos(gst_rad) + y*math.sin(gst_rad)
        y2 = -x*math.sin(gst_rad) + y*math.cos(gst_rad)
        z2 =  z

        lon = math.degrees(math.atan2(y2, x2))
        lat = math.degrees(math.atan2(z2, math.sqrt(x2**2 + y2**2)))
        alt = math.sqrt(x2**2 + y2**2 + z2**2) - 6371.0

        return lat, lon, alt
    except Exception:
        return 0.0, 0.0, 500.0


def elevation_from_observer(sat_lat, sat_lon, sat_alt_km,
                              obs_lat, obs_lon, obs_alt_m=35) -> float:
    """Calcule l'élévation du satellite depuis l'observateur (degrés)."""
    R = 6371.0
    obs_alt_km = obs_alt_m / 1000.0

    obs_lat_r = math.radians(obs_lat)
    obs_lon_r = math.radians(obs_lon)
    sat_lat_r = math.radians(sat_lat)
    sat_lon_r = math.radians(sat_lon)

    # Vecteurs ECEF
    R_obs = R + obs_alt_km
    R_sat = R + sat_alt_km

    ox = R_obs * math.cos(obs_lat_r) * math.cos(obs_lon_r)
    oy = R_obs * math.cos(obs_lat_r) * math.sin(obs_lon_r)
    oz = R_obs * math.sin(obs_lat_r)

    sx = R_sat * math.cos(sat_lat_r) * math.cos(sat_lon_r)
    sy = R_sat * math.cos(sat_lat_r) * math.sin(sat_lon_r)
    sz = R_sat * math.sin(sat_lat_r)

    # Vecteur observateur→satellite
    dx, dy, dz = sx-ox, sy-oy, sz-oz
    dist = math.sqrt(dx**2 + dy**2 + dz**2)

    # Normale locale
    nx = math.cos(obs_lat_r) * math.cos(obs_lon_r)
    ny = math.cos(obs_lat_r) * math.sin(obs_lon_r)
    nz = math.sin(obs_lat_r)

    dot = (dx*nx + dy*ny + dz*nz) / dist
    elev = math.degrees(math.asin(max(-1, min(1, dot))))
    return elev


# ── Prédiction de passages ───────────────────────────────────────────────

def predict_passes(tle_name: str, l1: str, l2: str,
                   hours_ahead: int = 24) -> list:
    """
    Calcule les N prochains passages visibles (élév > MIN_ELEVATION).
    Retourne une liste de dicts {start, end, max_elev, duration}.
    """
    now   = time.time()
    step  = 30   # secondes
    end_t = now + hours_ahead * 3600
    passes = []
    in_pass = False
    pass_start = 0
    max_elev   = 0.0

    t = now
    while t < end_t:
        lat, lon, alt = tle_to_state(l1, l2, t)
        elev = elevation_from_observer(lat, lon, alt,
                                        OBSERVER_LAT, OBSERVER_LON)

        if elev >= MIN_ELEVATION and not in_pass:
            in_pass    = True
            pass_start = t
            max_elev   = elev
        elif elev >= MIN_ELEVATION and in_pass:
            max_elev   = max(max_elev, elev)
        elif elev < MIN_ELEVATION and in_pass:
            in_pass = False
            duration = int(t - pass_start)
            if duration > 60:
                passes.append({
                    "start":    int(pass_start),
                    "end":      int(t),
                    "max_elev": round(max_elev, 1),
                    "duration": duration,
                    "start_fmt": datetime.fromtimestamp(pass_start).strftime("%d/%m %H:%M"),
                })
            if len(passes) >= 5:
                break

        t += step

    return passes


# ── État global ──────────────────────────────────────────────────────────

_state = {
    "passes":       [],     # prochains passages
    "recording":    False,
    "recording_sat": None,
    "decoding":     False,
    "last_image":   None,   # chemin de la dernière image
    "last_image_ts": None,
    "last_update":  0,
    "status":       "idle", # idle / waiting / recording / decoding / done
    "status_msg":   "En attente",
    "enabled":      True,   # False = scheduler pausé, pas d'enregistrement auto
}
_lock = Lock()
_stop_recording = Event()  # signalé par disable() pour interrompre un enregistrement en cours


def get_state() -> dict:
    with _lock:
        return dict(_state)


def _update_state(**kwargs):
    with _lock:
        _state.update(kwargs)


def enable():
    """Active l'enregistrement automatique."""
    log.info("Scheduler NOAA activé (capture automatique)")
    _update_state(enabled=True, status_msg="Scheduler actif")


def disable():
    """Désactive l'enregistrement automatique et arrête tout process en cours."""
    import subprocess as _sp
    log.info("Scheduler NOAA désactivé (capture automatique coupée)")
    _stop_recording.set()  # réveille immédiatement record_pass() si en attente
    _sp.run(["pkill", "-x", "rtl_fm"],  capture_output=True)
    _sp.run(["pkill", "-x", "sox"],     capture_output=True)
    _update_state(
        enabled=False,
        recording=False,
        decoding=False,
        recording_sat=None,
        status="idle",
        status_msg="Scheduler désactivé",
    )


# ── Enregistrement et décodage ───────────────────────────────────────────

def record_pass(sat_name: str, freq_hz: int, duration_s: int):
    """Enregistre le signal APT et décode l'image."""
    # Une capture précédente a pu laisser le signal armé (disable() appelé
    # entre deux passages) — on repart sur une base propre.
    _stop_recording.clear()

    if get_state().get("enabled") is False:
        # Le scheduler a été désactivé juste avant le démarrage du thread
        # (course avec _scheduler_loop) — ne pas lancer rtl_fm pour rien.
        log.info(f"Capture NOAA {sat_name} annulée (scheduler désactivé avant démarrage)")
        return

    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_out = WORK_DIR / f"noaa_{sat_name}_{ts}.wav"
    img_out = IMAGES_DIR / f"noaa_{sat_name}_{ts}.png"

    _update_state(recording=True, recording_sat=sat_name,
                  status="recording",
                  status_msg=f"Enregistrement {sat_name} ({duration_s}s)…")
    log.info(f"Capture NOAA démarrée : {sat_name} @ {freq_hz/1e6:.3f} MHz "
              f"({duration_s}s)")

    p_fm  = None
    p_sox = None
    try:
        cmd_rtlfm = [
            "rtl_fm", "-f", str(freq_hz),
            "-M", "fm", "-s", "48000", "-r", "11025",
            "-g", "49.6", "-"
        ]
        cmd_sox = [
            "sox", "-t", "raw", "-r", "11025",
            "-e", "s", "-b", "16", "-c", "1", "-",
            str(wav_out)
        ]

        p_fm  = subprocess.Popen(cmd_rtlfm, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
        p_sox = subprocess.Popen(cmd_sox, stdin=p_fm.stdout,
                                  stderr=subprocess.DEVNULL)
        p_fm.stdout.close()

        # Attendre la durée prévue, mais se réveiller immédiatement si
        # disable()/stop est déclenché — évite de rester bloqué jusqu'à
        # la fin du passage alors que les process ont déjà été tués.
        interrupted = _stop_recording.wait(timeout=duration_s)
        if interrupted:
            log.info(f"Capture NOAA {sat_name} interrompue (arrêt demandé)")

    except Exception as e:
        log.error(f"Erreur enregistrement : {e}")
        _update_state(recording=False, status="idle",
                      status_msg=f"Erreur enregistrement : {e}")
        return
    finally:
        # Arrêt propre dans tous les cas
        for p in [p_fm, p_sox]:
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()

    # Vérifier que le WAV existe et est assez grand
    if not wav_out.exists() or wav_out.stat().st_size < 1_000_000:
        log.warning(f"Capture NOAA {sat_name} trop courte ou vide — abandon")
        _update_state(recording=False, status="idle",
                      status_msg="Enregistrement trop court ou vide")
        return

    log.info(f"Capture NOAA {sat_name} terminée "
              f"({wav_out.stat().st_size // 1024} Ko) — décodage APT…")
    _update_state(recording=False, decoding=True,
                  status="decoding", status_msg="Décodage APT…")

    # Décodage avec noaa-apt
    try:
        result = subprocess.run(
            ["noaa-apt", str(wav_out), "-o", str(img_out),
             "--contrast", "histogram"],
            capture_output=True, timeout=180,
            cwd=str(WORK_DIR)
        )
        if result.returncode == 0 and img_out.exists():
            _update_state(
                decoding=False,
                last_image=str(img_out),
                last_image_ts=int(time.time()),
                status="done",
                status_msg=f"Image reçue — {sat_name} {ts}"
            )
            log.info(f"Image NOAA décodée : {img_out.name} (satellite {sat_name})")
        else:
            err = result.stderr.decode(errors="ignore").strip()
            raise RuntimeError(err or "Code retour non nul")
    except Exception as e:
        _update_state(decoding=False, status="idle",
                      status_msg=f"Erreur décodage : {e}")
        log.error(f"Erreur décodage : {e}")
    finally:
        # Nettoyer le WAV
        try:
            wav_out.unlink()
        except Exception:
            pass


# ── Boucle principale ────────────────────────────────────────────────────

def _scheduler_loop():
    """Boucle en arrière-plan : refresh TLE toutes les 12h, programme les passes."""
    tle_refresh = 0
    tles = {}

    while True:
        now = time.time()

        # Refresh TLE toutes les 12h
        if now - tle_refresh > 43200:
            tles = fetch_tle()
            tle_refresh = now

        # Calculer les prochains passages
        all_passes = []
        for sat_name, info in NOAA_SATS.items():
            tle_name = info["tle_name"]
            entry = tles.get(tle_name)
            if not entry:
                continue
            l1, l2 = entry
            passes = predict_passes(tle_name, l1, l2, hours_ahead=12)
            for p in passes:
                p["satellite"] = sat_name
                p["freq_mhz"]  = info["freq"] / 1e6
                all_passes.append(p)

        all_passes.sort(key=lambda x: x["start"])
        _update_state(passes=all_passes[:6], last_update=int(now))

        # Déclencher l'enregistrement si un passage approche (dans 30s)
        state = get_state()
        if state["enabled"] and not state["recording"] and not state["decoding"]:
            for p in all_passes:
                time_to_start = p["start"] - now
                if -REC_MARGIN <= time_to_start <= REC_MARGIN:
                    sat  = p["satellite"]
                    freq = NOAA_SATS[sat]["freq"]
                    dur  = p["duration"] + 2 * REC_MARGIN
                    Thread(target=record_pass,
                           args=(sat, freq, dur),
                           daemon=True).start()
                    break

        time.sleep(60)


def start():
    """Démarre le scheduler en arrière-plan."""
    Thread(target=_scheduler_loop, daemon=True, name="noaa-scheduler").start()
    log.info("NOAA tracker démarré")
