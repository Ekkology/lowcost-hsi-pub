"""
strategy.py — Interfaz abstracta para drivers del espectrómetro C12880MA.

Patrón Strategy: permite intercambiar LegacyDriver (libC12880MA)
e ImprovedDriver (pi4_hama) sin modificar el código del cliente (app.py).
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List


class SpectrometerStrategy(ABC):
    """Interfaz común para todos los drivers del C12880MA."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Nombre identificador del driver."""
        ...

    @abstractmethod
    def initialize(self) -> None:
        """Warm-up inicial del sensor (descarta el primer scan ruidoso)."""
        ...

    @abstractmethod
    def wavelength(self) -> List[float]:
        """Retorna lista de 288 longitudes de onda en nm."""
        ...

    @abstractmethod
    def measure(self, exposition: float, accum: int) -> Dict[str, Any]:
        """Realiza una medición espectral.

        Retorna dict con al menos:
            intensity_raw        : List[float] — 288 valores promediados sin corrección
            intensity_corrected  : List[float] — raw - dark (o raw si no hay dark)
            saturated_pixels     : List[int]   — índices de píxeles saturados
        """
        ...

    @abstractmethod
    def cleanup(self) -> None:
        """Libera recursos GPIO."""
        ...
