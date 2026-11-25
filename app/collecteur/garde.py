"""Garde-fou budgétaire, journal des crédits (en base) et verrou anti-chevauchement.

Logique reprise des scripts de collecte éprouvés :
  - garde-fou dur relu AVANT CHAQUE appel (engagé + coût > plafond OU solde < coût -> refus) ;
    arrêt si solde indéterminable ; classification recue | non_emise | perdue | refus | quota |
    serveur ; seule non_emise est réessayée ; perdue jamais redemandée ;
  - journal en DEUX TEMPS : ligne « lance » COMMITÉE avant l'appel, complétée après ; une ligne
    restée « lance » (processus tué pendant l'appel) compte au coût max ;
  - dépense = somme des débits RÉELS (solde avant − après) de NOTRE journal ; débit illisible
    compté au coût max ; débit supérieur au coût max d'un appel -> arrêt (consommation
    concurrente sur le compte).

MODE CRON : aucune confirmation humaine par passe. Les plafonds jour et mois, vérifiés avant
chaque appel sur le journal en base, sont l'autorisation permanente ; le plafond de session
(une passe) borne un emballement.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import psycopg2

from .config import CLE_VERROU, Config
from .tls import connecter


class ArretCollecte(Exception):
    """Arrêt propre et définitif de la passe (solde illisible, débit anormal, base injoignable…)."""


class RefusBudget(Exception):
    """L'appel dépasserait un plafond : il n'est PAS émis. `portee` : session | jour | mois | solde."""

    def __init__(self, portee: str, message: str):
        super().__init__(message)
        self.portee = portee


def maintenant() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------------- journal
class JournalDB:
    """credits_journal : source de vérité des dépenses. Connexion DÉDIÉE en autocommit : une
    ligne « lance » est durable avant l'appel, quoi qu'il arrive à la transaction principale."""

    def __init__(self, dsn: str, schema: str):
        self.cx = connecter(dsn)
        self.cx.autocommit = True
        with self.cx.cursor() as c:
            c.execute(f"set search_path to {schema}, public")

    def ajouter(self, obj: dict) -> None:
        with self.cx.cursor() as c:
            if obj["evt"] == "lance":
                c.execute("insert into credits_journal (ts, session, endpoint, statut, emis, solde_avant, appel_id, "
                          "cout_max, params) values (%s, %s, %s, 'lance', true, %s, %s, %s, %s)",
                          (obj["ts"], obj["session"], obj["endpoint"], obj.get("solde_avant"), obj["appel_id"],
                           obj["cout_max"], json.dumps(obj.get("params"))))
            elif obj["evt"] == "termine":
                c.execute("update credits_journal set statut = %s, emis = %s, solde_apres = %s, depense = %s, "
                          "http = %s, erreur = %s, termine_le = %s where appel_id = %s",
                          (obj["statut"], obj["emis"], obj.get("solde_apres"), obj.get("depense"), obj.get("http"),
                           obj.get("erreur"), obj["ts"], obj["appel_id"]))

    _DEPENSE = """
        case when statut = 'lance' then cout_max         -- tué pendant l'appel : coût max
             when not emis then 0                        -- jamais parti : non facturable
             when depense is null then cout_max          -- débit illisible : coût max
             when depense > cout_max or depense < 0 then cout_max   -- anomalie de solde : coût normal
             else depense end"""

    def totaux(self, session: Optional[str] = None, t: Optional[datetime] = None) -> dict:
        t = t or maintenant()
        with self.cx.cursor() as c:
            c.execute(f"""
                select coalesce(sum({self._DEPENSE}) filter (where session = %s), 0),
                       coalesce(sum({self._DEPENSE}) filter (where ts >= date_trunc('day', %s::timestamptz at time zone 'UTC') at time zone 'UTC'), 0),
                       coalesce(sum({self._DEPENSE}) filter (where ts >= date_trunc('month', %s::timestamptz at time zone 'UTC') at time zone 'UTC'), 0),
                       coalesce(sum({self._DEPENSE}), 0),
                       count(*) filter (where statut = 'lance')
                from credits_journal""", (session, t, t))
            s, j, m, tot, incertains = c.fetchone()
        return {"session": int(s), "jour": int(j), "mois": int(m), "total": int(tot), "incertains": int(incertains)}

    def fermer(self) -> None:
        self.cx.close()


# ------------------------------------------------------------------------------- garde-fou
@dataclass
class Garde:
    cfg: Config
    journal: JournalDB
    session: str
    plafond_session: int
    solde: Callable[[], Optional[int]]

    def verifier(self, endpoint: str, cout: int) -> int:
        """Garde-fou DUR avant CHAQUE appel. Retourne le solde avant ; lève RefusBudget ou
        ArretCollecte. Relit le journal à chaque fois (rien n'est gardé en mémoire)."""
        avant = self.solde()
        if avant is None:
            raise ArretCollecte("solde indéterminable : un garde-fou qui ne sait pas ce qu'il reste ne protège rien")
        tot = self.journal.totaux(self.session)
        p = self.cfg.plafonds
        if tot["session"] + cout > self.plafond_session:
            raise RefusBudget("session", f"{endpoint} : {tot['session']} engagés + {cout} > plafond de passe "
                                         f"{self.plafond_session}")
        if tot["jour"] + cout > p.jour:
            raise RefusBudget("jour", f"{endpoint} : {tot['jour']} dépensés aujourd'hui + {cout} > plafond jour {p.jour}")
        if tot["mois"] + cout > p.mois:
            raise RefusBudget("mois", f"{endpoint} : {tot['mois']} dépensés ce mois + {cout} > plafond mois {p.mois}")
        if avant < cout:
            raise RefusBudget("solde", f"{endpoint} : solde {avant} < coût {cout}")
        return avant


# ------------------------------------------------------------------------------- verrou
class VerrouDB:
    """Verrou consultatif PostgreSQL : une seule passe à la fois, quel que soit l'hôte (runner
    GitHub, poste local). Tenu par une connexion de SESSION (pooler Supabase port 5432) ; libéré
    à la fermeture de la connexion, donc aussi si le processus est tué."""

    def __init__(self, dsn: str, cle: int = CLE_VERROU):
        self.dsn, self.cle, self.cx = dsn, cle, None

    def prendre(self) -> bool:
        self.cx = connecter(self.dsn)
        self.cx.autocommit = True
        with self.cx.cursor() as c:
            c.execute("select pg_try_advisory_lock(%s)", (self.cle,))
            ok = c.fetchone()[0]
        if not ok:
            self.cx.close()
            self.cx = None
        return ok

    def rendre(self) -> None:
        if self.cx is not None:
            try:
                with self.cx.cursor() as c:
                    c.execute("select pg_advisory_unlock(%s)", (self.cle,))
            finally:
                self.cx.close()
                self.cx = None
