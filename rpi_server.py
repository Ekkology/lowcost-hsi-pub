"""
rpi_server.py — API REST del escáner hiperspectral (corre en la Raspberry Pi)
FastAPI + Uvicorn | Python 3.9+

Iniciar:
    uvicorn rpi_server:app --host 0.0.0.0 --port 8000

La Mac se conecta a http://<ip-raspberry>:8000
"""
import csv, json, os, threading, time, traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from Sensor import create_spectrometer
    SENSOR_OK = True
except Exception as e:
    SENSOR_OK = False
    print(f"Sensor no disponible: {e}")

try:
    import serial
    SERIAL_OK = True
except Exception:
    SERIAL_OK = False

# Pipeline de preprocesamiento (numpy puro): corre en la propia Raspberry sobre
# el espectros.csv ya guardado. Se importa de forma tolerante para no tumbar el
# servidor si falta una dependencia.
try:
    from preprocessing import PreprocessConfig, _save_pca_plot, preprocess_pipeline
    from spectral_core import load_dataset
    PREPROCESS_OK = True
except Exception as e:
    PREPROCESS_OK = False
    print(f"Preprocesamiento no disponible: {e}")

# Etiquetado manual de píxeles (segmentación). Núcleo puro en labeling.py.
try:
    from labeling import (
        default_levels,
        labels_from_csv,
        labels_from_json,
        labels_to_csv,
        labels_to_json,
        validate_levels,
    )
    LABELING_OK = True
except Exception as e:
    LABELING_OK = False
    print(f"Etiquetado no disponible: {e}")

# ── Constantes ────────────────────────────────────────────────────────────────
SCANS_DIR = "scans"
# Taxonomía de niveles de etiquetado (compartida entre escaneos para que el
# dataset sea consistente). Las etiquetas concretas van por escaneo.
LEVELS_FILE = "etiquetas_niveles.json"
# Referencias de calibración blanco (W) y negro (D) para la corrección exacta
# de reflectancia (Ec. 1). Una sola de cada tipo; una captura nueva sobrescribe.
CALIB_FILE = "calibracion.json"
# Lecturas de temperatura/humedad que envía el ESP8266 (DHT11) por POST.
ENV_FILE    = "ambiente.csv"          # histórico global (raíz del proyecto)
ENV_HISTORY = 500          # lecturas que se guardan en memoria (el CSV crece)
# Una lectura más vieja que esto ya no representa el ambiente actual: la UI la
# pinta en ámbar y, por punto, se anota como hueco en vez de repetir un dato
# rancio (el ESP reporta cada ~2 s; 30 s significan que se cayó o perdió WiFi).
ENV_STALE_S = 30.0
# Deriva térmica que dispara aviso durante un escaneo: el dark current del
# C12880MA depende de la temperatura, así que un salto así invalida la
# referencia D capturada antes de empezar (ver docs/adquisicion-hardware.md).
ENV_DRIFT_WARN_C = 2.0
RGB_BANDS = {"R": (620, 680), "G": (520, 560), "B": (450, 490)}
MOVE_FEED = 1000
SETTLE    = 0.3
# Tras este nº de errores SEGUIDOS del sensor, el escaneo se pone en PAUSA (no
# aborta): así, si el sensor se cae, la CNC deja de avanzar y puedes reconectarlo
# y reanudar desde el checkpoint. Un error aislado no pausa (rellena ceros).
MAX_SENSOR_ERRORS = 5

# ── Estado hardware ───────────────────────────────────────────────────────────
hw = {
    "sensor": None,
    "cnc_serial": None,
    "cnc_connected": False,
    "cnc_position": {"x": 0.0, "y": 0.0, "z": 0.0},
}

# El sensor (ADS1115 por I2C) NO admite accesos concurrentes: si el "en vivo"
# de la UI y el escaneo miden a la vez, el bus I2C colisiona y lanza
# OSError [Errno 5]. Este lock serializa TODA lectura del sensor.
_sensor_lock = threading.Lock()

# ── Estado scan ───────────────────────────────────────────────────────────────
scan_st = {
    "running": False, "paused": False,
    "progress": 0, "total": 0, "eta": "--", "errors": 0,
    "log": [],
}

# ── Estado ambiente (ESP8266 + DHT11) ─────────────────────────────────────────
# El ESP empuja lecturas por POST /data_temp cada pocos segundos, de forma
# asíncrona al escaneo. Guardamos la última en memoria (consulta al vuelo), el
# histórico en CSV y, mientras hay un escaneo en curso, una copia de la serie
# dentro de su carpeta + un acumulado (min/max/media/deriva) que termina en su
# config.json. Así cada captura queda autocontenida: no hay que cruzar a mano
# el CSV global por timestamps para saber en qué ambiente se tomó.
_env_lock = threading.Lock()
env_st = {
    "last":     None,   # último registro {ts, temperature, humidity, device}
    "last_t":   0.0,    # time.time() de esa lectura (para calcular antigüedad)
    "history":  [],     # buffer circular en memoria (ENV_HISTORY)
    "scan_dir": None,   # carpeta del escaneo en curso, o None
    "run":      None,   # acumulado del escaneo en curso, o None
}

