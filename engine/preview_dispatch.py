"""
Dispatch batterie SIMPLIFIE, utilise uniquement pour l'apercu instantane de
l'UI (avant de lancer la simulation complete). Deplace depuis app.py pour
que toute logique de calcul energetique reste dans engine/, testable
independamment de Streamlit -- son comportement n'a pas change lors du
deplacement.

Volontairement plus simple que le vrai dispatch (solve_day_dispatch /
solve_day_dispatch_heuristic dans fournisseur_roi.py) : pas de contrat_kw
(import/export non plafonnes), pas de rendement de charge/decharge (eta_c/
eta_d = 1 implicite), pas de decoupage jour par jour avec suivi mensuel du
pic de puissance. Ces simplifications sont assumees : ce module sert
seulement a donner un ordre de grandeur reactif dans l'apercu, la simulation
complete (run_fournisseur_model_from_data) reste la seule source fiable
pour le ROI/gain reel.
"""
import numpy as np

DT_HOURS = 0.25  # kWh par pas de temps pour 1 kW sur un quart d'heure


def simulate_battery_selfconso(pv_kwh: np.ndarray, conso_kwh: np.ndarray,
                                battery_power_kw: float, battery_capacity_kwh: float):
    """
    Dispatch glouton (maximisation autoconsommation instantanee, PAS d'arbitrage
    Belpex/day-ahead) : a chaque quart d'heure, le surplus PV charge la batterie
    (dans la limite de sa puissance et de sa capacite), le deficit est couvert
    par la batterie si elle a de l'energie disponible.
    """
    # Energie maximale echangeable avec la batterie sur UN pas de temps (kWh),
    # convertie depuis la puissance nominale (kW) via DT_HOURS = 0.25 h.
    p_max_kwh = battery_power_kw * DT_HOURS
    soc = 0.0  # etat de charge courant de la batterie, en kWh (0 au demarrage)
    n = len(pv_kwh)
    import_kwh = np.empty(n)
    export_kwh = np.empty(n)
    for i in range(n):
        surplus = pv_kwh[i] - conso_kwh[i]
        if surplus >= 0:
            # Production excedentaire : on charge la batterie en priorite,
            # dans la limite de sa puissance et de la place disponible ;
            # le reste (si la batterie est pleine ou trop lente) part au reseau.
            charge = min(surplus, p_max_kwh, battery_capacity_kwh - soc)
            soc += charge
            export_kwh[i] = surplus - charge
            import_kwh[i] = 0.0
        else:
            # Consommation superieure a la production : la batterie comble le
            # deficit si elle a de l'energie stockee, le reste vient du reseau.
            deficit = -surplus
            decharge = min(deficit, p_max_kwh, soc)
            soc -= decharge
            import_kwh[i] = deficit - decharge
            export_kwh[i] = 0.0
    return import_kwh, export_kwh
