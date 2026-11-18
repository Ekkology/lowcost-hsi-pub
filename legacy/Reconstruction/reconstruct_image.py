"""
Reconstrucción de imagen a partir de datos espectrales del sensor Hamamatsu C12880MA.
Cada CSV (captura_XXXX.csv) contiene 1 fila con x, y, z y 288 canales de intensidad.
Se convierte cada espectro a un color RGB visible usando las funciones CIE 1931.
"""

import argparse
import numpy as np
import os
import sys
from pathlib import Path

# La calibración, las funciones CIE 1931, la matriz XYZ→sRGB, la gamma y el
# lector de datasets viven en spectral_core (única fuente de verdad del
# proyecto). reconstruct_image aporta solo la lógica *a nivel de imagen*
# (integración a XYZ y normalización de la grilla por el blanco de la escena).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from spectral_core import (  # noqa: E402
    WAVELENGTHS,
    XYZ_TO_SRGB,
    cie_x_bar,
    cie_y_bar,
    cie_z_bar,
    load_dataset,
    srgb_gamma,
)


# ---------------------------------------------------------------------------
# 2. Conversión espectro → XYZ → sRGB lineal → sRGB gamma
# ---------------------------------------------------------------------------


def spectrum_to_xyz(wavelengths, intensities):
    """Integra el espectro con las funciones CIE para obtener XYZ."""
    # Solo usar rango visible (380-780 nm)
    mask = (wavelengths >= 380) & (wavelengths <= 780)
    wl = wavelengths[mask]
    spec = intensities[mask]

    if len(wl) < 2:
        return np.array([0.0, 0.0, 0.0])

    # Diferencial de longitud de onda
    dlambda = np.gradient(wl)

    X = np.sum(spec * cie_x_bar(wl) * dlambda)
    Y = np.sum(spec * cie_y_bar(wl) * dlambda)
    Z = np.sum(spec * cie_z_bar(wl) * dlambda)

    return np.array([X, Y, Z])


def xyz_to_srgb(xyz):
    """Convierte XYZ a sRGB [0-255]."""
    rgb_linear = XYZ_TO_SRGB @ xyz
    rgb_linear = np.clip(rgb_linear, 0, None)
    return rgb_linear


# ---------------------------------------------------------------------------
# 3. Reconstrucción de la grilla desde un dataset
# ---------------------------------------------------------------------------

def _cluster_values(values):
    """Agrupa valores flotantes en clusters, auto-detectando el umbral.

    Usa la media geométrica entre el gap mínimo y máximo como frontera
    entre ruido de backlash (gaps pequeños) y pasos reales de la grilla
    (gaps grandes).  Funciona sin parámetros de tolerancia manuales.

    Solo se usa como *fallback* para el formato B legacy (captura_*.csv), que
    no trae los índices (col, row). En el formato A canónico se usan esos
    índices directamente y no hace falta agrupar.
    """
    if not values:
        return []
    sorted_vals = np.array(sorted(values))
    if len(sorted_vals) <= 1:
        return sorted_vals.tolist()

    gaps = np.diff(sorted_vals)
    nonzero = gaps[gaps > 1e-9]

    if len(nonzero) == 0:
        return [float(np.mean(sorted_vals))]

    # Media geométrica: separa bien backlash (~0.1 mm) de paso (~5 mm)
    threshold = np.sqrt(float(nonzero.min()) * float(nonzero.max()))

    clusters = [[sorted_vals[0]]]
    for i in range(len(gaps)):
        if gaps[i] <= threshold:
            clusters[-1].append(sorted_vals[i + 1])
        else:
            clusters.append([sorted_vals[i + 1]])

    return [float(np.mean(c)) for c in clusters]


