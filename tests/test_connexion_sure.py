"""Règle de sécurité du 27/11/2026 : une erreur de connexion ne laisse JAMAIS sortir le DSN ni le mot de passe
(0 crédit ; base injoignable ou DSN illisible provoqués exprès, aucune base réelle)."""
import subprocess
import sys
from pathlib import Path

import pytest

from app.collecteur import tls

MDP = "Zq7-Secr3t@Mot!De:Passe"
MDP_URL = "Zq7-Secr3t%40Mot%21De%3APasse"
CAS = [
    f"postgresql://collecteur:{MDP_URL}@127.0.0.1:1/base_test?connect_timeout=1",      # injoignable (port 1)
    f"postgresql://collecteur:{MDP}@127.0.0.1:1/base_test",                            # @ et : non échappés
    f"host=127.0.0.1 port=1 dbname=base_test user=collecteur password='{MDP}' connect_timeout=1",
    f"host=127.0.0.1 port=1 dbname=base_test user=collecteur password={MDP_URL} connect_timeout=1 nimporte",  # DSN illisible
]


def _aucun_secret(texte: str):
    for s in (MDP, MDP_URL, "Secr3t", "postgresql://collecteur"):
        assert s not in texte, (s, texte)


@pytest.mark.parametrize("dsn", CAS)
def test_erreur_de_connexion_expurgee(dsn, capsys):
    with pytest.raises(tls.ConnexionImpossible) as e:
        tls.connecter(dsn, connect_timeout=1)
    msg = str(e.value)
    _aucun_secret(msg + capsys.readouterr().out + capsys.readouterr().err)
    assert e.value.__cause__ is None and e.value.__suppress_context__        # pas de message d'origine chaîné
    assert "127.0.0.1" in msg or "DSN illisible" in msg


def test_trace_non_rattrapee_sans_secret():
    """« Même en cas d'exception » : un script qui laisse remonter l'erreur n'affiche aucun secret."""
    racine = str(Path(tls.__file__).resolve().parents[2])
    code = (f"import sys; sys.path.insert(0, {racine!r}); from app.collecteur import tls; "
            f"tls.connecter({CAS[1]!r}, connect_timeout=1)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "ConnexionImpossible" in r.stderr
    _aucun_secret(r.stdout + r.stderr)


def test_decrire_et_expurger():
    assert tls.decrire(CAS[0]).startswith("hôte 127.0.0.1, port 1, base base_test")
    _aucun_secret(tls.decrire(CAS[1]) + tls.decrire(CAS[3]) + tls.decrire({"host": "h", "password": MDP}))
    _aucun_secret(tls.expurger(f"erreur : {CAS[1]} / password={MDP} / {MDP_URL}", CAS[1]))
