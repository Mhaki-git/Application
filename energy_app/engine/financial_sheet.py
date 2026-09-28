"""
Reproduit fidelement les formules de l'onglet "Residence Amadeus" (et des
autres onglets de sites) du fichier ENERGY_MIX_ANALYSIS -- financement par
emprunt, loyer de superficie, maintenance, revente electricite/CV, cash
flows sur 25 ans, payback, rendement brut, IRR.

Les formules ont ete relues directement dans le classeur (data_only=False)
et sont reproduites TELLES QUELLES ci-dessous, y compris leurs eventuelles
bizarreries (ex: la formule CAPEX additionne "Marge" (B3, un multiplicateur
sans unite) au lieu du cout PV -- c'est ce que fait le fichier source, donc
c'est ce que ce module calcule aussi, pour rester fidele a l'original).

NB_ANNEES = 25, comme les colonnes B:Z de l'onglet source.
"""
import numpy as np
import pandas as pd

from engine.finance_utils import (
    pmt as _pmt, amortization_schedule as _amortization_schedule,
    irr_from_cashflows, irr_failure_reason,
)
from reporting.pdf_style import NAVY, ACCENT, LIGHTGREY, fmt_pct as _fmt_pct, fmt_eur as _fmt_eur, fmt_number as _fmt_number

NB_ANNEES = 25
RENDEMENT_BRUT_PCT_MAX = 1000.0  # plafond d'affichage si payback ~ 0 (retour quasi instantane)

# Coefficients "en dur" de la formule Excel d'origine, repris tels quels
# (reproduction fidele) -- documentes ici pour eviter qu'un futur mainteneur
# les modifie par erreur en les prenant pour un bug plutot qu'une hypothese
# metier volontaire du classeur source.
PV_COST_EUR_PER_KWC = 450.0        # cout materiel PV suppose, EUR/kWc (avant marge)
PV_COST_FIXED_EUR = 3500.0         # cout fixe forfaitaire (etude, raccordement...), EUR
BESS_COST_EUR_PER_KVA = 500.0      # cout materiel batterie suppose, EUR/kVA
PV_PRODUCTION_DEFAULT_KWH_PER_KWC = 900.0  # rendement PV par defaut si non precise,
                                            # kWh/kWc/an -- ordre de grandeur Belgique
PV_DEGRADATION_YEAR2_FACTOR = 0.99   # production annee 2 = annee 1 * ce facteur (-1%)
PV_DEGRADATION_YEAR3PLUS_FACTOR = 0.996  # production annee N+1 = annee N * ce facteur (-0.4%/an)

DEFAULT_PARAMS = {
    "marge": 1.3,                          # B3
    "puissance_batterie_kva": 0.0,         # G4
    "subsides_pct": 0.2,                   # B6
    "kwc": 75.0,                           # G6
    "production_pv_estimee_kwh": None,     # G7 (None -> calcule G6*900 comme le modele Excel)
    "production_eolienne_kwh": 0.0,        # G3
    "valeur_cv_eur_mwh": 0.0,              # G9
    "indexation_pct": 0.0,                 # M3
    "prix_vente_kwh": 0.16,                # M5
    "revenu_batterie_eur_mva": 0.0,        # M6
    "apport_pct": 0.0,                     # S3 (fraction du CAPEX finance en fonds propres)
    "duree_financement_annees": 6,         # S6
    "taux_interet": 0.0282,                # S7
    "annees_exploitation_fw": 20,          # B9
    "surface_utile_m2": 0.0,               # Y3
    "loyer_m2": 0.0,                       # Y4
    "indexation_loyer_pct": 0.0,           # Y5 (note : non utilisee par la formule loyer -- voir plus bas)
    "frais_assurance_pct": 0.0,            # Y6 (fraction du CAPEX)
    "frais_maintenance_eur_kwc": 7.0,      # Y7
    "nettoyage_eur_kwc": 0.0,              # Y8
    "indexation_maintenance_pct": 0.0,     # Y9
}


