# Mejoras de ingeniería de software

Registro detallado de las refactorizaciones aplicadas al módulo de
preprocesamiento espectral y su entorno. Cada documento describe el problema,
el cambio, los archivos tocados y cómo verificarlo.

| # | Mejora | Estado | Documento |
|---|--------|--------|-----------|
| 1 | DRY: núcleo espectral compartido (`spectral_core.py`) | ✅ Hecho | [01-dry-spectral-core.md](01-dry-spectral-core.md) |
| 2 | Pipeline componible (patrón `Transform`) | ✅ Hecho | [02-pipeline-componible.md](02-pipeline-componible.md) |
| 3 | Tests automáticos (`unittest`, sin dependencias) | ✅ Hecho | [03-tests.md](03-tests.md) |
| 4 | Configuración centralizada (`PreprocessConfig`) | ✅ Hecho | [04-config-centralizada.md](04-config-centralizada.md) |
| 5 | `logging` en lugar de `print()` | ✅ Hecho | [05-logging.md](05-logging.md) |
| 6 | Empaquetado (`pyproject.toml` / `requirements.txt`) | ✅ Hecho | [06-empaquetado.md](06-empaquetado.md) |

## Contexto

El punto de partida fue `preprocessing.py`, que implementa el pipeline de la
guía de adquisición HSI (corrección blanco/negro, Savitzky-Golay, eliminación
de anómalos, Bartlett/KMO, PCA) en numpy puro. Las mejoras buscan que ese
código —y el resto del proyecto— siga buenas prácticas: sin duplicación,
componible, probado y fácil de extender.

## Cómo correr todo

```bash
# Tests (desde la raíz del repo)
python3 -m unittest discover -s tests -v

# Demo del pipeline con datos sintéticos
python3 preprocessing.py --demo

# Pipeline sobre capturas reales
python3 preprocessing.py --captures-dir captures --plot pca.png
```
