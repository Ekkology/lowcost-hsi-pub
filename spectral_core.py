"""Núcleo espectral compartido — única fuente de verdad del proyecto.

Reúne el conocimiento espectral que antes estaba **duplicado** en varios
módulos (el polinomio de calibración, las funciones CIE 1931, la conversión
a sRGB y la lectura de CSV de captura). No depende de hardware ni de la UI,
por lo que puede importarse desde cualquier parte:

    from spectral_core import WAVELENGTHS, spectrum_to_rgb, read_capture

Antes de esta extracción, ``WAVELENGTHS`` estaba escrito 4 veces
(libC12880MA.py, reconstruct_image.py, ui/spectral.py y preprocessing.py) y
las funciones CIE / la lectura de CSV estaban repetidas en 2-3 sitios. Aquí
viven una sola vez; el resto de módulos importa desde acá.
"""

from __future__ import annotations

import csv
import glob
import os
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Calibración de longitud de onda del Hamamatsu C12880MA
# (polinomio de 5º grado de la hoja de datos / libC12880MA.py).
# ---------------------------------------------------------------------------
NUM_CHANNELS = 288
_i = np.arange(NUM_CHANNELS)
WAVELENGTHS: np.ndarray = (
    3.11487118e2
    + 2.704770136 * _i
    - 1.198161774e-3 * _i**2
    - 7.333264294e-6 * _i**3
    + 8.195191046e-9 * _i**4
    + 6.104509768e-12 * _i**5
)

# Rango visible usado para reconstruir color.
VISIBLE_MIN_NM = 380.0
VISIBLE_MAX_NM = 780.0


# ---------------------------------------------------------------------------
# Funciones de igualación de color CIE 1931 (Wyman, Sloan & Shirley 2013).
# ---------------------------------------------------------------------------

def _gauss(x: np.ndarray, mu: float, s1: float, s2: float) -> np.ndarray:
    """Gaussiana asimétrica: ancho s1 a la izquierda de mu, s2 a la derecha."""
    t = np.where(x < mu, s1, s2)
    return np.exp(-0.5 * ((x - mu) / t) ** 2)


def cie_x_bar(wl: np.ndarray) -> np.ndarray:
    return (1.056 * _gauss(wl, 599.8, 37.9, 31.0)
            + 0.362 * _gauss(wl, 442.0, 16.0, 26.7)
            - 0.065 * _gauss(wl, 501.1, 20.4, 26.2))


def cie_y_bar(wl: np.ndarray) -> np.ndarray:
    return (0.821 * _gauss(wl, 568.8, 46.9, 40.5)
            + 0.286 * _gauss(wl, 530.9, 16.3, 31.1))


def cie_z_bar(wl: np.ndarray) -> np.ndarray:
    return (1.217 * _gauss(wl, 437.0, 11.8, 36.0)
            + 0.681 * _gauss(wl, 459.0, 26.0, 13.8))


# ---------------------------------------------------------------------------
# Conversión XYZ -> sRGB (D65).
# ---------------------------------------------------------------------------

XYZ_TO_SRGB = np.array([
    [ 3.2404542, -1.5371385, -0.4985314],
    [-0.9692660,  1.8760108,  0.0415560],
    [ 0.0556434, -0.2040259,  1.0572252],
])


def srgb_gamma(c: np.ndarray) -> np.ndarray:
    """Curva gamma sRGB."""
    return np.where(
        c <= 0.0031308,
        12.92 * c,
        1.055 * np.power(np.clip(c, 0.0031308, None), 1.0 / 2.4) - 0.055,
    )


