"""Panel del escáner hiperespectral (NiceGUI, estilo Vision UI).

Cliente del backend REST ``rpi_server.py`` (sensor real C12880MA + CNC en la
Raspberry Pi). Sustituye a ``mac_ui.py`` y al antiguo modo por subprocesos:
una sola UI, conectada al hardware real.

Arranque:
    python run_ui.py            # en la Mac o en la propia Raspberry (localhost)
"""

from __future__ import annotations

import json
import threading
import time

from nicegui import app, ui

from ui.rpi_client import RpiClient, DEFAULT_CONFIG, load_config, normalize_url, save_config
from ui.theme import (
    VISION_CSS, make_fig, make_env_fig, PLOTLY_LAYOUT,
    # Helpers de Plotly: viven en theme.py para compartirlos con ui/viewer.py.
    update_plot as _update_plot,
    event_points as _event_points,
)
from ui.viewer import open_viewer, is_cached as viewer_is_cached

# Núcleo de etiquetado manual (segmentación) — módulos de la raíz del repo.
from labeling import (
    apply_label_colors,
    build_label_figure,
    build_pixel_grid,
    default_levels,
    label_summary,
    labels_from_json,
    labels_to_json,
)
from spectral_core import load_dataset

# ── Cliente + estado (single-user, como la UI anterior) ─────────────────────────
cfg = load_config()
# Repara en el arranque una rpi_url guardada sin esquema (o vacía): de lo
# contrario las imágenes de Resultados salen 404 contra el puerto de la UI.
cfg["rpi_url"] = normalize_url(cfg.get("rpi_url"))
rpi = RpiClient(cfg["rpi_url"])

SETTLE = 0.3  # asentamiento estimado de la CNC por punto (para el ETA)
# Lecturas ambientales que se piden para la gráfica (el ESP reporta cada ~2 s,
# así que 400 son ~13 min de historia) y ritmo del poll.
ENV_POINTS = 400
ENV_POLL_S = 3.0

state = {
    "rpi_reachable": False,
    "sensor_connected": False, "sensor_name": None,
    "cnc_connected": False,
    "cnc_position": {"x": 0.0, "y": 0.0, "z": 0.0},
    "live_running": False, "live_fig": None, "live_info": "",
    "scan_running": False, "scan_paused": False,
    "scan_progress": 0, "scan_total": 0, "scan_eta": "--",
    "scan_errors": 0, "scan_log": [],
    "past_scans": [],
    "analyzing": set(),        # subdirs con análisis en curso (spinner)
    "analysis_error": {},      # subdir -> último mensaje de error
    "pca_ver": {},             # subdir -> versión (cache-buster de pca.png)
    "calib": {"white": None, "dark": None},  # referencias W/D (desde el servidor)
    "calib_busy": None,        # "white"|"dark" mientras captura
    "calib_error": None,       # último error de captura
    "calib_dirty": True,       # pedir recarga de estado de calibración
    # Ambiente (ESP8266 + DHT11): última lectura con su antigüedad, resumen del
    # escaneo en curso y serie reciente para la gráfica.
    "env": None,
    "env_hist": [],
    **cfg,
}


# ── Workers (hilos de fondo; no tocan la UI directamente) ───────────────────────
def live_worker():
    """Mide en continuo y deja la figura en ``state`` para que la pinte un timer.

    NO se detiene si el sensor está caído: sigue reintentando, pero con un
    intervalo más largo mientras falla (evita inundar el servidor con 409) y
    vuelve al ritmo normal en cuanto el sensor responde.
    """
    expo = state["exposition"] / 1000.0
    while state["live_running"]:
        delay = 0.6  # ritmo normal
        try:
            r = rpi.post("/sensor/measure", json={"exposition": expo, "accum": 1})
            if r.status_code == 409:
                state["live_info"] = "Esperando sensor… (desconectado)"
                delay = 2.0  # sensor caído: reintenta despacio, sin inundar
            else:
                r.raise_for_status()
                result = r.json()
                spectrum = result["intensity_raw"]
                wl = result.get("wavelengths") or list(range(340, 340 + 288))
                sat = len(result["saturated_pixels"])
                peak_idx = spectrum.index(max(spectrum))
                info = f"Pico: {wl[peak_idx]:.1f} nm  ·  Máx: {max(spectrum):.0f} ADC"
                if sat:
                    info += f"  ·  ⚠ {sat} px saturados"
                state["live_fig"] = make_fig(spectrum, wl, "En vivo")
                state["live_info"] = info
        except Exception as e:
            state["live_info"] = f"Esperando sensor… ({e})"
            delay = 2.0
        time.sleep(delay)


def env_worker():
    """Trae del servidor las lecturas del ESP8266 (temperatura y humedad).

    Corre siempre, haya escaneo o no: el ESP empuja sus datos de forma asíncrona
    y el chip del header debe reflejar el ambiente también entre capturas. Si la
    Raspberry no responde, baja el ritmo en vez de inundarla.
    """
    while True:
        delay = ENV_POLL_S
        try:
            r = rpi.get(f"/data_temp?n={ENV_POINTS}")
            if r.ok:
                data = r.json()
                state["env"] = {k: data.get(k) for k in ("last", "age_s", "stale", "run")}
                state["env_hist"] = data.get("history") or []
            else:
                delay = 10.0
        except Exception:
            # Servidor caído o URL aún sin configurar: se reintenta despacio.
            delay = 10.0
        time.sleep(delay)


def scan_poll_worker():
    """Sincroniza el estado del escaneo desde la Raspberry Pi."""
    while state["scan_running"]:
        try:
            r = rpi.get("/scan/status")
            if r.ok:
                st = r.json()
                state.update({
                    "scan_running": st["running"],
                    "scan_paused": st["paused"],
                    "scan_progress": st["progress"],
                    "scan_total": st["total"],
                    "scan_eta": st["eta"],
                    "scan_errors": st["errors"],
                    "scan_log": st["log"],
                })
                if st.get("env"):
                    state["env"] = st["env"]
        except Exception:
            pass
        time.sleep(0.8)


def env_chip_html():
    """HTML del indicador de ambiente del header, coloreado por antigüedad.

    Verde = lectura fresca; ámbar = el ESP lleva rato sin reportar (el dato que
    se ve ya no describe el ambiente); gris = nunca llegó nada.
    """
    env = state.get("env") or {}
    last = env.get("last")
    if not last:
        return ('<span class="sdot doff"></span>'
                '<span class="text-xs">Ambiente --</span>')
    txt = f'{last["temperature"]:.1f} °C · {last["humidity"]:.0f} % HR'
    if env.get("stale"):
        age = env.get("age_s")
        edad = f'{age/60:.0f} min' if age and age >= 60 else f'{age:.0f} s' if age else '?'
        return (f'<span class="sdot dwn"></span>'
                f'<span class="text-xs" style="color:var(--wn)">{txt} · hace {edad}</span>')
    return f'<span class="sdot dok"></span><span class="text-xs">{txt}</span>'


def env_summary_text(run):
    """Resumen ambiental de un escaneo en una línea (o None si no hubo datos)."""
    if not run or not run.get("n"):
        return None
    return (f'{run["t_start"]:.1f} → {run["t_end"]:.1f} °C '
            f'({run["t_drift"]:+.1f}) · {run["h_min"]:.0f}–{run["h_max"]:.0f} % HR')


# ── Editor de etiquetado manual (segmentación) ──────────────────────────────────
def _calib_fig(white, dark):
    """Figura Plotly (dict) con los espectros de referencia W y D disponibles."""
    data = []
    if white and white.get("spectrum"):
        wl = white.get("wavelengths") or list(range(340, 340 + len(white["spectrum"])))
        data.append({
            "x": wl, "y": white["spectrum"], "type": "scatter", "mode": "lines",
            "name": "Blanco (W)", "line": {"color": "#e7ecf7", "width": 2},
        })
    if dark and dark.get("spectrum"):
        wl = dark.get("wavelengths") or list(range(340, 340 + len(dark["spectrum"])))
        data.append({
            "x": wl, "y": dark["spectrum"], "type": "scatter", "mode": "lines",
            "name": "Negro (D)", "line": {"color": "#7b86a3", "width": 2},
        })
    layout = dict(PLOTLY_LAYOUT)
    layout["showlegend"] = True
    layout["legend"] = {"orientation": "h", "y": 1.12, "font": {"color": "#a0aec0"}}
    return {"data": data, "layout": layout}


