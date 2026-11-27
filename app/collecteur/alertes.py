"""Alertes de la passe (codes de sortie du job GitHub : un échec déclenche l'e-mail de GitHub). Décisions du 27/11/2026.

- 2 : arrêt de sûreté (inchangé) ;
- 3 : BLOCAGE « solde » ou « mois » : à la 1re passe bloquée d'un incident, puis au plus une fois par jour (UTC) ;
      les autres passes bloquées sortent en 0, avec une note de journal (panne silencieuse du 16 au 18/11 : 635 passes
      bloquées sorties en succès pendant 51,8 h) ; les blocages « jour » et « session » restent des fins de passe normales ;
- 4 : RAPPEL d'expiration de l'abonnement à J-3 et J-1 (jours UTC) de la date connue (COLLECTOR_EXPIRATION_ABONNEMENT),
      écrit à chaque passe de ces jours, échec une fois par jour ;
- 5 : ARRÊT PROPRE à l'expiration (abonnement non renouvelé) : à partir de la date d'expiration, la passe n'appelle plus
      Data Docked, désactive le déclencheur pg_cron 004 (`collecteur_declencheur`), écrit une session
      « collecte arrêtée — abonnement expiré » et sort une dernière fois en échec ; les passes suivantes (cron GitHub
      natif) voient l'arrêt et sortent en 0 sans rien faire. Réactiver : nouvelle date dans COLLECTOR_EXPIRATION_ABONNEMENT
      (variable de dépôt), puis `python -m app.collecteur reactiver` (réactive le job pg_cron).
"""
from datetime import datetime
from typing import Optional

CODE_ARRET, CODE_BLOCAGE, CODE_RAPPEL, CODE_EXPIRATION = 2, 3, 4, 5
BLOCAGES_EN_ECHEC = ("solde", "mois")
JALONS_JOURS = (3, 1)
MARQUE_RAPPEL = "RAPPEL EXPIRATION"
MARQUE_ECHEC = "(échec signalé)"
MARQUE_BLOCAGE = "ÉCHEC SIGNALÉ : blocage"
STATUT_EXPIRATION = "arret_expiration"
NOTE_EXPIRATION = "collecte arrêtée — abonnement expiré"
JOB_DECLENCHEUR = "collecteur_declencheur"


def jalon_expiration(expiration: Optional[datetime], t: datetime) -> Optional[int]:
    """Jours (UTC) restant avant la date d'expiration si c'est un jalon (3 ou 1), sinon None."""
    if expiration is None:
        return None
    j = (expiration.date() - t.date()).days
    return j if j in JALONS_JOURS else None


def message_rappel(expiration: datetime, jours: int) -> str:
    return f"{MARQUE_RAPPEL} J-{jours} : l'abonnement Data Docked expire le {expiration:%Y-%m-%d %H:%M} UTC"


def expire(expiration: Optional[datetime], t: datetime) -> bool:
    return expiration is not None and t >= expiration


def passe_bloquee(notes: Optional[str]) -> bool:
    return any(f"BLOCAGE ({p})" in (notes or "") for p in BLOCAGES_EN_ECHEC)


def echec_blocage(blocage: Optional[str], precedente_bloquee: bool, deja_signale_aujourdhui: bool) -> bool:
    """Blocage solde / mois en échec : 1re passe bloquée d'un incident, puis au plus une fois par jour."""
    if blocage not in BLOCAGES_EN_ECHEC:
        return False
    return (not precedente_bloquee) or (not deja_signale_aujourdhui)


def code_sortie(code_arret: int, blocage_en_echec: bool, rappel_en_echec: bool) -> int:
    if code_arret:
        return code_arret
    if blocage_en_echec:
        return CODE_BLOCAGE
    return CODE_RAPPEL if rappel_en_echec else 0


def arret_expiration(store, session: str, expiration: datetime, t: datetime, log=print) -> int:
    """À appeler AVANT tout appel Data Docked. -> code de sortie (5 la première fois, 0 si l'arrêt est déjà fait)."""
    deja = store._q("select 1 from collecte_sessions where statut = %s and started_at >= %s limit 1",
                    (STATUT_EXPIRATION, expiration), un=True)
    if deja:
        log(f"Collecte arrêtée depuis l'expiration de l'abonnement ({expiration:%Y-%m-%d %H:%M} UTC) : passe sans effet.")
        return 0
    try:
        store._q("select cron.alter_job(jobid, active := false) from cron.job where jobname = %s", (JOB_DECLENCHEUR,))
        store.commit()
        pgcron = f"déclencheur pg_cron « {JOB_DECLENCHEUR} » désactivé"
    except Exception as exc:  # noqa: BLE001  (l'arrêt est écrit quoi qu'il arrive)
        store.rollback()
        pgcron = f"désactivation du déclencheur pg_cron IMPOSSIBLE ({type(exc).__name__}) : le couper à la main"
    note = (f"{NOTE_EXPIRATION} ({expiration:%Y-%m-%d %H:%M} UTC) | {pgcron} | aucun appel Data Docked | "
            f"réactiver : nouvelle COLLECTOR_EXPIRATION_ABONNEMENT puis `python -m app.collecteur reactiver`")
    store.session_debut(session, "passe cron")
    store.session_fin(session, STATUT_EXPIRATION, note)
    log(f"::error::{note}")
    return CODE_EXPIRATION
