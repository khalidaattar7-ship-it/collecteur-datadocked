"""Alertes de passe (27/11/2026) : codes de sortie, rappel et arrêt à l'expiration (0 base, 0 crédit, date simulée)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.collecteur import alertes as al
from app.collecteur.config import Plafonds

U = timezone.utc
EXP = datetime(2026, 12, 18, 21, 8, 17, tzinfo=U)


@pytest.mark.parametrize("t,attendu", [
    (datetime(2026, 12, 14, 23, 59, tzinfo=U), None),
    (datetime(2026, 12, 15, 0, 0, tzinfo=U), 3),
    (datetime(2026, 12, 15, 23, 59, tzinfo=U), 3),
    (datetime(2026, 12, 16, 12, 0, tzinfo=U), None),
    (datetime(2026, 12, 17, 6, 0, tzinfo=U), 1),
    (datetime(2026, 12, 18, 6, 0, tzinfo=U), None),
])
def test_jalons_j3_j1(t, attendu):
    assert al.jalon_expiration(EXP, t) == attendu


def test_sans_date_aucun_rappel_ni_arret():
    t = datetime(2026, 12, 17, tzinfo=U)
    assert al.jalon_expiration(None, t) is None and not al.expire(None, t)


def test_blocage_premiere_passe_puis_une_fois_par_jour():
    # 1re passe bloquée de l'incident : échec, même si un échec a déjà été signalé aujourd'hui (incident précédent)
    assert al.echec_blocage("solde", precedente_bloquee=False, deja_signale_aujourdhui=True)
    # passes suivantes du même incident, même jour : 0
    assert not al.echec_blocage("solde", precedente_bloquee=True, deja_signale_aujourdhui=True)
    # incident qui dure : 1re passe d'un nouveau jour UTC -> échec
    assert al.echec_blocage("mois", precedente_bloquee=True, deja_signale_aujourdhui=False)
    # plafonds jour / session : jamais en échec
    assert not al.echec_blocage("jour", False, False) and not al.echec_blocage(None, False, False)


def test_passe_bloquee_lue_dans_les_notes():
    assert al.passe_bloquee("passe cron | BLOCAGE (solde) : get-vessels-by-area : solde 0 < coût 10 | ...")
    assert al.passe_bloquee("passe cron | BLOCAGE (mois) : ...")
    assert not al.passe_bloquee("passe cron | BLOCAGE (jour) : ...") and not al.passe_bloquee(None)


def test_codes_de_sortie():
    assert al.code_sortie(0, False, False) == 0
    assert al.code_sortie(0, True, False) == al.CODE_BLOCAGE == 3
    assert al.code_sortie(0, False, True) == al.CODE_RAPPEL == 4
    assert al.code_sortie(0, True, True) == 3                 # le blocage prime sur le rappel
    assert al.code_sortie(2, True, True) == 2                 # l'arrêt de sûreté prime sur tout


def test_message_rappel_porte_la_date():
    m = al.message_rappel(EXP, 3)
    assert m.startswith(al.MARQUE_RAPPEL) and "J-3" in m and "2026-12-18 21:08 UTC" in m


def test_expiration_lue_dans_l_environnement(monkeypatch):
    monkeypatch.setenv("COLLECTOR_EXPIRATION_ABONNEMENT", "2026-12-18T21:08:17Z")
    assert Plafonds().expiration_abonnement == EXP
    monkeypatch.setenv("COLLECTOR_EXPIRATION_ABONNEMENT", "aucune")
    assert Plafonds().expiration_abonnement is None


class FauxStore:
    """Enregistre les requêtes ; `arretee` simule une session d'arrêt déjà écrite ; `cron_ko` un refus de pg_cron."""
    def __init__(self, arretee=False, cron_ko=False):
        self.arretee, self.cron_ko = arretee, cron_ko
        self.sql, self.sessions, self.commits, self.rollbacks = [], [], 0, 0

    def _q(self, sql, params=None, un=False, tous=False):
        self.sql.append((sql, params))
        if "from collecte_sessions" in sql:
            return {"?column?": 1} if self.arretee else None
        if "cron.alter_job" in sql and self.cron_ko:
            raise PermissionError("permission denied for schema cron")
        return None

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def session_debut(self, s, notes):
        self.sessions.append([s, None, notes])

    def session_fin(self, s, statut, notes):
        self.sessions[-1][1:] = [statut, notes]


def test_arret_a_l_expiration_date_simulee():
    t = EXP + timedelta(minutes=3)
    assert al.expire(EXP, t) and not al.expire(EXP, EXP - timedelta(seconds=1)) and al.expire(EXP, EXP)
    st = FauxStore()
    code = al.arret_expiration(st, "tick-x", EXP, t, log=lambda m: None)
    assert code == al.CODE_EXPIRATION == 5
    assert any("cron.alter_job" in q and "active := false" in q and p == (al.JOB_DECLENCHEUR,) for q, p in st.sql)
    assert st.sessions[0][1] == al.STATUT_EXPIRATION and al.NOTE_EXPIRATION in st.sessions[0][2]
    assert "désactivé" in st.sessions[0][2] and "reactiver" in st.sessions[0][2]


def test_passes_suivantes_sans_effet_apres_l_arret():
    st = FauxStore(arretee=True)
    assert al.arret_expiration(st, "tick-y", EXP, EXP + timedelta(hours=2), log=lambda m: None) == 0
    assert not any("cron.alter_job" in q for q, _ in st.sql) and not st.sessions


def test_arret_ecrit_meme_si_pg_cron_refuse():
    st = FauxStore(cron_ko=True)
    assert al.arret_expiration(st, "tick-z", EXP, EXP + timedelta(minutes=1), log=lambda m: None) == 5
    assert st.rollbacks == 1 and "IMPOSSIBLE" in st.sessions[0][2]
