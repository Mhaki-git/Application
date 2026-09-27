"""
Tests de non-regression sur engine.fournisseur_roi.run_fournisseur_model_from_data.

Ces tests figent le comportement ACTUEL du moteur sur un jeu de donnees
synthetique deterministe (voir conftest.py), pour les trois modes de vente.
Ils ne pretendent pas que ces valeurs soient "correctes" au sens metier --
ils servent de filet de securite : si un refactor (renommage, extraction de
fonction, etc.) change silencieusement un resultat, un de ces tests echoue.

Si un changement de comportement est VOULU (correction d'un bug de calcul,
nouvelle regle metier), les valeurs de reference ci-dessous doivent etre
mises a jour consciemment, pas juste pour faire passer le test.
"""
import math

import pytest

from engine.fournisseur_roi import run_fournisseur_model_from_data

HORIZON_ANNEES = 3
DISCOUNT_RATE = 0.06


def _run(base_params, synthetic_timeseries, mode_vente):
    params = dict(base_params, mode_vente=mode_vente)
    return run_fournisseur_model_from_data(
        params=params,
        df=synthetic_timeseries,
        dayahead_pkl_path="__no_file__",
        horizon_annees=HORIZON_ANNEES,
        discount_rate=DISCOUNT_RATE,
    )


# mode -> valeurs de reference capturees sur le code actuel (voir docstring)
EXPECTED = {
    "fournisseur_principal": {
        "capex_total": 41500.0,
        "gain_vendeur_eur": 576.2959,
        "old_cost_year1": 525.4804,
        "revenue_client_year1": 379.0426,
        "payback_year": None,
        "roi_pct": -98.6113,
        "npv_eur": -40986.1588,
        "irr_pct": -82.3378,
        "total_gain_brut": 576.2959,
        "total_economie_client": 487.0795,
        "is_demo_prices": True,
        "pv_total_kwh_an1": 912.8095,
        "import_kwh_an1": 2676.6536,
        "export_kwh_an1": 358.9045,
    },
    "fournisseur_secondaire": {
        "capex_total": 41500.0,
        "gain_vendeur_eur": 326.9711,
        "old_cost_year1": 146.0495,
        "revenue_client_year1": 109.5371,
        "payback_year": None,
        "roi_pct": -99.2121,
        "npv_eur": -41208.6108,
        "irr_pct": -85.4987,
        "total_gain_brut": 326.9711,
        "total_economie_client": 122.1556,
        "is_demo_prices": False,
        "pv_total_kwh_an1": 912.8095,
        "import_kwh_an1": 2228.5714,
        "export_kwh_an1": 0.0,
    },
    "vente_directe": {
        "capex_total": 41500.0,
        "gain_vendeur_eur": 41500.0,
        "old_cost_year1": 525.4804,
        "revenue_client_year1": 505.3901,
        "payback_year": None,
        "roi_pct": -98.8975,
        "npv_eur": -41092.7203,
        "irr_pct": -83.5307,
        "total_gain_brut": 457.5338,
        "total_economie_client": 457.5338,
        "is_demo_prices": False,
        "pv_total_kwh_an1": 912.8095,
        "import_kwh_an1": 2228.5714,
        "export_kwh_an1": 0.0,
    },
}


def _assert_close(actual, expected, tol=1e-3):
    if expected is None:
        assert actual is None
        return
    if isinstance(expected, float) and math.isnan(expected):
        assert actual != actual  # NaN != NaN
        return
    assert actual == pytest.approx(expected, abs=tol)


@pytest.mark.parametrize("mode_vente", ["fournisseur_principal", "fournisseur_secondaire", "vente_directe"])
def test_engine_output_matches_reference(base_params, synthetic_timeseries, mode_vente):
    results = _run(base_params, synthetic_timeseries, mode_vente)
    expected = EXPECTED[mode_vente]

    er = results["bilan_energetique_annee1"]
    actual = {
        "capex_total": results["capex_total"],
        "gain_vendeur_eur": results["gain_vendeur_eur"],
        "old_cost_year1": results["old_cost_year1"],
        "revenue_client_year1": results["revenue_client_year1"],
        "payback_year": results["payback_year"],
        "roi_pct": results["roi_pct"],
        "npv_eur": results["npv_eur"],
        "irr_pct": results["irr_pct"],
        "total_gain_brut": results["total_gain_brut"],
        "total_economie_client": results["total_economie_client"],
        "is_demo_prices": results["is_demo_prices"],
        "pv_total_kwh_an1": er["pv_total_kwh"],
        "import_kwh_an1": er["import_kwh"],
        "export_kwh_an1": er["export_kwh"],
    }

    for key, expected_value in expected.items():
        _assert_close(actual[key], expected_value)


@pytest.mark.parametrize("mode_vente", ["fournisseur_principal", "fournisseur_secondaire", "vente_directe"])
def test_detail_annuel_has_expected_shape(base_params, synthetic_timeseries, mode_vente):
    results = _run(base_params, synthetic_timeseries, mode_vente)
    df = results["detail_annuel"]

    assert len(df) == HORIZON_ANNEES
    assert list(df["annee"]) == [1, 2, 3]
    expected_columns = {
        "annee", "revenu_client_eur", "cout_approvisionnement_eur",
        "gain_brut_fournisseur_eur", "economie_client_eur", "cashflow_cumule_eur",
    }
    assert expected_columns.issubset(df.columns)


def test_output_csv_not_written_by_default(base_params, synthetic_timeseries, tmp_path, monkeypatch):
    """Regression guard for the fix that stopped fournisseur_roi.py from
    unconditionally writing a CSV into the process's current working directory."""
    monkeypatch.chdir(tmp_path)
    _run(base_params, synthetic_timeseries, "vente_directe")
    assert not (tmp_path / "fournisseur_roi_resultats.csv").exists()
    assert not (tmp_path / "fournisseur_rapport_complet.txt").exists()


def test_output_csv_written_when_path_given(base_params, synthetic_timeseries, tmp_path):
    csv_path = tmp_path / "output.csv"
    params = dict(base_params, mode_vente="vente_directe")
    run_fournisseur_model_from_data(
        params=params,
        df=synthetic_timeseries,
        dayahead_pkl_path="__no_file__",
        horizon_annees=HORIZON_ANNEES,
        discount_rate=DISCOUNT_RATE,
        output_csv_path=str(csv_path),
    )
    assert csv_path.exists()
