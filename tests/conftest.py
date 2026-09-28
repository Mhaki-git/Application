"""
Fixtures partagees : jeu de donnees synthetique deterministe pour les tests
de non-regression sur le moteur (engine/fournisseur_roi.py).
"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def synthetic_timeseries():
    """
    30 jours de donnees quart-horaires (conso + PV), generees avec une graine
    fixe -- meme forme que celle produite par
    dataextraction.build_timeseries_from_sources (colonnes conso_kwh, pv_kwh,
    hour, is_weekend), mais sans dependance a un fichier CSV/Excel/PVGIS reel.
    """
    rng = np.random.default_rng(0)
    idx = pd.date_range("2024-01-01", periods=96 * 30, freq="15min")

    hours = idx.hour.values
    # Profil de consommation avec un pic matin/soir, pour que le dispatch
    # (import/export/batterie) ait un comportement non trivial a verifier.
    conso_base = 0.8 + 0.6 * np.sin((hours - 8) / 24 * 2 * np.pi * 2) ** 2
    conso_kwh = np.clip(conso_base + rng.normal(0, 0.1, len(idx)), 0.05, None)

    # Profil PV en cloche centree sur midi, nul la nuit.
    pv_shape = np.clip(np.sin((hours - 6) / 12 * np.pi), 0, None)
    pv_kwh = pv_shape * (1.0 + rng.normal(0, 0.03, len(idx)))
    pv_kwh = np.clip(pv_kwh, 0, None)

    df = pd.DataFrame({"conso_kwh": conso_kwh, "pv_kwh": pv_kwh}, index=idx)
    df.index.name = "datetime"
    df["hour"] = df.index.hour
    df["is_weekend"] = df.index.dayofweek >= 5
    return df


@pytest.fixture
def base_params():
    """Parametres de simulation representatifs, un jeu par mode de vente est derive de celui-ci."""
    return {
        "kwc": 50.0,
        "kva_onduleur": 33.0,
        "battery_power_kw": 20.0,
        "battery_capacity_kwh": 40.0,
        "contrat_kw": 100.0,
        "old_price_kwh_fixe": 0.16,
        "price_hp": 0.0,
        "price_hc": 0.0,
        "old_price_inj_fixe": 0.0,
        "heure_debut_hp": 7,
        "heure_debut_hc": 19,
        "old_cout_additionnel_contrat": 0.0,
        "prix_vente_kwh": 0.12,
        "prix_rachat_surplus_kwh": 0.03,
        "prix_injection_fournisseur": 0.00175,
        "marge_fournisseur": 0.01,
        "taxes_couts_proportionnels": 0.02,
        "marge_injection": 0.00175,
        "tarif_capacitaire_fournisseur": 3.0,
        "mode_vente": "fournisseur_principal",
        "prix_kwc_pv": 650.0,
        "prix_kwh_batterie": 225.0,
        "maintenance_eur_an": 0.0,
        "inflation_pct": 3.0,
        "degradation_pv_pct": 0.5,
        "degradation_batterie_pct": 2.0,
    }