def _env_run_new():
    return {"n": 0, "t_start": None, "t_end": None, "t_min": None, "t_max": None,
            "t_sum": 0.0, "h_start": None, "h_end": None, "h_min": None,
            "h_max": None, "h_sum": 0.0}

def _env_run_accum(run, rec):
    """Acumula una lectura en el resumen del escaneo (llamar bajo _env_lock)."""
    t, h = rec["temperature"], rec["humidity"]
    if run["n"] == 0:
        run.update({"t_start": t, "t_min": t, "t_max": t,
                    "h_start": h, "h_min": h, "h_max": h})
    run["n"]    += 1
    run["t_end"] = t; run["h_end"] = h
    run["t_min"] = min(run["t_min"], t); run["t_max"] = max(run["t_max"], t)
    run["h_min"] = min(run["h_min"], h); run["h_max"] = max(run["h_max"], h)
    run["t_sum"] += t; run["h_sum"] += h

def _env_run_public(run):
    """Resumen legible del acumulado (o None si no llegó ninguna lectura)."""
    if not run or run["n"] == 0:
        return None
    n = run["n"]
    return {
        "n": n,
        "t_start": run["t_start"], "t_end": run["t_end"],
        "t_min": run["t_min"], "t_max": run["t_max"],
        "t_mean": round(run["t_sum"] / n, 2),
        "t_drift": round(run["t_end"] - run["t_start"], 2),
        "h_start": run["h_start"], "h_end": run["h_end"],
        "h_min": run["h_min"], "h_max": run["h_max"],
        "h_mean": round(run["h_sum"] / n, 2),
        "h_drift": round(run["h_end"] - run["h_start"], 2),
    }

def _env_public():
    """Lectura ambiental para la API: última medida, antigüedad y resumen."""
    with _env_lock:
        last, t0 = env_st["last"], env_st["last_t"]
        run = _env_run_public(env_st["run"])
    if last is None:
        return {"last": None, "age_s": None, "stale": True, "run": run}
    age = round(time.time() - t0, 1)
    return {"last": last, "age_s": age, "stale": age > ENV_STALE_S, "run": run}

def _env_for_point():
    """Lectura vigente para estampar en un punto del escaneo.

    Devuelve (temp, hum, edad) o (None, None, edad) si está rancia: se prefiere
    un hueco explícito a repetir un dato que ya no describe el ambiente.
    """
    with _env_lock:
        last, t0 = env_st["last"], env_st["last_t"]
    if last is None:
        return None, None, None
    age = round(time.time() - t0, 1)
    if age > ENV_STALE_S:
        return None, None, age
    return last["temperature"], last["humidity"], age

def _env_begin(out_dir):
    """Empieza a copiar la serie ambiental dentro de la carpeta del escaneo."""
    with _env_lock:
        env_st["scan_dir"] = out_dir
        env_st["run"] = _env_run_new()

def _env_finish():
    """Cierra la copia por escaneo y devuelve el resumen para el config.json."""
    with _env_lock:
        run = _env_run_public(env_st["run"])
        env_st["scan_dir"] = None
        env_st["run"] = None
    return run

