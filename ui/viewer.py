"""Inspector espectral: navegación canal a canal de un escaneo ya capturado.

Diálogo modal hermano del etiquetador (``open_labeler`` en ``app.py``): aquel
*escribe* (marca píxeles con un nivel y persiste la segmentación), este solo
*lee* (mapa de calor del canal activo, superficie 3D y espectro del píxel).

Reemplaza al antiguo ``viewer.py`` de la raíz (matplotlib de escritorio), que
solo abría el formato B legacy (``captura_*.csv`` + ``manifest.json``) que el
servidor ya no genera.

Modelo de datos: el ``espectros.csv`` del escaneo se descarga UNA vez desde la
Raspberry y queda cacheado en ``scans/<subdir>/``. El cubo (filas, columnas,
canales) vive en la RAM del proceso de la UI (tu Mac), y al navegador solo
viaja el corte del canal visible. Consecuencia útil: con el CSV ya cacheado se
puede inspeccionar con la Raspberry apagada.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
from nicegui import run, ui

from spectral_core import load_dataset
from ui.theme import (
    ACCENT2,
    INK,
    PLOTLY_LAYOUT,
    WARN,
    event_points,
    make_fig,
    update_plot,
)

# Caché local de escaneos: misma carpeta ``scans/`` del repo.
CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scans"
)

# El espectros.csv completo pesa MB (~3 MB en 20×20, decenas en grillas
# grandes) y viaja por el WiFi de la Raspberry: nada que ver con los 5 s de las
# llamadas de control.
DOWNLOAD_TIMEOUT = 300

COLORSCALE = "Inferno"       # equivalente al cmap por defecto del visor antiguo
CHANNEL_COLOR = WARN         # indicador del canal activo sobre el espectro


# ── Caché local del CSV ────────────────────────────────────────────────────────
def local_csv_path(subdir: str) -> str:
    return os.path.join(CACHE_DIR, subdir, "espectros.csv")


def is_cached(subdir: str) -> bool:
    path = local_csv_path(subdir)
    return os.path.isfile(path) and os.path.getsize(path) > 0


def download_csv(rpi, subdir: str) -> str:
    """Baja el ``espectros.csv`` del escaneo a la caché local y devuelve la ruta.

    Escribe en un ``.part`` y renombra al final: si la descarga se corta, la
    caché nunca queda con un CSV truncado que luego se daría por bueno.
    """
    path = local_csv_path(subdir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.part"

    r = rpi.get(f"/scans/{subdir}/espectros.csv",
                timeout=DOWNLOAD_TIMEOUT, stream=True)
    r.raise_for_status()
    try:
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


# ── Cubo espectral ─────────────────────────────────────────────────────────────
@dataclass
class Cube:
    """Cubo (filas, columnas, canales) listo para cortar por canal.

    ``data[row, col, ch]`` en ADC; NaN en los puntos que el escaneo no cubrió.
    """

    data: np.ndarray
    wavelengths: np.ndarray
    mean_spectrum: np.ndarray
    vmin: float
    vmax: float

    @property
    def n_rows(self) -> int:
        return self.data.shape[0]

    @property
    def n_cols(self) -> int:
        return self.data.shape[1]

    @property
    def n_ch(self) -> int:
        return self.data.shape[2]


def build_cube(scan) -> Cube:
    """Arma el :class:`Cube` de un escaneo en formato A (con índices col/row).

    A diferencia del visor antiguo no hay que inferir la grilla redondeando las
    coordenadas en mm: el formato A ya trae ``grid_index`` explícito.
    """
    if scan.grid_index is None:
        raise ValueError(
            "El escaneo no trae índices (col,row); no se puede reconstruir el cubo."
        )
    gi = np.asarray(scan.grid_index)
    cols = gi[:, 0].astype(int)
    rows = gi[:, 1].astype(int)

    n_cols = int(cols.max()) + 1
    n_rows = int(rows.max()) + 1
    n_ch = int(scan.profiles.shape[1])

    data = np.full((n_rows, n_cols, n_ch), np.nan, dtype=float)
    data[rows, cols] = scan.profiles

    # Sobre profiles (sin NaN), no sobre el cubo: evita el aviso de nanmean
    # cuando la grilla quedó incompleta.
    mean_spectrum = np.asarray(scan.profiles, dtype=float).mean(axis=0)

    finite = data[np.isfinite(data)]
    vmin = float(finite.min()) if finite.size else 0.0
    vmax = float(finite.max()) if finite.size else 1.0

    return Cube(
        data=data,
        wavelengths=np.asarray(scan.wavelengths, dtype=float),
        mean_spectrum=mean_spectrum,
        vmin=vmin,
        vmax=vmax,
    )


def load_cube(rpi, subdir: str, force: bool = False) -> Cube:
    """Asegura el CSV en caché, lo parsea y devuelve el cubo. Bloqueante."""
    if force or not is_cached(subdir):
        download_csv(rpi, subdir)
    return build_cube(load_dataset(local_csv_path(subdir)))


# ── Figuras Plotly ─────────────────────────────────────────────────────────────
def _jsonable(arr) -> list:
    """Convierte a lista con ``None`` en vez de NaN.

    Plotly interpreta ``null`` como hueco; ``NaN`` crudo produce JSON inválido.
    """
    a = np.asarray(arr, dtype=float)
    out = a.tolist()
    mask = ~np.isfinite(a)
    if not mask.any():
        return out
    if a.ndim == 1:
        return [None if m else v for v, m in zip(out, mask)]
    return [
        [None if m else v for v, m in zip(row_v, row_m)]
        for row_v, row_m in zip(out, mask)
    ]


def _clim(cube: Cube, ch: int, autoscale: bool) -> tuple[float, float]:
    """Límites de color: rango global del cubo, o del canal si hay autoescala."""
    if not autoscale:
        return cube.vmin, cube.vmax
    layer = cube.data[:, :, ch]
    finite = layer[np.isfinite(layer)]
    if finite.size == 0:
        return cube.vmin, cube.vmax
    lo, hi = float(finite.min()), float(finite.max())
    return (lo, hi) if hi > lo else (lo, lo + 1.0)


def _colorbar() -> dict:
    return {
        "title": {"text": "ADC", "font": {"color": "#7b86a3", "size": 10}},
        "tickfont": {"color": "#7b86a3", "size": 9},
        "outlinewidth": 0,
        "thickness": 12,
    }


def map_fig(cube: Cube, ch: int, mode: str, autoscale: bool,
            sel: tuple[int, int] | None, uirev: str) -> dict:
    """Mapa del canal activo: heatmap 2D o superficie 3D.

    ``uirevision`` mantiene el zoom (2D) y el ángulo de cámara (3D) al cambiar
    de canal: es lo que el visor antiguo resolvía guardando elev/azim a mano.
    """
    layer = cube.data[:, :, ch]
    lo, hi = _clim(cube, ch, autoscale)
    wl = float(cube.wavelengths[ch])
    title = f"[{mode.upper()}]  Canal {ch:03d} / {cube.n_ch - 1}  ·  {wl:.1f} nm"

    common_layout = {
        "paper_bgcolor": "rgba(11,20,55,0)",
        "plot_bgcolor": "rgba(4,8,22,0.35)",
        "font": {"color": "#a0aec0", "size": 11},
        "margin": {"l": 45, "r": 10, "t": 36, "b": 40},
        "showlegend": False,
        "uirevision": uirev,
        "title": {"text": title, "font": {"color": INK, "size": 13}},
    }

    if mode == "3d":
        data = [{
            "type": "surface",
            "z": _jsonable(layer),
            "colorscale": COLORSCALE,
            "cmin": lo, "cmax": hi,
            "colorbar": _colorbar(),
            "hovertemplate": "col=%{x} · row=%{y}<br>%{z:.0f} ADC<extra></extra>",
        }]
        axis = {"color": "#7b86a3", "gridcolor": "rgba(255,255,255,0.10)",
                "showbackground": False}
        common_layout["scene"] = {
            "xaxis": {**axis, "title": "col"},
            # row 0 arriba, igual que la imagen RGB y el etiquetador.
            "yaxis": {**axis, "title": "row", "autorange": "reversed"},
            "zaxis": {**axis, "title": "ADC"},
            "aspectmode": "cube",
        }
        return {"data": data, "layout": common_layout}

    data = [{
        "type": "heatmap",
        "z": _jsonable(layer),
        "colorscale": COLORSCALE,
        "zmin": lo, "zmax": hi,
        "colorbar": _colorbar(),
        "hovertemplate": "col=%{x} · row=%{y}<br>%{z:.0f} ADC<extra></extra>",
    }]
    if sel is not None:
        col, row = sel
        data.append({
            "type": "scatter", "mode": "markers",
            "x": [col], "y": [row],
            "marker": {"symbol": "x-thin", "size": 11,
                       "line": {"color": CHANNEL_COLOR, "width": 2.5}},
            "hoverinfo": "skip",
        })
    common_layout["xaxis"] = {
        "title": "col", "color": "#7b86a3", "scaleanchor": "y",
        "constrain": "domain", "showgrid": False, "zeroline": False,
    }
    common_layout["yaxis"] = {
        "title": "row", "color": "#7b86a3", "autorange": "reversed",
        "showgrid": False, "zeroline": False,
    }
    return {"data": data, "layout": common_layout}


def spectrum_fig(cube: Cube, ch: int, sel: tuple[int, int] | None) -> dict:
    """Espectro completo del píxel (o el medio del escaneo) con el canal marcado."""
    if sel is None:
        spectrum = cube.mean_spectrum
        title = "Espectro medio — haz clic en el mapa para elegir un píxel"
    else:
        col, row = sel
        spectrum = cube.data[row, col, :]
        title = f"Espectro del píxel  col={col} · row={row}"

    wl = cube.wavelengths
    fig = make_fig(_jsonable(spectrum), wl.tolist(), title)

    x = float(wl[ch])
    val = float(spectrum[ch]) if np.isfinite(spectrum[ch]) else None

    layout = fig["layout"]
    layout["title"] = {"text": title, "font": {"color": INK, "size": 12}}
    layout["margin"] = {"l": 55, "r": 15, "t": 34, "b": 42}
    # Copia la lista: make_fig la construye sobre una copia superficial de
    # PLOTLY_LAYOUT y no conviene mutarla en sitio.
    layout["shapes"] = list(layout.get("shapes", [])) + [{
        "type": "line", "xref": "x", "yref": "paper",
        "x0": x, "x1": x, "y0": 0, "y1": 1,
        "line": {"color": CHANNEL_COLOR, "width": 1.4, "dash": "dash"},
    }]

    if val is not None:
        fig["data"] = list(fig["data"]) + [{
            "type": "scatter", "mode": "markers",
            "x": [x], "y": [val],
            "marker": {"color": CHANNEL_COLOR, "size": 8},
            "hovertemplate": f"Canal {ch:03d} · %{{x:.1f}} nm<br>%{{y:.0f}} ADC<extra></extra>",
        }]
    return fig


# ── Diálogo ────────────────────────────────────────────────────────────────────
async def open_viewer(rpi, subdir: str) -> None:
    """Abre el inspector espectral de un escaneo (diálogo modal).

    Descarga el CSV si no está cacheado. La carga es pesada (MB por red +
    parseo), así que corre en un hilo con ``run.io_bound`` para no bloquear el
    bucle de eventos de NiceGUI mientras tanto.
    """
    cached = is_cached(subdir)
    notif = ui.notification(
        f"Abriendo {subdir}…" if cached else f"Descargando {subdir} de la Raspberry…",
        spinner=True, timeout=None, close_button=False,
    )
    try:
        cube = await run.io_bound(load_cube, rpi, subdir)
    except Exception as e:
        notif.dismiss()
        ui.notify(f"No se pudo abrir el escaneo: {e}", color="negative")
        return
    notif.dismiss()

    st = {"ch": 0, "mode": "2d", "auto": False, "sel": None}
    uirev = {"2d": f"{subdir}-2d", "3d": f"{subdir}-3d"}

    def redraw_map():
        update_plot(plot, map_fig(cube, st["ch"], st["mode"], st["auto"],
                                  st["sel"], uirev[st["mode"]]))

    def redraw_spectrum():
        update_plot(spec_plot, spectrum_fig(cube, st["ch"], st["sel"]))

    def set_channel(ch: int):
        ch = int(np.clip(ch, 0, cube.n_ch - 1))
        if ch == st["ch"]:
            return
        st["ch"] = ch
        lbl_ch.set_text(f"Canal {ch:03d}  ·  {cube.wavelengths[ch]:.1f} nm")
        redraw_map()
        redraw_spectrum()

    def step_channel(delta: int):
        ch = int(np.clip(st["ch"] + delta, 0, cube.n_ch - 1))
        slider.value = ch          # no dispara el evento de cliente
        set_channel(ch)

    def on_slider(e):
        try:
            set_channel(int(e.args))
        except (TypeError, ValueError):
            pass

    def toggle_mode():
        st["mode"] = "3d" if st["mode"] == "2d" else "2d"
        btn_mode.set_text("Ver en 2D" if st["mode"] == "3d" else "Ver en 3D")
        redraw_map()

    def toggle_auto(e):
        st["auto"] = bool(e.value)
        redraw_map()

    def on_map_click(e):
        points = event_points(e)
        if not points:
            return
        p = points[0]
        col, row = p.get("x"), p.get("y")
        if col is None or row is None:
            return
        col = int(np.clip(int(round(col)), 0, cube.n_cols - 1))
        row = int(np.clip(int(round(row)), 0, cube.n_rows - 1))
        st["sel"] = (col, row)
        redraw_spectrum()
        if st["mode"] == "2d":
            redraw_map()   # reposiciona la X del píxel elegido

    async def reload_csv():
        n = ui.notification(f"Volviendo a descargar {subdir}…",
                            spinner=True, timeout=None, close_button=False)
        try:
            fresh = await run.io_bound(load_cube, rpi, subdir, True)
        except Exception as exc:
            n.dismiss()
            ui.notify(f"No se pudo recargar: {exc}", color="negative")
            return
        n.dismiss()
        cube.data = fresh.data
        cube.wavelengths = fresh.wavelengths
        cube.mean_spectrum = fresh.mean_spectrum
        cube.vmin, cube.vmax = fresh.vmin, fresh.vmax
        st["sel"] = None
        redraw_map()
        redraw_spectrum()
        ui.notify("Escaneo recargado desde la Raspberry", color="positive")

    with ui.dialog() as dlg, ui.card().style(
        "width:1150px;max-width:96vw;background:var(--bg2)"
    ):
        with ui.row().classes("w-full items-center justify-between"):
            ui.html('<div class="sec" style="margin-bottom:0">Inspector espectral</div>',
                    sanitize=False)
            ui.button(icon="close", on_click=dlg.close).props("flat round color=grey")

        with ui.row().classes("w-full gap-4 items-start no-wrap"):
            # ── Mapa del canal activo ──
            with ui.column().classes("gap-2").style("flex:2;min-width:420px"):
                plot = ui.plotly(
                    map_fig(cube, 0, "2d", False, None, uirev["2d"])
                ).classes("w-full").style("min-height:440px")
                plot.on("plotly_click", on_map_click)

                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.button(icon="chevron_left", on_click=lambda: step_channel(-1)) \
                        .props("flat dense color=cyan")
                    slider = ui.slider(min=0, max=cube.n_ch - 1, value=0) \
                        .props("label-always").classes("flex-1")
                    slider.on("update:model-value", on_slider, throttle=0.12)
                    ui.button(icon="chevron_right", on_click=lambda: step_channel(1)) \
                        .props("flat dense color=cyan")

                ui.label(
                    f"{cube.n_rows} × {cube.n_cols} px  ·  {cube.n_ch} canales  ·  "
                    f"{cube.wavelengths[0]:.0f}–{cube.wavelengths[-1]:.0f} nm"
                ).classes("text-xs").style("color:var(--muted2)")

            # ── Panel derecho ──
            with ui.column().classes("gap-3").style("flex:1;min-width:280px"):
                lbl_ch = ui.label(
                    f"Canal 000  ·  {cube.wavelengths[0]:.1f} nm"
                ).classes("text-sm text-bold").style(f"color:{ACCENT2}")

                spec_plot = ui.plotly(
                    spectrum_fig(cube, 0, None)
                ).classes("w-full").style("min-height:260px")

                ui.separator()
                btn_mode = ui.button("Ver en 3D", on_click=toggle_mode) \
                    .props("outline color=cyan dense").classes("text-xs w-full")
                ui.switch("Autoescala del color", value=False, on_change=toggle_auto) \
                    .props("dense color=cyan").classes("text-xs")
                ui.label(
                    "Autoescala: el color se ajusta al rango del canal visible. "
                    "Apagada, usa el rango global del cubo (canales comparables entre sí)."
                ).classes("text-xs").style("color:var(--muted2)")

                ui.separator()
                ui.button("Volver a descargar", on_click=reload_csv) \
                    .props("flat dense color=grey").classes("text-xs w-full")
                ui.label("Copia local en scans/ · se puede inspeccionar sin la Raspberry") \
                    .classes("text-xs").style("color:var(--muted2)")

    dlg.open()
