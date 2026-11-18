#!/usr/bin/env python3
import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import serial

from Sensor import SpectralReading, NUM_CHANNELS, pi4_hama


@dataclass
class GrblLimits:
    x_max: float
    y_max: float


class GrblClient:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 0.5, verbose: bool = True):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.verbose = verbose
        self.ser: Optional[serial.Serial] = None

    def open(self):
        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        time.sleep(2.0)
        # Wake GRBL
        self.ser.write(b"\r\n\r\n")
        time.sleep(0.5)
        self.ser.reset_input_buffer()

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def _readline(self) -> str:
        assert self.ser is not None
        return self.ser.readline().decode(errors="ignore").strip()

    def send(self, cmd: str, wait_ok: bool = True, extra_wait: float = 0.0) -> List[str]:
        """Envía un comando y espera ok/error/ALARM."""
        assert self.ser is not None
        if self.verbose:
            print(f">> {cmd}")

        self.ser.write((cmd + "\n").encode())

        if extra_wait:
            time.sleep(extra_wait)

        if not wait_ok:
            return []

        lines: List[str] = []
        while True:
            line = self._readline()
            if not line:
                continue
            lines.append(line)
            if self.verbose:
                print(f"<< {line}")

            if line.startswith(("ok", "error", "ALARM")):
                break

        # Abort temprano si hay alarmas
        for l in lines:
            if l.startswith("ALARM"):
                msg = f"GRBL en ALARM: {l}"
                if "ALARM:3" in l:
                    msg += " (límite físico activado). Comprueba que --safe-z y --z-safe-place estén dentro del recorrido del eje Z de tu máquina."
                raise RuntimeError(msg)
        return lines

    def query_status(self, retries: int = 3, wait_s: float = 2.5) -> str:
        """Retorna una línea tipo <Idle|...>. Reintenta si GRBL tarda (p. ej. tras un movimiento)."""
        assert self.ser is not None
        last_error: Optional[str] = None
        for attempt in range(retries):
            self.ser.write(b"?\n")
            t0 = time.time()
            while time.time() - t0 < wait_s:
                line = self._readline()
                if line.startswith("<") and line.endswith(">"):
                    if self.verbose:
                        print(f"<< {line}")
                    return line
                if line and not line.startswith("<"):
                    last_error = line
            if attempt < retries - 1:
                time.sleep(0.2)
        raise RuntimeError(
            "No se pudo leer status con '?'. "
            "GRBL puede no responder de inmediato tras un movimiento; reintenta o revisa la conexión."
            + (f" Última línea: {last_error}" if last_error else "")
        )

    def get_settings(self) -> Dict[str, str]:
        """Lee $$ y devuelve dict { '$130': '400.000', ... }."""
        out = self.send("$$")
        settings: Dict[str, str] = {}
        for line in out:
            # Formato: $130=400.000 (X axis max travel:mm)
            m = re.match(r"^\s*(\$\d+)\s*=\s*([-\d\.]+)\s*", line)
            if m:
                settings[m.group(1)] = m.group(2)
        return settings

    def get_limits(self) -> GrblLimits:
        s = self.get_settings()
        try:
            x = float(s["$130"])
            y = float(s["$131"])
        except KeyError:
            raise RuntimeError("No encontré $130/$131 en $$; no puedo determinar límites.")
        return GrblLimits(x_max=x, y_max=y)


def get_current_position(grbl: GrblClient) -> Tuple[float, float, float]:
    """Obtiene posición actual (MPos) desde GRBL. Devuelve (x, y, z) en mm."""
    status = grbl.query_status()
    m = re.search(r"MPos:([-\d.]+),([-\d.]+),([-\d.]+)", status)
    if not m:
        raise RuntimeError(f"No se pudo parsear MPos en status: {status}")
    return (float(m.group(1)), float(m.group(2)), float(m.group(3)))


def wait_until_idle(
    grbl: GrblClient,
    timeout_s: float = 60.0,
    poll_interval_s: float = 0.1,
    expect_motion: bool = False,
) -> None:
    """Espera a que GRBL reporte estado Idle (máquina parada). Lanza RuntimeError si se supera timeout_s.

    Si expect_motion=True, primero espera a ver un estado NO-Idle (Run/Jog)
    para evitar retornar en un Idle residual antes de que el movimiento inicie.
    """
    t0 = time.time()

    if expect_motion:
        # Fase 1: esperar a que GRBL salga de Idle (confirma que el movimiento arrancó)
        while time.time() - t0 < timeout_s:
            status = grbl.query_status()
            if "Idle" not in status:
                break
            time.sleep(poll_interval_s)

    # Fase 2: esperar a que GRBL vuelva a Idle (movimiento completado)
    while time.time() - t0 < timeout_s:
        status = grbl.query_status()
        if "Idle" in status:
            return
        time.sleep(poll_interval_s)
    raise RuntimeError(
        f"GRBL no pasó a Idle en {timeout_s:.0f} s. Revisa la conexión y que la máquina no esté en ALARM."
    )


