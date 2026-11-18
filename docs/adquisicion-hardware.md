# Adquisición: diagnóstico y decisión de hardware

Análisis de la cadena de captura del escáner (C12880MA → ADC → Raspberry Pi),
evaluación de alternativas y decisión razonada sobre cómo mejorarla.

**Fecha:** 2026-08-12
**Datos de referencia:** `scans/scan_2026-08-10T20-24-52/` (140×55 = 7700 puntos,
288 canales, exposición 30 ms, `accum=1`, lámpara al 30%)

---

## Resumen

El escáner tarda unas **2 horas** en una grilla de 7700 puntos, y ese es el
síntoma visible. Al medir sobre los datos reales aparecieron cuatro problemas de
fondo, tres de los cuales **degradan la calidad del dato**, no solo la velocidad:

| # | Hallazgo | Impacto | Coste de arreglarlo |
|---|----------|---------|---------------------|
| 1 | El 43% de los píxeles está recortado por el **techo del ADC**, no por el sensor | Datos inservibles en 430–719 nm | 1 línea |
| 2 | El ADC entrega **12 bits**, no 16 (probablemente es un ADS1015) | La resolución nominal es ficticia | verificar |
| 3 | El **ENOB real es 8.8 bits** (ruido analógico de 7.9 mV) | Techo de calidad del montaje | promediado |
| 4 | La **deriva térmica** durante 2 h se mapea a gradiente espacial | Contamina el PCA | escanear más rápido |
| 5 | El pin **TRG del sensor no se usa** | Muestreo sin sincronizar | firmware |