# ── App FastAPI ───────────────────────────────────────────────────────────────
app = FastAPI(title="Hyperspectral Scanner API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ── Helpers internos ──────────────────────────────────────────────────────────
def _log(msg, level="info"):
    ts   = datetime.now().strftime("%H:%M:%S")
    icon = {"info": "i", "ok": "v", "error": "x", "warn": "!"}.get(level, "-")
    scan_st["log"].append({"t": ts, "icon": icon, "msg": msg, "level": level})
    if len(scan_st["log"]) > 300:
        scan_st["log"].pop(0)

def _cnc_send(cmd, wait_ok=True, timeout=60):
    ser = hw["cnc_serial"]
    if not ser or not ser.is_open: return False
    ser.flushInput()
    ser.write((cmd + "\n").encode())
    ser.flush()
    _log(f"CNC >> {cmd}")
    if not wait_ok: return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ser.in_waiting == 0:
            time.sleep(0.05); continue
        line = ser.readline().decode(errors="ignore").strip()
        if not line: continue
        if line.startswith("ok"): return True
        if "error" in line.lower():
            _log(f"GRBL error [{cmd}]: {line}", "error"); return False
    _log(f"Timeout [{cmd}]", "warn"); return False

def _cnc_move(x=None, y=None, z=None):
    parts = [f"F{MOVE_FEED}"]
    if x is not None: parts.append(f"X{x:.3f}")
    if y is not None: parts.append(f"Y{y:.3f}")
    if z is not None: parts.append(f"Z{z:.3f}")
    _cnc_send("G1 " + " ".join(parts))
    time.sleep(SETTLE)

def _spectrum_to_rgb(spectrum, wavelengths):
    wl = np.array(wavelengths); sp = np.array(spectrum)
    rgb = []
    for band in ("R", "G", "B"):
        lo, hi = RGB_BANDS[band]
        mask = (wl >= lo) & (wl <= hi)
        rgb.append(float(sp[mask].mean()) if mask.any() else 0.0)
    return rgb

def _save_rgb(cube, rows, cols, xs, ys, step, path):
    img = cube.copy().astype(np.float32)
    for c in range(3):
        ch = img[:, :, c]; lo, hi = ch.min(), ch.max()
        if hi > lo: img[:, :, c] = (ch - lo) / (hi - lo)
    fig, ax = plt.subplots(figsize=(7, 7))
    ax.imshow(img, origin="upper", extent=[xs, xs+(cols-1)*step, ys+(rows-1)*step, ys])
    ax.set_xlabel("X (mm)"); ax.set_ylabel("Y (mm)")
    ax.set_title("Imagen hiperespectral RGB")
    patches = [mpatches.Patch(color=c, label=f"{b} {RGB_BANDS[b][0]}-{RGB_BANDS[b][1]}nm")
               for b, c in zip(("R", "G", "B"), ("red", "green", "blue"))]
    ax.legend(handles=patches, loc="upper right", fontsize=8)
    plt.tight_layout(); plt.savefig(path, dpi=150); plt.close()

def _load_past_scans():
    scans = []
    os.makedirs(SCANS_DIR, exist_ok=True)
    for folder in sorted(Path(SCANS_DIR).glob("scan_*")):
        if not folder.is_dir(): continue
        rgb   = folder / "imagen_rgb.png"
        ckpt  = folder / "config.json"
        cfg_s = {}
        if ckpt.exists():
            try:
                with open(ckpt) as f: cfg_s = json.load(f)
            except Exception: pass
        analysis = None
        apath = folder / "analisis.json"
        if apath.exists():
            try:
                with open(apath) as f: analysis = json.load(f)
            except Exception: pass
        labeled = 0
        lpath = folder / "etiquetas.csv"
        if lpath.exists() and LABELING_OK:
            try:
                labeled = len(labels_from_csv(lpath.read_text()))
            except Exception: pass
        scans.append({
            "ts":          cfg_s.get("ts", folder.name.replace("scan_", "")),
            "scan_subdir": folder.name,
            # Un escaneo abortado deja el PNG a 0 bytes (matplotlib no llegó a
            # escribirlo): existe pero no es una imagen, y en la UI saldría como
            # un hueco roto. Se anuncia como inexistente.
            "rgb_exists":  rgb.exists() and rgb.stat().st_size > 0,
            "pca_exists":  (folder / "pca.png").exists(),
            "points":      cfg_s.get("points_done", 0),
            "total":       cfg_s.get("points_total", 0),
            "errors":      cfg_s.get("errors", 0),
            "cfg":         cfg_s,
            "analysis":    analysis,
            "labeled":     labeled,
            # Resumen ambiental (escaneos anteriores a esta versión: None) y si
            # hay serie completa que abrir en /scan/<subdir>/env.
            "env":         cfg_s.get("env"),
            "env_series":  (folder / ENV_FILE).exists(),
        })
    return scans

# ── Scan worker (corre en background en la Raspberry) ─────────────────────────
def _scan_worker(params: dict):
    s    = scan_st
    cols = params["cols"];      rows  = params["rows"]
    xs   = params["x_start"];   ys    = params["y_start"];  zf   = params["z_focus"]
    step = params["step_mm"];   expo  = params["exposition_ms"] / 1000.0
    acc  = params["accum"];     sensor = hw["sensor"]
    lamp = params.get("lamp_power_pct", 0.0)
    total = cols * rows
    s["total"] = total; s["progress"] = 0; s["errors"] = 0

    ts  = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out = os.path.join(SCANS_DIR, f"scan_{ts}")
    os.makedirs(out, exist_ok=True)
    ckpt        = os.path.join(out, "progreso.json")
    csv_f_path  = os.path.join(out, "espectros.csv")
    rgb_path    = os.path.join(out, "imagen_rgb.png")
    env_pts_path = os.path.join(out, "ambiente_puntos.csv")

    wl   = sensor.wavelength()
    cube = np.zeros((rows, cols, 3), dtype=np.float32)
    done_set = set(); done_list = []

    if os.path.exists(ckpt):
        try:
            with open(ckpt) as f: ck = json.load(f)
            for p in ck.get("completed", []):
                done_list.append(p); done_set.add(tuple(p))
            s["progress"] = len(done_list)
            _log(f"Reanudando: {len(done_list)} puntos ya hechos", "warn")
        except Exception: pass

    csv_exists = os.path.exists(csv_f_path)
    cf = open(csv_f_path, "a", newline="")
    wr = csv.writer(cf)
    if not csv_exists:
        wr.writerow(["col", "row", "x_mm", "y_mm", "z_mm"] + [f"{w:.2f}nm" for w in wl])

    # Ambiente por punto, en un CSV aparte: en espectros.csv las columnas tras
    # z_mm son longitudes de onda y load_dataset las parsea por posición, así
    # que añadir columnas ahí rompería el pipeline.
    env_exists = os.path.exists(env_pts_path)
    ef = open(env_pts_path, "a", newline="")
    ew = csv.writer(ef)
    if not env_exists:
        ew.writerow(["col", "row", "ts", "temp_c", "hum_pct", "edad_s"])
    env_warned = 0   # múltiplos de ENV_DRIFT_WARN_C ya avisados

    # Desde aquí, cada POST del ESP se copia también en <out>/ambiente.csv y
    # alimenta el resumen que acabará en el config.json. Se activa al final de
    # la preparación: si algo falla antes, no queda apuntando a una carpeta de
    # un escaneo que nunca arrancó (el finally que lo cierra aún no existe).
    _env_begin(out)
    _cnc_send("G21"); _cnc_send("G90")
    _cnc_move(z=zf)
    _log(f"Iniciando grilla {cols}x{rows}={total} puntos", "ok")
    t0 = time.time()
    consecutive_errors = 0  # errores seguidos del sensor (dispara la pausa)

    try:
        for row in range(rows):
            y  = ys + row * step
            cr = range(cols) if row % 2 == 0 else range(cols - 1, -1, -1)
            for col in cr:
                if not s["running"]:
                    _log("Detenido por usuario", "warn"); return
                while s["paused"] and s["running"]:
                    time.sleep(0.3)
                if (col, row) in done_set: continue
                x = xs + col * step; _cnc_move(x=x, y=y)
                _log(f"Capturando ({col},{row})...", "info")
                # Bucle de medición con reintento: un error aislado rellena ceros
                # y sigue; MAX_SENSOR_ERRORS seguidos ponen el escaneo en PAUSA y,
                # al reanudar, se reintenta ESTE mismo punto (spectrum sigue None).
                spectrum = None
                while spectrum is None:
                    if not s["running"]:
                        _log("Detenido por usuario", "warn"); return
                    try:
                        with _sensor_lock:
                            result = sensor.measure(exposition=expo, accum=acc)
                        # Se guarda el espectro CRUDO: la corrección (dark, blanco/
                        # negro) la hace preprocessing.py, sin corregir dos veces.
                        # La saturación se sigue detectando sobre el crudo.
                        spectrum = result["intensity_raw"]
                        consecutive_errors = 0
                        _log(f"({col},{row}) OK - max={max(spectrum):.0f}", "info")
                        if result["saturated_pixels"]:
                            _log(f"Saturacion ({col},{row}): {len(result['saturated_pixels'])} px", "warn")
                    except Exception as e:
                        consecutive_errors += 1
                        s["errors"] += 1
                        _log(f"Error sensor ({col},{row}): {e}", "error")
                        traceback.print_exc()
                        if consecutive_errors >= MAX_SENSOR_ERRORS:
                            _log(
                                f"{consecutive_errors} errores seguidos del sensor: "
                                "ESCANEO EN PAUSA. Revisa el sensor y pulsa Reanudar.",
                                "warn",
                            )
                            s["paused"] = True
                            consecutive_errors = 0
                            while s["paused"] and s["running"]:
                                time.sleep(0.3)
                            # al reanudar, spectrum sigue None -> se reintenta el punto
                        else:
                            spectrum = [0.0] * 288  # error aislado: ceros y seguir
                wr.writerow([col, row, f"{x:.3f}", f"{y:.3f}", f"{zf:.3f}"] + spectrum)
                cf.flush()
                # Ambiente vigente en este punto (vacío si el ESP no reporta o
                # su última lectura ya está rancia).
                e_t, e_h, e_age = _env_for_point()
                ew.writerow([col, row, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                             "" if e_t is None else e_t,
                             "" if e_h is None else e_h,
                             "" if e_age is None else e_age])
                ef.flush()
                run = _env_public()["run"]
                if run:
                    # Un salto térmico invalida la referencia dark tomada antes
                    # de empezar: se avisa al cruzar cada múltiplo del umbral.
                    k = int(abs(run["t_drift"]) / ENV_DRIFT_WARN_C)
                    if k > env_warned:
                        env_warned = k
                        _log(f"Deriva térmica {run['t_drift']:+.1f} °C desde el inicio "
                             f"({run['t_start']:.1f} → {run['t_end']:.1f} °C): "
                             "considera recapturar la referencia oscura", "warn")
                cube[row, col] = _spectrum_to_rgb(spectrum, wl)
                done_list.append([col, row]); done_set.add((col, row))
                with open(ckpt, "w") as f: json.dump({"completed": done_list}, f)
                s["progress"] += 1
                d = s["progress"]; el = time.time() - t0
                if d > 0: s["eta"] = f"{(el/d)*(total-d)/60:.1f} min"
    except Exception as e:
        _log(f"Error critico: {e}", "error"); traceback.print_exc()
    finally:
        cf.close()
        ef.close()
        env_summary = _env_finish()
        if hw["cnc_connected"]: _cnc_move(x=xs, y=ys)
        _save_rgb(cube, rows, cols, xs, ys, step, rgb_path)
        s["running"] = False
        cfg_snap = {
            "ts": ts, "x_start": xs, "y_start": ys, "z_focus": zf,
            "cols": cols, "rows": rows, "step_mm": step,
            "exposition_ms": expo * 1000, "accum": acc,
            "lamp_power_pct": lamp,
            "points_done": s["progress"], "points_total": total, "errors": s["errors"],
            # Resumen ambiental de la corrida (None si el ESP no reportó nada).
            "env": env_summary,
        }
        with open(os.path.join(out, "config.json"), "w") as f:
            json.dump(cfg_snap, f, indent=2)
        _log(f"Escaneo terminado {s['progress']}/{total} puntos, {s['errors']} errores", "ok")

# ── Pydantic models ───────────────────────────────────────────────────────────
class SensorConnectBody(BaseModel):
    driver: str = "improved"

class MeasureBody(BaseModel):
    exposition: float
    accum: int

class CncConnectBody(BaseModel):
    port: str = "/dev/ttyUSB0"

class CncSendBody(BaseModel):
    cmd: str
    wait_ok: bool = True

class CncMoveBody(BaseModel):
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None

class ScanStartBody(BaseModel):
    cols: int;        rows: int
    x_start: float;  y_start: float;  z_focus: float
    step_mm: float;  exposition_ms: float;  accum: int
    # Potencia de la lámpara en % (10–99), anotada a mano en la UI.
    # Solo se registra en el config.json del escaneo.
    lamp_power_pct: float = 0.0

class ProcessBody(BaseModel):
    # Parámetros del pipeline (por defecto, los del paper). Si hay referencias
    # W/D capturadas, se usan para la corrección exacta (Ec. 1); si no, o si
    # ignore_references=True, corre en modo adaptado (pseudo-reflectancia).
    sg_window: int = 11
    outlier_k: float = 1.0
    n_components: int = 2
    ignore_references: bool = False

class CalibCaptureBody(BaseModel):
    kind: str            # "white" (blanco) | "dark" (negro)
    exposition: float
    accum: int = 1
    samples: int = 10    # tomas a promediar (reduce ruido)

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "sensor_connected": hw["sensor"] is not None,
        "sensor_name":      hw["sensor"].name if hw["sensor"] else None,
        "cnc_connected":    hw["cnc_connected"],
    }

