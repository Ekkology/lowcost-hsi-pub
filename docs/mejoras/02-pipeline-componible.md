# Mejora 2 — Pipeline componible (patrón Transform)

**Fecha:** 2026-07-22
**Estado:** ✅ Hecho

## Problema

`preprocess_pipeline()` tenía los cinco pasos de la guía **hardcodeados en una
secuencia fija**:

```python
corrected = white_dark_correction(profiles, white=white, dark=dark)
smoothed  = smooth_profiles(corrected, sg_window, sg_polyorder)
clean, keep = remove_outlier_profiles(smoothed, k=outlier_k, return_mask=True)
...
```

Funcionaba, pero:

- No se podía **reordenar, quitar o intercambiar** un paso sin editar la función.
- No se podían **inspeccionar los intermedios** de forma uniforme.
- No seguía el patrón que el propio proyecto ya adoptó para los drivers
  (**Strategy**, en `Sensor/strategy.py`, commit `16e3b39`).

## Solución

Se introdujo una capa de **pasos componibles** con una interfaz común
`apply(X) -> X'`, análoga a `sklearn.Pipeline` pero en numpy puro:

```python
class Transform:
    name: str = "transform"
    def apply(self, X): ...
    def __call__(self, X): return self.apply(X)
```

Pasos concretos (cada uno envuelve una función pura ya existente y probada):

| Clase | Paso de la guía | Función que envuelve |
|---|---|---|
| `WhiteDarkCorrection` | §1.3 corrección blanco/negro | `white_dark_correction` |
| `SavitzkyGolay` | §2.1 suavizado | `smooth_profiles` |
| `OutlierRemoval` | §2.2 anómalos (guarda la máscara en `last_mask`) | `remove_outlier_profiles` |
| `PCAStep` | §3.3 PCA (guarda el `PCAResult` en `last_result`) | `pca` |

Y un orquestador:

```python
class Pipeline:
    def __init__(self, steps: list[Transform]): ...
    def run(self, X, record=False): ...   # record=True -> también el historial
```

Uso:

```python
pipe = Pipeline([
    WhiteDarkCorrection(white=W, dark=D),
    SavitzkyGolay(window=9, polyorder=2),
    OutlierRemoval(k=1.5),
    PCAStep(n_components=3),
])
scores = pipe.run(profiles)
scores, history = pipe.run(profiles, record=True)  # inspeccionar intermedios
```

## Compatibilidad

- `preprocess_pipeline()` **conserva su firma y su salida** (`PipelineResult`
  con diagnósticos Bartlett/KMO y `report()`), pero internamente **ahora se
  construye sobre los pasos componibles** — no hay lógica duplicada: la capa de
  objetos es el motor real.
- Las funciones puras (`white_dark_correction`, `smooth_profiles`, etc.) siguen
  disponibles por separado. Nada de la API anterior se rompió.

## Archivos tocados

| Archivo | Cambio |
|---|---|
| `preprocessing.py` | Nueva sección "Pasos componibles" (`Transform`, 4 pasos, `Pipeline`); `preprocess_pipeline` refactorizado para usarlos. |

## Por qué los diagnósticos no son pasos

Bartlett y KMO **no transforman** los datos: son *diagnósticos* que deciden si
tiene sentido reducir dimensión. Por eso se mantienen como funciones y se
reportan en `PipelineResult`, en lugar de forzarlos dentro de la cadena de
`Transform` (que asume `X -> X'`).

## Verificación

Cubierto por `tests/test_preprocessing.py::TestPipelineComponible`:

- `test_pipeline_equivale_a_funcional`: la cadena de objetos da el mismo
  resultado que llamar las funciones sueltas.
- `test_pipeline_record_devuelve_historial`: `record=True` devuelve el nombre y
  la salida de cada etapa en orden.

```bash
python3 -m unittest tests.test_preprocessing.TestPipelineComponible -v
```
