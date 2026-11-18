# Mejora 5 — `logging` en lugar de `print()`

**Fecha:** 2026-07-22
**Estado:** ✅ Hecho

## Problema

`preprocessing.py` comunicaba todo con `print()`. Eso tiene dos problemas:

1. **Al importarlo como librería**, cualquier mensaje contamina la salida del
   programa que lo usa (la UI, un script, un futuro clasificador), sin forma de
   silenciarlo.
2. No hay **niveles** (info vs. advertencia): todo sale igual.

El proyecto ya demuestra que le importan los logs (`ui/log_parser.py`), así que
usar el módulo estándar `logging` es coherente.

## Solución

- Un logger de módulo: `logger = logging.getLogger(__name__)`.
- Las funciones de librería **no imprimen**: emiten `logger.info` /
  `logger.warning`. Ejemplo: `preprocess_pipeline` avisa con `logger.warning`
  cuando corre en modo adaptado (sin referencias W/D).
- `main()` (el CLI) configura la salida con `logging.basicConfig` y expone
  `-q/--quiet` para bajar el nivel a `WARNING`.

### Comportamiento

```bash
# Normal: INFO + advertencias + reporte
python3 preprocessing.py --demo

# Silencioso: solo advertencias/errores
python3 preprocessing.py --demo --quiet
```

Como librería, quien importe `preprocessing` decide qué ver:

```python
import logging
logging.getLogger("preprocessing").setLevel(logging.ERROR)  # silenciar
```

(Los tests hacen exactamente esto para no ensuciar su salida.)

## Archivos tocados

| Archivo | Cambio |
|---|---|
| `preprocessing.py` | Logger de módulo; `print()` → `logger.*`; flag `--quiet`; aviso de modo adaptado. |
| `tests/test_preprocessing.py` | Baja el nivel del logger a ERROR durante los tests. |

## Nota

La reconstrucción legacy (`legacy/Reconstruction/reconstruct_image.py`) todavía
usa `print()`. Queda fuera de esta mejora por ser código legacy; si se
reactiva, conviene migrarlo igual.
