from dataclasses import dataclass, field
from typing import List
import csv, os, time

NUM_CHANNELS = 288


@dataclass
class SpectralReading:
    channels: List[float]           # 288 valores del sensor
    wavelengths: List[float]        # 288 valores de longitud de onda (nm)
    exposition: float               # tiempo de exposicion (s)
    accumulations: int              # acumulaciones
    timestamp: float = field(default_factory=time.time)

    def min_value(self) -> tuple[float, float]:
        """Retorna (valor_min, longitud_de_onda_nm)."""
        idx = self.channels.index(min(self.channels))
        return self.channels[idx], self.wavelengths[idx]

    def max_value(self) -> tuple[float, float]:
        """Retorna (valor_max, longitud_de_onda_nm)."""
        idx = self.channels.index(max(self.channels))
        return self.channels[idx], self.wavelengths[idx]

    def to_csv_row(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> List:
        """Fila CSV: [x, y, z, ch0, ch1, ..., ch287]."""
        return [round(x, 3), round(y, 3), round(z, 3)] + self.channels

    @staticmethod
    def csv_header() -> List[str]:
        return ["x", "y", "z"] + [f"channel_{i}" for i in range(NUM_CHANNELS)]
