"""Cycle de vie d'une escale (0 crédit, pas d'API) : fiches get-vessel-location SYNTHÉTIQUES
au format réel, traitées par traiter_location dans un schéma jetable."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.collecteur.boucles import Etat, cadence_phase, traiter_location, traiter_port_calls
from app.collecteur.config import NIVEAUX, Config
from app.collecteur.scheduler import Scheduler, eligible, projection
from app.collecteur.garde import JournalDB
from app.collecteur.store import Store

DSN = os.getenv("COLLECTOR_DB_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="COLLECTOR_DB_DSN absente")
T0 = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
# latitudes au méridien du port (33.6 N, -7.6167) : distance ≈ (lat − 33.6) × 111 km
LOIN, PROCHE, ZONE, QUAI = 37.0, 34.5, 33.78, 33.602


def dd(t: datetime) -> str:
    return t.strftime("%b %d, %Y %H:%M UTC")


def fiche(mmsi, lat, ns, sog, pr, type_="Container Ship", dest="Casablanca Morocco", last=None, atd=None):
    return {"name": f"N{mmsi}", "imo": f"9{mmsi}", "mmsi": mmsi, "latitude": str(lat), "longitude": "-7.6167",
            "speed": str(sog), "course": "180", "heading": "180", "navigationalStatus": ns, "destination": dest,
            "unlocodeDestination": "MACAS", "positionReceived": dd(pr), "updateTime": dd(pr),
            "typeSpecific": type_, "dataSource": "Terrestrial", "draught": "9.6 m.",
            "unlocodeLastport": last, "atdUtc": dd(atd) if atd else None, "etaUtc": dd(pr)}


@pytest.fixture
def env(tmp_path):
    schema = f"collecteur_cycle_{os.getpid()}"
    store = Store(DSN, schema)
    store.appliquer_ddl()
    cfg = Config(schema=schema)
    yield cfg, store, Etat()
    store.supprimer_schema()
    store.fermer()


def suivre(cfg, store, etat, mmsi, f, t):
    e = next(x for x in store.escales_ouvertes() if x["mmsi"] == mmsi) if store.escale_ouverte(mmsi) else None
    return traiter_location(store, etat, cfg, e, f, t)


def test_cycle_liner_complet(env):
    cfg, store, etat = env
    m = "111"
    store.creer_escale(m, None)
    store.commit()
    seq = [  # (lat, ns, sog, minutes) -> (statut, phase, cadence)
        (LOIN, "Underway using Engine", 14, 0, ("approaching", "approche", 60)),
        (PROCHE, "Underway using Engine", 13, 400, ("approaching", "approche", 20)),
        (ZONE, "Underway using Engine", 8, 900, ("approaching", "mouvement", 12)),
        (ZONE, "At Anchor", 0.1, 912, ("anchored", "rade_stable", 30)),
        (ZONE, "At Anchor", 0.2, 1500, ("anchored", "rade_stable", 30)),
        (ZONE, "Underway using Engine", 5, 1620, ("anchored", "mouvement", 12)),
        (QUAI, "Moored", 0.0, 1632, ("berthed", "quai", 60)),
        (QUAI, "Moored", 0.0, 2400, ("berthed", "quai", 60)),
        (QUAI + 0.01, "Underway using Engine", 4, 2520, ("anchored", "mouvement", 12)),
        (34.2, "Underway using Engine", 12, 2600, ("anchored", "hors_zone", 60)),
    ]
    for lat, ns, sog, mn, attendu in seq:
        t = T0 + timedelta(minutes=mn)
        s = suivre(cfg, store, etat, m, fiche(m, lat, ns, sog, t - timedelta(minutes=2)), t)
        assert (s["statut"], s["phase"], s["cadence_min"]) == attendu, (mn, s)
    ev = {r["type"]: r for r in store._q("select * from evenements", tous=True)}
    assert set(ev) == {"mouillage", "sortie_mouillage", "accostage", "sortie_quai", "entree_port", "sortie_port"}
    acc = ev["accostage"]["signaux"]
    assert acc["observe"] and acc["fourchette_appel_min"] == 12 and acc["etat_avant"] == "zone_mouvement"
    assert ev["accostage"]["confiance"] == "rafale"
    e = store._q("select * from escales", un=True)
    assert e["berth_entry_at"] == T0 + timedelta(minutes=1630) and e["anchorage_entry_at"] is not None
    # confirmation du départ : lastPort = MACAS + atdUtc -> autorité
    t = T0 + timedelta(minutes=2660)
    atd = T0 + timedelta(minutes=2530)
    s = suivre(cfg, store, etat, m, fiche(m, 34.5, "Underway using Engine", 13, t, last="MACAS", atd=atd), t)
    assert s["statut"] == "departed"
    app = store._q("select * from evenements where type = 'appareillage'", un=True)
    assert app["confiance"] == "autorite" and app["timestamp_utc"] == atd
    assert store._q("select departure_at from escales", un=True)["departure_at"] == atd


def test_depart_de_repli_sans_atd(env):
    cfg, store, etat = env
    m = "222"
    store.creer_escale(m, None)
    store.commit()
    t = T0
    suivre(cfg, store, etat, m, fiche(m, QUAI, "Moored", 0, t), t)
    for i, lat in enumerate((34.2, 34.5, 34.8, 35.0)):           # hors zone (> 50 km), s'éloigne, pas d'atd
        t += timedelta(minutes=60)
        s = suivre(cfg, store, etat, m, fiche(m, lat, "Underway using Engine", 13, t), t)
    assert s["statut"] == "departed"
    app = store._q("select * from evenements where type = 'appareillage'", un=True)
    assert app["confiance"] == "basse" and app["timestamp_utc"] == T0 + timedelta(minutes=60)


def test_non_liner_pas_d_approche(env):
    cfg, store, etat = env
    corps = {"expected": {"pages": "1", "list": [
        {"mmsi": "301", "imo": "1", "name": "VRAQ", "shipType": "Bulk Carrier", "etaUtc": dd(T0), "length": "190"},
        {"mmsi": "302", "imo": "2", "name": "BOX", "shipType": "Container Ship", "etaUtc": dd(T0), "length": "300"},
        {"mmsi": "303", "imo": "3", "name": "TUG", "shipType": "Tug", "etaUtc": dd(T0), "length": "27"}]}}
    s = traiter_port_calls(store, corps, "expected", T0)
    assert s["escales_creees"] == 1 and store.escale_ouverte("302") and not store.escale_ouverte("301")
    # un vraquier amorcé par la découverte mais trouvé au large -> hors_perimetre
    store.creer_escale("301", None)
    store.commit()
    s = suivre(cfg, store, etat, "301", fiche("301", LOIN, "Underway using Engine", 12, T0, "Bulk Carrier"), T0)
    assert s["statut"] == "hors_perimetre"
    # un vraquier en zone -> suivi in-port
    store.creer_escale("304", None)
    store.commit()
    s = suivre(cfg, store, etat, "304", fiche("304", ZONE, "At Anchor", 0, T0, "Bulk Carrier"), T0)
    assert (s["statut"], s["phase"], s["cadence_min"]) == ("anchored", "rade_stable", 30)


def test_arrival_fait_autorite(env):
    cfg, store, _ = env
    corps = {"arrival": {"pages": "6", "list": [
        {"mmsi": "401", "imo": "4", "name": "A", "shipType": "General Cargo Ship", "etaUtc": dd(T0), "length": "120"},
        {"mmsi": "402", "imo": "5", "name": "B", "shipType": "Bulk Carrier",
         "etaUtc": dd(T0 - timedelta(days=10)), "length": "190"}]}}
    traiter_port_calls(store, corps, "arrival", T0 + timedelta(hours=2))
    traiter_port_calls(store, corps, "arrival", T0 + timedelta(hours=30))       # relecture : idempotent
    ev = store._q("select * from evenements", tous=True)
    assert len(ev) == 1 and ev[0]["type"] == "arrivee" and ev[0]["confiance"] == "autorite"
    assert store.escale_ouverte("401")["arrivee_utc"] == T0 and store.escale_ouverte("402") is None


def test_rafale_bornee(env):
    cfg, store, etat = env
    m = "501"
    store.creer_escale(m, None)
    store.commit()
    t = T0
    suivre(cfg, store, etat, m, fiche(m, ZONE, "At Anchor", 0, t), t)
    for mn in (120, 132, 144):
        t = T0 + timedelta(minutes=mn)
        s = suivre(cfg, store, etat, m, fiche(m, ZONE + mn / 10000, "Underway using Engine", 1.8, t), t)
        assert s["cadence_min"] == 12
    t = T0 + timedelta(minutes=120 + 200)                        # > 3 h de dérive : mouvement_lent
    s = suivre(cfg, store, etat, m, fiche(m, ZONE + 0.05, "Underway using Engine", 1.5, t), t)
    assert s["phase"] == "mouvement" and s["cadence_min"] == cfg.cadences.mouvement_lent


def test_echelle_degradation_priorisee():
    # 1) sweeps in-port d'abord, 2) approche au large ensuite, 3) approche finale en dernier
    premier = {k: next(i for i, n in enumerate(NIVEAUX) if n[k] != NIVEAUX[0][k]) for k in NIVEAUX[0]}
    assert premier["quai"] <= premier["rade"] < premier["loin"] < premier["proche"]
    assert premier["rafale"] < premier["loin"]
    assert all(10 <= n["rafale"] <= 15 for n in NIVEAUX)
    for a, b in zip(NIVEAUX, NIVEAUX[1:]):
        assert all(b[k] >= a[k] for k in a)                      # monotone


def test_projection_degrade(env, tmp_path):
    cfg, store, etat = env
    for i in range(60):                                         # 60 navires stables en zone
        e = store.creer_escale(str(600 + i), None, "anchored")
        etat.escale(e["escale_id"]).update(phase="rade_stable", en_zone=True, dernier_dist=10.0)
    store.commit()
    j = JournalDB(DSN, cfg.schema)
    p = projection(cfg, j, [dict(e, categorie="VRAC", min_dist=10.0) for e in store.escales_ouvertes()], etat,
                   t=datetime(2026, 11, 1, 0, 5, tzinfo=timezone.utc))
    # 60 × 1440/30 = 2 880/j : au-dessus du plafond jour -> élargir les sweeps (niveau >= 1), approche finale intacte
    assert p["niveau"] >= 1 and NIVEAUX[p["niveau"]]["proche"] == NIVEAUX[0]["proche"]


def test_arrivee_d_une_visite_precedente_ignoree(env):
    """Liner hebdomadaire : la liste arrival contient aussi l'arrivée de la semaine passée."""
    cfg, store, _ = env
    eta = T0 + timedelta(hours=20)
    corps_exp = {"expected": {"pages": "1", "list": [
        {"mmsi": "901", "imo": "9", "name": "HEBDO", "shipType": "Container Ship", "etaUtc": dd(eta), "length": "200"}]}}
    traiter_port_calls(store, corps_exp, "expected", T0)
    semaine_passee, cette_fois, quai = T0 - timedelta(days=7), T0 + timedelta(hours=22), T0 + timedelta(hours=30)
    corps_arr = {"arrival": {"pages": "6", "list": [
        {"mmsi": "901", "imo": "9", "name": "HEBDO", "shipType": "Container Ship", "etaUtc": dd(t), "length": "200"}
        for t in (quai, cette_fois, semaine_passee)]}}
    traiter_port_calls(store, corps_arr, "arrival", T0 + timedelta(hours=31))
    e = store.escale_ouverte("901")
    assert e["arrivee_utc"] == cette_fois                    # 1re entrée de la chaîne, pas la semaine passée
    ev = store._q("select * from evenements where type = 'arrivee'", tous=True)
    assert len(ev) == 1 and ev[0]["timestamp_utc"] == cette_fois


