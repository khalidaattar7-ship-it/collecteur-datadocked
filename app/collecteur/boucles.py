"""Traitement des réponses Data Docked -> vessels / escales / positions / evenements (0 crédit).

Les appels sont faits par le scheduler ; ce module ne fait que PARSER et RANGER. Il est donc
rejouable sur les réponses déjà payées (api_raw) : c'est le test d'idempotence.

Horodatage d'une position = positionReceived (à la minute), JAMAIS updateTime (= heure de
l'appel). Dédoublonnage sur (mmsi, position_received).

PÉRIMÈTRE (décisions du 2026-11-12) :
  - APPROCHE : LINER uniquement (VESSEL_TYPE_MAPPING). Un non-LINER n'est jamais suivi au large.
  - IN-PORT : tout le commerce présent dans la zone portuaire (rayon de la découverte, 50 km),
    pour le module attente.

PHASES d'une escale (état local, etat.json) et cadence associée (échelle NIVEAUX) :
  approche        LINER hors zone : « proche » (<= 150 km) ou « loin »
  rade_stable     en zone, immobile, pas à quai : « rade » (30 min nominal)
  quai            Moored à <= quai_km, immobile : « quai » (60 min nominal)
  mouvement       en zone et en mouvement (SOG > 1 kn, ou Underway sans SOG) : RAFALE (10-15
                  min) jusqu'au changement d'état ; au-delà de rafale_max_h (dérive au large en
                  attente) : mouvement_lent
  hors_zone       sorti de la zone après y avoir été : appels de CONFIRMATION du départ
                  (atdUtc / unlocodeLastport = MACAS), puis départ de repli
STATUTS en base : approaching | anchored | berthed | departed | abandoned | hors_perimetre.

ÉVÉNEMENTS (table evenements, un par type et par escale, signaux = fourchette) :
  arrivee (arrival.etaUtc, autorité) ; mouillage ; sortie_mouillage ; accostage ; sortie_quai ;
  appareillage (atdUtc, autorité ; sinon repli = 1re position hors zone, confiance basse).
  Fourchette d'un changement d'état = [dernier relevé dans l'ancien état, premier dans le
  nouveau], en minutes, sur positionReceived ET sur l'heure des appels (mesure de précision).
Seule une position NOUVELLE et pas plus ancienne que la dernière vue fait évoluer l'état.
"""
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .config import PORT, PORT_LOCODE, Config
from .polygones import Polygones, destination_autre_port, premier_token
from .types_navires import classer

RE_CASA = re.compile(r"CASA|MA ?CAS\b|MACAS", re.I)
FENETRE_ARRIVAL_H = 72          # un événement `arrival` plus vieux n'ouvre pas d'escale
PHASES_PORT = ("rade_stable", "quai", "mouvement", "hors_zone")


# ------------------------------------------------------------------------------- parsing
def parse_dd(s) -> Optional[datetime]:
    """« Nov 11, 2026 18:47 UTC » -> datetime UTC."""
    try:
        return datetime.strptime(str(s).strip(), "%b %d, %Y %H:%M UTC").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def num(x) -> Optional[float]:
    """'9.6 m.' -> 9.6 ; '' / None / 'None' -> None."""
    try:
        v = float(str(x).split()[0])
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, IndexError):
        return None


def txt(x) -> Optional[str]:
    s = None if x is None else str(x).strip()
    return None if s in (None, "", "None", "null") else s


def haversine_km(la1, lo1, la2, lo2) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def cap_vers_port(la, lo) -> float:
    p1, p2 = math.radians(la), math.radians(PORT[0])
    dl = math.radians(PORT[1] - lo)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def fiche_location(corps) -> Optional[dict]:
    """La fiche navire d'une réponse get-vessel-location (enveloppe tolérée : detail/data)."""
    if isinstance(corps, dict):
        if "latitude" in corps or "positionReceived" in corps:
            return corps
        for k in ("detail", "data", "response"):
            if isinstance(corps.get(k), dict):
                f = fiche_location(corps[k])
                if f is not None:
                    return f
    return None


def valider_port_calls(search_type: str):
    return lambda c: isinstance(c, dict) and isinstance(c.get(search_type), dict)


def valider_location(c) -> bool:
    return isinstance(c, dict)


def valider_area(c) -> bool:
    return isinstance(c, dict) and isinstance(c.get("vessels"), list)


