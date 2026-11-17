"""Tests du collecteur — ZÉRO crédit : l'API Data Docked est simulée (httpx.MockTransport) avec
des réponses réelles déjà reçues (C:\\poragent_data), la base est un schéma jetable.

    COLLECTOR_DB_DSN=... python -m pytest api/tests_collecteur -q
"""
import json
import os
from pathlib import Path

import httpx
import pytest

from app.collecteur import __main__ as cli
from app.collecteur.boucles import Etat
from app.collecteur.client import ClientDD
from app.collecteur.config import Config, Plafonds
from app.collecteur.garde import ArretCollecte, JournalDB, RefusBudget, VerrouDB, maintenant
from app.collecteur.scheduler import EtatDB, Scheduler, plan
from app.collecteur.store import Store
from app.collecteur.types_navires import VESSEL_TYPE_MAPPING, classer

DONNEES = Path(r"C:\poragent_data\datadocked_temps_reel")
EXPECTED = DONNEES / "brut" / "20261109T180957_port-calls_MACAS_expected_p1.json"
LOCATION = sorted((DONNEES / "suivi_elargi_20261111" / "brut").glob("*loc_suivi*"))[-1]
AREA = DONNEES / "brut" / "20261109T180947_area_MACAS_50km.json"
DSN = os.getenv("COLLECTOR_DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="COLLECTOR_DB_DSN absente")


def charger(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))["data"]


class FakeDD:
    """API simulée : solde décrémenté du coût réel de chaque appel servi."""

    def __init__(self, solde=1000, debit=None):
        self.solde = solde
        self.appels = []
        self.debit = debit or {"get-vessel-location": 1, "port-calls-by-port": 50, "get-vessels-by-area": 10}
        self.erreurs = []
        self.solde_ko = False
        self.location = charger(LOCATION)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        ep = req.url.path.rsplit("/", 1)[-1]
        if ep == "my-credits":
            if self.solde_ko:
                raise httpx.ConnectError("dns", request=req)
            return httpx.Response(200, json={"detail": {"credits": self.solde}})
        self.appels.append(ep)
        if self.erreurs:
            exc, facture = self.erreurs.pop(0)
            if facture:
                self.solde -= self.debit[ep]
            raise exc("simulé", request=req)
        self.solde -= self.debit[ep]
        if ep == "port-calls-by-port":
            st = req.url.params.get("search_type")
            corps = charger(EXPECTED)
            return httpx.Response(200, json=corps if st == "expected" else {st: corps["expected"]})
        if ep == "get-vessels-by-area":
            return httpx.Response(200, json=charger(AREA))
        mmsi = req.url.params.get("imo_or_mmsi")
        return httpx.Response(200, json=dict(self.location, mmsi=mmsi))


@pytest.fixture
def env(monkeypatch):
    schema = f"collecteur_test_{os.getpid()}"
    monkeypatch.setattr("app.collecteur.client.ATTENTE_AVANT_SOLDE_APRES_S", 0)
    store = Store(DSN, schema)
    store.appliquer_ddl()
    cfg = Config(schema=schema)
    journal = JournalDB(DSN, schema)
    yield cfg, store, journal
    journal.fermer()
    store.supprimer_schema()
    store.fermer()


def client(cfg, store, journal, fake, plafond=1000, session="S1"):
    return ClientDD(cfg, "cle-test", journal, session, plafond, store=store,
                    transport=httpx.MockTransport(fake), dormir=lambda s: None)


def scheduler(cfg, store, journal, fake, session="S1", plafond=1000):
    return Scheduler(cfg, client(cfg, store, journal, fake, plafond, session), store, EtatDB(store, cfg), journal,
                     log=lambda m: None, dormir=lambda s: None)


# --------------------------------------------------------------------------- classification
def test_types():
    try:                                                             # référence : dépôt de modélisation seulement
        from app.ingestion.validators import VESSEL_TYPE_MAPPING as REFERENCE
        assert VESSEL_TYPE_MAPPING == REFERENCE                      # la copie autonome n'a pas dérivé
    except ModuleNotFoundError:
        pass
    assert classer("Container Ship")["categorie"] == "LINER"
    assert classer("Bulk Carrier")["categorie"] == "VRAC"
    for t in ("Tug", "Motor Hopper", "Pilot", "Fishing vessel", "Pleasure craft", "Hopper Dredger", "Crane Ship"):
        assert not classer(t)["commerce"], t
    assert not classer("Unknown", 27)["commerce"] and classer("Unknown", 120)["commerce"]


