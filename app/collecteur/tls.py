"""Connexion PostgreSQL du collecteur : TLS verify-full IMPOSÉ par le code, indépendamment du DSN.

Toute connexion vers un hôte non local passe en `sslmode=verify-full` avec la racine de Supabase embarquée dans le
dépôt (`certs/supabase-prod-ca-2021.crt`, certificat PUBLIC, aucune clé) : certificat ET nom d'hôte vérifiés. Les
paramètres sont passés en mots-clés, qui l'emportent sur ceux du DSN (un `sslmode=prefer|require|disable` du secret
est donc ignoré). Après connexion, le mode et l'usage effectif de TLS sont relus : plus faible -> refus.
Seule exception : la base locale de test (localhost, 127.0.0.1, ::1, socket Unix), sans TLS.
"""
import os
import re
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

import psycopg2
from psycopg2.extensions import parse_dsn

NOM_CA = "supabase-prod-ca-2021.crt"
# certs/ à la racine du dépôt : parents[2] dans le dépôt autonome (app/…), parents[3] dans le projet (api/app/…).
CA_SUPABASE = next((r / "certs" / NOM_CA for r in Path(__file__).resolve().parents[2:4] if (r / "certs" / NOM_CA).exists()),
                   Path(__file__).resolve().parents[2] / "certs" / NOM_CA)
HOTES_LOCAUX = {"", "localhost", "127.0.0.1", "::1"}


class TLSInsuffisant(RuntimeError):
    """Connexion refusée : TLS plus faible que verify-full vers un hôte distant."""


def est_local(dsn: str) -> bool:
    hotes = (parse_dsn(dsn).get("host") or "").split(",")
    return all(h.strip() in HOTES_LOCAUX or h.strip().startswith("/") for h in hotes)


def parametres_tls(dsn: str) -> dict:
    """Mots-clés TLS imposés (vides pour la base locale de test)."""
    if est_local(dsn):
        return {}
    ca = Path(os.getenv("COLLECTOR_SSLROOTCERT") or CA_SUPABASE)
    if not ca.is_file():
        raise TLSInsuffisant(f"racine TLS absente ({ca}) : verify-full impossible, connexion refusée")
    return {"sslmode": "verify-full", "sslrootcert": str(ca)}


def connecter(dsn: str, **kw):
    """psycopg2.connect avec TLS verify-full imposé ; vérifie le mode EFFECTIF de la session. Toute erreur sort
    expurgée (ConnexionImpossible, jamais le DSN ni le mot de passe)."""
    with sans_secret(dsn):
        tls = parametres_tls(dsn)
        cx = psycopg2.connect(dsn, **kw, **tls)
        if tls:
            mode = cx.get_dsn_parameters().get("sslmode")
            if mode != "verify-full" or not cx.info.ssl_in_use:
                cx.close()
                raise TLSInsuffisant(f"session TLS insuffisante (sslmode={mode}, TLS={cx.info.ssl_in_use}) : "
                                     "connexion refusée")
        return cx


# ------------------------------------------------------------------ secrets : jamais affichés (règle du 27/11/2026)
# Toute connexion passe par `sans_secret` : une exception (DSN illisible, base injoignable, TLS…) ne sort que sous la
# forme ConnexionImpossible, au message EXPURGÉ (hôte, port, base ; jamais le DSN ni le mot de passe), sans chaînage
# (`from None`) : ni le message d'origine ni son contexte ne sont réaffichés par une trace non rattrapée.
class ConnexionImpossible(RuntimeError):
    """Connexion impossible ; message expurgé (aucun secret)."""


_URL = re.compile(r"^\s*postgres(?:ql)?://(?P<user>[^:/@]*)(?::(?P<pw>.*))?@(?P<reste>[^@]*)$", re.S)
_CLE_PW = re.compile(r"(password\s*=\s*)('(?:[^'\\]|\\.)*'|\S+)", re.I)


def _secrets(dsn) -> list[str]:
    """Mots de passe présents dans un DSN (URL ou clé=valeur) ou un dict de paramètres, sous leurs formes probables."""
    vus = set()
    try:
        if isinstance(dsn, dict):
            vus.add(str(dsn.get("password") or ""))
        else:
            s = str(dsn or "")
            m = _URL.match(s)
            if m and m.group("pw"):
                vus.add(m.group("pw"))
            for m in _CLE_PW.finditer(s):
                vus.add(m.group(2).strip("'"))
        for p in list(vus):
            vus |= {urllib.parse.unquote(p), urllib.parse.quote(p, safe="")}
    except Exception:  # noqa: BLE001  (l'expurgation ne doit jamais lever)
        pass
    # >= 4 caractères : un mot de passe trivial (« p ») ne doit pas mutiler le message ; URL et password= restent masqués
    return sorted((p for p in vus if len(p) >= 4), key=len, reverse=True)


def expurger(texte, dsn=None) -> str:
    """Retire de `texte` tout mot de passe de `dsn`, toute URL postgres et toute valeur password=."""
    t = str(texte)
    for p in _secrets(dsn):
        t = t.replace(p, "***")
    t = re.sub(r"(postgres(?:ql)?://)[^\s'\"]*", r"\1<DSN masqué>", t)
    return _CLE_PW.sub(r"\1***", t)


def decrire(dsn) -> str:
    """« hôte H, port P, base B » sans jamais lever ni montrer de secret."""
    try:
        p = dict(dsn) if isinstance(dsn, dict) else parse_dsn(dsn)
    except Exception:  # noqa: BLE001
        m = _URL.match(str(dsn or ""))
        if not m:
            return "DSN illisible"
        hp = re.match(r"(?P<host>[^:/?]*)(?::(?P<port>[^/?]*))?(?:/(?P<db>[^?]*))?", m.group("reste"))
        p = {k: v for k, v in (hp.groupdict() if hp else {}).items() if v}
        p = {"host": p.get("host"), "port": p.get("port"), "dbname": p.get("db")}
    hote = (p.get("host") or "?").split(",")[0]
    return expurger(f"hôte {hote}, port {str(p.get('port') or '5432').split(',')[0]}, base {p.get('dbname') or '?'}", dsn)


@contextmanager
def sans_secret(dsn):
    """Contexte de TOUTE ouverture de connexion : exceptions converties en ConnexionImpossible expurgée."""
    try:
        yield
    except (ConnexionImpossible, TLSInsuffisant):      # messages écrits ici, sans secret
        raise
    except Exception as exc:  # noqa: BLE001
        raise ConnexionImpossible(f"connexion impossible ({decrire(dsn)}) : {type(exc).__name__} : "
                                  f"{expurger(str(exc).strip(), dsn)[:300]}") from None
