"""
Recuperation du profil de production PV via l'API PVGIS (Commission europeenne),
en remplacement de l'onglet PVS de l'ancien fichier Excel.

Utilise pvlib.iotools.get_pvgis_hourly, qui appelle
https://re.jrc.ec.europa.eu/api/v5_2/seriescalc et gere le format de reponse
PVGIS pour nous (pas besoin de refaire l'appel HTTP a la main).

Convention d'azimut pvlib : 0=nord, 90=est, 180=sud, 270=ouest.
"""
import pandas as pd
import numpy as np
from pvlib.iotools import get_pvgis_hourly

# Annee de reference utilisee pour le profil PV. PVGIS ne fournit des donnees
# recentes que jusqu'a ~2 ans avant aujourd'hui ; 2020 est une annee "TMY-like"
# generalement disponible pour toute l'Europe et sert de profil-type stable.
DEFAULT_PVGIS_YEAR = 2020


def fetch_pv_profile_1kwc(lat: float, lon: float, tilt: float = 35.0,
                            azimuth: float = 180.0, system_loss_pct: float = 14.0,
                            year: int = DEFAULT_PVGIS_YEAR) -> pd.Series:
    """
    Recupere le profil de production PV normalise a 1 kWc pour un site donne.

    Parametres
    ----------
    lat, lon : coordonnees du site (degres decimaux)
    tilt : inclinaison des panneaux (degres, 0=horizontal)
    azimuth : orientation (convention pvlib : 180=sud, 90=est, 270=ouest)
    system_loss_pct : pertes systeme globales (onduleur, cablage, salissure...),
                       14% est la valeur par defaut PVGIS standard
    year : annee de reference pour le profil horaire (PVGIS fournit des
           donnees satellite historiques, pas de previsions)

    Retourne
    --------
    pd.Series indexee par datetime horaire (naive, heure locale UTC+1 -- voir
    note ci-dessous), valeurs en kWh produits par kWc installe, pour cette heure.
    """
    data, meta = get_pvgis_hourly(
        latitude=lat,
        longitude=lon,
        start=year,
        end=year,
        pvcalculation=True,     # demande le calcul de puissance PV (colonne "P"), pas seulement l'irradiance
        peakpower=1.0,          # on demande le profil pour 1 kWc, a l'echelle ensuite
        surface_tilt=tilt,
        surface_azimuth=azimuth,
        loss=system_loss_pct,
        outputformat="json",
    )

    # Garde-fou : si PVGIS change son format de reponse ou renvoie une erreur
    # silencieuse (coordonnees hors zone de couverture, etc.), on echoue tot
    # avec un message clair plutot que de propager un DataFrame incomplet.
    if "P" not in data.columns:
        raise RuntimeError(
            "Reponse PVGIS inattendue : pas de colonne 'P' (puissance AC). "
            "Verifie les coordonnees et reessaie."
        )

    # data.index est en UTC (tz-aware) -> on repasse en naive, heure locale
    # Belgique, pour matcher la convention du reste du pipeline (index naive,
    # aligne sur l'annee calendaire du client).
    idx = data.index.tz_convert("Europe/Brussels").tz_localize(None)
    power_w = data["P"].values  # puissance AC moyenne sur l'heure, en W, pour 1 kWc

    profil = pd.Series(power_w / 1000.0, index=idx, name="pv_kwh_per_kwc")
    # PVGIS peut renvoyer l'annee complete avec un timestamp de depart/fin
    # legerement decale (29 fev, DST) -- on force l'annee cible et on trie.
    profil = profil[~profil.index.duplicated(keep="first")].sort_index()
    return profil


def expand_to_quarter_hour(hourly_profile_1kwc: pd.Series, kwc: float,
                             target_index: pd.DatetimeIndex) -> pd.Series:
    """
    Convertit un profil horaire (kWh/kWc) en serie quart-horaire (kWh, pour
    la puissance kwc donnee), alignee sur target_index (index quart-horaire
    de la conso client).

    Meme logique que l'ancienne lecture de l'onglet PVS : chaque heure est
    divisee en 4 quarts-heure de production egale (pas de vraie variabilite
    infra-horaire -- limitation intrinseque a la resolution de PVGIS, voir
    discussion sur la resolution temporelle du modele).
    """
    # On reindexe le profil PV sur une annee "type" (mois/jour/heure), en
    # ignorant l'annee reelle de la donnee client vs l'annee PVGIS -- on
    # mappe uniquement par (mois, jour, heure) pour rester robuste aux
    # annees bissextiles / decalages de calendrier entre les deux sources.
    # Version vectorisee (l'ancienne boucle Python par quart d'heure prenait
    # ~4 s par appel, soit a chaque changement de kWc dans l'apercu) --
    # semantique identique : cle (mois, jour, heure), derniere valeur gardee
    # en cas de doublon, 29 fevrier -> 28 fevrier, sinon moyenne du profil.
    src_idx = pd.DatetimeIndex(hourly_profile_1kwc.index)
    # Table de correspondance (mois, jour, heure) -> production (kWh/kWc).
    # "keep='last'" : en cas de doublon (ex. chevauchement DST), on garde la
    # derniere valeur rencontree, comme le faisait l'ancienne boucle.
    lookup = pd.Series(
        np.asarray(hourly_profile_1kwc.values, dtype=float),
        index=pd.MultiIndex.from_arrays([src_idx.month, src_idx.day, src_idx.hour]))
    lookup = lookup[~lookup.index.duplicated(keep="last")]
    # Valeur de secours si une (mois, jour, heure) cible n'a aucune correspondance
    # dans le profil PVGIS (ne devrait arriver que pour des cas limites de calendrier).
    fallback = float(np.nanmean(hourly_profile_1kwc.values))

    hours = pd.DatetimeIndex(target_index).floor("h")  # arrondit chaque quart d'heure a son heure pleine
    month, day, hour = hours.month, hours.day, hours.hour
    pos = lookup.index.get_indexer(pd.MultiIndex.from_arrays([month, day, hour]))
    # PVGIS ne fournit pas le 29 fevrier (annee de reference non bissextile ou
    # simplement absente) : si la cible tombe un 29/02, on retombe sur le 28/02
    # a la meme heure plutot que sur la valeur de secours generique.
    is_feb29 = np.asarray((month == 2) & (day == 29))
    pos_feb28 = lookup.index.get_indexer(
        pd.MultiIndex.from_arrays([np.full(len(hours), 2), np.full(len(hours), 28), hour]))
    pos = np.where((pos < 0) & is_feb29, pos_feb28, pos)

    values = lookup.to_numpy()
    # get_indexer renvoie -1 pour les positions non trouvees ; np.maximum(pos, 0)
    # evite un index negatif invalide, et le np.where remplace ensuite ces cas
    # (pos < 0, donc toujours non trouves a ce stade) par le fallback.
    qh_values = np.where(pos >= 0, values[np.maximum(pos, 0)], fallback)
    # Repartition uniforme de la production horaire sur les 4 quarts d'heure :
    # kWh/kWc horaire * kWc installes / 4 = kWh par quart d'heure.
    pv_kwh_qh = (qh_values * kwc) / 4.0
    return pd.Series(pv_kwh_qh, index=target_index, name="pv_kwh")
