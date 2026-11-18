# Mejora 3 — Tests automáticos

**Fecha:** 2026-07-22
**Estado:** ✅ Hecho

## Problema

La correctitud del módulo se verificó **a mano en la terminal** durante el
desarrollo (SG sobre una parábola, Ec. 1 en los extremos, PCA rank-1, etc.).
Esas comprobaciones **se perdían**: no quedaban en el repo y nadie más las
podía re-ejecutar. Cualquier cambio futuro podía romper algo en silencio.

## Solución

Se creó la carpeta **`tests/`** con una suite basada en **`unittest`** de la
biblioteca estándar. Se eligió `unittest` en vez de `pytest` para respetar la
filosofía del proyecto de **cero dependencias externas** (todo el pipeline está
en numpy puro; no hay scipy ni sklearn instalados).

### Archivos

| Archivo | Cubre |
|---|---|
| `tests/test_spectral_core.py` | Calibración (288 canales, rango, monotonía), color (`spectrum_to_rgb`, `rgb_to_hex`), lectura de CSV. |
| `tests/test_preprocessing.py` | Savitzky-Golay, corrección blanco/negro, outliers, Bartlett/KMO, PCA y pipeline componible. |

### Casos destacados (verificaciones convertidas en tests permanentes)

- **Savitzky-Golay reproduce exacto un polinomio de grado ≤ 2** (1-D y batch),
  con `atol=1e-9` — esto valida el manejo correcto de bordes (`mode='interp'`).
- **Savitzky-Golay reduce el ruido** respecto de la señal limpia.
- **Ec. 1**: `P=W → 1`, `P=D → 0`, punto medio → `0.5`; y denominador `W=D` no
  produce `inf`/`nan`.
- **Outliers**: un perfil claramente atípico se descarta; `k` mayor conserva más.
- **Bartlett**: sobre datos correlacionados rechaza H0 (`p < 0.05`); y
  `_chi2_sf(3.8415, dof=1) ≈ 0.05` (valor crítico conocido de la chi²).
- **KMO** siempre en `[0, 1]`.
- **PCA**: datos rank-1 concentran > 99% en PC1; shapes y varianza acumulada ≤ 1.
- **Pipeline componible** equivale a llamar las funciones sueltas.

## Cómo correr

Desde la raíz del repo:

```bash
python3 -m unittest discover -s tests -v
```

Resultado actual: **25 tests, todos OK** en ~0.08 s.

## Nota sobre imports

Los tests hacen `import preprocessing` / `import spectral_core` directamente.
Al ejecutar `python3 -m unittest discover -s tests` **desde la raíz del repo**,
la raíz queda en `sys.path` y los imports resuelven sin configuración extra.
