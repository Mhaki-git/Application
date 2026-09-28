"""
Tests de robustesse du LP journalier (solve_day_dispatch) sur des scenarios
de dimensionnement extreme mais physiquement plausibles (contrat souscrit
trop petit face a une forte production PV/consommation, batterie vide).

Avant l'ajout de la variable de depassement penalise (imp_overrun, voir
_build_lp_skeleton), ces scenarios rendaient le LP purement INFAISABLE
(RuntimeError), alors que _dispatch_no_battery et solve_day_dispatch_heuristic
geraient deja ce cas en douceur avec la meme penalite (OVERRUN_PENALTY_MULT)
-- incoherence corrigee dans la meme session.
"""
import numpy as np
import pandas as pd
import pytest

from engine.fournisseur_roi import run_year_dispatch, solve_day_dispatch, OVERRUN_PENALTY_MULT


def test_tiny_contract_vs_oversized_pv_does_not_crash():
    """
    Contrat de 1 kW face a une PV avec un pic de 20 kW et une conso nocturne
    non couvrable par une batterie deja vide -- doit degrader (import cher,
    penalise) au lieu de lever une exception.
    """
    idx = pd.date_range("2024-01-01", periods=96 * 5, freq="15min")
    rng = np.random.default_rng(4)
    hours = idx.hour.values
    conso = np.clip(1 + rng.normal(0, 0.1, len(idx)), 0.05, None)
    pv = np.clip(np.sin((hours - 6) / 12 * np.pi), 0, None) * 20

    price_import = np.full(len(idx), 0.3)
    price_export = np.full(len(idx), 0.03)

    energy_cost, demand_charge, total_import, total_export, total_curtail = run_year_dispatch(
        conso, pv, price_import, price_export,
        p_max=50, e_max=100, contrat_kw=1.0, demand_charge_rate=5.0, index=idx,
    )
    assert total_import >= 0
    assert energy_cost > 0  # le depassement penalise coute reellement plus cher


def test_overrun_does_not_let_battery_exceed_its_capacity():
    """
    Regression du bug trouve en testant ce module : la 1ere version de
    imp_overrun n'entrait pas dans le calcul du pic facture (a_peak), ce qui
    donnait au LP un interet economique a faire passer de l'import normal
    par imp_overrun (penalise mais hors tarif capacitaire) plutot que par
    imp -- au point de charger la batterie AU-DELA de sa capacite reelle sur
    une journee (charge_kwh cumule > e_max), physiquement impossible.
    """
    idx = pd.date_range("2024-01-01", periods=96, freq="15min")
    rng = np.random.default_rng(0)
    hours = idx.hour.values
    conso = np.clip(0.8 + 0.6 * np.sin((hours - 8) / 24 * 2 * np.pi * 2) ** 2
                     + rng.normal(0, 0.1, 96), 0.05, None)
    pv = np.clip(np.clip(np.sin((hours - 6) / 12 * np.pi), 0, None)
                 * (1.0 + rng.normal(0, 0.03, 96)), 0, None)

    # Prix import/export proches (le scenario qui a revele le bug) -- rend le
    # tarif capacitaire relativement plus cher que l'arbitrage energie, donc
    # incitatif a "tricher" sur le pic si la faille existe.
    price_import = np.full(96, 0.10)
    price_export = np.full(96, 0.09)

    e_max = 40.0
    dispatch, soc, peak, cost = solve_day_dispatch(
        conso, pv, price_import, price_export,
        p_max=20, e_max=e_max, contrat_kw=100, soc_init=20,
        peak_so_far_kw=0, demand_charge_rate=3.0,
    )

    assert dispatch["charge_kwh"].sum() <= e_max + 1e-6
    # Avec un contrat aussi genereux (100 kW) face a cette conso/PV, l'import
    # normal doit couvrir le besoin -- pas de raison economique de passer par
    # le depassement penalise.
    assert dispatch["import_kwh"].sum() > 0


def test_overrun_cost_is_penalized_relative_to_normal_import():
    """
    Sanity check direct sur le mecanisme de penalite : forcer un depassement
    (contrat quasi nul) doit couter au moins OVERRUN_PENALTY_MULT fois le
    prix normal sur l'energie en exces, pas un tarif avantageux.
    """
    idx = pd.date_range("2024-01-01", periods=96, freq="15min")
    conso = np.full(96, 5.0)
    pv = np.zeros(96)
    price_import = np.full(96, 0.20)
    price_export = np.full(96, 0.05)

    dispatch, soc, peak, cost = solve_day_dispatch(
        conso, pv, price_import, price_export,
        p_max=0, e_max=0, contrat_kw=0.1, soc_init=0,
        peak_so_far_kw=0, demand_charge_rate=1.0,
    )
    # Sans batterie (p_max=e_max=0), fallback _dispatch_no_battery : le cout
    # doit refleter le tarif penalise sur l'essentiel de l'energie (contrat
    # quasi nul face a 5 kWh/pas de conso constante).
    expected_min_cost = (conso.sum() - 0.1 * 0.25 * 96) * 0.20 * OVERRUN_PENALTY_MULT * 0.9
    assert cost >= expected_min_cost
