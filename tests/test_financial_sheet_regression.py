"""
Tests de non-regression sur engine.financial_sheet.compute_fiche.

Meme principe que test_fournisseur_roi_regression.py : fige le comportement
actuel de compute_fiche(DEFAULT_PARAMS) pour detecter tout changement
silencieux introduit par un refactor (ex: extraction de finance_utils.py).
"""
import pytest

from engine.financial_sheet import compute_fiche, DEFAULT_PARAMS, build_fiche_pdf

EXPECTED = {
    "capex_pv": 47375.0,
    "capex_bess": 0.0,
    "capex_total": 47376.04,
    "opex_total": 10500.0,
    "production_pv_estimee": 67500.0,
    "production_totale_estimee": 67500.0,
    "apport_eur": 0.0,
    "montant_financement": 47376.04,
    "annuite": 8693.3948,
    "total_interets": 4784.329,
    "prix_revient_kwh": 0.046415,
    "marge_brute_pct": 244.7155,
    "gain_total_25ans_eur": 146762.451,
    "payback_annees": 0.0,
    "rendement_brut_pct": None,
    "irr_pct": None,
}


def test_compute_fiche_matches_reference():
    fiche = compute_fiche(DEFAULT_PARAMS)

    assert len(fiche["detail_annuel"]) == 25
    for key, expected_value in EXPECTED.items():
        actual = fiche[key]
        if expected_value is None:
            assert actual is None
        else:
            tol = 1e-5 if key == "prix_revient_kwh" else 1e-3
            assert actual == pytest.approx(expected_value, abs=tol)


def test_build_fiche_pdf_runs(tmp_path):
    fiche = compute_fiche(DEFAULT_PARAMS)
    out = tmp_path / "fiche.pdf"
    build_fiche_pdf(fiche, str(out), client_name="Test", site_name="Site")
    assert out.exists()
    assert out.stat().st_size > 0
