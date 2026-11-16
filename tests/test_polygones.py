"""Polygones, classification polygonale et congestion (parité ANP) — 0 crédit, données synthétiques.

Les tests de géométrie et de congestion sont purs ; ceux de classification utilisent une base
PostgreSQL jetable (COLLECTOR_DB_DSN), sinon ils sont sautés.
"""
import math
import os
import random
from datetime import datetime, timedelta, timezone

import pytest

from app.collecteur import congestion as cong
from app.collecteur import polygones as pg
from app.collecteur.boucles import Etat, traiter_location
from app.collecteur.config import Config
from app.collecteur.store import Store

T0 = datetime(2026, 11, 13, 6, 0, tzinfo=timezone.utc)
RADE = (33.680, -7.640)          # ~10 km au NO du port
QUAI_A = (33.603, -7.605)
QUAI_B = (33.598, -7.583)
TERRE = (33.540, -7.540)         # au SE : côté terre


def nuage(centre, n, rayon_km, ns, sog=0.1, mmsi0=0, graine=1):
    rnd = random.Random(graine)
    la, lo = centre
    pts = []
    for i in range(n):
        a, r = rnd.uniform(0, 2 * math.pi), rayon_km * math.sqrt(rnd.random())
        pts.append({"mmsi": str(mmsi0 + i), "name": f"N{mmsi0 + i}", "nav_status": ns, "sog": sog,
                    "lat": la + r * math.sin(a) / 110.574,
                    "lon": lo + r * math.cos(a) / (111.320 * math.cos(math.radians(la))),
                    "distance_to_port_km": 10.0 if ns == "At Anchor" else 0.5})
    return pts


def jeu():
    return (nuage(RADE, 15, 2.0, "At Anchor", mmsi0=100) + nuage(QUAI_A, 6, 0.15, "Moored", 0, 200, 2)
            + nuage(QUAI_B, 6, 0.15, "Moored", 0, 300, 3) + nuage(TERRE, 4, 0.5, "At Anchor", mmsi0=400, graine=4))


# --------------------------------------------------------------------------- géométrie
def test_geometrie():
    carre = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert pg.enveloppe(carre + [(0.5, 0.5)]) == [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert pg.dans(0.5, 0.5, carre) and not pg.dans(1.5, 0.5, carre)
    assert abs(pg.aire_km2(pg.avec_marge([(0, 0)], 1.0, 64)) - math.pi) < 0.01
    lab = pg.dbscan([(0, 0), (0.1, 0), (0, 0.1), (5, 5), (5.1, 5), (20, 20)], 0.5, 2)
    assert lab[0] == lab[1] == lab[2] != lab[3] == lab[4] and lab[5] == -1
    assert pg.premier_token("Moored | At Anchor") == "Moored"


def test_calibration_et_garde_fou():
    r = pg.calibrer(jeu())
    types = sorted(p["type"] for p in r["polygones"])
    assert types == ["port", "quai", "quai", "rade"]
    assert len(r["rejets"]) == 1 and "terre" in r["rejets"][0]["motif"]          # grappe côté terre écartée
    rade = next(p for p in r["polygones"] if p["type"] == "rade")
    assert rade["n_navires"] == 15 and 8 < rade["dist_seed_km"] < 14
    polys = pg.Polygones()
    for p in r["polygones"]:
        getattr(polys, p["type"]).append(p["km"])
    v = pg.valider([p for p in jeu() if p["mmsi"] < "400"], polys)
    assert v["at_anchor_dans_rade"]["taux"] == 1.0 and v["moored_dans_quai"]["taux"] == 1.0
    assert polys.zone(*RADE) == "rade" and polys.zone(*QUAI_A) == "quai" and polys.zone(34.5, -8.5) == "hors"


# --------------------------------------------------------------------------- congestion (parité)
def test_congestion_parite_anp():
    t = T0
    esc = [
        # mouillage puis quai : rade [m, a[, quai [a, s[
        {"categorie": "LINER", "statut": "departed", "anchorage_entry_at": t, "berth_entry_at": t + timedelta(hours=10),
         "berth_exit_at": t + timedelta(hours=20), "departure_at": t + timedelta(hours=21)},
        # accostage direct : jamais en rade
        {"categorie": "VRAC", "statut": "berthed", "anchorage_entry_at": None, "berth_entry_at": t + timedelta(hours=2)},
        # incohérente (accostage < mouillage) : exclue de la RADE ; son séjour à quai, valide, compte
        # (comme dans le compteur ANP : quai_ok ne dépend pas de la cohérence du mouillage)
        {"categorie": "VRAC", "statut": "departed", "anchorage_entry_at": t + timedelta(hours=5),
         "berth_entry_at": t + timedelta(hours=1), "berth_exit_at": t + timedelta(hours=9)},
        # séjour à quai > 60 j : exclu (plafond)
        {"categorie": "SPECIAL", "statut": "berthed", "berth_entry_at": t - timedelta(days=61)},
        # année hors [2000, 2030] : ignorée
        {"categorie": "VRAC", "statut": "anchored", "anchorage_entry_at": datetime(2323, 1, 1, tzinfo=timezone.utc)},
        # encore au mouillage (causal) : compte jusqu'à son départ / sa sortie
        {"categorie": "VRAC", "statut": "anchored", "anchorage_entry_at": t + timedelta(hours=3)},
    ]
    iv = cong.intervalles(esc, t + timedelta(hours=30))
    assert iv["exclues"]["incoherentes"] == 1 and iv["exclues"]["quai_plus_60j"] == 1
    assert cong.compter(iv, t + timedelta(hours=4)) == {"rade": 2, "rade_liner": 1, "a_quai": 2, "n_total": 4}
    assert cong.compter(iv, t + timedelta(hours=12)) == {"rade": 1, "rade_liner": 0, "a_quai": 2, "n_total": 3}
    assert cong.compter(iv, t + timedelta(hours=25))["a_quai"] == 1        # LINER parti, VRAC direct toujours à quai


def test_creneaux_heure_locale():
    c = cong.creneaux(datetime(2026, 11, 13, 6, 30, tzinfo=timezone.utc), datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc))
    assert [x.hour for x in c] == [8, 9, 10] and str(c[0].tzinfo) == "Africa/Casablanca"


