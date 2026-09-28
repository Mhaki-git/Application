"""
Tests unitaires pour les fonctions de calage/completion calendaire ajoutees
recemment (alignement Belpex sur les vraies dates client, completion de
releves de consommation incomplets, mise a l'echelle des profils integres,
regle du tarif capacitaire). Aucun de ces tests n'existait avant -- ce trou
de couverture a laisse passer un bug bloquant (source Day-Ahead horaire mal
geree par le calage calendaire, corrige dans la meme session), d'ou ces tests
de non-regression cibles.
"""
import numpy as np
import pandas as pd
import pytest

from engine.fournisseur_roi import (
    _align_market_price_to_calendar,
    demand_charge_rate_for_mode,
    load_dayahead_prices,
)
from io_excel.dataextraction import (
    conso_from_normalized_profile,
    fill_missing_periods_by_calendar_symmetry,
    read_dayahead_csv,
)


# ---------------------------------------------------------------------
# _align_market_price_to_calendar
# ---------------------------------------------------------------------

def test_align_hourly_source_ffills_within_the_hour():
    """
    Cas reel de fetch_belpex.py : la source Day-Ahead est HORAIRE (malgre le
    suffixe "_qh" du nom de fichier data/belpex_<annee>_qh.pkl). Chaque quart
    d'heure d'une meme heure doit recevoir le meme prix horaire -- pas une
    moyenne annuelle plate (bug corrige : le calage par minute exacte ratait
    les cibles a :15/:30/:45 quand la source n'a que des minute=0).
    """
    idx_src = pd.date_range("2024-01-01", "2024-12-31 23:00", freq="h")
    market_price = pd.Series(np.arange(len(idx_src), dtype=float), index=idx_src)

    target_index = pd.date_range("2024-01-01", periods=8, freq="15min")
    out = _align_market_price_to_calendar(market_price, target_index)

    assert list(out.values) == [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]


def test_align_quarter_hourly_source_still_works():
    """Source deja quart-horaire (cas des .pkl actuellement sur disque) : le calage doit rester exact."""
    idx_src = pd.date_range("2024-01-01", "2024-12-31 23:45", freq="15min")
    market_price = pd.Series(np.arange(len(idx_src), dtype=float), index=idx_src)

    target_index = pd.date_range("2024-06-15 12:00", periods=4, freq="15min")
    out = _align_market_price_to_calendar(market_price, target_index)

    # Les 4 valeurs d'une meme heure source sont moyennees puis repetees
    # (voir docstring de la fonction) -- donc constantes sur l'heure.
    assert out.iloc[0] == out.iloc[1] == out.iloc[2] == out.iloc[3]


def test_align_cross_year_matches_month_day_hour():
    """Client en 2025, source Belpex en 2024 : le calage doit ignorer l'annee et matcher (mois, jour, heure)."""
    idx_src = pd.date_range("2024-01-01", "2024-12-31 23:45", freq="15min")
    rng = np.random.default_rng(0)
    market_price = pd.Series(rng.uniform(0.05, 0.15, len(idx_src)), index=idx_src)

    target_index = pd.date_range("2025-03-02 12:00", periods=1, freq="15min")
    out = _align_market_price_to_calendar(market_price, target_index)

    expected = market_price[(market_price.index.month == 3) & (market_price.index.day == 2)
                             & (market_price.index.hour == 12)].mean()
    assert out.iloc[0] == pytest.approx(expected)


def test_align_feb_29_falls_back_to_feb_28():
    """Cible bissextile (29 fevrier), source non bissextile : repli sur le 28 fevrier."""
    idx_src = pd.date_range("2025-01-01", "2025-12-31 23:45", freq="15min")  # 2025 non bissextile
    rng = np.random.default_rng(1)
    market_price = pd.Series(rng.uniform(0.05, 0.15, len(idx_src)), index=idx_src)

    target_index = pd.date_range("2028-02-29 12:00", periods=1, freq="15min")  # 2028 bissextile
    out = _align_market_price_to_calendar(market_price, target_index)

    expected = market_price[(market_price.index.month == 2) & (market_price.index.day == 28)
                             & (market_price.index.hour == 12)].mean()
    assert out.iloc[0] == pytest.approx(expected)


