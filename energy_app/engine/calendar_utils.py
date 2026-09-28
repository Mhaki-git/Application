"""
Utilitaires de calage/completion par SYMETRIE CALENDAIRE, partages entre
engine/fournisseur_roi.py (alignement des prix Day-Ahead sur les vraies
dates client) et io_excel/dataextraction.py (completion des releves de
consommation incomplets). Facorise ce qui etait duplique a l'identique dans
les deux fichiers -- une seule implementation a corriger/faire evoluer.

Principe : etant donne des points connus indexes par (jour_de_l_annee,
sous-cle -- ex. heure ou (heure, minute)), retrouver pour un point cible la
valeur du jour connu le plus proche en distance CYCLIQUE (sur une annee de
365 jours, sans distinction avant/apres -- un jour proche du 31/12 est aussi
proche du 1er janvier suivant), a la meme sous-cle.
"""
import bisect

_CUM_DAYS = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]


def normalized_doy(month: int, day: int) -> int:
    """
    Jour normalise sur une annee de 365 jours (1..365), en traitant le 29
    fevrier comme le 28 -- rend la distance calendaire comparable entre
    annees bissextiles et non bissextiles.
    """
    day = 28 if (month == 2 and day == 29) else day
    return _CUM_DAYS[month - 1] + day


def normalized_doy_ts(ts) -> int:
    """Variante de normalized_doy prenant directement un objet type Timestamp (.month/.day)."""
    return normalized_doy(ts.month, ts.day)


class NearestByCalendarSymmetry:
    """
    Index pre-construit pour retrouver rapidement, par sous-cle (ex. heure du
    jour, ou (heure, minute)), le point connu dont le jour normalise est le
    plus proche (distance cyclique) d'un jour cible.

    Usage :
        idx = NearestByCalendarSymmetry()
        for ts, val in some_series.items():
            idx.add(subkey=(ts.hour, ts.minute), month=ts.month, day=ts.day, value=val)
        idx.finalize()
        value = idx.closest(subkey=(12, 0), month=3, day=15, fallback=0.0)
    """

    def __init__(self):
        self._by_subkey = {}

    def add(self, subkey, month: int, day: int, value: float) -> None:
        self._by_subkey.setdefault(subkey, []).append((normalized_doy(month, day), value))

    def finalize(self) -> None:
        for key in self._by_subkey:
            self._by_subkey[key].sort()

    def closest(self, subkey, month: int, day: int, fallback: float) -> float:
        candidates = self._by_subkey.get(subkey)
        if not candidates:
            return fallback
        target_doy = normalized_doy(month, day)
        doys = [d for d, _ in candidates]
        pos = bisect.bisect_left(doys, target_doy)
        # Voisins immediats dans la liste triee (distance non cyclique) +
        # premier/dernier element (bouclent sur l'annee, pour ne jamais rater
        # un jour proche a cheval sur le 31/12-1/1).
        idx_candidates = {max(0, pos - 1), min(len(doys) - 1, pos), 0, len(doys) - 1}
        best_val, best_dist = None, None
        for idx in idx_candidates:
            doy, val = candidates[idx]
            dist = abs(doy - target_doy)
            dist = min(dist, 365 - dist)
            if best_dist is None or dist < best_dist:
                best_dist, best_val = dist, val
        return best_val
