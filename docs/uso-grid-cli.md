# Uso de grid-cli

`grid-cli.py` ejecuta un recorrido en grilla (serpiente) sobre una CNC con controlador GRBL. En cada punto de la grilla sube el cabezal a una altura Z segura y ejecuta una función de captura (preparada para cámara espectral). La grilla se centra automáticamente en el centro de la máquina usando los límites leídos de GRBL (`$130`, `$131`).

## Requisitos

- Python 3
- `pyserial`: `pip install pyserial`
- CNC con firmware GRBL (v1.1 recomendado) accesible por puerto serial

## Ejecución básica

```bash
python grid-cli.py
```

Si no pasas ancho, alto, paso ni velocidad, el programa te los pedirá por consola (con valores por defecto).

## Argumentos

| Argumento | Default | Descripción |
|-----------|---------|-------------|
| `--port` | `/dev/ttyUSB0` | Puerto serial (en Windows suele ser `COM3`, etc.). |
| `--baud` | `115200` | Velocidad del puerto serial. |
| `--width` | (pregunta) | Ancho de la grilla en mm. |
| `--height` | (pregunta) | Alto de la grilla en mm. |
| `--step` | (pregunta) | Paso entre puntos en mm (distancia entre posiciones de captura). |
| `--feed` | (pregunta) | Velocidad de avance F en mm/min. |
| `--safe-z` | `10.0` | Altura Z segura inicial (mm). Debe estar dentro del recorrido del eje Z de tu máquina. |
| `--z-safe-place` | (igual que `--safe-z`) | Altura Z a la que se sube el cabezal para cada captura (cámara espectral). |
| `--output-dir` | `captures` | Carpeta donde se guardan los CSV de cada punto (x, y, z + 288 canales espectrales). |
| `--capture-delay` | `0.015` | Tiempo en segundos por captura (C1288MA ~10 ms + guardado). |
| `--margin` | `0.0` | Margen en mm que se respeta dentro de los límites de la máquina. |
| `--dwell` | `0.0` | Pausa en segundos en cada punto (G4 P). |
| `--home` | — | Ejecuta homing (`$H`) antes de empezar (requiere homing configurado). |
| `--unlock` | — | Envía `$X` antes de empezar (útil si GRBL estaba en ALARM). |
| `--hard-limits` | `keep` | `keep` no modifica `$21`; `off` pone `$21=0` temporalmente. |
| `--quiet` | — | Reduce mensajes en consola. |

## Flujo del recorrido

1. Conexión al puerto y, si se usan, `$X` / `$H` / `$21=0`.
2. Lectura de límites de la máquina (`$$` → `$130`, `$131`).
3. Cálculo de la grilla centrada y comprobación de que no se sale de límites.
4. **Preparación**: `G21` (mm), `G90` (absoluto), `G0 Z{safe_z}`.
5. **Posición 0 de la grilla**: movimiento rápido a la primera posición (`G0 X Y`), espera a que GRBL reporte **Idle** (CNC estacionada), espera de 1 segundo y mensaje *"Listo para iniciar la captura y el recorrido."*
6. **Por cada punto** (orden serpiente): en cada punto se espera a que GRBL reporte **Idle** antes de ejecutar la captura; el cabezal solo avanza al siguiente cuando la captura y el almacenamiento han terminado.
   - Si no es el primer punto: `G1 X Y F{feed}` hasta ese punto; se espera a **Idle** (CNC parada en ese punto).
   - Si la Z actual no está en `z_safe_place` (tolerancia 0,01 mm): `G0 Z{z_safe_place}`; se espera a **Idle**.
   - **Captura**: se espera `--capture-delay` segundos (simulando C1288MA + guardado), se guarda un CSV con 288 canales en `--output-dir` y se imprime "captura" y X, Y, Z. Solo entonces se pasa al siguiente punto.
   - Si `--dwell` > 0: pausa `G4 P{dwell}`.
7. Al final del recorrido se espera de nuevo a **Idle** antes de dar por terminado, para no cerrar el puerto mientras la CNC sigue en movimiento.

Las capturas empiezan en la posición 0 de la grilla; en cada punto la CNC se estaciona (Idle) antes de la captura y el avance al siguiente ocurre solo cuando la captura y el guardado han culminado.

## Guardado de datos espectrales

En cada punto se escribe un archivo CSV en la carpeta indicada por `--output-dir` (por defecto `captures/`). Los archivos se nombran `captura_0001.csv`, `captura_0002.csv`, etc. (índice del punto en la grilla).

Formato del CSV:

- **Cabecera**: `x,y,z,channel_0,channel_1,...,channel_287` (posición en mm + 288 canales del sensor C1288MA).
- **Datos**: una fila con los valores de X, Y, Z (3 decimales) y 288 valores numéricos de luz.

Hasta conectar el sensor real, los 288 valores se generan simulados (ceros). Al integrar el C1288MA se sustituye esa lista por la lectura real en `do_capture`.

## Ejemplos

Grilla 250×120 mm, paso 5 mm, feed 800 mm/min, Z segura 2 mm (máquina con poco recorrido en Z):

```bash
python grid-cli.py --width 250 --height 120 --step 5 --feed 800 --safe-z 2 --z-safe-place 2
```

Mismo caso con homing previo y desbloqueo:

```bash
python grid-cli.py --width 250 --height 120 --step 5 --feed 800 --safe-z 2 --unlock --home
```

Puerto y pausa de 1 s por punto:

```bash
python grid-cli.py --port /dev/ttyUSB0 --width 200 --height 100 --step 10 --dwell 1
```

Tiempo de captura (C1288MA) y carpeta de salida: 20 ms por punto y CSV en `datos_grilla/`:

```bash
python grid-cli.py --width 250 --height 120 --step 5 --feed 800 --capture-delay 0.02 --output-dir datos_grilla
```

Ver la ayuda:

```bash
python grid-cli.py --help
```

## Salida de captura

En cada punto se imprime algo como:

```
captura
  X=185.000  Y=205.000  Z=2.000  -> captures/captura_0001.csv
```

Son las coordenadas máquina (MPos) y la ruta del CSV guardado. Dentro del CSV van x, y, z y los 288 canales (simulados hasta conectar el C1288MA).

## Errores frecuentes

- **ALARM:3 (límite físico)**: suele ocurrir si `--safe-z` o `--z-safe-place` están fuera del recorrido del eje Z. Ajusta ambos a un valor que tu máquina pueda alcanzar (por ejemplo `--safe-z 2 --z-safe-place 2`).
- **"No se pudo leer status con '?'"**: GRBL a veces tarda en responder tras un movimiento. El programa reintenta; si persiste, revisa cable, puerto y que ningún otro programa use el mismo puerto.
- **La grilla se sale de límites**: reduce `--width` o `--height`, o usa `--margin` para dejar margen a los límites.

## Estructura de la grilla

La grilla es un rectángulo centrado en el centro de la máquina `(X_max/2, Y_max/2)`. Los puntos se recorren en zigzag (serpiente): primera fila de izquierda a derecha, segunda de derecha a izquierda, etc. El número de puntos depende de `width`, `height` y `step`.
