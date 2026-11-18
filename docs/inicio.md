# Puesta en marcha

Guía de instalación y arranque del escáner hiperespectral de bajo costo
(Hamamatsu **C12880MA** + CNC GRBL). Cubre los dos procesos que forman el
sistema y cómo ponerlos a funcionar juntos.

---

## Arquitectura

El sistema está partido en dos procesos que se comunican por HTTP:

```
┌──────────────────────┐        REST / HTTP        ┌────────────────────────────┐
│  Panel (UI)          │  ──────────────────────▶  │  Backend (rpi_server.py)   │
│  run_ui.py · NiceGUI │        puerto 8000        │  · Sensor C12880MA (I2C/GPIO)
│  navegador :8080     │  ◀──────────────────────  │  · CNC GRBL (serial USB)   │
│  (Mac / PC / la Pi)  │      estado · imágenes    │  (Raspberry Pi)            │
└──────────────────────┘                           └────────────────────────────┘
```

- **El backend corre en la Raspberry Pi**: es quien tiene el hardware (el
  espectrómetro por I2C/GPIO y la CNC por USB). Expone una API REST.
- **El panel corre donde quieras** (tu Mac/PC, o la propia Pi contra
  `localhost`). Solo consume la API: no toca hardware.

> Los dos procesos son independientes. Puedes arrancarlos en cualquier orden,
> pero el panel no podrá conectar hasta que el backend esté escuchando.

---

## Caso A — Raspberry Pi (backend)

Es donde vive el hardware. Requiere un entorno virtual: Raspberry Pi OS reciente
(Bookworm) bloquea el `pip` global (*externally-managed-environment*).

### 1. Habilitar el bus del sensor (solo una vez)

El C12880MA se lee a través de un **ADC ADS1115 por I2C** más pines GPIO:

```bash
sudo raspi-config     # Interface Options → I2C → activar
                      # (activa también SPI si tu montaje lo usa)
sudo reboot
```

### 2. Entorno virtual e instalación

```bash
cd ~/lowcost-hsi      # ruta del repo en la Pi

# venv que además ve las libs de sistema (GPIO suele venir por apt)
python3 -m venv --system-site-packages .venv
source .venv/bin/activate

# backend + hardware (extras declarados en pyproject.toml)
pip install '.[server,rpi]'
```

Esto instala:

| Extra      | Paquetes                                                                            |
| ---------- | ----------------------------------------------------------------------------------- |
| `server` | `fastapi`, `uvicorn[standard]`, `pyserial`, `matplotlib`, `pillow`        |
| `rpi`    | `RPi.GPIO`, `adafruit-blinka`, `adafruit-circuitpython-ads1x15`, `gpiozero` |

(`numpy` entra como dependencia base del proyecto.)

> **Nota:** el extra `rpi` **solo instala en una Raspberry Pi**. Esas ruedas no
> existen para macOS/Windows; por eso están separadas del `server`.

### 3. Arrancar el servidor

```bash
uvicorn rpi_server:app --host 0.0.0.0 --port 8000
```

Déjalo corriendo. Comprueba que responde:

```bash
curl http://localhost:8000/health
# → {"sensor_connected": false, "sensor_name": null, "cnc_connected": false}
```

`--host 0.0.0.0` hace que sea accesible desde la red (para que el panel en la
Mac lo alcance). Anota la **IP de la Pi** (`hostname -I`).

---

## Caso B — Mac / PC (panel de control)

La UI es un panel web NiceGUI. No necesita hardware.

### 1. Entorno virtual e instalación

```bash
cd lowcost-hsi

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install '.[ui]'              # nicegui, requests, plotly, pillow
```

### 2. Arrancar el panel

```bash
python run_ui.py                 # → http://localhost:8080
```

no se pueden obserbar las fotos 1Para usar otro puerto (p. ej. si el 8080 está ocupado):

```bash
PORT=8090 python run_ui.py       # → http://localhost:8090
```

### 3. Conectar con la Raspberry

En el navegador, pestaña **Configuración**:

1. Pon la **URL del servidor** (`http://<IP-de-la-Pi>:8000`) y pulsa
   **Conectar RPI**. Los tres indicadores del encabezado (RPI / Sensor / CNC)
   reflejan el estado.
2. **Conectar sensor** (driver `improved` recomendado: hace resta de dark y
   detecta saturación).
3. **Conectar CNC** (puerto, p. ej. `/dev/ttyUSB0`).
4. Ajusta los **parámetros de grilla** y pulsa **Guardar configuración**.

> Sin backend corriendo, el panel **carga y se ve**, pero *"Conectar RPI"* dará
> error de conexión. Es lo esperado.

---

## Caso C — Todo en la Raspberry (opcional)

Si prefieres operar desde una pantalla conectada a la propia Pi, corre **ambos**
procesos ahí: el backend como en el Caso A, y además el panel:

```bash
pip install '.[ui]'              # en el mismo venv de la Pi
python run_ui.py                 # y en el panel usa rpi_url = http://localhost:8000
```

---

## Configuración (`scanner_config.json`)

El panel lee y guarda un único archivo de config en la raíz del repo. Se crea/
actualiza al pulsar **Guardar configuración**.