def build_grid(scan):
    """Arma la grilla (rows_y, cols_x, grid_dict) desde un ScanData.

    Si el dataset trae índices (col, row) — formato A canónico — los usa
    directamente: reconstrucción exacta, sin adivinar. Si no —formato B
    legacy— cae al agrupamiento geométrico de coordenadas (``_cluster_values``).

    grid_dict[(row_idx, col_idx)] = (filepath, x, y, z, channels)
    """
    coords = scan.coords
    profiles = scan.profiles
    n = len(scan)
    fpath = scan.files[0] if scan.files else None

    if scan.grid_index is not None:
        cols = scan.grid_index[:, 0]
        rows = scan.grid_index[:, 1]
        n_cols = int(cols.max()) + 1
        n_rows = int(rows.max()) + 1
        # Coordenada física representativa de cada índice (media de sus puntos).
        cols_x = [
            float(coords[cols == c, 0].mean()) if np.any(cols == c) else float(c)
            for c in range(n_cols)
        ]
        rows_y = [
            float(coords[rows == r, 1].mean()) if np.any(rows == r) else float(r)
            for r in range(n_rows)
        ]
        grid_dict = {}
        for i in range(n):
            grid_dict[(int(rows[i]), int(cols[i]))] = (
                fpath,
                float(coords[i, 0]), float(coords[i, 1]), float(coords[i, 2]),
                profiles[i],
            )
        return rows_y, cols_x, grid_dict

    # --- Fallback formato B: inferir la grilla agrupando coordenadas ---
    cols_x = _cluster_values(coords[:, 0].tolist())
    rows_y = _cluster_values(coords[:, 1].tolist())
    grid_dict = {}
    for i in range(n):
        x, y, z = coords[i]
        col_idx = int(np.argmin([abs(x - cx) for cx in cols_x]))
        row_idx = int(np.argmin([abs(y - ry) for ry in rows_y]))
        grid_dict[(row_idx, col_idx)] = (fpath, float(x), float(y), float(z), profiles[i])
    return rows_y, cols_x, grid_dict


def subtract_dark(intensities, dark_level=None):
    """Resta el nivel de oscuridad (dark current) del espectro."""
    if dark_level is None:
        # Estimar dark como la mediana de los extremos del espectro (fuera del visible)
        dark_level = np.median(intensities[:10])
    return np.clip(intensities - dark_level, 0, None)


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Reconstruye imagen RGB a partir de capturas espectrales del C12880MA."
    )
    p.add_argument("--captures-dir", default=str(_REPO_ROOT / "scans"),
                   help="Ruta a un espectros.csv (formato A) o a una carpeta con "
                        "espectros.csv / captura_*.csv (default: <proyecto>/scans)")
    p.add_argument("--output-dir", default=str(Path(__file__).resolve().parent),
                   help="Carpeta para imágenes de salida (default: Reconstruction/)")
    p.add_argument("--scale", type=int, default=80,
                   help="Factor de ampliación para la imagen escalada (default: 80)")
    return p.parse_args()


