"""
Extrait le profil de production PV (pour 1 kWc, au pas horaire) depuis
l'onglet "PVS" de ton fichier Excel modele, et le sauvegarde en dur dans
data/pv_profile_1kwc.pkl.

A EXECUTER UNE SEULE FOIS (ou a chaque fois que tu changes le profil PV
dans l'Excel -- topologie, inclinaison, orientation...). L'appli Streamlit
relit ensuite directement ce fichier .pkl, sans jamais appeler PVGIS ni
demander de latitude/longitude.

--------------------------------------------------------------------------
UTILISATION :
--------------------------------------------------------------------------
    python extract_pv_from_excel.py "ENERGY MIX ANALYSIS - AMELIORATION (2).xlsm"
"""
import sys
import os
import openpyxl
import pandas as pd
import numpy as np

MAX_SCAN_ROWS_H = 8900
_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(_APP_ROOT, "data")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "pv_profile_1kwc.pkl")


def extract_pv_profile_1kwc(xlsm_path: str) -> pd.Series:
    """
    Lit l'onglet PVS : dates en colonne A (a partir de la ligne 5), profil
    de production pour 1 kWc en colonne BX (index 115), exactement comme
    dataextraction._read_pv_series -- sauf qu'ici on garde le pas HORAIRE
    natif (pas de desagregation en quart-heure : ca, l'appli le fait a la
    volee via pv_pvgis.expand_to_quarter_hour, a partir de ce fichier fige).
    """
    # data_only=True : on lit les valeurs calculees par Excel (pas les
    # formules) ; read_only=True : mode flux, indispensable pour parcourir
    # rapidement un fichier volumineux sans tout charger en memoire.
    wb = openpyxl.load_workbook(xlsm_path, data_only=True, read_only=True)
    ws = wb["PVS"]

    dates = []
    vals = []
    # Colonne A = dates, colonne BX (index 115, 0-based -> colonne 116 en
    # 1-based) = production pour 1 kWc. On s'arrete des qu'une date est vide
    # (fin des donnees), plutot que de scanner jusqu'a MAX_SCAN_ROWS_H a chaque fois.
    for row in ws.iter_rows(min_row=5, max_row=4 + MAX_SCAN_ROWS_H,
                             min_col=1, max_col=116, values_only=True):
        d = row[0]
        if d is None:
            break
        dates.append(d)
        vals.append(row[115] if row[115] is not None else 0.0)
    wb.close()

    if not dates:
        raise ValueError("Aucune date valide trouvee en colonne A de l'onglet PVS.")

    vals = np.array(vals, dtype=float)
    series = pd.Series(vals, index=pd.DatetimeIndex(dates), name="pv_kwh_per_kwc")
    # Deduplique (garde la premiere occurrence) et trie par date -- au cas ou
    # l'Excel contiendrait des lignes en double ou dans le desordre.
    series = series[~series.index.duplicated(keep="first")].sort_index()
    return series


def main():
    """
    Point d'entree CLI : lit le chemin du fichier Excel en argument, extrait
    le profil PV et le fige dans data/pv_profile_1kwc.pkl. A relancer
    manuellement chaque fois que le profil source change -- ce script n'est
    jamais appele par l'application Streamlit elle-meme.
    """
    if len(sys.argv) != 2:
        print("Usage : python extract_pv_from_excel.py <chemin_vers_le_xlsm>")
        sys.exit(1)

    xlsm_path = sys.argv[1]
    series = extract_pv_profile_1kwc(xlsm_path)

    print(f"  -> {len(series)} points horaires, de {series.index[0]} a {series.index[-1]}")
    print(f"  -> Total = {series.sum():,.1f} kWh/kWc/an".replace(",", " "))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    series.to_pickle(OUTPUT_PATH)
    print(f"\nSauvegarde : {OUTPUT_PATH}")
    print("L'appli va desormais utiliser ce profil fige automatiquement "
          "(plus besoin de latitude/longitude).")


if __name__ == "__main__":
    main()
