"""
improved_driver.py — Strategy concreta que envuelve pi4_hama (raíz del proyecto).

Características adicionales respecto a LegacyDriver:
  - Dark subtraction con scan de descarte al inicializar (datasheet)
  - Detección de saturación por píxel integrada en el driver
  - Promedio correcto desde el origen (no suma)
"""
import os
import sys
from typing import Any, Dict, List

from Sensor.strategy import SpectrometerStrategy

# pi4_hama.py vive en la raíz del proyecto, un nivel arriba de Sensor/
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pi4_hama import pi4_hama as _Hama  # noqa: E402


class ImprovedDriver(SpectrometerStrategy):

    def __init__(self):
        self._hama = _Hama()

    @property
    def name(self) -> str:
        return "improved"

    def initialize(self) -> None:
        self._hama.initialize()

    def wavelength(self) -> List[float]:
        return self._hama.wavelength()

    def measure(self, exposition: float, accum: int) -> Dict[str, Any]:
        result = self._hama.measure(exposition=exposition, accum=accum)
        return {
            "intensity_raw": result["intensity_raw"],
            "intensity_corrected": result["intensity_corrected"],
            "saturated_pixels": result["saturated_pixels"],
        }

    def cleanup(self) -> None:
        self._hama.cleanup()
