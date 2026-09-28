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

st.set_page_config(page_title="Analyse énergétique", layout="wide", page_icon=":material/bolt:")

# Un dossier de travail par session (et non a chaque rerun : mkdtemp au niveau
# module en creait un nouveau a chaque clic, jamais supprime).
if "workdir" not in st.session_state:
    st.session_state["workdir"] = tempfile.mkdtemp(prefix="energy_app_")
WORKDIR = st.session_state["workdir"]

# ---------------------------------------------------------------------
# Profil PV -- fige, extrait une fois pour toutes depuis l'Excel via
# extract_pv_from_excel.py (plus d'appel PVGIS, plus de latitude/longitude
# a saisir a chaque fois).
# ---------------------------------------------------------------------
PV_PROFILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pv_profile_1kwc.pkl")


@st.cache_data
def _load_pv_profile_1kwc(path: str):
    # Cache Streamlit : evite de relire le pickle a chaque rerun (le fichier ne
    # change jamais en cours de session), cle sur le chemin passe en argument.
    return pd.read_pickle(path)

# ---------------------------------------------------------------------
# Theme clair / sombre
# ---------------------------------------------------------------------
# Streamlit n'expose pas d'API publique pour changer de theme depuis le
# script : on modifie la config de theme (lue a chaque run pour construire
# le message envoye au navigateur) puis on relance le script. Cette config
# est globale au serveur -- adapte a un usage local mono-utilisateur ; deux
# onglets avec des choix differents se realignent chacun a leur prochain clic.
# Le choix est garde dans l'URL (?theme=dark) pour survivre a un rechargement.
from streamlit import config as _st_config

THEMES = {
    "light": {
        "streamlit": {
            "theme.base": "light",
            "theme.primaryColor": "#1B2A4A",
            "theme.backgroundColor": "#FFFFFF",
            "theme.secondaryBackgroundColor": "#F4F6F9",
            "theme.textColor": "#1F2937",
            "theme.borderColor": "#E3E7EE",
            "theme.linkColor": "#1B2A4A",
            "theme.sidebar.backgroundColor": "#F7F8FA",
            "theme.sidebar.secondaryBackgroundColor": "#FFFFFF",
        },
        "ink": "#111827", "muted": "#6B7280", "body": "#374151", "line": "#E3E7EE",
        "card": "#F7F8FA", "chip": "#F4F6F9", "grid": "#EEF0F4", "axis_text": "#4B5563",
        "eyebrow": "#B7791F", "brand": "#1B2A4A", "navy": "#1B2A4A", "navy_soft": "#9AA9C2",
        "teal_soft": "#A9D6CF", "zero": "#9CA3AF", "surface": "#FFFFFF", "winter": "#3D5A80",
    },
    "dark": {
        "streamlit": {
            "theme.base": "dark",
            "theme.primaryColor": "#5B7FD6",
            "theme.backgroundColor": "#0E131C",
            "theme.secondaryBackgroundColor": "#19202C",
            "theme.textColor": "#E5E7EB",
            "theme.borderColor": "#2A3341",
            "theme.linkColor": "#8FA9E8",
            "theme.sidebar.backgroundColor": "#121824",
            "theme.sidebar.secondaryBackgroundColor": "#19202C",
        },
        "ink": "#F3F4F6", "muted": "#9CA3AF", "body": "#D1D5DB", "line": "#2A3341",
        "card": "#151B26", "chip": "#19202C", "grid": "#232B38", "axis_text": "#9CA3AF",
        "eyebrow": "#E3A33B", "brand": "#2B3D66", "navy": "#8FA9E8", "navy_soft": "#4A5D85",
        "teal_soft": "#2F6B63", "zero": "#6B7280", "surface": "#0E131C", "winter": "#6C8CC0",
    },
}

_theme_qp = st.query_params.get("theme")
if "theme_mode" not in st.session_state:
    st.session_state["theme_mode"] = _theme_qp if _theme_qp in THEMES else "light"
theme_mode = st.session_state["theme_mode"]


def _toggle_theme() -> None:
    """Bascule clair/sombre : callback du bouton de theme (on_click), execute avant le
    rerun declenche par Streamlit suite au clic. Met a jour le session_state ET
    l'URL (query_params) pour que le choix survive a un rechargement de page."""
    new_mode = "dark" if st.session_state["theme_mode"] == "light" else "light"
    st.session_state["theme_mode"] = new_mode
    st.query_params["theme"] = new_mode


def _apply_streamlit_theme(mode: str) -> bool:
    """Applique les options de theme Streamlit correspondant a `mode` a la config
    globale du serveur, et indique si un changement a effectivement eu lieu (pour
    savoir si un st.rerun est necessaire -- voir l'appel juste en dessous)."""
    changed = False
    for key, value in THEMES[mode]["streamlit"].items():
        if _st_config.get_option(key) != value:
            _st_config.set_option(key, value)
            changed = True
    return changed


if _apply_streamlit_theme(theme_mode):
    # Le theme du run courant est deja parti vers le navigateur : on relance
    # pour que le nouveau soit pris en compte.
    st.rerun()

PAL = THEMES[theme_mode]

# ---------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------
INK = PAL["ink"]
MUTED = PAL["muted"]
LINE = PAL["line"]
NAVY = PAL["navy"]
SOLAR = "#E3A33B"
TEAL = "#2A9D8F"
CHART_FONT = "Inter, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"

st.markdown(f"""
<style>
[data-testid="stMainBlockContainer"] {{ max-width: 1240px; padding-top: 2.5rem; padding-bottom: 5rem; }}
[data-testid="stSidebarContent"] {{ padding-top: .5rem; }}
/* Pas d'estompage des elements pendant un rerun : evite le clignotement a chaque clic. */
[data-stale="true"] {{ opacity: 1 !important; transition: none !important; }}

.app-eyebrow {{ font-size: .72rem; font-weight: 600; letter-spacing: .09em; text-transform: uppercase; color: {PAL["eyebrow"]}; }}
.app-title {{ font-size: 2rem; font-weight: 650; letter-spacing: -.025em; color: {INK}; line-height: 1.2; margin: .15rem 0 .35rem; }}
.app-sub {{ font-size: .95rem; color: {MUTED}; max-width: 46rem; }}

.sec {{ display: flex; gap: .9rem; align-items: baseline; margin: 2.6rem 0 1rem; padding-bottom: .7rem; border-bottom: 1px solid {LINE}; }}
.sec-num {{ font-size: .8rem; font-weight: 600; color: {PAL["eyebrow"]}; font-variant-numeric: tabular-nums; }}
.sec-title {{ font-size: 1.2rem; font-weight: 600; color: {INK}; letter-spacing: -.01em; }}
.sec-desc {{ font-size: .86rem; color: {MUTED}; margin-top: .15rem; }}

.grp {{ font-size: .7rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: {MUTED}; margin: .9rem 0 .45rem; }}
.note {{ display: flex; align-items: baseline; gap: .5rem; font-size: .85rem; color: {PAL["body"]}; margin: .35rem 0 .5rem; }}
.note::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: {TEAL}; flex: none; transform: translateY(-1px); }}
.note.warn::before {{ background: {SOLAR}; }}
.note b {{ font-weight: 600; color: {INK}; }}

[data-testid="stMetric"] {{ background: {PAL["card"]}; border: 1px solid {LINE}; border-radius: 10px; padding: .8rem 1rem; }}
[data-testid="stMetricLabel"] p {{ font-size: .78rem; font-weight: 500; color: {MUTED}; }}
[data-testid="stMetricValue"] {{ font-size: 1.35rem; font-weight: 600; color: {INK}; font-variant-numeric: tabular-nums; }}

.stTabs [data-baseweb="tab-list"] {{ gap: 1.6rem; border-bottom: 1px solid {LINE}; }}
.stTabs [data-baseweb="tab"] {{ padding: .7rem 0; }}
.stTabs [data-baseweb="tab"] p {{ font-size: .9rem; font-weight: 500; }}

.chart-title {{ font-size: .95rem; font-weight: 600; color: {INK}; margin: 1.2rem 0 0; }}
.chart-sub {{ font-size: .8rem; color: {MUTED}; margin: .1rem 0 .2rem; }}

.kv {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
.kv td {{ padding: .55rem 0; border-bottom: 1px solid {LINE}; color: {PAL["body"]}; }}
.kv td:last-child {{ text-align: right; font-weight: 600; color: {INK}; font-variant-numeric: tabular-nums; }}
.kv tr.strong td {{ font-weight: 600; color: {INK}; }}

.ctx {{ display: flex; flex-wrap: wrap; gap: .4rem; margin: -.2rem 0 1.2rem; }}
.chip {{ font-size: .78rem; padding: .2rem .65rem; border-radius: 999px; background: {PAL["chip"]}; border: 1px solid {LINE}; color: {PAL["body"]}; }}

.brand {{ display: flex; align-items: center; gap: .65rem; margin: .2rem 0 1.4rem; }}
.brand-mark {{ width: 30px; height: 30px; border-radius: 8px; background: {PAL["brand"]}; position: relative; flex: none; }}
.brand-mark::after {{ content: ""; position: absolute; left: 9px; top: 9px; width: 12px; height: 12px; border-radius: 50%; background: {SOLAR}; }}
.brand-name {{ font-weight: 600; font-size: .95rem; color: {INK}; line-height: 1.15; }}
.brand-tag {{ font-size: .75rem; color: {MUTED}; }}
</style>
""", unsafe_allow_html=True)