# ------------------------------------------------------------------------------- état local
class Etat:
    """Phase et compteurs par escale, horloges des boucles — EN MÉMOIRE (tests, rejeu).
    La version persistante en base (cron, sans état local) est scheduler.EtatDB."""

    def __init__(self, chemin: Optional[Path] = None):
        self.chemin = chemin
        self.d = json.loads(chemin.read_text(encoding="utf-8")) if chemin and chemin.exists() else {}
        self.d.setdefault("escales", {})
        self.d.setdefault("horloges", {})

    def escale(self, escale_id) -> dict:
        loc = self.d["escales"].setdefault(str(escale_id), {})
        for k, v in {"prochain_appel": None, "cadence_min": None, "vides": 0, "perimes": 0, "phase": None,
                     "phase_depuis": None, "en_zone": False, "min_dist": None, "dernier_dist": None,
                     "n_appels": 0, "confirmations": 0, "sortie_zone_pr": None,
                     "prec": None}.items():           # prec = dernier relevé : {pr, appel, etat}
            loc.setdefault(k, v)
        return loc

    def horloge(self, nom: str) -> Optional[datetime]:
        v = self.d["horloges"].get(nom)
        return datetime.fromisoformat(v) if v else None

    def regler_horloge(self, nom: str, t: datetime) -> None:
        self.d["horloges"][nom] = t.isoformat()

    def sauver(self) -> None:
        if self.chemin is None:
            return
        self.chemin.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.chemin.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.chemin)


# ------------------------------------------------------------------------------- roster
# Deux événements `arrival` d'un même navire à moins de 4 jours l'un de l'autre appartiennent à
# la même escale (mouillage puis quai = plusieurs entrées dans la zone) — règle du registre
# d'escales. Au-delà, c'est une autre visite.
CHAINE_ARRIVAL = timedelta(days=4)
RECUL_MAX_ARRIVAL = timedelta(days=30)    # navire amorcé « déjà présent » : attente longue tolérée


def dd_fmt(t: datetime) -> str:
    return t.strftime("%b %d, %Y %H:%M UTC")


def arrivee_retenue(escale: dict, t_evt: datetime) -> Optional[datetime]:
    """Heure d'arrivée à retenir pour une escale OUVERTE, ou None si l'événement appartient à
    une autre visite. Ne prend jamais l'arrivée d'une visite précédente (liner hebdomadaire)."""
    ex = escale.get("arrivee_utc")
    if ex is None:
        if escale.get("expected_eta") is not None:            # escale ouverte par `expected`
            return t_evt if abs(t_evt - escale["expected_eta"]) <= CHAINE_ARRIVAL else None
        return t_evt if t_evt >= escale["created_at"] - RECUL_MAX_ARRIVAL else None
    if t_evt > ex + CHAINE_ARRIVAL:
        return t_evt                                          # visite plus récente : elle remplace
    if t_evt < ex - CHAINE_ARRIVAL:
        return None                                           # visite précédente : ignorée
    return min(ex, t_evt)                                     # même chaîne : la première entrée


def deja_couvert(derniere: Optional[dict], t_evt: Optional[datetime], search_type: str) -> bool:
    """L'événement (arrivée constatée, ou ETA déclarée d'expected) appartient-il à une visite
    DÉJÀ suivie et close ? Sans ce contrôle, une liste relue rouvrirait chaque jour une escale
    pour un navire déjà parti (ETA périmée, arrivée de la visite terminée)."""
    if derniere is None or derniere["statut"] in ("approaching", "anchored", "berthed"):
        return False
    if t_evt is None:
        return derniere["created_at"] > t_evt_none_seuil()
    if search_type == "arrival":
        if derniere.get("arrivee_utc") is not None and abs(t_evt - derniere["arrivee_utc"]) <= CHAINE_ARRIVAL:
            return True
        return t_evt <= derniere["created_at"]
    fin = derniere.get("departure_at") or derniere.get("derniere_obs") or derniere["created_at"]
    return t_evt <= fin


def t_evt_none_seuil() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=7)


