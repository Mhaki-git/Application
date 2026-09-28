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
        s = f"{x:,.{decimals}f}".replace(",", " ")
        return f"{s} {suffix}" if suffix else s
    except Exception:
        return "N/A"


def fmt_eur(x, unit_suffix: bool = True):
    return fmt_number(x, suffix="EUR" if unit_suffix else "")


def fmt_pct(x):
    try:
        return f"{x:.1f} %"
    except Exception:
        return "N/A"
