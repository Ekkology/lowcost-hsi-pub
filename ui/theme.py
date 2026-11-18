"""Tema visual del panel (estilo Vision UI: navy profundo + glassmorphism).

Expone:
  - ``VISION_CSS``: hoja de estilos que reviste los componentes Quasar/NiceGUI.
  - ``make_fig``: figura Plotly del espectro con la paleta del panel.
  - ``PLOTLY_LAYOUT`` y constantes de color reutilizables.
  - ``make_env_fig``: figura de temperatura/humedad (ESP8266 + DHT11).
  - ``update_plot`` / ``event_points``: helpers de Plotly compartidos por los
    módulos que dibujan (``app.py`` y ``viewer.py``). Viven aquí, y no en
    ``app.py``, para que ``viewer.py`` pueda usarlos sin un import circular.
"""

from __future__ import annotations

# ── Paleta ─────────────────────────────────────────────────────────────────────
ACCENT = "#0075ff"       # azul de marca
ACCENT2 = "#21d4fd"      # cian de acento
GOOD = "#01b574"
WARN = "#ffb547"
CRIT = "#e31a1a"
INK = "#e7ecf7"
MUTED = "#a0aec0"
SURFACE = "#0b1437"

# Bandas RGB usadas para la vista previa rápida (nm).
RGB_BANDS = {"R": (620, 680), "G": (520, 560), "B": (450, 490)}

# Umbral de saturación del C12880MA (ADC de 15 bits · 90%).
SAT_LEVEL = 32767 * 0.9


# ── CSS ─────────────────────────────────────────────────────────────────────────
VISION_CSS = """
<style>
:root{
  --bg:#060b26; --bg2:#0b1437;
  --card-a:rgba(6,11,40,0.72); --card-b:rgba(10,14,35,0.68);
  --stroke:rgba(255,255,255,0.10); --stroke2:rgba(255,255,255,0.06);
  --ink:#e7ecf7; --muted:#a0aec0; --muted2:#7b86a3;
  --ac:#0075ff; --ac2:#21d4fd; --ok:#01b574; --wn:#ffb547; --er:#e31a1a;
  --grid:rgba(255,255,255,0.07);
}
body,.nicegui-content,.q-page,.q-page-container,.q-layout{
  background:transparent!important; color:var(--ink)!important;
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
body{
  background:
    radial-gradient(1200px 600px at 82% -12%, rgba(0,117,255,0.18), transparent 60%),
    radial-gradient(900px 500px at 3% 112%, rgba(33,212,253,0.12), transparent 55%),
    linear-gradient(180deg,#040817,#060b26 42%,#05091d)!important;
  background-attachment:fixed!important;
}
.q-header{
  background:linear-gradient(127deg,var(--card-a),var(--card-b))!important;
  border-bottom:1px solid var(--stroke)!important; backdrop-filter:blur(14px);
}
.q-drawer{
  background:linear-gradient(180deg,rgba(8,12,34,0.85),rgba(6,10,28,0.8))!important;
  border-right:1px solid var(--stroke)!important; backdrop-filter:blur(14px);
}
.card{
  background:linear-gradient(127deg,var(--card-a),var(--card-b));
  border:1px solid var(--stroke); border-radius:18px; padding:20px;
  backdrop-filter:blur(14px);
}
.q-card{
  background:linear-gradient(127deg,var(--card-a),var(--card-b))!important;
  border:1px solid var(--stroke)!important; border-radius:18px!important;
  box-shadow:none!important; color:var(--ink)!important;
}
.sec{
  font-size:10px; letter-spacing:.22em; text-transform:uppercase;
  color:var(--ac2); margin-bottom:14px; padding-bottom:9px;
  border-bottom:1px solid var(--stroke); font-weight:700;
}
/* status dots */
.sdot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle}
.dok{background:var(--ok);box-shadow:0 0 0 4px rgba(1,181,116,.18)}
.derr{background:var(--er);box-shadow:0 0 0 4px rgba(227,26,26,.16)}
.dwn{background:var(--wn);box-shadow:0 0 0 4px rgba(255,181,71,.18)}
.doff{background:var(--muted2)}
/* inputs */
.q-field__control{background:rgba(255,255,255,0.04)!important; border-radius:12px!important}
.q-field__control:before{border-color:var(--stroke)!important}
.q-field__native,.q-field__input,.q-field__label,.q-field__prefix,.q-field__suffix{color:var(--ink)!important}
.q-field__label{color:var(--muted2)!important}
/* buttons */
.q-btn{border-radius:12px!important; text-transform:none!important; font-weight:600!important; letter-spacing:.01em}
.q-btn--standard.bg-cyan,.q-btn.bg-cyan{
  background:linear-gradient(135deg,var(--ac),var(--ac2))!important; color:#fff!important;
  box-shadow:0 8px 18px rgba(0,117,255,.35)!important;
}
/* progress + tabs */
.q-linear-progress__track{background:rgba(255,255,255,.08)!important}
.q-linear-progress__model{background:linear-gradient(90deg,var(--ac),var(--ac2))!important}
.q-tab{border-radius:12px; text-transform:none; font-weight:600; font-size:13px; min-height:44px; color:var(--muted)}
.q-tab--active{color:var(--ink)}
.q-tab__indicator{background:var(--ac2)!important}
.q-separator{background:var(--stroke)!important}
/* nav (drawer) */
.navitem{display:flex;align-items:center;gap:12px;padding:11px 12px;border-radius:14px;
  color:var(--muted);cursor:pointer;font-size:13.5px;font-weight:500;transition:.18s}
.navitem:hover{color:var(--ink);background:rgba(255,255,255,0.03)}
.navitem.on{color:var(--ink);background:linear-gradient(127deg,var(--card-a),var(--card-b));border:1px solid var(--stroke)}
/* log */
.logbox{
  background:rgba(4,8,22,0.6); border:1px solid var(--stroke); border-radius:12px; padding:12px;
  height:230px; overflow-y:auto;
  font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace; font-size:11.5px; line-height:1.9;
}
.logbox .ok{color:var(--ok)} .logbox .err{color:var(--er)} .logbox .wrn{color:var(--wn)} .logbox .inf{color:var(--muted2)}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:600;padding:3px 9px;border-radius:999px}
.pill-ok{color:var(--ok);background:rgba(1,181,116,.12)}
.pill-er{color:var(--er);background:rgba(227,26,26,.12)}
.pill-wn{color:var(--wn);background:rgba(255,181,71,.12)}
</style>
"""


