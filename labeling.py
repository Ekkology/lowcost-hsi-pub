"""labeling.py — Etiquetado manual de píxeles sobre un escaneo HSI.

Segmentación **manual** (NO clasificación automática): el usuario selecciona
píxeles o zonas en la imagen reconstruida y les asigna un *nivel* (p. ej. grado
de infección) definido por él mismo. La propagación automática de esas etiquetas
al resto de la imagen es una etapa posterior; aquí solo se produce la verdad de
campo.

Este módulo aporta la lógica **pura** (numpy + spectral_core, misma filosofía de
núcleo compartido del repo):

  - taxonomía de niveles por defecto y su validación,
  - construcción del color RGB por píxel para pintar la grilla interactiva,
  - la figura Plotly (como dict) de la imagen de etiquetado,
  - (de)serialización de etiquetas a CSV (``col,row,nivel``) y a JSON.

La UI (``ui/app.py``) lo usa para la imagen interactiva; el servidor
(``rpi_server.py``) para persistir. Ninguna dependencia de NiceGUI/FastAPI aquí.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Optional

import numpy as np

from spectral_core import ScanData, rgb_to_hex, spectrum_to_rgb

# ===========================================================================
# Taxonomía de niveles
# ---------------------------------------------------------------------------
# El set por defecto (editable desde la UI). Cada nivel: id estable, nombre y
# color hex para pintarlo sobre la grilla.
# ===========================================================================

DEFAULT_LEVELS: list[dict] = [
    {"id": 0, "name": "Sano", "color": "#3fb950"},
    {"id": 1, "name": "Leve", "color": "#d29922"},
    {"id": 2, "name": "Moderado", "color": "#db6d28"},
    {"id": 3, "name": "Severo", "color": "#f85149"},
]


def default_levels() -> list[dict]:
    """Copia fresca de la taxonomía por defecto (para no mutar la global)."""
    return [dict(lv) for lv in DEFAULT_LEVELS]


def _valid_hex(color: str) -> bool:
    if not isinstance(color, str) or not color.startswith("#"):
        return False
    if len(color) not in (4, 7):  # #rgb o #rrggbb
        return False
    try:
        int(color[1:], 16)
    except ValueError:
        return False
    return True


def validate_levels(levels) -> list[dict]:
    """Normaliza y valida una lista de niveles; lanza ``ValueError`` si es inválida.

    Cada nivel debe tener ``id`` (entero único), ``name`` (no vacío) y ``color``
    (hex ``#rgb``/``#rrggbb``). Devuelve la lista saneada.
    """
    if not isinstance(levels, list) or not levels:
        raise ValueError("Se requiere al menos un nivel")
    out: list[dict] = []
    seen: set[int] = set()
    for i, lv in enumerate(levels):
        if not isinstance(lv, dict):
            raise ValueError(f"Nivel #{i} inválido: se esperaba un objeto")
        try:
            lid = int(lv["id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Nivel #{i}: 'id' entero requerido")
        name = str(lv.get("name", "")).strip()
        color = str(lv.get("color", "")).strip()
        if not name:
            raise ValueError(f"Nivel #{i}: 'name' vacío")
        if not _valid_hex(color):
            raise ValueError(f"Nivel '{name}': color hex inválido ({color!r})")
        if lid in seen:
            raise ValueError(f"id de nivel duplicado: {lid}")
        seen.add(lid)
        out.append({"id": lid, "name": name, "color": color})
    return out


# ===========================================================================
# Grilla de píxeles para la imagen interactiva
# ===========================================================================


@dataclass
class PixelGrid:
    """Arrays por-píxel para pintar y etiquetar la imagen.

    - ``n_rows`` / ``n_cols``: dimensiones de la grilla.
    - ``cols`` / ``rows``: índice de columna/fila por marcador (paralelos).
    - ``colors``: color hex RGB reconstruido de cada píxel (fondo de la imagen).
    - ``keys``: ``(col, row)`` por marcador, para mapear selección → píxel.
    """

    n_rows: int
    n_cols: int
    cols: np.ndarray
    rows: np.ndarray
    colors: list[str]
    keys: list[tuple[int, int]]

    def __len__(self) -> int:
        return len(self.keys)


def build_pixel_grid(scan: ScanData) -> PixelGrid:
    """Arma la :class:`PixelGrid` de un escaneo (formato A, con índices col/row).

    Reconstruye el color de cada píxel con :func:`spectral_core.spectrum_to_rgb`
    (CIE 1931). Requiere que el escaneo traiga ``grid_index``; si no, no se puede
    etiquetar por píxel de forma fiable y se lanza ``ValueError``.
    """
    if scan.grid_index is None:
        raise ValueError(
            "El escaneo no trae índices (col,row); no se puede etiquetar por píxel."
        )
    gi = np.asarray(scan.grid_index)
    cols = gi[:, 0].astype(int)
    rows = gi[:, 1].astype(int)
    n_cols = int(cols.max()) + 1
    n_rows = int(rows.max()) + 1

    colors: list[str] = []
    keys: list[tuple[int, int]] = []
    for i in range(len(scan)):
        r, g, b = spectrum_to_rgb(scan.profiles[i])
        colors.append(rgb_to_hex(r, g, b))
        keys.append((int(cols[i]), int(rows[i])))

    return PixelGrid(
        n_rows=n_rows, n_cols=n_cols, cols=cols, rows=rows, colors=colors, keys=keys
    )


def apply_label_colors(
    grid: PixelGrid, labels: dict, levels: list[dict]
) -> list[str]:
    """Color por píxel: el del nivel si está etiquetado, si no el RGB de fondo."""
    color_by_id = {lv["id"]: lv["color"] for lv in levels}
    out: list[str] = []
    for i, key in enumerate(grid.keys):
        lvl = labels.get(key)
        out.append(color_by_id.get(lvl, grid.colors[i]) if lvl is not None else grid.colors[i])
    return out


def build_label_figure(
    grid: PixelGrid, colors: list[str], title: str = "Etiquetado"
) -> dict:
    """Figura Plotly (dict) de la imagen interactiva de etiquetado.

    Un marcador cuadrado por píxel en ``(col, row)``; ``customdata`` lleva
    ``[col, row]`` para resolver clicks/selección. El eje Y va invertido (row 0
    arriba) y con relación de aspecto cuadrada (``scaleanchor``).
    """
    n = len(grid)
    return {
        "data": [
            {
                # scattergl escala mejor en grillas grandes.
                "type": "scattergl" if n > 2000 else "scatter",
                "x": grid.cols.tolist(),
                "y": grid.rows.tolist(),
                "customdata": [[c, r] for (c, r) in grid.keys],
                "mode": "markers",
                "marker": {
                    "symbol": "square",
                    "size": 14,
                    "color": colors,
                    "line": {"width": 0},
                },
                "hovertemplate": "col=%{x} · row=%{y}<extra></extra>",
            }
        ],
        "layout": {
            "paper_bgcolor": "rgba(11,20,55,0)",
            "plot_bgcolor": "rgba(4,8,22,0.35)",
            "font": {"color": "#a0aec0", "size": 11},
            "xaxis": {
                "title": "col",
                "color": "#7b86a3",
                "scaleanchor": "y",
                "constrain": "domain",
                "showgrid": False,
                "zeroline": False,
            },
            "yaxis": {
                "title": "row",
                "color": "#7b86a3",
                "autorange": "reversed",
                "showgrid": False,
                "zeroline": False,
            },
            "margin": {"l": 40, "r": 10, "t": 34, "b": 35},
            "showlegend": False,
            "dragmode": "select",
            "title": {"text": title, "font": {"color": "#e7ecf7", "size": 13}},
        },
    }


# ===========================================================================
# (De)serialización de etiquetas
# ---------------------------------------------------------------------------
# Formato en disco: CSV ``col,row,nivel`` (una fila por píxel etiquetado),
# legible y consistente con el resto de artefactos del escaneo. En la API viajan
# como JSON con claves "col,row" (JSON no admite tuplas como clave).
# ===========================================================================


def labels_to_csv(labels: dict) -> str:
    """Serializa ``{(col,row): nivel}`` a texto CSV con cabecera."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["col", "row", "nivel"])
    for (c, r), lvl in sorted(labels.items()):
        w.writerow([c, r, lvl])
    return buf.getvalue()


def labels_from_csv(text: str) -> dict:
    """Parsea un CSV ``col,row,nivel`` a ``{(col,row): nivel}`` (tolerante)."""
    labels: dict[tuple[int, int], int] = {}
    reader = csv.reader(io.StringIO(text))
    next(reader, None)  # cabecera
    for row in reader:
        if len(row) < 3:
            continue
        try:
            c, r, lvl = int(row[0]), int(row[1]), int(row[2])
        except ValueError:
            continue
        labels[(c, r)] = lvl
    return labels


def labels_to_json(labels: dict) -> dict:
    """``{(col,row): nivel}`` → ``{"col,row": nivel}`` (serializable en JSON)."""
    return {f"{c},{r}": int(lvl) for (c, r), lvl in labels.items()}


def labels_from_json(obj: Optional[dict]) -> dict:
    """``{"col,row": nivel}`` → ``{(col,row): nivel}`` (tolerante a basura)."""
    out: dict[tuple[int, int], int] = {}
    for k, v in (obj or {}).items():
        try:
            cs, rs = str(k).split(",")
            out[(int(cs), int(rs))] = int(v)
        except (ValueError, AttributeError):
            continue
    return out


def label_summary(labels: dict, levels: list[dict], total_pixels: int) -> dict:
    """Resumen para la UI: total, etiquetados y conteo por nivel."""
    counts = {lv["id"]: 0 for lv in levels}
    for lvl in labels.values():
        counts[lvl] = counts.get(lvl, 0) + 1
    return {"total": total_pixels, "labeled": len(labels), "by_level": counts}
