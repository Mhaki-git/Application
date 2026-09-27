"""
MODELE ECONOMIQUE "FOURNISSEUR" : economies client + rentabilite (ROI).
(voir docstring complet dans le script d'origine fourni par l'utilisateur)
"""
import numpy as np
import pandas as pd
from scipy import sparse
from scipy.optimize import linprog

from io_excel.dataextraction import extract_all, XLSM_PATH as DEFAULT_XLSM_PATH
from engine.finance_utils import compute_npv_irr

import sys


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()


XLSM_PATH = DEFAULT_XLSM_PATH
DAYAHEAD_PKL_PATH = "dayahead_prices_2024_qh.pkl"

ETA_C = 0.95
ETA_D = 0.95
SOC_INIT_RATIO = 0.5
DT = 0.25
STEPS_PER_DAY = 96

HORIZON_ANNEES = 20
APPLIQUER_INFLATION_AU_PRIX_MARCHE = False

OVERRUN_PENALTY_MULT = 3.0
DISCOUNT_RATE_DEFAULT = 0.06


def _build_lp_skeleton(n: int):
    i_n = sparse.eye(n, format="csr")
    z_n = sparse.csr_matrix((n, n))
    s_n = sparse.eye(n, k=-1, format="csr")
    zeros_col = sparse.csr_matrix((n, 1))
    ones_col = sparse.csr_matrix(np.ones((n, 1)))

    a_balance = sparse.hstack([-i_n, i_n, i_n, -i_n, z_n, -i_n, zeros_col], format="csr")
    a_peak = sparse.hstack([z_n, z_n, i_n, z_n, z_n, z_n, -DT * ones_col], format="csr")

    return {
        "I_N": i_n, "Z_N": z_n, "S_N": s_n, "zeros_col": zeros_col,
        "A_balance": a_balance, "A_peak": a_peak,
    }


_LP_SKELETON = _build_lp_skeleton(STEPS_PER_DAY)


def load_dayahead_prices(dayahead_pkl_path, target_index: pd.DatetimeIndex) -> tuple:
    try:
        if not dayahead_pkl_path:
            raise FileNotFoundError(dayahead_pkl_path)
        market_price = pd.read_pickle(dayahead_pkl_path)
        if market_price.index.tz is not None:
            market_price = market_price.tz_localize(None)
        market_price = market_price[~market_price.index.duplicated(keep="first")]
        market_price = market_price.reindex(target_index, method="ffill")
        n_nan = int(market_price.isna().sum())
        if n_nan:
            print(f"  /!\\ {n_nan} prix Day-Ahead manquants (NaN) dans '{dayahead_pkl_path}' "
                  f"(trou dans les donnees source, ex. panne API un jour donne) -- "
                  f"combles par report de la derniere valeur connue (ffill), puis bfill "
                  f"si le trou est en tout debut de serie.")
            market_price = market_price.ffill().bfill()
        med = float(np.nanmedian(np.abs(market_price.values)))
        if med > 2.0:
            print(f"  /!\\ ATTENTION UNITES : la mediane des prix Day-Ahead chargee est "
                  f"{med:.1f} -- ca ressemble a des EUR/MWh, pas des EUR/kWh.")
        return market_price, False
    except FileNotFoundError:
        print(f"  /!\\ Fichier Day-Ahead '{dayahead_pkl_path}' introuvable -> MODE DEMO.")
        rng = np.random.default_rng(42)
        hours = target_index.hour.values
        base = 0.06 + 0.03 * np.sin((hours - 8) / 24 * 2 * np.pi * 2)
        noise = rng.normal(0, 0.015, size=len(target_index))
        demo_price = np.clip(base + noise, 0.01, None)
        return pd.Series(demo_price, index=target_index), True


def build_tariff_fournisseur(market_price_values: np.ndarray, params: dict, inflate_market: bool = False):
    marge_f = params["marge_fournisseur"]
    taxes = params["taxes_couts_proportionnels"]
    marge_inj = params["marge_injection"]
    price_import = market_price_values + marge_f + taxes
    price_export = np.maximum(market_price_values + marge_inj, 0.0)
    return price_import, price_export


def build_tariff_reference(params: dict, hour_all: np.ndarray):
    """
    Construit price_import/price_export SANS marche Day-Ahead (pas de Belpex),
    pour les modes "fournisseur secondaire" et "vente directe" : le prix
    d'achat reseau est soit fixe (old_price_kwh_fixe), soit Heures Pleines /
    Heures Creuses (price_hp/price_hc + heure_debut_hp/heure_debut_hc) --
    exactement les memes champs "communs" que ceux utilises pour l'ancien
    cout client (voir client_old_cost), reutilises ici comme reference de
    cout reseau pour piloter le dispatch batterie (charge aux heures creuses,
    decharge/ecretement aux heures pleines -- meme logique economique que
    l'arbitrage Belpex, mais sur un tarif reseau fixe au lieu du marche).

    Le prix de rachat du surplus injecte est un tarif fixe separe
    (prix_rachat_surplus_kwh), independant de la structure d'achat.
    """
    if params.get("price_hp") or params.get("price_hc"):
        is_hp = (hour_all >= params["heure_debut_hp"]) & (hour_all < params["heure_debut_hc"])
        price_import = np.where(is_hp, params["price_hp"], params["price_hc"]).astype(float)
    else:
        price_import = np.full(len(hour_all), params["old_price_kwh_fixe"], dtype=float)
    price_export = np.full(len(hour_all), params.get("prix_rachat_surplus_kwh", 0.0), dtype=float)
    return price_import, price_export


def _dispatch_no_battery(conso, pv, price_import, price_export,
                          contrat_kw, peak_so_far_kw, dt=DT):
    """
    Fallback partage (LP et heuristique) quand e_max<=0 ou p_max<=0 : pas de
    batterie, autoconsommation directe uniquement, import/export plafonnes
    au contrat, depassement penalise (OVERRUN_PENALTY_MULT).
    """
    N = len(conso)
    net = conso - pv
    imp_uncapped = np.maximum(net, 0)
    exp_uncapped = np.maximum(-net, 0)

    imp = np.minimum(imp_uncapped, contrat_kw * dt)
    imp_overrun = imp_uncapped - imp
    exp = np.minimum(exp_uncapped, contrat_kw * dt)
    curtail = exp_uncapped - exp

    overrun_cost = (imp_overrun * price_import * OVERRUN_PENALTY_MULT).sum()
    day_cost = float((imp * price_import - exp * price_export).sum() + overrun_cost)
    new_peak = max(peak_so_far_kw, (imp / dt).max() if N else 0.0)
    dispatch = pd.DataFrame({
        "charge_kwh": np.zeros(N), "decharge_kwh": np.zeros(N),
        "import_kwh": imp, "export_kwh": exp, "soc_kwh": np.zeros(N),
        "curtail_kwh": curtail,
    })
    return dispatch, 0.0, new_peak, day_cost