**Decisión: Raspberry Pi Pico 2 (RP2350)** como controlador de adquisición,
después de agotar dos fases de coste cero. El razonamiento completo está en
[Decisión](#decisión).

> Las cifras de **datos** (ENOB, clipping, deriva, rangos) están medidas sobre el
> escaneo real. Las cifras de **tiempo** son estimaciones derivadas del código;
> el sistema no está instrumentado todavía. Ver [Qué falta medir](#qué-falta-medir).

---

## 1. Cómo funciona hoy

```
C12880MA ──Video(analógico)──▶ ADS1x15 ──I2C 100 kHz──▶ Raspberry Pi ──▶ CSV
    ▲                                                        │
    └────────────── CLK, ST (GPIO, generados por Python) ─────┘

TRG (pin 7) ──✗ sin conectar
EOS (pin 9) ──✗ sin conectar
```

Por cada punto de la grilla (`rpi_server.py:250`):

1. `_cnc_move()` mueve la CNC y espera un `SETTLE` fijo de 0.3 s.
2. `sensor.measure()` → `pi4_hama._single_scan()`: pulsos de CLK, integración,
   y 288 lecturas del ADC una a una por I2C.
3. Se escribe la fila del CSV y se reescribe `progreso.json` entero.

### Presupuesto de tiempo estimado por punto

| Fase | Coste | Referencia |
|------|-------|-----------|
| 288 lecturas del ADC | 460–630 ms | `pi4_hama.py:143-148` |
| ~480 × `sleep(1e-6)` (real ≈ 60–100 µs c/u) | 30–50 ms | `pi4_hama.py:93-106` |
| Exposición | 30 ms | `pi4_hama.py:132` |
| `SETTLE` fijo tras mover | 300 ms | `rpi_server.py:75,141` |
| Espera del `ok` de GRBL (polling a 50 ms) | 0–50 ms | `rpi_server.py:126-127` |
| Reescritura de `progreso.json` | 10–40 ms, creciente | `rpi_server.py:302` |
| **Total** | **≈ 1.0 s/punto → ~2.1 h** | |

---

## 2. Diagnóstico

### 2.1 El clipping es del ADC, no del sensor

El dato más importante del análisis. Sobre las 2.2 millones de lecturas del
escaneo:

```
mínimo global : 3680 counts  =  0.460 V
máximo global : 32752 counts =  4.094 V   ← tope exacto del rango ±4.096 V
```

El ADS está en ganancia 1× (rango ±4.096 V). El C12880MA se alimenta a
**+Vs = 5 V** (pines 1 y 3 del datasheet) y su salida de vídeo sube por encima de
4.1 V. **El sensor sigue entregando señal donde el ADC ya no puede seguirlo.**

Extensión del daño:

| Métrica | Valor |
|---------|-------|
| Lecturas pegadas al tope | 390 486 (17.6%) |
| Píxeles espaciales con ≥1 canal recortado | **3315 de 7700 (43.1%)** |
| Píxeles con ≥10 canales recortados | 3273 (42.5%) |
| Media de canales recortados por píxel | 50.7 de 288 |
| Banda afectada | **430–719 nm** |

Esa banda contiene las tres ventanas RGB definidas en `rpi_server.py:73`
(450–680 nm) y toda la firma de la clorofila — justo la señal que el pipeline
necesita. En casi la mitad de la imagen, lo que hay guardado en el CSV no es un
espectro sino una meseta plana.

Además, el detector de saturación de `pi4_hama.py:189-192` usa un umbral del 90%
de 32767 (= 29490). No detecta el problema como lo que es: no estamos *cerca*
del tope, estamos *en* el tope.

**Corrección** (`pi4_hama.py:58`):

```python
self.ads.gain = 2/3      # ±6.144 V en lugar de ±4.096 V
```

No cuesta resolución efectiva: el ruido medido es 7.9 mV y el paso de
cuantización solo pasaría de 2 mV a 3 mV, muy por debajo del ruido.

> Esto es mejor que bajar la exposición, que era la reacción intuitiva: bajar
> exposición sí cuesta SNR real.

### 2.2 El ADC no es de 16 bits

En las 2.2 M de lecturas hay **1818 valores distintos, y todos son múltiplos de
16**, sin una sola excepción. Los 4 bits bajos son siempre cero.

La explicación más probable: **el chip es un ADS1015 (12 bits), no un ADS1115**.
La librería Adafruit devuelve el valor del ADS1015 desplazado 4 bits a la
izquierda para igualar la escala del ADS1115, lo que produce exactamente
múltiplos de 16. `pi4_hama.py:57` instancia la clase `ADS1115` sin verificar nada.

Si se confirma, hay una consecuencia aprovechable: el ADS1015 llega a
**3300 SPS**, no a 860. Y `860` ni siquiera existe en su tabla de tasas — el
código que la clase `ADS1115` escribe en el registro corresponde a **1600 SPS**
en un ADS1015. Es decir, probablemente el vaciado real ya es de ~180 ms y no de
335 ms, y configurándolo bien bajaría a **87 ms**.

**Verificación pendiente:** leer la serigrafía del chip, o cronometrar
`_single_scan()`.

### 2.3 La resolución real son 8.8 bits

Midiendo el ruido en las zonas donde la lámpara no emite (UV < 400 nm e
IR > 800 nm), donde la señal es plana y lo que queda es ruido puro:

| Métrica | Valor |
|---------|-------|
| Swing útil (dark → tope) | 28 651 counts |
| Ruido σ de lectura | **63 counts ≈ 7.9 mV** |
| Paso de cuantización | 16 counts ≈ 2 mV |
| **ENOB real** | **8.8 bits** |
| SNR | 53 dB |

El ruido es 4× el paso de cuantización. Los bits bajos digitalizan ruido.

Dos consecuencias:

- **Cualquier ADC de 10 bits o más ya iguala lo que se obtiene hoy.** La discusión
  sobre bits nominales es irrelevante mientras el ruido analógico sea el límite.
- **El ruido de 7.9 mV es del montaje analógico** (sensor, cableado, lámpara,
  alimentación), no del ADC. Ningún ADC lo va a arreglar. La única palanca es
  **promediar**, y promediar exige velocidad.

### 2.4 La lentitud contamina el PCA

Siguiendo el nivel de oscuro a lo largo de las ~2 h del escaneo (el orden de
filas del CSV es el orden temporal):

```
decil  1:  4088 counts        decil  6:  4043
decil  2:  4054               decil  7:  4096
decil  3:  4023               decil  8:  4162
decil  4:  3991               decil  9:  4284
decil  5:  4002               decil 10:  4273
```

Deriva de **+271 counts, más de 4σ del ruido de lectura**. El datasheet de
Hamamatsu documenta el efecto: la gráfica *Dark output vs. ambient temperature*
muestra la salida de oscuro subiendo de ~5000 counts a 20 °C hasta ~20000 a 50 °C,
con forma exponencial. Dos horas de Raspberry Pi, lámpara y drivers calentando el
recinto lo explican.

**Por qué importa:** el barrido es en zig-zag por filas
(`rpi_server.py:253`), así que **el tiempo se mapea directamente al eje Y de la
imagen**. La deriva térmica aparece en el cubo como un gradiente espacial
vertical perfectamente sistemático — indistinguible a posteriori de variación
real de la muestra.

Cuando `preprocessing.py` corra el PCA, hay riesgo real de que el PC1 esté
capturando el calentamiento de la electrónica en lugar de la química de la hoja.

> **La velocidad no es comodidad en este proyecto: es una variable de calidad
> del dato.** Un escaneo de 20 minutos tiene ~6× menos deriva que uno de 2 horas.

**Estado actual:** la temperatura ya se **mide** durante la captura. Un ESP8266
con DHT11 reporta a `POST /data_temp` y el servidor guarda la serie dentro de la
carpeta del escaneo (`ambiente.csv`) más la lectura vigente en cada punto
(`ambiente_puntos.csv`), con un resumen en su `config.json`. Eso permite
contrastar el gradiente vertical del cubo contra la deriva térmica real en vez de
suponerla. Ver [puesta en marcha](inicio.md#temperatura-y-humedad-esp8266--dht11).

### 2.5 El sensor tiene un pin de sincronización que no se usa

Del datasheet oficial (KACC1226E), tabla de conexiones:

| Pin | Símbolo | I/O | Descripción |
|-----|---------|-----|-------------|
| 7 | **TRG** | O | *Pulse for capturing sensor video signals* |
| 9 | EOS | O | End of scan |

Y la nota al pie del diagrama de temporización:

> *"If the first TRG pulse after ST goes low is counted as the first pulse, the
> Video signal should be acquired at the rising edge of the 89th TRG pulse."*

El fabricante dice exactamente cuándo muestrear. El montaje actual no conecta
TRG ni EOS: muestrea a ciegas, confiando en que Python mantenga el ritmo.
**No hay garantía de que la lectura número _n_ corresponda al píxel _n_.**
Es candidato serio a explicar parte de esos 7.9 mV de ruido.

El datasheet también fija **f(CLK) máximo en 5 MHz**, con un scan completo en
**76.2 µs**. El montaje actual está unas **4400× por debajo** de lo que el sensor
admite.

### 2.6 Posible violación de niveles lógicos

Para que el vídeo llegue a 4.09 V, el sensor debe estar a **Vs = 5 V**. Pero los
GPIO de la Raspberry Pi generan CLK y ST a **3.3 V**, y el C12880MA a 5 V espera
un nivel alto en torno a 4 V.

El montaje está funcionando fuera de especificación en los niveles de entrada.
Funciona, pero de forma marginal, y puede contribuir al ruido y a lecturas
erráticas.

**Verificación pendiente:** medir Vs con un multímetro entre los pines 1/3 y GND.

---

## 3. Alternativas evaluadas

Escenarios calculados con la exposición ya corregida (~10 ms) sobre la grilla de
7700 puntos.

| Opción | Vaciado | Promedios viables | ENOB efectivo | Tiempo total | Coste |
|--------|---------|-------------------|---------------|--------------|-------|
| Estado actual | 335 ms | 1 | 8.8 | ~2.1 h | — |
| A. Solo software | ~335 ms | 1 | 8.8 | ~1.1 h | 0 € |
| B. ADS1015 @ 3300 SPS | 87 ms | 4 | 9.8 | ~65 min | 0 € |
| C. MCP3208 (SPI, 12 bit) | ~20 ms | 16 | 10.8 | ~77 min | ~3 € |
| **D. Pico 2 (RP2350)** | **<1 ms** | **16** | **~11** | **~38 min** | **~5 €** |
| E. Teensy 4.x | <1 ms | 16 | ~11 | ~38 min | ~30 € |
| F. C13016 oficial | — | — | 16 nominal | — | **~1600 €** |
| G. Pushbroom (PSNet) | n/a | n/a | — | segundos | ~500 € + rediseño |

### A. Solo software, sin tocar hardware

Independiente de todo lo demás y siempre rentable:

- **`G4P0` en lugar del `SETTLE` a ciegas.** GRBL responde `ok` cuando *acepta* el
  comando en el buffer, no cuando el eje llega — por eso hay 0.3 s fijos tapando
  el agujero. Un `G4P0` (dwell cero) solo devuelve `ok` con el planner vacío, es
  decir al terminar el movimiento de verdad. Entonces `SETTLE` baja a lo que
  realmente tarde el asentamiento mecánico (~0.05–0.1 s). **Ahorro: ~26 min.**
  Y además elimina el riesgo actual de medir con el eje aún moviéndose.
- **Eliminar `progreso.json`.** Se reescribe entero en cada punto: es O(n²), unos
  300 MB escritos en la microSD a lo largo del escaneo. Y es **código muerto**:
  `rpi_server.py:218-220` crea una carpeta con timestamp nuevo en cada
  `/scan/start`, así que el bloque de reanudación de las líneas 229-236 nunca se
  ejecuta. El progreso se puede reconstruir del propio `espectros.csv`, que ya
  guarda `col,row`.
- **Quitar los `sleep(self.delay)`** del bucle de lectura: la transacción I2C ya
  dura más de 1 ms. ~5 min.
- **`ser.readline()` bloqueante** en `_cnc_send` en vez de polling a 50 ms.
- Cachear `wavelength()`, que se recalcula en cada `measure()`.

**Veredicto:** hacerlo siempre, es gratis. Pero no toca el cuello real.

### B. Configurar bien el ADS1015

Si se confirma la hipótesis de §2.2, cambiar a la clase correcta con
`data_rate=3300` baja el vaciado a 87 ms. Es la mejor relación esfuerzo/ganancia
del documento si el chip resulta ser ese.

**Limitación:** no arregla el jitter del CLK, ni el TRG, ni permite promediar
mucho. Es un parche bueno, no una solución.

### C. MCP3208 por SPI

Cambio mínimo de hardware: 12 bits, ~100 kSPS, mismo código que el MCP3008 pero
sin la pérdida a 10 bits.

**Limitación decisiva:** leyendo píxel a píxel desde Python, cada `xfer2` arrastra
50–100 µs de overhead del intérprete, así que el vaciado real es ~20 ms, no el
límite teórico. Al multiplicarlo por 16 promedios, la ventaja se evapora: misma
calidad que el Pico pero el doble de tiempo. Y sigue sin resolver el timing.

### D. Raspberry Pi Pico 2 (RP2350) ← elegida

**Modelo exacto: Raspberry Pi Pico 2, versión sin W** (no hace falta WiFi: va por
USB a la Pi, que ya tiene red). ~5 €.

**Por qué el Pico 2 y no el Pico 1:** el ADC del RP2040 tiene la errata
**RP2040-E11** — picos de no linealidad diferencial en los códigos 512, 1536,
2560 y 3584 que producen **códigos faltantes**, con un **ENOB medido de 8.7 bits**.
Eso es *igual o peor* que los 8.8 bits actuales. El RP2350 elimina esos picos y
sube a **ENOB 9.5**. La diferencia entre mejorar y no mejorar.

Lo que aporta:

1. **Promediado real.** El ruido de 7.9 mV equivale a ~10 LSB del ADC del Pico, así
   que hay dither de sobra y el promediado funciona limpiamente: 16 scans → ~11
   bits efectivos. A 500 kS/s esos 16 scans no cuestan tiempo apreciable.
2. **TRG + DMA.** Muestrear donde el fabricante indica, no a ojo.
3. **PIO.** CLK y ST con precisión de 8 ns y cero jitter. Hoy los genera Python
   sobre Linux no-tiempo-real: el tiempo de integración real varía punto a punto
   y no se sabe cuánto. Esa varianza entra en el PCA como ruido no calibrable, y
   **ningún ADC colgado de la Pi lo arregla**.
4. **Auto-exposición por punto:** medir, detectar saturación, ajustar y remedir en
   el micro, sin coste apreciable. Resuelve estructuralmente el problema de §2.1,
   que con exposición fija volverá a aparecer en cualquier muestra con zonas
   claras y oscuras.

**El problema a resolver antes de comprar:** el ADC del Pico admite 0–3.3 V y la
señal de vídeo va de 0.46 V a ~4.5 V. **No cabe.** Dos salidas:

- **D1 — alimentar el sensor a 3.3 V.** El breakout de GroupGets acepta 5 V *o*
  3.3 V (uno u otro, nunca ambos). Con Vs = 3.3 V el vídeo queda en 0.3–3.0 V,
  entra directo en el ADC sin componentes adicionales, y de paso los niveles
  lógicos pasan a ser correctos (§2.6). Cuesta sensibilidad, que se compensa con
  exposición o luz. **Opción recomendada para empezar.**
- **D2 — Pico 2 + ADC externo por SPI** (MCP3208, o ADS7886 para más velocidad).
  El Pico genera el timing por PIO y lee el ADC externo con DMA disparado por
  TRG. Mantiene Vs = 5 V y todo el swing del sensor. Más cableado, pero es la
  configuración técnicamente correcta.

El Pico sirve igual en ambas, así que la compra no se pierde si hay que pasar de
D1 a D2.

**Firmware:** no se escribe desde cero. [uutzinger/C12880MA](https://github.com/uutzinger/C12880MA)
ya implementa lectura con TRG + DMA (Teensy 3.x, ~800 kS/s) y la lógica de
temporización es portable al PIO. También está el
[ejemplo oficial de GroupGets](https://github.com/groupgets/c12880ma).

**Encaje con la arquitectura actual:** `Sensor/strategy.py` ya define
`SpectrometerStrategy` y `create_spectrometer()` es una fábrica. Añadir un
`PicoDriver` que hable por serie es **un archivo nuevo y una línea** en el
diccionario `_drivers` de `Sensor/__init__.py`. Ni `rpi_server.py` ni la UI ni el
pipeline se enteran.

### E. Teensy 4.x

Más potente que el Pico y con el código de referencia ya escrito para esa
familia — pero el repo de uutzinger declara explícitamente que **no soporta la
4.x**, y la 3.x está descontinuada. Seis veces el precio para portar el mismo
código igualmente. Descartada.

### F. Circuito oficial Hamamatsu C13016

Resuelve todo de fábrica: 16 bits, USB, software de evaluación incluido.
Cuesta [unos 2674 AUD](https://int.rsdelivers.com/product/hamamatsu-photonics/c13016/hamamatsu-photonics-evaluation-circuit-evaluation/2009489),
del orden de 1500–1600 € — **nueve veces el precio del propio sensor**.
Descartada por presupuesto.

### G. Cambiar a arquitectura pushbroom

Merece conocerse aunque no se adopte ahora. En marzo de 2026 se publicó
[PSNet](https://www.biorxiv.org/content/10.64898/2026.03.02.709086v2.full):
cámara hiperespectral con Raspberry Pi Camera Module 3 NoIR, rejilla de
difracción y carcasa impresa en 3D, **por menos de £500 completo**, aplicada a
**detección presintomática de enfermedad en plantas** — detectan firmas
espectrales a 2 y 4 días post-inoculación, antes de que haya síntomas visibles.
Es el objetivo de este proyecto, resuelto con otra arquitectura.

La diferencia de fondo: un pushbroom captura **una línea espacial entera con
todas sus bandas en una sola exposición**. Donde aquí hacen falta 140
movimientos y 140 mediciones, allí hace falta una toma. No es un 5× ni un 20×:
son dos órdenes de magnitud.

**Por qué no ahora:** implica rehacer la óptica entera y jubilar el montaje
puntual. Ya están construidos la CNC, el servidor REST, la UI, el pipeline en
numpy puro y el etiquetado manual. Cambiar de arquitectura significa tirar buena
parte de eso.

**Recomendación:** anotarlo como la v2 del proyecto, o como lo que se haría
empezando de cero hoy. No como el siguiente paso.

---

## Decisión

**Raspberry Pi Pico 2 (RP2350) en configuración D1**, después de agotar las fases
de coste cero.

El argumento no es «es más rápido». Es que en este proyecto concreto:

1. **El límite de calidad es el ruido analógico de 7.9 mV**, y la única palanca
   contra él es promediar. Promediar 16 veces solo es viable si cada scan cuesta
   microsegundos en lugar de un tercio de segundo. **Velocidad y calidad son el
   mismo problema aquí, no dos objetivos en tensión.**
2. **La deriva térmica se mapea a gradiente espacial** por el barrido en zig-zag,
   y amenaza directamente el PCA que es el corazón del pipeline. Acortar el
   escaneo es la forma directa de atacarla.
3. **El jitter de la exposición no lo arregla ningún ADC** conectado a la Pi.
   Solo un micro con timing determinista.
4. **El coste de integración es bajo** gracias al patrón Strategy ya existente.

Las opciones B y C son parches razonables, pero ninguna toca los puntos 2 y 3.
La opción G es probablemente superior a largo plazo, pero exige tirar medio
proyecto.

---

## Plan por fases

### Fase 0 — coste cero, inmediato

- [ ] `ads.gain = 2/3` en `pi4_hama.py:58` → recupera el 43% de píxeles recortados
- [ ] Verificar la serigrafía del ADC (¿ADS1015?) y medir Vs con multímetro
- [ ] Si es ADS1015: clase correcta + `data_rate=3300`
- [ ] Repetir un escaneo pequeño (20×20) y **volver a medir** ENOB y clipping

### Fase 1 — software, sin hardware

- [ ] Instrumentar `_scan_worker` con acumuladores de tiempo (move / measure / io)
      y volcarlos al `config.json` del escaneo
- [ ] `G4P0` para sincronizar con GRBL, bajar `SETTLE`
- [ ] Eliminar `progreso.json` (código muerto + O(n²))
- [ ] Quitar los `sleep()` del camino crítico, cachear `wavelength()`

### Fase 2 — Pico 2

- [ ] Comprar Pico 2 (~5 €)
- [ ] Portar la lógica de TRG + DMA de uutzinger al PIO del RP2350
- [ ] Protocolo serie mínimo: `measure(exposición, promedios) → 288 valores`
- [ ] `Sensor/pico_driver.py` implementando `SpectrometerStrategy`
- [ ] Registrar `"pico"` en `create_spectrometer()`
- [ ] Escaneo de validación contra un escaneo de referencia del driver actual

### No planificado

- Pushbroom (opción G) — v2 del proyecto

---

## Qué falta medir

Honestidad sobre lo que aún no está confirmado:

- **Los tiempos son estimaciones**, derivadas de leer el código. Nada está
  cronometrado. La fase 1 empieza por instrumentar justo por esto.
- **La hipótesis del ADS1015 no está confirmada**, solo es la explicación más
  probable de los múltiplos de 16.
- **Vs no está medido.** Todo el razonamiento de §2.1 y §2.6 asume 5 V, deducido
  del techo de 4.094 V en los datos.
- **El paso de 1 mm puede estar sobremuestreando.** Si la huella óptica del sensor
  es mayor que 1 mm, subir a 2 mm da un 4× sin perder información y sin tocar
  nada. No es verificable desde el código: depende de la óptica real.
- **Los ENOB proyectados** de la tabla de alternativas asumen que el promediado
  reduce el ruido con √N. Es válido mientras el ruido sea aleatorio; la parte
  sistemática (deriva térmica, no linealidad del ADC) no se promedia.

---

## Referencias

- [Datasheet C12880MA / C16767MA (KACC1226E) — Hamamatsu](https://www.hamamatsu.com/content/dam/hamamatsu-photonics/sites/documents/99_SALES_LIBRARY/ssd/c12880ma_c16767ma_kacc1226e.pdf)
  — pinout, TRG, diagrama de temporización, curva de dark vs temperatura
- [uutzinger/C12880MA](https://github.com/uutzinger/C12880MA) — lectura con TRG + DMA
- [groupgets/c12880ma](https://github.com/groupgets/c12880ma) — ejemplo oficial
- [C12880MA Breakout Board v2 — datasheet](https://groupgets-files.s3.amazonaws.com/hamamatsu/uspectrometer/C12880MA%20Breakout%20Board%20v2%20-%20Datasheet%20-%201.2.pdf)
  — alimentación 5 V o 3.3 V
- [RP2040 DNL errata (RP2040-E11)](https://www.eevblog.com/forum/microcontrollers/rp2040-dnl-errata/)
- [RP2350 ADC INL/DNL](https://github.com/earlephilhower/arduino-pico/discussions/2763) — ENOB 9.5 vs 8.7
- [Comparación RP2350 vs RP2040 — SparkFun](https://news.sparkfun.com/11692)
- [Corrección software de la no linealidad del ADC RP2040/RP2350](https://github.com/kitanokitsune/rp2040adc_correction)
- [PSNet — HSI de bajo coste para detección presintomática (bioRxiv, 2026)](https://www.biorxiv.org/content/10.64898/2026.03.02.709086v2.full)
- [C13016 — precio en RS](https://int.rsdelivers.com/product/hamamatsu-photonics/c13016/hamamatsu-photonics-evaluation-circuit-evaluation/2009489)
