"""
Formatage et style ReportLab partages entre pdf_report.py et financial_sheet.py.
"""
from reportlab.lib import colors

NAVY = colors.HexColor("#1B2A4A")
ACCENT = colors.HexColor("#F5A623")
LIGHTGREY = colors.HexColor("#F2F2F2")


def fmt_eur(x, unit_suffix: bool = True):
    try:
        s = f"{x:,.0f}".replace(",", " ")
        return f"{s} EUR" if unit_suffix else s
    except Exception:
        return "N/A"


def fmt_pct(x):
    try:
        return f"{x:.1f} %"
    except Exception:
        return "N/A"