def test_escale_amorcee_par_arrival_deja_repartie(env):
    """Bootstrap : arrivée constatée il y a 2 j, navire déjà reparti avant l'ouverture du suivi."""
    cfg, store, etat = env
    arr = T0 - timedelta(days=2)
    corps = {"arrival": {"pages": "6", "list": [
        {"mmsi": "951", "imo": "1", "name": "PARTI", "shipType": "Container Ship", "etaUtc": dd(arr), "length": "200"},
        {"mmsi": "952", "imo": "2", "name": "AILLEURS", "shipType": "Container Ship", "etaUtc": dd(arr),
         "length": "200"}]}}
    traiter_port_calls(store, corps, "arrival", T0)
    atd = arr + timedelta(hours=30)                                 # après l'arrivée, avant l'ouverture
    s = suivre(cfg, store, etat, "951", fiche("951", LOIN, "Underway using Engine", 14, T0, dest="Dakar",
                                              last="MACAS", atd=atd), T0)
    assert s["statut"] == "departed"
    assert store._q("select departure_at from escales where mmsi = '951'", un=True)["departure_at"] == atd
    s = suivre(cfg, store, etat, "952", fiche("952", LOIN, "Underway using Engine", 14, T0, dest="Valencia",
                                              last="ESVLC", atd=T0 - timedelta(hours=3)), T0)
    assert s["statut"] == "departed"                               # reparti, heure inconnue
    assert store._q("select departure_at from escales where mmsi = '952'", un=True)["departure_at"] is None
    assert store._q("select count(*) n from evenements where type = 'appareillage'", un=True)["n"] == 1


