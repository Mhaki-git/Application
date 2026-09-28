import os
import tempfile
import datetime

import streamlit as st
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from io_excel.dataextraction import build_timeseries_from_sources
from engine.fournisseur_roi import run_fournisseur_model_from_data
from reporting.pdf_report import build_pdf_report
from reporting.pdf_style import fmt_eur, fmt_number

st.set_page_config(page_title="Analyse Energetique - PV & Batterie", layout="wide", page_icon="⚡")

WORKDIR = tempfile.mkdtemp(prefix="energy_app_")

# ---------------------------------------------------------------------
# Profil PV -- fige, extrait une fois pour toutes depuis l'Excel via
# extract_pv_from_excel.py (plus d'appel PVGIS, plus de latitude/longitude
# a saisir a chaque fois).
# ---------------------------------------------------------------------
PV_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pv_profile_1kwc.pkl")


@st.cache_data
def _load_pv_profile_1kwc(path: str):
    return pd.read_pickle(path)

# ---------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------
st.markdown("""
<style>
.metric-card { background:#F2F2F2; border-radius:10px; padding:14px 18px; }
h1, h2, h3 { color:#1B2A4A; }
</style>
""", unsafe_allow_html=True)

st.title("⚡ Analyse energetique -- PV + Batterie")
st.caption("Saisis les parametres, lance le calcul, recupere un rapport PDF.")

if "results" not in st.session_state:
    st.session_state["results"] = None

# ---------------------------------------------------------------------
# 0. Mode
# ---------------------------------------------------------------------
st.header("0. Mode")
MODE_LABELS = {
    "fournisseur_principal": " Fournisseur principal - Vente d'électricité avec Trading Belpex "
                              "",
    "fournisseur_secondaire": " Fournisseur secondaire - Vente d'électricité produite "
                               "",
    "vente_directe": " Vente directe - Installations CAPEX "
                      "",
}
mode = st.radio(
    "Quel est ton role sur ce projet ?",
    options=list(MODE_LABELS.keys()),
    format_func=lambda m: MODE_LABELS[m],
    horizontal=False,
)
st.session_state["mode"] = mode
use_belpex = (mode == "fournisseur_principal")

# ---------------------------------------------------------------------
# 1. Donnees du site
# ---------------------------------------------------------------------
st.header("1. Données du site")

col1, col2 = st.columns(2)
CONSO_PROFILES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "conso_profiles.pkl")
CONSO_PROFILE_LABELS = {
    "residentiel_famille_enfants_scolarises": "Résidentiel - Famille avec enfants scolarisés",
    "residentiel_famille_jeunes_enfants": "Résidentiel - Famille avec jeunes enfants",
    "residentiel_menage_1_2_personnes": "Résidentiel - Ménage avec 1 ou 2 personnes",
    "residentiel_retraites_ou_domicile": "Résidentiel - Retraités ou travail à domicile",
    "residentiel_conso_concentree_nuit": "Résidentiel - Consommation concentrée la nuit",
    "industriel_conso_constante": "Industriel - Consommation d'énergie constante",
    "industriel_conso_soiree": "Industriel - Consommation d'énergie en soirée",
    "industriel_semaine_conso": "Industriel - En semaine, axé sur la consommation",
    "industriel_commerce_heures_ouverture": "Industriel - Commerce (heures d'ouverture)",
}


@st.cache_data
def _load_conso_profiles(path: str):
    return pd.read_pickle(path)


with col1:
    st.subheader("Consommation client")
    conso_source = st.radio(
        "Source de la consommation",
        options=["upload", "profil_integre"],
        format_func=lambda x: (
            "Uploader un CSV de consommation" if x == "upload"
            else "Choisir un profil-type intégré (quart-horaire)"
        ),
        horizontal=True,
    )

    conso_file = None
    conso_unit = "kW"
    conso_series_profile = None

    if conso_source == "upload":
        conso_file = st.file_uploader(
            "CSV de consommation (2 colonnes : horodatage + valeur). "
            "Pas de temps quelconque (15/30/60 min), detecte automatiquement.",
            type=["csv"])
        conso_unit = st.radio("Unite de la colonne valeur", options=["kW", "kWh"], horizontal=True)
    else:
        if os.path.exists(CONSO_PROFILES_PATH):
            conso_profiles = _load_conso_profiles(CONSO_PROFILES_PATH)
            _profile_keys = list(conso_profiles.keys())
            profile_key = st.selectbox(
                "Profil-type de consommation",
                options=_profile_keys,
                format_func=lambda k: CONSO_PROFILE_LABELS.get(k, k),
            )
            conso_annuelle_kwh = st.number_input(
                "Consommation annuelle cible (kWh/an)",
                value=100000.0, step=1000.0, min_value=0.0,
                help="Le profil intégré est une forme normalisée (répartition quart-horaire sur "
                     "l'année) -- il est mis à l'échelle avec cette valeur pour obtenir la "
                     "consommation réelle en kWh.",
            )
            from io_excel.dataextraction import conso_from_normalized_profile
            conso_series_profile = conso_from_normalized_profile(
                conso_profiles[profile_key], conso_annuelle_kwh)
            st.success(
                f"Profil '{CONSO_PROFILE_LABELS.get(profile_key, profile_key)}' chargé, "
                f"mis à l'échelle sur {fmt_number(conso_annuelle_kwh, suffix='kWh/an')}"
            )
        else:
            st.error(
                "Aucun profil de consommation intégré trouvé (`data/conso_profiles.pkl`). "
                "Lance `python io_sources/extract_conso_profiles_from_excel.py <fichier.xlsm>` "
                "une fois, ou choisis l'upload CSV ci-dessus."
            )

with col2:
    st.subheader("Production PV")
    pv_source = st.radio(
        "Source du profil PV",
        options=["excel", "pvgis"],
        format_func=lambda x: (
            "Fichier Excel figé (extrait une fois via extract_pv_from_excel.py)" if x == "excel"
            else "PVGIS (API en ligne, saisie latitude/longitude)"
        ),
        horizontal=True,
    )

    pv_profile_1kwc = None

    if pv_source == "excel":
        if os.path.exists(PV_PROFILE_PATH):
            pv_profile_1kwc = _load_pv_profile_1kwc(PV_PROFILE_PATH)
            st.success(
                f"Profil PV charge depuis l'Excel (fige) : "
                f"{fmt_number(pv_profile_1kwc.sum(), suffix='kWh/kWc/an')}"
            )
            st.caption(
                "Ce profil vient de `data/pv_profile_1kwc.pkl`. Pour le changer "
                "(nouvelle topologie/inclinaison dans l'Excel), relance "
                "`python extract_pv_from_excel.py <fichier.xlsm>` puis redemarre l'appli."
            )
        else:
            st.error(
                "Aucun profil PV fige trouve (`data/pv_profile_1kwc.pkl`). "
                "Lance `python extract_pv_from_excel.py <fichier.xlsm>` une fois, "
                "ou choisis la source PVGIS ci-dessus."
            )
    else:
        st.caption(
            "Recupere le profil de production via l'API PVGIS (Commission "
            "europeenne), pour la position et l'orientation saisies ci-dessous."
        )
        cc1, cc2 = st.columns(2)
        pv_lat = cc1.number_input("Latitude", value=50.85, format="%.4f")
        pv_lon = cc2.number_input("Longitude", value=4.35, format="%.4f")
        cc3, cc4, cc5 = st.columns(3)
        pv_tilt = cc3.number_input("Inclinaison des panneaux (°)", value=35.0, step=1.0)
        pv_azimuth = cc4.number_input("Orientation (° -- 180=Sud, 90=Est, 270=Ouest)", value=180.0, step=1.0)
        pv_loss = cc5.number_input("Pertes systeme (%)", value=14.0, step=1.0)

        @st.cache_data
        def _fetch_pv_pvgis(lat, lon, tilt, azimuth, loss):
            from io_sources.pv_pvgis import fetch_pv_profile_1kwc
            return fetch_pv_profile_1kwc(lat, lon, tilt=tilt, azimuth=azimuth, system_loss_pct=loss)

        try:
            with st.spinner("Recuperation du profil PV via PVGIS..."):
                pv_profile_1kwc = _fetch_pv_pvgis(pv_lat, pv_lon, pv_tilt, pv_azimuth, pv_loss)
            st.success(
                f"Profil PV recupere via PVGIS : "
                f"{fmt_number(pv_profile_1kwc.sum(), suffix='kWh/kWc/an')}"
            )
        except Exception as e:
            st.error(f"Erreur lors de l'appel PVGIS : {e}")

BELPEX_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
belpex_source = "demo"
belpex_upload_file = None