def traiter_port_calls(store, corps: dict, search_type: str, t_appel: datetime) -> dict:
    """expected : candidats LINER en approche (seuls les LINER ouvrent une escale).
    arrival : heure d'arrivée qui fait autorité ; amorce le suivi in-port du commerce récent.
    Tous les navires reçus sont enregistrés dans vessels. Idempotent."""
    bloc = corps.get(search_type) or {}
    stats = {"recus": 0, "commerce": 0, "liner": 0, "exclus": 0, "escales_creees": 0, "escales_maj": 0,
             "arrivees": 0}
    for x in bloc.get("list", []):
        stats["recus"] += 1
        mmsi = txt(x.get("mmsi"))
        if not mmsi:
            continue
        longueur = num(x.get("length"))
        cl = classer(x.get("shipType"), longueur)
        store.upsert_vessel({"mmsi": mmsi, "imo": txt(x.get("imo")), "name": txt(x.get("name")),
                             "ship_type_anp": cl["ship_type_anp"], "categorie": cl["categorie"],
                             "gt": num(x.get("gt")), "length": longueur, "beam": num(x.get("beam")),
                             "country": txt(x.get("country")), "type_specific": txt(x.get("shipType"))})
        if not cl["commerce"]:
            stats["exclus"] += 1
            continue
        stats["commerce"] += 1
        liner = cl["categorie"] == "LINER"
        stats["liner"] += liner
        t_evt = parse_dd(x.get("etaUtc"))
        ouverte = store.escale_ouverte(mmsi)
        if search_type == "expected":
            # etaUtc de `expected` = ETA DÉCLARÉE par le bord. Approche = LINER uniquement.
            if not liner:
                continue
            if ouverte is None and deja_couvert(store.derniere_escale(mmsi), t_evt, "expected"):
                stats["deja_couvert"] = stats.get("deja_couvert", 0) + 1
            elif ouverte is None:
                store.creer_escale(mmsi, txt(x.get("imo")), "approaching", expected_eta=t_evt)
                stats["escales_creees"] += 1
            elif t_evt is not None and ouverte["expected_eta"] != t_evt:
                store.maj_escale(ouverte["escale_id"], expected_eta=t_evt)
                stats["escales_maj"] += 1
        elif search_type == "arrival":
            # etaUtc de `arrival` = heure d'arrivée CONSTATÉE (vérifiée par recoupement) : autorité.
            if t_evt is None:
                continue
            if (ouverte is None and t_appel - t_evt <= timedelta(hours=FENETRE_ARRIVAL_H)
                    and not deja_couvert(store.derniere_escale(mmsi), t_evt, "arrival")):
                # arrivee_utc posée ci-dessous par la règle commune (avec son événement)
                ouverte = store.creer_escale(mmsi, txt(x.get("imo")), "approaching")
                stats["escales_creees"] += 1
            if ouverte is not None:
                retenue = arrivee_retenue(ouverte, t_evt)
                if retenue is not None and retenue != ouverte["arrivee_utc"]:
                    store.maj_escale(ouverte["escale_id"], arrivee_utc=retenue)
                    store.evenement_remplacer(ouverte["escale_id"], "arrivee", retenue, "autorite",
                                              {"source": "port-calls-by-port arrival", "etaUtc": dd_fmt(retenue),
                                               "vu_le": t_appel.isoformat()})
                    stats["escales_maj"] += 1
                    stats["arrivees"] += 1
    store.commit()
    return stats


# ------------------------------------------------------------------------------- découverte
# Un navire tout juste parti est souvent encore dans le rayon de la découverte (sortie en
# cours) : on ne lui rouvre pas d'escale avant ce délai après la clôture de la précédente.
REOUVERTURE_MIN = timedelta(hours=12)


def traiter_area(store, corps: dict, t_appel: Optional[datetime] = None) -> dict:
    """get-vessels-by-area : SANS horodatage -> aucune position stockée ; amorce le suivi IN-PORT
    de tout navire de COMMERCE présent (toutes catégories, via typeSpecific)."""
    t_appel = t_appel or datetime.now(timezone.utc)
    stats = {"recus": 0, "commerce": 0, "exclus": 0, "escales_creees": 0, "tout_juste_partis": 0}
    for x in corps.get("vessels", []):
        stats["recus"] += 1
        mmsi = txt(x.get("mmsi"))
        if not mmsi:
            continue
        connu = store.vessel(mmsi)
        cl = classer(x.get("typeSpecific"), num(connu["length"]) if connu and connu["length"] is not None else None)
        store.upsert_vessel({"mmsi": mmsi, "name": txt(x.get("name")), "ship_type_anp": cl["ship_type_anp"],
                             "categorie": cl["categorie"], "type_specific": txt(x.get("typeSpecific"))})
        if not cl["commerce"]:
            stats["exclus"] += 1
            continue
        stats["commerce"] += 1
        if store.escale_ouverte(mmsi) is None:
            d = store.derniere_escale(mmsi)
            fin = d and (d.get("departure_at") or d.get("derniere_obs") or d["created_at"])
            if fin is not None and t_appel - fin < REOUVERTURE_MIN:
                stats["tout_juste_partis"] += 1
                continue
            store.creer_escale(mmsi, None, "approaching")
            stats["escales_creees"] += 1
    store.commit()
    return stats


# ------------------------------------------------------------------------------- cadences
# DIAGNOSTIC DE COUVERTURE : une escale marquée (suivi.etat « diag_cadence_min » +
# « diag_jusqu_au ») est interrogée à cadence CONSTANTE quelle que soit la distance ou la phase,
# hors dégradation, jusqu'à l'accostage / au départ ou l'échéance. Sert à caractériser la
# fraîcheur des positions terrestres en fonction de la distance (DECISIONS.md).
STATUTS_FIN_DIAG = ("anchored", "berthed", "departed", "abandoned", "hors_perimetre")