# --------------------------------------------------------------------------- brut + crédits
def test_roster_brut_complet_et_credits_exacts(env):
    cfg, store, journal = env
    fake = FakeDD(solde=1000)
    scheduler(cfg, store, journal, fake).roster("expected")
    assert fake.appels == ["port-calls-by-port"]
    raw = store._q("select * from api_raw", tous=True)
    assert len(raw) == 1 and raw[0]["corps"] == charger(EXPECTED) and raw[0]["credits_depenses"] == 50
    cj = store._q("select * from credits_journal", un=True)
    assert (cj["statut"], cj["solde_avant"], cj["solde_apres"], cj["depense"]) == ("recue", 1000, 950, 50)
    assert journal.totaux("S1")["session"] == 50
    n_liner = sum(classer(x["shipType"])["categorie"] == "LINER" for x in charger(EXPECTED)["expected"]["list"])
    assert store.compter("escales") == n_liner and store.compter("vessels") == 48


def test_positions_dedoublonnees_et_etat_courant(env):
    cfg, store, journal = env
    fake = FakeDD()
    m = fake.location["mmsi"]
    e = store.creer_escale(m, None)
    store.commit()
    sch = scheduler(cfg, store, journal, fake)
    sch.suivre(store.escales_ouvertes()[0], 0)
    sch.suivre(store.escales_ouvertes()[0], 0)                        # même positionReceived
    sch.etat.sauver()
    assert store.compter("positions") == 1
    assert sch.compteurs["positions_nouvelles"] == 1 and sch.compteurs["positions_doublons"] == 1
    v = store._q("select * from etat_courant", un=True)
    assert v["mmsi"] == m and v["escale_id"] == e["escale_id"] and v["next_due_at"] is not None


# --------------------------------------------------------------------------- garde-fou
def test_refus_plafond_session_sans_emission(env):
    cfg, store, journal = env
    fake = FakeDD()
    c = client(cfg, store, journal, fake, plafond=50)
    c.appeler("port-calls-by-port", {"page": 1})
    with pytest.raises(RefusBudget) as r:
        c.appeler("get-vessel-location", {"imo_or_mmsi": "1"})
    assert r.value.portee == "session" and fake.appels == ["port-calls-by-port"]


def test_plafond_jour_lu_en_base(env):
    cfg, store, journal = env
    cfg.plafonds = Plafonds(mois=28000, jour=55, tick=1000)
    fake = FakeDD()
    client(cfg, store, journal, fake, session="A").appeler("port-calls-by-port", {"page": 1})
    # une AUTRE passe (autre session) voit la dépense du jour en base
    with pytest.raises(RefusBudget) as r:
        client(cfg, store, journal, fake, session="B").appeler("get-vessels-by-area", {})
    assert r.value.portee == "jour" and len(fake.appels) == 1


def test_perdue_jamais_reessayee_et_comptee(env):
    cfg, store, journal = env
    fake = FakeDD()
    fake.erreurs = [(httpx.ReadTimeout, True)]
    res = client(cfg, store, journal, fake).appeler("port-calls-by-port", {"page": 1})
    assert res.statut == "perdue" and fake.appels == ["port-calls-by-port"] and res.depense == 50
    assert journal.totaux("S1")["session"] == 50


def test_non_emise_reessayee(env):
    cfg, store, journal = env
    fake = FakeDD()
    fake.erreurs = [(httpx.ConnectError, False), (httpx.ConnectError, False)]
    res = client(cfg, store, journal, fake).appeler("get-vessel-location", {"imo_or_mmsi": "1"})
    assert res.statut == "recue" and len(fake.appels) == 3 and journal.totaux("S1")["session"] == 1


def test_debit_anormal_arrete(env):
    """Débit lu > coût d'un appel (consommation concurrente, recharge…) : compté au coût NORMAL, alerte."""
    cfg, store, journal = env
    fake = FakeDD(debit={"get-vessel-location": 5, "port-calls-by-port": 50, "get-vessels-by-area": 10})
    with pytest.raises(ArretCollecte, match="ANOMALIE de solde.*débit lu 5.*coût normal 1"):
        client(cfg, store, journal, fake).appeler("get-vessel-location", {"imo_or_mmsi": "1"})
    cj = store._q("select depense, erreur from credits_journal order by id desc limit 1", un=True)
    assert cj["depense"] == 1 and "ANOMALIE" in cj["erreur"]


def test_expiration_solde_negatif_comptee_au_cout_normal(env):
    """Cas réel du 16/11/2026 : solde 29 006 -> −5 à l'expiration de l'abonnement. Pas une dépense de
    29 011 crédits : appel compté 1, alerte, et le garde-fou ne voit pas le plafond mensuel franchi."""
    cfg, store, journal = env
    fake = FakeDD(solde=29006, debit={"get-vessel-location": 29011, "port-calls-by-port": 50, "get-vessels-by-area": 10})
    with pytest.raises(ArretCollecte, match="après -5"):
        client(cfg, store, journal, fake).appeler("get-vessel-location", {"imo_or_mmsi": "1"})
    assert journal.totaux()["mois"] == 1