if use_belpex:
    st.subheader("Prix de marché (Belpex Day-Ahead)")
    _belpex_annees_dispo = sorted([
        f.split("_")[1] for f in os.listdir(BELPEX_DATA_DIR)
        if f.startswith("belpex_") and f.endswith("_qh.pkl")
    ]) if os.path.isdir(BELPEX_DATA_DIR) else []

    belpex_options = (["auto"] if _belpex_annees_dispo else []) + _belpex_annees_dispo + ["upload", "demo"]
    belpex_source = st.radio(
        "Source des prix Day-Ahead",
        options=belpex_options,
        format_func=lambda x: (
            "Auto (suit les dates réelles de la consommation client)" if x == "auto" else
            f"Annee {x} (integree, forcée)" if x not in ("upload", "demo") else
            "Uploader mon propre fichier .pkl" if x == "upload" else
            "Mode demo (prix simules -- a eviter pour un vrai client)"
        ),
        horizontal=True,
    )
    belpex_date_fin = None
    if belpex_source == "auto":
        st.caption(
            "Le prix Day-Ahead est calé jour par jour (mois/jour/heure) sur les dates de la "
            "consommation client -- l'année Belpex intégrée la plus proche est choisie "
            "automatiquement (celle qui correspond si elle existe, sinon la plus récente)."
        )
    elif belpex_source not in ("upload", "demo"):
        st.caption(
            f"Prix Day-Ahead : année '{belpex_source}' forcée (calée jour par jour sur les "
            f"dates de la consommation client, quelle que soit leur vraie année)."
        )
        _limiter_periode = st.checkbox(
            f"Limiter la source à une tranche du 1er janvier {belpex_source} à une date de fin",
            value=False,
            help="Si le reste de l'année source n'est pas jugé pertinent ou fiable (ex: données "
                 "incomplètes après une certaine date), le calage jour/mois/heure n'utilisera "
                 "que les prix Belpex de cette tranche.",
        )
        if _limiter_periode:
            belpex_date_fin = st.date_input(
                f"Date de fin de la tranche (1er janvier {belpex_source} -> cette date)",
                value=datetime.date(int(belpex_source), 12, 31),
                min_value=datetime.date(int(belpex_source), 1, 1),
                max_value=datetime.date(int(belpex_source), 12, 31),
            )

    if belpex_source == "upload":
        belpex_upload_file = st.file_uploader(
            "Fichier .pkl (EUR/kWh, index datetime)", type=["pkl"])

    if not _belpex_annees_dispo:
        st.caption(
            "Aucune annee Belpex integree pour l'instant -- lance `python fetch_belpex.py "
            "<annee> <cle_api>` une fois pour en ajouter (voir le fichier fetch_belpex.py)."
        )
else:
    st.caption(
        "ℹ️ Pas besoin de prix Belpex dans ce mode : le dispatch de la batterie "
        "s'appuie directement sur le tarif reseau (fixe ou HP/HC) defini ci-dessous."
    )

if conso_file is None and conso_series_profile is None:
    st.info("Charge un CSV de consommation ou choisis un profil intégré pour continuer.")
    st.stop()

if pv_profile_1kwc is None:
    st.info("Choisis une source de profil PV valide (Excel figé ou PVGIS) pour continuer.")
    st.stop()

# ---------------------------------------------------------------------
# 2. Parametres
# ---------------------------------------------------------------------
st.header("2. Paramètres")

st.subheader("Installation")

PV_CATALOGUE = [
    {"kwc": 15.0, "kva": 10.0, "prix_kwc": 664.0},
    {"kwc": 30.0, "kva": 20.0, "prix_kwc": 774.0},
    {"kwc": 45.0, "kva": 30.0, "prix_kwc": 668.0},
    {"kwc": 75.0, "kva": 50.0, "prix_kwc": 664.0},
]

pv_input_mode = st.radio(
    "Puissance PV installée",
    options=["manuel", "catalogue"],
    format_func=lambda x: "Saisie manuelle (kVA calculé, kWc / 1.5)" if x == "manuel" else "Choisir dans le catalogue",
    horizontal=True,
)

c1, c2, c3, c4 = st.columns(4)

if pv_input_mode == "catalogue":
    _catalogue_labels = [
        f"{item['kwc']:.0f} kWc -- {item['kva']:.0f} kVA -- {item['prix_kwc']:.0f} €/kWc"
        for item in PV_CATALOGUE
    ]
    _choice_idx = c1.selectbox(
        "Modele catalogue", options=list(range(len(PV_CATALOGUE))),
        format_func=lambda i: _catalogue_labels[i])
    _chosen = PV_CATALOGUE[_choice_idx]
    kwc = _chosen["kwc"]
    kva_onduleur = _chosen["kva"]
    prix_kwc_pv_catalogue = _chosen["prix_kwc"]
else:
    kwc = c1.number_input("Puissance PV (kWc)", value=100.0, step=1.0)
    kva_onduleur = kwc / 1.5
    prix_kwc_pv_catalogue = None

c1.caption(
    f"Onduleur : **{kva_onduleur:.1f} kVA**"
    + (" (catalogue)" if pv_input_mode == "catalogue" else " (règle kWc / 1.5)")
    + " -- l'écrêtement de production suppose un facteur de puissance (cos φ) = 1 (kVA ≈ kW)."
)

battery_power_kw = c2.number_input("Puissance batterie (kW)", value=0.0, step=1.0)
battery_capacity_kwh = c3.number_input("Capacite batterie (kWh)", value=0.0, step=1.0)
contrat_kw = c4.number_input("Puissance souscrite / contrat (kW)", value=150.0, step=1.0)
has_battery = (battery_power_kw > 0) and (battery_capacity_kwh > 0)


@st.cache_data(show_spinner=False)
def _build_preview_timeseries_csv(conso_bytes: bytes, pv_profile: pd.Series, kwc_val: float, unit_val: str):
    import io
    buf = io.BytesIO(conso_bytes)
    return build_timeseries_from_sources(buf, pv_profile, kwc=kwc_val, unit=unit_val)


@st.cache_data(show_spinner=False)
def _build_preview_timeseries_profile(conso_series: pd.Series, pv_profile: pd.Series, kwc_val: float):
    return build_timeseries_from_sources(conso_series, pv_profile, kwc=kwc_val)


@st.cache_data(show_spinner=False)
def _simulate_battery_selfconso(pv_kwh: np.ndarray, conso_kwh: np.ndarray,
                                 battery_power_kw: float, battery_capacity_kwh: float):
    """Cache Streamlit autour de engine.preview_dispatch.simulate_battery_selfconso (voir ce module)."""
    from engine.preview_dispatch import simulate_battery_selfconso
    return simulate_battery_selfconso(pv_kwh, conso_kwh, battery_power_kw, battery_capacity_kwh)


_live_pvm = None
try:
    if conso_series_profile is not None:
        _df_preview = _build_preview_timeseries_profile(conso_series_profile, pv_profile_1kwc, kwc)
    else:
        _df_preview = _build_preview_timeseries_csv(conso_file.getvalue(), pv_profile_1kwc, kwc, conso_unit)
    _pv_kwh_prev = _df_preview["pv_kwh"].values
    if kva_onduleur > 0:
        _pv_kwh_prev = np.minimum(_pv_kwh_prev, kva_onduleur * 0.25)
    _conso_kwh_prev = _df_preview["conso_kwh"].values

    _pv_total_prev = float(_pv_kwh_prev.sum())
    _conso_total_prev = float(_conso_kwh_prev.sum())

    if has_battery:
        # Avec batterie : dispatch glouton (autoconsommation max, pas d'arbitrage
        # marche) pour approcher l'effet reel de la batterie sur l'apercu.
        _import_prev, _export_prev_arr = _simulate_battery_selfconso(
            _pv_kwh_prev, _conso_kwh_prev, battery_power_kw, battery_capacity_kwh)
        _export_prev = float(_export_prev_arr.sum())
        _import_total_prev = float(_import_prev.sum())
        _autoconso_prev = 100 * (_pv_total_prev - _export_prev) / _pv_total_prev if _pv_total_prev > 0 else 0.0
        _autonomie_prev = 100 * (_conso_total_prev - _import_total_prev) / _conso_total_prev if _conso_total_prev > 0 else 0.0
    else:
        # Sans batterie : autoconsommation "directe", PV utilisee au meme pas de
        # temps que la conso, intervalle par intervalle -- borne mathematiquement
        # a <= PV total (donc <= 100%), contrairement au calcul post-simulation
        # (qui infere l'autoconsommation depuis l'import/export et peut etre
        # fausse par l'arbitrage batterie -- voir le message ci-dessous).
        _pv_utilisee_prev = float(np.minimum(_pv_kwh_prev, _conso_kwh_prev).sum())
        _export_prev = max(_pv_total_prev - _pv_utilisee_prev, 0.0)
        _autoconso_prev = 100 * _pv_utilisee_prev / _pv_total_prev if _pv_total_prev > 0 else 0.0
        _autonomie_prev = 100 * _pv_utilisee_prev / _conso_total_prev if _conso_total_prev > 0 else 0.0

    _live_pvm = {
        "pv_total": _pv_total_prev, "conso_total": _conso_total_prev,
        "export": _export_prev, "autoconso_pct": _autoconso_prev, "autonomie_pct": _autonomie_prev,
    }
