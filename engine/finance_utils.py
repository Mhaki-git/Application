"""
Utilitaires financiers partages entre fournisseur_roi.py et financial_sheet.py
(NPV/IRR, PMT/amortissement).
"""
from scipy.optimize import brentq


def irr_from_cashflows(cashflows, start_period: int = 0):
    """
    Calcule le TRI (IRR) d'une serie de cashflows par bissection (brentq).

    `start_period` fixe l'exposant du premier cashflow dans l'actualisation
    (1/(1+r)**t) : 0 si le premier cashflow de la liste est deja actualise a
    l'annee 0 (convention fournisseur_roi.py), 1 s'il correspond a la fin de
    la premiere annee (convention financial_sheet.py). Retourne NaN si aucune
    racine n'est trouvee dans [-0.99, 10.0] -- utiliser irr_failure_reason()
    dans ce cas pour distinguer "toujours rentable" de "jamais rentable".
    """
    irr = float("nan")
    try:
        # f(r) = VAN au taux r. brentq a besoin d'un changement de signe entre
        # les deux bornes pour garantir l'existence d'une racine (theoreme des
        # valeurs intermediaires) -- sans ca, pas de recherche possible.
        f = lambda r: sum(cf / (1 + r) ** t for t, cf in enumerate(cashflows, start=start_period))
        # -0.99 (borne basse) evite la division par (1+r)=0 ; 10.0 (soit
        # 1000% de rendement) est une borne haute jugee largement suffisante
        # pour tout cas realiste. Si aucun changement de signe -> pas d'IRR.
        if f(-0.99) * f(10.0) < 0:
            irr = brentq(f, -0.99, 10.0)
    except Exception:
        pass
    return irr


def irr_failure_reason(cashflows) -> str:
    """
    A appeler seulement quand irr_from_cashflows() a retourne NaN, pour
    expliquer POURQUOI a l'utilisateur plutot qu'un simple "N/A" ambigu (deux
    causes tres differentes peuvent produire le meme NaN) :

    - "toujours_rentable" : tous les cash-flows (hors le premier, l'investissement
      initial) sont positifs ou nuls -- rentabilite si elevee qu'aucun taux
      d'actualisation teste (jusqu'a 1000%) ne suffit a annuler la VAN.
    - "jamais_rentable" : tous les cash-flows (hors le premier) sont negatifs
      ou nuls -- le projet ne se rembourse jamais sur l'horizon simule.
    - "indetermine" : cas residuel (signes mixtes mais pas de racine dans la
      plage testee -- rare, ex. plusieurs changements de signe qui s'annulent).
    """
    if len(cashflows) < 2:
        return "indetermine"
    # On exclut le premier cashflow (investissement initial, generalement
    # negatif) : seul le signe des flux suivants determine si le projet peut
    # theoriquement s'annuler a un taux d'actualisation quelconque.
    rest = cashflows[1:]
    if all(cf >= 0 for cf in rest):
        return "toujours_rentable"
    if all(cf <= 0 for cf in rest):
        return "jamais_rentable"
    return "indetermine"


def compute_npv_irr(cashflows: list, discount_rate: float):
    """NPV (cashflows[0] actualise a l'annee 0) + IRR. Convention fournisseur_roi.py."""
    npv = sum(cf / (1 + discount_rate) ** t for t, cf in enumerate(cashflows))
    irr = irr_from_cashflows(cashflows, start_period=0)
    return npv, irr


def pmt(rate: float, nper: int, pv: float) -> float:
    """Equivalent de -PMT(rate, nper, pv) d'Excel (annuite constante)."""
    if nper <= 0:
        return 0.0
    if rate == 0:
        # Cas particulier obligatoire : la formule generale divise par (1 -
        # (1+r)**-n) qui vaut 0 quand r=0, division par zero sinon.
        return pv / nper
    return pv * rate / (1 - (1 + rate) ** (-nper))


def amortization_schedule(rate: float, nper: int, pv: float):
    """
    Reproduit IPMT/PMT periode par periode via un calcul d'amortissement
    classique (solde restant du qui decroit) -- donne les memes valeurs que
    les fonctions financieres Excel pour un pret a annuite constante.
    Retourne (annuite, [interets_periode_1..nper], [capital_periode_1..nper]).
    """
    annuite = pmt(rate, nper, pv)
    solde = pv  # capital restant du, decroit a chaque periode
    interets, capital = [], []
    for _ in range(nper):
        interet = solde * rate          # part interets de l'annuite (IPMT)
        princ = annuite - interet       # part capital rembourse (PPMT)
        solde -= princ
        interets.append(interet)
        capital.append(princ)
    return annuite, interets, capital