def spectrum_to_rgb(channels: list | np.ndarray) -> Tuple[int, int, int]:
    """Convierte un espectro de 288 canales a un color (R, G, B) en 0-255.

    Hace resta de dark (mediana de los primeros 10 canales), integra con las
    funciones CIE 1931, pasa XYZ->sRGB con gamma y normaliza por luminancia.
    """
    intensities = np.asarray(channels, dtype=np.float64)

    # Resta de dark: mediana de los primeros canales (fuera del visible).
    dark = np.median(intensities[:10])
    intensities = np.clip(intensities - dark, 0, None)

    mask = (WAVELENGTHS >= VISIBLE_MIN_NM) & (WAVELENGTHS <= VISIBLE_MAX_NM)
    wl = WAVELENGTHS[mask]
    spec = intensities[mask]

    if len(wl) < 2:
        return (0, 0, 0)

    dlambda = np.gradient(wl)

    X = np.sum(spec * cie_x_bar(wl) * dlambda)
    Y = np.sum(spec * cie_y_bar(wl) * dlambda)
    Z = np.sum(spec * cie_z_bar(wl) * dlambda)

    xyz = np.array([X, Y, Z])
    if Y > 0:
        xyz = xyz / Y  # normalizar por luminancia

    rgb_linear = XYZ_TO_SRGB @ xyz
    rgb_linear = np.clip(rgb_linear, 0, None)

    mx = rgb_linear.max()
    if mx > 1.0:
        rgb_linear /= mx

    rgb_8 = np.clip(srgb_gamma(rgb_linear) * 255, 0, 255).astype(int)
    return (int(rgb_8[0]), int(rgb_8[1]), int(rgb_8[2]))


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convierte una tupla RGB a string hex tipo '#ff8800'."""
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# Lectura de CSV de captura (formato grid-cli: x, y, z, channel_0..channel_287).
# ---------------------------------------------------------------------------

@dataclass
class CaptureData:
    """Una captura puntual: coordenadas físicas + espectro de 288 canales."""

    x: float
    y: float
    z: float
    channels: list[float]


def read_capture(filepath: str) -> CaptureData:
    """Lee un CSV de una sola fila (x, y, z, channel_0..channel_287)."""
    with open(filepath, "r") as f:
        reader = csv.reader(f)
        next(reader)  # header
        row = next(reader)
    return CaptureData(
        x=float(row[0]),
        y=float(row[1]),
        z=float(row[2]),
        channels=[float(v) for v in row[3:]],
    )


# Alias retrocompatible con el nombre que usaba ui/spectral.py.
parse_capture_csv = read_capture


# ===========================================================================
# Lector unificado de datasets (formato canónico A + legacy B)
# ---------------------------------------------------------------------------
# Formato A (canónico, lo produce rpi_server.py): un único CSV por escaneo,
# una fila por punto, con cabecera
#     col,row,x_mm,y_mm,z_mm,<λ0>nm,<λ1>nm,...
# Guarda los índices de grilla (col,row) y el eje espectral explícitos, así que
# la reconstrucción no necesita re-agrupar coordenadas.
#
# Formato B (legacy, grid-cli): un CSV por punto (captura_XXXX.csv) con cabecera
#     x,y,z,channel_0..channel_287
# Se sigue leyendo por compatibilidad, ensamblándolo en el mismo contenedor.
# ===========================================================================


@dataclass
class ScanData:
    """Dataset espectral: N puntos, cada uno con su espectro y su posición.

    - ``profiles``: matriz (N, C) de intensidades, una fila por punto.
    - ``coords``: matriz (N, 3) con las coordenadas físicas (x, y, z) en mm.
    - ``wavelengths``: vector (C,) con la longitud de onda de cada canal (nm).
    - ``grid_index``: matriz (N, 2) de enteros (col, row) cuando el formato la
      trae (formato A); ``None`` en el formato B legacy (hay que inferir la
      grilla desde ``coords``).
    - ``files``: rutas de origen.
    """

    profiles: np.ndarray
    coords: np.ndarray
    wavelengths: np.ndarray
    grid_index: Optional[np.ndarray] = None
    files: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return self.profiles.shape[0]

    @property
    def n_cols(self) -> int:
        if self.grid_index is None or len(self) == 0:
            return 0
        return int(self.grid_index[:, 0].max()) + 1

    @property
    def n_rows(self) -> int:
        if self.grid_index is None or len(self) == 0:
            return 0
        return int(self.grid_index[:, 1].max()) + 1


# Nombres de columna que se interpretan como metadatos (no espectro). Se admiten
# variantes con y sin sufijo _mm para las coordenadas.
_META_ALIASES = {
    "col": "col", "row": "row",
    "x": "x", "x_mm": "x",
    "y": "y", "y_mm": "y",
    "z": "z", "z_mm": "z",
}


def _parse_wavelength_header(name: str) -> Optional[float]:
    """Extrae la longitud de onda (nm) de una cabecera tipo '340.12nm'.

    Devuelve ``None`` si la columna no codifica un nm explícito (p. ej.
    ``channel_0``), en cuyo caso el lector recurre a la calibración WAVELENGTHS.
    """
    n = name.strip().lower()
    if n.endswith("nm"):
        try:
            return float(n[:-2])
        except ValueError:
            return None
    return None


def read_scan(filepath: str) -> ScanData:
    """Lee un CSV de escaneo completo (formato A, una fila por punto).

    Clasifica las columnas por nombre: ``col,row,x,y,z`` (con o sin ``_mm``) son
    metadatos; el resto son canales espectrales. Si las cabeceras espectrales
    traen el nm (``...nm``) se usa ese eje; si no (p. ej. ``channel_i``) y hay
    288 canales, se usa la calibración del C12880MA.
    """
    with open(filepath, "r", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [r for r in reader if r]

    meta_idx: dict[str, int] = {}
    spec_cols: list[int] = []
    spec_wl: list[Optional[float]] = []
    for i, name in enumerate(header):
        key = _META_ALIASES.get(name.strip().lower())
        if key is not None and key not in meta_idx:
            meta_idx[key] = i
        else:
            spec_cols.append(i)
            spec_wl.append(_parse_wavelength_header(name))

    if not spec_cols:
        raise ValueError(f"{filepath}: no se encontraron columnas espectrales")
    if not rows:
        raise ValueError(f"{filepath}: el archivo no tiene filas de datos")

    profiles = np.array(
        [[float(r[i]) for i in spec_cols] for r in rows], dtype=np.float64
    )

    def _coord(axis: str) -> np.ndarray:
        idx = meta_idx.get(axis)
        if idx is None:
            return np.zeros(len(rows), dtype=np.float64)
        return np.array([float(r[idx]) for r in rows], dtype=np.float64)

    coords = np.column_stack([_coord("x"), _coord("y"), _coord("z")])

    grid_index = None
    if "col" in meta_idx and "row" in meta_idx:
        ci, ri = meta_idx["col"], meta_idx["row"]
        grid_index = np.array(
            [[int(float(r[ci])), int(float(r[ri]))] for r in rows], dtype=int
        )

    if all(w is not None for w in spec_wl):
        wavelengths = np.array(spec_wl, dtype=np.float64)
    elif len(spec_cols) == NUM_CHANNELS:
        wavelengths = WAVELENGTHS.copy()
    else:
        # Sin nm en cabecera y nº de canales no estándar: índice como fallback.
        wavelengths = np.arange(len(spec_cols), dtype=np.float64)

    return ScanData(
        profiles=profiles,
        coords=coords,
        wavelengths=wavelengths,
        grid_index=grid_index,
        files=[filepath],
    )


def _load_capture_files(files: list[str]) -> ScanData:
    """Ensambla varios captura_*.csv (formato B) en un único ScanData."""
    profiles, coords = [], []
    for fpath in files:
        cap = read_capture(fpath)
        profiles.append(np.asarray(cap.channels, dtype=np.float64))
        coords.append((cap.x, cap.y, cap.z))
    return ScanData(
        profiles=np.vstack(profiles),
        coords=np.array(coords, dtype=np.float64),
        wavelengths=WAVELENGTHS.copy(),
        grid_index=None,
        files=list(files),
    )


def load_dataset(path: str) -> ScanData:
    """Carga un dataset espectral autodetectando el formato.

    - ``path`` a un archivo → CSV de escaneo (formato A).
    - ``path`` a una carpeta con ``espectros.csv`` (directo o en una única
      subcarpeta ``scan_*``) → formato A.
    - ``path`` a una carpeta con ``captura_*.csv`` → formato B legacy,
      ensamblado en un solo :class:`ScanData`.
    """
    if os.path.isfile(path):
        return read_scan(path)

    if os.path.isdir(path):
        direct = os.path.join(path, "espectros.csv")
        if os.path.isfile(direct):
            return read_scan(direct)

        nested = sorted(glob.glob(os.path.join(path, "*", "espectros.csv")))
        if len(nested) == 1:
            return read_scan(nested[0])
        if len(nested) > 1:
            raise ValueError(
                f"Se encontraron {len(nested)} escaneos en {path}; indicá el "
                "archivo espectros.csv concreto a analizar."
            )

        legacy = sorted(glob.glob(os.path.join(path, "captura_*.csv")))
        if legacy:
            return _load_capture_files(legacy)

    raise FileNotFoundError(
        f"No se encontró ni 'espectros.csv' (formato A) ni 'captura_*.csv' "
        f"(formato B) en {path}"
    )