# ── Sensor ────────────────────────────────────────────────────────────────────
@app.post("/sensor/connect")
def sensor_connect(body: SensorConnectBody):
    if not SENSOR_OK:
        raise HTTPException(503, "Libreria de sensor no disponible en la Raspberry")
    try:
        s = create_spectrometer(body.driver)
        s.initialize()
        hw["sensor"] = s
        return {"ok": True, "name": s.name}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/sensor/disconnect")
def sensor_disconnect():
    if hw["sensor"]:
        try: hw["sensor"].cleanup()
        except Exception: pass
        hw["sensor"] = None
    return {"ok": True}

@app.get("/sensor/wavelength")
def sensor_wavelength():
    if not hw["sensor"]:
        raise HTTPException(409, "Sensor no conectado")
    return {"wavelengths": hw["sensor"].wavelength()}

@app.post("/sensor/measure")
def sensor_measure(body: MeasureBody):
    if not hw["sensor"]:
        raise HTTPException(409, "Sensor no conectado")
    try:
        with _sensor_lock:
            result = hw["sensor"].measure(exposition=body.exposition, accum=body.accum)
            result["wavelengths"] = hw["sensor"].wavelength()
        return result
    except Exception as e:
        raise HTTPException(500, str(e))

# ── CNC ───────────────────────────────────────────────────────────────────────
@app.post("/cnc/connect")
def cnc_connect(body: CncConnectBody):
    if not SERIAL_OK:
        raise HTTPException(503, "pyserial no instalado en la Raspberry")
    try:
        s = serial.Serial(body.port, 115200, timeout=10)
        time.sleep(2); s.flushInput(); s.write(b"\r\n\r\n"); time.sleep(1); s.flushInput()
        hw["cnc_serial"] = s; hw["cnc_connected"] = True
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/cnc/disconnect")
def cnc_disconnect():
    ser = hw["cnc_serial"]
    if ser and ser.is_open: ser.close()
    hw["cnc_serial"] = None; hw["cnc_connected"] = False
    return {"ok": True}