def _section(num: int, title: str, desc: str = "") -> None:
    """Affiche l'en-tete numerote d'une section principale de la page (ex: "01 Donnees du site")."""
    desc_html = f'<div class="sec-desc">{desc}</div>' if desc else ""
    st.markdown(
        f'<div class="sec"><span class="sec-num">{num:02d}</span>'
        f'<div><div class="sec-title">{title}</div>{desc_html}</div></div>',
        unsafe_allow_html=True)


def _group(label: str, container=st) -> None:
    """Affiche un intitule de sous-groupe (petites majuscules) au sein d'une section."""
    container.markdown(f'<div class="grp">{label}</div>', unsafe_allow_html=True)


def _note(text: str, warn: bool = False, container=st) -> None:
    """Affiche une remarque discrete avec puce de couleur (teal = info, orange = avertissement)."""
    container.markdown(f'<div class="note{" warn" if warn else ""}"><span>{text}</span></div>', unsafe_allow_html=True)


def _chart_title(title: str, sub: str = "") -> None:
    """Affiche le titre (et sous-titre optionnel) d'un graphique, en dehors du canevas Plotly."""
    sub_html = f'<div class="chart-sub">{sub}</div>' if sub else ""
    st.markdown(f'<div class="chart-title">{title}</div>{sub_html}', unsafe_allow_html=True)


def _kv_table(rows) -> None:
    """Affiche un tableau cle/valeur HTML simple. `rows` est une liste de tuples
    (cle, valeur, gras) ; `gras=True` met la ligne en evidence (ex: un total)."""
    html = "".join(
        f'<tr class="{"strong" if strong else ""}"><td>{k}</td><td>{v}</td></tr>'
        for k, v, strong in rows)
    st.markdown(f'<table class="kv">{html}</table>', unsafe_allow_html=True)


def _style_fig(fig, height: int, legend: bool = True):
    """Applique le style commun (police, couleurs du theme actif, fond transparent,
    grille, legende horizontale) a une figure Plotly, pour une apparence homogene
    entre tous les graphiques et coherente avec le theme clair/sombre courant."""
    fig.update_layout(
        height=height,
        font=dict(family=CHART_FONT, size=12, color=PAL["axis_text"]),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=8, r=8, t=36 if legend else 12, b=8),
        hoverlabel=dict(font_family=CHART_FONT, bgcolor=PAL["surface"], bordercolor=LINE),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title_text=""),
    )
    fig.update_xaxes(showgrid=False, linecolor=LINE, ticks="")
    fig.update_yaxes(gridcolor=PAL["grid"], zeroline=False, linecolor=LINE)
    return fig


PLOTLY_CONFIG = {"displayModeBar": False}

if "results" not in st.session_state:
    st.session_state["results"] = None

# ---------------------------------------------------------------------
# Barre laterale : projet, mode, lancement
# ---------------------------------------------------------------------
MODE_LABELS = {
    "fournisseur_principal": "Fournisseur principal",
    "fournisseur_secondaire": "Fournisseur secondaire",
    "vente_directe": "Vente directe",
}
MODE_DESC = {
    "fournisseur_principal": "Vente d'électricité avec trading Belpex Day-Ahead.",
    "fournisseur_secondaire": "Vente au client de l'électricité produite, à prix fixe.",
    "vente_directe": "Installation vendue au client, qui paie le CAPEX.",
}

with st.sidebar:
    st.markdown(
        '<div class="brand"><div class="brand-mark"></div><div>'
        '<div class="brand-name">Analyse énergétique</div>'
        '<div class="brand-tag">PV · Stockage · Rentabilité</div></div></div>',
        unsafe_allow_html=True)
    st.button(
        "Thème clair" if theme_mode == "dark" else "Thème sombre",
        icon=":material/light_mode:" if theme_mode == "dark" else ":material/dark_mode:",
        on_click=_toggle_theme, type="tertiary", key="theme_toggle")
    client_name = st.text_input("Client / projet", value="", placeholder="Nom utilisé dans le rapport PDF")
    mode = st.radio(
        "Mode de vente",
        options=list(MODE_LABELS.keys()),
        format_func=lambda m: MODE_LABELS[m],
    )
    st.caption(MODE_DESC[mode])
    st.divider()
    run_slot = st.empty()
    run_hint = st.empty()

st.session_state["mode"] = mode
# Seul le mode "fournisseur_principal" fait du trading Belpex Day-Ahead ; les
# deux autres modes pilotent la batterie sur le tarif reseau du client.
use_belpex = (mode == "fournisseur_principal")


def _stop_missing(message: str) -> None:
    # `run_slot`/`run_hint` sont des st.empty() crees dans la sidebar : on les
    # remplit ici avec un bouton desactive + message, puis st.stop() interrompt
    # le script sans erreur (empeche d'atteindre les sections suivantes tant que
    # les donnees de base -- conso et PV -- ne sont pas disponibles).
    st.info(message)
    run_slot.button("Lancer la simulation", type="primary", width="stretch",
                    disabled=True, key="run_disabled")
    run_hint.caption("Complétez les données du site pour lancer le calcul.")
    st.stop()


# ---------------------------------------------------------------------
# En-tete
# ---------------------------------------------------------------------
st.markdown(
    '<div class="app-eyebrow">Simulation PV &amp; batterie</div>'
    '<div class="app-title">Analyse énergétique</div>'
    '<div class="app-sub">Renseignez le site et les hypothèses, lancez la simulation, '
    'puis exportez le rapport client.</div>',
    unsafe_allow_html=True)

# ---------------------------------------------------------------------
# 1. Donnees du site
# ---------------------------------------------------------------------
_section(1, "Données du site", "Consommation du client, production solaire et prix de marché.")

col1, col2 = st.columns(2, gap="large")
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
    # Cache : le fichier de profils-types ne change pas en cours de session,
    # inutile de le relire a chaque rerun.
    return pd.read_pickle(path)


@st.cache_data(show_spinner=False)
def _scaled_conso_profile(profile_key: str, conso_annuelle_kwh: float):
    # Cache cle sur (profil choisi, consommation annuelle cible) : evite de
    # refaire la mise a l'echelle a chaque rerun si ni l'un ni l'autre n'a
    # change. show_spinner=False car l'operation est rapide (pas de retour
    # visuel necessaire).
    from io_excel.dataextraction import conso_from_normalized_profile
    return conso_from_normalized_profile(
        _load_conso_profiles(CONSO_PROFILES_PATH)[profile_key], conso_annuelle_kwh)


with col1.container(border=True):
    # Deux sources mutuellement exclusives pour la consommation : soit un CSV
    # importe par l'utilisateur, soit un profil-type integre (forme normalisee
    # mise a l'echelle plus bas). Les variables conso_file/conso_series_profile
    # sont utilisees plus loin pour savoir laquelle a ete fournie.
    _group("Consommation client")
    conso_source = st.radio(
        "Source",
        options=["upload", "profil_integre"],
        format_func=lambda x: "Fichier CSV" if x == "upload" else "Profil-type intégré",
        horizontal=True,
        key="conso_source",
    )

    conso_file = None
    conso_unit = "kW"
    conso_series_profile = None

    if conso_source == "upload":
        conso_file = st.file_uploader(
            "Relevé de consommation (CSV)",
            type=["csv"],
            help="2 colonnes : horodatage + valeur. Pas de temps quelconque "
                 "(15/30/60 min), détecté automatiquement.")
        conso_unit = st.radio("Unité de la colonne valeur", options=["kW", "kWh"], horizontal=True)
    else:
        if os.path.exists(CONSO_PROFILES_PATH):
            conso_profiles = _load_conso_profiles(CONSO_PROFILES_PATH)
            _profile_keys = list(conso_profiles.keys())
            profile_key = st.selectbox(
                "Profil-type",
                options=_profile_keys,
                format_func=lambda k: CONSO_PROFILE_LABELS.get(k, k),
            )
            _note("Le profil-type ne donne que la <i>forme</i> (répartition dans le temps) -- "
                  "indiquez ci-dessous la consommation réelle du client sur un an pour le mettre "
                  "à l'échelle.")
            conso_annuelle_kwh = st.number_input(
                "Consommation annuelle du client (kWh/an)",
                value=100000.0, step=1000.0, min_value=0.0,
                help="Consommation réelle du site sur 12 mois (voir facture ou compteur). "
                     "Le profil-type sert uniquement à répartir cette valeur heure par heure "
                     "sur l'année.",
            )
            conso_series_profile = _scaled_conso_profile(profile_key, conso_annuelle_kwh)
            _note(f"Profil quart-horaire chargé, mis à l'échelle sur "
                  f"<b>{fmt_number(conso_annuelle_kwh, suffix='kWh/an')}</b>")
        else:
            st.error(
                "Aucun profil de consommation intégré trouvé (`data/conso_profiles.pkl`). "
                "Lancez `python io_sources/extract_conso_profiles_from_excel.py <fichier.xlsm>` "
                "une fois, ou utilisez l'import CSV."
            )

