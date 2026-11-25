"""Plan d'appels, projection de coût, dégradation priorisée et PASSE (tick) sans état local.

Une passe (cron toutes les 5 min) : expected dû (quotidien) ? arrival dû (3,5 j) ? découverte
by-area due (5 h) ? ; fermeture des escales hors périmètre et congestion horaire (0 crédit) ;
niveau de dégradation ; puis les navires dont next_due_at est échu, par priorité, dans le
temps imparti. Tout l'état (phase, échéances, horloges, journal) est en base ; le verrou
consultatif est pris par l'appelant (__main__.cmd_tick).

DÉGRADATION PRIORISÉE (config.NIVEAUX) : on prend le niveau le plus fin dont la projection
tient sous la CIBLE mensuelle (25 000), sous le plafond jour ET, si le solde est connu, dont la
dépense projetée jusqu'au prochain renouvellement de l'abonnement tient dans (solde − réserve).
L'échelle élargit d'abord les sweeps in-port, puis la rafale, puis l'approche au large, et
l'approche finale LINER en dernier ; deux niveaux de survie ralentissent encore à l'approche de
zéro. Sous la réserve : mode survie (LINER seuls, niveau le plus large). Le blocage dur reste le
garde-fou (plafonds jour / mois sur la dépense réelle, solde < coût d'un appel).
"""
import calendar
import math
import time
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .boucles import (cadence_diag_s, lever_diagnostic, Etat, cadence_phase, traiter_area, traiter_location, traiter_port_calls, valider_area,
                      valider_location, valider_port_calls)
from . import congestion as cong
from .config import COUT, PORT, PORT_LOCODE, Config
from .garde import RefusBudget, maintenant
from .polygones import Polygones

CASA = ZoneInfo("Africa/Casablanca")
# Les rafales (accostages, départs) ne sont pas visibles sur une photo de l'instant : provision
# pour la projection = ~18,5 transitions par jour (mesure 13-16/11) × ~1,5 h × 5 appels/h à
# 12 min. À recaler sur la mesure après quelques jours de cadence adaptative.
PROVISION_RAFALES_JOUR = 140


# ------------------------------------------------------------------------------- état en base
def phase_db(cfg: Config, loc: dict) -> Optional[str]:
    """Phase interne -> phase exposée dans suivi.phase."""
    ph = loc.get("phase")
    if ph == "approche":
        d = loc.get("dernier_dist")
        return "approach_final" if d is not None and d <= cfg.cadences.loin_km else "approach_far"
    return {"rade_stable": "in_port_stable", "quai": "in_port_stable", "mouvement": "in_port_burst",
            "hors_zone": "departing"}.get(ph)


class EtatDB(Etat):
    """État du scheduler en base (tables suivi et horloges) : aucun fichier local, une passe
    cron reprend exactement où la précédente s'est arrêtée."""

    def __init__(self, store, cfg: Config):
        super().__init__(None)
        self.store, self.cfg = store, cfg
        self.d["horloges"] = dict(store.horloges())
        self._sales: set = set()
        self._horloges_sales: set = set()

    def charger(self, escale_ids) -> None:
        ids = [int(i) for i in escale_ids if str(i) not in self.d["escales"]]
        if ids:
            for eid, r in self.store.suivi(ids).items():
                self.d["escales"][str(eid)] = dict(r["etat"] or {})

    def escale(self, escale_id) -> dict:
        cle = str(escale_id)
        if cle not in self.d["escales"]:
            self.charger([escale_id])
        self._sales.add(cle)
        return super().escale(escale_id)

    def regler_horloge(self, nom: str, t) -> None:
        super().regler_horloge(nom, t)
        self._horloges_sales.add(nom)

    def sauver(self) -> None:
        rmax = timedelta(hours=self.cfg.cadences.rafale_max_h)
        for cle in self._sales:
            loc = self.d["escales"][cle]
            depuis = datetime.fromisoformat(loc["phase_depuis"]) if loc.get("phase_depuis") else None
            self.store.ecrire_suivi(
                int(cle), phase_db(self.cfg, loc),
                datetime.fromisoformat(loc["prochain_appel"]) if loc.get("prochain_appel") else None,
                datetime.fromisoformat(loc["dernier_appel"]) if loc.get("dernier_appel") else None,
                depuis + rmax if loc.get("phase") == "mouvement" and depuis else None,
                loc.get("cadence_min"), loc)
        for nom in self._horloges_sales | ({"congestion_slot"} & set(self.d["horloges"])):
            self.store.ecrire_horloge(nom, str(self.d["horloges"][nom]))
        self.store.commit()
        self._sales.clear()
        self._horloges_sales.clear()


