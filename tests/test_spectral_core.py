"""Tests del núcleo espectral compartido (spectral_core).

Ejecutar desde la raíz del repo:
    python3 -m unittest discover -s tests
"""

import csv
import os
import tempfile
import unittest

import numpy as np

import spectral_core as sc


class TestWavelengths(unittest.TestCase):
    def test_longitud_288_canales(self):
        self.assertEqual(len(sc.WAVELENGTHS), 288)
        self.assertEqual(sc.NUM_CHANNELS, 288)

    def test_rango_esperado_del_c12880ma(self):
        # El primer canal ronda 311 nm y el último ~880 nm (hoja de datos).
        self.assertAlmostEqual(sc.WAVELENGTHS[0], 311.49, places=1)
        self.assertGreater(sc.WAVELENGTHS[-1], 800)

    def test_monotona_creciente(self):
        self.assertTrue(np.all(np.diff(sc.WAVELENGTHS) > 0))


class TestColor(unittest.TestCase):
    def test_spectrum_to_rgb_devuelve_tupla_valida(self):
        rgb = sc.spectrum_to_rgb([500.0] * 288)
        self.assertEqual(len(rgb), 3)
        for c in rgb:
            self.assertIsInstance(c, int)
            self.assertTrue(0 <= c <= 255)

    def test_espectro_plano_bajo_da_negro(self):
        # Intensidad ~constante baja tras restar dark -> sin señal -> negro.
        self.assertEqual(sc.spectrum_to_rgb([100.0] * 288), (0, 0, 0))

    def test_rgb_to_hex(self):
        self.assertEqual(sc.rgb_to_hex(255, 136, 0), "#ff8800")
        self.assertEqual(sc.rgb_to_hex(0, 0, 0), "#000000")


class TestLecturaCSV(unittest.TestCase):
    def test_read_capture_parsea_formato_gridcli(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "captura_0001.csv")
            channels = list(range(288))
            with open(path, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["x", "y", "z"] + [f"ch{i}" for i in range(288)])
                w.writerow([1.5, 2.5, 3.5] + channels)

            cap = sc.read_capture(path)
            self.assertEqual((cap.x, cap.y, cap.z), (1.5, 2.5, 3.5))
            self.assertEqual(len(cap.channels), 288)
            self.assertEqual(cap.channels[10], 10.0)

    def test_alias_parse_capture_csv(self):
        self.assertIs(sc.parse_capture_csv, sc.read_capture)


if __name__ == "__main__":
    unittest.main()