with col2.container(border=True):
    # Deux sources pour le profil PV pour 1 kWc : soit le profil fige extrait
    # une fois pour toutes depuis l'Excel (rapide, pas d'appel reseau), soit un
    # appel a l'API PVGIS en direct (plus flexible sur la position/orientation
    # mais plus lent et dependant du reseau).
    _group("Production PV")
    pv_source = st.radio(
        "Source",
        options=["excel", "pvgis"],
        format_func=lambda x: "Profil figé (Excel)" if x == "excel" else "PVGIS (en ligne)",
        horizontal=True,
        key="pv_source",
        help="Profil figé : `data/pv_profile_1kwc.pkl`, extrait une fois via "
             "`python extract_pv_from_excel.py <fichier.xlsm>` (redémarrer l'appli ensuite). "
             "PVGIS : API de la Commission européenne, selon la position et l'orientation saisies.",
    )

    pv_profile_1kwc = None

    if pv_source == "excel":
        if os.path.exists(PV_PROFILE_PATH):
            pv_profile_1kwc = _load_pv_profile_1kwc(PV_PROFILE_PATH)
            _note(f"Productible : <b>{fmt_number(pv_profile_1kwc.sum(), suffix='kWh/kWc/an')}</b>")
        else:
            st.error(
                "Aucun profil PV figé trouvé (`data/pv_profile_1kwc.pkl`). "
                "Lancez `python extract_pv_from_excel.py <fichier.xlsm>` une fois, "
                "ou choisissez la source PVGIS."
            )
    else:
        cc1, cc2 = st.columns(2)
        pv_lat = cc1.number_input("Latitude", value=50.85, format="%.4f")
        pv_lon = cc2.number_input("Longitude", value=4.35, format="%.4f")
        cc3, cc4, cc5 = st.columns(3)
        pv_tilt = cc3.number_input("Inclinaison (°)", value=35.0, step=1.0)
        pv_azimuth = cc4.number_input("Orientation (°)", value=180.0, step=1.0,
                                      help="180 = Sud, 90 = Est, 270 = Ouest.")
        pv_loss = cc5.number_input("Pertes système (%)", value=14.0, step=1.0)

        @st.cache_data
        def _fetch_pv_pvgis(lat, lon, tilt, azimuth, loss):
            # Cache cle sur les 5 parametres : evite de refaire l'appel reseau
            # PVGIS a chaque rerun tant qu'aucun d'eux n'a change.
            from io_sources.pv_pvgis import fetch_pv_profile_1kwc
            return fetch_pv_profile_1kwc(lat, lon, tilt=tilt, azimuth=azimuth, system_loss_pct=loss)

        try:
            with st.spinner("Récupération du profil PV via PVGIS..."):
                pv_profile_1kwc = _fetch_pv_pvgis(pv_lat, pv_lon, pv_tilt, pv_azimuth, pv_loss)
            _note(f"Productible PVGIS : <b>{fmt_number(pv_profile_1kwc.sum(), suffix='kWh/kWc/an')}</b>")
        except Exception as e:
            st.error(f"Erreur lors de l'appel PVGIS : {e}")

BELPEX_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
belpex_source = "demo"
belpex_upload_file = None

if use_belpex:
    with st.container(border=True):
        # Ce bloc ne fait que CHOISIR la source de prix (auto / annee forcee /
        # upload / demo) ; la resolution effective du fichier de prix (lecture,
        # troncature eventuelle) se fait plus bas, dans le bloc "Calcul", une
        # fois les vraies dates de consommation du client connues.
        _group("Prix de marché · Belpex Day-Ahead")
        _belpex_annees_dispo = sorted([
            f.split("_")[1] for f in os.listdir(BELPEX_DATA_DIR)
            if f.startswith("belpex_") and f.endswith("_qh.pkl")
        ]) if os.path.isdir(BELPEX_DATA_DIR) else []

        belpex_options = (["auto"] if _belpex_annees_dispo else []) + _belpex_annees_dispo + ["upload", "demo"]
        bc1, bc2 = st.columns(2, gap="large")
        belpex_source = bc1.selectbox(
            "Source des prix",
            options=belpex_options,
            format_func=lambda x: (
                "Automatique (selon les dates de consommation)" if x == "auto" else
                f"Année {x} (intégrée, forcée)" if x not in ("upload", "demo") else
                "Fichier CSV personnel" if x == "upload" else
                "Démo (prix simulés, à éviter pour un vrai client)"
            ),
        )
        belpex_date_fin = None
        if belpex_source == "auto":
            bc2.caption(
                "Prix calés jour par jour (mois/jour/heure) sur les dates de la consommation. "
                "L'année intégrée correspondante est utilisée si elle existe, sinon la plus récente."
            )
        elif belpex_source not in ("upload", "demo"):
            bc2.caption(
                f"Année {belpex_source} forcée, calée jour par jour sur les dates de la "
                f"consommation quelle que soit leur année réelle."
            )
            _limiter_periode = bc2.checkbox(
                f"Limiter à une tranche du 1er janvier {belpex_source} à une date de fin",
                value=False,
                help="Si le reste de l'année source n'est pas jugé pertinent ou fiable (ex: données "
                     "incomplètes après une certaine date), le calage jour/mois/heure n'utilisera "
                     "que les prix Belpex de cette tranche.",
            )
            if _limiter_periode:
                belpex_date_fin = bc2.date_input(
                    "Date de fin de la tranche",
                    value=datetime.date(int(belpex_source), 12, 31),
                    min_value=datetime.date(int(belpex_source), 1, 1),
                    max_value=datetime.date(int(belpex_source), 12, 31),
                )
        elif belpex_source == "demo":
            _note("Prix simulés : résultats indicatifs uniquement.", warn=True, container=bc2)

        if belpex_source == "upload":
            belpex_upload_file = bc2.file_uploader(
                "Prix Day-Ahead (CSV)",
                type=["csv"],
                help="2 colonnes : horodatage + prix EUR/kWh. Format CSV (pas .pkl) : un fichier "
                     ".pkl exécuterait du code Python arbitraire à la lecture s'il provient d'une "
                     "source non fiable -- le CSV n'a pas ce risque.")

        if not _belpex_annees_dispo:
            st.caption(
                "Aucune année Belpex intégrée pour l'instant -- lancez `python fetch_belpex.py "
                "<annee> <cle_api>` une fois pour en ajouter."
            )
else:
    st.caption(
        "Pas de prix Belpex dans ce mode : le pilotage de la batterie s'appuie sur le "
        "tarif réseau du client (fixe ou HP/HC) défini plus bas."
    )

if conso_file is None and conso_series_profile is None:
    _stop_missing("Importez un relevé de consommation ou choisissez un profil-type pour continuer.")

if pv_profile_1kwc is None:
    _stop_missing("Choisissez une source de profil PV valide (Excel figé ou PVGIS) pour continuer.")

# ---------------------------------------------------------------------
# 2. Installation
# ---------------------------------------------------------------------
_section(2, "Installation", "Dimensionnement PV, onduleur, batterie et raccordement.")

PV_CATALOGUE = [
    {"kwc": 15.0, "kva": 10.0, "prix_kwc": 664.0},
    {"kwc": 30.0, "kva": 20.0, "prix_kwc": 774.0},
    {"kwc": 45.0, "kva": 30.0, "prix_kwc": 668.0},
    {"kwc": 75.0, "kva": 50.0, "prix_kwc": 664.0},
]

pv_input_mode = st.radio(
    "Puissance PV",
    options=["manuel", "catalogue"],
    format_func=lambda x: "Saisie manuelle" if x == "manuel" else "Catalogue",
    horizontal=True,
    help="Saisie manuelle : l'onduleur est dimensionné selon la règle kVA = kWc / 1.5.",
)

c1, c2, c3, c4 = st.columns(4)

if pv_input_mode == "catalogue":
    _catalogue_labels = [
        f"{item['kwc']:.0f} kWc · {item['kva']:.0f} kVA · {item['prix_kwc']:.0f} €/kWc"
        for item in PV_CATALOGUE
    ]
    _choice_idx = c1.selectbox(
        "Modèle", options=list(range(len(PV_CATALOGUE))),
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
    + (" (catalogue)" if pv_input_mode == "catalogue" else " (kWc / 1.5)"),
    help="L'écrêtement de production suppose un facteur de puissance (cos φ) = 1 (kVA ≈ kW).",
)

battery_power_kw = c2.number_input("Puissance batterie (kW)", value=0.0, step=1.0)
battery_capacity_kwh = c3.number_input("Capacité batterie (kWh)", value=0.0, step=1.0)
contrat_kw = c4.number_input("Puissance souscrite (kW)", value=150.0, step=1.0)
has_battery = (battery_power_kw > 0) and (battery_capacity_kwh > 0)