@app.post("/cnc/send")
def cnc_send_route(body: CncSendBody):
    ok = _cnc_send(body.cmd, wait_ok=body.wait_ok)
    return {"ok": ok}

@app.get("/cnc/position")
def cnc_position():
    ser = hw["cnc_serial"]
    if ser and ser.is_open:
        try:
            ser.write(b"?")
            resp = ser.readline().decode(errors="ignore").strip()
            if "MPos:" in resp:
                mpos = resp.split("MPos:")[1].split("|")[0].split(",")
                hw["cnc_position"] = {
                    "x": float(mpos[0]), "y": float(mpos[1]), "z": float(mpos[2]),
                }
        except Exception: pass
    return hw["cnc_position"]

@app.post("/cnc/move")
def cnc_move_route(body: CncMoveBody):
    _cnc_send("G21"); _cnc_send("G90")
    _cnc_move(x=body.x, y=body.y, z=body.z)
    return {"ok": True}

# ── Scan ──────────────────────────────────────────────────────────────────────
@app.post("/scan/start")
def scan_start(body: ScanStartBody):
    if scan_st["running"]:
        raise HTTPException(409, "Escaneo ya en curso")
    if not hw["sensor"]:
        raise HTTPException(409, "Sensor no conectado")
    if not hw["cnc_connected"]:
        raise HTTPException(409, "CNC no conectada")
    scan_st["running"] = True; scan_st["paused"] = False
    threading.Thread(target=_scan_worker, args=(body.dict(),), daemon=True).start()
    return {"ok": True}