def cadence_diag_s(loc: dict) -> Optional[int]:
    """Cadence infra-passe (s) d'une escale marquée : par zone de distance (« diag_zones » =
    [[distance_min_km, secondes], ...], la 1re zone dont la distance minimale est atteinte) ou fixe."""
    zones = loc.get("diag_zones")
    if zones and loc.get("diag_cadence_min"):
        d = loc.get("dernier_dist")
        for dmin, sec in sorted(zones, key=lambda z: -z[0]):
            if d is None or d >= dmin:
                return int(sec)
    return loc.get("diag_cadence_s")


def lever_diagnostic(loc: dict, t: datetime, motif: str) -> None:
    loc["diag_termine"] = {"le": t.isoformat(), "motif": motif}
    for k in ("diag_cadence_min", "diag_cadence_s", "diag_zones"):
        loc.pop(k, None)


def cadence_diagnostic(loc: dict, t: datetime, statut: Optional[str] = None) -> Optional[int]:
    cad, fin = loc.get("diag_cadence_min"), loc.get("diag_jusqu_au")
    if not cad:
        return None
    if statut in STATUTS_FIN_DIAG:
        lever_diagnostic(loc, t, statut)
        return None
    if loc.get("phase") == "rade_stable":                # arrêté dans la zone : l'arrivée est passée
        lever_diagnostic(loc, t, "arrêt en zone")
        return None
    if fin and t >= datetime.fromisoformat(fin):
        lever_diagnostic(loc, t, "échéance")
        return None
    s = cadence_diag_s(loc)
    return max(1, round(s / 60)) if s else int(cad)


def classer_position(cfg: Config, polys: Optional[Polygones], la: float, lo: float, dist: float,
                     ns: str, sog: Optional[float]) -> dict:
    """Classification d'une position : zone, à quai, en rade (même règle en direct et au rattrapage)."""
    r = cfg.regles
    en_zone = dist <= r.rade_km
    ns1 = premier_token(ns or "")                   # parité ANP : 1re valeur d'un champ multiple
    bouge = _bouge(sog, ns1, r.sog_mouvement)
    if polys is not None and not polys.vide:
        # CLASSIFICATION POLYGONALE : à quai = Moored ∧ QUAI (ou bassin tant que les quais
        # ne sont pas tous cartographiés) ; en rade = At Anchor ∧ RADE.
        zone = polys.zone(la, lo)
        dans_port = zone in ("quai", "port")
        a_quai = ns1 == "Moored" and dans_port and not bouge
        en_rade = ns1 == "At Anchor" and zone == "rade" and not bouge
    else:
        # Repli sans polygones (amorçage) : règles de distance.
        zone = None
        dans_port = dist <= r.quai_km
        a_quai = ns1 == "Moored" and dist <= r.quai_km and not bouge
        en_rade = en_zone and not bouge
    return {"en_zone": en_zone, "ns1": ns1, "bouge": bouge, "zone": zone, "dans_port": dans_port,
            "a_quai": a_quai, "en_rade": en_rade}


def fin_mouillage(ae: datetime, be: Optional[datetime], t_fin: datetime) -> datetime:
    """Sortie du mouillage restée ouverte : l'accostage s'il suit le mouillage, sinon la fin d'escale."""
    return be if be is not None and be >= ae else max(t_fin, ae)


def cloturer_sejours(escale: dict, maj: dict, t_fin: datetime) -> None:
    """Départ : ferme le séjour à quai et au mouillage restés ouverts (sortie de quai = t_fin, l'atdUtc
    quand il existe ; sortie du mouillage = accostage, sinon t_fin). Sans cela, un navire reparti reste
    compté à quai / en rade à tout instant ultérieur (comptages d'occupation et de file d'attente).
    Une sortie déjà observée n'est jamais remplacée."""
    be = maj.get("berth_entry_at") or escale.get("berth_entry_at")
    if be is not None and (maj.get("berth_exit_at") or escale.get("berth_exit_at")) is None:
        maj["berth_exit_at"] = max(t_fin, be)
    ae = maj.get("anchorage_entry_at") or escale.get("anchorage_entry_at")
    if ae is not None and (maj.get("anchorage_exit_at") or escale.get("anchorage_exit_at")) is None:
        maj["anchorage_exit_at"] = fin_mouillage(ae, be, t_fin)


def rattrapage_sorties(e: dict, t_fin_brut: Optional[datetime] = None) -> dict:
    """Sorties à remplir sur une escale EXISTANTE (rattrapage, 0 crédit), mêmes règles qu'en direct :
    mouillage suivi d'un accostage -> sortie du mouillage = accostage ; escale partie -> cloturer_sejours
    avec departure_at, sinon l'atdUtc retrouvé dans api_raw (`t_fin_brut`). Rien d'inventé sinon."""
    maj: dict = {}
    ae, be = e.get("anchorage_entry_at"), e.get("berth_entry_at")
    if ae is not None and e.get("anchorage_exit_at") is None and be is not None and be >= ae:
        maj["anchorage_exit_at"] = be
    t_fin = e.get("departure_at") or t_fin_brut
    if e.get("statut") == "departed" and t_fin is not None:
        cloturer_sejours(e, maj, t_fin)
    return maj