# ------------------------------------------------------------------------------- périmètre
MARGE_SOUS_PASSE_S = 15


def dus_denses(escales: list[dict], etat: "Etat", t: datetime) -> tuple[list[dict], Optional[datetime]]:
    """Escales marquées « diag_cadence_s » (cadence infra-passe, en secondes) dues à t, et la
    prochaine échéance parmi celles qui ne le sont pas encore."""
    dus, prochaine = [], None
    for e in escales:
        loc = etat.d["escales"].get(str(e["escale_id"]), {})
        cad = cadence_diag_s(loc)
        if not cad:
            continue
        der = loc.get("dernier_appel")
        ech = datetime.fromisoformat(der) + timedelta(seconds=int(cad)) if der else t
        if ech <= t:
            dus.append(e)
        elif prochaine is None or ech < prochaine:
            prochaine = ech
    return dus, prochaine


def flux_b_termine(cfg: Config, t: Optional[datetime] = None) -> bool:
    """Suivi in-port des non-LINER (flux B) arrêté à cfg.fin_flux_b."""
    return cfg.fin_flux_b is not None and (t or maintenant()) >= cfg.fin_flux_b


def eligible(cfg: Config, e: dict, loc: dict, t: Optional[datetime] = None) -> bool:
    """LINER : toujours (approche + port). Autre commerce : seulement en zone portuaire, ou
    amorcé par la découverte / arrival (pas encore positionné, sans ETA d'expected), et jamais
    après la fin du flux B."""
    if e.get("categorie") == "LINER":
        return True
    if flux_b_termine(cfg, t):
        return False
    if loc.get("en_zone") or (e.get("min_dist") is not None and e["min_dist"] <= cfg.regles.rade_km):
        return True
    return e.get("dern_dist") is None and e.get("expected_eta") is None


def _calls(debut: datetime, fin: datetime, cadence_min: float) -> int:
    if debut >= fin:
        return 0
    return math.ceil((fin - debut).total_seconds() / (cadence_min * 60))


def _occurrences(derniere: Optional[datetime], periode_h: float, t0: datetime, heures: float) -> int:
    """Déclenchements d'une boucle périodique dans [t0, t0 + heures[ (demi-ouvert)."""
    return _calls(t0 if derniere is None else max(t0, derniere + timedelta(hours=periode_h)),
                  t0 + timedelta(hours=heures), periode_h * 60)


def est_du(derniere: Optional[datetime], periode_h: float, t: datetime) -> bool:
    return derniere is None or t >= derniere + timedelta(hours=periode_h)


# ------------------------------------------------------------------------------- plan
def _appels_escale(cfg: Config, e: dict, loc: dict, niveau: int, t0: datetime, fin: datetime,
                   borne_haute: bool) -> tuple[int, int, str]:
    """(appels, cadence affichée, phase) d'une escale sur [t0, fin[."""
    n = cfg.cadences.niveaux[min(niveau, len(cfg.cadences.niveaux) - 1)]
    prochain = datetime.fromisoformat(loc["prochain_appel"]) if loc.get("prochain_appel") else t0
    debut = max(prochain, t0)
    phase = loc.get("phase")
    if phase is None:
        if borne_haute:                                     # inconnu : la cadence la plus chère
            return _calls(debut, fin, n["rafale"]), n["rafale"], "inconnue"
        phase = ({"berthed": "quai", "anchored": "rade_stable"}.get(e["statut"])
                 or ("approche" if e.get("categorie") == "LINER" else "rade_stable"))
    if phase == "mouvement":
        depuis = datetime.fromisoformat(loc["phase_depuis"]) if loc.get("phase_depuis") else t0
        fin_rafale = min(fin, depuis + timedelta(hours=cfg.cadences.rafale_max_h))
        a = _calls(debut, fin_rafale, n["rafale"])
        b = _calls(max(debut, fin_rafale), fin, cfg.cadences.mouvement_lent)
        return a + b, n["rafale"], phase
    dist = loc.get("dernier_dist") if loc.get("dernier_dist") is not None else e.get("dern_dist")
    cad = cadence_phase(cfg, phase, dist, niveau)
    return _calls(debut, fin, cad), cad, phase


