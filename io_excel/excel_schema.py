"""
Schema partage de l'onglet "DONNEES ENERGIE" du classeur Excel source :
adresse de chaque parametre. Utilise a la fois pour la lecture
(dataextraction.py) et l'ecriture (excel_writer.py) -- une seule source de
verite pour que les deux restent synchronises.
"""

PARAMETERS_SHEET_NAME = "DONNEES ENERGIE"

# cle du dict de parametres -> adresse de cellule dans l'onglet DONNEES ENERGIE
# Attention : ce mapping est la seule source de verite pour la position des
# parametres dans le classeur. Toute modification doit rester coherente avec
# la mise en page reelle du fichier Excel source, sous peine de lire/ecrire
# la mauvaise cellule silencieusement (openpyxl ne signale pas une adresse
# "valide mais fausse").
PARAM_CELL_MAP = {
    # Bloc installation (colonne F) : dimensionnement PV et batterie.
    "kwc": "F5",                 # puissance PV installee, en kWc
    "kva_onduleur": "F6",        # puissance de l'onduleur, en kVA
    "battery_power_kw": "F13",   # puissance de la batterie, en kW
    "battery_capacity_kwh": "F14",  # capacite de la batterie, en kWh

    # Bloc "ancien contrat" et tarification reseau (colonne J, lignes 6-16).
    "old_cout_additionnel_contrat": "J6",  # cout fixe additionnel de l'ancien contrat, en EUR/an
    "old_price_kwh_fixe": "J7",   # ancien prix d'achat kWh (tarif fixe), en EUR/kWh
    "old_price_inj_fixe": "J8",   # ancien prix de rachat de l'injection, en EUR/kWh
    "price_hp": "J9",             # prix reseau heures pleines, en EUR/kWh
    "price_hc": "J10",            # prix reseau heures creuses, en EUR/kWh
    "price_inj_hp": "J11",        # prix d'injection heures pleines, en EUR/kWh
    "price_inj_hc": "J12",        # prix d'injection heures creuses, en EUR/kWh
    "prix_kwc_pv": "J13",         # cout d'investissement PV, en EUR/kWc (sert au calcul du CAPEX PV)
    "prix_kwh_batterie": "J14",   # cout d'investissement batterie, en EUR/kWh (sert au calcul du CAPEX batterie)
    "heure_debut_hp": "J15",      # heure de debut de la plage heures pleines (entier, 0-23)
    "heure_debut_hc": "J16",      # heure de debut de la plage heures creuses (entier, 0-23)
    "contrat_kw": "N16",          # puissance souscrite du contrat reseau, en kW

    # Bloc "modele fournisseur" (colonne J, lignes 27-32) : parametres de revente au client.
    "prix_vente_kwh": "J27",          # prix de vente fixe au client, en EUR/kWh
    "prix_injection_fournisseur": "J28",  # prix auquel le fournisseur valorise l'injection, en EUR/kWh
    "marge_fournisseur": "J29",       # marge du fournisseur sur l'achat reseau, en EUR/kWh
    "taxes_couts_proportionnels": "J30",  # taxes/couts reseau proportionnels a la conso, en EUR/kWh
    "tarif_capacitaire_fournisseur": "J31",  # tarif capacitaire (peak shaving) facture par le fournisseur, en EUR/kW ; 0 desactive cette contrainte dans le LP
    "marge_injection": "J32",         # marge du fournisseur sur l'injection du surplus, en EUR/kWh

    # Bloc hypotheses financieres (colonne N, lignes 12-15).
    "inflation_pct": "N12",           # taux d'inflation annuel applique aux prix, en %
    "maintenance_eur_an": "N13",      # cout de maintenance annuel, en EUR/an
    "degradation_pv_pct": "N14",      # degradation annuelle du rendement PV, en %
    "degradation_batterie_pct": "N15",  # degradation annuelle de la capacite batterie, en %
}
