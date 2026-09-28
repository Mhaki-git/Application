"""
Formatage et style ReportLab partages entre pdf_report.py et financial_sheet.py.
"""
from reportlab.lib import colors

NAVY = colors.HexColor("#1B2A4A")
ACCENT = colors.HexColor("#F5A623")
LIGHTGREY = colors.HexColor("#F2F2F2")


def fmt_number(x, decimals: int = 0, suffix: str = "") -> str:
    """
    Formate un nombre avec separateur de milliers ' ' (espace, convention FR),
    et un suffixe d'unite optionnel (ex: "kWh/an", "EUR"). Usage generique --
    fmt_eur ci-dessous est le cas particulier "EUR" le plus frequent.
    """
    try:
        # Python formate d'abord avec des virgules (convention US), puis on les remplace
        # par des espaces pour obtenir le separateur de milliers de la convention FR.
        s = f"{x:,.{decimals}f}".replace(",", " ")
        return f"{s} {suffix}" if suffix else s
    except Exception:
        # x non numerique (None, chaine, NaN mal gere, etc.) : ne jamais lever d'exception
        # dans un rapport genere, afficher "N/A" a la place.
        return "N/A"


def fmt_eur(x, unit_suffix: bool = True):
    """
    Formate un montant en euros avec separateur de milliers "espace" et 0
    decimale (ex: 12345.6 -> "12 346 EUR"). unit_suffix=False omet le
    suffixe "EUR" (utile quand l'unite est deja indiquee dans le libelle
    de la ligne/colonne appelante).
    """
    return fmt_number(x, suffix="EUR" if unit_suffix else "")


def fmt_pct(x):
    """
    Formate une valeur numerique deja exprimee en pourcentage (pas une
    fraction : 12.3 -> "12.3 %", pas 0.123) avec une decimale.
    Retourne "N/A" si x n'est pas formattable (None, NaN, texte, etc.),
    ce qui permet de l'utiliser directement sur des resultats potentiellement
    non calculables (ex: TRI absent) sans verification prealable.
    """
    try:
        return f"{x:.1f} %"
    except Exception:
        return "N/A"