def test_decouverte_ne_rouvre_pas_un_navire_tout_juste_parti(env):
    cfg, store, etat = env
    from app.collecteur.boucles import traiter_area
    e = store.creer_escale("961", None, "departed")
    store.maj_escale(e["escale_id"], departure_at=T0 - timedelta(hours=3))
    store.commit()
    zone = {"vessels": [{"mmsi": "961", "name": "SORTANT", "typeSpecific": "Bulk Carrier"}]}
    assert traiter_area(store, zone, T0)["escales_creees"] == 0
    assert traiter_area(store, zone, T0 + timedelta(hours=13))["escales_creees"] == 1   # revenu plus tard


# ---- sorties de quai / de mouillage (comptages d'occupation et de file d'attente) ----------
def a_quai(store, t):
    return store._q("select count(*) n from escales where berth_entry_at < %s "
                    "and (berth_exit_at is null or berth_exit_at >= %s)", (t, t), un=True)["n"]


def en_rade(store, t):
    return store._q("select count(*) n from escales where anchorage_entry_at < %s "
                    "and (anchorage_exit_at is null or anchorage_exit_at >= %s)", (t, t), un=True)["n"]


def test_navire_reparti_plus_compte_a_quai(env):
    """Départ confirmé par atdUtc directement depuis le quai (aucun relevé « en mouvement » entre les deux) :
    la sortie de quai = atdUtc ; le navire n'est plus compté à quai ensuite."""
    cfg, store, etat = env
    m = "211"
    store.creer_escale(m, None)
    store.commit()
    suivre(cfg, store, etat, m, fiche(m, QUAI, "Moored", 0.0, T0), T0)
    t1 = T0 + timedelta(hours=1)
    assert a_quai(store, t1) == 1
    atd = T0 + timedelta(hours=20)
    t = atd + timedelta(minutes=50)
    s = suivre(cfg, store, etat, m, fiche(m, 33.9, "Underway using Engine", 13, t, last="MACAS", atd=atd), t)
    assert s["statut"] == "departed"
    e = store._q("select * from escales where mmsi = %s", (m,), un=True)
    assert e["berth_exit_at"] == atd and e["departure_at"] == atd
    assert a_quai(store, atd - timedelta(minutes=1)) == 1
    assert a_quai(store, atd + timedelta(minutes=1)) == 0


