"""
Genere un rapport PDF propre et presentable a partir du dict retourne par
fournisseur_roi.run_fournisseur_model().
"""
import io
import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
)
from reportlab.lib.enums import TA_CENTER

from reporting.pdf_style import NAVY, ACCENT, LIGHTGREY, fmt_eur as _fmt_eur, fmt_pct as _fmt_pct


def _chart_cashflow(result_df, capex_total):
    """
    Construit le graphique du cashflow cumule (CAPEX inclus) sur tout
    l'horizon d'analyse, sous forme d'image PNG en memoire.

    Entrees :
      - result_df : DataFrame avec au moins les colonnes "annee" et
        "cashflow_cumule_eur" (une ligne par annee de l'horizon).
      - capex_total : parametre non utilise dans le calcul du graphique
        (conserve pour la signature/compatibilite de l'appelant).
    Sortie : io.BytesIO positionne au debut, contenant le PNG (dpi=160),
    a inserer directement dans un flowable reportlab Image.
    """
    fig, ax = plt.subplots(figsize=(6.3, 3.2), dpi=160)
    ax.plot(result_df["annee"], result_df["cashflow_cumule_eur"], color="#1B2A4A", linewidth=2.2)
    ax.axhline(0, color="#999999", linewidth=1, linestyle="--")
    # Zone orange : cashflow cumule positif (periode rentable).
    ax.fill_between(result_df["annee"], result_df["cashflow_cumule_eur"], 0,
                     where=(result_df["cashflow_cumule_eur"] >= 0), color="#F5A623", alpha=0.25)
    # Zone bleu marine : cashflow cumule encore negatif (avant retour sur investissement).
    ax.fill_between(result_df["annee"], result_df["cashflow_cumule_eur"], 0,
                     where=(result_df["cashflow_cumule_eur"] < 0), color="#1B2A4A", alpha=0.15)
    ax.set_xlabel("Annee")
    ax.set_ylabel("Cashflow cumule (EUR)")
    ax.set_title("Cashflow cumule fournisseur (CAPEX inclus)", fontsize=10, color="#1B2A4A")
    # Formatte l'axe Y avec un separateur de milliers "espace" (convention FR), sans decimales.
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, p: f"{v:,.0f}".replace(",", " ")))
    ax.grid(alpha=0.25)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)  # libere la figure matplotlib pour eviter une fuite memoire sur generation repetee.
    buf.seek(0)
    return buf


def _chart_client_savings(result_df):
    """
    Construit le graphique en barres de l'economie annuelle realisee par le
    client (EUR/an, une barre par annee de l'horizon).

    Entree : result_df avec les colonnes "annee" et "economie_client_eur".
    Sortie : io.BytesIO contenant le PNG (dpi=160), pret pour reportlab.Image.
    """
    fig, ax = plt.subplots(figsize=(6.3, 3.0), dpi=160)
    ax.bar(result_df["annee"], result_df["economie_client_eur"], color="#1B2A4A")
    ax.set_xlabel("Annee")
    ax.set_ylabel("Economie client (EUR/an)")
    ax.set_title("Economie annuelle pour le client", fontsize=10, color="#1B2A4A")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, p: f"{v:,.0f}".replace(",", " ")))
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf


def _chart_gain_split(decomp):
    """
    Construit un camembert repartissant le gain de l'annee 1 entre la part
    attribuable au PV et celle attribuable a la batterie/arbitrage Belpex.

    Entree : decomp (dict "decomposition_gains" du resultat du moteur), doit
    contenir "gain_pv_eur" et "gain_batterie_belpex_eur" (EUR, annee 1).
    Piege : les valeurs negatives sont plafonnees a 0 pour le graphique
    (matplotlib ne sait pas dessiner un camembert avec part negative) ; le
    texte "N/A" est affiche si les deux parts valent 0 une fois plafonnees.
    Sortie : io.BytesIO contenant le PNG (dpi=160).
    """
    labels = ["PV", "Batterie / Belpex"]
    values = [max(decomp["gain_pv_eur"], 0), max(decomp["gain_batterie_belpex_eur"], 0)]
    fig, ax = plt.subplots(figsize=(4.2, 3.2), dpi=160)
    colors_pie = ["#F5A623", "#1B2A4A"]
    if sum(values) > 0:
        ax.pie(values, labels=labels, autopct="%1.0f%%", colors=colors_pie,
               textprops={"fontsize": 9})
    else:
        # Cas ou les deux gains sont nuls ou negatifs : pas de camembert exploitable.
        ax.text(0.5, 0.5, "N/A", ha="center", va="center")
    ax.set_title("Origine du gain annee 1", fontsize=10, color="#1B2A4A")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf


def build_pdf_report(results: dict, output_path: str, client_name: str = ""):
    """
    Genere le rapport PDF complet (page de garde + 4 sections + tableau
    detaille) a partir du dict `results` retourne par
    fournisseur_roi.run_fournisseur_model(), et l'ecrit sur disque a
    `output_path`.

    Entrees :
      - results : dict attendu avec (entre autres) les cles "params",
        "detail_annuel" (DataFrame annuel), "decomposition_gains",
        "capex_pv"/"capex_batterie"/"capex_total" (EUR), "old_cost_year1"/
        "revenue_client_year1" (EUR), "total_economie_client" (EUR),
        "payback_year" (annees ou None), "total_gain_brut" (EUR),
        "roi_pct"/"irr_pct" (%), "npv_eur" (EUR), "discount_rate" (fraction,
        ex 0.05 pour 5%), "mode_vente", "is_demo_prices" (bool),
        "bilan_energetique_annee1".
      - output_path : chemin du fichier .pdf a creer/ecraser.
      - client_name : optionnel, affiche sur la page de garde si fourni.
    Sortie : le chemin output_path (pour chainage), avec le fichier ecrit
    sur disque en effet de bord.
    """
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleCustom", parent=styles["Title"], textColor=NAVY, fontSize=22)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], textColor=NAVY, spaceBefore=14, spaceAfter=6)
    body = ParagraphStyle("BodyCustom", parent=styles["BodyText"], fontSize=9.5, leading=13)
    small = ParagraphStyle("Small", parent=styles["BodyText"], fontSize=8, textColor=colors.grey)
    center = ParagraphStyle("Center", parent=body, alignment=TA_CENTER)

    doc = SimpleDocTemplate(output_path, pagesize=A4,
                             topMargin=1.6 * cm, bottomMargin=1.6 * cm,
                             leftMargin=1.8 * cm, rightMargin=1.8 * cm)
    story = []

    params = results["params"]
    result_df = results["detail_annuel"]
    decomp = results["decomposition_gains"]
    horizon = len(result_df)  # nombre d'annees du detail = horizon d'analyse affiche partout dans le rapport.

    # --- Page de garde ---
    story.append(Spacer(1, 3 * cm))
    story.append(Paragraph("Analyse Energetique", title_style))
    story.append(Paragraph("Modele fournisseur : PV + Batterie", styles["Heading3"]))
    story.append(Spacer(1, 0.6 * cm))
    if client_name:
        story.append(Paragraph(f"Client : {client_name}", body))
    story.append(Paragraph(f"Date : {datetime.date.today().strftime('%d/%m/%Y')}", body))
    story.append(Paragraph(f"Horizon d'analyse : {horizon} ans", body))
    if results.get("is_demo_prices"):
        # Garde-fou visuel : si les prix Day-Ahead viennent du mode demo (donnees simulees,
        # pas un vrai fichier Belpex), un avertissement en rouge est insere pour eviter
        # qu'un rapport non fiable soit envoye tel quel a un client.
        story.append(Spacer(1, 0.4 * cm))
        story.append(Paragraph(
            "ATTENTION : ce rapport a ete genere avec des prix de marche Day-Ahead "
            "SIMULES (mode demo) -- les chiffres ne sont pas fiables pour une decision "
            "commerciale. Fournir un fichier de prix Belpex reel avant l'envoi au client.",
            ParagraphStyle("Warn", parent=body, textColor=colors.red)))
    story.append(PageBreak())

    # --- Installation & CAPEX ---
    story.append(Paragraph("Installation & investissement", h2))
    er = results.get("bilan_energetique_annee1", {})  # dict optionnel : cle "import_kwh" utilisee ci-dessous, defaut {} si absente.
    install_data = [
        ["Puissance PV installee", f"{params['kwc']:.1f} kWc"],
        ["Consommation totale apres asset (an 1)", f"{er.get('import_kwh', 0):,.0f} kWh/an".replace(",", " ")],
        ["Puissance batterie", f"{params['battery_power_kw']:.1f} kW"],
        ["Capacite batterie", f"{params['battery_capacity_kwh']:.1f} kWh"],
        ["Puissance souscrite (contrat)", f"{params['contrat_kw']:.1f} kW"],
        ["CAPEX PV", _fmt_eur(results["capex_pv"])],
        ["CAPEX Batterie", _fmt_eur(results["capex_batterie"])],
        ["CAPEX TOTAL", _fmt_eur(results["capex_total"])],
    ]
    # Table libelle/valeur en 2 colonnes ; largeurs fixes en cm pour un alignement stable sur A4.
    t = Table(install_data, colWidths=[8 * cm, 7 * cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHTGREY),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHTGREY]),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, -1), (-1, -1), 1, NAVY),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))
    story.append(t)

    # --- Cote client ---
    story.append(Paragraph("Cote client", h2))
    old_c = results["old_cost_year1"]
    new_c = results["revenue_client_year1"]
    savings_1 = old_c - new_c
    # Evite une division par zero si l'ancien cout est nul (ancien cout inconnu/0) ;
    # affiche alors "N/A" via fmt_pct qui echoue proprement sur NaN.
    pct = 100 * savings_1 / old_c if old_c else float("nan")
    client_data = [
        ["Ancien cout annuel (avant contrat)", _fmt_eur(old_c)],
        [f"Nouveau cout annuel (prix fixe {params['prix_vente_kwh']:.3f} EUR/kWh)", _fmt_eur(new_c)],
        ["Economie annee 1", f"{_fmt_eur(savings_1)}  ({_fmt_pct(pct)})"],
        [f"Economie cumulee sur {horizon} ans", _fmt_eur(results["total_economie_client"])],
    ]
    t2 = Table(client_data, colWidths=[9 * cm, 6 * cm])
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHTGREY),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHTGREY]),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t2)
    story.append(Spacer(1, 0.3 * cm))
    story.append(Image(_chart_client_savings(result_df), width=15.5 * cm, height=7.3 * cm))

    story.append(PageBreak())

    # --- Rentabilite ---
    # Le libelle de section et l'intitule de la ligne "gain" dependent du mode de vente :
    # en "vente_directe" le client est lui-meme l'investisseur, sinon on parle du
    # point de vue fournisseur/investisseur tiers qui revend l'energie au client.
    _is_vente_directe = results.get("mode_vente") == "vente_directe"
    _titre_rentabilite = ("Rentabilite (cote client -- investissement direct)" if _is_vente_directe
                           else "Rentabilite (cote investisseur / fournisseur)")
    _label_gain = (f"Retour net cumule client ({horizon} ans, apres remplacement batterie)" if _is_vente_directe
                   else f"Gain brut cumule ({horizon} ans)")
    story.append(Paragraph(_titre_rentabilite, h2))
    payback = results["payback_year"]
    # payback_year vaut None si le CAPEX n'est jamais rembourse dans l'horizon simule.
    payback_str = f"{payback:.1f} ans" if payback is not None else f"Non atteint sur {horizon} ans"
    roi_data = [
        ["CAPEX initial", _fmt_eur(results["capex_total"])],
        [_label_gain, _fmt_eur(results["total_gain_brut"])],
        ["ROI net (non actualise)", _fmt_pct(results["roi_pct"])],
        [f"VAN (taux {results['discount_rate']*100:.1f}%)", _fmt_eur(results["npv_eur"])],
        # `x == x` est faux si x est NaN (float) : c'est le test utilise ici pour detecter
        # un TRI non calculable (ex: cashflows qui ne changent jamais de signe).
        ["TRI", _fmt_pct(results["irr_pct"]) if results["irr_pct"] == results["irr_pct"] else "Non calculable"],
        ["Temps de retour (payback)", payback_str],
    ]
    t3 = Table(roi_data, colWidths=[9 * cm, 6 * cm])
    t3.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHTGREY),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHTGREY]),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 2), (-1, 2), 1, NAVY),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
    ]))
    story.append(t3)
    story.append(Spacer(1, 0.3 * cm))
    story.append(Image(_chart_cashflow(result_df, results["capex_total"]), width=15.5 * cm, height=7.3 * cm))

    story.append(PageBreak())

    # --- Origine du gain ---
    story.append(Paragraph("Origine du gain (annee 1)", h2))
    story.append(Paragraph(
        "Decomposition en cascade, sans double comptage : le gain PV correspond a "
        "l'autoconsommation + l'injection directe, le gain batterie a l'arbitrage "
        "Day-Ahead et a l'ecretement de pointe, EN PLUS de ce que le PV seul apporterait.",
        body))
    story.append(Spacer(1, 0.2 * cm))
    story.append(Image(_chart_gain_split(decomp), width=9 * cm, height=7 * cm))

    gain_data = [
        ["Cout si 100% reseau", _fmt_eur(decomp["cout_sans_rien_eur"])],
        ["Cout avec PV seul", _fmt_eur(decomp["cout_pv_seul_eur"])],
        ["Cout avec PV + batterie", _fmt_eur(decomp["cout_pv_batterie_eur"])],
        ["Gain attribuable au PV", _fmt_eur(decomp["gain_pv_eur"])],
        ["Gain attribuable a la batterie", _fmt_eur(decomp["gain_batterie_belpex_eur"])],
        ["Gain total vs 100% reseau", _fmt_eur(decomp["gain_total_vs_tout_reseau_eur"])],
    ]
    t4 = Table(gain_data, colWidths=[9 * cm, 6 * cm])
    t4.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHTGREY),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHTGREY]),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t4)

    # Avertissement additionnel si une des deux composantes du gain (PV ou batterie) est
    # negative : signale un cas limite du dispatch (ex: tarif capacitaire penalisant)
    # que le camembert ci-dessus ne peut pas representer correctement (valeurs plafonnees a 0).
    if decomp["gain_pv_eur"] < 0 or decomp["gain_batterie_belpex_eur"] < 0:
        story.append(Spacer(1, 0.3 * cm))
        story.append(Paragraph(
            "ATTENTION : un gain decompose ci-dessus est negatif -- cas limite du dispatch "
            "(le tarif capacitaire ou une contrainte de contrat peut rendre l'ajout de PV/batterie "
            "ponctuellement defavorable dans cette decomposition). Le graphique plafonne a 0 pour "
            "rester lisible. Verifier le gain total et les hypotheses (contrat souscrit, tarif "
            "capacitaire) avant d'envoyer ce rapport a un client.",
            ParagraphStyle("Warn2", parent=body, textColor=colors.red)))

    story.append(PageBreak())

    # --- Detail annuel (tableau complet) ---
    story.append(Paragraph("Detail annee par annee", h2))
    header = ["Annee", "Revenu client", "Cout approv.", "Gain brut", "Economie client", "Cashflow cumule"]
    rows = [header]  # premiere ligne = en-tete, reutilisee comme repeatRows=1 plus bas (repetee sur chaque page).
    for _, r in result_df.iterrows():
        rows.append([
            int(r["annee"]),
            f"{r['revenu_client_eur']:,.0f}".replace(",", " "),
            f"{r['cout_approvisionnement_eur']:,.0f}".replace(",", " "),
            f"{r['gain_brut_fournisseur_eur']:,.0f}".replace(",", " "),
            f"{r['economie_client_eur']:,.0f}".replace(",", " "),
            f"{r['cashflow_cumule_eur']:,.0f}".replace(",", " "),
        ])
    t5 = Table(rows, colWidths=[1.6 * cm, 2.9 * cm, 2.9 * cm, 2.9 * cm, 2.9 * cm, 2.9 * cm], repeatRows=1)
    t5.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHTGREY]),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(t5)

    story.append(Spacer(1, 1 * cm))
    story.append(Paragraph(
        "Ce rapport est genere automatiquement a partir d'un modele d'optimisation "
        "(dispatch Day-Ahead heure par heure) applique aux profils de consommation et "
        "de production reels du site. Les hypotheses de prix de marche, d'inflation et "
        "de degradation figurent dans le fichier Excel source.", small))

    doc.build(story)
    return output_path
