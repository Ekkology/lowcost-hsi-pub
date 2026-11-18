"""
legacy_driver.py — Strategy concreta que envuelve Sensor/libC12880MA.

Diferencias respecto al driver original:
  - measure() retorna dict normalizado (igual que ImprovedDriver)
  - Promedia correctamente: libC12880MA acumula sumando, aquí se divide
"""
from typing import Any, Dict, List

from Sensor.libC12880MA import pi4_hama as _LibHama
from Sensor.strategy import SpectrometerStrategy

_ADS1115_MAX = 32767
_SAT_THRESHOLD = 0.90


class LegacyDriver(SpectrometerStrategy):

    def __init__(self):
        self._hama = _LibHama()

    @property
    def name(self) -> str:
        return "legacy"

    def initialize(self) -> None:
        # libC12880MA no tiene initialize(); se descarta la primera lectura manualmente
        self._hama.measure(1e-6, 1)

    def wavelength(self) -> List[float]:
        return self._hama.wavelength().tolist()

    def measure(self, exposition: float, accum: int) -> Dict[str, Any]:
        raw_sum = self._hama.measure(exposition, accum)

        # libC12880MA retorna la suma de 'accum' scans; normalizar a promedio
        intensity = [v / accum for v in raw_sum]

        threshold = _ADS1115_MAX * _SAT_THRESHOLD
        saturated = [i for i, v in enumerate(intensity) if v >= threshold]

        return {
            "intensity_raw": intensity,
            "intensity_corrected": intensity,
            "saturated_pixels": saturated,
        }

    def cleanup(self) -> None:
        self._hama.cleanup()
