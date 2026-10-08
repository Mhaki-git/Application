"""
Extrait les profils-types de consommation quart-horaire depuis l'onglet
"CONSOMMATION" de ton fichier Excel modele (colonnes D a L), et les
sauvegarde en dur dans data/conso_profiles.pkl.

A EXECUTER UNE SEULE FOIS (ou a chaque fois que tu changes ces profils dans
l'Excel). L'appli Streamlit relit ensuite directement ce fichier .pkl, sans
jamais rouvrir le xlsm pour ca.

Chaque profil est une serie normalisee (fraction de la consommation annuelle
par quart d'heure, somme = 1 sur l'annee) -- l'appli la met a l'echelle avec
la consommation annuelle cible (kWh/an) saisie par l'utilisateur.

--------------------------------------------------------------------------
UTILISATION :
--------------------------------------------------------------------------
    python extract_conso_profiles_from_excel.py "ENERGY MIX ANALYSIS - AMELIORATION (2).xlsm"
"""
import sys
import os
import openpyxl
import pandas as pd
import numpy as np

SHEET_NAME = "CONSOMMATION"
MAX_SCAN_ROWS_QH = 40000
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(_APP_ROOT, "data")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "conso_profiles.pkl")

# Colonnes D a L (1-indexees) -> cle courte utilisee dans l'appli.
PROFILE_COLUMNS = {
    4: "residentiel_famille_enfants_scolarises",
    5: "residentiel_famille_jeunes_enfants",
    6: "residentiel_menage_1_2_personnes",
    7: "residentiel_retraites_ou_domicile",
    8: "residentiel_conso_concentree_nuit",
    9: "industriel_conso_constante",
    10: "industriel_conso_soiree",
    11: "industriel_semaine_conso",
    12: "industriel_commerce_heures_ouverture",
}


def hourly_values_to_quarter_hour_profile(hourly_values: np.ndarray, year: int) -> pd.Series:
    """
    Convertit 8760 (ou 8784) valeurs HORAIRES d'un profil normalise en profil
    quart-horaire complet sur l'annee civile `year`.

    Chaque heure est repartie a parts egales sur ses 4 quarts d'heure (valeur/4),
    puis le profil est renormalise a une somme de 1.0. Si la source compte 8760
    valeurs et que `year` est bissextile, le 29 fevrier reprend le 28 fevrier
    (meme repli que l'alignement PV, voir io_sources/pv_pvgis.py).
    """
    hourly_values = np.asarray(hourly_values, dtype=float)
    if len(hourly_values) not in (8760, 8784):
        raise ValueError(f"Profil horaire attendu sur 8760 ou 8784 valeurs, recu {len(hourly_values)}.")

    # Table (mois, jour, heure) -> valeur, construite sur l'annee de reference
    # 2023 (non bissextile) pour 8760 valeurs, ou sur `year` pour 8784.
    ref_year = 2023 if len(hourly_values) == 8760 else year
    ref_idx = pd.date_range(f"{ref_year}-01-01", periods=len(hourly_values), freq="h")
    lookup = pd.Series(hourly_values, index=pd.MultiIndex.from_arrays(
        [ref_idx.month, ref_idx.day, ref_idx.hour]))

    qh_index = pd.date_range(f"{year}-01-01", f"{year}-12-31 23:45", freq="15min")
    month, day, hour = qh_index.month, qh_index.day, qh_index.hour
    day = np.where((month == 2) & (day == 29) & (ref_year != year), 28, day)
    values = lookup.reindex(pd.MultiIndex.from_arrays([month, day, hour])).values / 4.0

    total = float(values.sum())
    if total <= 0:
        raise ValueError("Profil horaire nul sur toute l'annee.")
    return pd.Series(values / total, index=qh_index)


