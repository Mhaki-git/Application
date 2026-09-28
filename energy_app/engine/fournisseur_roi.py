"""
MODELE ECONOMIQUE "FOURNISSEUR" : economies client + rentabilite (ROI).
(voir docstring complet dans le script d'origine fourni par l'utilisateur)
"""
import os
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pickle import PicklingError
from typing import Callable, Optional

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.optimize import linprog

try:
    # Acces direct a HiGHS (le solveur deja utilise par scipy.linprog) :
    # ~2x plus rapide sur le LP journalier. Optionnel -- repli sur linprog.
    import highspy
except ImportError:  # pragma: no cover
    highspy = None

from io_excel.dataextraction import extract_all, XLSM_PATH as DEFAULT_XLSM_PATH
from engine.finance_utils import compute_npv_irr
from engine.calendar_utils import NearestByCalendarSymmetry

import sys


class _ThreadLogRouter:
    """
    Remplace sys.stdout une fois pour toutes : tout est ecrit sur la sortie
    d'origine et, pour le seul thread courant, duplique dans son fichier de
    rapport (local.log). Remplace l'ancien echange global de sys.stdout, qui
    melangeait les journaux de deux simulations lancees en meme temps
    (Streamlit execute chaque session dans son propre thread) et pouvait
    restaurer la mauvaise sortie.
    """

    def __init__(self, base):
        self.base = base
        self.local = threading.local()

    def write(self, data):
        self.base.write(data)
        log = getattr(self.local, "log", None)
        if log is not None:
            log.write(data)

    def flush(self):
        self.base.flush()
        log = getattr(self.local, "log", None)
        if log is not None:
            log.flush()

    def __getattr__(self, name):
        return getattr(self.base, name)


_LOG_ROUTER_LOCK = threading.Lock()


def _install_thread_log_router() -> "_ThreadLogRouter":
    """
    Installe (une seule fois, de facon idempotente et thread-safe) le
    _ThreadLogRouter a la place de sys.stdout, et retourne l'instance
    installee (nouvelle ou deja existante) pour que l'appelant puisse y
    attacher son propre fichier de log par thread.
    """
    with _LOG_ROUTER_LOCK:
        if not isinstance(sys.stdout, _ThreadLogRouter):
            sys.stdout = _ThreadLogRouter(sys.stdout)
        return sys.stdout


XLSM_PATH = DEFAULT_XLSM_PATH
DAYAHEAD_PKL_PATH = "dayahead_prices_2024_qh.pkl"

# Rendements aller (charge) et retour (decharge) de la batterie (sans unite,
# fraction de 1) : la moitie des pertes de conversion est imputee a chaque
# sens, ce qui donne un rendement aller-retour global de ETA_C * ETA_D = 90%.
ETA_C = 0.95
ETA_D = 0.95
SOC_INIT_RATIO = 0.5  # SOC de depart de chaque annee simulee, en fraction de e_max (pas d'etat reporte d'une annee sur l'autre).
DT = 0.25  # Duree d'un pas de temps, en heures (quart d'heure) -- sert a convertir puissance (kW) <-> energie (kWh) sur un pas.
STEPS_PER_DAY = 96  # 24h / DT : nombre de pas de temps quart-horaires dans une journee.

HORIZON_ANNEES = 20
APPLIQUER_INFLATION_AU_PRIX_MARCHE = False  # Si False, seuls marge/taxes/injection sont indexes sur l'inflation annee apres annee -- le prix de marche Day-Ahead lui-meme reste celui de l'annee source (hypothese : le marche de gros n'est pas suppose suivre l'inflation domestique).

OVERRUN_PENALTY_MULT = 3.0  # Multiplicateur de penalite (x price_import) applique a tout depassement du contrat_kw souscrit -- assez eleve pour que le LP/l'heuristique n'y recourent qu'en dernier recours, jamais par arbitrage economique.
DISCOUNT_RATE_DEFAULT = 0.06  # Taux d'actualisation par defaut (6%/an) utilise pour le calcul de la VAN (compute_npv_irr).


def _build_lp_skeleton(n: int):
    """
    Variables du LP journalier, dans l'ordre (chaque bloc de taille n, sauf
    new_peak qui est scalaire) : charge, decharge, imp, exp, soc, curtail,
    imp_overrun, new_peak.

    imp_overrun : depassement d'import AU-DELA de contrat_kw*dt, autorise
    mais fortement penalise (OVERRUN_PENALTY_MULT) -- sans cette variable,
    un scenario physiquement impossible a satisfaire dans la limite du
    contrat (ex: forte conso nocturne + batterie vide + petit contrat)
    rendait le LP purement INFAISABLE (RuntimeError), alors que
    _dispatch_no_battery et solve_day_dispatch_heuristic geraient deja ce
    cas en douceur avec la meme penalite -- incoherence corrigee ici.
    """
    i_n = sparse.eye(n, format="csr")  # matrice identite n x n : coefficient 1 sur chaque pas de temps pris isolement.
    z_n = sparse.csr_matrix((n, n))  # bloc nul n x n, pour les variables qui n'interviennent pas dans une contrainte donnee.
    s_n = sparse.eye(n, k=-1, format="csr")  # identite decalee d'une sous-diagonale : s_n @ soc = soc au pas de temps precedent (t-1), utilise pour la contrainte de continuite du SOC.
    zeros_col = sparse.csr_matrix((n, 1))
    ones_col = sparse.csr_matrix(np.ones((n, 1)))

    # Contrainte de bilan energetique a chaque pas de temps (egalite, voir A_eq
    # dans _lp_equality_matrix) : decharge + imp + imp_overrun - charge - exp -
    # curtail = conso - pv. Autrement dit toute l'energie qui entre dans le
    # noeud (batterie qui se decharge, import reseau normal + en depassement)
    # doit egaler ce qui en sort (charge batterie, export, ecretement) plus le
    # desequilibre net conso-pv de ce pas de temps.
    a_balance = sparse.hstack([-i_n, i_n, i_n, -i_n, z_n, -i_n, i_n, zeros_col], format="csr")
    # imp_overrun contribue AUSSI au pic de puissance facturable (tarif
    # capacitaire) : c'est de la puissance reellement soutiree du reseau a
    # cet instant, peu importe qu'elle depasse ou non le contrat souscrit --
    # sans ca, le LP peut "arbitrer" en faisant passer de l'import par
    # imp_overrun (penalise mais fixe) plutot que imp (dans la limite du
    # contrat) simplement pour echapper au tarif capacitaire sur le pic,
    # ce qui n'a pas de sens physique.
    # Contrainte d'inegalite (imp + imp_overrun) * DT - new_peak <= 0 a chaque
    # pas de temps : force new_peak (variable scalaire, kW) a etre au moins
    # egale a la puissance importee (normale + en depassement) sur CHAQUE pas
    # de temps du mois -- new_peak represente donc le pic de puissance
    # facturable du mois une fois le LP resolu (voir usage de c[7*N] plus bas).
    a_peak = sparse.hstack([z_n, z_n, i_n, z_n, z_n, z_n, i_n, -DT * ones_col], format="csr")

    return {
        "I_N": i_n, "Z_N": z_n, "S_N": s_n, "zeros_col": zeros_col,
        "A_balance": a_balance, "A_peak": a_peak,
    }


_LP_SKELETON = _build_lp_skeleton(STEPS_PER_DAY)

DISPATCH_COLUMNS = ("charge_kwh", "decharge_kwh", "import_kwh", "export_kwh", "soc_kwh", "curtail_kwh")