def compute_fiche(params: dict) -> dict:
    """
    Calcule l'integralite de la fiche (bloc parametres + tableau 25 ans +
    indicateurs de synthese), a partir d'un dict `params` (voir DEFAULT_PARAMS
    pour les cles attendues -- les cles manquantes sont completees avec les
    valeurs par defaut du modele Excel d'origine).
    """
    p = {**DEFAULT_PARAMS, **{k: v for k, v in params.items() if v is not None}}

    marge = p["marge"]
    kwc = p["kwc"]
    puissance_batterie_kva = p["puissance_batterie_kva"]
    subsides_pct = p["subsides_pct"]
    annees_exploitation_fw = int(p["annees_exploitation_fw"])

    # --- Bloc "Details projets" / CAPEX (B3:B9) ---------------------------
    pv_cost = PV_COST_EUR_PER_KWC * kwc * marge + PV_COST_FIXED_EUR   # B4
    bess_cost = BESS_COST_EUR_PER_KVA * puissance_batterie_kva        # B5
    capex = (1 - subsides_pct) * marge + pv_cost + bess_cost  # B7 (formule reproduite telle quelle)

    # --- Bloc "Donnees de production" (D3:H9) ------------------------------
    production_pv_estimee = p["production_pv_estimee_kwh"]
    if production_pv_estimee is None:
        production_pv_estimee = kwc * PV_PRODUCTION_DEFAULT_KWH_PER_KWC  # G7 par defaut (formule Excel d'origine)
    production_eolienne = p["production_eolienne_kwh"]      # G3
    production_totale_estimee = production_pv_estimee + production_eolienne  # G8

    # --- Bloc "Donnees financieres & fiscales" (P3:S9) ---------------------
    apport_pct = p["apport_pct"]
    apport_eur = apport_pct * capex                          # S4
    montant_financement = capex - apport_eur                 # S5
    duree_financement = int(p["duree_financement_annees"])   # S6
    taux_interet = p["taux_interet"]                          # S7
    annuite, interets_periode, capital_periode = _amortization_schedule(
        taux_interet, duree_financement, montant_financement)
    total_interets = annuite * duree_financement - montant_financement  # S9

    # --- Tableau annuel (25 ans) --------------------------------------------
    annees = np.arange(1, NB_ANNEES + 1)

    production = np.empty(NB_ANNEES)
    production[0] = production_totale_estimee
    if NB_ANNEES > 1:
        production[1] = production[0] * PV_DEGRADATION_YEAR2_FACTOR
        for i in range(2, NB_ANNEES):
            production[i] = production[i - 1] * PV_DEGRADATION_YEAR3PLUS_FACTOR

    financement = np.zeros(NB_ANNEES)
    financement[0] = montant_financement

    facturation_elec = np.where(
        annees <= annees_exploitation_fw,
        production * p["prix_vente_kwh"] * (1 + p["indexation_pct"]) ** (annees - 1),
        0.0)

    revenu_batterie = np.zeros(NB_ANNEES)  # G5 (duree/exploitation batterie) est vide dans le
    # classeur source -- la formule Excel (=M6/1000*G4*G5) vaut donc 0 quel
    # que soit M6/G4 tant que G5 n'est pas renseigne. Reproduit a l'identique.

    cv = production * p["valeur_cv_eur_mwh"] / 1000.0  # G9 en €/MWh

    facturation_maintenance = np.where(
        annees > annees_exploitation_fw,
        kwc * p["frais_maintenance_eur_kwc"] * (1 + p["indexation_maintenance_pct"]) ** (annees - 1),
        0.0)
    facturation_nettoyage = np.where(
        annees > annees_exploitation_fw,
        p["nettoyage_eur_kwc"] * kwc * (1 + p["indexation_maintenance_pct"]) ** (annees - 1),
        0.0)

    cash_flows_positifs = (financement + facturation_elec + revenu_batterie + cv
                            + facturation_maintenance + facturation_nettoyage)

    investissement = np.zeros(NB_ANNEES)
    investissement[0] = capex

    remb_capital = np.zeros(NB_ANNEES)
    remb_interets = np.zeros(NB_ANNEES)
    # Le remboursement demarre annee 2 (grace de la 1ere annee), pour
    # duree_financement annuites -- reproduit le decalage d'index de la
    # formule Excel (IPMT/PMT bases sur l'annee precedente).
    for k in range(duree_financement):
        year_idx = k + 1  # annee 2 -> k=0
        if year_idx < NB_ANNEES:
            remb_interets[year_idx] = interets_periode[k]
            remb_capital[year_idx] = capital_periode[k]

    # Loyer superficie -- NB : la formule source indexe avec Y9 (indexation
    # maintenance/nettoyage), pas Y5 (indexation loyer) -- reproduit tel quel.
    loyer_superficie = np.where(
        annees <= annees_exploitation_fw,
        p["loyer_m2"] * p["surface_utile_m2"] * (1 + p["indexation_maintenance_pct"]) ** (annees - 1),
        0.0)
    assurance = np.where(
        annees <= annees_exploitation_fw,
        p["frais_assurance_pct"] * capex * (1 + p["indexation_maintenance_pct"]) ** (annees - 1),
        0.0)
    maintenance_monitoring = np.where(
        annees <= annees_exploitation_fw,
        kwc * p["frais_maintenance_eur_kwc"] * (1 + p["indexation_maintenance_pct"]) ** (annees - 1),
        0.0)
    nettoyage = np.where(
        annees <= annees_exploitation_fw,
        p["nettoyage_eur_kwc"] * kwc * (1 + p["indexation_maintenance_pct"]) ** (annees - 1),
        0.0)

    cash_flows_negatifs = (investissement + remb_capital + remb_interets
                            + loyer_superficie + assurance + maintenance_monitoring + nettoyage)

    resultat_annuel = cash_flows_positifs - cash_flows_negatifs
    resultat_cumule = np.cumsum(resultat_annuel)

    df = pd.DataFrame({
        "annee": annees,
        "production_kwh": production,
        "financement_eur": financement,
        "facturation_electricite_eur": facturation_elec,
        "revenu_batterie_eur": revenu_batterie,
        "cv_eur": cv,
        "facturation_maintenance_eur": facturation_maintenance,
        "facturation_nettoyage_eur": facturation_nettoyage,
        "cash_flows_positifs_eur": cash_flows_positifs,
        "investissement_eur": investissement,
        "remb_capital_eur": remb_capital,
        "remb_interets_eur": remb_interets,
        "loyer_superficie_eur": loyer_superficie,
        "assurance_eur": assurance,
        "maintenance_monitoring_eur": maintenance_monitoring,
        "nettoyage_eur": nettoyage,
        "cash_flows_negatifs_eur": cash_flows_negatifs,
        "resultat_annuel_eur": resultat_annuel,
        "resultat_cumule_eur": resultat_cumule,
    })

    # --- Prix de revient / marge brute (M4, M7) -----------------------------
    opex_total = float(assurance.sum() + maintenance_monitoring.sum() + nettoyage.sum())  # B8
    denom = annees_exploitation_fw * production_totale_estimee
    prix_revient_kwh = ((duree_financement * annuite + apport_eur + opex_total) / denom
                         if denom else float("nan"))
    prix_vente_kwh = p["prix_vente_kwh"]
    marge_brute_pct = (100 * (prix_vente_kwh - prix_revient_kwh) / prix_revient_kwh
                        if prix_revient_kwh else float("nan"))

    # --- Payback / rendement brut / IRR --------------------------------------
    payback_year = None
    payback_fraction = 0.0
    for i in range(NB_ANNEES - 1):
        if resultat_cumule[i] <= 0 < resultat_cumule[i + 1]:
            payback_year = annees[i]
            payback_fraction = -resultat_cumule[i] / (-resultat_cumule[i] + resultat_cumule[i + 1])
            break
    payback = (payback_year + payback_fraction) if payback_year is not None else 0.0
    # Reproduit Excel : MIN() sur une plage sans aucun marqueur renvoie 0 (cas
    # ou le resultat cumule est deja positif des l'annee 1 -- pas de "retour
    # sur investissement" a proprement parler puisque le CAPEX est finance).
    rendement_brut_pct = min(100 / payback, RENDEMENT_BRUT_PCT_MAX) if payback else None

    irr = irr_from_cashflows(resultat_annuel, start_period=1)
    irr_failure_reason_ = None if irr == irr else irr_failure_reason(list(resultat_annuel))

    return {
        "params": p,
        "capex_pv": pv_cost, "capex_bess": bess_cost, "capex_total": capex,
        "opex_total": opex_total,
        "production_pv_estimee": production_pv_estimee,
        "production_totale_estimee": production_totale_estimee,
        "apport_eur": apport_eur, "montant_financement": montant_financement,
        "annuite": annuite, "total_interets": total_interets,
        "prix_revient_kwh": prix_revient_kwh, "marge_brute_pct": marge_brute_pct,
        "gain_total_25ans_eur": float(resultat_annuel.sum()),
        "payback_annees": payback, "rendement_brut_pct": rendement_brut_pct, "irr_pct": irr * 100 if irr == irr else None,
        "irr_failure_reason": irr_failure_reason_,
        "detail_annuel": df,
    }