except Exception as _e:
    _live_pvm = None
    st.caption(f"ℹ️ Aperçu instantané indisponible pour l'instant ({_e}).")

if _live_pvm:
    if has_battery:
        st.caption(
            f"📊 Aperçu instantané AVEC batterie ({battery_power_kw:.0f} kW / "
            f"{battery_capacity_kwh:.0f} kWh, dispatch simplifié autoconsommation max) -- "
            f"PV produit : **{fmt_number(_live_pvm['pv_total'], suffix='kWh/an')}** · "
            f"Conso client : **{fmt_number(_live_pvm['conso_total'], suffix='kWh/an')}** · "
            f"Injection réseau estimée : **{fmt_number(_live_pvm['export'], suffix='kWh/an')}** · "
            f"Autoconsommation : **{_live_pvm['autoconso_pct']:.1f} %** · "
            f"Autonomie énergétique : **{_live_pvm['autonomie_pct']:.1f} %**"
        )
        st.caption(
            "⚠️ Dispatch simplifié (maximise juste l'autoconsommation, sans arbitrage "
            "de marché) -- utile pour un ordre de grandeur rapide. Lance la simulation "
            "complète (bouton \"🚀 Lancer la simulation\" plus bas) pour le dispatch "
            "day-ahead optimisé et les chiffres de ROI/gain fiables."
        )
    else:
        st.caption(
            f"📊 Aperçu instantané (sans batterie -- puissance/capacité à 0, recalculé à "
            f"chaque changement) -- PV produit : **{fmt_number(_live_pvm['pv_total'], suffix='kWh/an')}** · "
            f"Conso client : **{fmt_number(_live_pvm['conso_total'], suffix='kWh/an')}** · "
            f"Injection réseau estimée : **{fmt_number(_live_pvm['export'], suffix='kWh/an')}** · "
            f"Autoconsommation : **{_live_pvm['autoconso_pct']:.1f} %** · "
            f"Autonomie énergétique : **{_live_pvm['autonomie_pct']:.1f} %**"
        )
else:
    st.caption(
        "Impossible de calculer l'aperçu instantané -- vérifie le fichier CSV de "
        "consommation ci-dessus."
    )

st.subheader("Tarif réseau du client")
st.caption(
    "Utilise pour l'ancien cout du client (comparaison) ET, en mode fournisseur "
    "secondaire / vente directe, comme reference de cout reseau pour piloter le "
    "dispatch de la batterie (a la place du marche Belpex)."
)
tarif_structure = st.radio(
    "Structure tarifaire", options=["fixe", "hp_hc"],
    format_func=lambda x: "Prix fixe (un seul prix)" if x == "fixe" else "Heures Pleines / Heures Creuses",
    horizontal=True,
)
c1, c2, c3, c4 = st.columns(4)
if tarif_structure == "fixe":
    old_price_kwh_fixe = c1.number_input("Prix kWh fixe (€/kWh)", value=0.16, step=0.01, format="%.4f")
    price_hp, price_hc = 0.0, 0.0
    heure_debut_hp, heure_debut_hc = 7, 19
else:
    price_hp = c1.number_input("Prix Heures Pleines (€/kWh)", value=0.19, step=0.01, format="%.4f")
    price_hc = c2.number_input("Prix Heures Creuses (€/kWh)", value=0.13, step=0.01, format="%.4f")
    heure_debut_hp = c3.number_input("Debut heures pleines (h)", value=7, min_value=0, max_value=23, step=1)
    heure_debut_hc = c4.number_input("Debut heures creuses (h)", value=19, min_value=0, max_value=23, step=1)
    old_price_kwh_fixe = 0.0
c1, c2 = st.columns(2)
old_cout_additionnel_contrat = c1.number_input("Frais fixes annuels ancien contrat (EUR)", value=0.0, step=10.0)
prix_rachat_surplus_kwh = c2.number_input(
    "Prix de rachat du surplus injecte (€/kWh) -- utilise hors mode fournisseur principal",
    value=0.03, step=0.005, format="%.4f")

if mode == "fournisseur_principal":
    st.subheader("Ton offre -- prix facture au client (bloc fournisseur principal)")
    c1, c2, c3 = st.columns(3)
    prix_vente_kwh = c1.number_input("Prix de vente fixe au client (€/kWh)", value=0.12, step=0.005, format="%.4f")
    marge_fournisseur = c2.number_input("Ta marge sur achat reseau (€/kWh)", value=0.01, step=0.005, format="%.4f")
    taxes_couts_proportionnels = c3.number_input("Taxes/couts reseau proportionnels (€/kWh)", value=0.02, step=0.005, format="%.4f")
    c1, c2 = st.columns(2)
    marge_injection = c1.number_input("Marge sur injection surplus (€/kWh)", value=0.00175, step=0.005, format="%.4f")
    tarif_capacitaire_fournisseur = c2.number_input(
        "Tarif capacitaire / pointe (€/kW/mois)",
        value=3.0, step=0.5,
    )

elif mode == "fournisseur_secondaire":
    st.subheader("Ton offre -- vente de l'energie produite (prix fixe, pas de Belpex)")
    c1, c2 = st.columns(2)
    prix_vente_kwh = c1.number_input("Prix de vente de l'energie au client (€/kWh)", value=0.12, step=0.005, format="%.4f")
    tarif_capacitaire_fournisseur = c2.number_input(
        "Tarif capacitaire / pointe (€/kW/mois) -- non utilise dans ce mode",
        value=3.0, step=0.5,
        help="Retire du calcul du gain en mode fournisseur secondaire (demande "
             "utilisateur) -- ce champ est conserve dans l'interface pour reference "
             "mais n'a plus d'effet sur les resultats de ce mode. Il reste actif "
             "pour les modes fournisseur principal et vente directe."
    )
    marge_fournisseur = 0.0
    taxes_couts_proportionnels = 0.0
    marge_injection = 0.0

else:  # vente_directe
    st.subheader("Vente directe -- le client paie le CAPEX")
    st.caption(
        "Pas de prix de vente a fixer : le client garde son propre tarif reseau "
        "(ci-dessus), et toute l'economie du PV+batterie lui revient directement."
    )
    tarif_capacitaire_fournisseur = st.number_input(
        "Tarif capacitaire / pointe (EUR/kW/mois)",
        value=3.0, step=0.5,
    )
    prix_vente_kwh = None  # auto-calcule par le moteur (moyenne ponderee du tarif reseau)
    marge_fournisseur = 0.0
    taxes_couts_proportionnels = 0.0
    marge_injection = 0.0

st.subheader("Investissement & couts")
c1, c2, c3 = st.columns(3)
if mode == "vente_directe":
    st.caption(
        "Mode vente directe : indique ton PRIX DE REVIENT (ce que le materiel/pose "
        "te coute reellement), et la marge que tu veux appliquer -- le prix de vente "
        "au client (= le CAPEX qu'il paie, = ton gain vendeur) est calcule automatiquement."
    )
    if pv_input_mode == "catalogue":
        prix_revient_kwc_pv = prix_kwc_pv_catalogue
        c1.metric("Prix de revient PV (€/kWc)", f"{prix_revient_kwc_pv:.0f} €/kWc")
        c1.caption("Prix fixé par le modèle catalogue choisi ci-dessus.")
    else:
        prix_revient_kwc_pv = c1.number_input("Prix de revient PV (€/kWc)", value=650.0, step=10.0)
    prix_revient_kwh_batterie = c2.number_input("Prix de revient batterie (€/kWh)", value=225.0, step=10.0)
    marge_vente_pct = c3.number_input("Marge (%)", value=30.0, step=1.0, min_value=0.0, max_value=95.0,
                                       help="Marge sur prix de vente : 30% -> prix de vente = prix de revient / (1-30%) = ×1.4286")
    _marge_mult = 1.0 / (1.0 - marge_vente_pct / 100.0)
    prix_kwc_pv = prix_revient_kwc_pv * _marge_mult
    prix_kwh_batterie = prix_revient_kwh_batterie * _marge_mult
    st.caption(
        f"→ Prix de vente PV : **{fmt_number(prix_kwc_pv)} €/kWc** · Prix de vente batterie : "
        f"**{fmt_number(prix_kwh_batterie)} €/kWh** (multiplicateur ×{_marge_mult:.4f})"
    )
    maintenance_eur_an = st.number_input("Maintenance annuelle (€/an)", value=0.0, step=50.0)
