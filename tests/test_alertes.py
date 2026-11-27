"""Alertes de passe (27/11/2026) : codes de sortie et rappel d'expiration (0 base, 0 crédit)."""
from datetime import datetime, timezone

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


def test_sans_date_aucun_rappel():
    assert al.jalon_expiration(None, datetime(2026, 12, 17, tzinfo=U)) is None


def test_codes_de_sortie():
    assert al.code_sortie(0, None, False) == 0
    assert al.code_sortie(0, "jour", False) == 0             # plafond jour : fin de passe normale
    assert al.code_sortie(0, "session", False) == 0
    assert al.code_sortie(0, "solde", False) == al.CODE_BLOCAGE == 3
    assert al.code_sortie(0, "mois", False) == 3
    assert al.code_sortie(0, None, True) == al.CODE_RAPPEL == 4
    assert al.code_sortie(0, "solde", True) == 3             # le blocage prime sur le rappel
    assert al.code_sortie(2, "solde", True) == 2             # l'arrêt de sûreté prime sur tout


def test_message_rappel_porte_la_date():
    m = al.message_rappel(EXP, 3)
    assert m.startswith(al.MARQUE_RAPPEL) and "J-3" in m and "2026-12-18 21:08 UTC" in m


def test_expiration_lue_dans_l_environnement(monkeypatch):
    monkeypatch.setenv("COLLECTOR_EXPIRATION_ABONNEMENT", "2026-12-18T21:08:17Z")
    assert Plafonds().expiration_abonnement == EXP
    monkeypatch.setenv("COLLECTOR_EXPIRATION_ABONNEMENT", "aucune")
    assert Plafonds().expiration_abonnement is None