def plan(cfg: Config, escales: list[dict], etat: Etat, heures: float, niveau: int = 0,
         expected: bool = True, arrival: bool = True, decouverte: bool = True, pages: int = 1,
         max_navires: Optional[int] = None, t0: Optional[datetime] = None, borne_haute: bool = True) -> dict:
    t0 = t0 or maintenant()
    fin = t0 + timedelta(hours=heures)
    sel = selectionner([e for e in escales if eligible(cfg, e, etat.escale(e["escale_id"]), t0)], etat, max_navires)
    navires, appels_loc = [], 0
    for e in sel:
        n, cad, phase = _appels_escale(cfg, e, etat.escale(e["escale_id"]), niveau, t0, fin, borne_haute)
        appels_loc += n
        navires.append({"escale_id": e["escale_id"], "mmsi": e["mmsi"], "nom": e.get("name"),
                        "categorie": e.get("categorie"), "statut": e["statut"], "phase": phase,
                        "dist_km": e.get("dern_dist"), "cadence_min": cad, "appels": n})
    f = cfg.frequences
    n_exp = _occurrences(etat.horloge("expected"), f.expected_h, t0, heures) if expected else 0
    n_arr = _occurrences(etat.horloge("arrival"), f.arrival_h, t0, heures) if arrival else 0
    n_dec = _occurrences(etat.horloge("decouverte"), f.decouverte_h, t0, heures) if decouverte else 0
    cr_roster = (n_exp + n_arr) * pages * COUT["port-calls-by-port"]
    cr_dec = n_dec * COUT["get-vessels-by-area"]
    cr_loc = appels_loc * COUT["get-vessel-location"]
    return {"heures": heures, "niveau": niveau, "cadences": cfg.cadences.niveaux[niveau], "navires": navires,
            "appels_location": appels_loc, "credits_location": cr_loc, "expected": n_exp, "arrival": n_arr,
            "credits_roster": cr_roster, "decouvertes": n_dec, "credits_decouverte": cr_dec,
            "credits_total": cr_loc + cr_roster + cr_dec}


def selectionner(escales: list[dict], etat: Etat, max_navires: Optional[int]) -> list[dict]:
    """Priorité : approche finale LINER, puis navires en mouvement / en zone, puis le reste."""
    t = maintenant()

    def cle(e):
        loc = etat.d["escales"].get(str(e["escale_id"]), {})
        phase = loc.get("phase")
        dist = e.get("dern_dist") if e.get("dern_dist") is not None else 1e6
        rang = (-1 if loc.get("diag_cadence_min") else       # diagnostic de couverture : toujours servi
                0 if phase == "mouvement" else
                1 if phase == "approche" and dist <= 150 else
                2 if phase in ("quai", "rade_stable", "hors_zone") else
                3 if phase == "approche" else 4)
        eta = abs((e["expected_eta"] - t).total_seconds()) if e.get("expected_eta") else 1e12
        return (rang, dist, eta, e["escale_id"])

    s = sorted(escales, key=cle)
    return s if max_navires is None else s[:max_navires]


def prochain_renouvellement(t: datetime, jour: int) -> datetime:
    """Prochain renouvellement de l'abonnement : jour `jour` du mois à 23:59:59 UTC (heure exacte
    inconnue : la fin de journée est l'hypothèse prudente, horizon le plus long)."""
    def au(annee, mois):
        j = min(jour, calendar.monthrange(annee, mois)[1])
        return t.replace(year=annee, month=mois, day=j, hour=23, minute=59, second=59, microsecond=0)
    r = au(t.year, t.month)
    if r <= t:
        r = au(t.year + (t.month == 12), t.month % 12 + 1)
    return r


def besoin_jusqu_au_renouvellement(par_jour: float, par_jour_apres_b: Optional[float], t: datetime,
                                   renouvellement: datetime, fin_flux_b: Optional[datetime]) -> float:
    """Crédits projetés de t au renouvellement. Si le flux B s'arrête avant, la dépense « tous
    navires » ne court que jusqu'à sa fin, puis la dépense LINER seule prend le relais."""
    h = max((renouvellement - t).total_seconds() / 3600, 0.0)
    if par_jour_apres_b is not None and fin_flux_b is not None and t < fin_flux_b < renouvellement:
        h_b = (fin_flux_b - t).total_seconds() / 3600
        return par_jour / 24 * h_b + par_jour_apres_b / 24 * (h - h_b)
    return par_jour / 24 * h


