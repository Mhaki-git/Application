"""
Garde-fou structurel : dataextraction.py et excel_writer.py doivent lire la
carte des cellules Excel depuis la MEME source (excel_schema.py), pas depuis
deux copies independantes qui pourraient diverger silencieusement.
"""
from io_excel import dataextraction, excel_writer, excel_schema


def test_param_cell_map_is_shared_not_duplicated():
    assert dataextraction.PARAM_CELL_MAP is excel_schema.PARAM_CELL_MAP
    assert excel_writer.PARAM_CELL_MAP is excel_schema.PARAM_CELL_MAP


def test_param_cell_map_has_expected_keys():
    expected_keys = {
        "kwc", "kva_onduleur", "battery_power_kw", "battery_capacity_kwh",
        "old_cout_additionnel_contrat", "old_price_kwh_fixe", "old_price_inj_fixe",
        "price_hp", "price_hc", "price_inj_hp", "price_inj_hc",
        "prix_kwc_pv", "prix_kwh_batterie", "heure_debut_hp", "heure_debut_hc",
        "contrat_kw", "prix_vente_kwh", "prix_injection_fournisseur",
        "marge_fournisseur", "taxes_couts_proportionnels",
        "tarif_capacitaire_fournisseur", "marge_injection",
        "inflation_pct", "maintenance_eur_an", "degradation_pv_pct",
        "degradation_batterie_pct",
    }
    assert set(excel_schema.PARAM_CELL_MAP) == expected_keys