@app.post("/scan/stop")
def scan_stop():
    scan_st["running"] = False
    return {"ok": True}

@app.post("/scan/pause")
def scan_pause():
    scan_st["paused"] = True
    return {"ok": True}

@app.post("/scan/resume")
def scan_resume():
    scan_st["paused"] = False
    return {"ok": True}

@app.get("/scan/status")
def scan_status():
    return {
        "running":  scan_st["running"],
        "paused":   scan_st["paused"],
        "progress": scan_st["progress"],
        "total":    scan_st["total"],
        "eta":      scan_st["eta"],
        "errors":   scan_st["errors"],
        "log":      scan_st["log"][-60:],
        # La UI pinta T/HR con este mismo poll: no necesita un segundo timer
        # contra /data_temp mientras hay escaneo.
        "env":      _env_public(),
    }

@app.get("/scan/results")
def scan_results():
    return _load_past_scans()

def _scan_dir(subdir: str) -> Path:
    """Resuelve la carpeta de un escaneo evitando path traversal.

    Solo admite nombres ``scan_*`` directos bajo ``SCANS_DIR``; cualquier ``/``,
    ``\\`` o intento de subir de directorio se rechaza.
    """
    if "/" in subdir or "\\" in subdir or not subdir.startswith("scan_"):
        raise HTTPException(400, "Nombre de escaneo inválido")
    base = Path(SCANS_DIR).resolve()
    folder = (base / subdir).resolve()
    if base not in folder.parents or not folder.is_dir():
        raise HTTPException(404, "Escaneo no encontrado")
    return folder

