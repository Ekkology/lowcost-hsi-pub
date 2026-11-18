"""Tests del núcleo de etiquetado manual (labeling.py). numpy puro, sin UI."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from labeling import (  # noqa: E402
    DEFAULT_LEVELS,
    apply_label_colors,
    build_label_figure,
    build_pixel_grid,
    default_levels,
    label_summary,
    labels_from_csv,
    labels_from_json,
    labels_to_csv,
    labels_to_json,
    validate_levels,
)
from spectral_core import NUM_CHANNELS, ScanData  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────
def _make_scan(n_cols=3, n_rows=2, seed=0) -> ScanData:
    rng = np.random.default_rng(seed)
    keys = [(c, r) for r in range(n_rows) for c in range(n_cols)]
    n = len(keys)
    profiles = rng.uniform(100, 3000, size=(n, NUM_CHANNELS))
    grid_index = np.array(keys, dtype=int)  # (col, row)
    coords = np.zeros((n, 3))
    from spectral_core import WAVELENGTHS

    return ScanData(
        profiles=profiles,
        coords=coords,
        wavelengths=WAVELENGTHS.copy(),
        grid_index=grid_index,
    )


# ── Niveles ─────────────────────────────────────────────────────────────────
def test_default_levels_is_copy():
    a = default_levels()
    a[0]["name"] = "X"
    assert DEFAULT_LEVELS[0]["name"] == "Sano"  # no se mutó la global


def test_validate_levels_ok():
    lv = validate_levels(
        [{"id": 0, "name": "A", "color": "#fff"}, {"id": 1, "name": "B", "color": "#00ff00"}]
    )
    assert len(lv) == 2 and lv[1]["name"] == "B"


@pytest.mark.parametrize(
    "bad",
    [
        [],
        "nope",
        [{"id": 0, "name": "", "color": "#fff"}],          # nombre vacío
        [{"id": 0, "name": "A", "color": "rojo"}],          # hex inválido
        [{"id": 0, "name": "A", "color": "#fff"},
         {"id": 0, "name": "B", "color": "#000"}],          # id duplicado
        [{"name": "A", "color": "#fff"}],                   # sin id
    ],
)
def test_validate_levels_rejects(bad):
    with pytest.raises(ValueError):
        validate_levels(bad)


# ── PixelGrid ───────────────────────────────────────────────────────────────
def test_build_pixel_grid_dims_and_keys():
    scan = _make_scan(n_cols=3, n_rows=2)
    grid = build_pixel_grid(scan)
    assert grid.n_cols == 3 and grid.n_rows == 2
    assert len(grid) == 6
    assert set(grid.keys) == {(c, r) for r in range(2) for c in range(3)}
    assert all(isinstance(c, str) and c.startswith("#") for c in grid.colors)


def test_build_pixel_grid_requires_grid_index():
    scan = _make_scan()
    scan.grid_index = None
    with pytest.raises(ValueError):
        build_pixel_grid(scan)


def test_apply_label_colors_overrides_only_labeled():
    scan = _make_scan(n_cols=2, n_rows=1)
    grid = build_pixel_grid(scan)
    levels = default_levels()
    labels = {(0, 0): 3}  # Severo -> #f85149
    colors = apply_label_colors(grid, labels, levels)
    idx00 = grid.keys.index((0, 0))
    idx10 = grid.keys.index((1, 0))
    assert colors[idx00] == "#f85149"
    assert colors[idx10] == grid.colors[idx10]  # sin etiqueta: color de fondo


def test_apply_label_colors_unknown_level_falls_back():
    scan = _make_scan(n_cols=1, n_rows=1)
    grid = build_pixel_grid(scan)
    colors = apply_label_colors(grid, {(0, 0): 99}, default_levels())
    assert colors[0] == grid.colors[0]  # nivel inexistente -> fondo


def test_build_label_figure_shape():
    scan = _make_scan(n_cols=2, n_rows=2)
    grid = build_pixel_grid(scan)
    fig = build_label_figure(grid, grid.colors, title="T")
    tr = fig["data"][0]
    assert tr["mode"] == "markers"
    assert len(tr["x"]) == len(grid) == len(tr["customdata"])
    assert tr["customdata"][0] == [grid.keys[0][0], grid.keys[0][1]]
    assert fig["layout"]["yaxis"]["autorange"] == "reversed"
    assert fig["layout"]["xaxis"]["scaleanchor"] == "y"


# ── (De)serialización ────────────────────────────────────────────────────────
def test_labels_csv_roundtrip():
    labels = {(0, 0): 1, (2, 5): 3, (1, 1): 0}
    text = labels_to_csv(labels)
    assert text.splitlines()[0] == "col,row,nivel"
    assert labels_from_csv(text) == labels


def test_labels_json_roundtrip():
    labels = {(0, 0): 1, (3, 2): 2}
    assert labels_from_json(labels_to_json(labels)) == labels


def test_labels_from_json_tolerates_garbage():
    assert labels_from_json({"bad": 1, "1,2": 3, "x,y": 0}) == {(1, 2): 3}
    assert labels_from_json(None) == {}


def test_labels_from_csv_tolerates_bad_rows():
    text = "col,row,nivel\n0,0,1\nbasura\n2,3,x\n4,5,2\n"
    assert labels_from_csv(text) == {(0, 0): 1, (4, 5): 2}


# ── Resumen ──────────────────────────────────────────────────────────────────
def test_label_summary_counts():
    levels = default_levels()
    labels = {(0, 0): 0, (1, 0): 0, (2, 0): 3}
    s = label_summary(labels, levels, total_pixels=10)
    assert s == {"total": 10, "labeled": 3, "by_level": {0: 2, 1: 0, 2: 0, 3: 1}}
