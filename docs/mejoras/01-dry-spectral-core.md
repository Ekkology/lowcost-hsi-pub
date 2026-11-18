# Mejora 1 — DRY: núcleo espectral compartido

**Fecha:** 2026-07-22
**Estado:** ✅ Hecho

## Problema

El mismo conocimiento espectral estaba **copiado y pegado** en varios módulos.
Concretamente, el polinomio de calibración de longitud de onda del C12880MA
(el que empieza con `2.704770136 * i`) aparecía en **4 lugares** de la capa de
procesamiento/UI:

- `legacy/Reconstruction/reconstruct_image.py`
- `ui/spectral.py`
- `preprocessing.py`
- (además del driver `libC12880MA.py`)

Y no era lo único duplicado:

- Las **funciones CIE 1931** (`cie_x_bar`, `cie_y_bar`, `cie_z_bar`) estaban en
  `reconstruct_image.py` y en `ui/spectral.py`.
- La **lectura del CSV de captura** estaba triplicada con tres nombres y dos
  formatos de retorno distintos: `read_capture` (reconstruct), `parse_capture_csv`
  (spectral) y `_read_capture` (preprocessing).

El propio `ui/spectral.py` admitía el problema en su docstring: *"Replicates the
color science ... so the UI can compute RGB without importing hardware-dependent
modules"*. Eso no es una razón para copiar, sino un **smell de acoplamiento**:
la ciencia espectral (sin hardware) estaba mezclada con el driver.

**Consecuencia:** recalibrar el sensor obligaba a editar 4 archivos y recordar
mantenerlos sincronizados.

## Solución

Se creó **`spectral_core.py`** como *única fuente de verdad*, sin dependencias
de hardware ni de la UI. Contiene:

- `WAVELENGTHS`, `NUM_CHANNELS`, `VISIBLE_MIN_NM`, `VISIBLE_MAX_NM`
- Funciones CIE 1931 + `XYZ_TO_SRGB` + `srgb_gamma`
- `spectrum_to_rgb`, `rgb_to_hex`
- `CaptureData`, `read_capture` (+ alias `parse_capture_csv`)

Luego:

- **`ui/spectral.py`** pasó a ser un *shim* delgado que **re-exporta** desde
  `spectral_core`. Así los imports de la UI siguen intactos
  (`from ui.spectral import WAVELENGTHS, spectrum_to_rgb, rgb_to_hex, parse_capture_csv`)
  sin tocar `ui/components/spectral_plot.py` ni `ui/components/capture_tab.py`.
- **`preprocessing.py`** ahora importa `NUM_CHANNELS`, `WAVELENGTHS` y
  `read_capture` desde el core, y eliminó su definición duplicada del polinomio
  y su propio `_read_capture`.

## Archivos tocados

| Archivo | Cambio |
|---|---|
| `spectral_core.py` | **Nuevo.** Única fuente de verdad. |
| `ui/spectral.py` | Reescrito como shim que re-exporta del core. |
| `preprocessing.py` | Importa del core; elimina duplicados. |

## Frontera consciente (lo que NO se unificó)

Las copias del polinomio en `libC12880MA.py`, `Sensor/libC12880MA.py` y
`pi4_hama.py` **se dejaron a propósito**: son código de **driver de hardware**
que calcula los nm en el dispositivo. Es una frontera razonable que el firmware
sea autónomo; unificarlo lo acoplaría a la capa de procesamiento. La
duplicación eliminada fue la de la **capa de procesamiento/UI**, que era el
problema real.

## Verificación

```bash
# La UI sigue importando igual y funciona:
python3 -c "from ui.spectral import WAVELENGTHS, spectrum_to_rgb, rgb_to_hex, parse_capture_csv; print(WAVELENGTHS[0])"

# El polinomio ya solo vive una vez en la capa de procesamiento:
grep -rn "2.704770136" --include="*.py" . | grep -v legacy
#  -> spectral_core.py, y solo los drivers de hardware (libC12880MA, pi4_hama)
```