def open_labeler(subdir: str):
    """Abre el editor de etiquetado manual para un escaneo (diálogo modal).

    Descarga el espectros.csv (servido como estático), reconstruye la imagen y
    permite marcar píxeles/zonas con un nivel. NO clasifica: solo persiste la
    segmentación manual en el servidor (etiquetas.csv del escaneo).
    """
    import os as _os
    import tempfile

    # 1) Taxonomía de niveles (global, compartida entre escaneos).
    try:
        levels = rpi.get("/labels/levels").json()["levels"]
    except Exception:
        levels = default_levels()

    # 2) Imagen del escaneo: bajar CSV, parsear y armar la grilla de píxeles.
    try:
        csv_text = rpi.get(f"/scans/{subdir}/espectros.csv", timeout=30).text
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        tmp.write(csv_text)
        tmp.close()
        scan = load_dataset(tmp.name)
        _os.unlink(tmp.name)
        grid = build_pixel_grid(scan)
    except Exception as e:
        ui.notify(f"No se pudo cargar la imagen del escaneo: {e}", color="negative")
        return

    # 3) Etiquetas existentes.
    try:
        labels = labels_from_json(rpi.get(f"/scan/{subdir}/labels").json()["labels"])
    except Exception:
        labels = {}

    key_to_idx = {k: i for i, k in enumerate(grid.keys)}
    st = {"level": levels[0]["id"] if levels else 0, "labels": dict(labels)}
    lvl_buttons = {}  # id -> botón, para resaltar el activo

    def refresh():
        colors = apply_label_colors(grid, st["labels"], levels)
        fig = build_label_figure(
            grid, colors, title=f"{subdir}  ·  {len(st['labels'])} etiquetados"
        )
        _update_plot(plot, fig)
        s = label_summary(st["labels"], levels, len(grid))
        by = "  ".join(
            f'{lv["name"]}: {s["by_level"].get(lv["id"], 0)}' for lv in levels
        )
        lbl_sum.set_text(f'{s["labeled"]}/{s["total"]} px  ·  {by}')

    def pick_level(lid):
        st["level"] = lid
        for i, b in lvl_buttons.items():
            b.props(remove="unelevated") if i != lid else b.props("unelevated")

    def assign(keys):
        for k in keys:
            if k not in key_to_idx:
                continue
            if st["level"] is None:  # goma de borrar
                st["labels"].pop(k, None)
            else:
                st["labels"][k] = st["level"]
        refresh()

    def show_spectrum(key):
        idx = key_to_idx.get(key)
        if idx is None:
            return
        fig = make_fig(
            list(scan.profiles[idx]), list(scan.wavelengths), f"Píxel ({key[0]},{key[1]})"
        )
        _update_plot(spec_plot, fig)
        lbl_pix.set_text(f"Píxel seleccionado: col {key[0]}, row {key[1]}")

    def on_click(e):
        pts = _event_points(e)
        keys = []
        for p in pts:
            cd = p.get("customdata")
            if cd and len(cd) >= 2:
                keys.append((int(cd[0]), int(cd[1])))
            elif "x" in p and "y" in p:
                keys.append((int(round(p["x"])), int(round(p["y"]))))
        if keys:
            show_spectrum(keys[0])
            assign(keys[:1])

    def on_select(e):
        pts = _event_points(e)
        keys = []
        for p in pts:
            cd = p.get("customdata")
            if cd and len(cd) >= 2:
                keys.append((int(cd[0]), int(cd[1])))
            elif "x" in p and "y" in p:
                keys.append((int(round(p["x"])), int(round(p["y"]))))
        if keys:
            assign(keys)

    def save():
        try:
            rpi.put(
                f"/scan/{subdir}/labels", json={"labels": labels_to_json(st["labels"])}
            ).raise_for_status()
            ui.notify(f"{len(st['labels'])} etiquetas guardadas", color="positive")
        except Exception as e:
            ui.notify(f"Error al guardar: {e}", color="negative")

    def clear_all():
        st["labels"].clear()
        refresh()

    def build_palette():
        """(Re)dibuja los botones de nivel desde ``levels`` (editable en la UI)."""
        palette_box.clear()
        lvl_buttons.clear()
        with palette_box:
            for lv in levels:
                b = ui.button(
                    lv["name"], on_click=lambda i=lv["id"]: pick_level(i)
                ).props("dense").style(
                    f'background:{lv["color"]};color:#08101f;font-weight:700'
                ).classes("w-full")
                lvl_buttons[lv["id"]] = b
            b_erase = ui.button(
                "Borrar (sin nivel)", on_click=lambda: pick_level(None)
            ).props("outline dense color=grey").classes("w-full")
            lvl_buttons[None] = b_erase
        if st["level"] not in lvl_buttons:
            st["level"] = levels[0]["id"] if levels else None
        pick_level(st["level"])

    def edit_levels():
        """Editor de la taxonomía de niveles (nombre + color); PUT a /labels/levels."""
        rows = [dict(lv) for lv in levels]

        with ui.dialog() as ed, ui.card().style("width:470px;max-width:94vw;background:var(--bg2)"):
            _html('<div class="sec">Editar niveles</div>')
            box = ui.column().classes("gap-2 w-full")

            def redraw():
                box.clear()
                with box:
                    for i, lv in enumerate(rows):
                        with ui.row().classes("items-center gap-2 w-full no-wrap"):
                            ui.input(
                                value=lv["name"],
                                on_change=lambda e, r=lv: r.__setitem__("name", e.value),
                            ).props("dense").classes("flex-1")
                            ui.color_input(
                                value=lv["color"],
                                on_change=lambda e, r=lv: r.__setitem__("color", e.value),
                            ).props("dense").style("width:130px")
                            ui.button(
                                icon="delete",
                                on_click=lambda idx=i: (rows.pop(idx), redraw()),
                            ).props("flat round dense color=red")

            def add_level():
                nid = (max([r["id"] for r in rows]) + 1) if rows else 0
                rows.append({"id": nid, "name": f"Nivel {nid}", "color": "#8b949e"})
                redraw()

            def save_levels():
                try:
                    r = rpi.put("/labels/levels", json={"levels": rows})
                    r.raise_for_status()
                    levels[:] = r.json()["levels"]  # muta la lista in place
                    build_palette()
                    refresh()
                    ui.notify("Niveles guardados", color="positive")
                    ed.close()
                except Exception as e:
                    ui.notify(f"Error al guardar niveles: {e}", color="negative")

            redraw()
            with ui.row().classes("gap-2 w-full"):
                ui.button("Añadir nivel", on_click=add_level).props("outline color=cyan dense").classes("flex-1")
                ui.button("Guardar", on_click=save_levels).props("color=cyan dense").classes("flex-1")
        ed.open()

    with ui.dialog() as dlg, ui.card().style("width:1150px;max-width:96vw;background:var(--bg2)"):
        with ui.row().classes("w-full items-center justify-between"):
            _html('<div class="sec" style="margin-bottom:0">Etiquetado manual</div>')
            ui.button(icon="close", on_click=dlg.close).props("flat round color=grey")

        with ui.row().classes("w-full gap-4 items-start no-wrap"):
            # ── Imagen interactiva ──
            with ui.column().classes("gap-2").style("flex:2;min-width:420px"):
                init_fig = build_label_figure(
                    grid, apply_label_colors(grid, st["labels"], levels), title=subdir
                )
                plot = ui.plotly(init_fig).classes("w-full").style("min-height:440px")
                plot.on("plotly_click", on_click)
                plot.on("plotly_selected", on_select)
                ui.label(
                    "Clic = 1 píxel · arrastra una caja = zona. Elige el nivel y pinta."
                ).classes("text-xs").style("color:var(--muted2)")
                lbl_sum = ui.label("").classes("text-xs").style("color:var(--ac2)")

            # ── Controles ──
            with ui.column().classes("gap-3").style("flex:1;min-width:260px"):
                with ui.row().classes("w-full items-center justify-between"):
                    _html('<div class="sec" style="margin-bottom:0">Nivel activo</div>')
                    ui.button("Editar niveles", on_click=lambda: edit_levels()) \
                        .props("flat dense color=cyan").classes("text-xs")
                palette_box = ui.column().classes("gap-2 w-full")
                build_palette()

                ui.separator()
                lbl_pix = ui.label("Ningún píxel seleccionado").classes("text-xs").style("color:var(--muted2)")
                spec_plot = ui.plotly(
                    make_fig([0] * 288, list(range(340, 340 + 288)), "Espectro")
                ).classes("w-full").style("min-height:220px")

                ui.separator()
                with ui.row().classes("gap-2 w-full"):
                    ui.button("Guardar", on_click=save).props("color=cyan").classes("flex-1")
                    ui.button("Limpiar", on_click=clear_all).props("outline color=red").classes("flex-1")

    pick_level(st["level"])
    refresh()
    dlg.open()