def cadence_phase(cfg: Config, phase: Optional[str], dist: Optional[float], niveau: int,
                  phase_depuis: Optional[datetime] = None, t: Optional[datetime] = None) -> int:
    c = cfg.cadences
    n = c.niveaux[min(niveau, len(c.niveaux) - 1)]
    if phase == "approche":
        return n["proche"] if dist is not None and dist <= c.loin_km else n["loin"]
    if phase == "quai":
        return n["quai"]
    if phase == "rade_stable":
        return n["rade"]
    if phase == "mouvement":
        if phase_depuis and t and t - phase_depuis > timedelta(hours=c.rafale_max_h):
            return c.mouvement_lent
        return n["rafale"]
    if phase == "hors_zone":
        return c.confirmation_depart
    return n["loin"]


def _bouge(sog: Optional[float], ns: str, seuil: float) -> bool:
    if sog is not None:
        return sog > seuil
    return ns.startswith("Underway")


def _fourchette(prec: Optional[dict], pr: datetime, t_appel: datetime, cadence: Optional[int]) -> dict:
    if not prec:
        return {"observe": False}
    av_pr, av_ap = datetime.fromisoformat(prec["pr"]), datetime.fromisoformat(prec["appel"])
    return {"observe": True, "avant_pr": av_pr.isoformat(), "apres_pr": pr.isoformat(),
            "fourchette_pos_min": round((pr - av_pr).total_seconds() / 60, 1),
            "avant_appel": av_ap.isoformat(), "apres_appel": t_appel.isoformat(),
            "fourchette_appel_min": round((t_appel - av_ap).total_seconds() / 60, 1),
            "etat_avant": prec.get("etat"), "cadence_min": cadence}


