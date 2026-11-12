"""Client Data Docked du collecteur : chaque appel est gardé, journalisé, classé, archivé.

Séquence d'UN essai :
  1. garde-fou dur (solde réel + plafonds sur le journal en base) — refus = rien n'est émis ;
  2. ligne « lance » du journal, COMMITÉE ;
  3. requête, cadencée par endpoint ;
  4. BRUT archivé dans api_raw (corps COMPLET, commit) AVANT tout traitement, quel que soit le
     statut (copie disque en plus si COLLECTOR_BRUT_DIR) ;
  5. solde relu ; journal complété avec le débit RÉEL (solde avant − après) ;
  6. débit > coût max -> ArretCollecte (consommation concurrente sur le compte).
Nouvel essai SEULEMENT si la requête n'est jamais partie (non_emise), ou si 429/5xx n'a rien
débité. Une requête partie dont la réponse est perdue n'est JAMAIS redemandée : un ReadTimeout
a été facturé 50 crédits le 2026-10-20.
"""
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

import httpx

from .config import (API, ATTENTE_AVANT_SOLDE_APRES_S, BACKOFF_S, COUT, INTERVALLE_S, TENTATIVES_NON_EMISE,
                     TENTATIVES_QUOTA_SANS_DEBIT, TIMEOUT_S, Config)
from .garde import ArretCollecte, Garde, JournalDB, maintenant

NON_EMISE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


@dataclass
class Resultat:
    statut: str                 # recue | non_emise | perdue | refus | quota | serveur
    http: Optional[int]
    corps: Any
    erreur: Optional[str]
    depense: Optional[int]
    appel_id: str
    brut: Optional[str]
    api_raw_id: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.statut == "recue"


def classer_reponse(r: httpx.Response, valider: Optional[Callable[[Any], bool]]) -> tuple[str, Any, Optional[str]]:
    if r.status_code == 429:
        return "quota", _corps(r), r.text[:300]
    if 400 <= r.status_code < 500:
        return "refus", _corps(r), r.text[:300]
    if r.status_code >= 500:
        return "serveur", _corps(r), r.text[:300]
    try:
        corps = r.json()
    except ValueError:
        return "perdue", {"texte": r.text[:5000]}, f"200 non-JSON : {r.text[:200]}"
    if valider is not None and not valider(corps):
        return "perdue", corps, f"200 au format inattendu : {str(corps)[:200]}"
    return "recue", corps, None


def _corps(r: httpx.Response):
    try:
        return r.json()
    except ValueError:
        return {"texte": r.text[:5000]}