def extract_conso_profiles(xlsm_path: str) -> dict:
    """
    Lit l'onglet CONSOMMATION : dates en colonne A (a partir de la ligne 2),
    profils normalises (fraction de l'annee) en colonnes D a K.

    Retourne un dict {cle_profil: pd.Series} -- chaque serie est indexee par
    datetime quart-horaire, et somme a 1.0 sur l'annee (verifie a l'extraction).
    """
    # data_only=True : recupere les valeurs deja calculees par Excel ;
    # read_only=True : lecture en flux, necessaire vu le volume potentiel
    # (jusqu'a MAX_SCAN_ROWS_QH lignes, soit plusieurs annees au pas quart-horaire).
    wb = openpyxl.load_workbook(xlsm_path, data_only=True, read_only=True)
    ws = wb[SHEET_NAME]

    dates = []
    cols_vals = {col: [] for col in PROFILE_COLUMNS}
    max_col = max(PROFILE_COLUMNS)
    # On s'arrete des que la colonne date (A) est vide -- fin des donnees --
    # plutot que de lire systematiquement jusqu'a MAX_SCAN_ROWS_QH.
    for row in ws.iter_rows(min_row=2, max_row=1 + MAX_SCAN_ROWS_QH,
                             min_col=1, max_col=max_col, values_only=True):
        d = row[0]
        if d is None:
            break
        dates.append(d)
        for col in PROFILE_COLUMNS:
            v = row[col - 1]
            cols_vals[col].append(v if v is not None else 0.0)
    wb.close()

    if not dates:
        raise ValueError(f"Aucune date valide trouvee en colonne A de l'onglet {SHEET_NAME}.")

    index = pd.DatetimeIndex(dates)

    # Les profils de l'onglet CONSOMMATION sont des valeurs HORAIRES (8760 par
    # an) posees sur les premieres lignes d'une colonne de dates QUART-HORAIRES :
    # lus tels quels, ils ne couvrent que ~1/4 de l'annee (fin vers le 2 avril),
    # le reste etant a 0. On detecte ce cas (donnees non nulles seulement sur
    # le debut de la plage) et on reconstruit l'annee quart-horaire complete.
    last_nonzero = max(
        (int(np.flatnonzero(np.array(cols_vals[col], dtype=float))[-1]) + 1
         if np.any(np.array(cols_vals[col], dtype=float)) else 0)
        for col in PROFILE_COLUMNS)
    is_hourly = last_nonzero in (8760, 8784) and len(dates) > last_nonzero * 1.5
    if is_hourly:
        print(f"  Profils horaires detectes ({last_nonzero} valeurs sur {len(dates)} lignes) -> "
              f"conversion en quart-horaire sur l'annee {index[0].year}.")

    profiles = {}
    for col, key in PROFILE_COLUMNS.items():
        vals = np.array(cols_vals[col], dtype=float)
        if is_hourly:
            profiles[key] = hourly_values_to_quarter_hour_profile(
                vals[:last_nonzero], index[0].year).rename(key)
            continue
        series = pd.Series(vals, index=index, name=key)
        # Deduplique (garde la premiere occurrence) et trie par date, comme
        # pour le profil PV -- garantit un index propre avant mise a l'echelle.
        series = series[~series.index.duplicated(keep="first")].sort_index()
        total = float(series.sum())
        # Verification d'invariant : un profil normalise doit sommer a 1.0 sur
        # l'annee (tolerance +/-1% pour les arrondis Excel). Un ecart plus
        # important signale une colonne source mal alignee ou corrompue.
        if not (0.99 <= total <= 1.01):
            print(f"  /!\\ Profil '{key}' : somme annuelle = {total:.4f} (attendu ~1.0) -- "
                  f"verifie la colonne source dans l'Excel.")
        profiles[key] = series

    return profiles


def main():
    """
    Point d'entree CLI : lit le chemin du fichier Excel en argument, extrait
    les profils de consommation et les fige dans data/conso_profiles.pkl.
    A relancer manuellement chaque fois que les profils source changent --
    ce script n'est jamais appele par l'application Streamlit elle-meme.
    """
    if len(sys.argv) != 2:
        print("Usage : python extract_conso_profiles_from_excel.py <chemin_vers_le_xlsm>")
        sys.exit(1)

    xlsm_path = sys.argv[1]
    profiles = extract_conso_profiles(xlsm_path)

    for key, series in profiles.items():
        print(f"  -> {key} : {len(series)} points, de {series.index[0]} a {series.index[-1]}, "
              f"somme = {series.sum():.4f}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    pd.to_pickle(profiles, OUTPUT_PATH)
    print(f"\nSauvegarde : {OUTPUT_PATH}")
    print("L'appli pourra desormais proposer ces profils integres en plus de l'upload CSV.")


if __name__ == "__main__":
    main()