# --------------------------------------------------------------------------- classification (base)
DSN = os.getenv("COLLECTOR_DB_DSN")
avec_base = pytest.mark.skipif(not DSN, reason="COLLECTOR_DB_DSN absente")


@pytest.fixture
def env():
    schema = f"collecteur_poly_{os.getpid()}"
    store = Store(DSN, schema)
    store.appliquer_ddl()
    yield Config(schema=schema), store
    store.supprimer_schema()
    store.fermer()


def polygones_jeu():
    r = pg.calibrer(jeu())
    p = pg.Polygones(version=1)
    for x in r["polygones"]:
        getattr(p, x["type"]).append(x["km"])
    return p


def dd(t):
    return t.strftime("%b %d, %Y %H:%M UTC")


def fiche(m, pos, ns, sog, pr):
    return {"mmsi": m, "imo": "9" + m, "name": "N" + m, "latitude": str(pos[0]), "longitude": str(pos[1]),
            "speed": str(sog), "course": "0", "navigationalStatus": ns, "positionReceived": dd(pr),
            "typeSpecific": "Container Ship", "destination": "Casablanca", "unlocodeDestination": "MACAS"}


def suivre(cfg, store, etat, m, f, t, polys):
    e = next(x for x in store.escales_ouvertes() if x["mmsi"] == m)
    return traiter_location(store, etat, cfg, e, f, t, 0, polys)


@avec_base
def test_cycle_polygonal(env):
    cfg, store = env
    polys, etat, m = polygones_jeu(), Etat(), "501"
    store.creer_escale(m, None)
    store.commit()
    au_large_hors_rade = (33.95, -7.75)           # ~40 km : dans la zone de suivi, hors polygone rade
    seq = [
        ((34.6, -8.2), "Underway using Engine", 13, 0, "approaching"),
        (au_large_hors_rade, "At Anchor", 0.1, 60, "approaching"),     # dérive d'attente : PAS en rade
        (RADE, "At Anchor", 0.1, 180, "anchored"),                      # entrée au mouillage
        ((33.66, -7.63), "Underway using Engine", 6, 600, "anchored"),  # sortie de mouillage -> rafale
        (QUAI_A, "Moored", 0.0, 612, "berthed"),                        # accostage + entrée port
        ((33.61, -7.60), "Underway using Engine", 3, 900, "anchored"),  # sortie de quai (changement de poste)
        (QUAI_B, "Moored", 0.0, 912, "berthed"),                        # 2e poste : 1er accostage conservé
        ((33.70, -7.60), "Underway using Engine", 10, 1200, "anchored"),  # dernière sortie de quai, sortie port
    ]
    for pos, ns, sog, mn, statut in seq:
        t = T0 + timedelta(minutes=mn)
        s = suivre(cfg, store, etat, m, fiche(m, pos, ns, sog, t), t, polys)
        assert s["statut"] == statut, (mn, s)
    e = store._q("select * from escales", un=True)
    assert e["anchorage_entry_at"] == T0 + timedelta(minutes=180)
    assert e["berth_entry_at"] == T0 + timedelta(minutes=612)               # 1er accostage
    assert e["berth_exit_at"] == T0 + timedelta(minutes=1200)               # DERNIÈRE sortie de quai
    assert e["port_entry_at"] == T0 + timedelta(minutes=612)
    ev = {r["type"]: r for r in store._q("select * from evenements", tous=True)}
    assert {"mouillage", "sortie_mouillage", "accostage", "entree_port", "sortie_quai", "sortie_port"} <= set(ev)
    assert ev["accostage"]["confiance"] == "rafale" and ev["accostage"]["signaux"]["fourchette_appel_min"] == 12
    assert ev["sortie_quai"]["timestamp_utc"] == T0 + timedelta(minutes=1200)