def build_fiche_pdf(fiche: dict, output_path: str, client_name: str = "", site_name: str = "") -> str:
    """
    Genere un PDF reproduisant la structure de l'onglet Excel source :
    page 1 (portrait) = blocs de parametres + indicateurs de synthese,
    page 2 (paysage) = le tableau annuel complet sur 25 ans.
    """
    import datetime
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, A3, landscape
    from reportlab.lib.units import cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        BaseDocTemplate, PageTemplate, Frame, NextPageTemplate, PageBreak,
        Paragraph, Spacer, Table, TableStyle
    )
    from reportlab.lib.enums import TA_CENTER

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleCustom", parent=styles["Title"], textColor=NAVY, fontSize=20)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], textColor=NAVY, fontSize=12, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("BodyCustom", parent=styles["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=7.5, textColor=colors.grey)

    p = fiche["params"]
    df = fiche["detail_annuel"]

    def param_table(rows, col_widths):
        t = Table(rows, colWidths=col_widths)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), LIGHTGREY),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.white),
        ]))
        return t

    frame_portrait = Frame(1.5 * cm, 1.5 * cm, A4[0] - 3 * cm, A4[1] - 3 * cm, id="portrait")
    landscape_a3 = landscape(A3)
    frame_landscape = Frame(1 * cm, 1 * cm, landscape_a3[0] - 2 * cm, landscape_a3[1] - 2 * cm, id="landscape")

    doc = BaseDocTemplate(output_path, pagesize=A4,
                           topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                           leftMargin=1.5 * cm, rightMargin=1.5 * cm)
    doc.addPageTemplates([
        PageTemplate(id="Portrait", frames=[frame_portrait], pagesize=A4),
        PageTemplate(id="Landscape", frames=[frame_landscape], pagesize=landscape_a3),
    ])

    story = []
    story.append(Paragraph("Fiche financiere -- Vente d'electricite (fournisseur secondaire)", title_style))
    if site_name:
        story.append(Paragraph(f"Site : {site_name}", styles["Heading3"]))
    if client_name:
        story.append(Paragraph(f"Client : {client_name}", body))
    story.append(Paragraph(f"Date : {datetime.date.today().strftime('%d/%m/%Y')}", body))
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Details projet & CAPEX", h2))
    story.append(param_table([
        ["Marge", f"{p['marge']:.2f}", "PV", _fmt_eur(fiche['capex_pv'])],
        ["BESS", _fmt_eur(fiche['capex_bess']), "Subsides", _fmt_pct(100 * p["subsides_pct"])],
        ["CAPEX total", _fmt_eur(fiche['capex_total']), "OPEX (25 ans)", _fmt_eur(fiche['opex_total'])],
        ["Annees d'exploitation FW", f"{p['annees_exploitation_fw']:.0f} ans", "", ""],
    ], [5.5 * cm, 4 * cm, 5.5 * cm, 4 * cm]))

    story.append(Paragraph("Donnees de production", h2))
    story.append(param_table([
        ["Puissance PV", f"{p['kwc']:.1f} kWc", "Production PV estimee", _fmt_number(fiche['production_pv_estimee'], suffix="kWh/an")],
        ["Production eolienne estimee", _fmt_number(p['production_eolienne_kwh'], suffix="kWh/an"), "Production totale estimee", _fmt_number(fiche['production_totale_estimee'], suffix="kWh/an")],
        ["Valeur CV", f"{p['valeur_cv_eur_mwh']:.1f} EUR/MWh", "", ""],
    ], [5.5 * cm, 4 * cm, 5.5 * cm, 4 * cm]))

    story.append(Paragraph("Prix", h2))
    story.append(param_table([
        ["Indexation", _fmt_pct(100 * p["indexation_pct"]), "Prix de revient energie", f"{fiche['prix_revient_kwh']:.4f} EUR/kWh"],
        ["Prix de vente selectionne", f"{p['prix_vente_kwh']:.3f} EUR/kWh", "Marge brute Fairwind", _fmt_pct(fiche["marge_brute_pct"])],
        ["Revenu annuel batterie", f"{p['revenu_batterie_eur_mva']:.1f} EUR/MVA", "", ""],
    ], [5.5 * cm, 4 * cm, 5.5 * cm, 4 * cm]))

    story.append(Paragraph("Donnees financieres & fiscales", h2))
    story.append(param_table([
        ["Apport", _fmt_pct(100 * p["apport_pct"]), "Apport (montant)", _fmt_eur(fiche['apport_eur'])],
        ["Montant du financement", _fmt_eur(fiche['montant_financement']), "Duree du financement", f"{p['duree_financement_annees']:.0f} ans"],
        ["Taux d'interet", _fmt_pct(100 * p["taux_interet"]), "Annuite a rembourser", _fmt_eur(fiche['annuite'])],
        ["Total interets", _fmt_eur(fiche['total_interets']), "", ""],
    ], [5.5 * cm, 4 * cm, 5.5 * cm, 4 * cm]))

    story.append(Paragraph("Loyer + Maintenance", h2))
    story.append(param_table([
        ["Surface utile", f"{p['surface_utile_m2']:.0f} m2", "Loyer surface", f"{p['loyer_m2']:.2f} EUR/m2"],
        ["Indexation loyer", _fmt_pct(100 * p["indexation_loyer_pct"]), "Frais assurance", _fmt_pct(100 * p["frais_assurance_pct"])],
        ["Frais Maintenance/Monitoring", f"{p['frais_maintenance_eur_kwc']:.1f} EUR/kWc", "Nettoyage", f"{p['nettoyage_eur_kwc']:.1f} EUR/kWc"],
        ["Indexation Maintenance/Nettoyage", _fmt_pct(100 * p["indexation_maintenance_pct"]), "", ""],
    ], [5.5 * cm, 4 * cm, 5.5 * cm, 4 * cm]))

    story.append(Paragraph("Synthese", h2))
    payback_txt = (f"{fiche['payback_annees']:.1f} ans" if fiche["payback_annees"] else
                   "Deja positif des l'annee 1 (pas de payback -- CAPEX finance)")
    if not fiche["rendement_brut_pct"]:
        rendement_txt = "Non applicable"
    elif fiche["rendement_brut_pct"] >= RENDEMENT_BRUT_PCT_MAX:
        rendement_txt = f"> {_fmt_pct(RENDEMENT_BRUT_PCT_MAX)}"
    else:
        rendement_txt = _fmt_pct(fiche["rendement_brut_pct"])
    # irr_pct est deja None (pas NaN) a ce stade -- compute_fiche() convertit
    # le NaN eventuel de irr_from_cashflows() en None avant de le retourner.
    if fiche["irr_pct"] is not None:
        irr_txt = _fmt_pct(fiche["irr_pct"])
    else:
        irr_txt = {
            "toujours_rentable": "Non calculable (rentabilite tres elevee)",
            "jamais_rentable": "Non calculable (jamais rentable sur cet horizon)",
        }.get(fiche.get("irr_failure_reason"), "Non calculable")
    t_synth = Table([
        [f"Gain total ({NB_ANNEES} ans)", _fmt_eur(fiche['gain_total_25ans_eur'])],
        ["Payback", payback_txt],
        ["Rendement brut", rendement_txt],
        ["Taux de rendement (IRR)", irr_txt],
    ], colWidths=[9 * cm, 10 * cm])
    t_synth.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHTGREY),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("LINEBELOW", (0, -1), (-1, -1), 1, NAVY),
    ]))
    story.append(t_synth)
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph(
        "Ce document reproduit fidelement les formules du modele Excel source "
        "(financement par emprunt, loyer de superficie, maintenance, revente "
        "electricite/CV). Les eventuelles particularites du modele d'origine "
        "(ex: la formule CAPEX, ou l'indexation du loyer) sont reprises telles "
        "quelles.", small))

    # --- Page 2 (paysage) : tableau annuel complet --------------------------
    story.append(NextPageTemplate("Landscape"))
    story.append(PageBreak())
    story.append(Paragraph(f"Detail annuel complet ({NB_ANNEES} ans)", h2))

    row_defs = [
        ("Production (kWh)", "production_kwh", ",.0f"),
        ("Financement", "financement_eur", ",.0f"),
        ("Facturation electricite", "facturation_electricite_eur", ",.0f"),
        ("Revenu Batterie", "revenu_batterie_eur", ",.0f"),
        ("CV", "cv_eur", ",.0f"),
        ("Facturation maintenance", "facturation_maintenance_eur", ",.0f"),
        ("Facturation nettoyage", "facturation_nettoyage_eur", ",.0f"),
        ("Cash flows positifs", "cash_flows_positifs_eur", ",.0f"),
        ("Investissement", "investissement_eur", ",.0f"),
        ("Remb. capital", "remb_capital_eur", ",.0f"),
        ("Remb. interets", "remb_interets_eur", ",.0f"),
        ("Loyer superficie", "loyer_superficie_eur", ",.0f"),
        ("Assurance", "assurance_eur", ",.0f"),
        ("Maintenance/monitoring", "maintenance_monitoring_eur", ",.0f"),
        ("Nettoyage", "nettoyage_eur", ",.0f"),
        ("Cash flows negatifs", "cash_flows_negatifs_eur", ",.0f"),
        ("Resultat annuel", "resultat_annuel_eur", ",.0f"),
        ("Resultat cumule", "resultat_cumule_eur", ",.0f"),
    ]
    header = ["Poste"] + [f"An {int(a)}" for a in df["annee"]]
    table_rows = [header]
    for label, col, fmt in row_defs:
        decimals = int(fmt.split(".")[1].rstrip("f")) if "." in fmt else 0
        table_rows.append([label] + [_fmt_number(v, decimals=decimals) for v in df[col]])

    n_year_cols = NB_ANNEES
    label_w = 3.6 * cm
    year_w = (landscape_a3[0] - 2 * cm - label_w) / n_year_cols
    t_annuel = Table(table_rows, colWidths=[label_w] + [year_w] * n_year_cols, repeatRows=1)
    t_annuel.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 5.6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHTGREY]),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, 8), (-1, 8), 0.6, NAVY),  # separateur avant cash flows positifs
        ("LINEBELOW", (0, 16), (-1, 16), 0.6, NAVY),  # separateur avant cash flows negatifs
        ("FONTNAME", (0, 8), (-1, 8), "Helvetica-Bold"),
        ("FONTNAME", (0, 16), (-1, 16), "Helvetica-Bold"),
        ("FONTNAME", (0, -2), (-1, -2), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story.append(t_annuel)

    doc.build(story)
    return output_path