| Clave                    | Significado                                |
| ------------------------ | ------------------------------------------ |
| `rpi_url`              | URL del backend (`http://<ip>:8000`)     |
| `cnc_port`             | Puerto serial de la CNC (`/dev/ttyUSB0`) |
| `x_start`, `y_start` | Esquina de arranque de la grilla (mm)      |
| `z_focus`              | Altura de trabajo del sensor (mm)          |
| `cols`, `rows`       | Nº de puntos en X e Y                     |
| `step_mm`              | Separación entre puntos (mm)              |
| `exposition`           | Tiempo de integración (**ms**)      |
| `accum`                | Acumulaciones (promedio por punto)         |

---

## Flujo de una captura y dónde quedan los datos

1. En **En vivo**, verifica señal antes de barrer: pico alto **sin saturación**
   (ajusta `exposition`/`accum`).
2. En **Escaneo**, pulsa **Iniciar**. El backend recorre la grilla en zig-zag,
   mide cada punto y va guardando.
3. Al terminar, en la **Raspberry** queda una carpeta por escaneo:

```
scans/scan_<timestamp>/
├── espectros.csv        # una fila por punto: col,row,x_mm,y_mm,z_mm,<λ>nm...  (CRUDO)
├── imagen_rgb.png       # vista previa RGB por bandas
├── config.json          # parámetros usados + resultados + resumen ambiental
├── progreso.json        # checkpoint (permite reanudar un escaneo cortado)
├── ambiente.csv         # serie T/HR del ESP8266 durante la captura
└── ambiente_puntos.csv  # T/HR vigente en cada punto: col,row,ts,temp_c,hum_pct,edad_s
```

> Los espectros se guardan **crudos** (`intensity_raw`): toda la corrección
> (dark, blanco/negro) la aplica el pipeline de análisis, de forma reproducible
> y sin corregir dos veces.

### Temperatura y humedad (ESP8266 + DHT11)

Un ESP8266 con un DHT11 empuja lecturas cada ~2 s a `POST /data_temp` con
`{"temperature": <float>, "humidity": <float>}` (opcionalmente `"device"`). El
servidor las guarda en `ambiente.csv` (raíz del proyecto, histórico completo) y,
mientras hay un escaneo en curso, también dentro de su carpeta, más una fila por
punto en `ambiente_puntos.csv`. Una lectura de más de 30 s se considera rancia:
se anota como hueco en vez de repetirse.

Esto existe por la **deriva térmica del dark current** documentada en
[adquisición de hardware](adquisicion-hardware.md): el barrido es en zig-zag, así
que el tiempo se mapea al eje Y de la imagen y el calentamiento aparece como un
gradiente vertical falso. Con la serie guardada por captura, ese efecto se puede
comprobar y descontar en vez de confundirlo con la muestra.

En el panel:

- **Chip del header** — T/HR actual siempre visible; ámbar si el ESP dejó de
  reportar, gris si nunca lo hizo.
- **Escaneo** — gráfica T/HR en vivo y deriva acumulada; si supera 2 °C, el
  registro avisa de que conviene recapturar la referencia oscura.
- **Resultados** — cada tarjeta resume el ambiente de esa captura, y el botón
  *Ambiente* abre su serie completa.

Endpoints: `GET /data_temp?n=<N>` (última lectura con su antigüedad + historia)
y `GET /scan/<subdir>/env` (serie de una captura, submuestreada).

---

## Análisis posterior (en cualquier máquina)

Con el `espectros.csv` descargado de la Pi:

```bash
pip install '.[viz]'      # matplotlib + pillow (opcional, para gráficas/imágenes)

# Preprocesamiento (corrección → Savitzky-Golay → outliers → Bartlett/KMO → PCA)
python preprocessing.py --captures-dir scans/scan_<ts>/espectros.csv

# Reconstrucción RGB (usa los índices col,row: reconstrucción exacta)
python legacy/Reconstruction/reconstruct_image.py --captures-dir scans/scan_<ts>
```

Ambos leen tanto el formato canónico (`espectros.csv`) como el legacy
(`captura_*.csv`) mediante `spectral_core.load_dataset`.

---

## Solución de problemas

| Síntoma                                             | Causa / solución                                                                                                                           |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `error: externally-managed-environment` (en la Pi) | Falta el venv. Crea y activa`.venv` (Caso A, paso 2).                                                                                     |
| `[Errno 48] address already in use`                | El puerto ya está ocupado. Libera con`lsof -i :8080` → `kill <PID>`, o usa `PORT=8090 python run_ui.py`.                            |
| `Sensor no disponible` al iniciar el backend       | Faltan libs de hardware o el I2C está desactivado. Revisa`raspi-config` y `pip install '.[rpi]'`.                                      |
| El panel no conecta a la Pi                          | Verifica IP/puerto, que el backend corre con`--host 0.0.0.0` y que ambos están en la misma red. Prueba `curl http://<ip>:8000/health`. |
| `NotOpenSSLWarning` de urllib3                     | Aviso inofensivo (LibreSSL en macOS). Se puede ignorar.                                                                                     |
| Saturación en el escaneo                            | Baja`exposition` o revisa la iluminación. La saturación se detecta sobre el crudo y se avisa en el log.                                 |

---

## Resumen de comandos

```bash
# Raspberry Pi (backend)
python3 -m venv --system-site-packages .venv && source .venv/bin/activate
pip install '.[server,rpi]'
uvicorn rpi_server:app --host 0.0.0.0 --port 8000

# Mac / PC (panel)
python3 -m venv .venv && source .venv/bin/activate
pip install '.[ui]'
python run_ui.py            # http://localhost:8080
```