# ── Lightbox para las imágenes de resultados (JS autocontenido) ─────────────────
_LIGHTBOX_HTML = """
<div id="lb-overlay" onclick="lbClose(event)" style="
    display:none;position:fixed;inset:0;z-index:9999;background:rgba(2,5,18,0.94);
    flex-direction:column;align-items:center;justify-content:center;gap:12px">
  <div style="position:absolute;top:16px;right:20px;font-size:11px;color:#7b86a3;letter-spacing:2px;cursor:pointer"
       onclick="document.getElementById('lb-overlay').style.display='none'">ESC / CLICK FUERA PARA CERRAR</div>
  <div id="lb-caption" style="font-size:11px;color:#7b86a3;letter-spacing:1px;position:absolute;bottom:20px"></div>
  <div id="lb-container" onclick="event.stopPropagation()" style="
       overflow:auto;max-width:95vw;max-height:82vh;cursor:grab;border-radius:10px"
       onmousedown="lbDragStart(event)" onmousemove="lbDragMove(event)"
       onmouseup="lbDragEnd()" onmouseleave="lbDragEnd()">
    <img id="lb-img" draggable="false" style="display:block;max-width:none;transform-origin:top left" src="">
  </div>
  <div style="display:flex;gap:12px;position:absolute;bottom:16px">
    <button onclick="lbZoom(1.25);event.stopPropagation()" style="background:#0f1535;border:1px solid #252b45;color:#21d4fd;font-size:12px;padding:6px 18px;border-radius:8px;cursor:pointer">+ ZOOM</button>
    <button onclick="lbFit();event.stopPropagation()" style="background:#0f1535;border:1px solid #252b45;color:#7b86a3;font-size:12px;padding:6px 18px;border-radius:8px;cursor:pointer">FIT</button>
    <button onclick="lbZoom(0.8);event.stopPropagation()" style="background:#0f1535;border:1px solid #252b45;color:#21d4fd;font-size:12px;padding:6px 18px;border-radius:8px;cursor:pointer">- ZOOM</button>
  </div>
</div>"""

_LIGHTBOX_JS = """<script>
var _s=1,_d=false,_x=0,_y=0,_sx=0,_sy=0;
function lbOpen(url,cap){_s=1;var img=document.getElementById('lb-img');var ov=document.getElementById('lb-overlay');img.onload=function(){lbFit();};img.src=url;document.getElementById('lb-caption').textContent=cap;ov.style.display='flex';document.body.style.overflow='hidden';}
function lbClose(e){if(e&&e.target!==document.getElementById('lb-overlay'))return;document.getElementById('lb-overlay').style.display='none';document.body.style.overflow='';}
function lbFit(){var img=document.getElementById('lb-img');var c=document.getElementById('lb-container');var sw=c.clientWidth/img.naturalWidth;var sh=c.clientHeight/img.naturalHeight;_s=Math.min(sw,sh,1);img.style.transform='scale('+_s+')';img.style.width=img.naturalWidth+'px';img.style.height=img.naturalHeight+'px';c.scrollLeft=0;c.scrollTop=0;}
function lbZoom(f){_s=Math.min(Math.max(_s*f,0.1),10);document.getElementById('lb-img').style.transform='scale('+_s+')';}
function lbDragStart(e){_d=true;_x=e.clientX;_y=e.clientY;var c=document.getElementById('lb-container');_sx=c.scrollLeft;_sy=c.scrollTop;c.style.cursor='grabbing';}
function lbDragMove(e){if(!_d)return;var c=document.getElementById('lb-container');c.scrollLeft=_sx-(e.clientX-_x);c.scrollTop=_sy-(e.clientY-_y);}
function lbDragEnd(){_d=false;var c=document.getElementById('lb-container');if(c)c.style.cursor='grab';}
document.addEventListener('wheel',function(e){var ov=document.getElementById('lb-overlay');if(!ov||ov.style.display==='none')return;e.preventDefault();lbZoom(e.deltaY<0?1.1:0.9);},{passive:false});
document.addEventListener('keydown',function(e){if(e.key==='Escape'){var ov=document.getElementById('lb-overlay');if(ov){ov.style.display='none';document.body.style.overflow='';}}});
</script>"""


def _html(content: str):
    """``ui.html`` con HTML crudo (sanitize desactivado, requerido por NiceGUI 3)."""
    return ui.html(content, sanitize=False)


_env_thread = {"started": False}


