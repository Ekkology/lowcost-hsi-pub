"""Preprocesamiento espectral para HSI de bajo costo (Hamamatsu C12880MA).

Adapta el pipeline del artículo:

    "Evaluation of fungal diseases using image analysis coupled to
     spectroscopic technology" — Int. J. Comput. Intell. Syst., 11 (2018) 86-100

al hardware de este proyecto (espectrómetro puntual de 288 canales que escanea
una grilla, un CSV por píxel), en lugar de la cámara de línea Resonon + SpectrononPro
que usa el paper.

Todo está implementado en **numpy puro** (sin scipy ni scikit-learn), siguiendo la
misma filosofía que el resto del repo (p. ej. las funciones CIE están hechas a mano
en ``ui/spectral.py``). Cada paso corresponde a una sección de la guía:

    1. Corrección blanco/negro (Ec. 1)      -> white_dark_correction()
    2. Suavizado Savitzky-Golay (2do grado)  -> savitzky_golay() / smooth_profiles()
    3. Eliminación de perfiles anómalos      -> remove_outlier_profiles()
       (mediana ± 1σ por longitud de onda)
    4. Verificación de redundancia           -> bartlett_sphericity() / kmo()
    5. Reducción de dimensionalidad (PCA)    -> pca()

La función ``preprocess_pipeline()`` encadena los cinco pasos y devuelve un objeto
con los resultados intermedios y un reporte legible.

Uso rápido:

    python3 preprocessing.py --captures-dir captures      # sobre datos reales
    python3 preprocessing.py --demo                        # datos sintéticos
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Calibración, lectura de CSV y carga de datasets vienen del núcleo compartido
# (única fuente de verdad); antes estaban duplicadas aquí.
from spectral_core import NUM_CHANNELS, WAVELENGTHS, ScanData, load_dataset


# ===========================================================================
# 0. Carga de datos (formato grid-cli: x, y, z, channel_0..channel_287)
# ===========================================================================


# El contenedor de datos vive en el núcleo compartido; se mantiene el nombre
# ``SpectralDataset`` como alias retrocompatible.
SpectralDataset = ScanData


def load_captures(captures_dir: str) -> ScanData:
    """Carga capturas en un ScanData, autodetectando el formato.

    Delega en :func:`spectral_core.load_dataset`, que acepta tanto el CSV único
    de escaneo (formato A canónico, rpi_server) como la colección
    ``captura_*.csv`` (formato B legacy). ``captures_dir`` puede ser una carpeta
    o directamente la ruta a un ``espectros.csv``.
    """
    return load_dataset(captures_dir)


def load_reference(filepath: str) -> np.ndarray:
    """Carga un espectro de referencia (blanca W o negra D) desde un CSV.

    Acepta el mismo formato de captura (x, y, z, 288 canales) y devuelve solo
    el vector de 288 intensidades. Si el archivo tiene varias filas (varias
    tomas de la referencia), promedia todas para reducir ruido.
    """
    rows = []
    with open(filepath, "r") as f:
        reader = csv.reader(f)
        next(reader)  # header
        for row in reader:
            if len(row) >= 3 + NUM_CHANNELS:
                rows.append([float(v) for v in row[3 : 3 + NUM_CHANNELS]])
    if not rows:
        raise ValueError(f"El archivo de referencia {filepath} no tiene datos válidos")
    return np.mean(np.array(rows, dtype=np.float64), axis=0)


# ===========================================================================
# 1. Corrección espectral blanco/negro  (Guía §1.3, Ecuación 1)
# ===========================================================================
#
#     P'_ij = (P_ij - D_ij) / (W_ij - D_ij)
#
# El paper usa un panel de Teflón (~99.9%) como blanco W y el lente tapado
# (~0.0%) como negro D. Este proyecto NO tiene esas referencias todavía, así
# que ``white_dark_correction`` funciona en dos modos:
#
#   - Modo completo  (white=... , dark=...):  aplica la Ec. 1 tal cual.
#   - Modo adaptado  (sin referencias):       estima el dark por perfil desde
#     los canales de borde (fuera del visible) y normaliza para obtener una
#     "pseudo-reflectancia" en [0, 1]. Es un stand-in; para resultados
#     comparables al paper hay que capturar W y D reales y pasarlos.
# ===========================================================================


def white_dark_correction(
    profiles: np.ndarray,
    white: Optional[np.ndarray] = None,
    dark: Optional[np.ndarray] = None,
    dark_channels: int = 10,
    eps: float = 1e-8,
) -> np.ndarray:
    """Corrige perfiles a reflectancia según la Ec. 1 de la guía.

    Args:
        profiles: matriz (N, 288) de intensidades crudas.
        white: espectro de referencia blanca W (288,). Si es None, se adapta.
        dark: espectro de referencia negra D (288,). Si es None, se estima.
        dark_channels: nº de canales de borde usados para estimar el dark
            cuando no hay referencia D (ruido base del sensor).
        eps: piso para evitar división por cero cuando W ≈ D.

    Returns:
        Matriz (N, 288) de reflectancia. En modo completo queda ~[0, 1]; en
        modo adaptado se recorta a [0, 1].
    """
    profiles = np.asarray(profiles, dtype=np.float64)

    if white is not None and dark is not None:
        # --- Modo completo: Ecuación 1 exacta ---
        W = np.asarray(white, dtype=np.float64)
        D = np.asarray(dark, dtype=np.float64)
        denom = W - D
        denom = np.where(np.abs(denom) < eps, eps, denom)
        corrected = (profiles - D) / denom
        return corrected

    # --- Modo adaptado: sin referencias físicas ---
    # Dark por perfil = mediana de los primeros canales (fuera del visible),
    # igual criterio que la reconstrucción actual (spectral.py:85).
    D_est = np.median(profiles[:, :dark_channels], axis=1, keepdims=True)
    numer = profiles - D_est
    # "Blanco" estimado = pico de cada perfil (máximo tras restar dark).
    W_est = np.max(numer, axis=1, keepdims=True)
    W_est = np.where(W_est < eps, eps, W_est)
    corrected = numer / W_est
    return np.clip(corrected, 0.0, 1.0)


# ===========================================================================
# 2. Suavizado Savitzky-Golay de 2do grado  (Guía §2.1)
# ===========================================================================


def _savgol_coeffs(window_length: int, polyorder: int, deriv: int = 0) -> np.ndarray:
    """Coeficientes del filtro Savitzky-Golay (implementación numpy pura).

    Ajusta por mínimos cuadrados un polinomio de grado ``polyorder`` a una
    ventana centrada de ``window_length`` puntos y devuelve los coeficientes
    de correlación que producen el valor (o su derivada) en el punto central.
    """
    half = (window_length - 1) // 2
    x = np.arange(-half, half + 1, dtype=np.float64)
    # Matriz de diseño de Vandermonde: columnas x^0, x^1, ..., x^polyorder.
    A = np.vander(x, polyorder + 1, increasing=True)
    # La fila `deriv` de la pseudoinversa da los coeficientes buscados.
    pinv = np.linalg.pinv(A)
    return pinv[deriv] * math.factorial(deriv)


def _savgol_edge_matrix(window_length: int, polyorder: int) -> np.ndarray:
    """Matriz sombrero H = A·pinv(A) (window_length × window_length).

    Aplicada a una ventana devuelve los valores del polinomio ajustado en cada
    posición de esa ventana; sirve para tratar los bordes al estilo de scipy
    ``mode='interp'`` (ajuste polinómico local, sin reflejar la señal).
    """
    half = (window_length - 1) // 2
    x = np.arange(-half, half + 1, dtype=np.float64)
    A = np.vander(x, polyorder + 1, increasing=True)
    return A @ np.linalg.pinv(A)


def _validate_sg(window_length: int, polyorder: int, length: int) -> int:
    if window_length % 2 == 0:
        window_length += 1  # debe ser impar
    if window_length <= polyorder:
        raise ValueError("window_length debe ser mayor que polyorder")
    if window_length > length:
        raise ValueError("window_length no puede superar el largo de la señal")
    return window_length


def savitzky_golay(
    y: np.ndarray, window_length: int = 11, polyorder: int = 2
) -> np.ndarray:
    """Suaviza un espectro 1-D con Savitzky-Golay (2do grado por defecto).

    Interior por convolución con los coeficientes centrales; bordes por ajuste
    polinómico local (equivalente a scipy ``mode='interp'``), de modo que un
    polinomio de grado ≤ polyorder se reproduce exacto en toda la señal.
    """
    y = np.asarray(y, dtype=np.float64)
    window_length = _validate_sg(window_length, polyorder, y.shape[-1])

    coeffs = _savgol_coeffs(window_length, polyorder)  # posición central
    H = _savgol_edge_matrix(window_length, polyorder)
    half = (window_length - 1) // 2
    n = y.shape[0]

    out = np.empty(n, dtype=np.float64)
    windows = np.lib.stride_tricks.sliding_window_view(y, window_length)
    out[half : n - half] = windows @ coeffs
    out[:half] = (H @ y[:window_length])[:half]
    out[n - half :] = (H @ y[-window_length:])[window_length - half :]
    return out


def smooth_profiles(
    profiles: np.ndarray, window_length: int = 11, polyorder: int = 2
) -> np.ndarray:
    """Aplica Savitzky-Golay a cada fila de una matriz (N, 288)."""
    profiles = np.asarray(profiles, dtype=np.float64)
    if profiles.ndim == 1:
        return savitzky_golay(profiles, window_length, polyorder)

    window_length = _validate_sg(window_length, polyorder, profiles.shape[1])

    coeffs = _savgol_coeffs(window_length, polyorder)
    H = _savgol_edge_matrix(window_length, polyorder)
    half = (window_length - 1) // 2
    n = profiles.shape[1]

    out = np.empty_like(profiles)
    windows = np.lib.stride_tricks.sliding_window_view(
        profiles, window_length, axis=1
    )
    out[:, half : n - half] = windows @ coeffs
    out[:, :half] = (profiles[:, :window_length] @ H.T)[:, :half]
    out[:, n - half :] = (profiles[:, -window_length:] @ H.T)[:, window_length - half :]
    return out


# ===========================================================================
# 3. Eliminación de perfiles anómalos  (Guía §2.2)
#    Criterio: mediana ± k·σ por longitud de onda. Si un perfil cae fuera del
#    rango en CUALQUIER λ, se descarta completo. El paper usa k = 1.
# ===========================================================================


def remove_outlier_profiles(
    profiles: np.ndarray, k: float = 1.0, return_mask: bool = False
):
    """Descarta perfiles atípicos según mediana ± k·σ por longitud de onda.

    Args:
        profiles: matriz (N, 288).
        k: nº de desviaciones estándar (paper = 1, más estricto que el clásico 3).
        return_mask: si True, también devuelve la máscara booleana de conservados.

    Returns:
        profiles_filtrados  ó  (profiles_filtrados, keep_mask).
    """
    profiles = np.asarray(profiles, dtype=np.float64)
    median = np.median(profiles, axis=0)  # (288,)
    std = np.std(profiles, axis=0)  # (288,)

    lower = median - k * std
    upper = median + k * std

    inside = (profiles >= lower) & (profiles <= upper)  # (N, 288)
    keep = np.all(inside, axis=1)  # conservar solo si cabe en TODAS las λ

    filtered = profiles[keep]
    if return_mask:
        return filtered, keep
    return filtered


# ===========================================================================
# 4. Verificación de redundancia  (Guía §3.2)
#    Bartlett (esfericidad) + KMO (adecuación muestral).
# ===========================================================================


def _gammaln(x: float) -> float:
    return math.lgamma(x)


def _chi2_sf(x: float, dof: float) -> float:
    """Función de supervivencia de la chi-cuadrado, P(X > x).

    Equivale a la gamma incompleta superior regularizada Q(dof/2, x/2),
    implementada con serie / fracción continua (Numerical Recipes) para no
    depender de scipy.
    """
    if x <= 0:
        return 1.0
    a = dof / 2.0
    xx = x / 2.0
    if xx < a + 1.0:
        # Serie para P(a, x); Q = 1 - P.
        ap = a
        total = 1.0 / a
        term = total
        for _ in range(1000):
            ap += 1.0
            term *= xx / ap
            total += term
            if abs(term) < abs(total) * 1e-14:
                break
        p = total * math.exp(-xx + a * math.log(xx) - _gammaln(a))
        return 1.0 - p
    # Fracción continua para Q(a, x) directamente.
    b = xx + 1.0 - a
    c = 1e300
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < 1e-300:
            d = 1e-300
        c = b + an / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    q = math.exp(-xx + a * math.log(xx) - _gammaln(a)) * h
    return q


@dataclass
class BartlettResult:
    chi_square: float
    dof: float
    p_value: float
    singular: bool  # True si R es casi singular (colinealidad extrema)


def bartlett_sphericity(profiles: np.ndarray) -> BartlettResult:
    """Test de esfericidad de Bartlett sobre la matriz de correlación.

    H0: las variables (longitudes de onda) no están correlacionadas. Un p-valor
    ≈ 0 (como en el paper) rechaza H0 → hay redundancia que PCA puede resumir.

    En datos espectrales las bandas vecinas están fuertemente correlacionadas,
    por lo que det(R) suele ser ~0 (R casi singular). En ese caso el estadístico
    tiende a infinito y el p-valor a 0, exactamente lo que reporta el paper.
    """
    profiles = np.asarray(profiles, dtype=np.float64)
    n, p = profiles.shape
    R = np.corrcoef(profiles, rowvar=False)
    # Eliminar posibles NaN de columnas con varianza cero.
    R = np.nan_to_num(R, nan=0.0)
    np.fill_diagonal(R, 1.0)

    sign, logdet = np.linalg.slogdet(R)
    dof = p * (p - 1) / 2.0

    if sign <= 0 or not np.isfinite(logdet):
        # R singular / no definida positiva -> colinealidad extrema.
        return BartlettResult(float("inf"), dof, 0.0, singular=True)

    chi_square = -(n - 1 - (2 * p + 5) / 6.0) * logdet
    p_value = _chi2_sf(chi_square, dof)
    return BartlettResult(chi_square, dof, p_value, singular=False)


def kmo(profiles: np.ndarray) -> tuple[np.ndarray, float]:
    """Índice Kaiser-Meyer-Olkin (adecuación muestral para PCA/factorial).

    Devuelve (kmo_por_variable, kmo_global). Escala 0-1; el paper obtuvo 0.9813
    ("muy adecuado"). Usa la pseudoinversa de R para tolerar colinealidad.
    """
    profiles = np.asarray(profiles, dtype=np.float64)
    R = np.corrcoef(profiles, rowvar=False)
    R = np.nan_to_num(R, nan=0.0)
    np.fill_diagonal(R, 1.0)

    R_inv = np.linalg.pinv(R)
    d = np.sqrt(np.diag(R_inv))
    denom = np.outer(d, d)
    denom = np.where(denom < 1e-12, 1e-12, denom)
    partial = -R_inv / denom  # matriz de correlaciones parciales
    np.fill_diagonal(partial, 0.0)

    R_off = R.copy()
    np.fill_diagonal(R_off, 0.0)

    sum_r2 = np.sum(R_off**2)
    sum_p2 = np.sum(partial**2)

    kmo_total = sum_r2 / (sum_r2 + sum_p2) if (sum_r2 + sum_p2) > 0 else 0.0

    r2_col = np.sum(R_off**2, axis=0)
    p2_col = np.sum(partial**2, axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        kmo_per_var = np.where(
            (r2_col + p2_col) > 0, r2_col / (r2_col + p2_col), 0.0
        )
    return kmo_per_var, float(kmo_total)


# ===========================================================================
# 5. Reducción de dimensionalidad — PCA  (Guía §3.3)
# ===========================================================================


@dataclass
class PCAResult:
    scores: np.ndarray  # (N, n_components) — perfiles en el espacio reducido
    components: np.ndarray  # (n_components, 288) — vectores propios (loadings)
    explained_variance_ratio: np.ndarray  # (n_components,)
    mean: np.ndarray  # (288,) — media usada para centrar

    @property
    def cumulative_variance(self) -> float:
        return float(np.sum(self.explained_variance_ratio))


def pca(profiles: np.ndarray, n_components: int = 2) -> PCAResult:
    """PCA por SVD (numpy puro), ordenado por varianza explicada.

    El paper obtiene >99.1% de varianza con solo 2 componentes; con datos
    espectrales muy correlacionados es normal ver ese comportamiento.
    """
    X = np.asarray(profiles, dtype=np.float64)
    mean = X.mean(axis=0)
    Xc = X - mean

    # SVD económica: Xc = U · diag(S) · Vt
    U, S, Vt = np.linalg.svd(Xc, full_matrices=False)

    n = X.shape[0]
    explained_variance = (S**2) / max(n - 1, 1)
    total = explained_variance.sum()
    ratio = explained_variance / total if total > 0 else explained_variance

    k = min(n_components, Vt.shape[0])
    components = Vt[:k]
    scores = Xc @ components.T

    return PCAResult(
        scores=scores,
        components=components,
        explained_variance_ratio=ratio[:k],
        mean=mean,
    )


# ===========================================================================
# Pasos componibles  (patrón Transform, análogo al Strategy de Sensor/)
# ---------------------------------------------------------------------------
# Cada paso encapsula una de las funciones puras de arriba tras una interfaz
# común ``apply(X) -> X'``. Así el pipeline es una lista reordenable/extensible
# (mismo espíritu que scikit-learn Pipeline, pero en numpy puro).
# ===========================================================================


class Transform:
    """Base de un paso de preprocesamiento. Subclases implementan ``apply``."""

    name: str = "transform"

    def apply(self, X: np.ndarray) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    def __call__(self, X: np.ndarray) -> np.ndarray:
        return self.apply(X)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"


@dataclass
class WhiteDarkCorrection(Transform):
    """Paso 1: corrección blanco/negro (Ec. 1)."""

    white: Optional[np.ndarray] = None
    dark: Optional[np.ndarray] = None
    dark_channels: int = 10
    name: str = "correccion_blanco_negro"

    def apply(self, X: np.ndarray) -> np.ndarray:
        return white_dark_correction(
            X, white=self.white, dark=self.dark, dark_channels=self.dark_channels
        )


@dataclass
class SavitzkyGolay(Transform):
    """Paso 2: suavizado Savitzky-Golay."""

    window: int = 11
    polyorder: int = 2
    name: str = "savitzky_golay"

    def apply(self, X: np.ndarray) -> np.ndarray:
        return smooth_profiles(X, self.window, self.polyorder)


@dataclass
class OutlierRemoval(Transform):
    """Paso 3: eliminación de perfiles anómalos (mediana ± k·σ)."""

    k: float = 1.0
    name: str = "eliminacion_anomalos"
    last_mask: Optional[np.ndarray] = None  # máscara de la última corrida

    def apply(self, X: np.ndarray) -> np.ndarray:
        clean, keep = remove_outlier_profiles(X, k=self.k, return_mask=True)
        self.last_mask = keep
        return clean


@dataclass
class PCAStep(Transform):
    """Paso 5: reducción de dimensionalidad con PCA."""

    n_components: int = 2
    name: str = "pca"
    last_result: Optional["PCAResult"] = None  # se llena al aplicar

    def apply(self, X: np.ndarray) -> np.ndarray:
        self.last_result = pca(X, self.n_components)
        return self.last_result.scores


class Pipeline:
    """Encadena una lista de ``Transform`` y los aplica en orden.

    Ejemplo:
        pipe = Pipeline([WhiteDarkCorrection(), SavitzkyGolay(window=9),
                         OutlierRemoval(k=1.5), PCAStep(n_components=3)])
        scores = pipe.run(profiles)
    """

    def __init__(self, steps: list[Transform]):
        self.steps = steps

    def run(self, X: np.ndarray, record: bool = False):
        """Aplica los pasos en secuencia.

        Si ``record`` es True devuelve también la lista de (nombre, salida) de
        cada etapa, útil para inspeccionar/graficar los intermedios.
        """
        out = np.asarray(X, dtype=np.float64)
        history = []
        for step in self.steps:
            out = step.apply(out)
            if record:
                history.append((step.name, out))
        return (out, history) if record else out

    def __repr__(self) -> str:
        return "Pipeline([\n  " + ",\n  ".join(repr(s) for s in self.steps) + "\n])"


# ===========================================================================
# Configuración centralizada
# ---------------------------------------------------------------------------
# Todos los parámetros del pipeline en un solo lugar (antes andaban sueltos y
# repetidos en varias firmas). Serializable, lo que facilita reproducir una
# corrida y comparar contra el paper.
# ===========================================================================


@dataclass
class PreprocessConfig:
    """Parámetros del pipeline de preprocesamiento (Guía §4)."""

    dark_channels: int = 10   # canales de borde para estimar dark (sin ref. D)
    sg_window: int = 11       # ventana Savitzky-Golay (se fuerza a impar)
    sg_polyorder: int = 2     # grado del polinomio SG (paper: 2do grado)
    outlier_k: float = 1.0    # nº de σ para el filtro de anómalos (paper: 1)
    n_components: int = 2     # componentes PCA a retener (paper: 2 -> 99.1%)


def build_default_pipeline(
    config: Optional[PreprocessConfig] = None,
    white: Optional[np.ndarray] = None,
    dark: Optional[np.ndarray] = None,
) -> "Pipeline":
    """Construye el pipeline de transformación de la guía (hasta antes de PCA).

    PCA queda fuera porque va precedido de los diagnósticos Bartlett/KMO, que
    deciden si conviene reducir dimensión.
    """
    config = config or PreprocessConfig()
    return Pipeline([
        WhiteDarkCorrection(
            white=white, dark=dark, dark_channels=config.dark_channels
        ),
        SavitzkyGolay(window=config.sg_window, polyorder=config.sg_polyorder),
        OutlierRemoval(k=config.outlier_k),
    ])


# ===========================================================================
# Pipeline completo  (Guía §4)
# ===========================================================================


@dataclass
class PipelineResult:
    corrected: np.ndarray  # tras corrección blanco/negro
    smoothed: np.ndarray  # tras Savitzky-Golay
    inlier_mask: np.ndarray  # True = perfil conservado (índices del original)
    clean: np.ndarray  # perfiles limpios (sin anómalos)
    bartlett: BartlettResult
    kmo_total: float
    pca: PCAResult
    n_input: int
    n_kept: int

    def report(self) -> str:
        """Reporte legible del pipeline, paso a paso."""
        pct_removed = 100.0 * (self.n_input - self.n_kept) / max(self.n_input, 1)
        lines = [
            "=== Preprocesamiento espectral (pipeline HSI) ===",
            f"1-2. Corrección + suavizado: {self.n_input} perfiles de "
            f"{self.corrected.shape[1]} canales",
            f"3.   Eliminación de anómalos (mediana ± 1σ): "
            f"{self.n_input} → {self.n_kept} perfiles "
            f"({pct_removed:.1f}% descartados)",
            f"4.   Bartlett: χ² = {self.bartlett.chi_square:.4g}, "
            f"dof = {self.bartlett.dof:.0f}, p = {self.bartlett.p_value:.4g}"
            + ("  [R casi singular → colinealidad extrema]" if self.bartlett.singular else ""),
            f"     KMO global = {self.kmo_total:.4f}",
            f"5.   PCA: {len(self.pca.explained_variance_ratio)} componentes explican "
            f"{100 * self.pca.cumulative_variance:.2f}% de la varianza",
        ]
        for idx, r in enumerate(self.pca.explained_variance_ratio):
            lines.append(f"       PC{idx + 1}: {100 * r:.2f}%")
        return "\n".join(lines)


def preprocess_pipeline(
    profiles: np.ndarray,
    white: Optional[np.ndarray] = None,
    dark: Optional[np.ndarray] = None,
    config: Optional[PreprocessConfig] = None,
) -> PipelineResult:
    """Encadena los 5 pasos de la guía sobre una matriz (N, 288) de perfiles.

    Orden (Guía §4): corrección blanco/negro → Savitzky-Golay → eliminación de
    anómalos → Bartlett/KMO → PCA. Los parámetros van en ``config``
    (:class:`PreprocessConfig`); si es None se usan los valores del paper.
    """
    config = config or PreprocessConfig()
    profiles = np.asarray(profiles, dtype=np.float64)
    n_input = profiles.shape[0]

    if white is None or dark is None:
        logger.warning(
            "Sin referencias W/D completas: la corrección usa el modo adaptado "
            "(pseudo-reflectancia). Para resultados comparables al paper, pasá "
            "las referencias blanca y negra."
        )

    # Construido sobre los pasos componibles; la eliminación de anómalos guarda
    # su máscara en el propio paso.
    pipe = build_default_pipeline(config, white=white, dark=dark)
    _, history = pipe.run(profiles, record=True)
    corrected = history[0][1]
    smoothed = history[1][1]
    clean = history[2][1]
    keep = pipe.steps[2].last_mask

    logger.info(
        "Anómalos: %d → %d perfiles conservados (k=%.2f)",
        n_input, clean.shape[0], config.outlier_k,
    )

    if clean.shape[0] < 2:
        raise ValueError(
            "Quedaron muy pocos perfiles tras la eliminación de anómalos "
            f"({clean.shape[0]}). Probá con un k mayor."
        )

    bartlett = bartlett_sphericity(clean)
    _, kmo_total = kmo(clean)
    pca_result = pca(clean, n_components=config.n_components)

    return PipelineResult(
        corrected=corrected,
        smoothed=smoothed,
        inlier_mask=keep,
        clean=clean,
        bartlett=bartlett,
        kmo_total=kmo_total,
        pca=pca_result,
        n_input=n_input,
        n_kept=clean.shape[0],
    )


# ===========================================================================
# Demo / CLI
# ===========================================================================


def _synthetic_dataset(n_per_class: int = 400, seed: int = 0) -> np.ndarray:
    """Genera perfiles espectrales sintéticos con 4 'niveles de daño' para demo."""
    rng = np.random.default_rng(seed)
    wl = WAVELENGTHS
    profiles = []
    # Cada 'nivel' desplaza ligeramente la firma en el rojo/NIR (como la roya).
    for level, (amp, shift) in enumerate(
        [(1.00, 0.0), (0.95, 12.0), (0.85, 28.0), (0.70, 45.0)]
    ):
        base = amp * np.exp(-0.5 * ((wl - (720 + shift)) / 90.0) ** 2)
        base += 0.3 * np.exp(-0.5 * ((wl - 550) / 40.0) ** 2)
        for _ in range(n_per_class):
            noise = rng.normal(0, 0.02, size=wl.shape)
            # Ruido de alta frecuencia (lo que ataca Savitzky-Golay).
            hf = 0.01 * rng.normal(0, 1, size=wl.shape)
            profiles.append(np.clip(base + noise + hf, 0, None))
    prof = np.array(profiles)
    # Inyectar algunos outliers (perfiles corruptos).
    idx = rng.choice(len(prof), size=max(1, len(prof) // 50), replace=False)
    prof[idx] += rng.normal(0, 0.5, size=(len(idx), wl.shape[0]))
    return prof


def _save_pca_plot(result: PipelineResult, out_path: Path) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    scores = result.pca.scores
    fig, ax = plt.subplots(figsize=(7, 6))
    if scores.shape[1] >= 2:
        ax.scatter(scores[:, 0], scores[:, 1], s=6, alpha=0.5)
        ax.set_xlabel(
            f"PC1 ({100 * result.pca.explained_variance_ratio[0]:.1f}%)"
        )
        ax.set_ylabel(
            f"PC2 ({100 * result.pca.explained_variance_ratio[1]:.1f}%)"
        )
    else:
        ax.hist(scores[:, 0], bins=40)
        ax.set_xlabel("PC1")
    ax.set_title("Perfiles en el espacio PCA")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return True


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--captures-dir",
        default=None,
        help="Ruta a un espectros.csv (formato A) o a una carpeta con "
        "espectros.csv / captura_*.csv. Si se omite (o no existe), usa --demo.",
    )
    p.add_argument(
        "--white", default=None, help="CSV de referencia blanca W (opcional)."
    )
    p.add_argument(
        "--dark", default=None, help="CSV de referencia negra D (opcional)."
    )
    p.add_argument("--demo", action="store_true", help="Usar datos sintéticos.")
    p.add_argument("--sg-window", type=int, default=11)
    p.add_argument("--outlier-k", type=float, default=1.0)
    p.add_argument("--n-components", type=int, default=2)
    p.add_argument(
        "--plot",
        default=None,
        help="Ruta para guardar el scatter de PCA (requiere matplotlib).",
    )
    p.add_argument(
        "-q", "--quiet", action="store_true",
        help="Solo mostrar advertencias y errores.",
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )

    config = PreprocessConfig(
        sg_window=args.sg_window,
        outlier_k=args.outlier_k,
        n_components=args.n_components,
    )

    white = load_reference(args.white) if args.white else None
    dark = load_reference(args.dark) if args.dark else None

    if args.captures_dir and os.path.exists(args.captures_dir) and not args.demo:
        ds = load_captures(args.captures_dir)
        profiles = ds.profiles
        logger.info("Cargados %d perfiles desde %s", len(ds), args.captures_dir)
    else:
        profiles = _synthetic_dataset()
        logger.info("Modo DEMO: %d perfiles sintéticos generados", profiles.shape[0])

    result = preprocess_pipeline(profiles, white=white, dark=dark, config=config)
    logger.info("\n%s", result.report())

    if args.plot:
        ok = _save_pca_plot(result, Path(args.plot))
        if ok:
            logger.info("Gráfica PCA guardada en %s", args.plot)
        else:
            logger.warning("matplotlib no disponible; se omitió la gráfica.")


if __name__ == "__main__":
    main()