def projection(cfg: Config, journal, escales, etat, t: Optional[datetime] = None,
               solde: Optional[int] = None) -> dict:
    """Niveau le plus fin dont la projection (dépense réelle + plan réaliste 24 h × heures
    restantes) tient sous la cible mensuelle et le plafond jour, et, solde connu, dont la dépense
    projetée jusqu'au prochain renouvellement tient dans (solde − réserve)."""
    t = t or maintenant()
    tot = journal.totaux(t=t)
    jours = calendar.monthrange(t.year, t.month)[1]
    h_mois = (t.replace(day=jours, hour=23, minute=59, second=59) - t).total_seconds() / 3600
    h_jour = (t.replace(hour=23, minute=59, second=59) - t).total_seconds() / 3600
    renouv = prochain_renouvellement(t, cfg.plafonds.jour_renouvellement)
    disponible = None if solde is None else solde - cfg.plafonds.reserve_solde
    b_en_cours = cfg.fin_flux_b is not None and t < cfg.fin_flux_b < renouv
    liner = [e for e in escales if e.get("categorie") == "LINER"] if b_en_cours else None
    info_solde = None if solde is None else {"solde": solde, "reserve": cfg.plafonds.reserve_solde,
                                             "renouvellement": renouv.isoformat(),
                                             "fin_flux_b": cfg.fin_flux_b.isoformat() if cfg.fin_flux_b else None}
    lignes = []
    for niveau in range(len(cfg.cadences.niveaux)):
        p = plan(cfg, escales, etat, 24.0, niveau, t0=t, borne_haute=False)
        par_j = p["credits_total"] + PROVISION_RAFALES_JOUR
        pm = tot["mois"] + par_j / 24 * h_mois
        pj = tot["jour"] + par_j / 24 * h_jour
        ligne = {"niveau": niveau, "cadences": cfg.cadences.niveaux[niveau], "par_jour": round(par_j),
                 "projection_mois": round(pm), "projection_jour": round(pj)}
        ok_solde = True
        if disponible is not None:
            apres_b = None
            if liner is not None:
                apres_b = plan(cfg, liner, etat, 24.0, niveau, t0=t, borne_haute=False)["credits_total"] \
                    + PROVISION_RAFALES_JOUR
            besoin = besoin_jusqu_au_renouvellement(par_j, apres_b, t, renouv, cfg.fin_flux_b)
            ligne["besoin_renouvellement"] = round(besoin)
            ok_solde = besoin <= disponible
        lignes.append(ligne)
        if pm <= cfg.plafonds.cible_mois and pj <= cfg.plafonds.jour and ok_solde:
            return {"niveau": niveau, "lignes": lignes, "depense": tot, "solde": info_solde}
    alerte = "projection > cible même au niveau le plus large : le blocage dur (garde-fou) protégera"
    if disponible is not None and not ok_solde:
        alerte = (f"solde {solde} insuffisant jusqu'au renouvellement ({renouv:%d/%m}) même au niveau le plus "
                  f"large : collecte au niveau de survie")
    return {"niveau": len(cfg.cadences.niveaux) - 1, "lignes": lignes, "depense": tot, "solde": info_solde,
            "alerte": alerte}


def mode_survie(cfg: Config, solde: Optional[int]) -> bool:
    """Solde passé sous la réserve : seuls les LINER restent suivis, au niveau le plus large."""
    return solde is not None and solde <= cfg.plafonds.reserve_solde


