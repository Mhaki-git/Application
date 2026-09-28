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


def extract_conso_profiles(xlsm_path: str) -> dict:
    """
    Lit l'onglet CONSOMMATION : dates en colonne A (a partir de la ligne 2),
    profils normalises (fraction de l'annee) en colonnes D a K.

    Retourne un dict {cle_profil: pd.Series} -- chaque serie est indexee par
    datetime quart-horaire, et somme a 1.0 sur l'annee (verifie a l'extraction).
    """
    wb = openpyxl.load_workbook(xlsm_path, data_only=True, read_only=True)
    ws = wb[SHEET_NAME]

    dates = []
    cols_vals = {col: [] for col in PROFILE_COLUMNS}
    max_col = max(PROFILE_COLUMNS)
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
    profiles = {}
    for col, key in PROFILE_COLUMNS.items():
        vals = np.array(cols_vals[col], dtype=float)
        series = pd.Series(vals, index=index, name=key)
        series = series[~series.index.duplicated(keep="first")].sort_index()
        total = float(series.sum())
        if not (0.99 <= total <= 1.01):
            print(f"  /!\\ Profil '{key}' : somme annuelle = {total:.4f} (attendu ~1.0) -- "
                  f"verifie la colonne source dans l'Excel.")
        profiles[key] = series

    return profiles


def main():
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