# ── Plotly ──────────────────────────────────────────────────────────────────────
PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(11,20,55,0)",
    plot_bgcolor="rgba(4,8,22,0.35)",
    font=dict(color=MUTED, size=11),
    xaxis=dict(gridcolor="rgba(255,255,255,0.07)", zerolinecolor="rgba(255,255,255,0.10)",
               title="Longitud de onda (nm)", color="#7b86a3"),
    yaxis=dict(gridcolor="rgba(255,255,255,0.07)", zerolinecolor="rgba(255,255,255,0.10)",
               title="Intensidad (ADC)", color="#7b86a3"),
    margin=dict(l=55, r=20, t=30, b=45),
    showlegend=False,
)


def make_fig(spectrum, wavelengths, title="Espectro"):
    """Figura Plotly del espectro, con bandas RGB sombreadas y línea de saturación."""
    wl = list(wavelengths)
    sp = list(spectrum)
    shapes = []
    band_fill = {
        "R": "rgba(227,26,26,0.10)",
        "G": "rgba(1,181,116,0.10)",
        "B": "rgba(0,117,255,0.12)",
    }
    for band, (lo, hi) in RGB_BANDS.items():
        shapes.append({"type": "rect", "xref": "x", "yref": "paper",
                       "x0": lo, "x1": hi, "y0": 0, "y1": 1,
                       "fillcolor": band_fill[band], "line": {"width": 0}})
    # Un espectro puede traer huecos (None/NaN) si el punto no se llegó a medir:
    # se ignoran para decidir si hay saturación.
    finite = [v for v in sp if v is not None and v == v]
    if finite and max(finite) >= SAT_LEVEL:
        shapes.append({"type": "line", "xref": "paper", "yref": "y",
                       "x0": 0, "x1": 1, "y0": SAT_LEVEL, "y1": SAT_LEVEL,
                       "line": {"color": CRIT, "width": 1, "dash": "dash"}})
    layout = dict(PLOTLY_LAYOUT)
    layout["shapes"] = shapes
    return {
        "data": [{
            "x": wl, "y": sp, "type": "scatter", "mode": "lines",
            "line": {"color": ACCENT2, "width": 2},
            "fill": "tozeroy", "fillcolor": "rgba(33,212,253,0.10)", "name": title,
        }],
        "layout": layout,
    }


def make_env_fig(ts, temps, hums, title="Ambiente"):
    """Figura Plotly de temperatura y humedad contra el tiempo (doble eje).

    ``ts`` son las marcas de tiempo tal cual las guarda el servidor
    (``YYYY-MM-DD HH:MM:SS``); Plotly las interpreta como fechas.
    """
    layout = dict(
        paper_bgcolor="rgba(11,20,55,0)",
        plot_bgcolor="rgba(4,8,22,0.35)",
        font=dict(color=MUTED, size=11),
        xaxis=dict(gridcolor="rgba(255,255,255,0.07)",
                   zerolinecolor="rgba(255,255,255,0.10)",
                   title="Hora", color="#7b86a3"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.07)",
                   zerolinecolor="rgba(255,255,255,0.10)",
                   title="Temperatura (°C)", color=WARN),
        yaxis2=dict(title="Humedad (%)", color=ACCENT2, overlaying="y",
                    side="right", showgrid=False),
        margin=dict(l=50, r=50, t=28, b=40),
        showlegend=False,
        title=dict(text=title, font=dict(color=MUTED, size=12)),
        # Sin autosize, Plotly fija 700 px de ancho y desborda la columna del
        # panel de escaneo (aparece scroll horizontal en toda la página).
        autosize=True,
    )
    return {
        "data": [
            # Sin relleno hasta cero: el interés está en la deriva de unas
            # décimas, y un eje anclado en 0 la aplasta contra el borde.
            {"x": list(ts), "y": list(temps), "type": "scatter", "mode": "lines",
             "line": {"color": WARN, "width": 2}, "name": "T (°C)"},
            {"x": list(ts), "y": list(hums), "type": "scatter", "mode": "lines",
             "line": {"color": ACCENT2, "width": 1.5, "dash": "dot"},
             "name": "HR (%)", "yaxis": "y2"},
        ],
        "layout": layout,
    }


def update_plot(plot, fig):
    """Actualiza una figura Plotly soportando NiceGUI 2.x y previas."""
    if hasattr(plot, "update_figure"):
        plot.update_figure(fig)
    else:
        plot.figure = fig
        plot.update()


def event_points(e):
    """Extrae la lista de puntos de un evento Plotly de NiceGUI (tolerante)."""
    args = getattr(e, "args", None)
    if isinstance(args, dict):
        return args.get("points") or []
    if isinstance(args, list):
        return args
    return []