@app.post("/scan/{subdir}/process")
def scan_process(subdir: str, body: ProcessBody = ProcessBody()):
    """Corre el pipeline de preprocesamiento sobre el espectros.csv del escaneo.

    Genera ``pca.png`` y persiste el reporte/métricas en ``analisis.json`` dentro
    de la carpeta del escaneo, y los devuelve. Modo adaptado (sin referencias
    W/D): la corrección produce pseudo-reflectancia.
    """
    if not PREPROCESS_OK:
        raise HTTPException(503, "Módulo de preprocesamiento no disponible")
    folder = _scan_dir(subdir)
    csv_path = folder / "espectros.csv"
    if not csv_path.exists():
        raise HTTPException(404, "El escaneo no tiene espectros.csv")
    try:
        ds = load_dataset(str(csv_path))
    except Exception as e:
        raise HTTPException(400, f"No se pudo leer el escaneo: {e}")

    config = PreprocessConfig(
        sg_window=body.sg_window,
        outlier_k=body.outlier_k,
        n_components=body.n_components,
    )

    # Referencias W/D para la corrección exacta (Ec. 1). Se usan solo si ambas
    # existen y coinciden en nº de canales con el escaneo; si no, modo adaptado.
    white = dark = None
    if not body.ignore_references:
        white, dark = _load_reference_vectors(ds.profiles.shape[1])

    try:
        result = preprocess_pipeline(ds.profiles, white=white, dark=dark, config=config)
    except ValueError as e:
        # p. ej. quedaron muy pocos perfiles tras el filtro de anómalos.
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Error en el análisis: {e}")

    adapted = white is None or dark is None

    pca_ok = _save_pca_plot(result, folder / "pca.png")
    metrics = {
        "report": result.report(),
        "n_input": result.n_input,
        "n_kept": result.n_kept,
        "bartlett_p": result.bartlett.p_value,
        "bartlett_singular": result.bartlett.singular,
        "kmo": result.kmo_total,
        "explained_variance_ratio": [
            float(v) for v in result.pca.explained_variance_ratio
        ],
        "cumulative_variance": float(result.pca.cumulative_variance),
        "pca_exists": bool(pca_ok),
        "adapted_mode": adapted,
        "references": {"white": white is not None, "dark": dark is not None},
        "config": {
            "sg_window": config.sg_window,
            "outlier_k": config.outlier_k,
            "n_components": config.n_components,
        },
    }
    with open(folder / "analisis.json", "w") as f:
        json.dump(metrics, f, indent=2)
    return metrics

# ── Etiquetado manual (segmentación) ──────────────────────────────────────────
def _load_levels():
    """Taxonomía de niveles; cae a la de por defecto si el archivo falta o rompe."""
    try:
        with open(LEVELS_FILE) as f:
            return validate_levels(json.load(f))
    except Exception:
        return default_levels()

class LevelsBody(BaseModel):
    levels: list

class LabelsBody(BaseModel):
    labels: dict  # {"col,row": nivel}

@app.get("/labels/levels")
def get_levels():
    if not LABELING_OK:
        raise HTTPException(503, "Módulo de etiquetado no disponible")
    return {"levels": _load_levels()}

@app.put("/labels/levels")
def put_levels(body: LevelsBody):
    if not LABELING_OK:
        raise HTTPException(503, "Módulo de etiquetado no disponible")
    try:
        levels = validate_levels(body.levels)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with open(LEVELS_FILE, "w") as f:
        json.dump(levels, f, indent=2, ensure_ascii=False)
    return {"levels": levels}

@app.get("/scan/{subdir}/labels")
def get_labels(subdir: str):
    if not LABELING_OK:
        raise HTTPException(503, "Módulo de etiquetado no disponible")
    folder = _scan_dir(subdir)
    path = folder / "etiquetas.csv"
    labels = labels_from_csv(path.read_text()) if path.exists() else {}
    return {"labels": labels_to_json(labels), "count": len(labels)}

@app.put("/scan/{subdir}/labels")
def put_labels(subdir: str, body: LabelsBody):
    if not LABELING_OK:
        raise HTTPException(503, "Módulo de etiquetado no disponible")
    folder = _scan_dir(subdir)
    labels = labels_from_json(body.labels)
    (folder / "etiquetas.csv").write_text(labels_to_csv(labels))
    return {"count": len(labels)}