@st.cache_data(show_spinner=False)
def _build_preview_timeseries_csv(conso_bytes: bytes, pv_profile: pd.Series, kwc_val: float, unit_val: str):
    # Prend les bytes bruts du fichier (et non l'UploadedFile lui-meme, qui
    # n'est pas hashable de maniere stable) pour que le cache Streamlit puisse
    # cler dessus correctement. Reconstruit un buffer memoire a chaque appel.
    import io
    buf = io.BytesIO(conso_bytes)
    return build_timeseries_from_sources(buf, pv_profile, kwc=kwc_val, unit=unit_val)


@st.cache_data(show_spinner=False)
def _build_preview_timeseries_profile(conso_series: pd.Series, pv_profile: pd.Series, kwc_val: float):
    # Cache pour l'apercu instantane (recalcule a chaque changement de widget
    # dans "Installation") : evite de refaire l'alignement PV/conso si les
    # entrees n'ont pas change depuis le dernier rerun.
    return build_timeseries_from_sources(conso_series, pv_profile, kwc=kwc_val)


@st.cache_data(show_spinner=False)
def _simulate_battery_selfconso(pv_kwh: np.ndarray, conso_kwh: np.ndarray,
                                 battery_power_kw: float, battery_capacity_kwh: float):
    """Cache Streamlit autour de engine.preview_dispatch.simulate_battery_selfconso (voir ce module)."""
    from engine.preview_dispatch import simulate_battery_selfconso
    return simulate_battery_selfconso(pv_kwh, conso_kwh, battery_power_kw, battery_capacity_kwh)


_live_pvm = None
_live_err = None
try:
    # Cet apercu est un calcul RAPIDE et SIMPLIFIE (dispatch glouton, pas de
    # LP), recalcule a chaque changement de widget pour donner un retour
    # immediat avant de lancer la simulation complete (couteuse, plusieurs
    # minutes) via le bouton "Lancer la simulation" plus bas.
    if conso_series_profile is not None:
        _df_preview = _build_preview_timeseries_profile(conso_series_profile, pv_profile_1kwc, kwc)
    else:
        _df_preview = _build_preview_timeseries_csv(conso_file.getvalue(), pv_profile_1kwc, kwc, conso_unit)
    _pv_kwh_prev = _df_preview["pv_kwh"].values
    if kva_onduleur > 0:
        # Ecretement onduleur : la puissance instantanee est plafonnee a la
        # puissance de l'onduleur (kVA, cos phi = 1 suppose -> kVA ~= kW).
        # Facteur 0.25 car les valeurs sont en kWh par pas de 15 minutes
        # (kW * 0.25 h = kWh sur le quart d'heure).
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
    _live_err = _e

_group("Aperçu instantané" + (" · avec batterie" if has_battery else " · sans batterie"))
if _live_pvm:
    p1, p2, p3, p4, p5 = st.columns(5)
    p1.metric("PV produit (kWh/an)", fmt_number(_live_pvm["pv_total"]))
    p2.metric("Consommation (kWh/an)", fmt_number(_live_pvm["conso_total"]))
    p3.metric("Injection (kWh/an)", fmt_number(_live_pvm["export"]))
    p4.metric("Autoconsommation", f"{_live_pvm['autoconso_pct']:.1f} %")
    p5.metric("Autonomie", f"{_live_pvm['autonomie_pct']:.1f} %")
    if has_battery:
        st.caption(
            f"Batterie {battery_power_kw:.0f} kW / {battery_capacity_kwh:.0f} kWh en dispatch simplifié "
            "(autoconsommation maximale, sans arbitrage de marché) : ordre de grandeur uniquement. "
            "La simulation complète calcule le dispatch Day-Ahead optimisé et le ROI."
        )
    else:
        st.caption("Recalculé à chaque modification. Aucune batterie (puissance ou capacité à 0).")
else:
    st.caption(
        "Aperçu indisponible -- vérifiez le fichier de consommation"
        + (f" ({_live_err})." if _live_err else ".")
    )

# ---------------------------------------------------------------------
# 3. Tarifs et offre
# ---------------------------------------------------------------------
_section(3, "Tarifs & offre",
         "Tarif réseau actuel du client (référence de comparaison et de pilotage batterie) et offre proposée.")

with st.container(border=True):
    _group("Tarif réseau du client")
    tarif_structure = st.radio(
        "Structure tarifaire", options=["fixe", "hp_hc"],
        format_func=lambda x: "Prix unique" if x == "fixe" else "Heures pleines / creuses",
        horizontal=True,
        help="Sert à calculer l'ancien coût du client (comparaison) et, en mode fournisseur "
             "secondaire / vente directe, de référence de coût réseau pour piloter la batterie "
             "(à la place du marché Belpex).",
    )
    c1, c2, c3, c4 = st.columns(4)
    if tarif_structure == "fixe":
        old_price_kwh_fixe = c1.number_input("Prix du kWh (€/kWh)", value=0.16, step=0.01, format="%.4f")
        price_hp, price_hc = 0.0, 0.0
        heure_debut_hp, heure_debut_hc = 7, 19
    else:
        price_hp = c1.number_input("Prix heures pleines (€/kWh)", value=0.19, step=0.01, format="%.4f")
        price_hc = c2.number_input("Prix heures creuses (€/kWh)", value=0.13, step=0.01, format="%.4f")
        heure_debut_hp = c3.number_input("Début heures pleines (h)", value=7, min_value=0, max_value=23, step=1)
        heure_debut_hc = c4.number_input("Début heures creuses (h)", value=19, min_value=0, max_value=23, step=1)
        old_price_kwh_fixe = 0.0
    c1, c2, _c3, _c4 = st.columns(4)
    old_cout_additionnel_contrat = c1.number_input("Frais fixes annuels (€)", value=0.0, step=10.0)
    prix_rachat_surplus_kwh = c2.number_input(
        "Rachat du surplus injecté (€/kWh)",
        value=0.03, step=0.005, format="%.4f",
        help="Utilisé hors mode fournisseur principal.")

with st.container(border=True):
    if mode == "fournisseur_principal":
        _group("Offre fournisseur · prix facturé au client")
        c1, c2, c3 = st.columns(3)
        prix_vente_kwh = c1.number_input("Prix de vente au client (€/kWh)", value=0.12, step=0.005, format="%.4f")
        marge_fournisseur = c2.number_input("Marge sur achat réseau (€/kWh)", value=0.01, step=0.005, format="%.4f")
        taxes_couts_proportionnels = c3.number_input("Taxes et coûts réseau proportionnels (€/kWh)", value=0.02, step=0.005, format="%.4f")
        c1, c2, _c3 = st.columns(3)
        marge_injection = c1.number_input("Marge sur injection du surplus (€/kWh)", value=0.00175, step=0.005, format="%.4f")
        tarif_capacitaire_fournisseur = c2.number_input(
            "Tarif capacitaire / pointe (€/kW/mois)",
            value=3.0, step=0.5,
        )

    elif mode == "fournisseur_secondaire":
        _group("Offre fournisseur · vente de l'énergie produite")
        c1, c2, _c3 = st.columns(3)
        prix_vente_kwh = c1.number_input("Prix de vente de l'énergie (€/kWh)", value=0.12, step=0.005, format="%.4f")
        tarif_capacitaire_fournisseur = c2.number_input(
            "Tarif capacitaire / pointe (€/kW/mois)",
            value=3.0, step=0.5, disabled=True,
            help="Sans effet dans ce mode : retiré du calcul du gain en fournisseur secondaire "
                 "(demande utilisateur). Conservé pour référence ; il reste actif pour les modes "
                 "fournisseur principal et vente directe."
        )
        marge_fournisseur = 0.0
        taxes_couts_proportionnels = 0.0
        marge_injection = 0.0

    else:  # vente_directe
        _group("Vente directe · le client paie le CAPEX")
        c1, c2, _c3 = st.columns(3)
        tarif_capacitaire_fournisseur = c1.number_input(
            "Tarif capacitaire / pointe (€/kW/mois)",
            value=3.0, step=0.5,
        )
        c2.caption(
            "Pas de prix de vente à fixer : le client garde son tarif réseau, et toute "
            "l'économie du PV + batterie lui revient."
        )
        prix_vente_kwh = None  # auto-calcule par le moteur (moyenne ponderee du tarif reseau)
        marge_fournisseur = 0.0
        taxes_couts_proportionnels = 0.0
        marge_injection = 0.0

# ---------------------------------------------------------------------
# 4. Investissement et hypotheses
# ---------------------------------------------------------------------
_section(4, "Investissement & hypothèses", "Coûts de l'installation et paramètres du modèle financier.")