def solve_day_dispatch(conso, pv, price_import, price_export,
                        p_max, e_max, contrat_kw,
                        soc_init, peak_so_far_kw, demand_charge_rate,
                        eta_c=ETA_C, eta_d=ETA_D, dt=DT):
    N = len(conso)

    if contrat_kw <= 0:
        raise ValueError("contrat_kw (Excel N16) est nul ou absent.")

    if e_max <= 0 or p_max <= 0:
        return _dispatch_no_battery(conso, pv, price_import, price_export,
                                     contrat_kw, peak_so_far_kw, dt=dt)

    sk = _LP_SKELETON
    I_N, S_N, zeros_col = sk["I_N"], sk["S_N"], sk["zeros_col"]

    A_balance = sk["A_balance"]
    b_balance = conso - pv

    A_soc = sparse.hstack([-eta_c * I_N, (1 / eta_d) * I_N, sk["Z_N"], sk["Z_N"],
                            I_N - S_N, sk["Z_N"], zeros_col], format="csr")
    b_soc = np.zeros(N)
    b_soc[0] = soc_init

    A_eq = sparse.vstack([A_balance, A_soc], format="csr")
    b_eq = np.concatenate([b_balance, b_soc])

    A_peak = sk["A_peak"]
    b_peak = np.zeros(N)

    curtail_upper = np.maximum(pv, 0.0)

    bounds = (
        [(0, p_max * dt)] * N
        + [(0, p_max * dt)] * N
        + [(0, contrat_kw * dt)] * N
        + [(0, contrat_kw * dt)] * N
        + [(0, e_max)] * N
        + [(0, curtail_upper[t]) for t in range(N)]
        + [(peak_so_far_kw, None)]
    )

    c = np.zeros(6 * N + 1)
    c[2 * N:3 * N] = price_import
    c[3 * N:4 * N] = -price_export
    c[6 * N] = demand_charge_rate

    res = linprog(c, A_ub=A_peak, b_ub=b_peak, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if not res.success:
        raise RuntimeError(
            f"LP journalier n'a pas converge : {res.message}. "
            f"Verifie contrat_kw ({contrat_kw} kW), battery_power_kw ({p_max} kW) et "
            f"battery_capacity_kwh ({e_max} kWh) vs les pics reels de conso/PV.")

    x = res.x
    charge, decharge = x[0:N], x[N:2 * N]
    imp, exp, soc = x[2 * N:3 * N], x[3 * N:4 * N], x[4 * N:5 * N]
    curtail = x[5 * N:6 * N]
    new_peak = x[6 * N]
    day_cost = float((imp * price_import - exp * price_export).sum())
    dispatch = pd.DataFrame({
        "charge_kwh": charge, "decharge_kwh": decharge,
        "import_kwh": imp, "export_kwh": exp, "soc_kwh": soc,
        "curtail_kwh": curtail,
    })
    return dispatch, soc[-1], new_peak, day_cost


def solve_day_dispatch_heuristic(conso, pv, price_import, price_export,
                                  p_max, e_max, contrat_kw,
                                  soc_init, peak_so_far_kw, demand_charge_rate,
                                  eta_c=ETA_C, eta_d=ETA_D, dt=DT):
    """
    Dispatch HEURISTIQUE (pas de LP, pas d'arbitrage sur les prix) : reproduit
    le comportement d'un onduleur hybride standard en mode "self-consumption",
    ce que fait reellement une installation sans EMS pilote/API de marche :

      - surplus PV (pv > conso) -> charge la batterie en priorite, l'exces
        eventuel est exporte (plafonne au contrat), le reste est ecrete.
      - deficit (conso > pv) -> la batterie se decharge en priorite pour
        couvrir le manque, le reste est importe du reseau (plafonne au
        contrat, depassement penalise comme dans _dispatch_no_battery).

    Aucune decision n'est prise en fonction de price_import/price_export ni
    de demand_charge_rate : ces deux arguments ne servent qu'a calculer le
    cout a posteriori (meme convention de retour que solve_day_dispatch),
    pas a piloter la charge/decharge. Consequence a connaitre : contrairement
    au LP, cette heuristique n'ecrete PAS delibermement les pics de puissance
    mensuels (pas d'optimisation du tarif capacitaire) -- le lissage de pointe
    n'est qu'un effet de bord si un pic de conso coincide avec un SOC dispo.
    """
    N = len(conso)

    if contrat_kw <= 0:
        raise ValueError("contrat_kw (Excel N16) est nul ou absent.")

    if e_max <= 0 or p_max <= 0:
        return _dispatch_no_battery(conso, pv, price_import, price_export,
                                     contrat_kw, peak_so_far_kw, dt=dt)

    charge = np.zeros(N)
    decharge = np.zeros(N)
    imp = np.zeros(N)
    exp = np.zeros(N)
    curtail = np.zeros(N)
    soc_arr = np.zeros(N)

    soc = soc_init
    p_step = p_max * dt
    import_cap = contrat_kw * dt
    export_cap = contrat_kw * dt

    for t in range(N):
        net = pv[t] - conso[t]  # > 0 : surplus PV disponible ; < 0 : deficit a couvrir

        if net > 0:
            room_kwh = max(e_max - soc, 0.0) / eta_c
            c = min(net, p_step, room_kwh)
            charge[t] = c
            soc += c * eta_c
            remaining = net - c
            e = min(remaining, export_cap)
            exp[t] = e
            curtail[t] = remaining - e
        elif net < 0:
            deficit = -net
            available_kwh = soc * eta_d
            d = min(deficit, p_step, available_kwh)
            decharge[t] = d
            soc -= d / eta_d
            imp[t] = min(deficit - d, import_cap)
            # au-dela de import_cap : depassement contrat, penalise ci-dessous

        soc_arr[t] = soc

    deficit_all = np.maximum(-(pv - conso), 0.0)
    imp_overrun = np.maximum(deficit_all - decharge - import_cap, 0.0)
    overrun_cost = (imp_overrun * price_import * OVERRUN_PENALTY_MULT).sum()

    day_cost = float((imp * price_import - exp * price_export).sum() + overrun_cost)
    new_peak = max(peak_so_far_kw, (imp / dt).max() if N else 0.0)

    dispatch = pd.DataFrame({
        "charge_kwh": charge, "decharge_kwh": decharge,
        "import_kwh": imp, "export_kwh": exp, "soc_kwh": soc_arr,
        "curtail_kwh": curtail,
    })
    return dispatch, soc, new_peak, day_cost


def run_year_dispatch(conso_all, pv_all, price_import_all, price_export_all,
                       p_max, e_max, contrat_kw, demand_charge_rate,
                       index, return_detail=False, dispatch_fn=solve_day_dispatch):
    n_days = len(conso_all) // STEPS_PER_DAY
    soc = e_max * SOC_INIT_RATIO
    current_month, peak_so_far = None, 0.0
    month_peaks = {}
    total_energy_cost = 0.0
    total_export = 0.0
    total_import = 0.0
    total_curtail = 0.0
    day_frames = [] if return_detail else None

    for d in range(n_days):
        sl = slice(d * STEPS_PER_DAY, (d + 1) * STEPS_PER_DAY)
        idx = index[sl]
        month = idx[0].month
        if month != current_month:
            if current_month is not None:
                month_peaks[current_month] = peak_so_far
            current_month = month
            peak_so_far = 0.0

        day_df, soc, new_peak, day_cost = dispatch_fn(
            conso_all[sl], pv_all[sl], price_import_all[sl], price_export_all[sl],
            p_max=p_max, e_max=e_max, contrat_kw=contrat_kw,
            soc_init=soc, peak_so_far_kw=peak_so_far,
            demand_charge_rate=demand_charge_rate,
        )
        peak_so_far = new_peak
        total_energy_cost += day_cost
        total_export += float(day_df["export_kwh"].sum())
        total_import += float(day_df["import_kwh"].sum())
        total_curtail += float(day_df["curtail_kwh"].sum())

        if return_detail:
            day_df = day_df.copy()
            day_df.index = idx
            day_df["price_import"] = price_import_all[sl]
            day_df["price_export"] = price_export_all[sl]
            day_frames.append(day_df)

    month_peaks[current_month] = peak_so_far
    demand_charge_total = demand_charge_rate * sum(month_peaks.values())

    if return_detail:
        full_dispatch = pd.concat(day_frames)
        return total_energy_cost, demand_charge_total, total_import, total_export, total_curtail, full_dispatch
    return total_energy_cost, demand_charge_total, total_import, total_export, total_curtail


def decompose_gains(conso_all, pv_all, price_import_all, price_export_all,
                     p_max, e_max, contrat_kw, demand_charge_rate, index,
                     scenario_c_precomputed=None, dispatch_fn=solve_day_dispatch):
    cost_A_energy, cost_A_peak, imp_A, exp_A, curt_A = run_year_dispatch(
        conso_all, np.zeros_like(pv_all), price_import_all, price_export_all,
        p_max=0, e_max=0, contrat_kw=contrat_kw,
        demand_charge_rate=demand_charge_rate, index=index, dispatch_fn=dispatch_fn)
    cost_A = cost_A_energy + cost_A_peak

    cost_B_energy, cost_B_peak, imp_B, exp_B, curt_B = run_year_dispatch(
        conso_all, pv_all, price_import_all, price_export_all,
        p_max=0, e_max=0, contrat_kw=contrat_kw,
        demand_charge_rate=demand_charge_rate, index=index, dispatch_fn=dispatch_fn)
    cost_B = cost_B_energy + cost_B_peak

    if scenario_c_precomputed is not None:
        cost_C, imp_C, exp_C = scenario_c_precomputed
    else:
        cost_C_energy, cost_C_peak, imp_C, exp_C, curt_C = run_year_dispatch(
            conso_all, pv_all, price_import_all, price_export_all,
            p_max=p_max, e_max=e_max, contrat_kw=contrat_kw,
            demand_charge_rate=demand_charge_rate, index=index, dispatch_fn=dispatch_fn)
        cost_C = cost_C_energy + cost_C_peak

    gain_pv = cost_A - cost_B
    gain_batterie = cost_B - cost_C
    gain_total = cost_A - cost_C

    return {
        "cout_sans_rien_eur": cost_A,
        "cout_pv_seul_eur": cost_B,
        "cout_pv_batterie_eur": cost_C,
        "gain_pv_eur": gain_pv,
        "gain_batterie_belpex_eur": gain_batterie,
        "gain_total_vs_tout_reseau_eur": gain_total,
        "import_sans_rien_kwh": imp_A,
        "import_pv_seul_kwh": imp_B,
        "import_pv_batterie_kwh": imp_C,
        "export_pv_seul_kwh": exp_B,
        "export_pv_batterie_kwh": exp_C,
        "curtail_pv_seul_kwh": curt_B,
    }


def print_energy_report(conso_all, pv_all, p_max, e_max, contrat_kw, demand_charge_rate,
                         index, detail_precompute=None,
                         total_import=None, total_export=None, total_curtail=None):
    detail = detail_precompute

    pv_total = float(pv_all.sum())
    conso_total = float(conso_all.sum())
    part_non_importee = conso_total - total_import
    pv_exporte = pv_total - part_non_importee

    charge_total = float(detail["charge_kwh"].sum())
    decharge_total = float(detail["decharge_kwh"].sum())
    cycles_equivalents = decharge_total / e_max if e_max > 0 else 0.0

    if e_max > 0:
        pct_temps_pleine = 100 * (detail["soc_kwh"] >= 0.98 * e_max).mean()
        pct_temps_vide = 100 * (detail["soc_kwh"] <= 0.02 * e_max).mean()
    else:
        pct_temps_pleine = pct_temps_vide = 0.0

    prix_moyen_achat = (detail["import_kwh"] * detail["price_import"]).sum() / total_import if total_import > 0 else float("nan")
    prix_moyen_vente = (detail["export_kwh"] * detail["price_export"]).sum() / total_export if total_export > 0 else float("nan")

    print(f"\n=== BILAN ENERGETIQUE DETAILLE (annee 1, systeme PV + batterie) ===")
    print(f"PV produit : {pv_total:,.0f} kWh/an")
    print(f"Importe du reseau : {total_import:,.0f} kWh/an, prix moyen paye : {prix_moyen_achat*100:.2f} c/kWh")
    print(f"Exporte au reseau : {total_export:,.0f} kWh/an, prix moyen recu : {prix_moyen_vente*100:.2f} c/kWh")
    print(f"Cycles equivalents : {cycles_equivalents:,.1f}")

    return {
        "pv_total_kwh": pv_total,
        "conso_total_kwh": conso_total,
        "pv_non_importe_kwh": part_non_importee,
        "pv_exporte_kwh": pv_exporte,
        "import_kwh": total_import,
        "export_kwh": total_export,
        "curtail_kwh": total_curtail,
        "charge_kwh": charge_total,
        "decharge_kwh": decharge_total,
        "cycles_equivalents": cycles_equivalents,
        "pct_temps_pleine": pct_temps_pleine,
        "pct_temps_vide": pct_temps_vide,
        "prix_moyen_achat_eur_kwh": prix_moyen_achat,
        "prix_moyen_vente_eur_kwh": prix_moyen_vente,
    }


def client_old_cost(conso_all: np.ndarray, hour_all: np.ndarray, params: dict,
                     index: pd.DatetimeIndex = None) -> float:
    if params["price_hp"] or params["price_hc"]:
        is_hp = (hour_all >= params["heure_debut_hp"]) & (hour_all < params["heure_debut_hc"])
        price = np.where(is_hp, params["price_hp"], params["price_hc"])
    else:
        price = np.full_like(conso_all, params["old_price_kwh_fixe"], dtype=float)
    energy_cost = float((conso_all * price).sum())

    # Tarif capacitaire (pointe) sur le pic mensuel NON MITIGE (sans PV/batterie) --
    # symetrique avec demand_charge_total dans run_year_dispatch, qui applique le
    # meme tarif au pic APRES PV/batterie. Sans ce terme, le nouveau cout porte
    # seul cette charge, ce qui gonfle artificiellement l'"economie client".
    # NB : cette fonction n'est de toute facon jamais appelee en mode
    # "fournisseur_secondaire" (voir _run_fournisseur_model_impl) -- le retrait
    # du tarif capacitaire demande pour ce mode ne concerne donc pas ce calcul.
    demand_charge = 0.0
    if index is not None and params.get("tarif_capacitaire_fournisseur"):
        idx = pd.DatetimeIndex(index)
        conso_kw = conso_all * 4.0  # kWh par quart-heure -> kW moyen sur l'intervalle
        month_peaks = pd.Series(conso_kw, index=idx).groupby(idx.month).max()
        demand_charge = params["tarif_capacitaire_fournisseur"] * float(month_peaks.sum())

    return energy_cost + params["old_cout_additionnel_contrat"] + demand_charge


def prix_vente_annee(prix_base: float, year: int, revision_pct: float = 0.0,
                      periode_revision_annees: int = 5) -> float:
    if revision_pct == 0.0 or periode_revision_annees is None:
        return prix_base
    n_revisions = (year - 1) // periode_revision_annees
    return prix_base * (1 + revision_pct) ** n_revisions


def run_fournisseur_model(xlsm_path=XLSM_PATH, dayahead_pkl_path=DAYAHEAD_PKL_PATH,
                           horizon_annees=HORIZON_ANNEES,
                           discount_rate=DISCOUNT_RATE_DEFAULT,
                           annee_remplacement_batterie=13,
                           cout_remplacement_batterie_eur=25000,
                           revision_prix_pct=0.0,
                           periode_revision_annees=5,
                           parallel_years=False,
                           rapport_txt_path=None,
                           progress_callback=None,
                           param_overrides=None,
                           data_source=None,
                           output_csv_path=None):
    """
    param_overrides : dict optionnel de parametres (memes cles que celles lues
    depuis l'Excel, cf. dataextraction._read_parameters) qui ecrasent, EN
    MEMOIRE UNIQUEMENT, les valeurs lues depuis xlsm_path. Le fichier Excel
    source n'est JAMAIS modifie ni resauvegarde -- ceci evite le piege
    openpyxl qui supprime le cache des valeurs de formules (ex: profil PV
    par orientation dans l'onglet PVS) lors d'un save(), ce qui ferait
    silencieusement tomber la production PV a 0 au prochain calcul.
    Si 'kwc' est present dans param_overrides, il est aussi passe a
    extract_all() pour re-mettre a l'echelle le profil PV (qui est stocke en
    Excel comme un profil "par kWc", independant de F5).

    data_source : optionnel, tuple (params, df) deja construit -- voir
    run_fournisseur_model_from_data ci-dessous, qui est le point d'entree
    recommande pour le nouveau flux sans Excel (CSV conso + PVGIS).

    rapport_txt_path : optionnel, chemin ou dupliquer la sortie console
    (log complet). None (par defaut) = pas de fichier ecrit, juste la
    sortie console normale.

    output_csv_path : optionnel, voir _run_fournisseur_model_impl -- None
    (par defaut) = pas de CSV ecrit sur disque.
    """
    if rapport_txt_path is None:
        return _run_fournisseur_model_impl(
            xlsm_path, dayahead_pkl_path, horizon_annees, discount_rate,
            annee_remplacement_batterie, cout_remplacement_batterie_eur,
            revision_prix_pct, periode_revision_annees, parallel_years,
            progress_callback, param_overrides, data_source, output_csv_path)

    stdout_original = sys.stdout
    log_file = open(rapport_txt_path, "w", encoding="utf-8")
    sys.stdout = _Tee(stdout_original, log_file)
    try:
        return _run_fournisseur_model_impl(
            xlsm_path, dayahead_pkl_path, horizon_annees, discount_rate,
            annee_remplacement_batterie, cout_remplacement_batterie_eur,
            revision_prix_pct, periode_revision_annees, parallel_years,
            progress_callback, param_overrides, data_source, output_csv_path)
    finally:
        sys.stdout = stdout_original
        log_file.close()
        print(f"\n(Rapport complet sauvegarde dans : {rapport_txt_path})")


def run_fournisseur_model_from_data(params: dict, df, dayahead_pkl_path=None,
                                     horizon_annees=HORIZON_ANNEES,
                                     discount_rate=DISCOUNT_RATE_DEFAULT,
                                     annee_remplacement_batterie=13,
                                     cout_remplacement_batterie_eur=25000,
                                     revision_prix_pct=0.0,
                                     periode_revision_annees=5,
                                     rapport_txt_path=None,
                                     progress_callback=None,
                                     output_csv_path=None):
    """
    Point d'entree pour le nouveau flux SANS Excel : params est le dict
    complet de parametres (tarifs, batterie, CAPEX...) saisi directement dans
    l'interface, df vient de dataextraction.build_timeseries_from_sources
    (CSV conso client + profil PVGIS). Aucun fichier .xlsm requis.
    """
    return run_fournisseur_model(
        xlsm_path=None,
        dayahead_pkl_path=dayahead_pkl_path,
        horizon_annees=horizon_annees,
        discount_rate=discount_rate,
        annee_remplacement_batterie=annee_remplacement_batterie,
        cout_remplacement_batterie_eur=cout_remplacement_batterie_eur,
        revision_prix_pct=revision_prix_pct,
        periode_revision_annees=periode_revision_annees,
        rapport_txt_path=rapport_txt_path,
        progress_callback=progress_callback,
        param_overrides=None,
        data_source=(params, df),
        output_csv_path=output_csv_path,
    )


def _dispatch_one_year(args):
    (year, conso_all, pv_all_year1, market_price_values, params, index,
     p_max, contrat_kw, e_max_year1, degr_pv, degr_batt, inflation,
     annee_remplacement_batterie, cout_remplacement_batterie_eur,
     revenue_client_year1, old_cost_year1, revision_prix_pct, periode_revision_annees,
     use_belpex, price_import_year1, price_export_year1, dispatch_fn) = args

    pv_year = pv_all_year1 * ((1 - degr_pv) ** (year - 1))

    if annee_remplacement_batterie is not None and year >= annee_remplacement_batterie:
        age_batterie = year - annee_remplacement_batterie
    else:
        age_batterie = year - 1
    e_max_year = e_max_year1 * ((1 - degr_batt) ** age_batterie)

    inflation_factor = (1 + inflation) ** (year - 1)
    # Tarif capacitaire retire du calcul du gain UNIQUEMENT en mode
    # "fournisseur_secondaire" (demande utilisateur) -- voir la meme regle
    # dans _run_fournisseur_model_impl.
    demand_charge_year = (
        0.0 if params.get("mode_vente") == "fournisseur_secondaire"
        else params["tarif_capacitaire_fournisseur"] * inflation_factor
    )
    maintenance_year = params["maintenance_eur_an"] * inflation_factor

    if use_belpex:
        marge_f_year = params["marge_fournisseur"] * inflation_factor
        taxes_year = params["taxes_couts_proportionnels"] * inflation_factor
        marge_inj_year = params["marge_injection"] * inflation_factor
        mp_year = market_price_values * ((1 + inflation) ** (year - 1)) if APPLIQUER_INFLATION_AU_PRIX_MARCHE else market_price_values
        price_import_year = mp_year + marge_f_year + taxes_year
        price_export_year = np.maximum(mp_year + marge_inj_year, 0.0)
    else:
        # Pas de marche Day-Ahead : le tarif de reference (fixe ou HP/HC) est
        # simplement indexe globalement sur l'inflation, sans decomposition
        # marge/taxes (qui n'ont pas de sens sans prix de gros de reference).
        price_import_year = price_import_year1 * inflation_factor
        price_export_year = price_export_year1 * inflation_factor

    energy_cost, demand_charge_total, total_import, total_export, total_curtail = run_year_dispatch(
        conso_all, pv_year, price_import_year, price_export_year,
        p_max=p_max, e_max=e_max_year, contrat_kw=contrat_kw,
        demand_charge_rate=demand_charge_year, index=index,
        dispatch_fn=dispatch_fn,
    )

    if params.get("mode_vente") == "fournisseur_secondaire":
        # Idem annee 1 : on ne vend que la production PV, au prix fixe --
        # pv_year et price_import_year integrent deja respectivement la
        # degradation PV et l'inflation de cette annee-la.
        pv_totale_year = float(pv_year.sum())
        prix_vente_year = prix_vente_annee(
            params["prix_vente_kwh"], year, revision_prix_pct, periode_revision_annees)
        old_cost_year = float((pv_year * price_import_year).sum())
        revenue_client_year = pv_totale_year * prix_vente_year
        cout_appro_total = demand_charge_total + maintenance_year
        dont_energie_eur = 0.0
    else:
        frais_fixes_reseau_year = params["old_cout_additionnel_contrat"] * inflation_factor
        cout_appro_total = energy_cost + demand_charge_total + maintenance_year + frais_fixes_reseau_year

        revenue_client_year = prix_vente_annee(
            params["prix_vente_kwh"], year, revision_prix_pct, periode_revision_annees
        ) * float(conso_all.sum()) if revision_prix_pct else revenue_client_year1
        old_cost_year = old_cost_year1 * inflation_factor
        dont_energie_eur = energy_cost

    capex_remplacement_year = (
        cout_remplacement_batterie_eur
        if (annee_remplacement_batterie is not None and year == annee_remplacement_batterie)
        else 0.0
    )

    gross_profit_year = revenue_client_year - cout_appro_total - capex_remplacement_year
    client_savings_year = old_cost_year - revenue_client_year

    return {
        "annee": year,
        "revenu_client_eur": revenue_client_year,
        "cout_approvisionnement_eur": cout_appro_total,
        "dont_energie_eur": dont_energie_eur,
        "dont_pointe_eur": demand_charge_total,
        "dont_maintenance_eur": maintenance_year,
        "dont_remplacement_batterie_eur": capex_remplacement_year,
        "gain_brut_fournisseur_eur": gross_profit_year,
        "ancien_cout_client_indexe_eur": old_cost_year,
        "economie_client_eur": client_savings_year,
        "import_reseau_kwh": total_import,
        "export_reseau_kwh": total_export,
        "curtail_pv_kwh": total_curtail,
    }


def _run_fournisseur_model_impl(xlsm_path, dayahead_pkl_path, horizon_annees, discount_rate,
                                 annee_remplacement_batterie, cout_remplacement_batterie_eur,
                                 revision_prix_pct, periode_revision_annees, parallel_years,
                                 progress_callback=None, param_overrides=None,
                                 data_source=None, output_csv_path=None):
    """
    data_source : optionnel, tuple (params: dict, df: pd.DataFrame) deja
    construit (typiquement via dataextraction.build_timeseries_from_sources,
    dans le nouveau flux sans Excel). Si fourni, xlsm_path est ignore et on
    saute completement la lecture Excel. param_overrides s'applique quand
    meme par-dessus, pour rester coherent avec l'ancien comportement.

    output_csv_path : optionnel, chemin ou sauvegarder le detail annuel en
    CSV (debug/inspection manuelle). None (par defaut) = pas d'ecriture --
    l'appelant recupere de toute facon le DataFrame complet dans le dict
    retourne (cle "detail_annuel").
    """
    if data_source is not None:
        print("Utilisation des donnees fournies directement (pas de lecture Excel).")
        params, df = data_source
    else:
        print("Lecture des parametres et series depuis l'Excel...")
        kwc_override = param_overrides.get("kwc") if param_overrides else None
        params, df = extract_all(xlsm_path, kwc=kwc_override)
    if param_overrides:
        overridden = {k: v for k, v in param_overrides.items() if k in params}
        params.update(overridden)
        print(f"Parametres surcharges (interface, pas ecrits dans l'Excel) : {overridden}")

    conso_all = df["conso_kwh"].values
    pv_all_year1_raw = df["pv_kwh"].values
    hour_all = df["hour"].values
    index = df.index

    # Ecretement onduleur : l'onduleur ne peut jamais injecter plus que sa
    # puissance nominale (kva_onduleur), meme si le champ PV pourrait produire
    # plus a cet instant (fort ensoleillement, faible temperature...). On
    # plafonne donc chaque pas de temps a kva_onduleur * DT (kWh max par
    # intervalle), AVANT que la production n'entre dans le dispatch -- cette
    # energie est perdue, elle ne peut ni etre autoconsommee, ni stockee, ni
    # injectee. Approximation kVA ~= kW (facteur de puissance ~1, standard
    # pour ce type de modele). Comme la degradation PV annee par annee ne fait
    # que reduire la production (jamais l'augmenter), un seul ecretement ici
    # suffit pour tout l'horizon (voir _dispatch_one_year : pv_year = pv_all_year1 * degr).
    kva_onduleur = params.get("kva_onduleur") or 0.0
    if kva_onduleur > 0:
        pv_cap_kwh_per_step = kva_onduleur * DT
        pv_all_year1 = np.minimum(pv_all_year1_raw, pv_cap_kwh_per_step)
        pv_clip_onduleur_kwh = float((pv_all_year1_raw - pv_all_year1).sum())
        if pv_clip_onduleur_kwh > 0:
            print(f"  /!\\ Ecretement onduleur : {pv_clip_onduleur_kwh:,.0f} kWh/an de production PV "
                  f"perdue car superieure a la puissance de l'onduleur ({kva_onduleur:.1f} kVA).")
    else:
        pv_all_year1 = pv_all_year1_raw
        pv_clip_onduleur_kwh = 0.0

    mode_vente = params.get("mode_vente", "fournisseur_principal")
    use_belpex = (mode_vente == "fournisseur_principal")
    # Fournisseur principal : dispatch LP (arbitrage economique sur le marche
    # Belpex). Fournisseur secondaire / vente directe : dispatch heuristique
    # (autoconsommation directe + charge sur surplus PV / decharge sur
    # deficit), car ces clients n'ont typiquement pas d'EMS pilote capable de
    # faire de l'arbitrage sur un signal de prix -- voir solve_day_dispatch_heuristic.
    dispatch_fn = solve_day_dispatch if use_belpex else solve_day_dispatch_heuristic
    if not use_belpex:
        print(f"Mode '{mode_vente}' : dispatch batterie HEURISTIQUE (self-consumption, "
              f"sans arbitrage prix -- pas de pilotage EMS suppose).")

    if use_belpex:
        market_price, is_demo = load_dayahead_prices(dayahead_pkl_path, index)
        market_price_values = market_price.values
    else:
        print(f"Mode '{mode_vente}' : pas de marche Day-Ahead -- dispatch sur tarif reseau de reference (fixe/HP-HC).")
        market_price_values = None
        is_demo = False

    p_max = params["battery_power_kw"]
    e_max_year1 = params["battery_capacity_kwh"]
    contrat_kw = params["contrat_kw"]
    # Tarif capacitaire retire du calcul du gain UNIQUEMENT en mode
    # "fournisseur_secondaire" (demande utilisateur) : annule a la fois son
    # cout (dont_pointe_eur) et l'incitation d'ecretement de pointe dans le LP
    # (c[6*N] dans solve_day_dispatch) pour ce mode-la seulement -- les autres
    # modes (fournisseur_principal, vente_directe) gardent le tarif capacitaire
    # normalement, tout comme client_old_cost.
    demand_charge_rate = (
        0.0 if mode_vente == "fournisseur_secondaire"
        else params["tarif_capacitaire_fournisseur"]
    )
    inflation = params["inflation_pct"] / 100.0
    degr_pv = params["degradation_pv_pct"] / 100.0
    degr_batt = params["degradation_batterie_pct"] / 100.0

    capex_pv = params["kwc"] * params["prix_kwc_pv"]
    capex_batterie = params["battery_capacity_kwh"] * params["prix_kwh_batterie"]
    capex_total = capex_pv + capex_batterie

    if cout_remplacement_batterie_eur is None:
        cout_remplacement_batterie_eur = capex_batterie

    conso_totale = float(conso_all.sum())
    pv_totale_y1 = float(pv_all_year1.sum())

    if use_belpex:
        price_import_year1, price_export_year1 = build_tariff_fournisseur(market_price_values, params)
    else:
        price_import_year1, price_export_year1 = build_tariff_reference(params, hour_all)

    if mode_vente == "vente_directe":
        # Le client paie le CAPEX : pas de marge fournisseur, le prix de
        # vente est calcule automatiquement (moyenne ponderee par la conso
        # du tarif reseau de reference) pour que le "gain brut fournisseur"
        # reste nul par construction -- toute la valeur du PV+batterie
        # remonte dans "economie_client", pas dans un profit fournisseur.
        params = dict(params)  # ne pas muter le dict de l'appelant
        params["prix_vente_kwh"] = float((conso_all * price_import_year1).sum() / conso_totale) if conso_totale > 0 else 0.0
        print(f"  -> Mode vente directe : prix de vente auto-calcule = {params['prix_vente_kwh']:.4f} EUR/kWh "
              f"(moyenne ponderee du tarif reseau de reference)")

    if mode_vente == "fournisseur_secondaire":
        # En mode secondaire, tu ne vends QUE l'energie produite par le PV
        # (pas toute la consommation du client -- le reste, le client
        # continue de l'acheter a son fournisseur/tarif habituel, donc ca ne
        # change rien pour lui et ca s'annule dans le calcul d'economie).
        # Client : gagne la difference de prix, sur la seule quantite PV,
        # entre l'ancien tarif reseau (price_import_year1, tenant compte de
        # la structure HP/HC le cas echeant) et le nouveau prix fixe.
        # Fournisseur : encaisse prix_vente_kwh sur la production PV totale.
        old_cost_year1 = float((pv_all_year1 * price_import_year1).sum())
        revenue_client_year1 = pv_totale_y1 * params["prix_vente_kwh"]
    else:
        old_cost_year1 = client_old_cost(conso_all, hour_all, params, index=index)
        revenue_client_year1 = params["prix_vente_kwh"] * conso_totale

    print(f"\n=== ANNEE 1 (reference) ===")
    print(f"Consommation totale du client : {conso_totale:,.0f} kWh/an")
    if mode_vente == "fournisseur_secondaire":
        print(f"Production PV vendue au client : {pv_totale_y1:,.0f} kWh/an")
        print(f"Cout de cette energie a l'ancien tarif : {old_cost_year1:,.0f} EUR/an")
        print(f"Cout de cette energie au nouveau prix fixe ({params['prix_vente_kwh']:.3f} EUR/kWh) : {revenue_client_year1:,.0f} EUR/an")
    else:
        print(f"Ancien cout client (avant toi) : {old_cost_year1:,.0f} EUR/an")
        print(f"Nouveau cout client (prix fixe {params['prix_vente_kwh']:.3f} EUR/kWh) : {revenue_client_year1:,.0f} EUR/an")

    print("\nCalcul du dispatch annee 1 (PV + batterie, systeme reel)...")
    (energy_cost_y1, demand_charge_y1, import_y1, export_y1, curtail_y1,
     detail_y1) = run_year_dispatch(
        conso_all, pv_all_year1, price_import_year1, price_export_year1,
        p_max=p_max, e_max=e_max_year1, contrat_kw=contrat_kw,
        demand_charge_rate=demand_charge_rate, index=index, return_detail=True,
        dispatch_fn=dispatch_fn)
    cost_C_year1 = energy_cost_y1 + demand_charge_y1

    print("Calcul de la decomposition des gains (annee 1)...")
    decomp = decompose_gains(
        conso_all, pv_all_year1, price_import_year1, price_export_year1,
        p_max=p_max, e_max=e_max_year1, contrat_kw=contrat_kw,
        demand_charge_rate=demand_charge_rate, index=index,
        scenario_c_precomputed=(cost_C_year1, import_y1, export_y1),
        dispatch_fn=dispatch_fn)

    energy_report = print_energy_report(
        conso_all, pv_all_year1, p_max=p_max, e_max=e_max_year1, contrat_kw=contrat_kw,
        demand_charge_rate=demand_charge_rate, index=index, detail_precompute=detail_y1,
        total_import=import_y1, total_export=export_y1, total_curtail=curtail_y1)

    print(f"\nSimulation du dispatch Day-Ahead sur {horizon_annees} annees...")

    common_args = (conso_all, pv_all_year1, market_price_values, params, index,
                   p_max, contrat_kw, e_max_year1, degr_pv, degr_batt, inflation,
                   annee_remplacement_batterie, cout_remplacement_batterie_eur,
                   revenue_client_year1, old_cost_year1, revision_prix_pct, periode_revision_annees,
                   use_belpex, price_import_year1, price_export_year1, dispatch_fn)
    tasks = [(year,) + common_args for year in range(1, horizon_annees + 1)]

    rows = []
    for i, task in enumerate(tasks):
        year = task[0]
        if year == 1 and annee_remplacement_batterie != 1:
            maintenance_year1 = params["maintenance_eur_an"]
            if mode_vente == "fournisseur_secondaire":
                # Pas de cout d'energie/frais fixes reseau ici : tu ne vends
                # que la production PV (encaissee dans revenue_client_year1),
                # tes seuls couts sont la maintenance et le tarif capacitaire.
                cout_appro_y1 = demand_charge_y1 + maintenance_year1
            else:
                cout_appro_y1 = energy_cost_y1 + demand_charge_y1 + maintenance_year1 + params["old_cout_additionnel_contrat"]
            rows.append({
                "annee": 1,
                "revenu_client_eur": revenue_client_year1,
                "cout_approvisionnement_eur": cout_appro_y1,
                "dont_energie_eur": 0.0 if mode_vente == "fournisseur_secondaire" else energy_cost_y1,
                "dont_pointe_eur": demand_charge_y1,
                "dont_maintenance_eur": maintenance_year1,
                "dont_remplacement_batterie_eur": 0.0,
                "gain_brut_fournisseur_eur": revenue_client_year1 - cout_appro_y1,
                "ancien_cout_client_indexe_eur": old_cost_year1,
                "economie_client_eur": (
                    old_cost_year1 - cout_appro_y1 if mode_vente == "vente_directe"
                    else old_cost_year1 - revenue_client_year1
                ),
                "import_reseau_kwh": import_y1,
                "export_reseau_kwh": export_y1,
                "curtail_pv_kwh": curtail_y1,
            })
        else:
            rows.append(_dispatch_one_year(task))
        label = "economie client" if mode_vente == "vente_directe" else "gain brut fournisseur"
        valeur_log = rows[-1]['economie_client_eur'] if mode_vente == "vente_directe" else rows[-1]['gain_brut_fournisseur_eur']
        print(f"  Annee {year:>2} : {label} = {valeur_log:>10,.0f} EUR")
        if progress_callback:
            progress_callback(year, horizon_annees)

    result = pd.DataFrame(rows).sort_values("annee").reset_index(drop=True)

    if mode_vente == "vente_directe":
        # Le client possede tout (CAPEX + systeme) : toute la valeur du
        # dispatch PV+batterie doit remonter dans "economie_client_eur" (son
        # vrai nouveau cout = cout_approvisionnement_eur, PAS un "revenu
        # fournisseur" fictif) -- il n'y a pas de flux de profit fournisseur
        # separe dans ce mode (le seul profit fournisseur possible est la
        # marge sur la vente du materiel, deja capturee via prix_kwc_pv /
        # prix_kwh_batterie si tu les fixes au-dessus de ton cout reel).
        result["economie_client_eur"] = result["ancien_cout_client_indexe_eur"] - result["cout_approvisionnement_eur"]
        result["revenu_client_eur"] = result["cout_approvisionnement_eur"]
        result["cashflow_annuel_eur"] = result["economie_client_eur"] - result["dont_remplacement_batterie_eur"]
        # Le gain du vendeur en vente directe n'est pas un flux annuel comme
        # dans les modes fournisseur (pas d'abonnement/marge energie recurrente) :
        # c'est un gain ponctuel realise a la vente du systeme, egal au CAPEX
        # que le client paie. On l'attribue conventionnellement a l'annee 1
        # (annee de la vente) pour rester representable dans la colonne annuelle
        # -- des lors distinct de "economie_client_eur", qui reste le flux de
        # savings du client sur la duree de vie du systeme.
        result["gain_brut_fournisseur_eur"] = 0.0
        result.loc[result["annee"] == 1, "gain_brut_fournisseur_eur"] = capex_total
    else:
        result["cashflow_annuel_eur"] = result["gain_brut_fournisseur_eur"]

    result["cashflow_cumule_eur"] = result["cashflow_annuel_eur"].cumsum() - capex_total

    payback_year = None
    for i, r in result.iterrows():
        if r["cashflow_cumule_eur"] >= 0:
            prev_cum = result.loc[i - 1, "cashflow_cumule_eur"] if i > 0 else -capex_total
            frac = -prev_cum / (r["cashflow_cumule_eur"] - prev_cum) if r["cashflow_cumule_eur"] != prev_cum else 0
            payback_year = (i - 1 + 1) + frac if i > 0 else frac
            break

    total_gain_brut = result["cashflow_annuel_eur"].sum()
    roi_pct = 100 * (total_gain_brut - capex_total) / capex_total if capex_total > 0 else float("nan")
    total_economie_client = result["economie_client_eur"].sum()

    cashflows = [-capex_total] + result["cashflow_annuel_eur"].tolist()
    npv, irr = compute_npv_irr(cashflows, discount_rate)

    print(f"\n=== RESULTATS ROI FOURNISSEUR ({horizon_annees} ans) ===")
    print(f"CAPEX initial : {capex_total:,.0f} EUR")
    print(f"Gain brut cumule : {total_gain_brut:,.0f} EUR")
    print(f"ROI net non-actualise : {roi_pct:.1f} %")
    print(f"VAN (taux {discount_rate*100:.1f}%) : {npv:,.0f} EUR")

    e_max_pour_pct = e_max_year1 if e_max_year1 > 0 else float("nan")
    n_pts = len(detail_y1)  # peut etre < len(index) si l'annee ne fait pas un nombre entier
    # de jours complets (le dispatch travaille jour par jour, 96 pas de 15 min) --
    # on tronque tout le monde a la meme longueur pour rester aligne.
    if n_pts < len(index):
        print(f"  /!\\ {len(index) - n_pts} derniers points (jour incomplet) exclus du graphique "
              f"quart-horaire -- sans impact sur les totaux annuels.")
    serie_qh_annee1 = pd.DataFrame({
        "conso_kwh": conso_all[:n_pts],
        "pv_kwh": pv_all_year1[:n_pts],
        "import_kwh": detail_y1["import_kwh"].values,
        "export_kwh": detail_y1["export_kwh"].values,
        "soc_kwh": detail_y1["soc_kwh"].values,
        "soc_pct": 100.0 * detail_y1["soc_kwh"].values / e_max_pour_pct,
    }, index=index[:n_pts])
    if use_belpex:
        serie_qh_annee1["belpex_eur_kwh"] = market_price_values[:n_pts]
    else:
        # Tarif reseau de reference (fixe OU HP/HC selon ce que le client a
        # rempli -- voir build_tariff_reference) applique a chaque pas de
        # temps. Sert a calculer un cout reseau avant/apres PV (app.py),
        # independamment du fait que le tarif soit fixe ou HP/HC.
        serie_qh_annee1["price_reference_eur_kwh"] = price_import_year1[:n_pts]

    if output_csv_path:
        result.to_csv(output_csv_path, index=False)

    return {
        "params": params,
        "mode_vente": mode_vente,
        "capex_pv": capex_pv,
        "capex_batterie": capex_batterie,
        "capex_total": capex_total,
        # Gain du vendeur : en vente directe, distinct du gain client -- c'est
        # le CAPEX encaisse a la vente du systeme (voir commentaire plus haut).
        # Dans les modes fournisseur, le "gain vendeur" est simplement le gain
        # brut cumule (deja suivi via total_gain_brut) -- ce champ evite de
        # dupliquer un cas particulier cote app.py.
        "gain_vendeur_eur": capex_total if mode_vente == "vente_directe" else total_gain_brut,
        "old_cost_year1": old_cost_year1,
        "revenue_client_year1": revenue_client_year1,
        "detail_annuel": result,
        "serie_qh_annee1": serie_qh_annee1,
        "payback_year": payback_year,
        "roi_pct": roi_pct,
        "npv_eur": npv,
        "irr_pct": irr * 100 if not np.isnan(irr) else float("nan"),
        "discount_rate": discount_rate,
        "total_gain_brut": total_gain_brut,
        "total_economie_client": total_economie_client,
        "is_demo_prices": is_demo,
        "decomposition_gains": decomp,
        "bilan_energetique_annee1": energy_report,
        "pv_clip_onduleur_kwh": pv_clip_onduleur_kwh,
    }