else:
    if pv_input_mode == "catalogue":
        prix_kwc_pv = prix_kwc_pv_catalogue
        c1.metric("Cout PV (€/kWc)", f"{prix_kwc_pv:.0f} €/kWc")
        c1.caption("Prix fixé par le modèle catalogue choisi ci-dessus.")
    else:
        prix_kwc_pv = c1.number_input("Cout PV (€/kWc)", value=650.0, step=10.0)
    prix_kwh_batterie = c2.number_input("Cout batterie (€/kWh)", value=225.0, step=10.0)
    maintenance_eur_an = c3.number_input("Maintenance annuelle (€/an)", value=0.0, step=50.0)

st.subheader("Hypotheses financieres globales")
c1, c2, c3 = st.columns(3)
inflation_pct = c1.number_input("Inflation couts (%/an)", value=3.0, step=0.5)
degradation_pv_pct = c2.number_input("Degradation PV (%/an)", value=0.5, step=0.1)
degradation_batterie_pct = c3.number_input("Degradation batterie (%/an)", value=2.0, step=0.5)

st.subheader("Parametres du modele ROI")
c1, c2, c3 = st.columns(3)
horizon_annees = c1.number_input("Horizon d'analyse (annees)", value=15, min_value=1, max_value=30, step=1)
discount_rate = c2.number_input("Taux d'actualisation VAN/TRI (%)", value=6.0, step=0.5) / 100.0
annee_remplacement_batterie = c3.number_input("Annee de remplacement batterie (0 = jamais)", value=0, min_value=0, max_value=30, step=1)

client_name = st.text_input("Nom du client / projet (pour le rapport PDF)", value="")

submitted = st.button("🚀 Lancer la simulation", use_container_width=True)

# ---------------------------------------------------------------------
# 3. Calcul
# ---------------------------------------------------------------------
if submitted:
    params = {
        "kwc": kwc,
        "kva_onduleur": kva_onduleur,
        "battery_power_kw": battery_power_kw,
        "battery_capacity_kwh": battery_capacity_kwh,
        "contrat_kw": contrat_kw,
        "old_price_kwh_fixe": old_price_kwh_fixe,
        "price_hp": price_hp,
        "price_hc": price_hc,
        "old_price_inj_fixe": 0.0,
        "heure_debut_hp": heure_debut_hp,
        "heure_debut_hc": heure_debut_hc,
        "old_cout_additionnel_contrat": old_cout_additionnel_contrat,
        "prix_vente_kwh": prix_vente_kwh,
        "prix_rachat_surplus_kwh": prix_rachat_surplus_kwh,
        "prix_injection_fournisseur": marge_injection,
        "marge_fournisseur": marge_fournisseur,
        "taxes_couts_proportionnels": taxes_couts_proportionnels,
        "marge_injection": marge_injection,
        "tarif_capacitaire_fournisseur": tarif_capacitaire_fournisseur,
        "mode_vente": mode,
        "prix_kwc_pv": prix_kwc_pv,
        "prix_kwh_batterie": prix_kwh_batterie,
        "maintenance_eur_an": maintenance_eur_an,
        "inflation_pct": inflation_pct,
        "degradation_pv_pct": degradation_pv_pct,
        "degradation_batterie_pct": degradation_batterie_pct,
    }
    st.session_state["last_params"] = params

    # --- Resolution du fichier de prix Belpex (ignore si mode != fournisseur_principal,
    #     le moteur route automatiquement sur le tarif de reference dans ce cas) ---
    if belpex_source == "upload":
        if belpex_upload_file is None:
            st.error("Choisis une annee integree ou charge un fichier .pkl pour les prix Belpex.")
            st.stop()
        dayahead_path = os.path.join(WORKDIR, "belpex_upload.pkl")
        with open(dayahead_path, "wb") as f:
            f.write(belpex_upload_file.getbuffer())
    elif belpex_source == "demo":
        dayahead_path = "__no_file__"  # force le mode demo dans load_dayahead_prices
    elif belpex_source == "auto":
        dayahead_path = None  # resolu plus bas, une fois les vraies dates client connues (df)
    else:
        dayahead_path = os.path.join(BELPEX_DATA_DIR, f"belpex_{belpex_source}_qh.pkl")
        if belpex_date_fin is not None:
            # Tranche demandee : 1er janvier -> belpex_date_fin de l'annee source. On ecrit
            # une copie tronquee du .pkl (meme pattern que l'upload) -- load_dayahead_prices
            # n'utilisera alors, pour son calage jour/mois/heure, que les prix de cette tranche.
            _full_series = pd.read_pickle(dayahead_path)
            _tz = _full_series.index.tz
            _fin_ts = pd.Timestamp(belpex_date_fin, tz=_tz) + pd.Timedelta(hours=23, minutes=45)
            _tronquee = _full_series[_full_series.index <= _fin_ts]
            dayahead_path = os.path.join(WORKDIR, f"belpex_{belpex_source}_tronque.pkl")
            _tronquee.to_pickle(dayahead_path)
            st.caption(
                f"Prix Day-Ahead : source '{belpex_source}' limitée à la tranche "
                f"1er janvier -> {belpex_date_fin.strftime('%d/%m/%Y')} "
                f"({len(_tronquee)} points sur {len(_full_series)})."
            )

    try:
        # pv_profile_1kwc a deja ete resolu plus haut (source Excel fige OU
        # PVGIS, selon le choix fait dans "1. Donnees du site"). La conso
        # peut venir d'un CSV uploade OU d'un profil-type integre mis a
        # l'echelle (conso_series_profile), selon le choix fait plus haut.
        with st.spinner("Lecture de la consommation et alignement avec le profil PV..."):
            df = build_timeseries_from_sources(
                conso_series_profile if conso_series_profile is not None else conso_file,
                pv_profile_1kwc, kwc=kwc, unit=conso_unit)

        n_truncated_qh = df.attrs.get("n_truncated_qh", 0)
        if n_truncated_qh:
            st.warning(
                f"⚠️ Le relevé de consommation dépasse 12 mois : {n_truncated_qh} pas de temps "
                f"({n_truncated_qh * 15 / 60:.0f} h) au-delà de la première année ont été ignorés. "
                f"La simulation tourne sur exactement 1 an, à partir de la première date du relevé."
            )

        if dayahead_path is None:  # belpex_source == "auto"
            annees_client = sorted(set(df.index.year.tolist()))
            annee_choisie = next((str(a) for a in annees_client if str(a) in _belpex_annees_dispo),
                                  max(_belpex_annees_dispo))
            dayahead_path = os.path.join(BELPEX_DATA_DIR, f"belpex_{annee_choisie}_qh.pkl")
            st.caption(
                f"Prix Day-Ahead : année Belpex '{annee_choisie}' utilisée comme référence "
                f"(calée jour par jour sur les dates réelles {annees_client[0]}-{annees_client[-1]})."
            )

        progress_bar = st.progress(0.0, text="Simulation en cours...")

        def _progress(year, total):
            progress_bar.progress(year / total, text=f"Simulation en cours... annee {year}/{total}")

        with st.spinner("Calcul du dispatch Day-Ahead sur l'horizon complet (peut prendre plusieurs minutes)..."):
            results = run_fournisseur_model_from_data(
                params=params,
                df=df,
                dayahead_pkl_path=dayahead_path,
                horizon_annees=int(horizon_annees),
                discount_rate=discount_rate,
                annee_remplacement_batterie=(int(annee_remplacement_batterie) if annee_remplacement_batterie else None),
                cout_remplacement_batterie_eur=None,
                rapport_txt_path=os.path.join(WORKDIR, "rapport.txt"),
                progress_callback=_progress,
            )
        progress_bar.progress(1.0, text="Termine !")
        st.session_state["results"] = results
        st.session_state["client_name"] = client_name
        if results["bilan_energetique_annee1"]["pv_total_kwh"] <= 0 and params["kwc"] > 0:
            st.warning("⚠️ Le PV calcule est a 0 alors que kWc > 0 -- verifie les coordonnees "
                       "et l'orientation saisies.")
        st.success("Simulation terminee.")
    except Exception as e:
        st.error(f"Erreur pendant la simulation : {e}")
        st.session_state["results"] = None