def test_navire_accoste_plus_compte_en_rade(env):
    """Mouillage puis 1er relevé à quai sans relevé intermédiaire en mouvement : sortie du mouillage =
    accostage ; le navire n'est plus compté en rade."""
    cfg, store, etat = env
    m = "212"
    store.creer_escale(m, None)
    store.commit()
    suivre(cfg, store, etat, m, fiche(m, LOIN, "Underway using Engine", 14, T0), T0)
    t_m = T0 + timedelta(hours=30)
    suivre(cfg, store, etat, m, fiche(m, ZONE, "At Anchor", 0.1, t_m), t_m)
    assert en_rade(store, t_m + timedelta(hours=1)) == 1
    t_q = t_m + timedelta(hours=40)
    s = suivre(cfg, store, etat, m, fiche(m, QUAI, "Moored", 0.0, t_q), t_q)
    assert s["statut"] == "berthed"
    e = store._q("select * from escales where mmsi = %s", (m,), un=True)
    assert e["anchorage_exit_at"] == e["berth_entry_at"] <= t_q
    assert en_rade(store, t_q + timedelta(minutes=1)) == 0


def test_sortie_mouillage_observee_non_remplacee(env):
    """Sortie du mouillage vue en mouvement (environ 1 h 30 avant l'accostage) : conservée."""
    cfg, store, etat = env
    m = "213"
    store.creer_escale(m, None)
    store.commit()
    suivre(cfg, store, etat, m, fiche(m, LOIN, "Underway using Engine", 14, T0), T0)
    t_m = T0 + timedelta(hours=30)
    suivre(cfg, store, etat, m, fiche(m, ZONE, "At Anchor", 0.1, t_m), t_m)
    t_b = t_m + timedelta(hours=10)
    suivre(cfg, store, etat, m, fiche(m, ZONE - 0.05, "Underway using Engine", 6, t_b), t_b)
    t_q = t_b + timedelta(minutes=80)
    suivre(cfg, store, etat, m, fiche(m, QUAI, "Moored", 0.0, t_q), t_q)
    e = store._q("select * from escales where mmsi = %s", (m,), un=True)
    assert e["anchorage_exit_at"] == t_b


