"""Tests del pipeline de preprocesamiento espectral.

Convierten en tests permanentes las verificaciones que antes se hacían a mano
en la terminal. Ejecutar desde la raíz del repo:

    python3 -m unittest discover -s tests
"""

import logging
import unittest

import numpy as np

import preprocessing as pp

# Silenciar los avisos del pipeline (p. ej. "sin referencias W/D") durante los
# tests para no ensuciar la salida.
logging.getLogger("preprocessing").setLevel(logging.ERROR)


class TestSavitzkyGolay(unittest.TestCase):
    def test_reproduce_polinomio_grado2_1d(self):
        # SG de orden 2 debe reproducir exacto un polinomio de grado <= 2.
        x = np.linspace(-5, 5, 50)
        y = 2 * x**2 - 3 * x + 1
        out = pp.savitzky_golay(y, window_length=7, polyorder=2)
        np.testing.assert_allclose(out, y, atol=1e-9)

    def test_reproduce_polinomio_grado2_batch(self):
        x = np.linspace(-5, 5, 50)
        y = 2 * x**2 - 3 * x + 1
        Y = np.vstack([y, y[::-1], 3 * y - 2])
        out = pp.smooth_profiles(Y, window_length=7, polyorder=2)
        np.testing.assert_allclose(out, Y, atol=1e-9)

    def test_reduce_ruido(self):
        rng = np.random.default_rng(0)
        x = np.linspace(-5, 5, 100)
        clean = np.sin(x)
        noisy = clean + rng.normal(0, 0.3, size=x.shape)
        smoothed = pp.savitzky_golay(noisy, window_length=11, polyorder=2)
        self.assertLess(
            np.std(smoothed - clean), np.std(noisy - clean),
            "el suavizado debe acercar la señal a la versión limpia",
        )

    def test_ventana_par_se_ajusta_a_impar(self):
        y = np.arange(20, dtype=float)
        # window par (10) no debe romper; se corrige internamente a 11.
        out = pp.savitzky_golay(y, window_length=10, polyorder=2)
        self.assertEqual(out.shape, y.shape)


class TestCorreccionBlancoNegro(unittest.TestCase):
    def test_modo_completo_extremos(self):
        # Ec. 1: P=W -> 1, P=D -> 0, punto medio -> 0.5
        W = np.full(288, 100.0)
        D = np.full(288, 5.0)
        P = np.vstack([W, D, (W + D) / 2])
        c = pp.white_dark_correction(P, white=W, dark=D)
        self.assertAlmostEqual(c[0, 0], 1.0)
        self.assertAlmostEqual(c[1, 0], 0.0)
        self.assertAlmostEqual(c[2, 0], 0.5)

    def test_modo_adaptado_en_rango_0_1(self):
        rng = np.random.default_rng(1)
        profiles = rng.uniform(10, 500, size=(20, 288))
        c = pp.white_dark_correction(profiles)  # sin W/D
        self.assertTrue(np.all(c >= 0.0) and np.all(c <= 1.0))

    def test_denominador_cero_no_explota(self):
        # W == D en algún canal: no debe producir inf/nan.
        W = np.full(288, 50.0)
        D = np.full(288, 50.0)
        c = pp.white_dark_correction(np.full((1, 288), 50.0), white=W, dark=D)
        self.assertTrue(np.all(np.isfinite(c)))


class TestOutliers(unittest.TestCase):
    def test_elimina_perfil_atipico_y_conserva_normales(self):
        rng = np.random.default_rng(2)
        base = rng.normal(0, 0.01, size=(200, 30)) + 1.0
        atipico = np.full((1, 30), 10.0)  # perfil claramente fuera de rango
        data = np.vstack([base, atipico])
        clean, keep = pp.remove_outlier_profiles(data, k=1.0, return_mask=True)
        self.assertFalse(keep[-1], "el perfil atípico debe descartarse")
        self.assertLess(clean.shape[0], data.shape[0])

    def test_k_mayor_conserva_mas(self):
        rng = np.random.default_rng(3)
        data = rng.normal(0, 1, size=(300, 20))
        n1 = pp.remove_outlier_profiles(data, k=1.0).shape[0]
        n3 = pp.remove_outlier_profiles(data, k=3.0).shape[0]
        self.assertGreaterEqual(n3, n1)


