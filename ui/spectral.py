"""Utilidades espectrales para la UI.

Este módulo pasó a ser un *shim* delgado: toda la lógica (calibración de
longitud de onda, funciones CIE 1931, conversión a sRGB y lectura de CSV)
vive ahora en ``spectral_core.py``, la única fuente de verdad del proyecto.

Se re-exportan los nombres públicos para no romper los imports existentes:

    from ui.spectral import WAVELENGTHS, spectrum_to_rgb, rgb_to_hex, parse_capture_csv
"""

from __future__ import annotations

from spectral_core import (  # noqa: F401  (re-exportados intencionalmente)
    NUM_CHANNELS,
    WAVELENGTHS,
    CaptureData,
    cie_x_bar,
    cie_y_bar,
    cie_z_bar,
    parse_capture_csv,
    rgb_to_hex,
    spectrum_to_rgb,
)

__all__ = [
    "NUM_CHANNELS",
    "WAVELENGTHS",
    "CaptureData",
    "cie_x_bar",
    "cie_y_bar",
    "cie_z_bar",
    "parse_capture_csv",
    "rgb_to_hex",
    "spectrum_to_rgb",
]