def create_app():
    """Registra la página principal del panel."""
    # Un único hilo por proceso: la página puede abrirse en varias pestañas,
    # pero el estado del ambiente es del módulo y basta con un poll.
    if not _env_thread["started"]:
        _env_thread["started"] = True
        threading.Thread(target=env_worker, daemon=True).start()

    @ui.page("/")
    def index():
        ui.dark_mode(True)
        ui.add_head_html(VISION_CSS)

        # ---- Header: marca + estado del hardware ----
        with ui.header().classes("items-center justify-between q-px-lg q-py-sm"):
            with ui.column().classes("gap-0"):
                ui.label("HSI SCAN").classes("text-subtitle1 text-bold").style("letter-spacing:.14em")
                ui.label("C12880MA · CNC GRBL").classes("text-caption").style("color:var(--muted2)")
            with ui.row().classes("items-center gap-4"):
                h_rpi = _html('<span class="sdot doff"></span><span class="text-xs">RPI</span>')
                h_sensor = _html('<span class="sdot doff"></span><span class="text-xs">Sensor</span>')
                h_cnc = _html('<span class="sdot doff"></span><span class="text-xs">CNC</span>')
                h_env = _html(env_chip_html())

        # ---- Drawer lateral: navegación ----
        with ui.left_drawer(value=True, fixed=True, bordered=True).classes("q-pa-md").style("width:230px"):
            with ui.row().classes("items-center gap-3 q-mb-md q-px-xs"):
                ui.icon("blur_on", size="26px").style("color:var(--ac2)")
                ui.label("Escáner HSI").classes("text-subtitle1 text-bold")
            with ui.tabs().props("vertical").classes("w-full") as tabs:
                tc = ui.tab("Configuración", icon="settings")
                tcal = ui.tab("Calibración", icon="wb_sunny")
                tl = ui.tab("En vivo", icon="show_chart")
                ts = ui.tab("Escaneo", icon="grid_on")
                tr = ui.tab("Resultados", icon="image")

        # ---- Contenido ----
        with ui.tab_panels(tabs, value=tc).classes("w-full q-pa-md"):
            _t = {}  # timers, cancelados al desconectar

            def env_tick():
                # Solo pinta: los datos los trae env_worker en su propio hilo
                # (una petición HTTP dentro de un timer bloquearía la UI).
                try:
                    h_env.set_content(env_chip_html())
                except RuntimeError:
                    pass

            _t["env"] = ui.timer(2.0, env_tick)

            # ═══ CONFIGURACIÓN ═══
            with ui.tab_panel(tc):
                with ui.row().classes("w-full gap-4 items-start"):

                    # Conexiones
                    with ui.column().classes("card gap-3").style("min-width:250px;flex:1"):
                        _html('<div class="sec">Conexiones</div>')

                        ui.label("Raspberry Pi").classes("text-sm text-bold")
                        inp_rpi = ui.input("URL del servidor", value=state["rpi_url"]).classes("w-full")
                        lbl_rpi = ui.label("No conectado").classes("text-xs").style("color:var(--muted2)")

                        ui.separator()

                        ui.label("Sensor C12880MA").classes("text-sm text-bold")
                        lbl_s = ui.label("No conectado").classes("text-xs").style("color:var(--muted2)")
                        drv_select = ui.toggle(
                            {"improved": "Improved", "legacy": "Legacy"}, value="improved",
                        ).props("dense").classes("text-xs")

                        ui.separator()

                        ui.label("CNC GRBL").classes("text-sm text-bold")
                        inp_port = ui.input("Puerto", value=state["cnc_port"]).classes("w-full")
                        lbl_c = ui.label("No conectado").classes("text-xs").style("color:var(--muted2)")

                        def connect_rpi():
                            try:
                                # normalize_url evita guardar una base sin esquema o
                                # vacía: rompería las <img> de Resultados, que el
                                # navegador pediría al puerto de la UI (404).
                                rpi.base = normalize_url(inp_rpi.value)
                                inp_rpi.value = rpi.base
                                state["rpi_url"] = rpi.base
                                cfg["rpi_url"] = rpi.base
                                save_config(cfg)
                                data = rpi.get("/health").json()
                                state["rpi_reachable"] = True
                                lbl_rpi.set_text("Conectado")
                                lbl_rpi.style("color:var(--ok)")
                                h_rpi.set_content('<span class="sdot dok"></span><span class="text-xs">RPI</span>')
                                state["sensor_connected"] = data["sensor_connected"]
                                state["sensor_name"] = data["sensor_name"]
                                state["cnc_connected"] = data["cnc_connected"]
                                if data["sensor_connected"]:
                                    lbl_s.set_text(f"Conectado · {data['sensor_name']}")
                                    lbl_s.style("color:var(--ok)")
                                    h_sensor.set_content('<span class="sdot dok"></span><span class="text-xs">Sensor</span>')
                                if data["cnc_connected"]:
                                    lbl_c.set_text("Conectado")
                                    lbl_c.style("color:var(--ok)")
                                    h_cnc.set_content('<span class="sdot dok"></span><span class="text-xs">CNC</span>')
                                try:
                                    rs = rpi.get("/scan/status")
                                    if rs.ok:
                                        st = rs.json()
                                        already = state["scan_running"]
                                        state.update({
                                            "scan_running": st["running"], "scan_progress": st["progress"],
                                            "scan_total": st["total"], "scan_eta": st["eta"],
                                            "scan_errors": st["errors"], "scan_log": st["log"],
                                        })
                                        if st["running"] and not already:
                                            threading.Thread(target=scan_poll_worker, daemon=True).start()
                                except Exception:
                                    pass
                                ui.notify("Raspberry Pi conectada", color="positive")
                            except Exception as e:
                                state["rpi_reachable"] = False
                                lbl_rpi.set_text(f"Error: {e}")
                                lbl_rpi.style("color:var(--er)")
                                ui.notify(f"No se pudo conectar: {e}", color="negative")

                        def connect_sensor():
                            if not state["rpi_reachable"]:
                                ui.notify("Conecta la RPI primero", color="warning"); return
                            drv = drv_select.value
                            if not drv:
                                ui.notify("Selecciona un driver", color="warning"); return
                            try:
                                lbl_s.set_text(f"Conectando ({drv})...")
                                data = rpi.post("/sensor/connect", json={"driver": str(drv)}).json()
                                state["sensor_connected"] = True
                                state["sensor_name"] = data["name"]
                                lbl_s.set_text(f"Conectado · {data['name']}")
                                lbl_s.style("color:var(--ok)")
                                h_sensor.set_content('<span class="sdot dok"></span><span class="text-xs">Sensor</span>')
                                ui.notify(f"Sensor conectado ({data['name']})", color="positive")
                            except Exception as e:
                                lbl_s.set_text(f"Error: {e}")
                                lbl_s.style("color:var(--er)")
                                ui.notify(f"Error: {e}", color="negative")

                        def connect_cnc():
                            if not state["rpi_reachable"]:
                                ui.notify("Conecta la RPI primero", color="warning"); return
                            lbl_c.set_text("Conectando...")
                            lbl_c.style("color:var(--wn)")
                            try:
                                port = inp_port.value
                                rpi.post("/cnc/connect", json={"port": port}).raise_for_status()
                                state["cnc_connected"] = True
                                state["cnc_port"] = port
                                cfg["cnc_port"] = port
                                save_config(cfg)
                                lbl_c.set_text("Conectado")
                                lbl_c.style("color:var(--ok)")
                                h_cnc.set_content('<span class="sdot dok"></span><span class="text-xs">CNC</span>')
                                ui.notify("CNC conectada", color="positive")
                            except Exception as e:
                                lbl_c.set_text(f"Error: {e}")
                                lbl_c.style("color:var(--er)")
                                ui.notify(f"Error: {e}", color="negative")

                        ui.button("Conectar RPI", on_click=connect_rpi).props("color=cyan").classes("w-full")
                        ui.button("Conectar sensor", on_click=connect_sensor).props("outline color=cyan").classes("w-full")
                        ui.button("Conectar CNC", on_click=connect_cnc).props("outline color=cyan").classes("w-full")

                    # Control CNC
                    with ui.column().classes("card gap-3").style("min-width:240px;flex:1"):
                        _html('<div class="sec">Control CNC</div>')
                        lbl_pos = ui.label("X: —   Y: —   Z: —").classes("text-sm text-bold").style("color:var(--ac2)")

                        def do_home():
                            if not state["cnc_connected"]:
                                ui.notify("CNC no conectada", color="warning"); return
                            threading.Thread(
                                target=lambda: rpi.post("/cnc/send", json={"cmd": "$H", "wait_ok": True}),
                                daemon=True,
                            ).start()
                            ui.notify("Homing iniciado", color="positive")

                        ui.button("Homing ($H)", on_click=do_home).props("color=purple").classes("w-full")
                        ui.separator()
                        with ui.row().classes("gap-2 w-full"):
                            nmx = ui.number("X mm", value=0, format="%.2f").classes("flex-1")
                            nmy = ui.number("Y mm", value=0, format="%.2f").classes("flex-1")
                            nmz = ui.number("Z mm", value=0, format="%.2f").classes("flex-1")

                        def do_move():
                            if not state["cnc_connected"]:
                                ui.notify("CNC no conectada", color="warning"); return
                            threading.Thread(
                                target=lambda: rpi.post("/cnc/move", json={"x": nmx.value, "y": nmy.value, "z": nmz.value}),
                                daemon=True,
                            ).start()

                        def ref_pos():
                            try:
                                r = rpi.get("/cnc/position")
                                if r.ok:
                                    p = r.json()
                                    state["cnc_position"] = p
                                    lbl_pos.set_text(f"X: {p['x']:.2f}   Y: {p['y']:.2f}   Z: {p['z']:.2f}")
                            except Exception:
                                pass

                        ui.button("Mover", on_click=do_move).props("outline color=cyan").classes("w-full")
                        ui.button("Actualizar posición", on_click=ref_pos).props("flat color=grey").classes("w-full text-xs")

                    # Parámetros de grilla
                    with ui.column().classes("card gap-3").style("min-width:240px;flex:1"):
                        _html('<div class="sec">Parámetros de grilla</div>')
                        with ui.row().classes("gap-2"):
                            nxs = ui.number("X inicio", value=state["x_start"], format="%.1f").classes("flex-1")
                            nys = ui.number("Y inicio", value=state["y_start"], format="%.1f").classes("flex-1")
                        nz = ui.number("Z foco (mm)", value=state["z_focus"], format="%.1f").classes("w-full")
                        with ui.row().classes("gap-2"):
                            ncols = ui.number("Columnas", value=state["cols"], format="%d", min=1).classes("flex-1")
                            nrows = ui.number("Filas", value=state["rows"], format="%d", min=1).classes("flex-1")
                        nstp = ui.number("Paso (mm)", value=state["step_mm"], format="%.2f", min=0.1).classes("w-full")
                        nexpo = ui.number("Integración (ms)", value=state["exposition"], format="%.1f", min=1).classes("w-full")
                        nacc = ui.number("Acumulaciones", value=state["accum"], format="%d", min=1).classes("w-full")
                        nlum = ui.number("Lámpara (%)", value=state["lamp_power_pct"], format="%.0f", min=0, max=99).classes("w-full")
                        ui.label("Potencia de la lámpara, anotada a mano (rango 10–99 %). Se guarda con cada captura; 0 = sin especificar.") \
                            .classes("text-xs").style("color:var(--muted2)")
                        lbl_prev = ui.label("").classes("text-xs").style("color:var(--muted2)")

                        def save_params(persist=True):
                            state["x_start"] = float(nxs.value or 0)
                            state["y_start"] = float(nys.value or 0)
                            state["z_focus"] = float(nz.value or 0)
                            state["cols"] = max(1, int(ncols.value or 1))
                            state["rows"] = max(1, int(nrows.value or 1))
                            state["step_mm"] = float(nstp.value or 1)
                            state["exposition"] = float(nexpo.value or 50)
                            state["accum"] = max(1, int(nacc.value or 1))
                            # 0 = sin especificar; cualquier otro valor se ajusta al
                            # rango real de la lámpara (10–99 %).
                            _lum_in = float(nlum.value or 0)
                            state["lamp_power_pct"] = 0.0 if _lum_in <= 0 else min(99.0, max(10.0, _lum_in))
                            nlum.value = state["lamp_power_pct"]
                            cfg["rpi_url"] = normalize_url(inp_rpi.value)
                            cfg["cnc_port"] = inp_port.value or DEFAULT_CONFIG["cnc_port"]
                            for k in ["x_start", "y_start", "z_focus", "cols", "rows", "step_mm", "exposition", "accum", "lamp_power_pct"]:
                                cfg[k] = state[k]
                            if persist:
                                rpi.base = cfg["rpi_url"]
                                state["rpi_url"] = cfg["rpi_url"]
                                save_config(cfg)
                                ui.notify("Configuración guardada", color="positive")
                            c, r, s = state["cols"], state["rows"], state["step_mm"]
                            e, a = state["exposition"] / 1000, state["accum"]
                            est = c * r * (e * a + SETTLE + 0.5) / 60
                            lbl_prev.set_text(f"{c*r} puntos · {(c-1)*s:.1f}×{(r-1)*s:.1f} mm · ~{est:.1f} min")

                        def set_step(paso: float):
                            """Cambia el paso conservando el área barrida.

                            Sirve para hacer una pasada gruesa de encuadre (paso 5 o
                            10 mm, minutos) sobre la misma zona que luego se escanea
                            fino a 1 mm (horas). El worker recorre
                            ``x = x_start + col*paso``, así que el área cubierta es
                            ``(cols-1)*paso`` y las columnas se derivan de ahí.
                            """
                            s0 = float(nstp.value or 1)
                            ancho = (int(ncols.value or 1) - 1) * s0
                            alto = (int(nrows.value or 1) - 1) * s0
                            nstp.value = paso
                            ncols.value = max(1, round(ancho / paso) + 1)
                            nrows.value = max(1, round(alto / paso) + 1)
                            save_params(persist=False)

                        ui.label("Prueba de encuadre: mismo área, menos puntos.") \
                            .classes("text-xs").style("color:var(--muted2)")
                        with ui.row().classes("gap-1 w-full"):
                            for _p in (10.0, 5.0, 2.0, 1.0):
                                ui.button(
                                    f"{_p:.0f} mm", on_click=lambda p=_p: set_step(p)
                                ).props("outline color=grey dense").classes("flex-1 text-xs")

                        ui.button("Guardar configuración", on_click=save_params).props("color=cyan").classes("w-full")
                        save_params(persist=False)

            # ═══ CALIBRACIÓN ═══
            with ui.tab_panel(tcal):
                with ui.row().classes("w-full gap-4 items-start"):
                    # ---- Captura de referencias ----
                    with ui.column().classes("card gap-3").style("min-width:300px;flex:1"):
                        _html('<div class="sec">Referencias blanco / negro</div>')
                        ui.label(
                            "Blanco (W): panel de referencia bien iluminado. "
                            "Negro (D): lente tapado o sin luz. El análisis las usa para "
                            "reflectancia real (Ec. 1); sin ellas, usa el modo adaptado."
                        ).classes("text-xs").style("color:var(--muted2)")

                        with ui.row().classes("gap-2 w-full"):
                            cal_expo = ui.number("Integración (ms)", value=state["exposition"], format="%.1f", min=1).classes("flex-1")
                            cal_acc = ui.number("Acumulaciones", value=state["accum"], format="%d", min=1).classes("flex-1")
                            cal_samp = ui.number("Tomas a promediar", value=10, format="%d", min=1).classes("flex-1")
                        ui.label("Captura con la misma integración que el escaneo para que la corrección sea válida.") \
                            .classes("text-xs").style("color:var(--wn)")

                        def do_capture(kind):
                            if not state["sensor_connected"]:
                                ui.notify("Conecta el sensor primero", color="warning"); return
                            if state["scan_running"]:
                                ui.notify("Escaneo en curso; el sensor está ocupado", color="warning"); return
                            if state["calib_busy"]:
                                ui.notify("Espera a que termine la captura en curso", color="warning"); return
                            state["calib_busy"] = kind
                            state["calib_error"] = None
                            payload = {
                                "kind": kind,
                                "exposition": float(cal_expo.value or 50),
                                "accum": int(cal_acc.value or 1),
                                "samples": int(cal_samp.value or 1),
                            }

                            def worker():
                                r = None
                                try:
                                    r = rpi.post("/calibration/capture", json=payload, timeout=180)
                                    r.raise_for_status()
                                    state["calib"][kind] = r.json()
                                except Exception as e:
                                    detail = e
                                    try:
                                        detail = r.json().get("detail", e)
                                    except Exception:
                                        pass
                                    state["calib_error"] = str(detail)
                                finally:
                                    state["calib_busy"] = None

                            threading.Thread(target=worker, daemon=True).start()
                            ui.notify(f"Capturando {'blanco' if kind == 'white' else 'negro'}…", color="info")

                        with ui.row().classes("gap-2 w-full"):
                            btn_w = ui.button("Capturar BLANCO", on_click=lambda: do_capture("white")).props("color=cyan").classes("flex-1")
                            btn_d = ui.button("Capturar NEGRO", on_click=lambda: do_capture("dark")).props("outline color=grey").classes("flex-1")

                        lbl_w = ui.label("Blanco (W): sin capturar").classes("text-xs").style("color:var(--muted2)")
                        lbl_d = ui.label("Negro (D): sin capturar").classes("text-xs").style("color:var(--muted2)")
                        lbl_cal_mode = _html("")

                        def clear_ref(kind):
                            try:
                                rpi.delete(f"/calibration/{kind}").raise_for_status()
                                state["calib"][kind] = None
                                ui.notify("Referencia borrada", color="positive")
                            except Exception as e:
                                ui.notify(f"Error: {e}", color="negative")

                        with ui.row().classes("gap-2"):
                            ui.button("Borrar blanco", on_click=lambda: clear_ref("white")).props("flat dense color=grey").classes("text-xs")
                            ui.button("Borrar negro", on_click=lambda: clear_ref("dark")).props("flat dense color=grey").classes("text-xs")

                    # ---- Vista de las referencias ----
                    with ui.column().classes("card gap-2").style("min-width:340px;flex:1"):
                        _html('<div class="sec">Espectros de referencia</div>')
                        cal_chart = ui.plotly(_calib_fig(None, None)).classes("w-full").style("min-height:320px")

                # Carga inicial del estado de calibración (en hilo, no bloquea).
                def load_calib_status():
                    try:
                        state["calib"] = rpi.get("/calibration").json()
                        state["calib_dirty"] = True
                    except Exception:
                        pass

                threading.Thread(target=load_calib_status, daemon=True).start()

                _cal_sig = {"v": None, "err": None}

                def _set_loading(btn, on):
                    btn.props("loading") if on else btn.props(remove="loading")

                def calib_tick():
                    try:
                        busy = state["calib_busy"]
                        _set_loading(btn_w, busy == "white")
                        _set_loading(btn_d, busy == "dark")

                        w = state["calib"].get("white")
                        d = state["calib"].get("dark")

                        if w:
                            lbl_w.set_text(
                                f'Blanco (W): {w.get("ts","?")} · máx {w.get("max",0):.0f} ADC · '
                                f'{w.get("samples","?")} tomas @ {w.get("exposition","?")} ms'
                                + (f' · ⚠ {w["saturated"]} px sat.' if w.get("saturated") else "")
                            )
                            lbl_w.style("color:var(--ok)")
                        else:
                            lbl_w.set_text("Blanco (W): sin capturar"); lbl_w.style("color:var(--muted2)")
                        if d:
                            lbl_d.set_text(
                                f'Negro (D): {d.get("ts","?")} · máx {d.get("max",0):.0f} ADC · '
                                f'{d.get("samples","?")} tomas @ {d.get("exposition","?")} ms'
                            )
                            lbl_d.style("color:var(--ok)")
                        else:
                            lbl_d.set_text("Negro (D): sin capturar"); lbl_d.style("color:var(--muted2)")

                        both = bool(w) and bool(d)
                        lbl_cal_mode.set_content(
                            '<span class="pill pill-ok">El análisis usará reflectancia real (Ec. 1)</span>'
                            if both else
                            '<span class="pill pill-er">Faltan referencias → el análisis usa modo adaptado</span>'
                        )

                        # Actualizar la gráfica solo si cambió (evita parpadeo).
                        sig = (
                            (w.get("ts") if w else None, len(w["spectrum"]) if w and w.get("spectrum") else 0),
                            (d.get("ts") if d else None, len(d["spectrum"]) if d and d.get("spectrum") else 0),
                        )
                        if sig != _cal_sig["v"]:
                            _cal_sig["v"] = sig
                            _update_plot(cal_chart, _calib_fig(w, d))

                        if state["calib_error"] and state["calib_error"] != _cal_sig["err"]:
                            _cal_sig["err"] = state["calib_error"]
                            ui.notify(f"Error de captura: {state['calib_error']}", color="negative")
                    except RuntimeError:
                        pass

                _t["calib"] = ui.timer(1.0, calib_tick)

            # ═══ EN VIVO ═══
            with ui.tab_panel(tl):
                with ui.column().classes("card gap-2 w-full"):
                    _html('<div class="sec">Espectro en vivo</div>')
                    ui.label("Captura continua para verificar señal y saturación antes del escaneo.") \
                        .classes("text-xs").style("color:var(--muted2)")
                    empty = make_fig([0] * 288, list(range(340, 340 + 288)), "En vivo")
                    live_chart = ui.plotly(empty).classes("w-full").style("min-height:340px")
                    lbl_live = ui.label("Pulsa Iniciar para comenzar").classes("text-xs").style("color:var(--muted2)")
                    with ui.row().classes("gap-3"):
                        btn_live_start = ui.button("Iniciar").props("color=cyan")
                        btn_live_stop = ui.button("Detener").props("outline color=grey")

                    def live_tick():
                        try:
                            fig = state.get("live_fig")
                            if fig:
                                # NiceGUI 2.x usa update_figure(); versiones
                                # previas, asignar .figure + update(). Soporta ambas.
                                if hasattr(live_chart, "update_figure"):
                                    live_chart.update_figure(fig)
                                else:
                                    live_chart.figure = fig
                                    live_chart.update()
                                state["live_fig"] = None
                            if state.get("live_info"):
                                lbl_live.set_text(state["live_info"])
                        except RuntimeError:
                            pass

                    def start_live():
                        if not state["sensor_connected"]:
                            ui.notify("Conecta el sensor primero", color="warning"); return
                        if state["scan_running"]:
                            ui.notify("Hay un escaneo en curso; el sensor está ocupado", color="warning"); return
                        if state["live_running"]:
                            return
                        state["live_running"] = True
                        threading.Thread(target=live_worker, daemon=True).start()

                    _t["live"] = ui.timer(0.5, live_tick)
                    btn_live_start.on("click", lambda: start_live())
                    btn_live_stop.on("click", lambda: state.update(live_running=False))

            # ═══ ESCANEO ═══
            with ui.tab_panel(ts):
                with ui.row().classes("w-full gap-4 items-start"):
                    with ui.column().classes("card gap-3").style("min-width:280px;flex:1"):
                        _html('<div class="sec">Control de escaneo</div>')
                        h_check = _html("")

                        def build_check():
                            try:
                                items = [
                                    (state["sensor_connected"], "Sensor conectado"),
                                    (state["cnc_connected"], "CNC conectada"),
                                    (state["cols"] > 0, "Grilla configurada"),
                                ]
                                ok_all, html = True, ""
                                for ok, label in items:
                                    if not ok:
                                        ok_all = False
                                    color = "var(--ok)" if ok else "var(--er)"
                                    mark = "✓" if ok else "✕"
                                    html += (f'<div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">'
                                             f'<span style="color:{color}">{mark}</span>'
                                             f'<span style="font-size:13px;color:{"var(--ink)" if ok else "var(--muted2)"}">{label}</span></div>')
                                h_check.set_content(f'<div style="margin-bottom:12px">{html}</div>')
                                return ok_all
                            except RuntimeError:
                                return False

                        build_check()
                        _t["check"] = ui.timer(2.0, build_check)

                        lbl_prog = ui.label("0 / 0 puntos").classes("text-sm text-bold").style("color:var(--ac2)")
                        lbl_eta = ui.label("Tiempo restante: --").classes("text-xs").style("color:var(--muted2)")
                        lbl_err = ui.label("").classes("text-xs").style("color:var(--er)")
                        scan_bar = ui.linear_progress(value=0).classes("w-full").style("height:8px")

                        # ---- Ambiente durante la captura (ESP8266 + DHT11) ----
                        ui.separator().classes("q-my-xs")
                        lbl_env = ui.label("Ambiente: sin datos del ESP").classes("text-xs").style("color:var(--muted2)")
                        env_chart = ui.plotly(make_env_fig([], [], [])).classes("w-full").style("min-height:180px")

                        def env_plot_tick():
                            """Pinta la serie T/HR y la deriva del escaneo en curso."""
                            try:
                                hist = state.get("env_hist") or []
                                _update_plot(env_chart, make_env_fig(
                                    [h["ts"] for h in hist],
                                    [h["temperature"] for h in hist],
                                    [h["humidity"] for h in hist],
                                ))
                                env = state.get("env") or {}
                                run = env.get("run")
                                resumen = env_summary_text(run)
                                if resumen:
                                    lbl_env.set_text(f"Durante el escaneo: {resumen}")
                                    # La deriva térmica corre el dark current: se
                                    # marca en ámbar para que salte a la vista.
                                    color = "var(--wn)" if abs(run["t_drift"]) >= 2.0 else "var(--muted2)"
                                    lbl_env.style(f"color:{color}")
                                elif env.get("last"):
                                    last = env["last"]
                                    lbl_env.set_text(
                                        f'Ambiente actual: {last["temperature"]:.1f} °C · '
                                        f'{last["humidity"]:.0f} % HR'
                                    )
                                    lbl_env.style("color:var(--muted2)")
                                else:
                                    lbl_env.set_text("Ambiente: sin datos del ESP")
                                    lbl_env.style("color:var(--muted2)")
                            except RuntimeError:
                                pass

                        _t["envplot"] = ui.timer(ENV_POLL_S, env_plot_tick)

                        def scan_tick():
                            try:
                                total, done = state["scan_total"], state["scan_progress"]
                                if total > 0:
                                    scan_bar.set_value(done / total)
                                    lbl_prog.set_text(f"{done} / {total} puntos")
                                lbl_eta.set_text(f"Tiempo restante: {state['scan_eta']}")
                                if state["scan_errors"] > 0:
                                    lbl_err.set_text(f"⚠ {state['scan_errors']} errores")
                                # El servidor puede auto-pausar si el sensor se cae:
                                # reflejar Pausar/Reanudar según el estado real.
                                btn_pause.set_text("Reanudar" if state["scan_paused"] else "Pausar")
                            except RuntimeError:
                                pass

                        _t["scan"] = ui.timer(1.0, scan_tick)

                        def start_scan():
                            if not build_check():
                                ui.notify("Completa todos los requisitos", color="warning"); return
                            if state["scan_running"]:
                                ui.notify("Escaneo ya en curso", color="warning"); return
                            # Detener el "en vivo": el sensor (I2C) no admite dos
                            # lecturas a la vez; si no, el escaneo choca con él.
                            state["live_running"] = False
                            try:
                                payload = {
                                    "cols": state["cols"], "rows": state["rows"],
                                    "x_start": state["x_start"], "y_start": state["y_start"],
                                    "z_focus": state["z_focus"], "step_mm": state["step_mm"],
                                    "exposition_ms": state["exposition"], "accum": state["accum"],
                                    "lamp_power_pct": state["lamp_power_pct"],
                                }
                                rpi.post("/scan/start", json=payload).raise_for_status()
                                state["scan_running"] = True
                                state["scan_paused"] = False
                                threading.Thread(target=scan_poll_worker, daemon=True).start()
                                ui.notify("Escaneo iniciado", color="positive")
                            except Exception as e:
                                ui.notify(f"Error al iniciar: {e}", color="negative")

                        def toggle_pause():
                            try:
                                endpoint = "/scan/resume" if state["scan_paused"] else "/scan/pause"
                                rpi.post(endpoint)
                                state["scan_paused"] = not state["scan_paused"]
                            except Exception:
                                pass

                        def stop_scan():
                            try:
                                rpi.post("/scan/stop")
                            except Exception:
                                pass
                            state["scan_running"] = False

                        with ui.row().classes("gap-2 w-full"):
                            ui.button("Iniciar", on_click=start_scan).props("color=cyan").classes("flex-1")
                            btn_pause = ui.button("Pausar", on_click=toggle_pause).props("outline color=amber").classes("flex-1")
                            ui.button("Detener", on_click=stop_scan).props("outline color=red").classes("flex-1")

                    with ui.column().classes("card gap-2").style("min-width:280px;flex:1"):
                        _html('<div class="sec">Registro</div>')
                        log_el = _html("").classes("logbox w-full")

                        def log_tick():
                            try:
                                entries = state["scan_log"][-60:]
                                cls = {"ok": "ok", "error": "err", "warn": "wrn"}
                                html = "".join(
                                    f'<div class="{cls.get(e["level"], "inf")}">[{e["t"]}] {e["icon"]} {e["msg"]}</div>'
                                    for e in entries
                                )
                                log_el.set_content(html)
                                ui.run_javascript("var lb=document.querySelector('.logbox');if(lb)lb.scrollTop=lb.scrollHeight")
                            except RuntimeError:
                                pass

                        _t["log"] = ui.timer(1.0, log_tick)

            # ═══ RESULTADOS ═══
            with ui.tab_panel(tr):
                _html('<div class="sec">Escaneos realizados</div>')
                lbl_empty = ui.label("Aún no hay escaneos completados.").classes("text-sm").style("color:var(--muted2)")
                res_col = ui.column().classes("w-full gap-4")
                # Firma del último render, LOCAL a este cliente: el DOM es propio
                # de cada pestaña, así que guardarla en el ``state`` del módulo
                # haría que una pestaña nueva se saltara el primer dibujado y se
                # quedara en blanco.
                _res_sig: dict[str, str | None] = {"v": None}
                _html(_LIGHTBOX_HTML)
                ui.add_body_html(_LIGHTBOX_JS)

                def open_lightbox(url, caption):
                    ui.run_javascript(f'lbOpen("{url}", "{caption}")')

                def open_env_dialog(subdir):
                    """Serie completa de temperatura/humedad registrada en ese escaneo."""
                    try:
                        data = rpi.get(f"/scan/{subdir}/env", timeout=15).json()
                    except Exception as e:
                        ui.notify(f"No se pudo leer el ambiente: {e}", color="negative")
                        return
                    if not data.get("ts"):
                        ui.notify("Ese escaneo no tiene lecturas del ESP", color="warning")
                        return
                    # Mismo patrón que el etiquetador y el inspector: el fondo
                    # explícito evita que Quasar pinte la card en blanco.
                    with ui.dialog() as dlg, ui.card().classes("gap-2").style(
                        "width:820px;max-width:96vw;background:var(--bg2)"
                    ):
                        ui.label(f"Ambiente · {subdir}").classes("text-sm text-bold").style("color:var(--ac2)")
                        ui.plotly(
                            make_env_fig(data["ts"], data["temperature"], data["humidity"],
                                         title=f'{data["n"]} lecturas del ESP8266')
                        ).classes("w-full").style("min-height:360px")
                        ui.label(
                            "Cada punto del escaneo tiene además su lectura en ambiente_puntos.csv."
                        ).classes("text-xs").style("color:var(--muted2)")
                        ui.button("Cerrar", on_click=dlg.close).props("outline color=cyan dense").classes("text-xs")
                    dlg.open()

                def start_analysis(subdir):
                    """Dispara el preprocesamiento en la Raspberry (hilo de fondo).

                    El pipeline corre en el servidor sobre el espectros.csv ya
                    guardado; el timer de resultados refresca la vista al terminar.
                    """
                    if subdir in state["analyzing"]:
                        return
                    state["analyzing"].add(subdir)
                    state["analysis_error"].pop(subdir, None)
                    render_results()  # refleja el spinner de inmediato

                    def worker():
                        try:
                            # El análisis en la Pi puede tardar: sube el timeout.
                            r = rpi.post(f"/scan/{subdir}/process", timeout=120)
                            r.raise_for_status()
                            state["pca_ver"][subdir] = state["pca_ver"].get(subdir, 0) + 1
                        except Exception as e:
                            detail = e
                            try:
                                detail = r.json().get("detail", e)  # type: ignore[name-defined]
                            except Exception:
                                pass
                            state["analysis_error"][subdir] = str(detail)
                        finally:
                            state["analyzing"].discard(subdir)

                    threading.Thread(target=worker, daemon=True).start()
                    ui.notify("Analizando escaneo…", color="info")

                def render_results():
                    try:
                        r = rpi.get("/scan/results")
                        if r.ok:
                            state["past_scans"] = r.json()
                    except Exception:
                        pass
                    scans = state["past_scans"]
                    # Reconstruir el DOM en cada tick recreaba las <img>, abortando
                    # la descarga antes de completarse: con pocas imágenes daba
                    # tiempo, pero al crecer la lista se quedaban en blanco para
                    # siempre. Solo se redibuja si cambió algo visible.
                    firma = json.dumps(
                        [scans, sorted(state["analyzing"]),
                         state["analysis_error"], state["pca_ver"],
                         # Estado de la caché local: cambia al descargar un
                         # escaneo, y así el icono de "Inspeccionar" se actualiza.
                         [s["scan_subdir"] for s in scans
                          if viewer_is_cached(s["scan_subdir"])]],
                        sort_keys=True, default=str,
                    )
                    if firma == _res_sig["v"]:
                        return
                    _res_sig["v"] = firma
                    try:
                        res_col.clear()
                        lbl_empty.visible = len(scans) == 0
                    except RuntimeError:
                        return
                    with res_col:
                        for sc in reversed(scans):
                            cfg_snap = sc.get("cfg", {})
                            with ui.row().classes("card w-full gap-6 items-start"):
                                if sc.get("rgb_exists"):
                                    img_url = f"{rpi.base}/scans/{sc['scan_subdir']}/imagen_rgb.png"
                                    caption = f"{sc['ts']}  |  {sc['points']}/{sc['total']} puntos"
                                    with ui.column().classes("gap-1 items-center"):
                                        ui.image(img_url).style(
                                            "width:180px;border-radius:10px;border:1px solid var(--stroke);cursor:pointer"
                                        ).on("click", lambda u=img_url, c=caption: open_lightbox(u, c))
                                        ui.label("click para ampliar").classes("text-xs").style("color:var(--muted2)")
                                with ui.column().classes("gap-2").style("flex:1;min-width:220px"):
                                    ui.label(sc["ts"]).classes("text-sm text-bold").style("color:var(--ac2)")
                                    err = sc["errors"]
                                    _html(
                                        f'<div style="display:flex;gap:8px;flex-wrap:wrap">'
                                        f'<span class="pill pill-ok">{sc["points"]}/{sc["total"]} puntos</span>'
                                        f'<span class="pill {"pill-er" if err > 0 else "pill-ok"}">{err} errores</span></div>'
                                    )
                                    if cfg_snap:
                                        params = [
                                            ("Grilla", f'{cfg_snap.get("cols", "?")} × {cfg_snap.get("rows", "?")}'),
                                            ("Paso", f'{cfg_snap.get("step_mm", "?")} mm'),
                                            ("Z foco", f'{cfg_snap.get("z_focus", "?")} mm'),
                                            ("Integración", f'{cfg_snap.get("exposition_ms", "?")} ms'),
                                            ("Acumulaciones", f'{cfg_snap.get("accum", "?")}'),
                                        ]
                                        # Escaneos anteriores a este campo no lo traen: se
                                        # muestra solo si se anotó (0 cuenta como no anotado).
                                        _lum = cfg_snap.get("lamp_power_pct")
                                        params.append(
                                            ("Lámpara", f"{_lum:.0f} %" if _lum else "sin especificar")
                                        )
                                        # Escaneos anteriores al registro ambiental
                                        # no traen env: se anuncia como sin registro.
                                        params.append(
                                            ("Ambiente", env_summary_text(sc.get("env")) or "sin registro")
                                        )
                                        with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-1"):
                                            for label, val in params:
                                                ui.label(label).classes("text-xs").style("color:var(--muted2)")
                                                ui.label(val).classes("text-xs").style("color:var(--ink)")

                                    # ── Análisis (preprocesamiento) ──
                                    subdir = sc["scan_subdir"]
                                    analysis = sc.get("analysis")
                                    analyzing = subdir in state["analyzing"]
                                    err = state["analysis_error"].get(subdir)

                                    ui.separator().classes("q-my-xs")
                                    with ui.row().classes("items-center gap-2"):
                                        btn_an = ui.button(
                                            "Re-analizar" if analysis else "Analizar",
                                            on_click=lambda s=subdir: start_analysis(s),
                                        ).props("outline color=cyan dense").classes("text-xs")
                                        labeled_n = sc.get("labeled", 0)
                                        ui.button(
                                            f"Etiquetar ({labeled_n})" if labeled_n else "Etiquetar",
                                            on_click=lambda s=subdir: open_labeler(s),
                                        ).props("outline color=purple dense").classes("text-xs")
                                        # Inspector espectral: hermano del etiquetador
                                        # (este solo lee: canal a canal + espectro).
                                        # El icono distingue si el escaneo ya está en
                                        # la caché local o hay que bajarlo primero.
                                        local = viewer_is_cached(subdir)
                                        btn_insp = ui.button(
                                            "Inspeccionar",
                                            icon="travel_explore" if local else "cloud_download",
                                            on_click=lambda s=subdir: open_viewer(rpi, s),
                                        ).props("outline color=cyan dense").classes("text-xs")
                                        if sc.get("env_series"):
                                            ui.button(
                                                "Ambiente", icon="thermostat",
                                                on_click=lambda s=subdir: open_env_dialog(s),
                                            ).props("outline color=amber dense").classes("text-xs")
                                        with btn_insp:
                                            ui.tooltip(
                                                "Copia local lista: abre al instante, sin Raspberry"
                                                if local else
                                                "Descarga el escaneo de la Raspberry la primera vez"
                                            )
                                        if analyzing:
                                            btn_an.props("loading")
                                            ui.label("Procesando en la Raspberry…").classes("text-xs").style("color:var(--muted2)")
                                        elif not sc.get("rgb_exists") and not analysis:
                                            ui.label("Corrección + Savitzky-Golay + PCA (modo adaptado)").classes("text-xs").style("color:var(--muted2)")

                                    if err:
                                        ui.label(f"⚠ {err}").classes("text-xs").style("color:var(--er)")

                                    if analysis:
                                        ev = analysis.get("explained_variance_ratio", [])
                                        cum = analysis.get("cumulative_variance", 0.0)
                                        kmo_v = analysis.get("kmo", 0.0)
                                        pct_removed = 100.0 * (analysis["n_input"] - analysis["n_kept"]) / max(analysis["n_input"], 1)
                                        _html(
                                            '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">'
                                            f'<span class="pill pill-ok">KMO {kmo_v:.3f}</span>'
                                            f'<span class="pill pill-ok">PCA {100*cum:.1f}% var</span>'
                                            f'<span class="pill pill-ok">{analysis["n_kept"]}/{analysis["n_input"]} perfiles</span>'
                                            f'<span class="pill {"pill-er" if pct_removed>50 else "pill-ok"}">{pct_removed:.0f}% anómalos</span>'
                                            + ('<span class="pill pill-er">modo adaptado (sin W/D)</span>' if analysis.get("adapted_mode") else '')
                                            + '</div>'
                                        )
                                        if analysis.get("pca_exists"):
                                            ver = state["pca_ver"].get(subdir, 0)
                                            pca_url = f"{rpi.base}/scans/{subdir}/pca.png?v={ver}"
                                            pca_cap = f"PCA · {subdir}"
                                            ui.image(pca_url).style(
                                                "width:200px;margin-top:6px;border-radius:10px;border:1px solid var(--stroke);cursor:pointer"
                                            ).on("click", lambda u=pca_url, c=pca_cap: open_lightbox(u, c))

                _t["results"] = ui.timer(3.0, render_results)
                render_results()

            def _cleanup(*_):
                for _timer in _t.values():
                    try:
                        _timer.cancel()
                    except Exception:
                        pass

            # Cancelar los timers SOLO del cliente que se va. El antiguo fallback
            # a app.on_disconnect era global: al recargar la página o cerrar otra
            # pestaña cancelaba también los timers del cliente activo, que ya no
            # se recrean (NiceGUI no reejecuta index al reconectar). Resultado:
            # Resultados se quedaba en "Aún no hay escaneos" para siempre. Si el
            # registro per-cliente no está disponible se prefiere no cancelar
            # nada: NiceGUI descarta esos timers al morir el cliente.
            try:
                ui.context.client.on_disconnect(_cleanup)
            except Exception:
                pass

    return index
