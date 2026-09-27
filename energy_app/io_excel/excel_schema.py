"""
Schema partage de l'onglet "DONNEES ENERGIE" du classeur Excel source :
adresse de chaque parametre. Utilise a la fois pour la lecture
(dataextraction.py) et l'ecriture (excel_writer.py) -- une seule source de
verite pour que les deux restent synchronises.
"""

PARAMETERS_SHEET_NAME = "DONNEES ENERGIE"

# cle du dict de parametres -> adresse de cellule dans l'onglet DONNEES ENERGIE
PARAM_CELL_MAP = {
    "kwc": "F5",
    "kva_onduleur": "F6",
    "battery_power_kw": "F13",
    "battery_capacity_kwh": "F14",

    "old_cout_additionnel_contrat": "J6",
    "old_price_kwh_fixe": "J7",
    "old_price_inj_fixe": "J8",
    "price_hp": "J9",
    "price_hc": "J10",
    "price_inj_hp": "J11",
    "price_inj_hc": "J12",
    "prix_kwc_pv": "J13",
    "prix_kwh_batterie": "J14",
    "heure_debut_hp": "J15",
    "heure_debut_hc": "J16",
    "contrat_kw": "N16",

    "prix_vente_kwh": "J27",
    "prix_injection_fournisseur": "J28",
    "marge_fournisseur": "J29",
    "taxes_couts_proportionnels": "J30",
    "tarif_capacitaire_fournisseur": "J31",
    "marge_injection": "J32",

    "inflation_pct": "N12",
    "maintenance_eur_an": "N13",
    "degradation_pv_pct": "N14",
    "degradation_batterie_pct": "N15",
}