def test_rattrapage_sorties():
    from app.collecteur.boucles import rattrapage_sorties
    a, b, d = T0, T0 + timedelta(hours=10), T0 + timedelta(hours=30)
    base = {"anchorage_entry_at": None, "anchorage_exit_at": None, "berth_entry_at": None, "berth_exit_at": None,
            "departure_at": None, "statut": "departed"}
    # parti depuis le quai, mouillage avant : sortie de quai = départ, sortie du mouillage = accostage
    assert rattrapage_sorties({**base, "anchorage_entry_at": a, "berth_entry_at": b, "departure_at": d}) == {
        "anchorage_exit_at": b, "berth_exit_at": d}
    # encore à quai : seule la sortie du mouillage est bornée par l'accostage
    assert rattrapage_sorties({**base, "statut": "berthed", "anchorage_entry_at": a, "berth_entry_at": b}) == {
        "anchorage_exit_at": b}
    # parti du mouillage sans accoster : sortie du mouillage = départ
    assert rattrapage_sorties({**base, "anchorage_entry_at": a, "departure_at": d}) == {"anchorage_exit_at": d}
    # parti sans heure de départ : atdUtc relu dans api_raw, sinon rien d'inventé
    assert rattrapage_sorties({**base, "berth_entry_at": b}, d) == {"berth_exit_at": d}
    assert rattrapage_sorties({**base, "berth_entry_at": b}) == {}
    # sortie déjà observée : jamais remplacée
    assert rattrapage_sorties({**base, "berth_entry_at": b, "berth_exit_at": b + timedelta(hours=2),
                               "departure_at": d}) == {}