with st.container(border=True):
    if mode == "vente_directe":
        _group("Prix de revient et marge")
        c1, c2, c3, c4 = st.columns(4)
        if pv_input_mode == "catalogue":
            prix_revient_kwc_pv = prix_kwc_pv_catalogue
            c1.metric("Prix de revient PV", f"{prix_revient_kwc_pv:.0f} €/kWc",
                      help="Prix fixé par le modèle catalogue choisi.")
        else:
            prix_revient_kwc_pv = c1.number_input("Prix de revient PV (€/kWc)", value=650.0, step=10.0)
        prix_revient_kwh_batterie = c2.number_input("Prix de revient batterie (€/kWh)", value=225.0, step=10.0)
        marge_vente_pct = c3.number_input("Marge (%)", value=30.0, step=1.0, min_value=0.0, max_value=95.0,
                                           help="Marge sur prix de vente : 30% -> prix de vente = prix de revient / (1-30%) = ×1.4286")
        maintenance_eur_an = c4.number_input("Maintenance (€/an)", value=0.0, step=50.0)
        _marge_mult = 1.0 / (1.0 - marge_vente_pct / 100.0)
        prix_kwc_pv = prix_revient_kwc_pv * _marge_mult
        prix_kwh_batterie = prix_revient_kwh_batterie * _marge_mult
        st.caption(
            f"Prix de vente PV : **{fmt_number(prix_kwc_pv)} €/kWc** · Prix de vente batterie : "
            f"**{fmt_number(prix_kwh_batterie)} €/kWh** · multiplicateur ×{_marge_mult:.4f}. "
            "Le prix de vente (= CAPEX payé par le client = gain vendeur) est calculé automatiquement."
        )
    else:
        _group("Coûts d'investissement")
        c1, c2, c3, _c4 = st.columns(4)
        if pv_input_mode == "catalogue":
            prix_kwc_pv = prix_kwc_pv_catalogue
            c1.metric("Coût PV", f"{prix_kwc_pv:.0f} €/kWc", help="Prix fixé par le modèle catalogue choisi.")
        else:
            prix_kwc_pv = c1.number_input("Coût PV (€/kWc)", value=650.0, step=10.0)
        prix_kwh_batterie = c2.number_input("Coût batterie (€/kWh)", value=225.0, step=10.0)
        maintenance_eur_an = c3.number_input("Maintenance (€/an)", value=0.0, step=50.0)

with st.expander("Hypothèses financières et modèle ROI"):
    _group("Évolution")
    c1, c2, c3 = st.columns(3)
    inflation_pct = c1.number_input("Inflation des coûts (%/an)", value=3.0, step=0.5)
    degradation_pv_pct = c2.number_input("Dégradation PV (%/an)", value=0.5, step=0.1)
    degradation_batterie_pct = c3.number_input("Dégradation batterie (%/an)", value=2.0, step=0.5)
    _group("Modèle")
    c1, c2, c3 = st.columns(3)
    horizon_annees = c1.number_input("Horizon d'analyse (années)", value=15, min_value=1, max_value=30, step=1)
    discount_rate = c2.number_input("Taux d'actualisation VAN/TRI (%)", value=6.0, step=0.5) / 100.0
    annee_remplacement_batterie = c3.number_input("Remplacement batterie (année, 0 = jamais)", value=0, min_value=0, max_value=30, step=1)

st.caption(
    f"Horizon {int(horizon_annees)} ans · actualisation {discount_rate * 100:.1f} % · "
    f"inflation {inflation_pct:.1f} %/an · dégradation PV {degradation_pv_pct:.1f} %/an"
)

_RUN_LABEL = "Lancer la simulation"
# Deux boutons identiques (sidebar + bas de page principale) pour l'ergonomie
# sur une page longue ; des `key` distinctes evitent un conflit d'identifiant
# Streamlit, et `submitted` est vrai si l'un OU l'autre a ete clique ce run-ci.
_submit_side = run_slot.button(_RUN_LABEL, type="primary", icon=":material/play_arrow:",
                               width="stretch", key="run_side")
run_hint.caption("Le dispatch Day-Ahead sur l'horizon complet peut prendre plusieurs minutes.")
st.write("")
_b1, _b2, _b3 = st.columns([1, 1, 1])
_submit_main = _b2.button(_RUN_LABEL, type="primary", icon=":material/play_arrow:",
                          width="stretch", key="run_main")
submitted = _submit_side or _submit_main

# ---------------------------------------------------------------------
# Calcul
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
            st.error("Choisissez une année intégrée ou importez un fichier CSV pour les prix Belpex.")
            st.stop()
        from io_excel.dataextraction import read_dayahead_csv
        dayahead_path = read_dayahead_csv(belpex_upload_file)
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
                f"Prix Day-Ahead : source {belpex_source} limitée à la tranche "
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
                f"Le relevé de consommation dépasse 12 mois : {n_truncated_qh} pas de temps "
                f"({n_truncated_qh * 15 / 60:.0f} h) au-delà de la première année ont été ignorés. "
                f"La simulation porte sur exactement 1 an, à partir de la première date du relevé."
            )

        if dayahead_path is None:  # belpex_source == "auto"
            annees_client = sorted(set(df.index.year.tolist()))
            annee_choisie = next((str(a) for a in annees_client if str(a) in _belpex_annees_dispo),
                                  max(_belpex_annees_dispo))
            dayahead_path = os.path.join(BELPEX_DATA_DIR, f"belpex_{annee_choisie}_qh.pkl")
            st.caption(
                f"Prix Day-Ahead : année Belpex {annee_choisie} utilisée comme référence "
                f"(calée jour par jour sur les dates réelles {annees_client[0]}-{annees_client[-1]})."
            )

        progress_bar = st.progress(0.0, text="Simulation en cours...")

        def _progress(year, total):
            # Callback transmis au moteur LP (run_fournisseur_model_from_data) :
            # appele une fois par annee de l'horizon pour faire avancer la barre
            # de progression pendant un calcul qui peut durer plusieurs minutes.
            progress_bar.progress(year / total, text=f"Simulation en cours · année {year}/{total}")

        with st.spinner("Calcul du dispatch Day-Ahead sur l'horizon complet..."):
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
        progress_bar.empty()
        # Les resultats sont stockes en session_state (et non dans une variable
        # locale) car ils doivent survivre aux reruns suivants : la section 5 est
        # affichee a partir de st.session_state["results"] en fin de script, meme
        # lorsque le rerun est declenche par un widget interne au fragment (ex:
        # changement de periode du graphique) plutot que par un nouveau calcul.
        st.session_state["results"] = results
        st.session_state["client_name"] = client_name
        # Un nouveau calcul invalide les PDF generes pour le precedent (sinon le
        # bouton de telechargement continuerait de servir l'ancien rapport, qui
        # ne correspondrait plus aux parametres/resultats affiches).
        st.session_state.pop("pdf_bytes", None)
        st.session_state.pop("fiche_pdf_bytes", None)
        if results["bilan_energetique_annee1"]["pv_total_kwh"] <= 0 and params["kwc"] > 0:
            st.warning("La production PV calculée est nulle alors que kWc > 0 -- vérifiez les "
                       "coordonnées et l'orientation saisies.")
        st.toast("Simulation terminée.", icon=":material/check_circle:")
    except Exception as e:
        st.error(f"Erreur pendant la simulation : {e}")
        st.session_state["results"] = None