def do_capture(
    grbl: GrblClient,
    hama: "pi4_hama",
    nm: List[float],
    exposition: float = 1e-2,
    accumulations: int = 1,
    output_dir: str = "captures",
    point_index: int = 0,
    capture_delay_s: float = 0.015,
) -> None:
    """Ejecuta una captura en el punto actual: lee el C12880MA y guarda 288 canales en CSV."""
    x, y, z = get_current_position(grbl)
    time.sleep(capture_delay_s)

    spectra = hama.measure(exposition, accumulations)
    reading = SpectralReading(
        channels=spectra,
        wavelengths=nm,
        exposition=exposition,
        accumulations=accumulations,
    )

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"captura_{point_index:04d}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SpectralReading.csv_header())
        w.writerow(reading.to_csv_row(x, y, z))

    print("captura")
    print(f"  X={x:.3f}  Y={y:.3f}  Z={z:.3f}  -> {path}")


def compute_centered_grid(limits: GrblLimits, width: float, height: float) -> Tuple[float, float, float, float]:
    """Devuelve (x0, y0, x1, y1) del rectángulo centrado en el centro máquina."""
    cx = limits.x_max / 2.0
    cy = limits.y_max / 2.0

    x0 = cx - width / 2.0
    x1 = cx + width / 2.0
    y0 = cy - height / 2.0
    y1 = cy + height / 2.0
    return x0, y0, x1, y1


def validate_within_limits(x0: float, y0: float, x1: float, y1: float, limits: GrblLimits, margin: float = 0.0):
    if x0 < 0 + margin or y0 < 0 + margin or x1 > limits.x_max - margin or y1 > limits.y_max - margin:
        raise ValueError(
            f"La grilla se sale de límites.\n"
            f"Grilla: X[{x0:.3f}, {x1:.3f}] Y[{y0:.3f}, {y1:.3f}]\n"
            f"Límites CNC: X[0, {limits.x_max:.3f}] Y[0, {limits.y_max:.3f}]\n"
            f"Reduce width/height o ajusta margen."
        )


def frange_steps(start: float, end: float, step: float) -> List[float]:
    """Incluye start y end aproximado en pasos; garantiza último punto <= end."""
    pts = []
    v = start
    eps = 1e-9
    while v <= end + eps:
        pts.append(round(v, 3))
        v += step
    # Si por redondeo se pasa, recorta
    pts = [p for p in pts if p <= end + eps]
    return pts


def run_serpentine(
    grbl: GrblClient,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    step: float,
    feed: float,
    safe_z: float,
    hama: "pi4_hama" = None,
    nm: List[float] = None,
    exposition: float = 1e-2,
    accumulations: int = 1,
    dwell_s: float = 0.0,
    z_safe_place: Optional[float] = None,
    output_dir: str = "captures",
    capture_delay_s: float = 0.015,
):
    """Recorrido en serpiente. Capturas empiezan en la primera posición; en cada punto se usa z_safe_place (cámara espectral)."""
    if z_safe_place is None:
        z_safe_place = safe_z

    grbl.send("G21")          # mm
    grbl.send("G90")          # absoluto
    grbl.send(f"G0 Z{safe_z}")  # Z seguro

    xs = frange_steps(x0, x1, step)
    ys = frange_steps(y0, y1, step)

    # Ir a la posición 0 de la grilla y esperar antes de iniciar
    grbl.send(f"G0 X{xs[0]} Y{ys[0]}")
    wait_until_idle(grbl, expect_motion=True)
    time.sleep(1.0)
    print("Listo para iniciar la captura y el recorrido.")
    os.makedirs(output_dir, exist_ok=True)

    point_index = 0
    first_point = True
    for row, y in enumerate(ys):
        if row % 2 == 0:
            x_iter = xs
        else:
            x_iter = list(reversed(xs))

        for x in x_iter:
            if not first_point:
                grbl.send(f"G1 X{x} Y{y} F{feed}")
                wait_until_idle(grbl, expect_motion=True)
            else:
                first_point = False

            _x, _y, z_now = get_current_position(grbl)
            if abs(z_now - z_safe_place) > 0.01:
                grbl.send(f"G0 Z{z_safe_place}")
                wait_until_idle(grbl, expect_motion=True)
            do_capture(grbl, hama=hama, nm=nm, exposition=exposition,
                       accumulations=accumulations, output_dir=output_dir,
                       point_index=point_index, capture_delay_s=capture_delay_s)
            point_index += 1
            if dwell_s > 0:
                grbl.send(f"G4 P{dwell_s}", extra_wait=0.0)

    wait_until_idle(grbl)
    print("✅ Recorrido terminado.")