def test_repositionnement_en_rade_pas_une_sortie(env):
    """Mouvement bref en rade (évitage, changement de poste de mouillage) puis revu au mouillage sans avoir
    accosté : la sortie de mouillage est annulée, le navire reste compté en rade."""
    cfg, store, etat = env
    m = "214"
    store.creer_escale(m, None)
    store.commit()
    suivre(cfg, store, etat, m, fiche(m, LOIN, "Underway using Engine", 14, T0), T0)
    t_m = T0 + timedelta(hours=30)
    suivre(cfg, store, etat, m, fiche(m, ZONE, "At Anchor", 0.1, t_m), t_m)
    t_b = t_m + timedelta(hours=5)
    suivre(cfg, store, etat, m, fiche(m, ZONE - 0.02, "Underway using Engine", 4, t_b), t_b)
    assert store._q("select anchorage_exit_at from escales where mmsi = %s", (m,), un=True)["anchorage_exit_at"] == t_b
    t_r = t_b + timedelta(minutes=40)
    suivre(cfg, store, etat, m, fiche(m, ZONE - 0.02, "At Anchor", 0.1, t_r), t_r)
    assert store._q("select anchorage_exit_at from escales where mmsi = %s", (m,), un=True)["anchorage_exit_at"] is None
    assert en_rade(store, t_r + timedelta(hours=1)) == 1


def test_entree_censuree(env):
    """Indicateur fixé au 1er relevé : vrai si déjà à quai ou déjà au mouillage, faux sinon ; jamais réécrit."""
    cfg, store, etat = env
    for m, lat, ns, sog, attendu in (("221", QUAI, "Moored", 0.0, True), ("222", ZONE, "At Anchor", 0.1, True),
                                     ("223", LOIN, "Underway using Engine", 14, False)):
        store.creer_escale(m, None)
        store.commit()
        suivre(cfg, store, etat, m, fiche(m, lat, ns, sog, T0), T0)
        assert store._q("select entree_censuree from escales where mmsi = %s", (m,), un=True)["entree_censuree"] is attendu
    t = T0 + timedelta(hours=40)
    suivre(cfg, store, etat, "223", fiche("223", QUAI, "Moored", 0.0, t), t)       # accostage observé ensuite
    assert store._q("select entree_censuree from escales where mmsi = '223'", un=True)["entree_censuree"] is False


def test_atd_du_port_precedent_ne_ferme_pas_l_escale(env):
    """Cas XPRESS ATLAS (136) : à quai, Data Docked renvoie lastPort = MACAS avec l'atdUtc du port précédent
    (antérieur à l'accostage) -> l'escale reste à quai ; l'atdUtc à jour, lui, ferme l'escale."""
    cfg, store, etat = env
    m = "136"
    store.creer_escale(m, None)
    store.commit()
    t = T0
    suivre(cfg, store, etat, m, fiche(m, PROCHE, "Underway using Engine", 12, t - timedelta(minutes=2)), t)
    t += timedelta(minutes=600)
    s = suivre(cfg, store, etat, m, fiche(m, QUAI, "Moored", 0.0, t - timedelta(minutes=2)), t)
    assert s["statut"] == "berthed"
    vieux_atd = T0 + timedelta(minutes=10)                # après le début de l'escale, AVANT l'accostage
    t += timedelta(minutes=600)
    s = suivre(cfg, store, etat, m, fiche(m, QUAI, "Moored", 0.0, t - timedelta(minutes=2), last="MACAS", atd=vieux_atd), t)
    assert s["statut"] == "berthed"
    assert store._q("select departure_at from escales", un=True)["departure_at"] is None
    atd = t + timedelta(minutes=30)
    t += timedelta(minutes=90)
    s = suivre(cfg, store, etat, m, fiche(m, 34.5, "Underway using Engine", 13, t, last="MACAS", atd=atd), t)
    assert s["statut"] == "departed"
    assert store._q("select departure_at from escales", un=True)["departure_at"] == atd
