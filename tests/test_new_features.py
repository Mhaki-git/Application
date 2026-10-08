"""
Tests des fonctionnalites : contrat client variable (marche), profils
integres horaires -> quart-horaires, import CSV avec choix de colonnes et
injection, marge batterie de la fiche financiere.
"""
import io
import os

import numpy as np
import pandas as pd
import pytest

from engine.financial_sheet import compute_fiche, DEFAULT_PARAMS
from engine.fournisseur_roi import run_fournisseur_model_from_data
from io_excel.dataextraction import (
    build_timeseries_from_sources, guess_csv_columns, list_csv_columns)
from io_sources.extract_conso_profiles_from_excel import hourly_values_to_quarter_hour_profile

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


# --- Profils integres --------------------------------------------------------

@pytest.mark.parametrize("n_hours,year", [(8760, 2024), (8760, 2025), (8784, 2024)])
def test_hourly_profile_covers_full_year(n_hours, year):
    hourly = np.arange(1, n_hours + 1, dtype=float)
    qh = hourly_values_to_quarter_hour_profile(hourly, year)
    expected_len = 366 * 96 if year == 2024 else 365 * 96
    assert len(qh) == expected_len
    assert qh.sum() == pytest.approx(1.0)
    assert (qh > 0).all()  # plus de zeros apres le 2 avril
    # les 4 quarts d'une meme heure sont egaux
    assert qh.iloc[0] == qh.iloc[3]


def test_shipped_profiles_are_full_year():
    profiles = pd.read_pickle(os.path.join(DATA_DIR, "conso_profiles.pkl"))
    for key, s in profiles.items():
        assert len(s) >= 365 * 96, key
        assert s.sum() == pytest.approx(1.0, abs=1e-6), key
        by_month = s.groupby(s.index.month).sum()
        assert (by_month > 0.03).all(), key  # aucun mois quasi vide


# --- Import CSV : colonnes + injection ---------------------------------------

def _csv_bytes(sep=";", decimal=","):
    idx = pd.date_range("2024-01-01", "2024-12-31 23:45", freq="15min")
    df = pd.DataFrame({
        "Horodatage": idx.strftime("%d/%m/%Y %H:%M"),
        "Prelevement (kW)": np.full(len(idx), 4.0),
        "Injection (kW)": np.full(len(idx), 2.0),
        "Autre": np.zeros(len(idx)),
    })
    return df.to_csv(sep=sep, index=False, decimal=decimal).encode("utf-8")


def test_guess_columns_finds_injection():
    cols = list_csv_columns(io.BytesIO(_csv_bytes()))
    g = guess_csv_columns(cols)
    assert g == {"timestamp": "Horodatage", "conso": "Prelevement (kW)", "injection": "Injection (kW)"}


def test_injection_column_is_read_and_converted():
    pv = pd.read_pickle(os.path.join(DATA_DIR, "pv_profile_1kwc.pkl"))
    df = build_timeseries_from_sources(
        io.BytesIO(_csv_bytes()), pv, kwc=10, unit="kW",
        timestamp_col="Horodatage", value_col="Prelevement (kW)", injection_col="Injection (kW)")
    n = len(df)
    assert df["conso_kwh"].sum() == pytest.approx(4.0 * 0.25 * n)
    assert df["injection_mesuree_kwh"].sum() == pytest.approx(2.0 * 0.25 * n)


def test_no_injection_column_when_not_chosen():
    pv = pd.read_pickle(os.path.join(DATA_DIR, "pv_profile_1kwc.pkl"))
    df = build_timeseries_from_sources(io.BytesIO(_csv_bytes()), pv, kwc=10, unit="kW")
    assert "injection_mesuree_kwh" not in df.columns


def test_explicit_column_choice_overrides_guess():
    pv = pd.read_pickle(os.path.join(DATA_DIR, "pv_profile_1kwc.pkl"))
    df = build_timeseries_from_sources(
        io.BytesIO(_csv_bytes()), pv, kwc=10, unit="kW",
        timestamp_col="Horodatage", value_col="Injection (kW)")
    assert df["conso_kwh"].sum() == pytest.approx(2.0 * 0.25 * len(df))


# --- Contrat client variable --------------------------------------------------

def _run(base_params, ts, **over):
    params = dict(base_params, mode_vente="fournisseur_principal", **over)
    return run_fournisseur_model_from_data(
        params=params, df=ts, dayahead_pkl_path="__no_file__", horizon_annees=2, discount_rate=0.06)


def test_fixed_contract_unchanged_by_new_params(base_params, synthetic_timeseries):
    a = _run(base_params, synthetic_timeseries)
    b = _run(base_params, synthetic_timeseries, contrat_client="fixe", marge_contrat_variable=0.5)
    assert a["revenue_client_year1"] == pytest.approx(b["revenue_client_year1"])


