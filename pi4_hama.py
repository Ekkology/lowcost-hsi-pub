"""
pi4_hama.py — Driver para Hamamatsu C12880MA con Raspberry Pi + ADS1115
------------------------------------------------------------------------
Correcciones respecto a versión original:
  1. Dark subtraction implementada correctamente
  2. Bug de acumulación corregido (ahora retorna promedio, no suma)
  3. Verificación de saturación por píxel
  4. Scan de descarte al inicializar (requerido por datasheet)
  5. Delay mínimo realista para Python/RPi (no se promete 1µs)
  6. Normalización opcional de salida
  7. Estructura de datos completa con metadata
  8. Documentación clara de limitaciones del ADS1115
"""

from time import sleep, time
import RPi.GPIO as GPIO
import busio
import board
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
from adafruit_ads1x15.ads1x15 import Mode
from datetime import datetime


# ---------------------------------------------------------------------------
# Límites del ADS1115 en modo SINGLE a 860 SPS
# Cada conversión tarda ~1.16 ms. Leer 288 píxeles = ~334 ms por espectro.
# El timing no puede seguir el CLK del sensor píxel a píxel. Como resultado,
# cada lectura ADC captura el nivel de voltaje del Video en el momento en que
# I2C termina la conversión, no en el flanco exacto del TRG.
# Para aplicaciones de alta precisión, considerar ADC SPI o microcontrolador.
# ---------------------------------------------------------------------------

# Valor máximo del ADS1115 en ganancia 1x (±4.096 V, 16 bits con signo)
ADS1115_MAX_VALUE = 32767

# Umbral de saturación: 90% del máximo para advertir antes del clipping real
SATURATION_THRESHOLD = 0.90