# ------------------------------------------------------------------------------- boucle
class Scheduler:
    def __init__(self, cfg: Config, client, store, etat: Etat, journal, log: Callable[[str], None] = print,
                 dormir: Callable = time.sleep):
        self.cfg, self.client, self.store, self.etat, self.journal = cfg, client, store, etat, journal
        self.log, self.dormir = log, dormir
        self.compteurs = {"appels": 0, "positions_nouvelles": 0, "positions_doublons": 0, "par_statut": {},
                          "evenements": {}}
        # Polygones actifs (rade / quai / port), chargés une fois par passe ; vides = repli distance.
        try:
            self.polys = Polygones.depuis_lignes(store.polygones_actifs())
        except Exception:  # noqa: BLE001 — table absente (DDL 003 pas encore appliqué) : repli distance
            store.rollback()
            self.polys = Polygones()

    def _compter(self, res) -> None:
        self.compteurs["appels"] += 1
        self.compteurs["par_statut"][res.statut] = self.compteurs["par_statut"].get(res.statut, 0) + 1

    # -- roster ---------------------------------------------------------------------
    def roster(self, search_type: str, pages: int = 1) -> None:
        for page in range(1, pages + 1):
            t = maintenant()
            res = self.client.appeler("port-calls-by-port",
                                      {"port_call": PORT_LOCODE, "search_type": search_type, "page": page},
                                      valider_port_calls(search_type))
            self._compter(res)
            if not res.ok:
                self.log(f"  {search_type} p{page} : {res.statut} ({res.http} {res.erreur}) — débit {res.depense}, "
                         f"jamais redemandée")
                break
            s = traiter_port_calls(self.store, res.corps, search_type, t)
            total = int(str(res.corps[search_type].get("pages") or 1))
            self.log(f"  {search_type} p{page}/{total} : {s['recus']} reçus, {s['commerce']} commerce "
                     f"(dont {s['liner']} LINER), {s['exclus']} exclus, {s['escales_creees']} escales ouvertes, "
                     f"{s['arrivees']} arrivées horodatées, débit {res.depense}")
            if page >= total:
                break
        self.etat.regler_horloge(search_type, maintenant())
        self.etat.sauver()

    # -- découverte -------------------------------------------------------------------
    def decouverte(self) -> None:
        res = self.client.appeler("get-vessels-by-area",
                                  {"latitude": round(PORT[0], 1), "longitude": round(PORT[1], 1),
                                   "circle_radius": self.cfg.frequences.rayon_decouverte_km}, valider_area)
        self._compter(res)
        if res.ok:
            s = traiter_area(self.store, res.corps, maintenant())
            self.log(f"  découverte : {s['recus']} navires dans {self.cfg.frequences.rayon_decouverte_km} km, "
                     f"{s['commerce']} commerce, {s['escales_creees']} suivis in-port amorcés, débit {res.depense}")
        else:
            self.log(f"  découverte : {res.statut} ({res.http} {res.erreur}), débit {res.depense}")
        self.etat.regler_horloge("decouverte", maintenant())
        self.etat.sauver()

    # -- périmètre (0 crédit) -----------------------------------------------------------
    def fermer_hors_perimetre(self) -> int:
        n = 0
        for e in self.store.escales_ouvertes():
            if not eligible(self.cfg, e, self.etat.escale(e["escale_id"])):
                self.store.maj_escale(e["escale_id"], statut="hors_perimetre")
                n += 1
        if n:
            self.store.commit()
            pourquoi = "fin du flux B (suivi in-port non-LINER arrêté)" if flux_b_termine(self.cfg) else "hors zone"
            self.log(f"  {n} escale(s) non-LINER fermée(s), {pourquoi} : hors_perimetre (0 crédit)")
        return n

    # -- suivi --------------------------------------------------------------------------
    def suivre(self, e: dict, niveau: int) -> None:
        t = maintenant()
        res = self.client.appeler("get-vessel-location", {"imo_or_mmsi": e["mmsi"]}, valider_location)
        self._compter(res)
        loc = self.etat.escale(e["escale_id"])
        if not res.ok:
            loc["dernier_appel"] = t.isoformat()
            # Pas de nouvel essai immédiat : le navire sera réinterrogé à sa cadence normale.
            cad = loc.get("diag_cadence_min") or loc.get("cadence_min") or self.cfg.cadences.niveaux[niveau]["loin"]
            loc["prochain_appel"] = (t + timedelta(minutes=cad)).isoformat()
            self.log(f"  {e.get('name') or e['mmsi']} : {res.statut} ({res.http} {res.erreur}), débit {res.depense}")
            self.etat.sauver()
            return
        loc["dernier_appel"] = t.isoformat()
        s = traiter_location(self.store, self.etat, self.cfg, e, res.corps, t, niveau, self.polys)
        self.compteurs["positions_nouvelles" if s["nouvelle"] else "positions_doublons"] += 1
        for ev in s["evenements"]:
            self.compteurs["evenements"][ev] = self.compteurs["evenements"].get(ev, 0) + 1
        d = f"{s['dist_km']:.1f} km" if s.get("dist_km") is not None else "sans position"
        self.log(f"  {e.get('name') or e['mmsi']} [{e['escale_id']} {e.get('categorie')}] : {d}, "
                 f"{s.get('nav_status') or '-'}, SOG {s.get('sog')}, âge {s.get('age_min', 0):.0f} min, "
                 f"{'NOUVELLE' if s['nouvelle'] else 'doublon'}, {s['statut']}/{s['phase']}"
                 f"{' ÉVÉNEMENTS ' + ','.join(s['evenements']) if s['evenements'] else ''}"
                 f"{' (' + s['motif'] + ')' if s['motif'] else ''}, prochain dans {s['cadence_min']} min, "
                 f"débit {res.depense}")
        self.etat.sauver()

    def plafond_diagnostic_atteint(self, denses: list[dict]) -> bool:
        """Plafond de crédits d'un groupe de diagnostic (« diag_groupe », « diag_plafond ») : compté
        dans credits_journal pour les navires du groupe depuis son début (suivi normal compris).
        Atteint : marques levées, retour aux cadences normales."""
        groupes = {}
        for e in denses:
            loc = self.etat.escale(e["escale_id"])
            if loc.get("diag_groupe") and loc.get("diag_plafond"):
                groupes.setdefault(loc["diag_groupe"], int(loc["diag_plafond"]))
        atteint = False
        for g, plafond in groupes.items():
            dep = self.store.depense_diagnostic(g)
            if dep >= plafond:
                atteint = True
                for k, l in self.etat.d["escales"].items():
                    if l.get("diag_groupe") == g and l.get("diag_cadence_min"):
                        lever_diagnostic(l, maintenant(), f"plafond du diagnostic ({dep}/{plafond} crédits)")
                self.log(f"  diagnostic {g} : plafond atteint ({dep}/{plafond} crédits), marques levées")
                self.etat.sauver()
        return atteint

    def congestion(self) -> None:
        """Recalcule les 3 dernières heures de congestion_horaire depuis les événements d'escale
        (définitions et règles de parité ANP : congestion.py). 0 crédit, idempotent.
        « bootstrap » les 48 premières heures : les navires déjà présents au démarrage n'ont pas
        d'heure d'entrée observée (borne haute = 1re observation)."""
        t = maintenant()
        premiere = self.etat.horloge("premiere_session")
        qualite = "bootstrap" if premiere is None or t - premiere < timedelta(hours=48) else "ok"
        if qualite == "ok" and flux_b_termine(self.cfg, t):
            # Rupture de mesure : sans suivi des non-LINER, rade / quai ne comptent plus que les LINER.
            qualite = "liner_seul"
        n = cong.recalculer(self.store, t - timedelta(hours=3), t, qualite)
        slot = t.astimezone(CASA).replace(minute=0, second=0, microsecond=0)
        if self.etat.d["horloges"].get("congestion_slot") != slot.isoformat():
            self.etat.d["horloges"]["congestion_slot"] = slot.isoformat()
            iv = cong.intervalles(self.store.intervalles_congestion(), t)
            c = cong.compter(iv, slot)
            self.log(f"  congestion {slot:%Y-%m-%d %H:00} (Casablanca) : rade {c['rade']} (LINER {c['rade_liner']}), "
                     f"à quai {c['a_quai']} [{qualite}, {n} créneaux recalculés, polygones "
                     f"v{self.polys.version or '-'}]")

    def tick(self, executer: bool, max_appels: Optional[int] = None) -> dict:
        """UNE passe. executer=False : estimation (lecture seule, 0 appel, 0 écriture)."""
        t0 = maintenant()
        fin = t0 + timedelta(seconds=self.cfg.duree_tick_s)
        f = self.cfg.frequences
        # Découverte AVANT arrival : une arrivée se rattache aux navires amorcés dans la même passe.
        dus_boucles = [nom for nom, periode in (("decouverte", f.decouverte_h), ("expected", f.expected_h),
                                                 ("arrival", f.arrival_h))
                       if est_du(self.etat.horloge(nom), periode, t0)]
        ouvertes = self.store.escales_ouvertes()
        self.etat.charger(e["escale_id"] for e in ouvertes)
        eligibles = [e for e in ouvertes
                     if eligible(self.cfg, e, self.etat.d["escales"].get(str(e["escale_id"]), {}))]
        # Solde (my-credits, 0 crédit) : contrainte de dégradation jusqu'au renouvellement.
        solde = self.client.solde() if self.client is not None else None
        survie = mode_survie(self.cfg, solde)
        if survie:
            eligibles = [e for e in eligibles if e.get("categorie") == "LINER"]
        pr = projection(self.cfg, self.journal, eligibles, self.etat, t0, solde)
        niveau = len(self.cfg.cadences.niveaux) - 1 if survie else pr["niveau"]
        if survie:
            pr["alerte"] = (f"MODE SURVIE : solde {solde} <= réserve {self.cfg.plafonds.reserve_solde}, LINER seuls "
                            f"au niveau {niveau}")
        dus = [e for e in selectionner(eligibles, self.etat, None)
               if e.get("next_due_at") is None or e["next_due_at"] <= t0]
        cout_boucles = sum(COUT["get-vessels-by-area"] if n == "decouverte" else COUT["port-calls-by-port"]
                           for n in dus_boucles)
        rapport = {"t": t0.isoformat(), "niveau": niveau, "cadences": self.cfg.cadences.niveaux[niveau],
                   "boucles_dues": dus_boucles, "escales_ouvertes": len(ouvertes), "suivables": len(eligibles),
                   "location_dus": len(dus), "cout_estime_passe": cout_boucles + len(dus),
                   "projection": pr["lignes"][-1], "depense": pr["depense"], "alerte": pr.get("alerte"),
                   "solde": pr.get("solde"), "survie": survie}
        if not executer:
            rapport["dus_detail"] = [(e["escale_id"], e.get("name"), e.get("categorie"), e["statut"],
                                      e.get("phase_suivi")) for e in dus]
            return rapport
        if self.etat.horloge("premiere_session") is None:
            self.etat.regler_horloge("premiere_session", t0)
        motif = "passe complète"
        try:
            for nom in dus_boucles:
                if nom == "decouverte":
                    self.decouverte()
                else:
                    self.roster(nom)
            self.fermer_hors_perimetre()
            self.congestion()
            # Recalcul après roster / découverte : les nouvelles escales sont dues tout de suite.
            ouvertes = self.store.escales_ouvertes()
            self.etat.charger(e["escale_id"] for e in ouvertes)
            eligibles = [e for e in ouvertes if eligible(self.cfg, e, self.etat.escale(e["escale_id"]))]
            if survie:
                eligibles = [e for e in eligibles if e.get("categorie") == "LINER"]
            n = 0
            for e in selectionner(eligibles, self.etat, None):
                if e.get("next_due_at") is not None and e["next_due_at"] > maintenant():
                    continue
                if maintenant() >= fin:
                    motif = "temps de passe écoulé (le reste reste dû pour la passe suivante)"
                    break
                if max_appels is not None and n >= max_appels:
                    motif = f"plafond d'appels de la passe ({max_appels})"
                    break
                self.suivre(e, niveau)
                n += 1
            # SOUS-PASSES DENSES (diagnostic) : les escales marquées « diag_cadence_s » sont
            # réinterrogées à cette cadence jusqu'à la fin de la passe. Même verrou, même garde-fou,
            # même journal : aucun appel facturé en dehors de ce pipeline.
            while motif == "passe complète":
                ouvertes = {e["escale_id"]: e for e in self.store.escales_ouvertes()}
                denses = [ouvertes[int(k)] for k, l in self.etat.d["escales"].items()
                          if cadence_diag_s(l) and int(k) in ouvertes]
                if not denses:
                    break
                if self.plafond_diagnostic_atteint(denses):
                    break
                dus_d, prochaine = dus_denses(denses, self.etat, maintenant())
                for e in dus_d:
                    if max_appels is not None and n >= max_appels:
                        motif = f"plafond d'appels de la passe ({max_appels})"
                        break
                    self.suivre(e, niveau)
                    n += 1
                if not dus_d:
                    attente = ((prochaine or maintenant()) - maintenant()).total_seconds()
                    if maintenant() + timedelta(seconds=max(attente, 0) + MARGE_SOUS_PASSE_S) >= fin:
                        break
                    time.sleep(max(attente, 1))
        except RefusBudget as exc:
            motif = f"BLOCAGE ({exc.portee}) : {exc}"
        finally:
            self.etat.sauver()
        rapport.update(motif=motif, compteurs=self.compteurs)
        return rapport
