"""TLS du collecteur : verify-full imposé par le code vers tout hôte distant (0 crédit, sans réseau).
Échoue si une connexion distante peut s'établir avec un mode plus faible."""
import pytest

from app.collecteur import tls

DISTANT = "postgresql://u:p@aws-1-eu-central-1.pooler.supabase.com:5432/postgres"


class CxFaux:
    def __init__(self, mode, ssl=True):
        self.mode, self.ferme = mode, False
        self.info = type("I", (), {"ssl_in_use": ssl})()

    def get_dsn_parameters(self):
        return {"sslmode": self.mode}

    def close(self):
        self.ferme = True


def test_racine_embarquee_publique():
    t = tls.CA_SUPABASE.read_text(encoding="ascii")
    assert t.count("BEGIN CERTIFICATE") == 1 and "PRIVATE KEY" not in t


@pytest.mark.parametrize("suffixe", ["", "?sslmode=disable", "?sslmode=prefer", "?sslmode=require", "?sslmode=verify-ca"])
def test_dsn_distant_toujours_verify_full(suffixe):
    p = tls.parametres_tls(DISTANT + suffixe)
    assert p["sslmode"] == "verify-full" and p["sslrootcert"].endswith(tls.NOM_CA)


def test_mots_cles_l_emportent_sur_le_dsn(monkeypatch):
    vu = {}
    monkeypatch.setattr(tls.psycopg2, "connect", lambda dsn, **kw: vu.update(kw) or CxFaux("verify-full"))
    tls.connecter(DISTANT + "?sslmode=disable", connect_timeout=5)
    assert vu["sslmode"] == "verify-full" and vu["connect_timeout"] == 5


@pytest.mark.parametrize("mode,ssl", [("require", True), ("prefer", True), ("disable", False), ("verify-full", False)])
def test_session_plus_faible_refusee(monkeypatch, mode, ssl):
    cx = CxFaux(mode, ssl)
    monkeypatch.setattr(tls.psycopg2, "connect", lambda dsn, **kw: cx)
    with pytest.raises(tls.TLSInsuffisant):
        tls.connecter(DISTANT)
    assert cx.ferme


def test_racine_absente_refus(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLECTOR_SSLROOTCERT", str(tmp_path / "absente.crt"))
    with pytest.raises(tls.TLSInsuffisant):
        tls.parametres_tls(DISTANT)


def test_base_locale_de_test_sans_tls():
    for dsn in ("postgresql://u:p@localhost:5433/portnet", "postgresql://u:p@127.0.0.1/x", "dbname=x"):
        assert tls.parametres_tls(dsn) == {}
    assert tls.parametres_tls("postgresql://u:p@localhost,db.exemple.org/x")["sslmode"] == "verify-full"   # mixte : distant