@avec_base
def test_deja_a_quai_et_multi_token(env):
    cfg, store = env
    polys, etat, m = polygones_jeu(), Etat(), "502"
    store.creer_escale(m, None)
    store.commit()
    s = suivre(cfg, store, etat, m, fiche(m, QUAI_B, "Moored | Underway", 0.0, T0), T0, polys)   # 1re valeur
    assert s["statut"] == "berthed"
    ev = store._q("select * from evenements where type = 'accostage'", un=True)
    assert ev["confiance"] == "non_observe" and ev["signaux"]["observe"] is False
    assert store._q("select berth_entry_at from escales", un=True)["berth_entry_at"] == T0   # pour a_quai


@avec_base
def test_polygones_en_base_et_congestion(env):
    cfg, store = env
    r = pg.calibrer(jeu())
    for i, p in enumerate(r["polygones"]):
        store.inserer_polygone(1, p["type"], f"{p['type']}_{i}", pg.geojson(p["km"]), p["n_points"], p["n_navires"],
                               p["aire_km2"], {})
    store.activer_polygones(1)
    store.commit()
    relu = pg.Polygones.depuis_lignes(store.polygones_actifs())
    assert relu.version == 1 and relu.zone(*RADE) == "rade" and relu.zone(*QUAI_A) == "quai"
    e = store.creer_escale("503", None, "anchored")
    store.maj_escale(e["escale_id"], anchorage_entry_at=T0)
    store.commit()
    n = cong.recalculer(store, T0, T0 + timedelta(hours=2))
    rows = store._q("select * from congestion_horaire order by hour_slot_casa", tous=True)
    assert n == len(rows) == 3 and all(x["rade"] == 1 and x["derivation_version"] == cong.VERSION for x in rows)


MOHAMMEDIA = (33.745, -7.350)          # mouillage de Mohammedia, ~25 km au NE, dans le rayon de 50 km


def test_calibration_exclut_le_mouillage_d_un_autre_port():
    autres = nuage(MOHAMMEDIA, 8, 1.0, "At Anchor", mmsi0=600, graine=6)
    for p in autres:
        p["destination"] = "Mohammedia anch Morocco"
    casa = jeu()
    for p in casa:
        p["destination"] = "Casablanca anch Morocco" if p["nav_status"] == "At Anchor" else "MA CAS"
    r = pg.calibrer(casa + autres)
    rades = [p for p in r["polygones"] if p["type"] == "rade"]
    assert len(rades) == 1 and rades[0]["n_navires"] == 15                     # seule la rade de Casablanca
    assert any("autre que Casablanca" in j["motif"] and j["n_navires"] == 8 for j in r["rejets"])


@avec_base
def test_navire_vers_un_autre_port_hors_perimetre(env):
    cfg, store = env
    polys, etat, m = polygones_jeu(), Etat(), "504"
    store.creer_escale(m, None)
    store.commit()
    f = fiche(m, MOHAMMEDIA, "At Anchor", 0.1, T0)
    f.update(destination="Mohammedia anch Morocco", unlocodeDestination=None, typeSpecific="Bulk Carrier")
    s = suivre(cfg, store, etat, m, f, T0, polys)
    assert s["statut"] == "hors_perimetre" and "autre port" in s["motif"]


