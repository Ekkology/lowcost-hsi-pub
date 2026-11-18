# Mejora 4 — Configuración centralizada

**Fecha:** 2026-07-22
**Estado:** ✅ Hecho

## Problema

Los parámetros del pipeline andaban **sueltos y repetidos** en varias firmas de
funciones (`sg_window`, `sg_polyorder`, `outlier_k`, `n_components`,
`dark_channels`). Cada default estaba escrito más de una vez, y no había forma
de **serializar** la configuración de una corrida para reproducirla o
compararla contra el paper.

## Solución

Un único dataclass **`PreprocessConfig`** con todos los parámetros y sus
defaults (que coinciden con el paper):

```python
@dataclass
class PreprocessConfig:
    dark_channels: int = 10   # canales de borde para estimar dark (sin ref. D)
    sg_window: int = 11       # ventana Savitzky-Golay (se fuerza a impar)
    sg_polyorder: int = 2     # grado SG (paper: 2do grado)
    outlier_k: float = 1.0    # nº de σ del filtro de anómalos (paper: 1)
    n_components: int = 2      # componentes PCA (paper: 2 -> 99.1%)
```

- `preprocess_pipeline()` ahora recibe `config: PreprocessConfig` en lugar de
  cinco kwargs sueltos. Si es `None`, usa los valores del paper.
- Se agregó `build_default_pipeline(config, white, dark)`, que arma el
  `Pipeline` de transformación (hasta antes de PCA) a partir de la config —
  conecta la mejora 4 con la 2.

### Antes / Después

```python
# Antes
preprocess_pipeline(profiles, sg_window=9, outlier_k=1.5, n_components=3)

# Después
cfg = PreprocessConfig(sg_window=9, outlier_k=1.5, n_components=3)
preprocess_pipeline(profiles, config=cfg)
```

## Archivos tocados

| Archivo | Cambio |
|---|---|
| `preprocessing.py` | Nuevo `PreprocessConfig` + `build_default_pipeline`; `preprocess_pipeline` y `main()` usan la config. |
| `tests/test_preprocessing.py` | Nueva clase `TestConfig`; el test del pipeline usa `PreprocessConfig`. |

## Verificación

```bash
python3 -m unittest tests.test_preprocessing.TestConfig -v
```

- `test_defaults_coinciden_con_el_paper`: defaults = 2do grado, k=1, 2 componentes.
- `test_config_propaga_n_components`: cambiar `n_components` cambia la salida de PCA.
- `test_build_default_pipeline_usa_config`: los pasos toman los valores de la config.