# ------------------------------------------------------------------------------- suivi
def traiter_location(store, etat: Etat, cfg: Config, escale: dict, corps, t_appel: datetime,
                     niveau: int = 0, polys: Optional[Polygones] = None) -> dict:
    """Une réponse get-vessel-location -> position, navire, phase, statut, événements.
    Retourne {'nouvelle', 'statut', 'phase', 'cadence_min', 'motif', 'evenements'}."""
    r, c = cfg.regles, cfg.cadences
    e_id, mmsi = escale["escale_id"], escale["mmsi"]
    loc = etat.escale(e_id)
    loc["n_appels"] += 1
    f = fiche_location(corps)
    statut = escale["statut"]
    sortie = {"nouvelle": False, "statut": statut, "phase": loc["phase"], "motif": None, "evenements": []}
    la, lo = (num(f.get("latitude")), num(f.get("longitude"))) if f else (None, None)
    pr = parse_dd(f.get("positionReceived")) if f else None
    if f is None or la is None or lo is None or pr is None:
        loc["vides"] += 1
        if loc["vides"] >= r.vides_obs:
            store.maj_escale(e_id, statut="abandoned")
            sortie.update(statut="abandoned", motif=f"{loc['vides']} réponses sans position")
        cad = cadence_diagnostic(loc, t_appel) or loc.get("cadence_min") or c.niveaux[niveau]["loin"]
        loc["cadence_min"] = cad
        loc["prochain_appel"] = (t_appel + timedelta(minutes=cad)).isoformat()
        store.commit()
        return {**sortie, "cadence_min": cad}
    loc["vides"] = 0

    cl = classer(f.get("typeSpecific"))
    store.upsert_vessel({"mmsi": mmsi, "imo": txt(f.get("imo")), "name": txt(f.get("name")),
                         "ship_type_anp": cl["ship_type_anp"], "categorie": cl["categorie"],
                         "draught": num(f.get("draught")), "callsign": txt(f.get("callsign")),
                         "type_specific": txt(f.get("typeSpecific"))})
    categorie = (store.vessel(mmsi) or {}).get("categorie")
    liner = categorie == "LINER"

    dist = haversine_km(la, lo, *PORT)
    ns = txt(f.get("navigationalStatus")) or ""
    sog = num(f.get("speed"))
    nouvelle = store.inserer_position({
        "mmsi": mmsi, "position_received": pr, "escale_id": e_id, "imo": txt(f.get("imo")), "lat": la, "lon": lo,
        "sog": sog, "cog": num(f.get("course")), "heading": num(f.get("heading")), "nav_status": ns or None,
        "distance_to_port_km": round(dist, 3), "bearing_to_port": round(cap_vers_port(la, lo), 2),
        "destination": txt(f.get("destination")), "unlocode_destination": txt(f.get("unlocodeDestination")),
        "call_time": t_appel, "data_source": txt(f.get("dataSource"))})
    sortie["nouvelle"] = nouvelle

    plus_recente = nouvelle and (escale["derniere_obs"] is None or pr >= escale["derniere_obs"])
    maj = {"derniere_obs": max(pr, escale["derniere_obs"]) if escale["derniere_obs"] else pr}
    if escale["premiere_obs"] is None or pr < escale["premiere_obs"]:
        maj["premiere_obs"] = pr
    if escale["imo"] is None and txt(f.get("imo")):
        maj["imo"] = txt(f.get("imo"))
    age_h = (t_appel - pr).total_seconds() / 3600
    loc["perimes"] = loc["perimes"] + 1 if age_h > r.perime_h else 0
    phase = loc["phase"]

    def evt(type_, ts, confiance, signaux):
        if store.evenement(e_id, type_, ts, confiance, signaux):
            sortie["evenements"].append(type_)

    if plus_recente:
        c_ = classer_position(cfg, polys, la, lo, dist, ns, sog)
        en_zone, ns1, bouge = c_["en_zone"], c_["ns1"], c_["bouge"]
        zone, dans_port, a_quai, en_rade = c_["zone"], c_["dans_port"], c_["a_quai"], c_["en_rade"]
        if escale.get("entree_censuree") is None:
            # 1er relevé de l'escale : déjà au mouillage ou déjà à quai -> entrée NON observée (censurée).
            maj["entree_censuree"] = bool(a_quai or en_rade)
        loc.setdefault("vu_port", False)
        loc["en_zone"] = loc["en_zone"] or en_zone
        loc["min_dist"] = dist if loc["min_dist"] is None else min(loc["min_dist"], dist)
        loc["dernier_dist"] = dist
        # Début de l'escale = la plus ancienne trace : arrivée (arrival), 1re observation, ouverture.
        # (Pas l'ouverture seule : une escale amorcée par `arrival` peut être ouverte APRÈS le
        # départ réel ; son atdUtc est alors antérieur à l'ouverture mais postérieur à l'arrivée.)
        debut = min(t for t in (escale.get("arrivee_utc"), escale["premiere_obs"], escale["created_at"]) if t)
        atd = parse_dd(f.get("atdUtc"))
        dest = f"{f.get('destination') or ''} {f.get('unlocodeDestination') or ''}"
        cad_avant = loc.get("cadence_min")
        etat_obs = "quai" if a_quai else ("rade" if en_rade else "zone_mouvement" if en_zone and bouge else
                                          "zone_stable" if en_zone else "hors_zone")
        etat_obs_port = dans_port
        prec_port = (loc["prec"] or {}).get("dans_port")
        # Entrée / sortie du polygone PORT (bassin).
        if dans_port and not loc["vu_port"]:
            loc["vu_port"] = True
            fo = _fourchette(loc["prec"], pr, t_appel, cad_avant)
            # Observée seulement si le relevé précédent était CONNU hors du port (classé avec les
            # polygones) ; un relevé antérieur aux polygones ne dit pas où était le navire.
            if fo["observe"] and (loc["prec"] or {}).get("dans_port") is False:
                if escale.get("port_entry_at") is None:
                    maj["port_entry_at"] = pr
                evt("entree_port", pr, "sweep", fo)
            elif escale.get("arrivee_utc") is not None and escale.get("port_entry_at") is None:
                maj["port_entry_at"] = escale["arrivee_utc"]
                evt("entree_port", escale["arrivee_utc"], "arrival", {"observe": False, "source": "arrival"})
        elif prec_port and not dans_port:
            store.evenement_remplacer(e_id, "sortie_port", pr, "sweep", _fourchette(loc["prec"], pr, t_appel, cad_avant))
            sortie["evenements"].append("sortie_port")

        if (txt(f.get("unlocodeLastport")) == PORT_LOCODE and atd is not None and debut is not None
                and atd > debut - timedelta(hours=1)):
            # Départ CONFIRMÉ par Data Docked : atdUtc fait autorité (aussi pour la sortie de quai).
            statut, phase, maj["departure_at"] = "departed", None, atd
            cloturer_sejours(escale, maj, atd)
            evt("appareillage", atd, "autorite", {"source": "atdUtc", "unlocodeLastport": PORT_LOCODE,
                                                   "vu_le": t_appel.isoformat(), "dist_km": round(dist, 1)})
            sortie["motif"] = f"atdUtc {atd:%Y-%m-%d %H:%M} (lastPort {PORT_LOCODE})"
        elif a_quai:
            if escale.get("berth_entry_at") is None:                          # 1er accostage seulement
                fo = _fourchette(loc["prec"], pr, t_appel, cad_avant)
                if statut == "berthed" or not fo["observe"] or loc["prec"].get("etat") == "quai":
                    # Déjà à quai (statut hérité, 1er relevé, ou relevé précédent à quai) : borne
                    # haute = 1re observation de l'escale.
                    fo = {**fo, "observe": False}
                maj["berth_entry_at"] = pr if fo["observe"] else min(t for t in (escale["premiere_obs"], pr) if t)
                if fo["observe"]:
                    evt("accostage", pr, "rafale" if loc["phase"] == "mouvement" else "sweep", fo)
                else:
                    # Déjà à quai au 1er relevé : heure = 1re observation (borne haute), pour le
                    # comptage a_quai ; exclue des statistiques de précision (observe = False).
                    evt("accostage", maj["berth_entry_at"], "non_observe", fo)
                    sortie["motif"] = "déjà à quai au premier relevé : accostage non observé"
                ae = escale.get("anchorage_entry_at")
                if ae is not None and escale.get("anchorage_exit_at") is None and maj["berth_entry_at"] >= ae:
                    # Sortie du mouillage non vue en mouvement : bornée par l'accostage (1er Moored à quai).
                    maj["anchorage_exit_at"] = maj["berth_entry_at"]
            statut, phase = "berthed", "quai"
        elif en_rade:
            if statut in ("approaching", "anchored") and escale.get("anchorage_entry_at") is None:
                fo = _fourchette(loc["prec"], pr, t_appel, cad_avant)
                if statut == "anchored" or loc["prec"] and loc["prec"].get("etat") == "rade":
                    fo = {**fo, "observe": False}                   # déjà au mouillage avant ce relevé
                if fo["observe"]:
                    maj["anchorage_entry_at"] = pr
                    evt("mouillage", pr, "sweep", fo)
                else:
                    # Déjà au mouillage au 1er relevé : l'arrivée constatée (arrival) si elle est
                    # antérieure, sinon la 1re observation (borne haute).
                    arr = escale.get("arrivee_utc")
                    prem = min(t for t in (escale["premiere_obs"], pr) if t)
                    t_m = arr if arr is not None and arr <= prem else prem
                    maj["anchorage_entry_at"] = t_m
                    evt("mouillage", t_m, "non_observe", {**fo, "source": "arrival" if t_m is arr else "1re observation"})
            elif (statut == "anchored" and escale.get("anchorage_exit_at") is not None
                  and escale.get("berth_entry_at") is None):
                # Revu au mouillage sans avoir accosté : la « sortie » vue en mouvement n'était qu'un
                # repositionnement (évitage, changement de poste de mouillage). Annulée : le navire est
                # toujours en attente en rade.
                maj["anchorage_exit_at"] = None
                store.evenement_supprimer(e_id, "sortie_mouillage")
            elif statut == "berthed":
                # quitte le quai pour la rade sans mouvement observé entre deux relevés
                maj["berth_exit_at"] = pr
                store.evenement_remplacer(e_id, "sortie_quai", pr, "sweep",
                                          _fourchette(loc["prec"], pr, t_appel, cad_avant))
                sortie["evenements"].append("sortie_quai")
            statut, phase = "anchored", "rade_stable"
        elif (en_zone and statut == "approaching" and not loc["vu_port"] and escale.get("anchorage_entry_at") is None
              and destination_autre_port({"destination": f.get("destination"),
                                          "unlocode_destination": f.get("unlocodeDestination")})):
            # Dans le rayon de suivi mais à destination d'un AUTRE port (mouillage de Mohammedia,
            # ~25 km au NE) et jamais vu dans un polygone de Casablanca : hors périmètre.
            statut, phase = "hors_perimetre", None
            sortie["motif"] = f"destination « {f.get('destination')} » (autre port), hors polygones de Casablanca"
        elif en_zone and not bouge:
            # Immobile dans la zone mais hors rade (dérive d'attente au large, ou statut AIS
            # incohérent) : cadence stable, PAS compté en rade.
            if statut == "berthed" and not a_quai:
                maj["berth_exit_at"] = pr
                store.evenement_remplacer(e_id, "sortie_quai", pr, "sweep",
                                          _fourchette(loc["prec"], pr, t_appel, cad_avant))
                sortie["evenements"].append("sortie_quai")
            phase = "rade_stable"
        elif en_zone and bouge:
            if statut == "anchored" and phase != "mouvement":
                maj["anchorage_exit_at"] = pr
                evt("sortie_mouillage", pr, "sweep", _fourchette(loc["prec"], pr, t_appel, cad_avant))
            elif statut == "berthed":
                # Sortie de quai : la DERNIÈRE fait foi (un changement de poste ne coupe pas le séjour).
                maj["berth_exit_at"] = pr
                store.evenement_remplacer(e_id, "sortie_quai", pr, "sweep",
                                          _fourchette(loc["prec"], pr, t_appel, cad_avant))
                sortie["evenements"].append("sortie_quai")
                statut = "anchored"                     # sorti du quai, encore en zone
            if phase != "mouvement":
                loc["phase_depuis"] = t_appel.isoformat()
            phase = "mouvement"
        elif loc["en_zone"]:
            # Sorti de la zone après y avoir été : appels de CONFIRMATION (atdUtc), puis repli.
            if phase != "hors_zone":
                loc["sortie_zone_pr"], loc["confirmations"] = pr.isoformat(), 0
            else:
                loc["confirmations"] += 1
            phase = "hors_zone"
            if loc["confirmations"] >= c.confirmations_max and dist > r.depart_km:
                sz = datetime.fromisoformat(loc["sortie_zone_pr"])
                statut, phase, maj["departure_at"] = "departed", None, sz
                cloturer_sejours(escale, maj, sz)
                evt("appareillage", sz, "basse", {"source": "repli : 1re position hors zone", "dist_km": round(dist, 1),
                                                   "confirmations_sans_atd": loc["confirmations"]})
                sortie["motif"] = f"départ de repli ({loc['confirmations']} confirmations sans atdUtc)"
        elif escale.get("arrivee_utc") is not None and dist > r.depart_km and not loc["en_zone"]:
            # Arrivée constatée (arrival) mais déjà repartie vers un autre port avant le 1er
            # relevé : départ certain, heure inconnue (atdUtc porte sur l'escale suivante). Pas
            # d'événement : on n'invente pas d'horodatage ; departure_at reste NULL.
            statut, phase = "departed", None
            sortie["motif"] = f"reparti avant le 1er relevé (lastPort {f.get('unlocodeLastport')}, {dist:.0f} km)"
        elif not liner:
            statut, phase = "hors_perimetre", None
            sortie["motif"] = f"non-LINER hors zone ({dist:.0f} km) : pas de suivi d'approche"
        else:
            phase = "approche"
            # Abandon sur destination : seulement une destination EXPLICITE vers un autre port, vue
            # sur `dest_autre_obs` réponses consécutives. Une réponse sans champs statiques
            # (destination / lastPort / atdUtc / etaUtc vides, cas observé en mer) ne dit rien de
            # la destination et ne compte pas : elle n'abandonne plus une approche (AKNOUL, 13/11).
            autre = destination_autre_port({"destination": f.get("destination"),
                                            "unlocode_destination": f.get("unlocodeDestination")})
            if autre:
                loc["dest_autre"] = loc.get("dest_autre", 0) + 1
            elif RE_CASA.search(dest):
                loc["dest_autre"] = 0
            if dist > c.loin_km and loc.get("dest_autre", 0) >= r.dest_autre_obs:
                statut, phase = "abandoned", None
                sortie["motif"] = (f"destination « {dest.strip()} » au large ({dist:.0f} km), "
                                   f"{loc['dest_autre']} réponses consécutives")
            elif ((loc["min_dist"] or 1e9) > c.loin_km
                  and t_appel - escale["created_at"] > timedelta(days=r.approche_max_j)):
                statut, phase = "abandoned", None
                sortie["motif"] = f"jamais à < {c.loin_km:.0f} km en {r.approche_max_j:.0f} j"
        if phase != loc["phase"] and phase != "mouvement":
            loc["phase_depuis"] = t_appel.isoformat()
        loc["phase"] = phase
        loc["prec"] = {"pr": pr.isoformat(), "appel": t_appel.isoformat(), "etat": etat_obs,
                       "dans_port": etat_obs_port, "zone": zone}
    elif loc["prec"] and loc["prec"]["pr"] == pr.isoformat():
        # Même position revue : l'appel atteste que l'état n'avait pas changé à cette heure.
        # La fourchette « appel » part donc du DERNIER appel dans l'ancien état.
        loc["prec"]["appel"] = t_appel.isoformat()
    if loc["perimes"] >= r.perime_obs and statut == "approaching":
        statut, phase = "abandoned", None
        sortie["motif"] = f"position périmée ({age_h:.0f} h) {loc['perimes']} fois"
    if statut != escale["statut"]:
        maj["statut"] = statut
    store.maj_escale(e_id, **maj)
    store.commit()

    depuis = datetime.fromisoformat(loc["phase_depuis"]) if loc.get("phase_depuis") else None
    cad = cadence_diagnostic(loc, t_appel, statut) or cadence_phase(cfg, loc["phase"], loc["dernier_dist"], niveau, depuis, t_appel)
    loc["cadence_min"] = cad
    loc["prochain_appel"] = (t_appel + timedelta(minutes=cad)).isoformat()
    sortie.update(statut=statut, phase=loc["phase"], cadence_min=cad, dist_km=dist, nav_status=ns, sog=sog,
                  age_min=age_h * 60)
    return sortie