class TestBartlettKMO(unittest.TestCase):
    def test_bartlett_datos_correlacionados_rechaza_h0(self):
        rng = np.random.default_rng(4)
        base = rng.normal(size=(500, 1))
        X = base + rng.normal(scale=0.5, size=(500, 6))  # 6 vars correlacionadas
        res = pp.bartlett_sphericity(X)
        self.assertLess(res.p_value, 0.05)

    def test_chi2_sf_extremos(self):
        # Función de supervivencia: P(X>0)=1 y decreciente.
        self.assertAlmostEqual(pp._chi2_sf(0.0, 5), 1.0)
        self.assertGreater(pp._chi2_sf(1.0, 5), pp._chi2_sf(20.0, 5))
        # Chequeo numérico contra un valor conocido: chi2(dof=1) en x=3.841 ~ 0.05
        self.assertAlmostEqual(pp._chi2_sf(3.8415, 1), 0.05, places=2)

    def test_kmo_en_rango_0_1(self):
        rng = np.random.default_rng(5)
        base = rng.normal(size=(400, 1))
        X = base + rng.normal(scale=0.5, size=(400, 8))
        _, kmo_total = pp.kmo(X)
        self.assertTrue(0.0 <= kmo_total <= 1.0)


class TestPCA(unittest.TestCase):
    def test_rank1_concentra_varianza_en_pc1(self):
        rng = np.random.default_rng(6)
        base = rng.normal(size=(500, 1))
        X = base @ np.ones((1, 6)) + rng.normal(scale=0.01, size=(500, 6))
        res = pp.pca(X, n_components=2)
        self.assertGreater(res.explained_variance_ratio[0], 0.99)

    def test_ratios_suman_a_lo_sumo_uno_y_shapes(self):
        rng = np.random.default_rng(7)
        X = rng.normal(size=(100, 12))
        res = pp.pca(X, n_components=3)
        self.assertEqual(res.scores.shape, (100, 3))
        self.assertEqual(res.components.shape, (3, 12))
        self.assertLessEqual(res.cumulative_variance, 1.0 + 1e-9)


class TestPipelineComponible(unittest.TestCase):
    def test_pipeline_equivale_a_funcional(self):
        rng = np.random.default_rng(8)
        profiles = rng.uniform(10, 500, size=(300, 288))

        # Vía objeto Pipeline (pasos hasta antes de PCA, que cambia dimensión).
        pipe = pp.Pipeline([
            pp.WhiteDarkCorrection(),
            pp.SavitzkyGolay(window=11, polyorder=2),
            pp.OutlierRemoval(k=1.5),
        ])
        clean_pipe = pipe.run(profiles)

        # Vía funciones sueltas.
        corr = pp.white_dark_correction(profiles)
        sm = pp.smooth_profiles(corr, 11, 2)
        clean_fn = pp.remove_outlier_profiles(sm, k=1.5)

        np.testing.assert_allclose(clean_pipe, clean_fn)

    def test_pipeline_record_devuelve_historial(self):
        rng = np.random.default_rng(9)
        profiles = rng.uniform(10, 500, size=(120, 288))
        pipe = pp.Pipeline([pp.WhiteDarkCorrection(), pp.SavitzkyGolay()])
        out, history = pipe.run(profiles, record=True)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0][0], "correccion_blanco_negro")
        self.assertEqual(history[1][0], "savitzky_golay")

    def test_preprocess_pipeline_reporte(self):
        rng = np.random.default_rng(10)
        # Datos correlacionados para que sobrevivan perfiles al filtro.
        base = rng.normal(size=(400, 1))
        profiles = np.abs(base @ np.ones((1, 288)) +
                          rng.normal(scale=0.02, size=(400, 288)))
        cfg = pp.PreprocessConfig(outlier_k=2.0, n_components=2)
        res = pp.preprocess_pipeline(profiles, config=cfg)
        self.assertEqual(res.n_input, 400)
        self.assertLessEqual(res.n_kept, res.n_input)
        self.assertIn("PCA", res.report())


class TestConfig(unittest.TestCase):
    def test_defaults_coinciden_con_el_paper(self):
        cfg = pp.PreprocessConfig()
        self.assertEqual(cfg.sg_polyorder, 2)   # Savitzky-Golay 2do grado
        self.assertEqual(cfg.outlier_k, 1.0)    # mediana ± 1σ
        self.assertEqual(cfg.n_components, 2)   # 2 componentes principales

    def test_config_propaga_n_components(self):
        rng = np.random.default_rng(11)
        base = rng.normal(size=(300, 1))
        profiles = np.abs(base @ np.ones((1, 288)) +
                          rng.normal(scale=0.02, size=(300, 288)))
        cfg = pp.PreprocessConfig(outlier_k=3.0, n_components=3)
        res = pp.preprocess_pipeline(profiles, config=cfg)
        self.assertEqual(res.pca.scores.shape[1], 3)

    def test_build_default_pipeline_usa_config(self):
        cfg = pp.PreprocessConfig(sg_window=9, outlier_k=1.5)
        pipe = pp.build_default_pipeline(cfg)
        self.assertEqual(pipe.steps[1].window, 9)
        self.assertEqual(pipe.steps[2].k, 1.5)


if __name__ == "__main__":
    unittest.main()
