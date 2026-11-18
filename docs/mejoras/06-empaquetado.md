# Mejora 6 — Empaquetado y dependencias explícitas

**Fecha:** 2026-07-22
**Estado:** ✅ Hecho

## Problema

No había forma declarada de saber qué necesita el proyecto para correr. Quien
clonara el repo tenía que **adivinar** las dependencias (numpy, matplotlib,
pillow, nicegui) y descubrir a los tropezones que **no** se usan scipy ni
scikit-learn. Tampoco había manera de instalar los módulos de la raíz
(`spectral_core`, `preprocessing`) como paquete, lo que empujaba a duplicar
código en lugar de importarlo (ver mejora 1).

## Solución

Dos archivos:

### `requirements.txt`
Lista plana para `pip install -r requirements.txt`, con un aviso explícito de
que el pipeline es numpy puro **a propósito** (no agregar scipy/sklearn sin
motivo). Separa el núcleo obligatorio (`numpy`) de lo opcional
(`matplotlib`, `pillow`).

### `pyproject.toml`
Metadata del proyecto + dependencias, con **extras** por caso de uso:

```toml
[project]
dependencies = ["numpy>=2.0"]          # el pipeline solo necesita numpy

[project.optional-dependencies]
viz = ["matplotlib>=3.9", "pillow>=11"]  # gráficas / reconstrucción
ui  = ["nicegui", "pillow>=11"]          # interfaz web (run_ui.py)
```

Instalación editable (además resuelve los imports entre módulos de la raíz):

```bash
pip install -e .          # solo núcleo
pip install -e ".[viz]"   # + gráficas
pip install -e ".[ui]"    # + interfaz web
```

`requires-python = ">=3.9"` (el intérprete del proyecto es 3.9; los `.pyc`
del repo confirman 3.9 y 3.11).

## Archivos tocados

| Archivo | Cambio |
|---|---|
| `requirements.txt` | **Nuevo.** Dependencias runtime + nota sobre numpy puro. |
| `pyproject.toml` | **Nuevo.** Metadata, deps, extras `viz`/`ui`, módulos y paquetes. |

## Decisiones

- **Sin `readme`** en `pyproject.toml`: el repo no tiene `README.md` todavía;
  se omitió la clave para no dejar una referencia rota (agregarla es un buen
  próximo paso).
- Se excluyen de la distribución `tests*`, `legacy*` y `docs*`
  (`[tool.setuptools.packages.find]`).
