"""
Recupere les vrais prix Day-Ahead (Belpex, zone Belgique) pour une annee
donnee via la plateforme de transparence ENTSO-E, et les sauvegarde au
format attendu par fournisseur_roi.load_dayahead_prices (pd.Series en
EUR/kWh, indexee par datetime).

A EXECUTER UNE SEULE FOIS PAR ANNEE (sur TON PC, pas dans l'appli Streamlit)
-- ca genere un fichier data/belpex_<annee>_qh.pkl que l'appli relira ensuite
directement, sans jamais rappeler l'API.

--------------------------------------------------------------------------
PREREQUIS -- obtenir une cle API ENTSO-E (gratuit) :
--------------------------------------------------------------------------
1. Cree un compte sur https://transparency.entsoe.eu/ (bouton "Login" en
   haut a droite, puis "Register").
2. Une fois connecte, envoie un email a transparency@entsoe.eu avec pour
   objet "Restful API access" en precisant l'adresse email de ton compte --
   la cle est generalement activee sous quelques jours ouvres.
3. Une fois active, va dans "My Account Settings" sur le site -> l'API
   Security Token apparait dans ton profil.
4. Installe la librairie : pip install entsoe-py

--------------------------------------------------------------------------
UTILISATION :
--------------------------------------------------------------------------
    python fetch_belpex.py 2024 TA_CLE_API_ICI
    python fetch_belpex.py 2025 TA_CLE_API_ICI
"""
import sys
import os
import pandas as pd
from entsoe import EntsoePandasClient

# Code de zone ENTSO-E pour la Belgique (bidding zone BE)
BIDDING_ZONE_BE = "BE"

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(_APP_ROOT, "data")


def fetch_belpex_year(year: int, api_key: str) -> pd.Series:
    client = EntsoePandasClient(api_key=api_key)

    start = pd.Timestamp(f"{year}-01-01", tz="Europe/Brussels")
    end = pd.Timestamp(f"{year + 1}-01-01", tz="Europe/Brussels")

    print(f"Telechargement des prix Day-Ahead Belgique pour {year} (ca peut prendre 1-2 min)...")
    prices_mwh = client.query_day_ahead_prices(BIDDING_ZONE_BE, start=start, end=end)

    if prices_mwh.empty:
        raise RuntimeError(
            f"Aucune donnee retournee par ENTSO-E pour {year} -- verifie ta cle API "
            f"et que l'annee demandee est bien disponible."
        )

    # ENTSO-E retourne les prix en EUR/MWh, au pas horaire, tz-aware.
    # Conversion en EUR/kWh (le moteur de calcul attend cette unite) + on
    # repasse en index naive (coherent avec le reste du pipeline).
    prices_mwh.index = prices_mwh.index.tz_convert("Europe/Brussels").tz_localize(None)
    prices_kwh = prices_mwh / 1000.0

    # Le moteur (load_dayahead_prices) fait deja un reindex + ffill vers la
    # grille quart-horaire du client -- pas besoin de desagreger nous-memes
    # ici, on sauvegarde tel quel au pas horaire natif d'ENTSO-E.
    prices_kwh = prices_kwh[~prices_kwh.index.duplicated(keep="first")].sort_index()
    prices_kwh.name = "price_eur_kwh"

    print(f"  -> {len(prices_kwh)} points horaires, "
          f"prix moyen = {prices_kwh.mean()*100:.2f} c/kWh, "
          f"min = {prices_kwh.min()*100:.2f} c/kWh, max = {prices_kwh.max()*100:.2f} c/kWh")

    return prices_kwh


def main():
    if len(sys.argv) != 3:
        print("Usage : python fetch_belpex.py <annee> <cle_api_entsoe>")
        print("Exemple : python fetch_belpex.py 2024 abcd1234-...")
        sys.exit(1)

    year = int(sys.argv[1])
    api_key = sys.argv[2]

    series = fetch_belpex_year(year, api_key)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"belpex_{year}_qh.pkl")
    series.to_pickle(out_path)
    print(f"\nSauvegarde : {out_path}")
    print("Ce fichier est maintenant utilisable directement par l'appli "
          "(selecteur d'annee Belpex).")


if __name__ == "__main__":
    main()