# ---------------------------------------------------------------------
# 5. Resultats
# ---------------------------------------------------------------------
@st.fragment
def _render_results(results):
    """Affiche la section 5 (resultats de la simulation deja calculee).

    Isolee dans un fragment Streamlit : les widgets internes (selecteur de
    periode du profil de charge, champs de la fiche financiere, boutons
    d'export) ne relancent QUE ce fragment et non le script entier -- sans
    cela, changer par exemple la date du profil de charge relancerait aussi
    tout le reste de la page (sections 1 a 4, et surtout re-executerait la
    simulation LP couteuse si `submitted` restait vrai). `results` est passe
    en argument mais provient toujours de st.session_state["results"], seule
    source de verite pour la persistance entre reruns.
    """
    mode_label = st.session_state.get("mode", "fournisseur_principal")
    _rp = results["params"]
    df = results["detail_annuel"]

    _section(5, "Résultats")
    _chips = [
        MODE_LABELS.get(mode_label, mode_label),
        f"{_rp['kwc']:.0f} kWc",
        (f"Batterie {_rp.get('battery_power_kw', 0):.0f} kW / {_rp.get('battery_capacity_kwh', 0):.0f} kWh"
         if (_rp.get("battery_power_kw") or 0) > 0 and (_rp.get("battery_capacity_kwh") or 0) > 0
         else "Sans batterie"),
        f"Horizon {len(df)} ans",
    ]
    if st.session_state.get("client_name"):
        _chips.insert(0, st.session_state["client_name"])
    st.markdown('<div class="ctx">' + "".join(f'<span class="chip">{c}</span>' for c in _chips) + "</div>",
                unsafe_allow_html=True)

    if results.get("is_demo_prices"):
        st.warning("Prix Day-Ahead en mode démo (simulés) -- importez un vrai fichier de prix "
                   "Belpex avant de communiquer ces chiffres à un client.")

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
    _autoconso_indirecte = (mode_label == "fournisseur_principal" and has_battery)
    _AUTOCONSO_HELP = (
        "Estimation indirecte (déduite de l'import/export réseau), moins fiable en mode "
        "arbitrage Belpex : la batterie peut se charger sur le réseau puis revendre, ce qui "
        "réduit l'import sans rapport avec le PV et peut gonfler ce chiffre."
        if _autoconso_indirecte else None
    )

    _is_fournisseur = mode_label in ("fournisseur_principal", "fournisseur_secondaire")

    _group("Rentabilité")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("CAPEX total", fmt_eur(results['capex_total']))
    m2.metric("Temps de retour", f"{results['payback_year']:.1f} ans" if results["payback_year"] else "Non atteint")
    m3.metric("VAN", fmt_eur(results['npv_eur']),
              help="Valeur Actuelle Nette : cash-flows **actualisés** (taux d'actualisation "
                   "appliqué) sur tout l'horizon -- prend en compte que 1 EUR futur vaut moins "
                   "que 1 EUR aujourd'hui.")
    _TRI_HELP = ("Taux de Rentabilité Interne : le taux d'actualisation pour lequel la VAN "
                 "serait nulle -- **actualisé**, comparable à un taux de placement financier. "
                 "Non calculable si aucune solution n'a été trouvée dans la plage testée "
                 "(taux de -99% à +1000%) : soit les cash-flows sont toujours positifs "
                 "(rentabilité hors plage, très favorable), soit toujours négatifs "
                 "(le projet ne se rembourse jamais sur cet horizon).")
    if results["irr_pct"] == results["irr_pct"]:  # pas NaN
        _tri_txt = f"{results['irr_pct']:.1f} %"
    else:
        from engine.finance_utils import irr_failure_reason
        _tri_cashflows = [-results["capex_total"]] + df["cashflow_annuel_eur"].tolist()
        _tri_reason = irr_failure_reason(_tri_cashflows)
        _tri_txt = {
            "toujours_rentable": "Hors plage",
            "jamais_rentable": "Jamais rentable",
        }.get(_tri_reason, "N/A")
        _TRI_HELP = {
            "toujours_rentable": "Non calculable : rentabilité très élevée, hors de la plage testée. ",
            "jamais_rentable": "Non calculable : le projet ne se rembourse jamais sur l'horizon. ",
        }.get(_tri_reason, "") + _TRI_HELP
    m4.metric("TRI", _tri_txt, help=_TRI_HELP)

    _group("Gains")
    m5, m6, m7, m8, m9 = st.columns(5)
    m5.metric("Économie client · an 1", fmt_eur(df.loc[0, 'economie_client_eur']))
    m6.metric(f"Économie client · {len(df)} ans", fmt_eur(results['total_economie_client']))
    m7.metric(
        "Gain brut cumulé" if _is_fournisseur else "Gain vendeur",
        fmt_eur(results['gain_vendeur_eur']),
        help=None if _is_fournisseur else "Égal au CAPEX encaissé.")
    m8.metric("ROI net non actualisé", f"{results['roi_pct']:.1f} %",
              help="Retour sur investissement **NON actualisé** : simple somme brute des "
                   "cash-flows sur l'horizon, rapportée au CAPEX -- ne tient PAS compte du fait "
                   "qu'un gain lointain vaut moins qu'un gain immédiat. À ne pas comparer "
                   "directement au TRI (lui, actualisé) : les deux mesurent des choses différentes.")
    m9.metric(
        "Gain brut fournisseur · an 1" if _is_fournisseur else "Gain vendeur · an 1",
        fmt_eur(df.loc[0, 'gain_brut_fournisseur_eur']),
        help=None if _is_fournisseur else "CAPEX encaissé à la vente.")

    _group("Bilan énergétique · année 1")
    m10, m11, m12, m13, m14 = st.columns(5)
    m10.metric("Consommation (kWh/an)", fmt_number(conso_total))
    m11.metric("Autoconsommation PV" + (" *" if _autoconso_indirecte else ""),
               f"{autoconsommation_pct:.1f} %", help=_AUTOCONSO_HELP)
    m12.metric("Autonomie" + (" *" if _autoconso_indirecte else ""),
               f"{autonomie_pct:.1f} %", help=_AUTOCONSO_HELP)
    m13.metric("Import (kWh/an)", fmt_number(er['import_kwh']),
               help="Consommation restant à acheter au réseau après PV et batterie.")
    m14.metric("Injection (kWh/an)", fmt_number(er['export_kwh']))
    if _autoconso_indirecte:
        st.caption("\\* Estimation indirecte en mode arbitrage Belpex -- voir l'infobulle.")

    if results.get("pv_clip_onduleur_kwh", 0.0) > 0:
        _note(
            f"Écrêtement onduleur : <b>{fmt_number(results['pv_clip_onduleur_kwh'], suffix='kWh/an')}</b> "
            f"de production perdue (puissance instantanée au-delà de {kva_onduleur:.1f} kVA). "
            f"Un onduleur plus puissant ou un autre modèle catalogue réduirait cette perte.",
            warn=True)

    st.write("")
    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["Cash-flow", "Origine du gain", "Détail annuel", "Profil de charge", "Saisonnalité"])

    with tab1:
        _chart_title("Cash-flow cumulé", "CAPEX inclus")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["annee"], y=df["cashflow_cumule_eur"],
                                 mode="lines+markers", name="Cash-flow cumulé",
                                 line=dict(color=NAVY, width=2.5), marker=dict(size=6),
                                 hovertemplate="Année %{x}<br>%{y:,.0f} €<extra></extra>"))
        fig.add_hline(y=0, line_dash="dot", line_color=PAL["zero"], line_width=1)
        _style_fig(fig, 360, legend=False)
        fig.update_xaxes(title_text="Année", dtick=1)
        fig.update_yaxes(title_text="€")
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

        _chart_title("Économie annuelle du client")
        fig2 = go.Figure()
        fig2.add_trace(go.Bar(x=df["annee"], y=df["economie_client_eur"], marker_color=SOLAR,
                              hovertemplate="Année %{x}<br>%{y:,.0f} €<extra></extra>"))
        _style_fig(fig2, 300, legend=False)
        fig2.update_xaxes(title_text="Année", dtick=1)
        fig2.update_yaxes(title_text="€/an")
        st.plotly_chart(fig2, width="stretch", config=PLOTLY_CONFIG)

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
                _cout_explication = (
                    "Avant : consommation totale × ancien tarif réseau. Après : résiduel réseau "
                    "(conso − PV) × ancien tarif + production PV × nouveau prix. Le client n'achète "
                    "que la production PV au fournisseur ; pas de crédit d'export, le surplus "
                    "appartient à l'opérateur de l'installation."
                )
            else:
                # vente_directe : le client possede l'asset, l'autoconsommation
                # PV ne lui coute rien -- seul l'echange net avec le reseau
                # compte (import au tarif reseau, export credite au rachat).
                cout_apres = (serie_cout["import_kwh"] * serie_cout["price_reference_eur_kwh"]
                              - serie_cout["export_kwh"] * prix_rachat)
                _cout_explication = (
                    "Avant : consommation totale × ancien tarif réseau. Après : import résiduel "
                    "(post PV + batterie) × tarif réseau, moins le surplus exporté crédité au tarif "
                    "de rachat. Le client possède l'installation : son autoconsommation ne lui coûte rien. "
                    "Si la puissance souscrite est dépassée à certains moments, le total « après PV » "
                    "ci-dessous reprend le coût réel du dispatch (pénalité de dépassement incluse), "
                    "légèrement supérieur à ce que ce recalcul mensuel simplifié suggère."
                )

            mois_labels = ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin",
                           "Juil", "Août", "Sep", "Oct", "Nov", "Déc"]
            avant_mensuel = cout_avant.groupby(serie_cout.index.month).sum().reindex(range(1, 13), fill_value=0.0)
            apres_mensuel = cout_apres.groupby(serie_cout.index.month).sum().reindex(range(1, 13), fill_value=0.0)
            total_avant = float(avant_mensuel.sum())
            total_apres = float(apres_mensuel.sum())

            # Le detail mensuel ci-dessus ne recalcule que import*tarif - export*rachat,
            # et ignore donc la penalite de depassement de puissance souscrite
            # (OVERRUN_PENALTY_MULT, voir engine/fournisseur_roi.py) que le moteur LP
            # applique reellement. Pour que "Ecart" soit coherent avec "Economie client"
            # (section resultats plus haut), on recale le total "apres PV" sur le vrai
            # cout d'energie de l'annee 1 (dont_energie_eur, overrun inclus) quand ce
            # dernier est disponible -- seule la repartition mensuelle reste approximative.
            if results.get("mode_vente") != "fournisseur_secondaire":
                _dont_energie_an1 = df.loc[0, "dont_energie_eur"] if "dont_energie_eur" in df.columns else None
                if _dont_energie_an1 is not None and not pd.isna(_dont_energie_an1):
                    _ecart_overrun = _dont_energie_an1 - total_apres
                    total_apres = float(_dont_energie_an1)
                    if abs(_ecart_overrun) > 1.0:
                        st.caption(
                            f"⚠️ Le dispatch réel inclut {fmt_eur(_ecart_overrun)} de surcoût "
                            "(dépassement de la puissance souscrite, pénalisé) non visible dans la "
                            "répartition mensuelle ci-dessous ; le total « après PV » en tient compte."
                        )

            _chart_title(f"Coût réseau du client · avant / après PV ({kwc:.0f} kWc)", "Année 1")
            k1, k2, k3 = st.columns(3)
            k1.metric("Coût annuel avant PV", fmt_eur(total_avant))
            k2.metric("Coût annuel après PV", fmt_eur(total_apres))
            k3.metric("Écart", fmt_eur(total_avant - total_apres))

            fig_mois = go.Figure()
            fig_mois.add_trace(go.Bar(x=mois_labels, y=avant_mensuel.values, name="Avant PV",
                                      marker_color=NAVY, hovertemplate="%{x}<br>%{y:,.0f} €<extra>Avant</extra>"))
            fig_mois.add_trace(go.Bar(x=mois_labels, y=apres_mensuel.values, name="Après PV",
                                      marker_color=TEAL, hovertemplate="%{x}<br>%{y:,.0f} €<extra>Après</extra>"))
            _style_fig(fig_mois, 380)
            fig_mois.update_layout(barmode="group", bargap=0.3, bargroupgap=0.1)
            fig_mois.update_yaxes(title_text="€")
            st.plotly_chart(fig_mois, width="stretch", key="cout_mensuel_avant_apres", config=PLOTLY_CONFIG)
            st.caption(_cout_explication)

    with tab2:
        decomp = results["decomposition_gains"]
        c1, c2 = st.columns([1, 1], gap="large")
        with c1:
            _chart_title("Répartition du gain", "Année 1")
            fig3 = go.Figure(data=[go.Pie(
                labels=["PV", "Batterie / Belpex"],
                values=[max(decomp["gain_pv_eur"], 0), max(decomp["gain_batterie_belpex_eur"], 0)],
                marker=dict(colors=[SOLAR, NAVY], line=dict(color=PAL["surface"], width=2)),
                hole=0.6, sort=False, textinfo="percent",
                hovertemplate="%{label}<br>%{value:,.0f} €<extra></extra>")])
            _style_fig(fig3, 320)
            st.plotly_chart(fig3, width="stretch", config=PLOTLY_CONFIG)
        with c2:
            _chart_title("Décomposition en cascade", "Sans double comptage")
            _kv_table([
                ("Coût sans installation", fmt_eur(decomp['cout_sans_rien_eur']), False),
                ("Coût avec PV", fmt_eur(decomp['cout_pv_seul_eur']), False),
                ("Coût avec PV + batterie", fmt_eur(decomp['cout_pv_batterie_eur']), False),
                ("Gain PV", fmt_eur(decomp['gain_pv_eur']), True),
                ("Gain batterie", fmt_eur(decomp['gain_batterie_belpex_eur']), True),
            ])
            if decomp["gain_pv_eur"] < 0 or decomp["gain_batterie_belpex_eur"] < 0:
                st.warning(
                    "Un gain décomposé est négatif : cas limite du dispatch (le tarif "
                    "capacitaire ou une contrainte de contrat peut rendre l'ajout de PV/batterie "
                    "ponctuellement défavorable dans cette décomposition). Le graphique plafonne "
                    "à 0 pour rester lisible -- vérifiez le gain total et les hypothèses "
                    "(contrat souscrit, tarif capacitaire)."
                )

        _chart_title("Bilan énergétique", "Année 1")
        _kv_table([
            ("PV produit", fmt_number(pv_total, suffix='kWh/an'), False),
            ("Consommation totale du client", fmt_number(conso_total, suffix='kWh/an'), False),
            ("Importé du réseau", fmt_number(er['import_kwh'], suffix='kWh/an'), False),
            ("Exporté au réseau", fmt_number(er['export_kwh'], suffix='kWh/an'), False),
            ("Autoconsommation PV (PV utilisé / PV produit)", f"{autoconsommation_pct:.1f} %", False),
            ("Autonomie (PV utilisé / consommation totale)", f"{autonomie_pct:.1f} %", False),
            ("Cycles équivalents batterie", f"{er['cycles_equivalents']:.1f}", False),
        ])

    with tab3:
        st.dataframe(df.style.format({
            "revenu_client_eur": "{:,.0f}",
            "cout_approvisionnement_eur": "{:,.0f}",
            "gain_brut_fournisseur_eur": "{:,.0f}",
            "economie_client_eur": "{:,.0f}",
            "cashflow_cumule_eur": "{:,.0f}",
        }), width="stretch", hide_index=True)

    with tab4:
        serie = results.get("serie_qh_annee1")
        if serie is None or serie.empty:
            st.info("Pas de série quart-horaire disponible pour cette simulation.")
        else:
            date_min = serie.index.min().date()
            date_max = serie.index.max().date()

            cnav1, cnav2, cnav3 = st.columns([1, 1, 1])
            vue = cnav1.selectbox("Période", ["Annee complete", "Annee", "Mois", "Semaine", "Jour"], index=0,
                                  format_func=lambda v: {"Annee complete": "Année complète",
                                                         "Annee": "Année civile"}.get(v, v))
            date_focus = cnav2.date_input(
                "À partir du", value=date_min, min_value=date_min, max_value=date_max,
                disabled=(vue == "Annee complete"), format="DD/MM/YYYY")
            resolution = cnav3.radio("Résolution", ["Quart-horaire", "Horaire"], index=0,
                                     disabled=(vue != "Jour"), horizontal=True)

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
                pas_label = "horaire (moyenne par heure)"
            else:
                serie_plot = fenetre.resample("1D").mean()
                pas_label = "journalier (moyenne par jour)"

            # Palette de couleurs
            PALETTE = dict(conso=NAVY, pv=SOLAR, import_=TEAL, soc="#8E7DBE", belpex="#C8553D")
            LARGEUR_TRAIT = 2.0

            fig_qh = go.Figure()
            fig_qh.add_trace(go.Scattergl(
                x=serie_plot.index, y=serie_plot["conso_kwh"] * 4, name="Consommation",
                mode="lines", line=dict(color=PALETTE["conso"], width=LARGEUR_TRAIT), yaxis="y1"))
            fig_qh.add_trace(go.Scattergl(
                x=serie_plot.index, y=serie_plot["pv_kwh"] * 4, name="Production PV",
                mode="lines", line=dict(color=PALETTE["pv"], width=LARGEUR_TRAIT),
                fill="tozeroy", fillcolor="rgba(227,163,59,0.15)", yaxis="y1"))
            fig_qh.add_trace(go.Scattergl(
                x=serie_plot.index, y=serie_plot["import_kwh"] * 4, name="Import réseau après pilotage",
                mode="lines", line=dict(color=PALETTE["import_"], width=LARGEUR_TRAIT), yaxis="y1"))
            fig_qh.add_trace(go.Scattergl(
                x=serie_plot.index, y=serie_plot["soc_pct"], name="État de charge batterie",
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
            _style_fig(fig_qh, 560)
            fig_qh.update_layout(
                hovermode="x unified",
                xaxis=dict(
                    title="", range=[x_start, x_end],
                    rangeslider=dict(visible=True, thickness=0.06, bgcolor=PAL["chip"]),
                    uirevision=f"{vue}|{date_focus}|{resolution}",
                ),
                yaxis=dict(title="kW", side="left"),
                yaxis2=dict(title="Batterie (%)", overlaying="y", side="right", range=[0, 100], showgrid=False),
                yaxis3=dict(title="Belpex (€/kWh)", overlaying="y", side="right",
                            position=0.97, showgrid=False, anchor="free"),
                legend=dict(uirevision="qh_legend_persistent"),
                margin=dict(t=40, b=8),
            )
            st.plotly_chart(fig_qh, width="stretch", key="qh_chart")
            st.caption(
                f"Pas {pas_label}. Cliquez sur une courbe de la légende pour l'afficher ou la "
                f"masquer ; ce choix est conservé en changeant de période."
            )

    with tab5:
        serie = results.get("serie_qh_annee1")
        if serie is None or serie.empty:
            st.info("Pas de série quart-horaire disponible pour cette simulation.")
        else:
            # Saisons astronomiques (bornes au 21 des mois) :
            # Hiver 21 dec -> 20 mars, Printemps 21 mars -> 20 juin,
            # Ete 21 juin -> 20 sept, Automne 21 sept -> 20 dec.
            SEASON_ORDER = ["Hiver", "Printemps", "Été", "Automne"]
            SEASON_COLORS = {"Hiver": PAL["winter"], "Printemps": "#6A994E", "Été": SOLAR, "Automne": "#C8553D"}
            COULEURS_HPHC_AVANT = [NAVY, PAL["navy_soft"]]
            COULEURS_HPHC_APRES = [TEAL, PAL["teal_soft"]]

            def _season_of(idx):
                m, d = idx.month, idx.day
                hiver = ((m == 12) & (d >= 21)) | np.isin(m, [1, 2]) | ((m == 3) & (d < 21))
                printemps = ((m == 3) & (d >= 21)) | np.isin(m, [4, 5]) | ((m == 6) & (d < 21))
                ete = ((m == 6) & (d >= 21)) | np.isin(m, [7, 8]) | ((m == 9) & (d < 21))
                return np.select([hiver, printemps, ete], ["Hiver", "Printemps", "Été"], default="Automne")

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
                st.markdown(f'<div class="grp" style="text-align:center">{title}</div>', unsafe_allow_html=True)
                fig = go.Figure(data=[go.Pie(
                    labels=labels, values=values,
                    marker=dict(colors=colors, line=dict(color=PAL["surface"], width=2)),
                    textinfo="percent", textfont=dict(size=11), hole=0.55, sort=False,
                    hovertemplate="%{label}<br>%{value:.1f} %<extra></extra>")])
                _style_fig(fig, 250)
                fig.update_layout(
                    margin=dict(t=8, b=8, l=8, r=8),
                    legend=dict(orientation="h", yanchor="top", y=-0.02, xanchor="center", x=0.5,
                                font=dict(size=10)),
                )
                st.plotly_chart(fig, width="stretch", key=key, config=PLOTLY_CONFIG)

            st.caption(
                "Saisons astronomiques (21 décembre / 21 mars / 21 juin / 21 septembre). "
                "Premier graphique : part de chaque saison dans le total annuel."
                + (" Graphiques HP/HC : part HP vs HC à l'intérieur de chaque saison (100 % par "
                   "saison)." if has_hphc else
                   " Tarif réseau fixe (pas de HP/HC renseigné) : pas de découpe HP/HC à afficher.")
            )

            _chart_title("Avant PV", "Consommation brute du client")
            row1 = st.columns(5) if has_hphc else st.columns(5)[:1]
            pct_avant = _pct_par_saison("conso_kwh")
            with row1[0]:
                _pie(pct_avant.index, pct_avant.values,
                     [SEASON_COLORS[s] for s in pct_avant.index],
                     "Par saison", "season_avant_global")
            if has_hphc:
                for i, saison in enumerate(SEASON_ORDER):
                    pct = _pct_hphc_saison("conso_kwh", saison)
                    with row1[i + 1]:
                        _pie(pct.index, pct.values, COULEURS_HPHC_AVANT,
                             f"HP/HC · {saison}", f"season_avant_hphc_{saison}")

            _chart_title("Après PV + batterie", "Énergie restant à importer du réseau")
            row2 = st.columns(5) if has_hphc else st.columns(5)[:1]
            pct_apres = _pct_par_saison("import_kwh")
            with row2[0]:
                _pie(pct_apres.index, pct_apres.values,
                     [SEASON_COLORS[s] for s in pct_apres.index],
                     "Par saison", "season_apres_global")
            if has_hphc:
                for i, saison in enumerate(SEASON_ORDER):
                    pct = _pct_hphc_saison("import_kwh", saison)
                    with row2[i + 1]:
                        _pie(pct.index, pct.values, COULEURS_HPHC_APRES,
                             f"HP/HC · {saison}", f"season_apres_hphc_{saison}")

    _next_section = 6
    if mode_label == "fournisseur_secondaire":
        _section(_next_section, "Fiche financière",
                 "Reproduit l'onglet Excel (financement par emprunt, loyer, maintenance, CV). "
                 "Champs pré-remplis depuis la simulation, modifiables avant génération du PDF.")
        _next_section += 1
        _fp_params = results["params"]
        _pv_prod_an1 = er.get("pv_total_kwh", 0.0) or 0.0

        with st.container(border=True):
            fc1, fc2, fc3 = st.columns(3, gap="large")
            with fc1:
                _group("Projet & CAPEX")
                fp_marge = st.number_input("Marge", value=1.3, step=0.1, key="fp_marge")
                fp_subsides_pct = st.number_input("Subsides (%)", value=20.0, step=1.0, key="fp_subsides") / 100
                fp_annees_exploit = st.number_input("Années d'exploitation FW", value=20, step=1, key="fp_annees_exploit")
                _group("Production")
                fp_kwc = st.number_input("Puissance PV (kWc)", value=float(_fp_params["kwc"]), step=1.0, key="fp_kwc")
                fp_batt_kva = st.number_input("Puissance batterie (kVA)", value=float(_fp_params.get("battery_power_kw", 0.0) or 0.0), step=1.0, key="fp_batt_kva")
                fp_prod_pv = st.number_input("Production PV estimée (kWh/an)", value=float(_pv_prod_an1), step=100.0, key="fp_prod_pv")
                fp_prod_eol = st.number_input("Production éolienne estimée (kWh/an)", value=0.0, step=100.0, key="fp_prod_eol")
                fp_valeur_cv = st.number_input("Valeur CV (€/MWh)", value=0.0, step=1.0, key="fp_valeur_cv")
            with fc2:
                _group("Prix")
                fp_indexation = st.number_input("Indexation (%/an)", value=0.0, step=0.1, key="fp_indexation") / 100
                fp_prix_vente = st.number_input("Prix de vente (€/kWh)", value=float(_fp_params.get("prix_vente_kwh", 0.16) or 0.16), step=0.001, format="%.3f", key="fp_prix_vente")
                fp_revenu_batt = st.number_input("Revenu annuel batterie (€/MVA)", value=0.0, step=1.0, key="fp_revenu_batt")
                _group("Financement & fiscalité")
                fp_apport_pct = st.number_input("Apport (%)", value=0.0, step=1.0, key="fp_apport") / 100
                fp_duree_fin = st.number_input("Durée du financement (ans)", value=6, step=1, key="fp_duree_fin")
                fp_taux_int = st.number_input("Taux d'intérêt (%)", value=2.82, step=0.01, format="%.2f", key="fp_taux_int") / 100
            with fc3:
                _group("Loyer & maintenance")
                fp_surface = st.number_input("Surface utile (m²)", value=0.0, step=10.0, key="fp_surface")
                fp_loyer_m2 = st.number_input("Loyer surface (€/m²)", value=0.0, step=0.5, key="fp_loyer_m2")
                fp_indexation_loyer = st.number_input(
                    "Indexation loyer (%)", value=0.0, step=0.1, key="fp_indexation_loyer",
                    help="Champ affiché pour fidélité avec le classeur Excel source, mais "
                         "SANS EFFET sur le calcul : le loyer est en réalité indexé sur "
                         "'Indexation maintenance/nettoyage' ci-dessous (reproduction fidèle "
                         "d'une particularité de l'Excel d'origine).") / 100
                fp_assurance_pct = st.number_input("Frais d'assurance (% du CAPEX)", value=0.0, step=0.1, key="fp_assurance") / 100
                fp_maintenance_kwc = st.number_input("Maintenance / monitoring (€/kWc)", value=7.0, step=0.5, key="fp_maintenance_kwc")
                fp_nettoyage_kwc = st.number_input("Nettoyage (€/kWc)", value=0.0, step=0.5, key="fp_nettoyage_kwc")
                fp_indexation_maint = st.number_input("Indexation maintenance/nettoyage (%)", value=0.0, step=0.1, key="fp_indexation_maint") / 100

            fs1, fs2 = st.columns([2, 1], vertical_alignment="bottom")
            fp_site_name = fs1.text_input("Nom du site", value="", placeholder="ex : Résidence Amadeus", key="fp_site_name")
            _gen_fiche = fs2.button("Générer la fiche (PDF)", icon=":material/description:",
                                    width="stretch", key="gen_fiche")

        if _gen_fiche:
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
            with st.spinner("Génération de la fiche PDF..."):
                build_fiche_pdf(_fiche, fiche_pdf_path,
                                client_name=st.session_state.get("client_name", ""),
                                site_name=fp_site_name)
            with open(fiche_pdf_path, "rb") as f:
                st.session_state["fiche_pdf_bytes"] = f.read()

        if "fiche_pdf_bytes" in st.session_state:
            st.download_button("Télécharger la fiche financière (PDF)",
                               data=st.session_state["fiche_pdf_bytes"],
                               file_name="fiche_financiere.pdf", mime="application/pdf",
                               icon=":material/download:", type="primary", width="stretch",
                               on_click="ignore")

    _section(_next_section, "Exports")
    e1, e2 = st.columns(2, gap="large")

    with e1.container(border=True):
        _group("Rapport client")
        st.caption("Synthèse PDF : hypothèses, indicateurs de rentabilité et graphiques.")
        # Meme emplacement pour les deux boutons : une fois le PDF genere, le
        # bouton de telechargement remplace celui de generation, sans rerun.
        _pdf_slot = st.empty()
        if "pdf_bytes" not in st.session_state:
            if _pdf_slot.button("Générer le rapport PDF", icon=":material/picture_as_pdf:",
                                width="stretch", key="gen_pdf"):
                pdf_path = os.path.join(WORKDIR, "rapport_energetique.pdf")
                with st.spinner("Génération du PDF..."):
                    build_pdf_report(results, pdf_path, client_name=st.session_state.get("client_name", ""))
                with open(pdf_path, "rb") as f:
                    st.session_state["pdf_bytes"] = f.read()
        if "pdf_bytes" in st.session_state:
            _pdf_slot.download_button("Télécharger le rapport (PDF)", data=st.session_state["pdf_bytes"],
                               file_name="rapport_energetique.pdf", mime="application/pdf",
                               icon=":material/download:", type="primary", width="stretch",
                               on_click="ignore")

    with e2.container(border=True):
        _group("Données")
        st.caption("Détail annuel de la simulation (cash-flows, gains, économies).")
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        st.download_button("Télécharger le détail (CSV)", data=csv_bytes,
                           file_name="detail_annuel.csv", mime="text/csv",
                           icon=":material/download:", width="stretch", on_click="ignore")


results = st.session_state.get("results")
if results:
    _render_results(results)
