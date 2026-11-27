"""Alertes de la passe (codes de sortie du job GitHub : un échec déclenche l'e-mail de GitHub).

- 2 : arrêt de sûreté (inchangé) ;
- 3 : BLOCAGE « solde » ou « mois » (décision du 27/11/2026, panne silencieuse du 16 au 18/11 : 635 passes bloquées
      sorties en succès pendant 51,8 h) ; les blocages « jour » et « session » restent des fins de passe normales ;
- 4 : RAPPEL d'expiration de l'abonnement Data Docked à J-3 et J-1 (jours UTC) de la date connue
      (COLLECTOR_EXPIRATION_ABONNEMENT) ; écrit dans le journal à chaque passe de ces jours, échec UNE fois par jour.
"""
from datetime import datetime
from typing import Optional

CODE_ARRET, CODE_BLOCAGE, CODE_RAPPEL = 2, 3, 4
BLOCAGES_EN_ECHEC = ("solde", "mois")
JALONS_JOURS = (3, 1)
MARQUE_RAPPEL = "RAPPEL EXPIRATION"
MARQUE_ECHEC = "(échec signalé)"


def jalon_expiration(expiration: Optional[datetime], t: datetime) -> Optional[int]:
    """Jours (UTC) restant avant la date d'expiration si c'est un jalon (3 ou 1), sinon None."""
    if expiration is None:
        return None
    j = (expiration.date() - t.date()).days
    return j if j in JALONS_JOURS else None


def message_rappel(expiration: datetime, jours: int) -> str:
    return f"{MARQUE_RAPPEL} J-{jours} : l'abonnement Data Docked expire le {expiration:%Y-%m-%d %H:%M} UTC"


def code_sortie(code_arret: int, blocage: Optional[str], rappel_en_echec: bool) -> int:
    if code_arret:
        return code_arret
    if blocage in BLOCAGES_EN_ECHEC:
        return CODE_BLOCAGE
    return CODE_RAPPEL if rappel_en_echec else 0