# Matrices d'egalite du LP (bilan + SOC) : ne dependent que de (N, eta_c, eta_d),
# pas des donnees du jour -- construites une fois puis reutilisees (auparavant
# reconstruites a chaque jour, ~5800 fois pour 15 ans).
_A_EQ_CACHE = {}


def _lp_equality_matrix(n: int, eta_c: float, eta_d: float):
    """
    Construit (ou recupere depuis le cache _A_EQ_CACHE) la matrice d'egalite
    complete du LP journalier : bilan energetique (A_balance) empile avec la
    contrainte de continuite du SOC (a_soc). Mise en cache par (n, eta_c,
    eta_d) car ces matrices sont purement structurelles -- independantes des
    donnees du jour (conso, pv, prix) -- et couteuses a reconstruire a chaque
    appel.
    """
    key = (n, eta_c, eta_d)
    a_eq = _A_EQ_CACHE.get(key)
    if a_eq is None:
        sk = _LP_SKELETON if n == STEPS_PER_DAY else _build_lp_skeleton(n)
        i_n, z_n, s_n = sk["I_N"], sk["Z_N"], sk["S_N"]
        # Continuite du SOC : soc[t] - soc[t-1] = eta_c*charge[t] - decharge[t]/eta_d
        # (soc[t-1] = 0 pour t=0, la valeur initiale reelle soc_init est injectee
        # via b_soc[0] dans le second membre, pas dans cette matrice). Les pertes
        # de conversion sont donc integrees ici : eta_c < 1 fait que charger 1 kWh
        # au reseau n'ajoute que eta_c kWh au SOC, et eta_d < 1 fait qu'il faut
        # puiser 1/eta_d kWh du SOC pour restituer 1 kWh en sortie.
        a_soc = sparse.hstack([-eta_c * i_n, (1 / eta_d) * i_n, z_n, z_n,
                               i_n - s_n, z_n, z_n, sk["zeros_col"]], format="csr")
        a_eq = sparse.vstack([sk["A_balance"], a_soc], format="csr")
        _A_EQ_CACHE[key] = a_eq
    return a_eq


def _dispatch_to_frame(arrays: dict) -> pd.DataFrame:
    """Convertit le dict de tableaux numpy issu d'un dispatch journalier en DataFrame (colonnes DISPATCH_COLUMNS)."""
    return pd.DataFrame({col: arrays[col] for col in DISPATCH_COLUMNS})


def _align_market_price_to_calendar(market_price: pd.Series, target_index: pd.DatetimeIndex) -> pd.Series:
    """
    Aligne une serie de prix Day-Ahead (indexee sur son annee source, ex.
    2024 ou 2025 -- eventuellement une simple TRANCHE de l'annee, ex. 1er
    janvier -> 15 aout, si l'utilisateur a choisi de limiter la source) sur
    target_index (les dates REELLES du client, qui peuvent demarrer
    n'importe quand -- ex. 02/03/2025), en calant par (mois, jour, heure)
    plutot que par timestamp exact.

    Sans ca, un reindex() direct sur des annees differentes ne matcherait
    quasiment aucun timestamp (source et cible n'ont pas la meme annee) et
    retomberait en NaN -> ffill/bfill, aplatissant le prix sur toute la
    periode au lieu de suivre le vrai profil journalier/saisonnier Belpex.

    Calage volontairement fait au niveau de l'HEURE (pas la minute) : la
    source Day-Ahead reelle (fetch_belpex.py, format natif ENTSO-E) est
    HORAIRE, pas quart-horaire, malgre le suffixe "_qh" du nom de fichier --
    caler sur (mois, jour, heure, minute) exact ferait manquer les cibles a
    :15/:30/:45 (aucune source a ces minutes) et les ferait retomber sur un
    mauvais fallback. Tous les quarts d'heure d'une meme heure recoivent donc
    le meme prix horaire, comme le faisait l'ancien reindex(...,method=ffill),
    que la source soit horaire ou deja quart-horaire (dans ce dernier cas, on
    ecrase juste par une moyenne des 4 valeurs -- v. groupby ci-dessous).

    Si l'heure cible n'a pas d'exacte correspondance dans la source (29
    fevrier absent d'une annee non bissextile, ou jour hors de la tranche
    source retenue) : repli sur le jour disponible le plus proche en
    distance calendaire CYCLIQUE, a la meme heure -- meme logique que
    dataextraction.fill_missing_periods_by_calendar_symmetry.
    """
    # Une source deja quart-horaire (4 valeurs/heure, potentiellement
    # differentes) est reduite a une valeur par heure (moyenne) pour que le
    # calage joue toujours au meme niveau de granularite, quelle que soit la
    # resolution native de la source.
    hourly = market_price.groupby(
        [market_price.index.month, market_price.index.day, market_price.index.hour]
    ).mean()
    hourly.index.names = ["month", "day", "hour"]

    exact_lookup = {}
    calendar_index = NearestByCalendarSymmetry()
    for (month, day, hour), val in hourly.items():
        exact_lookup[(month, day, hour)] = val
        calendar_index.add(subkey=hour, month=month, day=day, value=val)
    calendar_index.finalize()

    fallback = float(np.nanmean(hourly.values))

    out_values = np.empty(len(target_index))
    n_fallback = 0
    for i, ts in enumerate(target_index):
        key = (ts.month, ts.day, ts.hour)
        if key in exact_lookup:
            out_values[i] = exact_lookup[key]
        else:
            out_values[i] = calendar_index.closest(
                subkey=ts.hour, month=ts.month, day=ts.day, fallback=fallback)
            n_fallback += 1

    if n_fallback:
        print(f"  /!\\ {n_fallback} pas de temps sans correspondance exacte (heure) dans la "
              f"source Day-Ahead (29 fevrier, ou jour hors de la tranche source retenue) -- "
              f"combles par le jour disponible le plus proche (symetrie calendaire).")

    return pd.Series(out_values, index=target_index)


def load_dayahead_prices(dayahead_pkl_path, target_index: pd.DatetimeIndex) -> tuple:
    """
    dayahead_pkl_path : soit un chemin vers un .pkl (usage interne -- fichiers
    figes data/belpex_*_qh.pkl controles par l'appli, jamais fournis par un
    utilisateur externe), soit directement une pd.Series deja chargee (cas
    d'un upload utilisateur : lire un .pkl fourni par un tiers avec
    pd.read_pickle serait une deserialisation non fiable qui peut executer du
    code arbitraire -- app.py lit ces uploads en CSV via
    dataextraction.read_dayahead_csv et passe directement la Series ici).
    """
    try:
        if isinstance(dayahead_pkl_path, pd.Series):
            market_price = dayahead_pkl_path
        elif not dayahead_pkl_path:
            raise FileNotFoundError(dayahead_pkl_path)
        else:
            market_price = pd.read_pickle(dayahead_pkl_path)
        if market_price.index.tz is not None:
            market_price = market_price.tz_localize(None)
        market_price = market_price[~market_price.index.duplicated(keep="first")]

        market_price = _align_market_price_to_calendar(market_price, target_index)
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
    """
    Construit price_import/price_export (en EUR/kWh, meme resolution
    temporelle que market_price_values) pour le mode "fournisseur_principal" :
    le client achete au prix de marche Day-Ahead + marge fournisseur + taxes
    et couts proportionnels, et est rembourse sur son export au prix de
    marche + marge d'injection (potentiellement negative), plafonne a 0 pour
    ne jamais payer le client a exporter (pas de prix d'injection negatif
    facture au client). Le parametre inflate_market n'est pas utilise ici
    (l'indexation eventuelle du prix de marche sur l'inflation est geree
    separement, voir APPLIQUER_INFLATION_AU_PRIX_MARCHE dans _dispatch_one_year).
    """
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
    """Variante DataFrame de _dispatch_no_battery_arrays (voir cette derniere pour le detail) -- meme contrat de retour (frame, soc, new_peak, day_cost) que solve_day_dispatch."""
    arrays, soc, new_peak, day_cost = _dispatch_no_battery_arrays(
        conso, pv, price_import, price_export, contrat_kw, peak_so_far_kw, dt=dt)
    return _dispatch_to_frame(arrays), soc, new_peak, day_cost


