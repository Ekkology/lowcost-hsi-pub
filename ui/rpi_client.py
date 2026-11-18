"""Cliente REST del escáner: la UI habla con ``rpi_server.py`` por HTTP.

Reemplaza el lanzamiento de subprocesos (``grid-cli.py``) del diseño anterior:
ahora toda la captura ocurre en la Raspberry Pi (sensor real C12880MA + CNC) y
la UI solo consume su API REST. Es el mismo cliente que usaba ``mac_ui.py``,
extraído aquí como única fuente para el paquete ``ui``.
"""

from __future__ import annotations

import json
import os

import requests

# Segundos de espera por llamada HTTP a la Raspberry. Es solo el valor por
# defecto: quien necesite más (descargar el espectros.csv completo) lo pasa
# explícitamente con ``timeout=``.
RPI_TIMEOUT = 5

# Config única del proyecto (antes había scanner_config.json + _mac.json).
CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scanner_config.json",
)

DEFAULT_CONFIG = {
    "rpi_url": "http://raspberrypi.local:8000",
    "cnc_port": "/dev/ttyUSB0",
    "x_start": 10.0, "y_start": 10.0, "z_focus": 15.0,
    "cols": 20, "rows": 20, "step_mm": 1.0,
    "exposition": 50.0, "accum": 3,
    # Potencia de la lámpara en % (la lámpara regula de 10 a 99). Dato anotado
    # a mano: no se mide con el sensor. Se arrastra hasta el config.json de cada
    # captura para poder comparar escaneos con distinta iluminación.
    # 0 = sin especificar.
    "lamp_power_pct": 0.0,
}


def normalize_url(url: str | None) -> str:
    """Devuelve una URL absoluta y sin barra final, o el valor por defecto.

    Importante para las imágenes: la UI compone ``f"{rpi.base}/scans/..."`` y el
    navegador resuelve como RELATIVA cualquier cadena sin esquema, pidiéndosela
    al puerto de la UI (404) en vez de a la Raspberry. Sin esquema se asume
    ``http://``; vacío o inservible cae al valor por defecto.
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return DEFAULT_CONFIG["rpi_url"]
    if "://" not in u:
        u = f"http://{u}"
    return u


class RpiClient:
    """Envoltorio HTTP mínimo sobre la API REST del servidor de la Raspberry."""

    def __init__(self, base: str = DEFAULT_CONFIG["rpi_url"]):
        self.base = base.rstrip("/")

    def get(self, path: str, **kw):
        kw.setdefault("timeout", RPI_TIMEOUT)
        return requests.get(f"{self.base}{path}", **kw)

    def post(self, path: str, **kw):
        kw.setdefault("timeout", RPI_TIMEOUT)
        return requests.post(f"{self.base}{path}", **kw)

    def put(self, path: str, **kw):
        kw.setdefault("timeout", RPI_TIMEOUT)
        return requests.put(f"{self.base}{path}", **kw)

    def delete(self, path: str, **kw):
        kw.setdefault("timeout", RPI_TIMEOUT)
        return requests.delete(f"{self.base}{path}", **kw)


def load_config() -> dict:
    """Carga la config de UI, mezclando con los valores por defecto."""
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    """Persiste solo las claves conocidas de la config."""
    keep = {k: cfg[k] for k in DEFAULT_CONFIG if k in cfg}
    with open(CONFIG_FILE, "w") as f:
        json.dump(keep, f, indent=2)