class ClientDD:
    def __init__(self, cfg: Config, cle: str, journal: JournalDB, session: str, plafond_session: int,
                 store=None, transport: Optional[httpx.BaseTransport] = None, dormir: Callable = time.sleep):
        self.cfg = cfg
        self.journal = journal
        self.session = session
        self.store = store
        self.dormir = dormir
        self.http = httpx.Client(base_url=API, headers={"accept": "application/json", "x-api-key": cle},
                                 transport=transport)
        self.garde = Garde(cfg, journal, session, plafond_session, self.solde)
        self._derniers: dict[str, float] = {}

    # ---------------------------------------------------------------- utilitaires
    def _cadence(self, endpoint: str) -> None:
        attente = INTERVALLE_S.get(endpoint, 1.0) - (time.monotonic() - self._derniers.get(endpoint, -1e9))
        if attente > 0:
            self.dormir(attente)
        self._derniers[endpoint] = time.monotonic()

    def solde(self) -> Optional[int]:
        """my-credits, 0 crédit. None après 4 essais -> l'appelant DOIT s'arrêter."""
        for k in range(4):
            try:
                self._cadence("my-credits")
                r = self.http.get("/my-credits", timeout=20)
                if r.status_code == 200:
                    d = r.json()
                    return int((d.get("detail") or d)["credits"])
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                pass
            self.dormir(2 * 2 ** k)
        return None

    def _ecrire_brut(self, appel_id: str, endpoint: str, params: dict, statut: str, http, erreur, corps):
        """Copie disque optionnelle (COLLECTOR_BRUT_DIR) ; la copie qui fait foi est api_raw."""
        if self.cfg.dossier_brut is None:
            return None
        t = maintenant()
        nom = f"{t:%Y%m%dT%H%M%S}_{endpoint}_{appel_id[:8]}.json"
        chemin = self.cfg.dossier_brut / t.strftime("%Y-%m-%d") / nom
        chemin.parent.mkdir(parents=True, exist_ok=True)
        tmp = chemin.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": t.isoformat(), "appel_id": appel_id, "session": self.session,
                                   "endpoint": endpoint, "params": params, "statut": statut, "http": http,
                                   "erreur": erreur, "data": corps}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, chemin)
        return str(chemin)

    # ---------------------------------------------------------------- appel
    def appeler(self, endpoint: str, params: dict, valider: Optional[Callable[[Any], bool]] = None) -> Resultat:
        cout = COUT[endpoint]
        non_emises, sans_debit = 0, 0
        while True:
            avant = self.garde.verifier(endpoint, cout)          # RefusBudget / ArretCollecte
            appel_id = uuid.uuid4().hex
            self.journal.ajouter({"evt": "lance", "ts": maintenant().isoformat(), "appel_id": appel_id,
                                  "session": self.session, "endpoint": endpoint, "params": params,
                                  "cout_max": cout, "solde_avant": avant})
            self._cadence(endpoint)
            http, corps, erreur = None, None, None
            try:
                r = self.http.get(f"/{endpoint}", params=params,
                                  timeout=TIMEOUT_S.get(endpoint, TIMEOUT_S["default"]))
                http = r.status_code
                statut, corps, erreur = classer_reponse(r, valider)
            except NON_EMISE as exc:
                statut, erreur = "non_emise", type(exc).__name__
            except httpx.TransportError as exc:                  # partie, réponse perdue
                statut, erreur = "perdue", type(exc).__name__
            # BRUT archivé (api_raw, commit) AVANT tout traitement et avant même la relecture du
            # solde : une réponse payée est durable quoi qu'il arrive ensuite.
            brut = self._ecrire_brut(appel_id, endpoint, params, statut, http, erreur, corps)
            api_raw_id = None
            if self.store is not None:
                try:
                    api_raw_id = self.store.api_raw(endpoint, {**params, "appel_id": appel_id}, http, None, corps,
                                                    statut)
                    self.store.commit()
                except Exception as exc:  # noqa: BLE001 — pas de dépense sans stockage
                    raise ArretCollecte(f"base injoignable après un appel payé ({type(exc).__name__}: {exc}) ; "
                                        f"journal « lance » conservé : compté au coût max") from exc
            if statut != "non_emise":
                self.dormir(ATTENTE_AVANT_SOLDE_APRES_S)
            apres = self.solde()
            depense = None if apres is None else avant - apres
            emis = statut != "non_emise"
            self.journal.ajouter({"evt": "termine", "ts": maintenant().isoformat(), "appel_id": appel_id,
                                  "session": self.session, "endpoint": endpoint, "statut": statut, "http": http,
                                  "erreur": erreur, "emis": emis, "solde_avant": avant, "solde_apres": apres,
                                  "depense": depense, "cout_max": cout})
            res = Resultat(statut, http, corps, erreur, depense, appel_id, brut, api_raw_id)
            if self.store is not None:
                self.store.api_raw_credits(api_raw_id, depense)
                self.store.commit()
            if depense is not None and depense > cout:
                raise ArretCollecte(f"débit {depense} > coût max {cout} sur {endpoint} : consommation "
                                    f"concurrente sur le compte ?")
            if apres is None:
                raise ArretCollecte("solde indéterminable après l'appel")
            if statut == "non_emise" and non_emises < TENTATIVES_NON_EMISE - 1:
                non_emises += 1
                self.dormir(5.0 * non_emises)
                continue
            if statut in ("quota", "serveur") and depense == 0 and sans_debit < TENTATIVES_QUOTA_SANS_DEBIT - 1:
                self.dormir(BACKOFF_S[min(sans_debit, len(BACKOFF_S) - 1)])
                sans_debit += 1
                continue
            if statut in ("quota", "serveur") and depense != 0:
                res.statut = "perdue"                            # potentiellement facturée : jamais redemandée
            return res

    def fermer(self) -> None:
        self.http.close()