def _dispatch_no_battery_arrays(conso, pv, price_import, price_export,
                                 contrat_kw, peak_so_far_kw, dt=DT):
    """
    Fallback partage (LP et heuristique) quand e_max<=0 ou p_max<=0 : pas de
    batterie, autoconsommation directe uniquement, import/export plafonnes
    au contrat, depassement penalise (OVERRUN_PENALTY_MULT).
    """
    N = len(conso)
    net = conso - pv  # net > 0 : deficit a importer ; net < 0 : surplus PV a exporter (kWh sur le pas de temps).
    imp_uncapped = np.maximum(net, 0)
    exp_uncapped = np.maximum(-net, 0)

    imp = np.minimum(imp_uncapped, contrat_kw * dt)  # import plafonne a la puissance souscrite (contrat_kw en kW -> kWh via *dt).
    imp_overrun = imp_uncapped - imp  # depassement du contrat, autorise mais penalise ci-dessous (jamais bloquant).
    exp = np.minimum(exp_uncapped, contrat_kw * dt)  # export egalement plafonne au contrat (limite d'injection au reseau).
    curtail = exp_uncapped - exp  # surplus PV non exportable (au-dela du contrat) : perdu, ni facture ni credite.

    overrun_cost = (imp_overrun * price_import * OVERRUN_PENALTY_MULT).sum()
    day_cost = float((imp * price_import - exp * price_export).sum() + overrun_cost)
    new_peak = max(peak_so_far_kw, (imp / dt).max() if N else 0.0)  # met a jour le pic mensuel de puissance importee (kW) ; ignore volontairement imp_overrun (voir a_peak plus haut pour le cas LP, qui lui l'inclut).
    zeros = np.zeros(N)
    arrays = {
        "charge_kwh": zeros, "decharge_kwh": zeros,
        "import_kwh": imp, "export_kwh": exp, "soc_kwh": zeros,
        "curtail_kwh": curtail,
    }
    return arrays, 0.0, new_peak, day_cost


def solve_day_dispatch(conso, pv, price_import, price_export,
                        p_max, e_max, contrat_kw,
                        soc_init, peak_so_far_kw, demand_charge_rate,
                        eta_c=ETA_C, eta_d=ETA_D, dt=DT):
    """
    Dispatch journalier OPTIMAL (LP, arbitrage sur les prix) : voir
    _solve_day_lp_arrays pour le detail du probleme d'optimisation. Variante
    DataFrame de cette derniere -- meme contrat de retour partout dans ce
    module : (frame/tableaux, soc final, nouveau pic mensuel kW, cout du jour EUR).
    """
    arrays, soc, new_peak, day_cost = _solve_day_lp_arrays(
        conso, pv, price_import, price_export, p_max, e_max, contrat_kw,
        soc_init, peak_so_far_kw, demand_charge_rate, eta_c=eta_c, eta_d=eta_d, dt=dt)
    return _dispatch_to_frame(arrays), soc, new_peak, day_cost


def _solve_day_lp_arrays(conso, pv, price_import, price_export,
                         p_max, e_max, contrat_kw,
                         soc_init, peak_so_far_kw, demand_charge_rate,
                         eta_c=ETA_C, eta_d=ETA_D, dt=DT):
    """
    Resout le LP journalier qui minimise le cout d'energie (import - export,
    en EUR) plus la penalite de depassement de contrat et le tarif
    capacitaire sur le pic de puissance, en choisissant l'usage optimal de la
    batterie (arbitrage sur price_import/price_export). conso et pv sont en
    kWh par pas de temps (longueur N, generalement 96 = un jour). Retourne
    (dict de tableaux numpy par variable de dispatch, SOC final en kWh,
    nouveau pic mensuel en kW, cout du jour en EUR). Leve RuntimeError si le
    solveur ne converge pas (voir _lp_not_converged) et ValueError si
    contrat_kw est nul/absent.
    """
    N = len(conso)

    if contrat_kw <= 0:
        raise ValueError("contrat_kw (Excel N16) est nul ou absent.")

    if e_max <= 0 or p_max <= 0:
        # Pas de batterie utilisable : repli sur le meme calcul simplifie que
        # l'heuristique, pour rester coherent avec _solve_day_heuristic_arrays.
        return _dispatch_no_battery_arrays(conso, pv, price_import, price_export,
                                            contrat_kw, peak_so_far_kw, dt=dt)

    sk = _LP_SKELETON if N == STEPS_PER_DAY else _build_lp_skeleton(N)

    b_balance = conso - pv
    b_soc = np.zeros(N)
    b_soc[0] = soc_init  # seul le premier pas de temps recoit le SOC de depart ; les suivants sont enchaines par la contrainte de continuite (a_soc).

    A_eq = _lp_equality_matrix(N, eta_c, eta_d)
    b_eq = np.concatenate([b_balance, b_soc])

    A_peak = sk["A_peak"]
    b_peak = np.zeros(N)

    curtail_upper = np.maximum(pv, 0.0)  # on ne peut jamais ecreter plus que ce que le PV produit a cet instant.

    # Vecteur de couts (fonction objectif) du LP, une entree par variable de
    # decision (7*N variables par pas de temps + new_peak scalaire) : charge et
    # decharge n'ont pas de cout direct (seul le bilan les contraint), imp
    # coute price_import, exp RAPPORTE price_export (d'ou le signe -), curtail
    # est gratuit, imp_overrun coute price_import * OVERRUN_PENALTY_MULT (fort
    # dissuasif), et new_peak coute demand_charge_rate (EUR/kW) -- c'est le
    # terme qui incite le LP a ecreter les pics de puissance mensuels.
    c = np.zeros(7 * N + 1)
    c[2 * N:3 * N] = price_import
    c[3 * N:4 * N] = -price_export
    c[6 * N:7 * N] = price_import * OVERRUN_PENALTY_MULT
    c[7 * N] = demand_charge_rate

    if highspy is not None:
        x = _solve_lp_highspy(N, eta_c, eta_d, c, b_eq, p_max, e_max, contrat_kw,
                              curtail_upper, peak_so_far_kw, dt)
    else:
        x = _solve_lp_scipy(c, A_peak, b_peak, A_eq, b_eq, N, p_max, e_max, contrat_kw,
                            curtail_upper, peak_so_far_kw, dt)

    # Decoupage du vecteur solution x selon l'ordre etabli dans _build_lp_skeleton :
    # charge, decharge, imp, exp, soc, curtail, imp_overrun, new_peak (scalaire final).
    charge, decharge = x[0:N], x[N:2 * N]
    imp, exp, soc = x[2 * N:3 * N], x[3 * N:4 * N], x[4 * N:5 * N]
    curtail = x[5 * N:6 * N]
    imp_overrun = x[6 * N:7 * N]
    new_peak = x[7 * N]
    day_cost = float((imp * price_import - exp * price_export).sum()
                      + (imp_overrun * price_import * OVERRUN_PENALTY_MULT).sum())
    arrays = {
        "charge_kwh": charge, "decharge_kwh": decharge,
        "import_kwh": imp, "export_kwh": exp, "soc_kwh": soc,
        "curtail_kwh": curtail,
    }
    return arrays, soc[-1], new_peak, day_cost


