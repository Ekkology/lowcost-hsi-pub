#!/usr/bin/env python3
"""Arranque del panel del escáner hiperespectral.

Requiere ``rpi_server.py`` corriendo (en la Raspberry Pi o en localhost):
    uvicorn rpi_server:app --host 0.0.0.0 --port 8000
"""
import os

from nicegui import ui

from ui.app import create_app

# Puerto configurable (evita choques de puerto): PORT=8090 python run_ui.py
PORT = int(os.environ.get("PORT", "8080"))

create_app()
ui.run(host="0.0.0.0", port=PORT, title="Escáner HSI", favicon="🔬", dark=True, reload=False)