def main():
    args = parse_args()
    captures_dir = args.captures_dir
    output_dir = args.output_dir
    scale = args.scale

    os.makedirs(output_dir, exist_ok=True)
    output_path = Path(output_dir)

    scan = load_dataset(captures_dir)
    rows_y, cols_x, grid_dict = build_grid(scan)

    n_rows = len(rows_y)
    n_cols = len(cols_x)
    print(f"Grilla detectada: {n_rows} filas x {n_cols} columnas")
    print(f"  Y (filas): {[f'{v:.2f}' for v in rows_y]}")
    print(f"  X (cols):  {[f'{v:.2f}' for v in cols_x]}")
    print(f"Total píxeles: {n_rows * n_cols}")

    # Leer todos los espectros y calcular XYZ
    xyz_grid = np.zeros((n_rows, n_cols, 3))
    spectra = {}

    for r_idx in range(n_rows):
        for c_idx in range(n_cols):
            key = (r_idx, c_idx)
            if key in grid_dict:
                fpath, x, y, z, channels = grid_dict[key]
                intensity_clean = subtract_dark(channels)
                spectra[key] = (WAVELENGTHS, intensity_clean)
                xyz = spectrum_to_xyz(WAVELENGTHS, intensity_clean)
                xyz_grid[r_idx, c_idx] = xyz
                label = f"({x:.1f},{y:.1f})"
                print(f"  [{r_idx},{c_idx}] {label}: XYZ = [{xyz[0]:.1f}, {xyz[1]:.1f}, {xyz[2]:.1f}]")
            else:
                print(f"  [{r_idx},{c_idx}]: FALTANTE")

    # Normalizar XYZ: usar el máximo Y (luminancia) como referencia de blanco
    max_Y = np.max(xyz_grid[:, :, 1])
    if max_Y > 0:
        xyz_norm = xyz_grid / max_Y
    else:
        xyz_norm = xyz_grid

    # Convertir a sRGB lineal
    rgb_linear = np.zeros((n_rows, n_cols, 3))
    for r in range(n_rows):
        for c in range(n_cols):
            rgb_linear[r, c] = xyz_to_srgb(xyz_norm[r, c])

    # Normalizar al rango [0, 1] si hay valores > 1
    max_val = rgb_linear.max()
    if max_val > 1.0:
        rgb_linear /= max_val

    # Aplicar gamma sRGB
    rgb_gamma = srgb_gamma(rgb_linear)

    # Convertir a 8-bit
    rgb_8bit = np.clip(rgb_gamma * 255, 0, 255).astype(np.uint8)

    print(f"\nColores RGB reconstruidos:")
    for r_idx in range(n_rows):
        for c_idx in range(n_cols):
            key = (r_idx, c_idx)
            color = rgb_8bit[r_idx, c_idx]
            if key in grid_dict:
                _, x, y, _, _ = grid_dict[key]
                label = f"({x:.1f},{y:.1f})"
            else:
                label = f"[{r_idx},{c_idx}]"
            print(f"  {label}: RGB({color[0]:3d}, {color[1]:3d}, {color[2]:3d})")

    # -----------------------------------------------------------------------
    # 6. Generar imagen
    # -----------------------------------------------------------------------
    try:
        from PIL import Image

        # Imagen en escala real (1 pixel por medición)
        img_raw = Image.fromarray(rgb_8bit, 'RGB')
        img_raw.save(output_path / "imagen_espectral_raw.png")
        print(f"\nImagen raw guardada: {output_path / 'imagen_espectral_raw.png'} ({n_cols}x{n_rows} px)")

        # Imagen ampliada para visualización
        img_scaled = img_raw.resize((n_cols * scale, n_rows * scale), Image.NEAREST)
        img_scaled.save(output_path / "imagen_espectral.png")
        print(f"Imagen ampliada guardada: {output_path / 'imagen_espectral.png'} ({n_cols * scale}x{n_rows * scale} px)")

        # Imagen con etiquetas y bordes de celda
        from PIL import ImageDraw
        cell_size = 100
        margin = 50
        img_w = margin + n_cols * cell_size
        img_h = margin + n_rows * cell_size
        img_labeled = Image.new('RGB', (img_w, img_h), (30, 30, 30))
        draw = ImageDraw.Draw(img_labeled)

        # Dibujar celdas con color
        for r_idx in range(n_rows):
            # Etiqueta de fila (coordenada Y)
            y_pos = margin + r_idx * cell_size + cell_size // 2
            row_label = f"Y{rows_y[r_idx]:.1f}"
            draw.text((2, y_pos - 6), row_label, fill=(200, 200, 200))

            for c_idx in range(n_cols):
                x0 = margin + c_idx * cell_size
                y0 = margin + r_idx * cell_size
                x1 = x0 + cell_size - 2
                y1 = y0 + cell_size - 2
                color = tuple(rgb_8bit[r_idx, c_idx])
                draw.rectangle([x0, y0, x1, y1], fill=color, outline=(80, 80, 80))

                # Etiqueta dentro de la celda
                key = (r_idx, c_idx)
                if key in grid_dict:
                    _, gx, gy, _, _ = grid_dict[key]
                    label = f"({gx:.1f},{gy:.1f})"
                else:
                    label = f"[{r_idx},{c_idx}]"
                # Elegir color de texto según brillo
                brightness = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
                text_color = (0, 0, 0) if brightness > 128 else (255, 255, 255)
                draw.text((x0 + 3, y0 + 3), label, fill=text_color)
                draw.text((x0 + 3, y0 + cell_size - 20),
                          f"({color[0]},{color[1]},{color[2]})",
                          fill=text_color)

        # Etiquetas de columna (coordenada X)
        for c_idx in range(n_cols):
            x_pos = margin + c_idx * cell_size + 5
            col_label = f"X{cols_x[c_idx]:.1f}"
            draw.text((x_pos, 5), col_label, fill=(200, 200, 200))

        img_labeled.save(output_path / "imagen_espectral_etiquetada.png")
        print(f"Imagen etiquetada guardada: {output_path / 'imagen_espectral_etiquetada.png'} ({img_w}x{img_h} px)")

    except ImportError:
        print("\nPIL/Pillow no está instalado. Instalalo con: pip install Pillow")
        print("Guardando datos RGB como CSV...")
        with open(output_path / "colores_rgb.csv", 'w') as f:
            f.write("pixel,R,G,B\n")
            for r_idx in range(n_rows):
                for c_idx in range(n_cols):
                    color = rgb_8bit[r_idx, c_idx]
                    key = (r_idx, c_idx)
                    if key in grid_dict:
                        _, x, y, _, _ = grid_dict[key]
                        label = f"({x:.1f},{y:.1f})"
                    else:
                        label = f"[{r_idx},{c_idx}]"
                    f.write(f"{label},{color[0]},{color[1]},{color[2]}\n")
        print("Colores guardados en colores_rgb.csv")

    # -----------------------------------------------------------------------
    # 7. Gráfica de espectros (opcional)
    # -----------------------------------------------------------------------
    try:
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(n_rows, n_cols, figsize=(3 * n_cols, 3 * n_rows),
                                 sharex=True, sharey=True)
        if n_rows == 1 and n_cols == 1:
            axes = np.array([[axes]])
        elif n_rows == 1:
            axes = axes[np.newaxis, :]
        elif n_cols == 1:
            axes = axes[:, np.newaxis]

        for r_idx in range(n_rows):
            for c_idx in range(n_cols):
                ax = axes[r_idx, c_idx]
                key = (r_idx, c_idx)
                if key in spectra:
                    wl, spec = spectra[key]
                    color_rgb = rgb_8bit[r_idx, c_idx] / 255.0
                    ax.fill_between(wl, spec, alpha=0.3, color=color_rgb)
                    ax.plot(wl, spec, color=color_rgb, linewidth=0.8)
                    if key in grid_dict:
                        _, gx, gy, _, _ = grid_dict[key]
                        title = f"({gx:.1f},{gy:.1f})"
                    else:
                        title = f"[{r_idx},{c_idx}]"
                    ax.set_title(title, fontsize=9,
                                 color=color_rgb if np.mean(color_rgb) > 0.3 else 'gray')
                ax.set_xlim(380, 780)
                ax.tick_params(labelsize=6)
                if r_idx == n_rows - 1:
                    ax.set_xlabel("λ (nm)", fontsize=7)
                if c_idx == 0:
                    ax.set_ylabel("Intensidad", fontsize=7)

        plt.suptitle("Espectros por píxel - Hamamatsu C12880MA", fontsize=14)
        plt.tight_layout()
        plt.savefig(output_path / "espectros_por_pixel.png", dpi=150, bbox_inches='tight')
        plt.close()
        print(f"Gráfica de espectros guardada: {output_path / 'espectros_por_pixel.png'}")

    except ImportError:
        print("matplotlib no está instalado. Instalalo con: pip install matplotlib")
        print("(La gráfica de espectros es opcional)")


if __name__ == "__main__":
    main()
