"""CLI du collecteur temps réel Data Docked (cron sans serveur : GitHub Actions + Supabase).

  python -m app.collecteur tick                      ESTIMATION d'une passe (lecture seule, 0 appel)
  python -m app.collecteur tick --executer           UNE passe réelle (mode cron)
                               [--max-appels K]      borne le nombre d'appels location de la passe
  python -m app.collecteur estimer [--heures 24]     plan, niveaux, projection jour / mois (0 appel)
  python -m app.collecteur precision                 fourchettes d'accostage (médiane, p90)
  python -m app.collecteur rejouer                   retraite api_raw (idempotence, 0 crédit)
  python -m app.collecteur etat                      dépenses, escales, positions, suivi
  python -m app.collecteur ddl [--retention]         applique le schéma (+ job pg_cron de rétention)
  python -m app.collecteur polygones calibrer [--activer] [--export F.geojson]
                                                     dérive RADE / QUAI / PORT des positions en base
  python -m app.collecteur polygones valider [--version N]   cohérence At Anchor / Moored
  python -m app.collecteur polygones activer N        active une version de polygones
  python -m app.collecteur congestion --depuis AAAA-MM-JJ    recalcule congestion_horaire (parité ANP)
  python -m app.collecteur couverture [--depuis AAAA-MM-JJ]  fraîcheur des positions par tranche de distance
                                                     (escales du diagnostic de couverture, 0 crédit)

MODE CRON : pas de confirmation humaine par passe. L'autorisation permanente, ce sont les
plafonds (jour COLLECTOR_PLAFOND_JOUR=950, mois COLLECTOR_PLAFOND_MOIS=28000, passe
COLLECTOR_PLAFOND_TICK=150), vérifiés AVANT CHAQUE appel sur le journal en base. Sans
--executer, rien n'est dépensé.

Variables : DATADOCKED_API_KEY ; COLLECTOR_DB_DSN ou SUPABASE_DB_DSN.
Code de sortie : 0 passe faite ou sautée (verrou tenu) ; 2 arrêt de sûreté (solde illisible,
débit anormal, base injoignable) — visible en échec dans GitHub Actions.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import os
import statistics
import sys
from collections import Counter

from .boucles import Etat, fiche_location, traiter_area, traiter_location, traiter_port_calls, txt
from .client import ClientDD
from . import congestion as cong
from . import polygones as pg
from .config import COUT, Config, cle_api, dsn
from .garde import ArretCollecte, JournalDB, VerrouDB, maintenant
from .scheduler import PROVISION_RAFALES_JOUR, EtatDB, Scheduler, eligible, plan, projection
from .store import Store


def _ctx(cfg: Config):
    d = dsn()
    store = Store(d, cfg.schema)
    return store, EtatDB(store, cfg), JournalDB(d, cfg.schema)


def _eligibles(cfg, store, etat):
    ouvertes = store.escales_ouvertes()
    etat.charger(e["escale_id"] for e in ouvertes)
    return [e for e in ouvertes if eligible(cfg, e, etat.d["escales"].get(str(e["escale_id"]), {}))]


def _resume_github(texte: str) -> None:
    chemin = os.getenv("GITHUB_STEP_SUMMARY")
    if chemin:
        with open(chemin, "a", encoding="utf-8") as f:
            f.write(texte + "\n")


# ------------------------------------------------------------------------------- tick
def cmd_tick(args, cfg) -> int:
    if not args.executer:
        store, etat, journal = _ctx(cfg)
        sch = Scheduler(cfg, None, store, etat, journal)
        r = sch.tick(executer=False)
        print(f"=== ESTIMATION d'une passe ({r['t'][:19]} UTC) — rien n'est appelé ni écrit ===")
        print(f"  dépense réelle : jour {r['depense']['jour']} / {cfg.plafonds.jour}, mois {r['depense']['mois']} / "
              f"{cfg.plafonds.mois} (cible {cfg.plafonds.cible_mois}), lignes incertaines {r['depense']['incertains']}")
        print(f"  niveau {r['niveau']} {r['cadences']}{' — ' + r['alerte'] if r.get('alerte') else ''}")
        print(f"  boucles dues : {r['boucles_dues'] or 'aucune'} ; escales ouvertes {r['escales_ouvertes']}, "
              f"suivables {r['suivables']}, location dus {r['location_dus']}")
        print(f"  COÛT ESTIMÉ DE LA PASSE : {r['cout_estime_passe']} crédits (plafond de passe {cfg.plafonds.tick}"
              f"{', borné par --max-appels ' + str(args.max_appels) if args.max_appels is not None else ''})")
        for d in r["dus_detail"][:40]:
            print(f"    dû : [{d[0]}] {str(d[1])[:24]:24} {str(d[2]):8} {d[3]:12} {d[4]}")
        p = r["projection"]
        print(f"  projection au niveau le plus large : {p['par_jour']} cr/jour, fin de mois {p['projection_mois']}")
        print("Pour une passe réelle : --executer (mode cron).")
        return 0

    verrou = VerrouDB(dsn())
    if not verrou.prendre():
        msg = "Passe SAUTÉE : une autre passe tient le verrou (chevauchement évité)."
        print(msg)
        _resume_github(msg)
        return 0
    code = 0
    try:
        store, etat, journal = _ctx(cfg)
        session = "tick-" + maintenant().strftime("%Y%m%dT%H%M%SZ")
        client = ClientDD(cfg, cle_api(), journal, session, plafond_session=cfg.plafonds.tick, store=store)
        sch = Scheduler(cfg, client, store, etat, journal,
                        log=lambda m: print(f"[{maintenant():%H:%M:%S}] {m}", flush=True))
        store.session_debut(session, "passe cron")
        solde0 = client.solde()
        statut, r = "terminee", {}
        try:
            r = sch.tick(executer=True, max_appels=args.max_appels)
        except ArretCollecte as exc:
            statut, code = "arret", 2
            r = {"motif": f"ARRÊT DE SÛRETÉ : {exc}", "compteurs": sch.compteurs}
        tot = journal.totaux(session)
        solde1 = client.solde()
        delta = solde0 - solde1 if None not in (solde0, solde1) else None
        bilan = (f"{r.get('motif')} | dépense de la passe (journal) {tot['session']} | solde {solde0} -> {solde1} "
                 f"(Δ {delta}) | jour {tot['jour']} / mois {tot['mois']} | {json.dumps(r.get('compteurs'), ensure_ascii=False)}")
        store.session_fin(session, statut, bilan)
        print(bilan)
        _resume_github(f"### Passe {session}\n\n- {bilan}")
        client.fermer()
        journal.fermer()
    finally:
        verrou.rendre()
    return code


# ------------------------------------------------------------------------------- 0 crédit
def cmd_ddl(args, cfg) -> int:
    Store(dsn(), cfg.schema).appliquer_ddl(retention=args.retention)
    print(f"Schéma {cfg.schema} appliqué (idempotent){' + job pg_cron de rétention' if args.retention else ''}.")
    return 0


def hypothese_regime(cfg: Config) -> None:
    """Régime établi HYPOTHÉTIQUE (~2,4 LINER et ~7 navires de commerce arrivent par jour ;
    approche < 150 km ≈ 7 h ; LINER attendus J−2 ; ~25 navires de commerce en zone)."""
    f = cfg.frequences
    fixe = (24 / f.expected_h + 24 / f.arrival_h) * COUT["port-calls-by-port"] + 24 / f.decouverte_h * COUT[
        "get-vessels-by-area"]
    proche, loin, quai, rade = 0.4, 1.8, 16, 7
    print(f"Hypothèse de régime établi : {proche} LINER à < 150 km, {loin} LINER au large, {quai} navires à quai + "
          f"{rade} en rade, rafales {PROVISION_RAFALES_JOUR} appels/jour ; fixe {fixe:.0f} cr/jour")
    for i, n in enumerate(cfg.cadences.niveaux):
        j = (proche * 1440 / n["proche"] + loin * 1440 / n["loin"] + quai * 1440 / n["quai"] + rade * 1440 / n["rade"]
             + PROVISION_RAFALES_JOUR * 12 / n["rafale"] + fixe)
        print(f"  niveau {i} {n} : {j:4.0f} cr/jour ≈ {j * 30:6.0f} cr/mois")


def cmd_estimer(args, cfg) -> int:
    store, etat, journal = _ctx(cfg)
    escales = _eligibles(cfg, store, etat)
    tot = journal.totaux()
    print(f"Dépense réelle (journal) : jour {tot['jour']} / {cfg.plafonds.jour}, mois {tot['mois']} / "
          f"{cfg.plafonds.mois} (cible {cfg.plafonds.cible_mois})")
    p = plan(cfg, escales, etat, args.heures, 0, borne_haute=False)
    print(f"Plan réaliste {args.heures:.0f} h, niveau 0 : location {p['appels_location']} cr ({len(p['navires'])} navires), "
          f"roster {p['credits_roster']}, découverte {p['credits_decouverte']} -> {p['credits_total']} cr")
    for l in projection(cfg, journal, escales, etat)["lignes"]:
        print(f"  niveau {l['niveau']} : {l['par_jour']} cr/jour, fin de journée {l['projection_jour']}, "
              f"fin de mois {l['projection_mois']}")
    hypothese_regime(cfg)
    return 0


def cmd_precision(args, cfg) -> int:
    store, _, _ = _ctx(cfg)
    rows = store.precision_accostages()
    obs = [r for r in rows if (r["signaux"] or {}).get("observe")]
    print(f"Accostages : {len(rows)} événement(s), dont {len(obs)} avec fourchette observée")
    for cle, lib in (("fourchette_appel_min", "appels"), ("fourchette_pos_min", "positionReceived")):
        v = sorted(float(r["signaux"][cle]) for r in obs if r["signaux"].get(cle) is not None)
        if v:
            p90 = v[min(len(v) - 1, math.ceil(0.9 * len(v)) - 1)]
            print(f"  fourchette ({lib}) : n={len(v)}, médiane {statistics.median(v):.0f} min, p90 {p90:.0f} min, "
                  f"max {v[-1]:.0f} min")
    for r in obs:
        s = r["signaux"]
        print(f"  {str(r['name'])[:22]:22} {str(r['categorie']):7} {r['timestamp_utc']:%m-%d %H:%M} "
              f"appels {s.get('fourchette_appel_min')} min / positions {s.get('fourchette_pos_min')} min "
              f"(avant : {s.get('etat_avant')}, cadence {s.get('cadence_min')})")
    return 0


def _dispatcher(store, etat, cfg, endpoint, params, corps, t) -> dict:
    if corps is None:
        return {"ignore": "corps vide"}
    if endpoint == "port-calls-by-port":
        st = params.get("search_type") or next(k for k in ("arrival", "expected") if k in corps)
        return traiter_port_calls(store, corps, st, t)
    if endpoint == "get-vessels-by-area":
        return traiter_area(store, corps, t)
    f = fiche_location(corps)
    mmsi = txt((f or {}).get("mmsi")) or txt(params.get("imo_or_mmsi"))
    e = store.escale_ouverte(mmsi) if mmsi else None
    if e is None:
        return {"ignore": f"pas d'escale ouverte pour {mmsi}"}
    e = next((x for x in store.escales_ouvertes() if x["escale_id"] == e["escale_id"]), e)
    return traiter_location(store, etat, cfg, e, corps, t)


def cmd_rejouer(args, cfg) -> int:
    """Retraite toutes les réponses archivées avec un état JETABLE. Attendu : rien de nouveau."""
    store, _, _ = _ctx(cfg)
    etat = Etat()
    tables = ("vessels", "escales", "positions", "evenements")
    avant = {t: store.compter(t) for t in tables}
    rows = store.api_raw_a_rejouer(("port-calls-by-port", "get-vessels-by-area", "get-vessel-location"))
    for r in rows:
        params = {k: v for k, v in (r["params"] or {}).items() if k not in ("source_fichier", "appel_id")}
        _dispatcher(store, etat, cfg, r["endpoint"], params, r["corps"], r["captured_at"])
    apres = {t: store.compter(t) for t in tables}
    print(f"Rejeu de {len(rows)} réponses : avant {avant} -> après {apres} "
          f"({'IDEMPOTENT' if avant == apres else 'ÉCART'})")
    return 0 if avant == apres else 1


def cmd_etat(args, cfg) -> int:
    store, _, journal = _ctx(cfg)
    tot = journal.totaux()
    print(f"Dépense réelle (journal) : jour {tot['jour']}, mois {tot['mois']}, total {tot['total']}, "
          f"lignes incertaines {tot['incertains']}")
    for t in ("vessels", "escales", "suivi", "positions", "evenements", "api_raw", "credits_journal",
              "collecte_sessions", "congestion_horaire"):
        print(f"  {t:20} {store.compter(t)}")
    for r in store._q("select v.categorie, e.statut, count(*) n from escales e left join vessels v using (mmsi) "
                      "group by 1, 2 order by 1, 2", tous=True):
        print(f"  escales {str(r['categorie']):8} {r['statut']:15} {r['n']}")
    for r in store._q("select * from etat_courant order by distance_to_port_km nulls last", tous=True):
        print(f"  {str(r['name'])[:22]:22} {str(r['categorie']):8} {r['distance_to_port_km']:>8.1f} km "
              f"{str(r['nav_status'])[:22]:22} {r['position_received']:%m-%d %H:%M} {r['statut']:10} "
              f"{str(r['phase']):15} dû {r['next_due_at']:%m-%d %H:%M}" if r["next_due_at"] else
              f"  {str(r['name'])[:22]:22} {r['statut']} (pas d'échéance)")
    return 0


# ------------------------------------------------------------------------------- polygones
def _afficher_validation(v: dict) -> None:
    for nom, r in v.items():
        taux = f"{100 * r['taux']:.1f} %" if r["taux"] is not None else "-"
        print(f"  {nom:22} : {r['dedans']}/{r['n']} = {taux}")
        for o in r["outliers"][:15]:
            print(f"      hors : {str(o['nom'])[:22]:22} mmsi {o['mmsi']} ({o['lat']:.4f}, {o['lon']:.4f}) "
                  f"SOG {o['sog']} — à {o['a_km_du_polygone']} km du polygone")


def cmd_polygones(args, cfg) -> int:
    store = Store(dsn(), cfg.schema)
    positions = store.positions_calibration(args.rayon)
    if args.action == "calibrer":
        r = pg.calibrer(positions)
        version = store.prochaine_version_polygones()
        print(f"=== Calibration v{version} sur {len(positions)} positions à <= {args.rayon:.0f} km ===")
        compte = {}
        for p in r["polygones"]:
            compte[p["type"]] = compte.get(p["type"], 0) + 1
            nom = f"{p['type']}_{compte[p['type']]}"
            print(f"  {nom:8} {p['n_points']:4} positions, {p['n_navires'] or '-'} navires, {p['aire_km2']:7.3f} km², "
                  f"centre ({p['centre'][0]:.4f}, {p['centre'][1]:.4f}), à {p['dist_seed_km']} km du port, "
                  f"cap {p['cap_seed']}°")
            store.inserer_polygone(version, p["type"], nom, pg.geojson(p["km"]), p["n_points"], p["n_navires"],
                                   p["aire_km2"], r["params"][p["type"]])
        for j in r["rejets"]:
            print(f"  REJETÉE ({j['type']}) : {j['n_points']} positions, centre ({j['centre'][0]:.4f}, "
                  f"{j['centre'][1]:.4f}) — {j['motif']}")
        manquants = {"rade", "quai", "port"} - set(compte)
        if manquants:
            print(f"  ATTENTION : aucun polygone {sorted(manquants)} (données insuffisantes)")
        store.commit()
        print("Validation de cette version (en échantillon : les polygones viennent de ces mêmes positions) :")
        _afficher_validation(pg.valider(positions, pg.Polygones.depuis_lignes(store.polygones_version(version))))
        if args.export:
            fc = {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": l["geojson"],
                 "properties": {"version": version, "type": l["type"], "nom": l["nom"], "n_points": l["n_points"],
                                "n_navires": l["n_navires"], "aire_km2": float(l["aire_km2"])}}
                for l in store.polygones_version(version)]}
            with open(args.export, "w", encoding="utf-8") as f:
                json.dump(fc, f, ensure_ascii=False, indent=1)
            print(f"GeoJSON écrit : {args.export}")
        if args.activer:
            if manquants & {"rade", "quai"}:
                print("NON activée : il manque un polygone rade ou quai.")
                return 1
            store.activer_polygones(version)
            store.commit()
            print(f"Version v{version} ACTIVE (utilisée par la classification dès la passe suivante).")
        else:
            print(f"Version v{version} enregistrée INACTIVE : `polygones activer {version}` pour l'utiliser.")
        return 0
    if args.action == "valider":
        lignes = store.polygones_version(args.version) if args.version else store.polygones_actifs()
        if not lignes:
            print("Aucun polygone (actif ou de cette version).")
            return 1
        print(f"=== Validation des polygones v{lignes[0]['version']} sur {len(positions)} positions ===")
        _afficher_validation(pg.valider(positions, pg.Polygones.depuis_lignes(lignes)))
        return 0
    if args.action == "activer":
        n = store.activer_polygones(args.version)
        store.commit()
        print(f"v{args.version} active ({n} polygone(s))." if n else f"Aucun polygone en version {args.version}.")
        return 0 if n else 1
    return 1


TRANCHES_KM = ((0, 50), (50, 150), (150, 300), (300, 600), (600, 1e9))


def cmd_couverture(args, cfg) -> int:
    """Diagnostic de couverture terrestre : pour chaque APPEL get-vessel-location des escales
    marquées (ou des MMSI donnés), distance au port, âge de la position (réception de la réponse
    − positionReceived) et « vraie nouvelle position » (positionReceived différent de l'appel
    précédent du même navire) ; agrégé par tranche de distance. Lecture seule, 0 crédit."""
    from .boucles import fiche_location, haversine_km, num, parse_dd
    from .config import PORT
    store = Store(dsn(), cfg.schema)
    if args.mmsi:
        mmsis = args.mmsi.split(",")
    else:
        mmsis = [r["mmsi"] for r in store._q(
            "select e.mmsi from suivi s join escales e using (escale_id) "
            "where s.etat ? 'diag_debut'", tous=True)]
    if not mmsis:
        print("Aucune escale marquée pour le diagnostic (suivi.etat « diag_debut »).")
        return 1
    rows = store._q("""
        select captured_at, params->>'imo_or_mmsi' mmsi, corps from api_raw
        where endpoint = 'get-vessel-location' and statut_appel = 'recue'
          and params->>'imo_or_mmsi' = any(%s) and captured_at >= %s
        order by params->>'imo_or_mmsi', captured_at""", (mmsis, args.depuis or "2000-01-01"), tous=True)
    appels, prec = [], {}
    for r in rows:
        f = fiche_location(r["corps"]) or {}
        la, lo, pr = num(f.get("latitude")), num(f.get("longitude")), parse_dd(f.get("positionReceived"))
        if la is None or lo is None or pr is None:
            appels.append({"mmsi": r["mmsi"], "dist": None, "age": None, "nouvelle": False, "vide": True, "src": None,
                           "nom": f.get("name")})
            continue
        nouvelle = prec.get(r["mmsi"]) != pr
        prec[r["mmsi"]] = pr
        appels.append({"mmsi": r["mmsi"], "nom": f.get("name"), "dist": haversine_km(la, lo, *PORT), "vide": False,
                       "age": (r["captured_at"] - pr).total_seconds() / 60, "nouvelle": nouvelle,
                       "src": f.get("dataSource")})
    print(f"Diagnostic de couverture : {len(appels)} appels, navires : "
          + ", ".join(sorted({f"{a['nom']} ({a['mmsi']})" for a in appels if a['nom']})))
    print(f"{'tranche':>12} {'appels':>7} {'vides':>6} {'nouvelles':>10} {'taux':>6} {'âge méd.':>9} {'âge p90':>8} "
          f"{'âge max':>8}  sources")
    for lo_, hi in TRANCHES_KM:
        t = [a for a in appels if a["dist"] is not None and lo_ <= a["dist"] < hi]
        vides = [a for a in appels if a["vide"]] if lo_ == 0 else []
        if not t:
            print(f"{f'{lo_:.0f}-{hi:.0f}' if hi < 1e9 else f'{lo_:.0f}+':>12} {0:>7}")
            continue
        ages = sorted(a["age"] for a in t)
        q = lambda p: ages[min(len(ages) - 1, math.ceil(p * len(ages)) - 1)]
        nv = sum(a["nouvelle"] for a in t)
        src = ", ".join(f"{k} {v}" for k, v in sorted(Counter(a["src"] for a in t).items(), key=lambda x: -x[1]))
        print(f"{f'{lo_:.0f}-{hi:.0f}' if hi < 1e9 else f'{lo_:.0f}+':>12} {len(t):>7} {len(vides):>6} {nv:>10} "
              f"{100 * nv / len(t):>5.0f}% {statistics.median(ages):>8.1f} {q(0.9):>8.1f} {ages[-1]:>8.1f}  {src}")
    print("(âge en minutes = réception de la réponse − positionReceived ; « nouvelle » = positionReceived différent "
          "de l'appel précédent du navire ; à cadence 15 min, un taux proche de 100 % et un âge < 15 min = "
          "couverture continue)")
    return 0


def cmd_congestion(args, cfg) -> int:
    store = Store(dsn(), cfg.schema)
    debut = datetime.fromisoformat(args.depuis).replace(tzinfo=timezone.utc)
    n = cong.recalculer(store, debut, qualite=args.qualite)
    print(f"congestion_horaire recalculée : {n} créneaux depuis {args.depuis} (version {cong.VERSION})")
    return 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--schema", default="collecteur")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--executer", action="store_true", help="passe réelle (sinon estimation)")
    t.add_argument("--max-appels", type=int)
    t.set_defaults(f=cmd_tick)
    d = sub.add_parser("ddl"); d.add_argument("--retention", action="store_true"); d.set_defaults(f=cmd_ddl)
    e = sub.add_parser("estimer"); e.add_argument("--heures", type=float, default=24.0); e.set_defaults(f=cmd_estimer)
    sub.add_parser("precision").set_defaults(f=cmd_precision)
    sub.add_parser("rejouer").set_defaults(f=cmd_rejouer)
    sub.add_parser("etat").set_defaults(f=cmd_etat)
    pp = sub.add_parser("polygones")
    pp.add_argument("action", choices=("calibrer", "valider", "activer"))
    pp.add_argument("version", type=int, nargs="?")
    pp.add_argument("--version", dest="version_opt", type=int)
    pp.add_argument("--activer", action="store_true")
    pp.add_argument("--export")
    pp.add_argument("--rayon", type=float, default=60.0)
    pp.set_defaults(f=cmd_polygones)
    cv = sub.add_parser("couverture")
    cv.add_argument("--depuis")
    cv.add_argument("--mmsi", help="liste séparée par des virgules (défaut : escales marquées diag)")
    cv.set_defaults(f=cmd_couverture)
    cg = sub.add_parser("congestion")
    cg.add_argument("--depuis", required=True)
    cg.add_argument("--qualite", default="recalcul")
    cg.set_defaults(f=cmd_congestion)
    a = ap.parse_args()
    if getattr(a, "version_opt", None) is not None:
        a.version = a.version_opt
    sys.exit(a.f(a, Config(schema=a.schema)))


if __name__ == "__main__":
    main()
