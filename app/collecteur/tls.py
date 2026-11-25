"""Connexion PostgreSQL du collecteur : TLS verify-full IMPOSÉ par le code, indépendamment du DSN.

Toute connexion vers un hôte non local passe en `sslmode=verify-full` avec la racine de Supabase embarquée dans le
dépôt (`certs/supabase-prod-ca-2021.crt`, certificat PUBLIC, aucune clé) : certificat ET nom d'hôte vérifiés. Les
paramètres sont passés en mots-clés, qui l'emportent sur ceux du DSN (un `sslmode=prefer|require|disable` du secret
est donc ignoré). Après connexion, le mode et l'usage effectif de TLS sont relus : plus faible -> refus.
Seule exception : la base locale de test (localhost, 127.0.0.1, ::1, socket Unix), sans TLS.
"""
import os
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
    """psycopg2.connect avec TLS verify-full imposé ; vérifie le mode EFFECTIF de la session."""
    tls = parametres_tls(dsn)
    cx = psycopg2.connect(dsn, **kw, **tls)
    if tls:
        mode = cx.get_dsn_parameters().get("sslmode")
        if mode != "verify-full" or not cx.info.ssl_in_use:
            cx.close()
            raise TLSInsuffisant(f"session TLS insuffisante (sslmode={mode}, TLS={cx.info.ssl_in_use}) : connexion refusée")
    return cx
