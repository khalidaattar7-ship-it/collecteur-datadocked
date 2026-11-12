"""Congestion horaire dérivée des événements d'escale — PARITÉ avec le compteur ANP historique.

Définitions (identiques au compteur construit sur les exports de la capitainerie) :
  rade(t)       = escales avec entrée_mouillage <= t < accostage. CAUSAL : une escale encore
                  au mouillage (pas d'accostage) compte jusqu'à son départ ou sa sortie de rade.
                  Sans entrée au mouillage (accostage direct) : JAMAIS en rade. Accostage <
                  entrée au mouillage : incohérente, exclue.
  rade_LINER(t) = idem, catégorie LINER.
  a_quai(t)     = escales avec 1er accostage <= t < DERNIÈRE sortie de quai (un changement de
                  poste ne coupe pas le séjour). Séjour > 60 jours : aberrant, exclu (plafond).
  n_total(t)    = rade(t) + a_quai(t).
Règles de saisie : année hors [2000, 2030] -> valeur ignorée ; champ multi-valeurs « a | b »
-> 1re valeur (polygones.premier_token, appliqué au statut de navigation).
Grille : heures pleines, heure LOCALE de Casablanca (comme les exports).
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

CASA = ZoneInfo("Africa/Casablanca")
VERSION = "v2_polygones"
PLAFOND_QUAI = timedelta(days=60)
OUVERTS = ("approaching", "anchored", "berthed")


def annee_ok(t: Optional[datetime]) -> Optional[datetime]:
    return t if t is not None and 2000 <= t.year <= 2030 else None


def intervalles(escales: list[dict], maintenant: datetime) -> dict:
    """{'rade': [(debut, fin, cat)], 'quai': [(debut, fin, cat)], 'exclues': {...}}."""
    infini = maintenant + timedelta(days=3650)
    rade, quai = [], []
    exclues = {"incoherentes": 0, "quai_plus_60j": 0, "rade_sans_fin": 0}
    for e in escales:
        cat = e.get("categorie")
        ouverte = e["statut"] in OUVERTS
        mouil, acc = annee_ok(e.get("anchorage_entry_at")), annee_ok(e.get("berth_entry_at"))
        sortie_quai, depart = annee_ok(e.get("berth_exit_at")), annee_ok(e.get("departure_at"))
        if mouil is not None:
            if acc is not None and acc < mouil:
                exclues["incoherentes"] += 1
            else:
                fin = acc or annee_ok(e.get("anchorage_exit_at")) or depart or (infini if ouverte else None)
                if fin is None:
                    exclues["rade_sans_fin"] += 1
                else:
                    rade.append((mouil, fin, cat))
        if acc is not None:
            fin = sortie_quai or depart or (infini if ouverte else None)
            if fin is not None and fin < acc:
                exclues["incoherentes"] += 1
            elif fin is not None:
                if min(fin, maintenant) - acc > PLAFOND_QUAI:
                    exclues["quai_plus_60j"] += 1
                else:
                    quai.append((acc, fin, cat))
    return {"rade": rade, "quai": quai, "exclues": exclues}


def creneaux(debut: datetime, fin: datetime) -> list[datetime]:
    """Heures pleines locales de Casablanca dans [debut, fin]."""
    t = debut.astimezone(CASA).replace(minute=0, second=0, microsecond=0)
    if t < debut:
        t += timedelta(hours=1)
    sortie = []
    while t <= fin:
        sortie.append(t)
        t = (t.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(CASA)
    return sortie


def compter(iv: dict, t: datetime) -> dict:
    rade = [c for d, f, c in iv["rade"] if d <= t < f]
    quai = [1 for d, f, _ in iv["quai"] if d <= t < f]
    return {"rade": len(rade), "rade_liner": sum(c == "LINER" for c in rade), "a_quai": len(quai),
            "n_total": len(rade) + len(quai)}


def recalculer(store, debut: datetime, fin: Optional[datetime] = None, qualite: str = "ok") -> int:
    """Recalcule congestion_horaire sur [debut, fin] depuis les escales. Idempotent."""
    fin = fin or datetime.now(timezone.utc)
    iv = intervalles(store.intervalles_congestion(), fin)
    n = 0
    for t in creneaux(debut, fin):
        c = compter(iv, t)
        store.ecrire_congestion(t, c["rade"], c["rade_liner"], c["a_quai"], c["n_total"], qualite, VERSION)
        n += 1
    store.commit()
    return n