# ── Calibración blanco/negro (referencias de reflectancia) ────────────────────
def _load_calibration() -> dict:
    try:
        with open(CALIB_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_calibration(data: dict) -> None:
    with open(CALIB_FILE, "w") as f:
        json.dump(data, f, indent=2)

def _load_reference_vectors(n_channels: int):
    """Devuelve (white, dark) como arrays numpy si AMBAS existen y calzan en nº
    de canales con el escaneo; si falta alguna o no calza, devuelve (None, None)
    para que el pipeline caiga a modo adaptado sin romper."""
    data = _load_calibration()
    w, d = data.get("white"), data.get("dark")
    if not (w and d):
        return None, None
    wv = np.asarray(w.get("spectrum", []), dtype=np.float64)
    dv = np.asarray(d.get("spectrum", []), dtype=np.float64)
    if wv.shape[0] != n_channels or dv.shape[0] != n_channels:
        return None, None
    return wv, dv

def _calib_entry(d: Optional[dict]) -> Optional[dict]:
    """Resumen + espectro de una referencia para la UI (o None si no existe)."""
    if not d:
        return None
    sp = d.get("spectrum", [])
    return {
        "ts": d.get("ts"), "exposition": d.get("exposition"), "accum": d.get("accum"),
        "samples": d.get("samples"), "saturated": d.get("saturated", 0),
        "max": max(sp) if sp else 0,
        "spectrum": sp, "wavelengths": d.get("wavelengths", []),
    }

@app.get("/calibration")
def get_calibration():
    data = _load_calibration()
    return {"white": _calib_entry(data.get("white")), "dark": _calib_entry(data.get("dark"))}

@app.post("/calibration/capture")
def calibration_capture(body: CalibCaptureBody):
    if body.kind not in ("white", "dark"):
        raise HTTPException(400, "kind debe ser 'white' o 'dark'")
    if not hw["sensor"]:
        raise HTTPException(409, "Sensor no conectado")
    samples = max(1, min(int(body.samples), 100))
    try:
        acc, sat = [], set()
        for _ in range(samples):
            with _sensor_lock:
                r = hw["sensor"].measure(exposition=body.exposition, accum=body.accum)
            acc.append(r["intensity_raw"])
            sat.update(r.get("saturated_pixels", []))
        arr = np.mean(np.array(acc, dtype=np.float64), axis=0)
        wl = hw["sensor"].wavelength()
    except Exception as e:
        raise HTTPException(500, str(e))

    data = _load_calibration()
    data[body.kind] = {
        "spectrum": [float(v) for v in arr],
        "wavelengths": [float(w) for w in wl],
        "exposition": body.exposition, "accum": body.accum, "samples": samples,
        "saturated": len(sat),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_calibration(data)
    _log(f"Calibración {body.kind}: max={arr.max():.0f}, {len(sat)} px saturados", "ok")
    return {"kind": body.kind, **_calib_entry(data[body.kind])}

@app.delete("/calibration/{kind}")
def calibration_delete(kind: str):
    data = _load_calibration()
    if kind in data:
        del data[kind]
        _save_calibration(data)
    return {"ok": True}

# ── Ambiente: ESP8266 + DHT11 ────────────────────────────────────────────────
# El estado y los helpers viven arriba (junto a scan_st) porque _scan_worker
# los usa. Aquí quedan solo los endpoints.

class TempBody(BaseModel):
    temperature: float
    humidity: float
    device: Optional[str] = None

def _env_append_csv(path, rec, header):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow([rec["ts"], rec["temperature"], rec["humidity"], rec["device"]])

@app.post("/data_temp")
def data_temp_post(body: TempBody):
    rec = {
        "ts":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "temperature": round(float(body.temperature), 2),
        "humidity":    round(float(body.humidity), 2),
        "device":      body.device or "esp8266",
    }
    header = ["ts", "temperature", "humidity", "device"]
    with _env_lock:
        env_st["last"] = rec
        env_st["last_t"] = time.time()
        env_st["history"].append(rec)
        if len(env_st["history"]) > ENV_HISTORY:
            env_st["history"].pop(0)
        scan_dir = env_st["scan_dir"]
        if env_st["run"] is not None:
            _env_run_accum(env_st["run"], rec)
        try:
            _env_append_csv(ENV_FILE, rec, header)
        except Exception as e:
            raise HTTPException(500, f"No se pudo escribir {ENV_FILE}: {e}")
        # Copia dentro del escaneo en curso. Un fallo aquí NO se le devuelve al
        # ESP como error (el histórico global ya quedó guardado): solo se anota.
        if scan_dir:
            try:
                _env_append_csv(os.path.join(scan_dir, ENV_FILE), rec, header)
            except Exception as e:
                print(f"No se pudo escribir el ambiente del escaneo: {e}")
    return {"ok": True, **rec}

@app.get("/data_temp")
def data_temp_get(n: int = 1):
    """Última lectura (con su antigüedad) y las n más recientes."""
    n = max(1, min(int(n), ENV_HISTORY))
    with _env_lock:
        hist = env_st["history"][-n:]
    return {**_env_public(), "history": hist}

@app.get("/scan/{subdir}/env")
def scan_env(subdir: str, max_points: int = 600):
    """Serie ambiental registrada durante un escaneo (su ambiente.csv).

    Submuestrea a ``max_points`` para que una captura larga no mande miles de
    filas a la UI: se conserva 1 de cada k, y siempre la última.
    """
    path = _scan_dir(subdir) / ENV_FILE
    if not path.exists():
        return {"ts": [], "temperature": [], "humidity": [], "n": 0}
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                rows.append((row["ts"], float(row["temperature"]), float(row["humidity"])))
            except (KeyError, TypeError, ValueError):
                continue
    n = len(rows)
    k = max(1, -(-n // max(1, int(max_points))))   # techo de n/max_points
    sub = rows[::k]
    if n and sub[-1] is not rows[-1]:
        sub.append(rows[-1])
    return {
        "ts":          [r[0] for r in sub],
        "temperature": [r[1] for r in sub],
        "humidity":    [r[2] for r in sub],
        "n":           n,
    }

# ── Archivos estáticos (imágenes y CSVs de escaneos) ─────────────────────────
os.makedirs(SCANS_DIR, exist_ok=True)
app.mount("/scans", StaticFiles(directory=SCANS_DIR), name="scans")

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("rpi_server:app", host="0.0.0.0", port=8000, reload=False)