@avec_base
def test_statuts_herites_sans_heure_d_entree(env):
    """Escales passées « anchored » / « berthed » par les anciennes règles, sans heure d'entrée :
    l'heure manquante est posée (borne haute = 1re observation), sans fausse entrée au port."""
    cfg, store = env
    polys = polygones_jeu()
    for m, statut, pos, ns in (("601", "anchored", RADE, "At Anchor"), ("602", "berthed", QUAI_A, "Moored")):
        e = store.creer_escale(m, None, statut)
        store.maj_escale(e["escale_id"], premiere_obs=T0 - timedelta(hours=5), derniere_obs=T0 - timedelta(hours=2))
        store.commit()
        etat = Etat()
        etat.escale(e["escale_id"])["prec"] = {"pr": (T0 - timedelta(hours=2)).isoformat(),
                                               "appel": (T0 - timedelta(hours=2)).isoformat(), "etat": "quai"}
        suivre(cfg, store, etat, m, fiche(m, pos, ns, 0.0, T0), T0, polys)
    r = {x["mmsi"]: x for x in store._q("select * from escales", tous=True)}
    assert r["601"]["anchorage_entry_at"] == T0 - timedelta(hours=5)
    assert r["602"]["berth_entry_at"] == T0 - timedelta(hours=5)
    assert store._q("select count(*) n from evenements where type = 'entree_port'", un=True)["n"] == 0
    assert {x["confiance"] for x in store._q("select * from evenements", tous=True)} == {"non_observe"}


@avec_base
def test_cadence_diagnostic_constante_puis_levee(env):
    """Escale marquée : 15 min à toute distance (au large comme en approche finale), levée à l'accostage."""
    cfg, store = env
    polys, etat, m = polygones_jeu(), Etat(), "701"
    e = store.creer_escale(m, None)
    store.commit()
    loc = etat.escale(e["escale_id"])
    loc.update(diag_cadence_min=15, diag_debut=T0.isoformat(), diag_jusqu_au=(T0 + timedelta(days=3)).isoformat())
    for i, (pos, ns, sog) in enumerate((((36.5, -9.5), "Underway using Engine", 14), ((34.4, -7.9), "Underway using Engine", 12),
                                        (RADE, "At Anchor", 0.1))):
        t = T0 + timedelta(minutes=15 * i)
        s = suivre(cfg, store, etat, m, fiche(m, pos, ns, sog, t), t, polys)
        assert s["cadence_min"] == 15, (i, s)
    t = T0 + timedelta(minutes=60)
    s = suivre(cfg, store, etat, m, fiche(m, QUAI_A, "Moored", 0.0, t), t, polys)
    assert s["statut"] == "berthed" and s["cadence_min"] != 15 and "diag_cadence_min" not in loc
    assert loc["diag_termine"]["motif"] == "berthed"


@avec_base
def test_reponse_sans_destination_n_abandonne_pas(env):
    """Réponse sans champs statiques au large : l'approche continue ; une destination explicite vers
    un autre port n'abandonne qu'à la 2e réponse consécutive."""
    cfg, store = env
    polys, etat, m = polygones_jeu(), Etat(), "702"
    store.creer_escale(m, None)
    store.commit()
    loin = (36.27, -3.60)                                   # ~600 km, Alboran
    f = fiche(m, loin, "Underway using Engine", 12.6, T0)
    s = suivre(cfg, store, etat, m, f, T0, polys)
    assert s["statut"] == "approaching"
    t = T0 + timedelta(minutes=20)
    vide = {**fiche(m, (36.26, -3.68), "Underway using Engine", 12.6, t), "destination": None,
            "unlocodeDestination": None}
    s = suivre(cfg, store, etat, m, vide, t, polys)
    assert s["statut"] == "approaching", s
    for i, attendu in ((2, "approaching"), (3, "abandoned")):
        t = T0 + timedelta(minutes=20 * i)
        autre = {**fiche(m, (36.25, -3.70 - 0.05 * i), "Underway using Engine", 12.6, t), "destination": "Valencia",
                 "unlocodeDestination": "ESVLC"}
        s = suivre(cfg, store, etat, m, autre, t, polys)
        assert s["statut"] == attendu, (i, s)


def test_dus_denses_cadence_infra_passe():
    """Escales à cadence infra-passe : dues quand dernier appel + cadence est échu."""
    from app.collecteur.scheduler import dus_denses

    class E:
        d = {"escales": {"1": {"diag_cadence_s": 60, "dernier_appel": T0.isoformat()},
                         "2": {"diag_cadence_s": 60, "dernier_appel": (T0 + timedelta(seconds=30)).isoformat()},
                         "3": {"cadence_min": 20}}}
    dus, prochaine = dus_denses([{"escale_id": 1}, {"escale_id": 2}, {"escale_id": 3}], E(), T0 + timedelta(seconds=61))
    assert [e["escale_id"] for e in dus] == [1] and prochaine == T0 + timedelta(seconds=90)