def parse_args():
    p = argparse.ArgumentParser(
        description="Recorrido en grilla centrada en el centro de la CNC (serpiente). Lee límites desde $130/$131."
    )
    p.add_argument("--port", default="/dev/ttyUSB0", help="Puerto serial (ej: /dev/ttyUSB0)")
    p.add_argument("--baud", type=int, default=115200, help="Baudrate (default 115200)")
    p.add_argument("--width", type=float, required=False, help="Ancho de grilla en mm (ej: 250)")
    p.add_argument("--height", type=float, required=False, help="Alto de grilla en mm (ej: 120)")
    p.add_argument("--step", type=float, required=False, help="Paso en mm (distancia entre puntos) (ej: 5)")
    p.add_argument("--feed", type=float, required=False, help="Velocidad F en mm/min (ej: 800)")
    p.add_argument("--safe-z", type=float, default=10.0, help="Altura Z segura (mm)")
    p.add_argument("--z-safe-place", type=float, default=None,
                    help="Altura Z para captura / cámara espectral (default: igual que --safe-z)")
    p.add_argument("--output-dir", type=str, default="captures",
                   help="Carpeta donde se guardan los CSV de cada punto (x,y,z + 288 canales)")
    p.add_argument("--capture-delay", type=float, default=0.015,
                   help="Tiempo en segundos por captura (C1288MA ~10 ms + guardado, default 0.015)")
    p.add_argument("--exposition", type=float, default=1e-2,
                   help="Tiempo de exposicion del sensor C12880MA en segundos (default 1e-2)")
    p.add_argument("--accumulations", type=int, default=1,
                   help="Acumulaciones espectrales del sensor C12880MA (default 1)")
    p.add_argument("--margin", type=float, default=0.0, help="Margen a respetar dentro de los límites (mm)")
    p.add_argument("--dwell", type=float, default=0.0, help="Pausa por punto en segundos (G4 P)")
    p.add_argument("--home", action="store_true", help="Ejecuta $H antes de empezar (requiere homing funcional)")
    p.add_argument("--unlock", action="store_true", help="Ejecuta $X antes de empezar (si estabas en ALARM)")
    p.add_argument("--hard-limits", choices=["keep", "off"], default="keep",
                   help="keep: no toca $21, off: pone $21=0 temporalmente")
    p.add_argument("--quiet", action="store_true", help="Menos logs")
    return p.parse_args()


def prompt_if_missing(args) -> Tuple[float, float, float, float]:
    def ask_float(label: str, default: Optional[float] = None) -> float:
        while True:
            d = f" [{default}]" if default is not None else ""
            val = input(f"{label}{d}: ").strip()
            if not val and default is not None:
                return float(default)
            try:
                return float(val)
            except ValueError:
                print("Valor inválido. Intenta de nuevo.")

    width = args.width if args.width is not None else ask_float("Ancho de grilla (mm)", 250.0)
    height = args.height if args.height is not None else ask_float("Alto de grilla (mm)", 120.0)
    step = args.step if args.step is not None else ask_float("Paso entre puntos (mm)", 5.0)
    feed = args.feed if args.feed is not None else ask_float("Velocidad feed F (mm/min)", 800.0)
    return width, height, step, feed


def main():
    args = parse_args()
    verbose = not args.quiet

    width, height, step, feed = prompt_if_missing(args)

    if step <= 0:
        print("❌ step debe ser > 0")
        sys.exit(1)

    # Inicializar sensor C12880MA
    print("Inicializando sensor C12880MA...")
    hama = pi4_hama()
    nm = hama.wavelength().tolist()
    hama.measure(1e-6, 1)  # warm-up: primera lectura tras encendido es ruido
    print("Sensor C12880MA listo.")

    grbl = GrblClient(args.port, args.baud, verbose=verbose)
    try:
        grbl.open()

        # Opcionales de arranque
        if args.hard_limits == "off":
            grbl.send("$21=0")
        if args.unlock:
            grbl.send("$X")
        if args.home:
            grbl.send("$H", extra_wait=0.0)  # puede tardar; GRBL irá respondiendo

        limits = grbl.get_limits()
        print(f"📏 Límites CNC: X=0..{limits.x_max:.3f} mm, Y=0..{limits.y_max:.3f} mm")

        x0, y0, x1, y1 = compute_centered_grid(limits, width, height)
        validate_within_limits(x0, y0, x1, y1, limits, margin=args.margin)

        cx = limits.x_max / 2.0
        cy = limits.y_max / 2.0
        print(f"🎯 Centro máquina: X{cx:.3f} Y{cy:.3f}")
        print(f"🧩 Grilla centrada: X[{x0:.3f}, {x1:.3f}] Y[{y0:.3f}, {y1:.3f}]  step={step}  feed={feed}")

        # Status pre
        try:
            st = grbl.query_status()
            print(f"🟢 Estado: {st}")
        except Exception:
            pass

        run_serpentine(grbl, x0, y0, x1, y1, step, feed, args.safe_z,
                      hama=hama, nm=nm,
                      exposition=args.exposition, accumulations=args.accumulations,
                      dwell_s=args.dwell, z_safe_place=args.z_safe_place,
                      output_dir=args.output_dir, capture_delay_s=args.capture_delay)

    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
    finally:
        grbl.close()
        hama.cleanup()


if __name__ == "__main__":
    main()