# ---------------------------------------------------------------------
# 4. Resultats
# ---------------------------------------------------------------------
results = st.session_state.get("results")
if results:
    mode_label = st.session_state.get("mode", "fournisseur_principal")
    st.header("3. Resultats")

    if results.get("is_demo_prices"):
        st.warning("⚠️ Prix Day-Ahead en MODE DEMO (simules) -- charge un vrai fichier "
                   "de prix Belpex avant d'envoyer ces chiffres a un client.")

    df = results["detail_annuel"]
    er = results["bilan_energetique_annee1"]
    pv_total = er.get("pv_total_kwh", 0.0) or 0.0
    conso_total = er.get("conso_total_kwh", 0.0) or 0.0
    pv_exporte = er.get("pv_exporte_kwh", 0.0) or 0.0
    pv_curtail = er.get("curtail_kwh", 0.0) or 0.0
    # Ce calcul est INDIRECT (deduit de import/export), pas trace flux par flux
    # dans le moteur. En mode fournisseur_principal (arbitrage Belpex), la
    # batterie peut se charger avec de l'electricite reseau bon marche puis la
    # revendre plus cher -- ce qui reduit l'import reseau SANS rapport avec le
    # PV, et peut donc gonfler ce ratio au-dela de sa vraie valeur physique.
    # On le borne a [0, PV total] pour eviter une autoconsommation > 100%,
    # mais en mode arbitrage ce chiffre reste une approximation -- fiable
    # surtout en mode fournisseur_secondaire / vente_directe (pas d'arbitrage
    # reseau, la batterie ne fait que stocker le surplus PV).
    pv_utilisee = min(max(pv_total - pv_exporte - pv_curtail, 0.0), pv_total)
    autoconsommation_pct = 100 * pv_utilisee / pv_total if pv_total > 0 else 0.0
    autonomie_pct = 100 * pv_utilisee / conso_total if conso_total > 0 else 0.0
    if mode_label == "fournisseur_principal" and has_battery:
        st.info(
            "ℹ️ Autoconsommation/autonomie ci-dessous : estimation **indirecte** (déduite de "
            "l'import/export réseau, pas tracée flux par flux), moins fiable en mode arbitrage "
            "Belpex -- la batterie peut se charger avec de l'électricité réseau bon marché puis "
            "la revendre, ce qui réduit l'import sans rapport avec le PV et peut gonfler ces "
            "chiffres au-delà de leur vraie valeur physique. Fiable surtout en mode fournisseur "
            "secondaire / vente directe (pas d'arbitrage réseau)."
        )

    if results.get("pv_clip_onduleur_kwh", 0.0) > 0:
        st.info(
            f"ℹ️ Écrêtement onduleur : **{fmt_number(results['pv_clip_onduleur_kwh'], suffix='kWh/an')}** de "
            f"production PV perdue car la puissance instantanée dépassait la puissance "
            f"nominale de l'onduleur ({kva_onduleur:.1f} kVA). Augmente la puissance de "
            f"l'onduleur (ou choisis un autre modèle catalogue) pour réduire cette perte."
        )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("CAPEX total", fmt_eur(results['capex_total']))
    m2.metric("Payback", f"{results['payback_year']:.1f} ans" if results["payback_year"] else "Non atteint")
    m3.metric("VAN", fmt_eur(results['npv_eur']),
              help="Valeur Actuelle Nette : cash-flows **actualisés** (taux d'actualisation "
                   "appliqué) sur tout l'horizon -- prend en compte que 1 EUR futur vaut moins "
                   "que 1 EUR aujourd'hui.")
    if results["irr_pct"] == results["irr_pct"]:  # pas NaN
        _tri_txt = f"{results['irr_pct']:.1f} %"
    else:
        from engine.finance_utils import irr_failure_reason
        _tri_cashflows = [-results["capex_total"]] + df["cashflow_annuel_eur"].tolist()
        _tri_reason = irr_failure_reason(_tri_cashflows)
        _tri_txt = {
            "toujours_rentable": "Non calculable (rentabilité très élevée)",
            "jamais_rentable": "Non calculable (jamais rentable)",
        }.get(_tri_reason, "N/A")
    m4.metric("TRI", _tri_txt,
              help="Taux de Rentabilité Interne : le taux d'actualisation pour lequel la VAN "
                   "serait nulle -- **actualisé**, comparable à un taux de placement financier. "
                   "Non calculable si aucune solution n'a été trouvée dans la plage testée "
                   "(taux de -99% à +1000%) : soit les cash-flows sont toujours positifs "
                   "(rentabilité hors plage, très favorable), soit toujours négatifs "
                   "(le projet ne se rembourse jamais sur cet horizon).")

    m5, m6, m7, m8, m9 = st.columns(5)
    m5.metric("Economie client an 1", fmt_eur(df.loc[0,'economie_client_eur']))
    m6.metric(f"Economie client cumulee ({len(df)} ans)", fmt_eur(results['total_economie_client']))
    m7.metric(
        "Gain brut cumule (toi)" if mode_label in ("fournisseur_principal", "fournisseur_secondaire") else "Gain vendeur (= CAPEX encaisse)",
        fmt_eur(results['gain_vendeur_eur']))
    m8.metric("ROI net non-actualise", f"{results['roi_pct']:.1f} %",
              help="Retour sur investissement **NON actualisé** : simple somme brute des "
                   "cash-flows sur l'horizon, rapportée au CAPEX -- ne tient PAS compte du fait "
                   "qu'un gain lointain vaut moins qu'un gain immédiat. À ne pas comparer "
                   "directement au TRI (lui, actualisé) : les deux mesurent des choses différentes.")
    m9.metric(
        "Gain brut fournisseur an 1" if mode_label in ("fournisseur_principal", "fournisseur_secondaire") else "Gain vendeur an 1 (CAPEX encaisse a la vente)",
        fmt_eur(df.loc[0,'gain_brut_fournisseur_eur']))

    m10, m11, m12, m13, m14 = st.columns(5)
    m10.metric("Consommation totale client (an 1)", fmt_number(conso_total, suffix="kWh/an"))
    m13.metric("Consommation totale apres asset (an 1)",
               fmt_number(er['import_kwh'], suffix="kWh/an"))
    m11.metric("Autoconsommation PV", f"{autoconsommation_pct:.1f} %")
    m12.metric("Autonomie énergétique", f"{autonomie_pct:.1f} %")
    m14.metric("Injection réseau (an 1)", fmt_number(er['export_kwh'], suffix="kWh/an"))

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["Cashflow & Economie client", "Origine du gain", "Detail annuel",
         "Profil quart-horaire (annee 1)", "Repartition saisonniere"])

    with tab1:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["annee"], y=df["cashflow_cumule_eur"],
                                  mode="lines+markers", name="Cashflow cumule",
                                  line=dict(color="#1B2A4A", width=3)))
        fig.add_hline(y=0, line_dash="dash", line_color="grey")
        fig.update_layout(title="Cashflow cumule (CAPEX inclus)",
                           xaxis_title="Annee", yaxis_title="EUR", height=380)
        st.plotly_chart(fig, use_container_width=True)

        fig2 = go.Figure()
        fig2.add_trace(go.Bar(x=df["annee"], y=df["economie_client_eur"], marker_color="#F5A623"))
        fig2.update_layout(title="Economie annuelle pour le client",
                            xaxis_title="Annee", yaxis_title="EUR/an", height=350)
        st.plotly_chart(fig2, use_container_width=True)

        # Cout reseau avant/apres PV -- uniquement pertinent pour les modes
        # sans marche Day-Ahead (vente directe / fournisseur secondaire), ou
        # le "cout reseau" se lit directement sur un tarif de reference
        # (fixe ou HP/HC) applique a la conso brute vs a l'import residuel
        # apres PV+batterie. En mode fournisseur_principal, le cout depend
        # du marche Belpex heure par heure -- ce graphe n'a pas de sens la.
        serie_cout = results.get("serie_qh_annee1")
        if (results.get("mode_vente") != "fournisseur_principal"
                and serie_cout is not None and not serie_cout.empty
                and "price_reference_eur_kwh" in serie_cout.columns):

            NAVY = "#1B2A4A"
            TEAL = "#2EC4B6"
            kwc = results["params"]["kwc"]
            prix_rachat = results["params"].get("prix_rachat_surplus_kwh", 0.0)

            cout_avant = serie_cout["conso_kwh"] * serie_cout["price_reference_eur_kwh"]

            if results.get("mode_vente") == "fournisseur_secondaire":
                # Le client achete SEULEMENT la production PV au fournisseur
                # (au nouveau prix), et continue d'acheter le residuel a son
                # ancien fournisseur/tarif -- inchange. Pas de credit d'export
                # ici : le surplus PV non consomme appartient au fournisseur
                # (c'est lui l'operateur de l'asset), pas au client.
                # cout_apres = (conso-pv)*ancien_prix + pv*nouveau_prix
                #            = cout_avant - pv*(ancien_prix - nouveau_prix)
                prix_vente_kwh = results["params"]["prix_vente_kwh"]
                cout_apres = cout_avant - serie_cout["pv_kwh"] * (
                    serie_cout["price_reference_eur_kwh"] - prix_vente_kwh)
            else:
                # vente_directe : le client possede l'asset, l'autoconsommation
                # PV ne lui coute rien -- seul l'echange net avec le reseau
                # compte (import au tarif reseau, export credite au rachat).
                cout_apres = (serie_cout["import_kwh"] * serie_cout["price_reference_eur_kwh"]
                              - serie_cout["export_kwh"] * prix_rachat)

            mois_labels = ["Jan", "Fev", "Mar", "Avr", "Mai", "Jun",
                           "Jul", "Aou", "Sep", "Oct", "Nov", "Dec"]
            avant_mensuel = cout_avant.groupby(serie_cout.index.month).sum().reindex(range(1, 13), fill_value=0.0)
            apres_mensuel = cout_apres.groupby(serie_cout.index.month).sum().reindex(range(1, 13), fill_value=0.0)

            st.markdown(f"**Cout reseau : AVANT vs APRES PV ({kwc:.0f} kWc)**")

            fig_mois = go.Figure()
            fig_mois.add_trace(go.Bar(x=mois_labels, y=avant_mensuel.values, name="Cout AVANT PV",
                                       marker_color=NAVY))
            fig_mois.add_trace(go.Bar(x=mois_labels, y=apres_mensuel.values, name="Cout APRES PV",
                                       marker_color=TEAL))
            fig_mois.update_layout(
                title=f"Cout mensuel reseau : AVANT vs APRES PV ({kwc:.0f} kWc)",
                xaxis_title="", yaxis_title="EUR", barmode="group", height=460,
                bargap=0.35, bargroupgap=0.15,
                legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="center", x=0.5),
                plot_bgcolor="#F8F9FA",
            )
            st.plotly_chart(fig_mois, use_container_width=True, key="cout_mensuel_avant_apres")

            total_avant = float(avant_mensuel.sum())
            total_apres = float(apres_mensuel.sum())
            fig_annuel = go.Figure()
            fig_annuel.add_trace(go.Bar(
                y=["APRES PV", "AVANT PV"], x=[total_apres, total_avant], orientation="h",
                marker_color=[TEAL, NAVY],
                text=[fmt_eur(total_apres), fmt_eur(total_avant)],
                textposition="inside", insidetextanchor="end", textfont=dict(color="white", size=13),
            ))
            fig_annuel.update_layout(
                title=f"Cout annuel reseau : AVANT vs APRES PV ({kwc:.0f} kWc)",
                xaxis_title="EUR", height=320, showlegend=False,
                bargap=0.5,
                plot_bgcolor="#F8F9FA", margin=dict(t=50, b=30),
            )
            st.plotly_chart(fig_annuel, use_container_width=True, key="cout_annuel_avant_apres")

            if results.get("mode_vente") == "fournisseur_secondaire":
                st.caption(
                    "AVANT = conso totale x ancien tarif reseau. APRES = residuel reseau "
                    "(conso - PV) x ancien tarif + production PV x nouveau prix -- le client "
                    "n'achete QUE la production PV a toi, le reste continue d'etre achete a "
                    "son fournisseur/tarif habituel (inchange). Pas de credit d'export : le "
                    "surplus PV eventuel t'appartient (l'operateur de l'asset), pas au client."
                )
            else:
                st.caption(
                    "AVANT = conso totale x ancien tarif reseau. APRES = import residuel "
                    "(post PV+batterie) x tarif reseau, moins le surplus PV exporte credite au "
                    "tarif de rachat -- le client possede l'asset, son autoconsommation PV ne "
                    "lui coute rien, seul l'echange net avec le reseau compte."
                )

    with tab2:
        decomp = results["decomposition_gains"]
        c1, c2 = st.columns([1, 1])
        with c1:
            fig3 = go.Figure(data=[go.Pie(
                labels=["PV", "Batterie / Belpex"],
                values=[max(decomp["gain_pv_eur"], 0), max(decomp["gain_batterie_belpex_eur"], 0)],
                marker_colors=["#F5A623", "#1B2A4A"])])
            fig3.update_layout(title="Origine du gain -- annee 1", height=380)
            st.plotly_chart(fig3, use_container_width=True)
        with c2:
            st.markdown("**Decomposition en cascade (sans double comptage)**")
            st.write(f"- Cout sans assets : **{fmt_eur(decomp['cout_sans_rien_eur'])}**")
            st.write(f"- Cout avec PV : **{fmt_eur(decomp['cout_pv_seul_eur'])}**")
            st.write(f"- Cout avec PV + batterie : **{fmt_eur(decomp['cout_pv_batterie_eur'])}**")
            st.write(f"- Gain PV : **{fmt_eur(decomp['gain_pv_eur'])}**")
            st.write(f"- Gain Batterie : **{fmt_eur(decomp['gain_batterie_belpex_eur'])}**")
            if decomp["gain_pv_eur"] < 0 or decomp["gain_batterie_belpex_eur"] < 0:
                st.warning(
                    "⚠️ Un gain décomposé est négatif : cas limite du dispatch (le tarif "
                    "capacitaire ou une contrainte de contrat peut rendre l'ajout de PV/batterie "
                    "ponctuellement défavorable dans cette décomposition). Le camembert ci-contre "
                    "plafonne à 0 pour rester lisible -- vérifie le gain total et les hypothèses "
                    "(contrat souscrit, tarif capacitaire) si ce chiffre te surprend."
                )

        st.markdown("**Bilan energetique annee 1**")
        st.write(f"PV produit : {fmt_number(pv_total, suffix='kWh/an')}")
        st.write(f"Consommation totale client : {fmt_number(conso_total, suffix='kWh/an')}")
        st.write(f"Importé du reseau : {fmt_number(er['import_kwh'], suffix='kWh/an')}")
        st.write(f"Exporté au reseau : {fmt_number(er['export_kwh'], suffix='kWh/an')}")
        st.write(f"Autoconsommation PV (prod. PV utilisée / prod. PV totale) : {autoconsommation_pct:.1f} %")
        st.write(f"Autonomie énergétique (prod. PV utilisée / conso. totale client) : {autonomie_pct:.1f} %")
        st.write(f"Cycles équivalents batterie : {er['cycles_equivalents']:.1f}")

    with tab3:
        st.dataframe(df.style.format({
            "revenu_client_eur": "{:,.0f}",
            "cout_approvisionnement_eur": "{:,.0f}",
            "gain_brut_fournisseur_eur": "{:,.0f}",
            "economie_client_eur": "{:,.0f}",
            "cashflow_cumule_eur": "{:,.0f}",
        }), use_container_width=True)

    with tab4:
        serie = results.get("serie_qh_annee1")
        if serie is None or serie.empty:
            st.info("Pas de serie quart-horaire disponible pour cette simulation.")
        else:
            date_min = serie.index.min().date()
            date_max = serie.index.max().date()

            cnav1, cnav2, cnav3 = st.columns([1, 2, 1])
            vue = cnav1.selectbox("Vue", ["Annee complete", "Annee", "Mois", "Semaine", "Jour"], index=0)
            date_focus = cnav2.date_input(
                "Aller a cette date", value=date_min, min_value=date_min, max_value=date_max,
                disabled=(vue == "Annee complete"))
            resolution = cnav3.radio("Resolution", ["Quart-horaire", "Horaire"], index=0,
                                      disabled=(vue != "Jour"))

            if vue == "Jour":
                x_start = pd.Timestamp(date_focus)
                x_end = x_start + pd.Timedelta(days=1)
            elif vue == "Semaine":
                x_start = pd.Timestamp(date_focus)
                x_end = x_start + pd.Timedelta(days=7)
            elif vue == "Mois":
                x_start = pd.Timestamp(date_focus).replace(day=1)
                x_end = x_start + pd.DateOffset(months=1)
            elif vue == "Annee":
                x_start = pd.Timestamp(year=date_focus.year, month=1, day=1)
                x_end = pd.Timestamp(year=date_focus.year + 1, month=1, day=1)
            else:
                x_start, x_end = serie.index.min(), serie.index.max()

            # Sous "Jour", le pas natif (15 min / horaire) reste lisible.
            # Au-dela, 672 points (semaine) a 35040 points (annee) en quart-heure
            # rendent la courbe illisible -- on bascule automatiquement sur une
            # vue "tendance" agregee (moyenne horaire pour la semaine, moyenne
            # journaliere pour mois/annee). La moyenne (pas la somme) preserve
            # la conversion *4 -> kW, quelle que soit la maille d'agregation.
            fenetre = serie.loc[x_start:x_end]
            if vue == "Jour":
                if resolution == "Horaire":
                    serie_plot = fenetre.resample("1h").mean()
                    pas_label = "horaire (moyenne des 4 quarts d'heure)"
                else:
                    serie_plot = fenetre
                    pas_label = "quart-horaire natif (15 min)"
            elif vue == "Semaine":
                serie_plot = fenetre.resample("1h").mean()
                pas_label = "horaire -- vue tendance (moyenne par heure, plus lisible sur 7 jours)"
            else:
                serie_plot = fenetre.resample("1D").mean()
                pas_label = "journalier -- vue tendance (moyenne par jour, plus lisible sur mois/annee)"

            st.caption(
                f"Pas {pas_label}. Belpex reste natif au marche Day-Ahead (deja horaire, "
                f"seulement re-agrege visuellement si vue horaire/journaliere). "
                f"Clique un nom de courbe ci-dessous pour l'afficher/masquer -- "
                f"ca reste actif meme en changeant de date."
            )

            # Palette de couleurs
            PALETTE = dict(conso="#2E4057", pv="#F2A65A", import_="#87A878", soc="#B784A7", belpex="#E15554")
            LARGEUR_TRAIT = 3.0

            fig_qh = go.Figure()
            fig_qh.add_trace(go.Scattergl(
                x=serie_plot.index, y=serie_plot["conso_kwh"] * 4, name="Consommation client",
                mode="lines", line=dict(color=PALETTE["conso"], width=LARGEUR_TRAIT), yaxis="y1"))
            fig_qh.add_trace(go.Scattergl(
                x=serie_plot.index, y=serie_plot["pv_kwh"] * 4, name="Production PV",
                mode="lines", line=dict(color=PALETTE["pv"], width=LARGEUR_TRAIT),
                fill="tozeroy", fillcolor="rgba(242,166,90,0.18)", yaxis="y1"))
            fig_qh.add_trace(go.Scattergl(
                x=serie_plot.index, y=serie_plot["import_kwh"] * 4, name="Consommation apres pilotage (import reseau)",
                mode="lines", line=dict(color=PALETTE["import_"], width=LARGEUR_TRAIT), yaxis="y1"))
            fig_qh.add_trace(go.Scattergl(
                x=serie_plot.index, y=serie_plot["soc_pct"], name="Etat batterie",
                mode="lines", line=dict(color=PALETTE["soc"], width=LARGEUR_TRAIT, dash="dot"), yaxis="y2"))
            if "belpex_eur_kwh" in serie_plot.columns:
                fig_qh.add_trace(go.Scattergl(
                    x=serie_plot.index, y=serie_plot["belpex_eur_kwh"], name="Prix Belpex",
                    mode="lines", line=dict(color=PALETTE["belpex"], width=LARGEUR_TRAIT),
                    yaxis="y3", visible="legendonly"))

            # La legende garde un uirevision FIXE ("qh_legend_persistent") --
            # donc les courbes masquees/affichees via un clic restent dans cet
            # etat meme apres un changement de date/vue/resolution (qui
            # recree la figure a chaque rerun Streamlit). L'axe X, lui, recoit
            # un uirevision qui CHANGE avec la vue/date/resolution, pour que
            # le nouveau range demande (x_start/x_end) soit toujours applique
            # plutot que de conserver un ancien zoom manuel -- comportement
            # volontairement different de celui de la legende. Le "key" fixe
            # sur st.plotly_chart est indispensable : sans lui, Streamlit
            # recree le composant a chaque rerun et TOUT etat cote client
            # (legende, zoom) est perdu, quel que soit l'uirevision.
            fig_qh.update_layout(
                height=580,
                title=dict(text="Profil energetique -- annee 1", font=dict(size=16, color="#2E4057")),
                font=dict(family="Helvetica, Arial, sans-serif", size=12, color="#4A4A4A"),
                plot_bgcolor="#F8F9FA",
                paper_bgcolor="rgba(0,0,0,0)",
                hovermode="x unified",
                xaxis=dict(
                    title="", range=[x_start, x_end],
                    rangeslider=dict(visible=True, thickness=0.06, bgcolor="#E9ECEF"),
                    showgrid=True, gridcolor="#E2E8F0",
                    uirevision=f"{vue}|{date_focus}|{resolution}",
                ),
                yaxis=dict(title="kW", side="left", showgrid=True, gridcolor="#E2E8F0", zeroline=False),
                yaxis2=dict(title="Batterie (%)", overlaying="y", side="right", range=[0, 100], showgrid=False),
                yaxis3=dict(title="Belpex (EUR/kWh)", overlaying="y", side="right",
                            position=0.97, showgrid=False, anchor="free"),
                legend=dict(orientation="h", yanchor="top", y=-0.35, xanchor="center", x=0.5,
                            uirevision="qh_legend_persistent"),
                margin=dict(t=50, b=10),
            )
            st.plotly_chart(fig_qh, use_container_width=True, key="qh_chart")

    with tab5:
        serie = results.get("serie_qh_annee1")
        if serie is None or serie.empty:
            st.info("Pas de serie quart-horaire disponible pour cette simulation.")
        else:
            # Saisons astronomiques (bornes au 21 des mois, comme demande) :
            # Hiver 21 dec -> 20 mars, Printemps 21 mars -> 20 juin,
            # Ete 21 juin -> 20 sept, Automne 21 sept -> 20 dec.
            SEASON_ORDER = ["Hiver", "Printemps", "Ete", "Automne"]
            SEASON_COLORS = {"Hiver": "#4472C4", "Printemps": "#70AD47", "Ete": "#FFC000", "Automne": "#ED7D31"}
            COULEURS_HPHC_AVANT = ["#1F4E79", "#5B9BD5"]   # bleu fonce / bleu clair
            COULEURS_HPHC_APRES = ["#1E8449", "#48C9B0"]   # vert fonce / turquoise

            def _season_of(idx):
                m, d = idx.month, idx.day
                hiver = ((m == 12) & (d >= 21)) | np.isin(m, [1, 2]) | ((m == 3) & (d < 21))
                printemps = ((m == 3) & (d >= 21)) | np.isin(m, [4, 5]) | ((m == 6) & (d < 21))
                ete = ((m == 6) & (d >= 21)) | np.isin(m, [7, 8]) | ((m == 9) & (d < 21))
                return np.select([hiver, printemps, ete], ["Hiver", "Printemps", "Ete"], default="Automne")

            heure_hp = results["params"]["heure_debut_hp"]
            heure_hc = results["params"]["heure_debut_hc"]
            has_hphc = bool(results["params"].get("price_hp")) or bool(results["params"].get("price_hc"))
            label_hp = f"HP ({heure_hp:.0f}h-{heure_hc:.0f}h)"
            label_hc = f"HC ({heure_hc:.0f}h-{heure_hp:.0f}h)"

            heures = serie.index.hour
            is_hp = (heures >= heure_hp) & (heures < heure_hc)
            base = pd.DataFrame({
                "saison": _season_of(serie.index),
                "hphc": np.where(is_hp, label_hp, label_hc),
                "conso_kwh": serie["conso_kwh"].values,
                "import_kwh": serie["import_kwh"].values,
            }, index=serie.index)

            def _pct_par_saison(col):
                tot = base.groupby("saison")[col].sum().reindex(SEASON_ORDER, fill_value=0.0)
                grand_total = tot.sum()
                return 100 * tot / grand_total if grand_total > 0 else tot

            def _pct_hphc_saison(col, saison):
                sub = base.loc[base["saison"] == saison]
                tot = sub.groupby("hphc")[col].sum().reindex([label_hp, label_hc], fill_value=0.0)
                grand_total = tot.sum()
                return 100 * tot / grand_total if grand_total > 0 else tot

            def _pie(labels, values, colors, title, key):
                fig = go.Figure(data=[go.Pie(
                    labels=labels, values=values, marker_colors=colors,
                    textinfo="percent", textfont=dict(size=11), hole=0.0)])
                fig.update_layout(
                    title=dict(text=title, font=dict(size=13)), height=270,
                    margin=dict(t=40, b=10, l=10, r=10), showlegend=True,
                    legend=dict(orientation="h", y=-0.15, font=dict(size=10)),
                )
                st.plotly_chart(fig, use_container_width=True, key=key)

            st.caption(
                "Saisons astronomiques (21 decembre / 21 mars / 21 juin / 21 septembre). "
                "1ere colonne : part de chaque saison dans le total annuel (100% sur les 4 saisons)."
                + (" Colonnes HP/HC : part HP vs HC A L'INTERIEUR de chaque saison (100% par "
                   "saison, pas sur l'annee entiere)." if has_hphc else
                   " Le client est sur un tarif reseau FIXE (pas de HP/HC renseigne) -- "
                   "pas de decoupe HP/HC a afficher ici.")
            )

            st.markdown("**Avant PV -- consommation brute du client**")
            row1 = st.columns(5) if has_hphc else st.columns(5)[:1]
            pct_avant = _pct_par_saison("conso_kwh")
            with row1[0]:
                _pie(pct_avant.index, pct_avant.values,
                     [SEASON_COLORS[s] for s in pct_avant.index],
                     "Conso par saison", "season_avant_global")
            if has_hphc:
                for i, saison in enumerate(SEASON_ORDER):
                    pct = _pct_hphc_saison("conso_kwh", saison)
                    with row1[i + 1]:
                        _pie(pct.index, pct.values, COULEURS_HPHC_AVANT,
                             f"HP/HC - {saison}", f"season_avant_hphc_{saison}")

            st.markdown("**Apres PV + batterie -- ce qu'il reste a importer du reseau**")
            row2 = st.columns(5) if has_hphc else st.columns(5)[:1]
            pct_apres = _pct_par_saison("import_kwh")
            with row2[0]:
                _pie(pct_apres.index, pct_apres.values,
                     [SEASON_COLORS[s] for s in pct_apres.index],
                     "Conso reseau/saison (apres PV)", "season_apres_global")
            if has_hphc:
                for i, saison in enumerate(SEASON_ORDER):
                    pct = _pct_hphc_saison("import_kwh", saison)
                    with row2[i + 1]:
                        _pie(pct.index, pct.values, COULEURS_HPHC_APRES,
                             f"HP/HC apres PV - {saison}", f"season_apres_hphc_{saison}")

    if mode_label == "fournisseur_secondaire":
        st.header("4bis. Fiche financiere complete (modele Excel)")
        st.caption(
            "Reproduit l'onglet Excel (financement par emprunt, loyer, maintenance, "
            "CV) -- les champs ci-dessous sont pre-remplis depuis la simulation quand "
            "c'est pertinent, mais restent modifiables avant de generer le PDF."
        )
        _fp_params = results["params"]
        _pv_prod_an1 = er.get("pv_total_kwh", 0.0) or 0.0

        with st.expander("Parametres de la fiche financiere", expanded=True):
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                st.markdown("**Details projet & CAPEX**")
                fp_marge = st.number_input("Marge", value=1.3, step=0.1, key="fp_marge")
                fp_subsides_pct = st.number_input("Subsides (%)", value=20.0, step=1.0, key="fp_subsides") / 100
                fp_annees_exploit = st.number_input("Annees d'exploitation FW", value=20, step=1, key="fp_annees_exploit")
                st.markdown("**Donnees de production**")
                fp_kwc = st.number_input("Puissance PV (kWc)", value=float(_fp_params["kwc"]), step=1.0, key="fp_kwc")
                fp_batt_kva = st.number_input("Puissance batterie (kVA)", value=float(_fp_params.get("battery_power_kw", 0.0) or 0.0), step=1.0, key="fp_batt_kva")
                fp_prod_pv = st.number_input("Production PV estimee (kWh/an)", value=float(_pv_prod_an1), step=100.0, key="fp_prod_pv")
                fp_prod_eol = st.number_input("Production eolienne estimee (kWh/an)", value=0.0, step=100.0, key="fp_prod_eol")
                fp_valeur_cv = st.number_input("Valeur CV (EUR/MWh)", value=0.0, step=1.0, key="fp_valeur_cv")
            with fc2:
                st.markdown("**Prix**")
                fp_indexation = st.number_input("Indexation (%/an)", value=0.0, step=0.1, key="fp_indexation") / 100
                fp_prix_vente = st.number_input("Prix de vente selectionne (EUR/kWh)", value=float(_fp_params.get("prix_vente_kwh", 0.16) or 0.16), step=0.001, format="%.3f", key="fp_prix_vente")
                fp_revenu_batt = st.number_input("Revenu annuel batterie (EUR/MVA)", value=0.0, step=1.0, key="fp_revenu_batt")
                st.markdown("**Donnees financieres & fiscales**")
                fp_apport_pct = st.number_input("Apport (%)", value=0.0, step=1.0, key="fp_apport") / 100
                fp_duree_fin = st.number_input("Duree du financement (ans)", value=6, step=1, key="fp_duree_fin")
                fp_taux_int = st.number_input("Taux d'interet (%)", value=2.82, step=0.01, format="%.2f", key="fp_taux_int") / 100
            with fc3:
                st.markdown("**Loyer + Maintenance**")
                fp_surface = st.number_input("Surface utile (m2)", value=0.0, step=10.0, key="fp_surface")
                fp_loyer_m2 = st.number_input("Loyer surface (EUR/m2)", value=0.0, step=0.5, key="fp_loyer_m2")
                fp_indexation_loyer = st.number_input(
                    "Indexation loyer (%)", value=0.0, step=0.1, key="fp_indexation_loyer",
                    help="⚠️ Champ affiché pour fidélité avec le classeur Excel source, mais "
                         "SANS EFFET sur le calcul : le loyer est en réalité indexé sur "
                         "'Indexation Maintenance/Nettoyage' ci-dessous (reproduction fidèle "
                         "d'une particularité de l'Excel d'origine).") / 100
                fp_assurance_pct = st.number_input("Frais assurance (% du CAPEX)", value=0.0, step=0.1, key="fp_assurance") / 100
                fp_maintenance_kwc = st.number_input("Frais Maintenance/Monitoring (EUR/kWc)", value=7.0, step=0.5, key="fp_maintenance_kwc")
                fp_nettoyage_kwc = st.number_input("Nettoyage (EUR/kWc)", value=0.0, step=0.5, key="fp_nettoyage_kwc")
                fp_indexation_maint = st.number_input("Indexation Maintenance/Nettoyage (%)", value=0.0, step=0.1, key="fp_indexation_maint") / 100

        fp_site_name = st.text_input("Nom du site (ex: Residence Amadeus)", value="", key="fp_site_name")

        if st.button("📋 Generer la fiche financiere (PDF)", use_container_width=True):
            from engine.financial_sheet import compute_fiche, build_fiche_pdf
            _fiche_params = {
                "marge": fp_marge, "puissance_batterie_kva": fp_batt_kva,
                "subsides_pct": fp_subsides_pct, "kwc": fp_kwc,
                "production_pv_estimee_kwh": fp_prod_pv, "production_eolienne_kwh": fp_prod_eol,
                "valeur_cv_eur_mwh": fp_valeur_cv, "indexation_pct": fp_indexation,
                "prix_vente_kwh": fp_prix_vente, "revenu_batterie_eur_mva": fp_revenu_batt,
                "apport_pct": fp_apport_pct, "duree_financement_annees": fp_duree_fin,
                "taux_interet": fp_taux_int, "annees_exploitation_fw": fp_annees_exploit,
                "surface_utile_m2": fp_surface, "loyer_m2": fp_loyer_m2,
                "indexation_loyer_pct": fp_indexation_loyer, "frais_assurance_pct": fp_assurance_pct,
                "frais_maintenance_eur_kwc": fp_maintenance_kwc, "nettoyage_eur_kwc": fp_nettoyage_kwc,
                "indexation_maintenance_pct": fp_indexation_maint,
            }
            _fiche = compute_fiche(_fiche_params)
            fiche_pdf_path = os.path.join(WORKDIR, "fiche_financiere.pdf")
            with st.spinner("Generation de la fiche PDF..."):
                build_fiche_pdf(_fiche, fiche_pdf_path,
                                 client_name=st.session_state.get("client_name", ""),
                                 site_name=fp_site_name)
            with open(fiche_pdf_path, "rb") as f:
                st.session_state["fiche_pdf_bytes"] = f.read()

        if "fiche_pdf_bytes" in st.session_state:
            st.download_button("⬇️ Telecharger la fiche financiere (PDF)",
                                data=st.session_state["fiche_pdf_bytes"],
                                file_name="fiche_financiere.pdf", mime="application/pdf",
                                use_container_width=True)

    st.header("4. Exports")
    e1, e2 = st.columns(2)

    with e1:
        if st.button("📄 Generer le rapport PDF", use_container_width=True):
            pdf_path = os.path.join(WORKDIR, "rapport_energetique.pdf")
            with st.spinner("Generation du PDF..."):
                build_pdf_report(results, pdf_path, client_name=st.session_state.get("client_name", ""))
            with open(pdf_path, "rb") as f:
                st.session_state["pdf_bytes"] = f.read()

        if "pdf_bytes" in st.session_state:
            st.download_button("⬇️ Telecharger le PDF", data=st.session_state["pdf_bytes"],
                                file_name="rapport_energetique.pdf", mime="application/pdf",
                                use_container_width=True)

    with e2:
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Telecharger le detail (CSV)", data=csv_bytes,
                            file_name="detail_annuel.csv", mime="text/csv",
                            use_container_width=True)