def test_variable_contract_bills_market_price_plus_margin(base_params, synthetic_timeseries):
    res = _run(base_params, synthetic_timeseries, contrat_client="variable", marge_contrat_variable=0.02)
    mp = res["serie_qh_annee1"]["belpex_eur_kwh"].values
    conso = res["serie_qh_annee1"]["conso_kwh"].values
    expected = float((conso * (mp + 0.02 + base_params["taxes_couts_proportionnels"])).sum())
    assert res["revenue_client_year1"] == pytest.approx(expected, rel=1e-9)
    # prix moyen expose pour l'affichage / le PDF
    assert res["params"]["prix_vente_kwh"] == pytest.approx(expected / conso.sum())


def test_variable_contract_margin_raises_revenue_and_gain(base_params, synthetic_timeseries):
    lo = _run(base_params, synthetic_timeseries, contrat_client="variable", marge_contrat_variable=0.0)
    hi = _run(base_params, synthetic_timeseries, contrat_client="variable", marge_contrat_variable=0.05)
    assert hi["revenue_client_year1"] > lo["revenue_client_year1"]
    assert hi["total_gain_brut"] > lo["total_gain_brut"]
    assert hi["total_economie_client"] < lo["total_economie_client"]


def test_variable_contract_ignored_outside_principal_mode(base_params, synthetic_timeseries):
    params = dict(base_params, mode_vente="fournisseur_secondaire", contrat_client="variable",
                  marge_contrat_variable=0.5)
    ref = dict(base_params, mode_vente="fournisseur_secondaire")
    kw = dict(df=synthetic_timeseries, dayahead_pkl_path="__no_file__", horizon_annees=2, discount_rate=0.06)
    assert (run_fournisseur_model_from_data(params=params, **kw)["revenue_client_year1"]
            == pytest.approx(run_fournisseur_model_from_data(params=ref, **kw)["revenue_client_year1"]))


# --- Marge batterie de la fiche financiere -------------------------------------

def test_fiche_battery_margin_scales_bess_cost_only():
    base = compute_fiche(dict(DEFAULT_PARAMS, puissance_batterie_kva=100.0))
    marked = compute_fiche(dict(DEFAULT_PARAMS, puissance_batterie_kva=100.0, marge_batterie=1.5))
    assert marked["capex_bess"] == pytest.approx(base["capex_bess"] * 1.5)
    assert marked["capex_pv"] == pytest.approx(base["capex_pv"])


# --- Import Excel (conso / prix) ------------------------------------------------

def _xlsx_bytes(rows, header=None):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([None, None])  # ligne vide en tete, comme dans un export reel
    if header:
        ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_dayahead_excel_without_header_converts_mwh_to_kwh():
    from io_excel.dataextraction import read_dayahead_csv
    idx = pd.date_range("2025-01-01 00:15", periods=200, freq="15min")
    raw = _xlsx_bytes([[t.strftime("%d/%m/%Y %H:%M"), 65.0 + i % 3] for i, t in enumerate(idx)])
    s = read_dayahead_csv(io.BytesIO(raw))  # unit="auto"
    assert len(s) == 200
    assert s.median() == pytest.approx(0.066, abs=0.002)


def test_dayahead_explicit_units():
    from io_excel.dataextraction import read_dayahead_csv
    idx = pd.date_range("2025-01-01", periods=50, freq="15min")
    csv = pd.DataFrame({"timestamp": idx, "prix": 0.08}).to_csv(index=False).encode()
    assert read_dayahead_csv(io.BytesIO(csv), unit="eur_kwh").median() == pytest.approx(0.08)
    assert read_dayahead_csv(io.BytesIO(csv), unit="eur_mwh").median() == pytest.approx(0.00008)


def test_conso_excel_with_header_and_injection():
    idx = pd.date_range("2024-01-01", "2024-12-31 23:45", freq="15min")
    raw = _xlsx_bytes([[t.to_pydatetime(), 4.0, 1.0] for t in idx],
                      header=["Date", "Prelevement kW", "Injection kW"])
    cols = list_csv_columns(io.BytesIO(raw))
    assert cols == ["Date", "Prelevement kW", "Injection kW"]
    g = guess_csv_columns(cols)
    pv = pd.read_pickle(os.path.join(DATA_DIR, "pv_profile_1kwc.pkl"))
    df = build_timeseries_from_sources(io.BytesIO(raw), pv, kwc=5, unit="kW",
                                       timestamp_col=g["timestamp"], value_col=g["conso"],
                                       injection_col=g["injection"])
    assert df["conso_kwh"].sum() == pytest.approx(4.0 * 0.25 * len(df))
    assert df["injection_mesuree_kwh"].sum() == pytest.approx(1.0 * 0.25 * len(df))