def _lp_not_converged(message, contrat_kw, p_max, e_max):
    """Construit l'exception RuntimeError levee quand le LP journalier (scipy ou HiGHS) ne trouve pas de solution optimale, avec un message pointant vers les parametres les plus probablement en cause."""
    return RuntimeError(
        f"LP journalier n'a pas converge : {message}. "
        f"Verifie contrat_kw ({contrat_kw} kW), battery_power_kw ({p_max} kW) et "
        f"battery_capacity_kwh ({e_max} kWh) vs les pics reels de conso/PV.")


def _solve_lp_scipy(c, A_peak, b_peak, A_eq, b_eq, N, p_max, e_max, contrat_kw,
                    curtail_upper, peak_so_far_kw, dt):
    """Repli si highspy n'est pas installe : meme LP via scipy.optimize.linprog."""
    bounds = (
        [(0, p_max * dt)] * N
        + [(0, p_max * dt)] * N
        + [(0, contrat_kw * dt)] * N
        + [(0, contrat_kw * dt)] * N
        + [(0, e_max)] * N
        + [(0, curtail_upper[t]) for t in range(N)]
        + [(0, None)] * N  # imp_overrun : depassement d'import, non borne mais penalise
        + [(peak_so_far_kw, None)]
    )

    res = linprog(c, A_ub=A_peak, b_ub=b_peak, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if not res.success:
        raise _lp_not_converged(res.message, contrat_kw, p_max, e_max)
    return res.x


# Modele HiGHS reutilise d'un jour a l'autre (seuls couts, bornes et second
# membre changent) : evite la couche de validation de scipy.linprog, qui
# representait plus de la moitie du temps de resolution. Un modele par thread
# (Streamlit execute chaque session dans son propre thread ; un objet Highs
# n'est pas partage entre threads) et par (N, eta_c, eta_d).
_HIGHS_LOCAL = threading.local()


def _highs_day_model(n: int, eta_c: float, eta_d: float):
    """
    Recupere (en le creant au besoin) le modele HiGHS mis en cache pour ce
    thread et cette clef (n, eta_c, eta_d) : la structure du LP (matrice de
    contraintes, nombre de lignes/colonnes) est figee une fois pour toutes,
    seuls couts/bornes/second membre changeront a chaque appel de
    _solve_lp_highspy (voir h.changeCols*/changeRowsBounds). Le cache est
    stocke dans un threading.local() (_HIGHS_LOCAL) : un objet Highs n'est pas
    partageable entre threads, et Streamlit execute chaque session utilisateur
    dans son propre thread.
    """
    models = getattr(_HIGHS_LOCAL, "models", None)
    if models is None:
        models = _HIGHS_LOCAL.models = {}
    key = (n, eta_c, eta_d)
    model = models.get(key)
    if model is None:
        sk = _LP_SKELETON if n == STEPS_PER_DAY else _build_lp_skeleton(n)
        # Meme ordre de lignes que scipy.linprog : inegalites (pic) puis egalites.
        a = sparse.vstack([sk["A_peak"], _lp_equality_matrix(n, eta_c, eta_d)], format="csc")
        n_col, n_row = 7 * n + 1, a.shape[0]
        lp = highspy.HighsLp()
        lp.num_col_ = n_col
        lp.num_row_ = n_row
        # Couts/bornes initialises a zero ici : ce sont des valeurs de
        # placeholder, ecrasees a chaque jour par _solve_lp_highspy via
        # changeColsCost/changeColsBounds/changeRowsBounds avant chaque run().
        lp.col_cost_ = np.zeros(n_col)
        lp.col_lower_ = np.zeros(n_col)
        lp.col_upper_ = np.zeros(n_col)
        lp.row_lower_ = np.zeros(n_row)
        lp.row_upper_ = np.zeros(n_row)
        lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
        lp.a_matrix_.start_ = a.indptr
        lp.a_matrix_.index_ = a.indices
        lp.a_matrix_.value_ = a.data
        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        h.passModel(lp)
        model = (h, np.arange(n_col, dtype=np.int32), np.arange(n_row, dtype=np.int32))
        models[key] = model
    return model


def _solve_lp_highspy(N, eta_c, eta_d, c, b_eq, p_max, e_max, contrat_kw,
                      curtail_upper, peak_so_far_kw, dt):
    """
    Resout le meme LP journalier que _solve_lp_scipy mais via l'API bas
    niveau de highspy directement sur le modele mis en cache (_highs_day_model),
    en ne reenvoyant que ce qui change d'un jour a l'autre (couts, bornes,
    second membre) -- evite la couche de validation/reconstruction de
    scipy.optimize.linprog, qui dominait le temps de calcul (~2x plus rapide).
    """
    h, col_idx, row_idx = _highs_day_model(N, eta_c, eta_d)
    inf = highspy.kHighsInf

    # Bornes des variables (colonnes), dans l'ordre du skeleton : charge et
    # decharge <= p_max*dt (puissance batterie max sur un pas), imp et exp
    # <= contrat_kw*dt (puissance souscrite max sur un pas), soc <= e_max
    # (capacite batterie en kWh), curtail <= pv du pas (curtail_upper).
    col_lower = np.zeros(7 * N + 1)
    col_lower[7 * N] = peak_so_far_kw  # new_peak ne peut jamais redescendre sous le pic deja atteint plus tot dans le mois.
    col_upper = np.empty(7 * N + 1)
    col_upper[0:2 * N] = p_max * dt
    col_upper[2 * N:4 * N] = contrat_kw * dt
    col_upper[4 * N:5 * N] = e_max
    col_upper[5 * N:6 * N] = curtail_upper
    col_upper[6 * N:] = inf  # imp_overrun (non borne mais penalise) et new_peak

    # Bornes des lignes de contraintes : les N premieres (A_peak) sont des
    # inegalites <= 0 (borne inf -infini), les suivantes (A_eq, bilan + SOC)
    # sont des egalites strictes bornees des deux cotes par b_eq.
    row_lower = np.concatenate([np.full(N, -inf), b_eq])
    row_upper = np.concatenate([np.zeros(N), b_eq])

    # Pas de demarrage a chaud : reutiliser la base du jour precedent change
    # l'optimum retenu quand plusieurs sont equivalents (ex: export a prix nul
    # vs ecretement), donc les chiffres affiches -- on repart a froid comme
    # scipy.linprog pour garder des resultats identiques.
    h.clearSolver()
    h.changeColsCost(len(col_idx), col_idx, c)
    h.changeColsBounds(len(col_idx), col_idx, col_lower, col_upper)
    h.changeRowsBounds(len(row_idx), row_idx, row_lower, row_upper)
    h.run()
    status = h.getModelStatus()
    if status != highspy.HighsModelStatus.kOptimal:
        raise _lp_not_converged(h.modelStatusToString(status), contrat_kw, p_max, e_max)
    return np.asarray(h.getSolution().col_value)


def solve_day_dispatch_heuristic(conso, pv, price_import, price_export,
                                  p_max, e_max, contrat_kw,
                                  soc_init, peak_so_far_kw, demand_charge_rate,
                                  eta_c=ETA_C, eta_d=ETA_D, dt=DT):
    """
    Dispatch journalier HEURISTIQUE (self-consumption, sans arbitrage prix) :
    voir _solve_day_heuristic_arrays pour le detail. Variante DataFrame de
    cette derniere -- meme contrat de retour que solve_day_dispatch.
    """
    arrays, soc, new_peak, day_cost = _solve_day_heuristic_arrays(
        conso, pv, price_import, price_export, p_max, e_max, contrat_kw,
        soc_init, peak_so_far_kw, demand_charge_rate, eta_c=eta_c, eta_d=eta_d, dt=dt)
    return _dispatch_to_frame(arrays), soc, new_peak, day_cost


def _solve_day_heuristic_arrays(conso, pv, price_import, price_export,
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
        return _dispatch_no_battery_arrays(conso, pv, price_import, price_export,
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
            # room_kwh : place encore dispo dans la batterie, exprimee en kWh
            # PRELEVES AU RESEAU/PV (pas en kWh stockes) -- on divise par eta_c
            # car charger room_kwh*eta_c kWh dans la batterie en necessite
            # room_kwh en entree (pertes de conversion a la charge).
            room_kwh = max(e_max - soc, 0.0) / eta_c
            c = min(net, p_step, room_kwh)  # charge limitee par le surplus dispo, la puissance max batterie (p_step) et la place restante.
            charge[t] = c
            soc += c * eta_c  # seule une fraction eta_c de l'energie prelevee est effectivement stockee.
            remaining = net - c
            e = min(remaining, export_cap)
            exp[t] = e
            curtail[t] = remaining - e  # surplus non charge ni exportable (au-dela du contrat) : perdu.
        elif net < 0:
            deficit = -net
            available_kwh = soc * eta_d  # energie restituable au reseau/conso depuis le SOC actuel, compte tenu des pertes de decharge.
            d = min(deficit, p_step, available_kwh)  # decharge limitee par le deficit a couvrir, la puissance max batterie et l'energie dispo.
            decharge[t] = d
            soc -= d / eta_d  # il faut puiser d/eta_d kWh de SOC pour restituer d kWh en sortie (pertes de conversion a la decharge).
            imp[t] = min(deficit - d, import_cap)
            # au-dela de import_cap : depassement contrat, penalise ci-dessous

        soc_arr[t] = soc

    # imp_overrun recalcule a posteriori (et non au fil de la boucle) : c'est
    # le manque total (deficit_all) moins ce que la batterie a couvert
    # (decharge) moins la limite contractuelle -- equivalent a la logique
    # "au-dela de import_cap" ci-dessus, mais reformule vectoriellement une
    # fois la boucle terminee plutot que de dupliquer le calcul a chaque pas.
    deficit_all = np.maximum(-(pv - conso), 0.0)
    imp_overrun = np.maximum(deficit_all - decharge - import_cap, 0.0)
    overrun_cost = (imp_overrun * price_import * OVERRUN_PENALTY_MULT).sum()

    day_cost = float((imp * price_import - exp * price_export).sum() + overrun_cost)
    new_peak = max(peak_so_far_kw, (imp / dt).max() if N else 0.0)

    arrays = {
        "charge_kwh": charge, "decharge_kwh": decharge,
        "import_kwh": imp, "export_kwh": exp, "soc_kwh": soc_arr,
        "curtail_kwh": curtail,
    }
    return arrays, soc, new_peak, day_cost


def run_year_dispatch(conso_all, pv_all, price_import_all, price_export_all,
                       p_max, e_max, contrat_kw, demand_charge_rate,
                       index, return_detail=False, dispatch_fn=solve_day_dispatch):
    """
    Enchaine le dispatch jour par jour (via dispatch_fn, LP ou heuristique)
    sur toute la periode couverte par conso_all/pv_all (tableaux en kWh par
    pas de temps quart-horaire, longueur multiple de STEPS_PER_DAY -- les
    eventuels pas restants en fin de periode sont ignores, cf. n_days).
    Le SOC est reporte d'un jour sur l'autre (soc_init du jour suivant = soc
    final du jour precedent), mais reinitialise a e_max * SOC_INIT_RATIO en
    tout debut d'appel -- chaque appel demarre donc une "annee" fraiche.
    Le pic de puissance (peak_so_far_kw) est en revanche remis a zero a
    chaque changement de mois calendaire (voir month_peaks), pour calculer un
    tarif capacitaire mensuel independant d'un mois a l'autre.
    Retourne (cout energie EUR, cout capacitaire total EUR, import total kWh,
    export total kWh, ecretement total kWh), et en plus un DataFrame detaille
    quart-horaire si return_detail=True.
    """
    n_days = len(conso_all) // STEPS_PER_DAY
    soc = e_max * SOC_INIT_RATIO
    current_month, peak_so_far = None, 0.0
    month_peaks = {}
    total_energy_cost = 0.0
    total_export = 0.0
    total_import = 0.0
    total_curtail = 0.0
    day_arrays = [] if return_detail else None
    months = np.asarray(index.month)

    # Les fonctions de dispatch connues ont une variante interne qui renvoie
    # des tableaux numpy : evite de construire puis relire un DataFrame par
    # jour (c'etait ~60 % du temps en mode heuristique). Une dispatch_fn
    # externe (tests, variante) passe toujours par le contrat DataFrame.
    arrays_fn = _ARRAY_DISPATCH_IMPLS.get(dispatch_fn)

    for d in range(n_days):
        sl = slice(d * STEPS_PER_DAY, (d + 1) * STEPS_PER_DAY)
        month = months[d * STEPS_PER_DAY]
        if month != current_month:
            # Nouveau mois calendaire : on fige le pic du mois qui vient de se
            # terminer (utilise plus bas pour calculer demand_charge_total) et
            # on repart de zero pour le pic du nouveau mois.
            if current_month is not None:
                month_peaks[current_month] = peak_so_far
            current_month = month
            peak_so_far = 0.0

        day_args = (conso_all[sl], pv_all[sl], price_import_all[sl], price_export_all[sl])
        day_kwargs = dict(p_max=p_max, e_max=e_max, contrat_kw=contrat_kw,
                          soc_init=soc, peak_so_far_kw=peak_so_far,
                          demand_charge_rate=demand_charge_rate)
        if arrays_fn is not None:
            day, soc, new_peak, day_cost = arrays_fn(*day_args, **day_kwargs)
        else:
            day_df, soc, new_peak, day_cost = dispatch_fn(*day_args, **day_kwargs)
            day = {col: day_df[col].to_numpy() for col in DISPATCH_COLUMNS}
        peak_so_far = new_peak  # SOC (via soc_init ci-dessus) ET pic de puissance sont reportes au jour suivant.
        total_energy_cost += day_cost
        total_export += float(day["export_kwh"].sum())
        total_import += float(day["import_kwh"].sum())
        total_curtail += float(day["curtail_kwh"].sum())

        if return_detail:
            day_arrays.append(day)

    month_peaks[current_month] = peak_so_far  # fige aussi le pic du tout dernier mois (jamais ferme par la boucle ci-dessus).
    demand_charge_total = demand_charge_rate * sum(month_peaks.values())  # EUR/kW * somme des pics mensuels (kW) = cout capacitaire annuel total.

    if return_detail:
        n_pts = n_days * STEPS_PER_DAY
        full_dispatch = pd.DataFrame(
            {col: (np.concatenate([day[col] for day in day_arrays]) if day_arrays else np.zeros(0))
             for col in DISPATCH_COLUMNS},
            index=index[:n_pts])
        full_dispatch["price_import"] = price_import_all[:n_pts]
        full_dispatch["price_export"] = price_export_all[:n_pts]
        return total_energy_cost, demand_charge_total, total_import, total_export, total_curtail, full_dispatch
    return total_energy_cost, demand_charge_total, total_import, total_export, total_curtail


_ARRAY_DISPATCH_IMPLS = {
    solve_day_dispatch: _solve_day_lp_arrays,
    solve_day_dispatch_heuristic: _solve_day_heuristic_arrays,
}


def decompose_gains(conso_all, pv_all, price_import_all, price_export_all,
                     p_max, e_max, contrat_kw, demand_charge_rate, index,
                     scenario_c_precomputed=None, dispatch_fn=solve_day_dispatch):
    """
    Decompose le gain total (annee 1) en deux contributions distinctes, en
    comparant trois scenarios contrefactuels sur la meme periode :
      - A : "tout reseau" -- ni PV ni batterie (pv force a 0, p_max=e_max=0).
      - B : "PV seul" -- PV branche mais pas de batterie (p_max=e_max=0).
      - C : "PV + batterie" -- systeme complet (le scenario reel).
    gain_pv = A - B isole l'effet du PV seul (autoconsommation directe,
    sans stockage), gain_batterie = B - C isole l'effet ADDITIONNEL de la
    batterie (arbitrage/ecretement par-dessus le PV). Cette decomposition
    permet d'afficher separement au client la part de son economie due au PV
    et celle due a la batterie. scenario_c_precomputed permet de reutiliser
    un dispatch complet (cout, import, export) deja calcule par ailleurs pour
    le scenario C, au lieu de refaire les 365 jours de LP/heuristique une
    deuxieme fois pour rien.
    """
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
    """
    Affiche (print) et retourne un bilan energetique synthetique de l'annee 1
    (kWh produits/importes/exportes, cycles batterie equivalents, prix moyens
    reellement payes/recus). detail_precompute est le DataFrame quart-horaire
    deja calcule par run_year_dispatch(..., return_detail=True) : cette
    fonction ne relance aucun dispatch, elle ne fait qu'agreger des colonnes
    deja produites ailleurs.
    """
    detail = detail_precompute

    pv_total = float(pv_all.sum())
    conso_total = float(conso_all.sum())
    part_non_importee = conso_total - total_import  # consommation couverte par PV et/ou batterie plutot qu'importee du reseau.
    pv_exporte = pv_total - part_non_importee  # approximation : suppose que toute la conso non-importee vient du PV direct (ignore les cycles de charge/decharge batterie, negligeables sur un bilan annuel).

    charge_total = float(detail["charge_kwh"].sum())
    decharge_total = float(detail["decharge_kwh"].sum())
    cycles_equivalents = decharge_total / e_max if e_max > 0 else 0.0  # 1 cycle equivalent = decharger l'equivalent de la pleine capacite une fois, utile pour estimer l'usure/duree de vie batterie.

    if e_max > 0:
        # Fraction du temps ou la batterie est (quasi) pleine/vide (seuils a
        # 98%/2% pour absorber le bruit numerique du LP) : indicateur de
        # dimensionnement (batterie trop petite si souvent pleine, trop
        # grande si souvent vide).
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
    """
    Calcule ce que le client payait AVANT le systeme PV+batterie (annee 1),
    sur la base de sa consommation totale (conso_all, en kWh par pas de
    temps) et de son ancien tarif reseau : soit fixe (old_price_kwh_fixe),
    soit HP/HC (price_hp/price_hc selon hour_all et les heures de bascule),
    plus les frais fixes de contrat et, si applicable, le tarif capacitaire
    sur son pic de puissance mensuel non mitige (voir commentaire ci-dessous).
    Retourne un montant en EUR/an. N'est jamais appele en mode
    "fournisseur_secondaire" (voir _run_fournisseur_model_impl).
    """
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


def demand_charge_rate_for_mode(mode_vente: str, tarif_capacitaire_fournisseur: float) -> float:
    """
    Tarif capacitaire (peak shaving) a appliquer au dispatch, selon le mode
    de vente. Retire (0.0) UNIQUEMENT en mode "fournisseur_secondaire"
    (demande utilisateur) : annule a la fois son cout (dont_pointe_eur) et
    l'incitation d'ecretement de pointe dans le LP (c[6*N] dans
    solve_day_dispatch) pour ce mode-la seulement -- les autres modes
    (fournisseur_principal, vente_directe) gardent le tarif capacitaire
    normalement, tout comme client_old_cost.

    Regle centralisee ici (au lieu de dupliquee dans _run_fournisseur_model_impl
    et _dispatch_one_year) pour qu'une evolution future de cette regle metier
    n'ait qu'un seul endroit a modifier.
    """
    return 0.0 if mode_vente == "fournisseur_secondaire" else tarif_capacitaire_fournisseur


def prix_vente_annee(prix_base: float, year: int, revision_pct: float = 0.0,
                      periode_revision_annees: int = 5) -> float:
    """
    Applique une revision PAR PALIERS (pas une inflation continue annee par
    annee) au prix de vente fixe au client : le prix reste constant pendant
    periode_revision_annees, puis saute de revision_pct, etc. Par exemple avec
    periode_revision_annees=5 et revision_pct=0.1, le prix est le meme pour
    les annees 1 a 5, puis +10% pour les annees 6 a 10, etc. Retourne
    prix_base inchange si revision_pct vaut 0 ou si periode_revision_annees
    est None (pas de revision contractuelle).
    """
    if revision_pct == 0.0 or periode_revision_annees is None:
        return prix_base
    n_revisions = (year - 1) // periode_revision_annees  # division entiere : nombre de paliers de revision deja franchis avant cette annee.
    return prix_base * (1 + revision_pct) ** n_revisions


def run_fournisseur_model(xlsm_path=XLSM_PATH, dayahead_pkl_path=DAYAHEAD_PKL_PATH,
                           horizon_annees=HORIZON_ANNEES,
                           discount_rate=DISCOUNT_RATE_DEFAULT,
                           annee_remplacement_batterie=13,
                           cout_remplacement_batterie_eur=25000,
                           revision_prix_pct=0.0,
                           periode_revision_annees=5,
                           parallel_years=None,
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

    router = _install_thread_log_router()
    log_file = open(rapport_txt_path, "w", encoding="utf-8")
    router.local.log = log_file
    try:
        return _run_fournisseur_model_impl(
            xlsm_path, dayahead_pkl_path, horizon_annees, discount_rate,
            annee_remplacement_batterie, cout_remplacement_batterie_eur,
            revision_prix_pct, periode_revision_annees, parallel_years,
            progress_callback, param_overrides, data_source, output_csv_path)
    finally:
        router.local.log = None
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


@dataclass(frozen=True)
class YearDispatchContext:
    """
    Donnees/parametres COMMUNS a toutes les annees de l'horizon de simulation
    (contrairement a `year`, qui varie a chaque appel de _dispatch_one_year).
    Remplace l'ancien tuple positionnel a 20 elements ("common_args") --
    un reordonnancement des champs ne peut plus casser silencieusement le
    calcul, et chaque champ est nomme explicitement aux deux bouts.
    """
    conso_all: np.ndarray
    pv_all_year1: np.ndarray
    market_price_values: np.ndarray
    params: dict
    index: pd.DatetimeIndex
    p_max: float
    contrat_kw: float
    e_max_year1: float
    degr_pv: float
    degr_batt: float
    inflation: float
    annee_remplacement_batterie: Optional[int]
    cout_remplacement_batterie_eur: Optional[float]
    revenue_client_year1: float
    old_cost_year1: float
    revision_prix_pct: float
    periode_revision_annees: int
    use_belpex: bool
    price_import_year1: np.ndarray
    price_export_year1: np.ndarray
    dispatch_fn: Callable
    # (energy_cost, demand_charge_total, import, export, curtail) du dispatch
    # annee 1 deja calcule pour le detail quart-horaire : reutilise tel quel
    # par _dispatch_one_year(1) au lieu de refaire les 365 LP a l'identique.
    year1_dispatch: Optional[tuple] = None


def _dispatch_one_year(year: int, ctx: YearDispatchContext):
    """
    Calcule la ligne de resultat economique pour UNE annee donnee de
    l'horizon (year va de 1 a horizon_annees), a partir des donnees/parametres
    communs a toutes les annees (ctx). C'est ici que sont appliquees, annee
    apres annee : la degradation de la production PV (degr_pv), la
    degradation ET l'eventuel remplacement de la batterie (degr_batt,
    annee_remplacement_batterie), l'inflation sur les couts et tarifs
    (inflation), et l'eventuelle revision du prix de vente au client
    (revision_prix_pct). Concu pour etre appelable depuis un processus
    separe (voir _dispatch_years / ProcessPoolExecutor) : ne depend d'aucun
    etat global mutable, uniquement de `year` et `ctx` (tous deux picklables).
    Retourne un dict de resultats (une ligne du DataFrame final `detail_annuel`).
    """
    conso_all = ctx.conso_all
    pv_all_year1 = ctx.pv_all_year1
    market_price_values = ctx.market_price_values
    params = ctx.params
    index = ctx.index
    p_max = ctx.p_max
    contrat_kw = ctx.contrat_kw
    e_max_year1 = ctx.e_max_year1
    degr_pv = ctx.degr_pv
    degr_batt = ctx.degr_batt
    inflation = ctx.inflation
    annee_remplacement_batterie = ctx.annee_remplacement_batterie
    cout_remplacement_batterie_eur = ctx.cout_remplacement_batterie_eur
    revenue_client_year1 = ctx.revenue_client_year1
    old_cost_year1 = ctx.old_cost_year1
    revision_prix_pct = ctx.revision_prix_pct
    periode_revision_annees = ctx.periode_revision_annees
    use_belpex = ctx.use_belpex
    price_import_year1 = ctx.price_import_year1
    price_export_year1 = ctx.price_export_year1
    dispatch_fn = ctx.dispatch_fn

    # Degradation PV composee annee par annee : degr_pv est un taux annuel
    # (ex. 0.5%/an), applique en exposant (year-1) pour que l'annee 1 utilise
    # la production nominale pv_all_year1 telle quelle (facteur = 1).
    pv_year = pv_all_year1 * ((1 - degr_pv) ** (year - 1))

    if annee_remplacement_batterie is not None and year >= annee_remplacement_batterie:
        # La batterie a ete remplacee : son "age" redemarre a 0 l'annee du
        # remplacement (une batterie neuve n'a pas la degradation cumulee de
        # l'ancienne), d'ou la degradation appliquee ci-dessous repartant
        # de e_max_year1 (capacite nominale d'origine, supposee identique
        # pour la batterie de remplacement).
        age_batterie = year - annee_remplacement_batterie
    else:
        age_batterie = year - 1
    e_max_year = e_max_year1 * ((1 - degr_batt) ** age_batterie)

    inflation_factor = (1 + inflation) ** (year - 1)
    demand_charge_year = demand_charge_rate_for_mode(
        params.get("mode_vente"), params["tarif_capacitaire_fournisseur"]) * inflation_factor
    maintenance_year = params["maintenance_eur_an"] * inflation_factor

    if use_belpex:
        # Mode "fournisseur_principal" : marge, taxes et marge d'injection
        # sont indexes sur l'inflation comme n'importe quel cout/tarif fixe.
        # Le prix de marche Day-Ahead lui-meme (mp_year) n'est indexe que si
        # APPLIQUER_INFLATION_AU_PRIX_MARCHE est active (False par defaut :
        # le prix de gros n'est pas suppose suivre l'inflation domestique).
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

    # Annee 1 : entrees strictement identiques au dispatch deja fait pour le
    # detail (facteurs d'inflation/degradation = 1) -- on verifie l'egalite
    # exacte des prix par securite avant de reutiliser le resultat.
    reuse_year1 = (
        year == 1 and ctx.year1_dispatch is not None
        and e_max_year == e_max_year1
        and np.array_equal(price_import_year, price_import_year1)
        and np.array_equal(price_export_year, price_export_year1)
    )
    if reuse_year1:
        energy_cost, demand_charge_total, total_import, total_export, total_curtail = ctx.year1_dispatch
    else:
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
        # Modes "fournisseur_principal" et "vente_directe" : le fournisseur
        # (ou le client, en vente_directe) vend TOUTE la consommation du
        # client (pas seulement le PV) au prix fixe prix_vente_kwh, et
        # s'approvisionne au cout reel du dispatch (energy_cost) + tarif
        # capacitaire + maintenance + frais fixes reseau.
        frais_fixes_reseau_year = params["old_cout_additionnel_contrat"] * inflation_factor
        cout_appro_total = energy_cost + demand_charge_total + maintenance_year + frais_fixes_reseau_year

        # Optimisation : si aucune revision de prix n'est configuree
        # (revision_prix_pct falsy), le revenu client est constant sur tout
        # l'horizon -> on reutilise directement revenue_client_year1 au lieu
        # de refaire le meme calcul (prix_vente_annee renverrait de toute
        # facon prix_base inchange, mais ceci evite le recalcul de la somme).
        revenue_client_year = prix_vente_annee(
            params["prix_vente_kwh"], year, revision_prix_pct, periode_revision_annees
        ) * float(conso_all.sum()) if revision_prix_pct else revenue_client_year1
        old_cost_year = old_cost_year1 * inflation_factor
        dont_energie_eur = energy_cost

    # Le cout de remplacement de la batterie n'est impute qu'a l'annee exacte
    # du remplacement (capex ponctuel, pas amorti sur plusieurs annees).
    capex_remplacement_year = (
        cout_remplacement_batterie_eur
        if (annee_remplacement_batterie is not None and year == annee_remplacement_batterie)
        else 0.0
    )

    gross_profit_year = revenue_client_year - cout_appro_total - capex_remplacement_year
    client_savings_year = old_cost_year - revenue_client_year  # ce que le client economise cette annee-la par rapport a son ancien fournisseur/tarif.

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


# Nombre maximal de processus pour le calcul des annees en parallele : chaque
# processus charge numpy/scipy/pandas (~100 Mo), inutile d'en lancer plus que
# necessaire -- 8 couvre un horizon de 15 ans en 2 vagues.
MAX_YEAR_WORKERS = 8


def _should_parallelize_years(parallel_years, dispatch_fn, p_max, e_max, n_years: int) -> bool:
    """
    parallel_years : True/False force le choix ; None = automatique. En
    automatique, on ne parallelise que le dispatch LP avec batterie (~2 s par
    annee) : l'heuristique et le cas sans batterie prennent quelques
    centiemes de seconde par annee, moins que le demarrage des processus.
    """
    if n_years < 2 or (os.cpu_count() or 1) < 2:
        return False
    if parallel_years is not None:
        return bool(parallel_years)
    return dispatch_fn is solve_day_dispatch and p_max > 0 and e_max > 0


def _dispatch_years(years, ctx: YearDispatchContext, parallel: bool, on_year_done=None) -> dict:
    """
    Calcule _dispatch_one_year pour chaque annee, en parallele si demande.
    Les annees sont independantes (chacune repart de SOC_INIT_RATIO), donc le
    resultat est identique a un calcul sequentiel. Retourne {annee: ligne}.
    on_year_done(n_faites) est appele dans le processus principal.
    """
    rows = {}
    if parallel:
        n_workers = min(len(years), MAX_YEAR_WORKERS, max((os.cpu_count() or 2) - 1, 1))
        try:
            with ProcessPoolExecutor(max_workers=n_workers) as pool:
                futures = {pool.submit(_dispatch_one_year, y, ctx): y for y in years}
                for fut in as_completed(futures):
                    rows[futures[fut]] = fut.result()
                    if on_year_done:
                        on_year_done(len(rows))
            return rows
        except (BrokenProcessPool, PicklingError, OSError) as e:
            # Environnement sans multiprocessing fonctionnel : repli sequentiel.
            print(f"  /!\\ Calcul parallele indisponible ({e!r}) -- repli sur un calcul sequentiel.")
            rows = {}
    for y in years:
        rows[y] = _dispatch_one_year(y, ctx)
        if on_year_done:
            on_year_done(len(rows))
    return rows


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
    # injectee. Comme la degradation PV annee par annee ne fait que reduire
    # la production (jamais l'augmenter), un seul ecretement ici suffit pour
    # tout l'horizon (voir _dispatch_one_year : pv_year = pv_all_year1 * degr).
    #
    # /!\ APPROXIMATION VOLONTAIRE : kva_onduleur (kVA, puissance APPARENTE)
    # est utilise directement comme une puissance ACTIVE en kW, ce qui revient
    # a supposer un facteur de puissance (cos phi) egal a 1. C'est une
    # simplification standard pour ce type de modele, correcte pour la
    # grande majorite des onduleurs PV (cos phi proche de 1 en fonctionnement
    # normal). Si l'onduleur reel impose une limitation de puissance reactive
    # notable (cos phi < 1 impose par le gestionnaire de reseau, par exemple),
    # la puissance active reellement disponible est LEGEREMENT INFERIEURE a
    # kva_onduleur, et l'ecretement calcule ici sous-estime alors (un peu) la
    # perte de production reelle. Pas de parametre facteur de puissance
    # separe pour l'instant (choix produit : garder le modele simple) --
    # a garder en tete si un client a un onduleur avec cos phi impose bas.
    kva_onduleur = params.get("kva_onduleur") or 0.0
    if kva_onduleur > 0:
        pv_cap_kwh_per_step = kva_onduleur * DT
        pv_all_year1 = np.minimum(pv_all_year1_raw, pv_cap_kwh_per_step)
        pv_clip_onduleur_kwh = float((pv_all_year1_raw - pv_all_year1).sum())
        if pv_clip_onduleur_kwh > 0:
            print(f"  /!\\ Ecretement onduleur : {pv_clip_onduleur_kwh:,.0f} kWh/an de production PV "
                  f"perdue car superieure a la puissance de l'onduleur ({kva_onduleur:.1f} kVA, "
                  f"approximation cos phi = 1 -- voir commentaire dans le code).")
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
    demand_charge_rate = demand_charge_rate_for_mode(mode_vente, params["tarif_capacitaire_fournisseur"])
    inflation = params["inflation_pct"] / 100.0
    degr_pv = params["degradation_pv_pct"] / 100.0
    degr_batt = params["degradation_batterie_pct"] / 100.0

    capex_pv = params["kwc"] * params["prix_kwc_pv"]  # kWc installe * EUR/kWc.
    capex_batterie = params["battery_capacity_kwh"] * params["prix_kwh_batterie"]  # kWh de capacite installee * EUR/kWh.
    capex_total = capex_pv + capex_batterie

    if cout_remplacement_batterie_eur is None:
        # Par defaut, le remplacement de la batterie coute autant que
        # l'installation initiale (meme prix_kwh_batterie, meme capacite) --
        # l'appelant peut fournir un montant different si le prix futur
        # anticipe de la technologie batterie differe du prix actuel.
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

    year_ctx = YearDispatchContext(
        conso_all=conso_all, pv_all_year1=pv_all_year1, market_price_values=market_price_values,
        params=params, index=index, p_max=p_max, contrat_kw=contrat_kw, e_max_year1=e_max_year1,
        degr_pv=degr_pv, degr_batt=degr_batt, inflation=inflation,
        annee_remplacement_batterie=annee_remplacement_batterie,
        cout_remplacement_batterie_eur=cout_remplacement_batterie_eur,
        revenue_client_year1=revenue_client_year1, old_cost_year1=old_cost_year1,
        revision_prix_pct=revision_prix_pct, periode_revision_annees=periode_revision_annees,
        use_belpex=use_belpex, price_import_year1=price_import_year1,
        price_export_year1=price_export_year1, dispatch_fn=dispatch_fn,
        year1_dispatch=(energy_cost_y1, demand_charge_y1, import_y1, export_y1, curtail_y1),
    )

    # Annee 1 : passe par _dispatch_one_year comme toutes les autres annees
    # (formule unique, pas de duplication), mais y reutilise le dispatch deja
    # calcule ci-dessus (year1_dispatch) au lieu de le refaire a l'identique.
    years = list(range(1, horizon_annees + 1))
    parallel = _should_parallelize_years(parallel_years, dispatch_fn, p_max, e_max_year1, horizon_annees - 1)
    if parallel:
        print("  (annees calculees en parallele)")
    rows_by_year = _dispatch_years(
        years, year_ctx, parallel,
        on_year_done=(lambda n: progress_callback(n, horizon_annees)) if progress_callback else None)
    rows = [rows_by_year[y] for y in years]
    label = "economie client" if mode_vente == "vente_directe" else "gain brut fournisseur"
    for row in rows:
        valeur_log = row['economie_client_eur'] if mode_vente == "vente_directe" else row['gain_brut_fournisseur_eur']
        print(f"  Annee {row['annee']:>2} : {label} = {valeur_log:>10,.0f} EUR")

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

    # Temps de retour sur investissement (payback), en annees FRACTIONNAIRES :
    # on cherche la premiere annee ou le cashflow cumule (net du CAPEX) devient
    # positif ou nul, puis on interpole LINEAIREMENT entre le cumul de l'annee
    # precedente (encore negatif) et celui de cette annee-la pour estimer a
    # quel moment DANS l'annee le seuil de rentabilite est franchi (frac est la
    # fraction d'annee ecoulee au moment du franchissement). payback_year reste
    # None si le cashflow cumule ne redevient jamais positif sur tout l'horizon.
    payback_year = None
    for i, r in result.iterrows():
        if r["cashflow_cumule_eur"] >= 0:
            prev_cum = result.loc[i - 1, "cashflow_cumule_eur"] if i > 0 else -capex_total
            frac = -prev_cum / (r["cashflow_cumule_eur"] - prev_cum) if r["cashflow_cumule_eur"] != prev_cum else 0
            payback_year = (i - 1 + 1) + frac if i > 0 else frac
            break

    total_gain_brut = result["cashflow_annuel_eur"].sum()
    roi_pct = 100 * (total_gain_brut - capex_total) / capex_total if capex_total > 0 else float("nan")  # ROI net NON actualise (ne tient pas compte de la valeur temps de l'argent, contrairement a la VAN/TRI ci-dessous).
    total_economie_client = result["economie_client_eur"].sum()

    # VAN (NPV) et TRI (IRR) calcules sur la sequence de cashflows : -CAPEX a
    # l'annee 0, puis le cashflow annuel net (deja net du remplacement
    # batterie eventuel, voir cashflow_annuel_eur) pour chaque annee de
    # l'horizon -- voir engine.finance_utils.compute_npv_irr pour le detail
    # de calcul (actualisation au taux discount_rate, recherche de racine
    # pour le TRI).
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
