from Sensor.models import SpectralReading, NUM_CHANNELS
from Sensor.strategy import SpectrometerStrategy
from Sensor.legacy_driver import LegacyDriver
from Sensor.improved_driver import ImprovedDriver

# Retrocompatibilidad con código que importa pi4_hama directamente desde Sensor
from Sensor.libC12880MA import pi4_hama


def create_spectrometer(driver: str = "improved") -> SpectrometerStrategy:
    """Fábrica de drivers.

    Parámetros:
        driver: "legacy" → LegacyDriver (libC12880MA)
                "improved" → ImprovedDriver (pi4_hama)
    """
    _drivers = {"legacy": LegacyDriver, "improved": ImprovedDriver}
    if driver not in _drivers:
        raise ValueError(f"Driver desconocido: '{driver}'. Opciones: {list(_drivers)}")
    return _drivers[driver]()