def test_align_limited_slice_falls_back_to_nearest_available_day():
    """
    Source limitee a une tranche (1er janvier -> 15 aout, cas du reglage
    "limiter a une tranche" de l'UI) : un jour hors tranche (20 decembre)
    doit retomber sur le jour disponible le plus proche en distance
    calendaire cyclique (ici le 1er janvier, ~11 jours, plutot que le 15
    aout, ~127 jours), pas sur une moyenne plate.
    """
    idx_src = pd.date_range("2024-01-01", "2024-08-15 23:45", freq="15min")
    values = np.linspace(0.05, 0.20, len(idx_src))
    market_price = pd.Series(values, index=idx_src)

    target_index = pd.date_range("2025-12-20 12:00", periods=1, freq="15min")
    out = _align_market_price_to_calendar(market_price, target_index)

    expected = market_price[(market_price.index.month == 1) & (market_price.index.day == 1)
                             & (market_price.index.hour == 12)].mean()
    assert out.iloc[0] == pytest.approx(expected)


def test_load_dayahead_prices_accepts_a_series_directly():
    """
    Upload utilisateur : app.py lit le CSV en pd.Series (read_dayahead_csv)
    et la passe directement a load_dayahead_prices, SANS jamais l'ecrire sur
    disque en .pkl (pd.read_pickle sur un fichier tiers serait une
    deserialisation non fiable -- risque d'execution de code arbitraire).
    Verifie que load_dayahead_prices accepte bien ce chemin (pas de crash sur
    l'evaluation booleenne d'une Series, bug corrige dans la meme session).
    """
    idx_src = pd.date_range("2024-01-01", "2024-01-31 23:45", freq="15min")
    market_price = pd.Series(np.full(len(idx_src), 0.08), index=idx_src)

    target_index = pd.date_range("2025-01-15 12:00", periods=4, freq="15min")
    prices, is_demo = load_dayahead_prices(market_price, target_index)

    assert is_demo is False
    assert (prices == 0.08).all()


def test_read_dayahead_csv_then_load_dayahead_prices_end_to_end():
    """Chemin complet upload CSV -> Series -> calage calendaire, comme app.py."""
    import io

    csv_text = "timestamp,prix_eur_kwh\n" + "\n".join(
        f'{ts.strftime("%d/%m/%Y %H:%M")},{0.05 + 0.01 * (ts.hour % 4)}'
        for ts in pd.date_range("2024-01-01", "2024-01-31 23:45", freq="15min")
    )
    buf = io.BytesIO(csv_text.encode("utf-8"))

    series = read_dayahead_csv(buf)
    assert isinstance(series, pd.Series)
    assert len(series) > 0

    target_index = pd.date_range("2025-01-15 12:00", periods=1, freq="15min")
    prices, is_demo = load_dayahead_prices(series, target_index)
    assert is_demo is False
    assert prices.iloc[0] == pytest.approx(0.05 + 0.01 * (12 % 4))


# ---------------------------------------------------------------------
# fill_missing_periods_by_calendar_symmetry
# ---------------------------------------------------------------------

def test_fill_extends_short_reading_to_full_calendar_year():
    """Un releve de 9 mois doit etre etendu a une annee civile complete (35040 points en 2025)."""
    idx = pd.date_range("2025-01-01 00:00", "2025-09-30 23:45", freq="15min")
    rng = np.random.default_rng(2)
    s = pd.Series(rng.uniform(1, 10, len(idx)), index=idx, name="conso_kwh")

    filled, n_truncated = fill_missing_periods_by_calendar_symmetry(s)

    assert n_truncated == 0
    assert len(filled) == len(pd.date_range("2025-01-01", "2025-12-31 23:45", freq="15min"))
    assert filled.index[0] == pd.Timestamp("2025-01-01 00:00")
    assert filled.index[-1] == pd.Timestamp("2025-12-31 23:45")