def test_solde_indeterminable_arrete_sans_appel(env):
    cfg, store, journal = env
    fake = FakeDD()
    fake.solde_ko = True
    with pytest.raises(ArretCollecte, match="solde indéterminable"):
        client(cfg, store, journal, fake).appeler("get-vessel-location", {"imo_or_mmsi": "1"})
    assert fake.appels == []


def test_lance_sans_termine_compte_au_cout_max(env):
    cfg, store, journal = env
    journal.ajouter({"evt": "lance", "ts": maintenant().isoformat(), "appel_id": "x", "session": "S",
                     "endpoint": "port-calls-by-port", "cout_max": 50})
    t = journal.totaux("S")
    assert t["session"] == 50 and t["jour"] == 50 and t["incertains"] == 1


# --------------------------------------------------------------------------- verrou
def test_verrou_consultatif_anti_chevauchement():
    v1, v2 = VerrouDB(DSN, cle=987654), VerrouDB(DSN, cle=987654)
    assert v1.prendre()
    assert not v2.prendre()                                           # passe concurrente : sautée
    v1.rendre()
    assert v2.prendre()
    v2.rendre()
    # processus tué = connexion fermée sans unlock : le serveur libère le verrou
    v3 = VerrouDB(DSN, cle=987654)
    assert v3.prendre()
    v3.cx.close()
    v3.cx = None
    v4 = VerrouDB(DSN, cle=987654)
    assert v4.prendre()
    v4.rendre()


# --------------------------------------------------------------------------- tick
def test_tick_estimation_n_appelle_ni_n_ecrit_rien(env):
    cfg, store, journal = env
    for i in range(3):
        store.creer_escale(str(700 + i), None)
    store.commit()
    sch = Scheduler(cfg, None, store, EtatDB(store, cfg), journal)    # client None : tout appel planterait
    r = sch.tick(executer=False)
    assert r["boucles_dues"] == ["decouverte", "expected", "arrival"] and r["location_dus"] == 3
    assert r["cout_estime_passe"] == 50 + 50 + 10 + 3
    assert store.compter("suivi") == 0 and store.compter("horloges") == 0


def test_tick_complet_puis_rejoue(env):
    cfg, store, journal = env
    fake = FakeDD(solde=5000)
    r = scheduler(cfg, store, journal, fake, session="T1").tick(executer=True)
    n1 = len(fake.appels)
    assert fake.appels[:3] == ["get-vessels-by-area", "port-calls-by-port", "port-calls-by-port"]
    assert r["compteurs"]["par_statut"]["recue"] == n1
    assert store.compter("horloges") >= 3
    s = store._q("select count(*) n, count(next_due_at) d, min(next_due_at) m from suivi "
                 "join escales using (escale_id) where statut in ('approaching','anchored','berthed')", un=True)
    assert s["n"] == s["d"] and s["m"] > maintenant()                # toutes les échéances dans le futur
    assert journal.totaux("T1")["session"] == 5000 - fake.solde      # journal = écart de solde
    # passe REJOUÉE aussitôt, nouvelle instance (état relu en base) : rien n'est dû -> 0 appel
    avant = {t: store.compter(t) for t in ("positions", "escales", "evenements", "api_raw")}
    r2 = scheduler(cfg, store, journal, fake, session="T2").tick(executer=True)
    assert len(fake.appels) == n1 and r2["compteurs"]["appels"] == 0
    assert avant == {t: store.compter(t) for t in ("positions", "escales", "evenements", "api_raw")}


def test_tick_respecte_le_temps_imparti(env):
    cfg, store, journal = env
    cfg.duree_tick_s = 0                                             # plus de temps : aucun appel location
    for i in range(3):
        store.creer_escale(str(800 + i), None)
    for nom in ("expected", "arrival", "decouverte"):
        store.ecrire_horloge(nom, maintenant().isoformat())
    store.commit()
    fake = FakeDD()
    r = scheduler(cfg, store, journal, fake).tick(executer=True)
    assert fake.appels == [] and "temps de passe" in r["motif"]


def test_rejeu_api_raw_idempotent(env, monkeypatch):
    cfg, store, journal = env
    fake = FakeDD(solde=5000)
    scheduler(cfg, store, journal, fake).tick(executer=True, max_appels=5)
    monkeypatch.setenv("COLLECTOR_DB_DSN", DSN)
    assert cli.cmd_rejouer(None, cfg) == 0


def test_plan_borne_haute(env):
    cfg, store, journal = env
    for i in range(3):
        store.creer_escale(str(100 + i), None)
    store.commit()
    p = plan(cfg, store.escales_ouvertes(), Etat(), 2.0, 0, expected=False, arrival=False, decouverte=False)
    assert p["appels_location"] == 3 * (120 // 12)                    # phase inconnue -> cadence de rafale