class pi4_hama:

    def __init__(self):
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

        self.CLK = 18
        self.ST  = 17
        GPIO.setup(self.CLK, GPIO.OUT)
        GPIO.setup(self.ST,  GPIO.OUT)
        GPIO.output(self.CLK, 1)
        GPIO.output(self.ST,  0)

        # ADS1115
        self.ADC_CHAN = 0
        self.i2c = busio.I2C(board.SCL, board.SDA)
        self.ads = ADS.ADS1115(self.i2c)
        self.ads.mode = Mode.SINGLE
        self.ads.data_rate = 860
        self.chan = AnalogIn(self.ads, 0)  # Canal 0 — P0 en versiones modernas de la librería

                # Parámetros de medición
        self.exposition  = 1e-4   # Tiempo de integración en segundos
        #self.delay       = 5e-5    # ~50 µs: mínimo realista en Python/RPi
        self.delay       = 1e-6                           # sleep(1e-6) real ≈ 50-100 µs de todas formas
        self.accum       = 1      # Número de acumulaciones
        self.adc_delay = 2e-3
        # Dark frame
        self._dark_spectrum = None  # None = no dark capturado todavía

        # Estado interno
        self._initialized = False

    # -----------------------------------------------------------------------
    # Calibración de longitud de onda (coeficientes del inspection sheet)
    # Polinomio de grado 5 — reemplazar coeficientes con los de tu unidad
    # -----------------------------------------------------------------------
    def wavelength(self):
        nm = []
        for i in range(288):
            lam = (3.11487118e2
                   + 2.704770136   * i
                   - 1.198161774e-3 * i**2
                   - 7.333264294e-6 * i**3
                   + 8.195191046e-9 * i**4
                   + 6.104509768e-12 * i**5)
            nm.append(lam)
        return nm

    # -----------------------------------------------------------------------
    # Primitivas de timing
    # -----------------------------------------------------------------------
    def _clk_cycle(self):
        GPIO.output(self.CLK, 1)
        sleep(self.delay)
        GPIO.output(self.CLK, 0)
        sleep(self.delay)

    def _clk_high(self):
        GPIO.output(self.CLK, 1)
        sleep(self.delay)

    def _clk_low(self):
        GPIO.output(self.CLK, 0)
        sleep(self.delay)

    def _repeat(self, n, fn):
        for _ in range(n):
            fn()

    def _exposition_control(self):
        """Mantiene ST en alto durante el tiempo de integración,
        generando ciclos CLK mientras espera."""
        deadline = time() + self.exposition
        while time() < deadline:
            self._clk_cycle()

    # -----------------------------------------------------------------------
    # Scan completo — retorna lista de 288 valores raw del ADC
    # -----------------------------------------------------------------------
    def _single_scan(self):
        spectra = []

        # Inicialización de secuencia
        GPIO.output(self.CLK, 0)
        self._clk_cycle()
        GPIO.output(self.ST, 1)
        sleep(self.delay)
        self._repeat(3, self._clk_cycle)

        # --- Inicio integración ---
        self._exposition_control()
        GPIO.output(self.ST, 0)
        self._repeat(48, self._clk_cycle)
        # --- Fin integración ---

        # 39 ciclos de setup antes de leer (documentado en implementación original)
        self._repeat(39, self._clk_cycle)

        # Lectura de 288 píxeles
        # NOTA ADS1115: el valor leído corresponde al nivel promedio del Video
        # durante la conversión I2C (~1.16 ms), no al píxel exacto del TRG.
        for _ in range(288):
            GPIO.output(self.CLK, 1)
            spectra.append(self.chan.value)  # Dispara conversión ADC
            GPIO.output(self.CLK, 0)
            sleep(self.delay)

        # Pulsos de finalización
        self._repeat(8, self._clk_cycle)
        GPIO.output(self.CLK, 1)
        sleep(self.delay)

        return spectra

    # -----------------------------------------------------------------------
    # Inicialización: descarta el primer scan (requerido por datasheet)
    # "Do not use the video signal read at the first ST immediately
    #  after the power supply voltage (+Vs) is input."
    # -----------------------------------------------------------------------
    def initialize(self):
        if not self._initialized:
            _ = self._single_scan()   # Scan de descarte
            self._initialized = True

    # -----------------------------------------------------------------------
    # Dark calibration
    # Llamar con el sensor completamente tapado (sin luz)
    # -----------------------------------------------------------------------
    def capture_dark(self, accum=5):
        """Captura y guarda el dark spectrum promediando 'accum' scans.
        Llamar antes de cualquier medición, con la entrada de luz bloqueada."""
        self.initialize()
        dark_acc = [0.0] * 288
        for _ in range(accum):
            scan = self._single_scan()
            dark_acc = [dark_acc[i] + scan[i] for i in range(288)]
        self._dark_spectrum = [v / accum for v in dark_acc]
        return self._dark_spectrum

    def clear_dark(self):
        """Elimina el dark spectrum guardado."""
        self._dark_spectrum = None

    # -----------------------------------------------------------------------
    # Verificación de saturación
    # Retorna lista de índices de píxeles saturados
    # -----------------------------------------------------------------------
    def _check_saturation(self, spectra):
        threshold = ADS1115_MAX_VALUE * SATURATION_THRESHOLD
        saturated = [i for i, v in enumerate(spectra) if v >= threshold]
        return saturated

    # -----------------------------------------------------------------------
    # Medición principal
    # -----------------------------------------------------------------------
    def measure(self, exposition=None, accum=None, normalize=False):
        """
        Parámetros:
            exposition  : tiempo de integración en segundos (float)
            accum       : número de scans a promediar (int)
            normalize   : si True, retorna valores entre 0.0 y 1.0

        Retorna dict con:
            wavelengths_nm      : lista de 288 longitudes de onda
            intensity_raw       : promedio de scans sin corrección
            intensity_corrected : raw - dark (o raw si no hay dark)
            intensity_norm      : corrected normalizado (solo si normalize=True)
            dark_subtracted     : bool
            saturated_pixels    : índices de píxeles saturados
            integration_time_s  : tiempo de integración usado
            accumulations       : número de scans promediados
            timestamp           : ISO 8601
        """
        if exposition is not None:
            self.exposition = float(exposition)
        if accum is not None:
            self.accum = max(1, int(round(accum)))

        self.initialize()

        # Acumulación y promedio
        acc = [0.0] * 288
        for _ in range(self.accum):
            scan = self._single_scan()
            acc = [acc[i] + scan[i] for i in range(288)]

        intensity_raw = [v / self.accum for v in acc]

        # Dark subtraction
        if self._dark_spectrum is not None:
            intensity_corrected = [
                max(0.0, intensity_raw[i] - self._dark_spectrum[i])
                for i in range(288)
            ]
            dark_subtracted = True
        else:
            intensity_corrected = list(intensity_raw)
            dark_subtracted = False

        # Saturación (se verifica sobre raw, no sobre corrected)
        saturated = self._check_saturation(intensity_raw)

        # Normalización opcional
        if normalize:
            max_val = max(intensity_corrected) if max(intensity_corrected) > 0 else 1.0
            intensity_norm = [v / max_val for v in intensity_corrected]
        else:
            intensity_norm = None

        result = {
            "wavelengths_nm":       self.wavelength(),
            "intensity_raw":        intensity_raw,
            "intensity_corrected":  intensity_corrected,
            "intensity_norm":       intensity_norm,
            "dark_subtracted":      dark_subtracted,
            "saturated_pixels":     saturated,
            "integration_time_s":   self.exposition,
            "accumulations":        self.accum,
            "timestamp":            datetime.now().isoformat(),
        }

        if saturated:
            print(f"⚠️  Advertencia: {len(saturated)} píxel(es) saturado(s): {saturated[:10]}{'...' if len(saturated)>10 else ''}")
        if not dark_subtracted:
            print("⚠️  Advertencia: No hay dark frame. Llama capture_dark() primero para datos correctos.")

        return result

    def cleanup(self):
        GPIO.cleanup()


# ---------------------------------------------------------------------------
# Ejemplo de uso
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    spec = pi4_hama()

    print("Capturando dark frame (tapa la entrada de luz)...")
    sleep(2)
    spec.capture_dark(accum=5)
    print("Dark capturado.")

    print("Midiendo espectro...")
    result = spec.measure(exposition=50e-3, accum=10, normalize=True)

    print(f"Timestamp: {result['timestamp']}")
    print(f"Integración: {result['integration_time_s']*1000:.1f} ms, Acumulaciones: {result['accumulations']}")
    print(f"Dark subtraído: {result['dark_subtracted']}")
    print(f"Píxeles saturados: {result['saturated_pixels']}")
    print(f"Primer píxel — nm: {result['wavelengths_nm'][0]:.2f}, raw: {result['intensity_raw'][0]:.1f}, corr: {result['intensity_corrected'][0]:.1f}")

    spec.cleanup()