def test_fill_internal_gap_uses_nearest_calendar_day():
    """Trou interne (mi-octobre -> debut novembre) : complete par un jour proche, pas une valeur aberrante."""
    idx = pd.date_range("2025-01-01 00:00", "2025-12-31 23:45", freq="15min")
    doy = idx.dayofyear.values.astype(float)
    vals = 10 + 5 * np.sin(2 * np.pi * doy / 365)
    s = pd.Series(vals, index=idx, name="conso_kwh")

    mask = ~((s.index >= "2025-10-20") & (s.index < "2025-11-05"))
    s_holed = s[mask]

    filled, n_truncated = fill_missing_periods_by_calendar_symmetry(s_holed)

    assert n_truncated == 0
    assert len(filled) == len(idx)
    # La valeur completee doit rester proche (meme saison) de la vraie valeur du 28 octobre.
    real_val = s["2025-10-28 12:00"]
    filled_val = filled["2025-10-28 12:00"]
    assert abs(filled_val - real_val) / real_val < 0.15


def test_fill_truncates_readings_longer_than_one_year():
    """Un releve de 14 mois doit etre tronque a l'annee civile de sa premiere date."""
    idx = pd.date_range("2024-01-01 00:00", "2025-02-28 23:45", freq="15min")
    s = pd.Series(np.ones(len(idx)), index=idx, name="conso_kwh")

    filled, n_truncated = fill_missing_periods_by_calendar_symmetry(s)

    assert n_truncated > 0
    assert filled.index[-1].year == 2024
    assert len(filled) == len(pd.date_range("2024-01-01", "2024-12-31 23:45", freq="15min"))


def test_fill_handles_leap_year_source():
    """Releve couvrant une annee bissextile (2024, 366 jours) : la reconstruction doit rester coherente (35136 points)."""
    idx = pd.date_range("2024-01-01 00:00", "2024-12-31 23:45", freq="15min")
    s = pd.Series(np.ones(len(idx)), index=idx, name="conso_kwh")

    filled, n_truncated = fill_missing_periods_by_calendar_symmetry(s)

    assert n_truncated == 0
    assert len(filled) == len(idx)  # deja complet, rien a changer


def test_fill_short_series_returned_unchanged():
    """Moins de 2 points : rien a completer, retourne tel quel avec n_truncated=0."""
    idx = pd.date_range("2025-01-01", periods=1, freq="15min")
    s = pd.Series([5.0], index=idx, name="conso_kwh")

    filled, n_truncated = fill_missing_periods_by_calendar_symmetry(s)

    assert n_truncated == 0
    assert len(filled) == 1


# ---------------------------------------------------------------------
# conso_from_normalized_profile
# ---------------------------------------------------------------------

def test_conso_from_normalized_profile_scales_to_target():
    idx = pd.date_range("2024-01-01", "2024-12-31 23:45", freq="15min")
    profile = pd.Series(np.full(len(idx), 1.0 / len(idx)), index=idx)  # uniforme, somme = 1

    result = conso_from_normalized_profile(profile, 12000.0)

    assert result.sum() == pytest.approx(12000.0)
    assert result.name == "conso_kwh"


def test_conso_from_normalized_profile_rejects_negative_target():
    idx = pd.date_range("2024-01-01", periods=10, freq="15min")
    profile = pd.Series(np.full(10, 0.1), index=idx)

    with pytest.raises(ValueError):
        conso_from_normalized_profile(profile, -100.0)


def test_conso_from_normalized_profile_rejects_empty_profile():
    empty_profile = pd.Series([], dtype=float)

    with pytest.raises(ValueError):
        conso_from_normalized_profile(empty_profile, 1000.0)


# ---------------------------------------------------------------------
# demand_charge_rate_for_mode
# ---------------------------------------------------------------------

@pytest.mark.parametrize("mode_vente,expected", [
    ("fournisseur_principal", 3.5),
    ("vente_directe", 3.5),
    ("fournisseur_secondaire", 0.0),
])
def test_demand_charge_rate_for_mode(mode_vente, expected):
    assert demand_charge_rate_for_mode(mode_vente, 3.5) == expected
