"""Polygones RADE / QUAI / PORT dérivés des positions collectées, et classification des positions.

Aucune dépendance géométrique externe (le runner GitHub reste léger) : projection locale en km
autour du port, DBSCAN, enveloppe convexe, marge (buffer), point-dans-polygone.

Calibration (`calibrer`), à RELANCER quand les positions s'accumulent :
  - RADE  = enveloppe(s) des grappes DBSCAN de positions « At Anchor » immobiles ;
  - QUAI  = enveloppe(s) des grappes de positions « Moored » près du port ;
  - PORT  = enveloppe des positions de bassin (Moored ou lentes à quelques km) + des quais.
Le seed (port de Casablanca, rade au large côté N/NO) n'est qu'un GARDE-FOU : une grappe
incohérente (rade côté terre, quai loin du port) est écartée et signalée, jamais inventée.

Les polygones sont stockés en base (table polygones, versionnée) au format GeoJSON
[lon, lat] ; une seule version est active.
"""
import math
import re
from dataclasses import dataclass, field
from typing import Optional

# Seed d'orientation : bassin du port de Casablanca. Garde-fou de cohérence uniquement.
SEED = (33.60, -7.59)                 # lat, lon
# Côté terre vu du port (caps en degrés) : la côte court OSO–ENE, le large est au N/NO.
CAPS_TERRE = (100.0, 220.0)
# Destination déclarée Casablanca. Une position « At Anchor » qui déclare un AUTRE port est
# exclue de la rade : le rayon de 50 km englobe le mouillage de Mohammedia (~25 km au NE),
# constaté à la 1re calibration (destinations « Mohammedia anch »).
RE_CASA = re.compile(r"CASA|MA ?CAS\b|MACAS", re.I)


def destination_autre_port(p: dict) -> bool:
    d = f"{p.get('destination') or ''} {p.get('unlocode_destination') or ''}".strip()
    return bool(d) and d.upper() not in ("NONE", "NULL") and not RE_CASA.search(d)

PARAMS_DEFAUT = {
    "rade": {"eps_km": 3.0, "min_pts": 2, "marge_km": 1.5, "sog_max": 1.0, "dist_min_km": 1.5, "dist_max_km": 45.0},
    "quai": {"eps_km": 0.5, "min_pts": 2, "marge_km": 0.15, "dist_max_km": 6.0},
    "port": {"marge_km": 0.4, "dist_max_km": 6.0, "sog_max": 3.0},
}


# ------------------------------------------------------------------------------- géométrie
def premier_token(s) -> str:
    """Parité ANP : champ à valeurs multiples « a | b » -> 1re valeur."""
    return str(s or "").split("|")[0].strip()


def vers_km(lat: float, lon: float, origine=SEED) -> tuple[float, float]:
    la0, lo0 = origine
    return ((lon - lo0) * 111.320 * math.cos(math.radians(la0)), (lat - la0) * 110.574)


def vers_lonlat(x: float, y: float, origine=SEED) -> tuple[float, float]:
    la0, lo0 = origine
    return (lo0 + x / (111.320 * math.cos(math.radians(la0))), la0 + y / 110.574)


def dbscan(pts: list[tuple[float, float]], eps: float, min_pts: int) -> list[int]:
    """Étiquette de grappe par point (-1 = bruit). Grille de hachage : O(n) voisinages."""
    grille: dict = {}
    for i, (x, y) in enumerate(pts):
        grille.setdefault((int(x // eps), int(y // eps)), []).append(i)

    def voisins(i):
        x, y = pts[i]
        cx, cy = int(x // eps), int(y // eps)
        return [j for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in grille.get((cx + dx, cy + dy), ())
                if (pts[j][0] - x) ** 2 + (pts[j][1] - y) ** 2 <= eps * eps]

    lab, c = [None] * len(pts), -1
    for i in range(len(pts)):
        if lab[i] is not None:
            continue
        v = voisins(i)
        if len(v) < min_pts:
            lab[i] = -1
            continue
        c += 1
        lab[i] = c
        file = [j for j in v if j != i]
        while file:
            j = file.pop()
            if lab[j] == -1:
                lab[j] = c
            if lab[j] is not None:
                continue
            lab[j] = c
            vj = voisins(j)
            if len(vj) >= min_pts:
                file.extend(vj)
    return lab


def enveloppe(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Enveloppe convexe (chaîne monotone), sens trigonométrique, sans doublon de fermeture."""
    p = sorted(set(pts))
    if len(p) <= 2:
        return p

    def croix(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    bas, haut = [], []
    for q in p:
        while len(bas) >= 2 and croix(bas[-2], bas[-1], q) <= 0:
            bas.pop()
        bas.append(q)
    for q in reversed(p):
        while len(haut) >= 2 and croix(haut[-2], haut[-1], q) <= 0:
            haut.pop()
        haut.append(q)
    return bas[:-1] + haut[:-1]


def avec_marge(pts: list[tuple[float, float]], marge: float, n_arc: int = 8) -> list[tuple[float, float]]:
    """Enveloppe convexe des points dilatés d'un disque de rayon `marge` (km) : un polygone
    valide même pour 1 ou 2 points, et une tolérance à la dispersion des positions AIS."""
    dil = [(x + marge * math.cos(2 * math.pi * k / n_arc), y + marge * math.sin(2 * math.pi * k / n_arc))
           for x, y in pts for k in range(n_arc)]
    return enveloppe(dil)


def dans(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    """Point dans polygone (lancer de rayon)."""
    dedans, n = False, len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            dedans = not dedans
    return dedans


def aire_km2(poly) -> float:
    return abs(sum(poly[i][0] * poly[(i + 1) % len(poly)][1] - poly[(i + 1) % len(poly)][0] * poly[i][1]
                   for i in range(len(poly)))) / 2


def distance_km(x: float, y: float, poly) -> float:
    """Distance du point au bord du polygone (0 si dedans)."""
    if dans(x, y, poly):
        return 0.0
    best = 1e9
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % len(poly)]
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy or 1e-12)))
        best = min(best, math.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
    return best


def cap_depuis_seed(x: float, y: float) -> float:
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ------------------------------------------------------------------------------- jeu de polygones
@dataclass
class Polygones:
    """Polygones actifs, en km locaux pour les tests d'appartenance."""
    version: Optional[int] = None
    rade: list = field(default_factory=list)
    quai: list = field(default_factory=list)
    port: list = field(default_factory=list)

    @property
    def vide(self) -> bool:
        return not (self.rade or self.quai or self.port)

    def zone(self, lat: float, lon: float) -> str:
        """quai | port | rade | hors (priorité au plus précis)."""
        x, y = vers_km(lat, lon)
        if any(dans(x, y, p) for p in self.quai):
            return "quai"
        if any(dans(x, y, p) for p in self.port):
            return "port"
        if any(dans(x, y, p) for p in self.rade):
            return "rade"
        return "hors"

    @classmethod
    def depuis_lignes(cls, lignes: list[dict]) -> "Polygones":
        """Lignes de la table polygones (actives) -> objet. GeoJSON [lon, lat] -> km."""
        p = cls()
        for l in lignes:
            p.version = l["version"]
            anneau = l["geojson"]["coordinates"][0]
            km = [vers_km(lat, lon) for lon, lat in anneau[:-1]]
            getattr(p, l["type"]).append(km)
        return p


def geojson(poly_km) -> dict:
    anneau = [list(vers_lonlat(x, y)) for x, y in poly_km]
    return {"type": "Polygon", "coordinates": [anneau + [anneau[0]]]}


# ------------------------------------------------------------------------------- calibration
def calibrer(positions: list[dict], params: Optional[dict] = None) -> dict:
    """positions : dicts lat, lon, nav_status, sog, mmsi. Retourne {'polygones': [{type, km,
    n_points, n_navires, centre}], 'rejets': [...], 'params': ...}. Pur calcul (aucune E/S)."""
    pr = {k: dict(v) for k, v in PARAMS_DEFAUT.items()}
    for k, v in (params or {}).items():
        pr[k].update(v)
    sortie, rejets = [], []

    def grappes(pts, mmsis, eps, min_pts):
        lab = dbscan(pts, eps, min_pts)
        g: dict = {}
        for (pt, m), l in zip(zip(pts, mmsis), lab):
            if l >= 0:
                g.setdefault(l, []).append((pt, m))
        return list(g.values())

    def item(type_, membres, marge):
        pts = [p for p, _ in membres]
        cx, cy = sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts)
        return {"type": type_, "km": avec_marge(pts, marge), "n_points": len(pts),
                "n_navires": len({m for _, m in membres}), "centre": vers_lonlat(cx, cy)[::-1],
                "dist_seed_km": round(math.hypot(cx, cy), 2), "cap_seed": round(cap_depuis_seed(cx, cy))}

    # RADE : At Anchor immobiles, au large (garde-fou : pas côté terre, ni dans le bassin)
    r = pr["rade"]
    anc_tous = [p for p in positions
                if premier_token(p["nav_status"]) == "At Anchor" and (p.get("sog") is None or p["sog"] <= r["sog_max"])]
    autre = [p for p in anc_tous if destination_autre_port(p)]
    if autre:
        rejets.append({"type": "rade", "n_points": len(autre), "n_navires": len({p["mmsi"] for p in autre}),
                       "centre": (sum(p["lat"] for p in autre) / len(autre), sum(p["lon"] for p in autre) / len(autre)),
                       "motif": "destination déclarée autre que Casablanca : "
                                + ", ".join(sorted({str(p.get("destination")) for p in autre}))})
    anc = [(vers_km(p["lat"], p["lon"]), p["mmsi"]) for p in anc_tous if not destination_autre_port(p)]
    for membres in grappes([a for a, _ in anc], [m for _, m in anc], r["eps_km"], r["min_pts"]):
        it = item("rade", membres, r["marge_km"])
        cap = it["cap_seed"]
        if not (r["dist_min_km"] <= it["dist_seed_km"] <= r["dist_max_km"]):
            rejets.append({**it, "motif": f"distance au port {it['dist_seed_km']} km hors [{r['dist_min_km']}, "
                                          f"{r['dist_max_km']}]"})
        elif CAPS_TERRE[0] <= cap <= CAPS_TERRE[1]:
            rejets.append({**it, "motif": f"cap {cap}° depuis le port = côté terre"})
        else:
            sortie.append(it)

    # QUAI : Moored près du port
    q = pr["quai"]
    moo = [(vers_km(p["lat"], p["lon"]), p["mmsi"]) for p in positions if premier_token(p["nav_status"]) == "Moored"]
    moo = [(pt, m) for pt, m in moo if math.hypot(*pt) <= q["dist_max_km"]]
    for membres in grappes([a for a, _ in moo], [m for _, m in moo], q["eps_km"], q["min_pts"]):
        sortie.append(item("quai", membres, q["marge_km"]))

    # PORT / bassin : positions lentes ou à quai près du port, hors rade, + sommets des quais
    b = pr["port"]
    rades = [s["km"] for s in sortie if s["type"] == "rade"]
    bassin = [vers_km(p["lat"], p["lon"]) for p in positions
              if (premier_token(p["nav_status"]) == "Moored" or (p.get("sog") is not None and p["sog"] <= b["sog_max"]))]
    bassin = [pt for pt in bassin if math.hypot(*pt) <= b["dist_max_km"] and not any(dans(*pt, rp) for rp in rades)]
    sommets_quais = [v for s in sortie if s["type"] == "quai" for v in s["km"]]
    if bassin or sommets_quais:
        pts = bassin + sommets_quais
        cx, cy = sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts)
        sortie.append({"type": "port", "km": avec_marge(pts, b["marge_km"]), "n_points": len(bassin),
                       "n_navires": None, "centre": vers_lonlat(cx, cy)[::-1],
                       "dist_seed_km": round(math.hypot(cx, cy), 2), "cap_seed": round(cap_depuis_seed(cx, cy))})
    for s in sortie:
        s["aire_km2"] = round(aire_km2(s["km"]), 3)
    return {"polygones": sortie, "rejets": rejets, "params": pr}


def valider(positions: list[dict], polys: Polygones) -> dict:
    """Cohérence : At Anchor immobiles dans RADE, Moored près du port dans QUAI."""
    res = {}
    for nom, filtre, cible in (
            ("at_anchor_dans_rade", lambda p: premier_token(p["nav_status"]) == "At Anchor"
             and (p.get("sog") is None or p["sog"] <= 1.0) and (p.get("distance_to_port_km") or 0) <= 50
             and not destination_autre_port(p), polys.rade),
            ("moored_dans_quai", lambda p: premier_token(p["nav_status"]) == "Moored"
             and (p.get("distance_to_port_km") or 0) <= 6, polys.quai)):
        obs = [p for p in positions if filtre(p)]
        dedans, hors = 0, []
        for p in obs:
            x, y = vers_km(p["lat"], p["lon"])
            if any(dans(x, y, poly) for poly in cible):
                dedans += 1
            else:
                d = min((distance_km(x, y, poly) for poly in cible), default=None)
                hors.append({"mmsi": p["mmsi"], "nom": p.get("name"), "lat": p["lat"], "lon": p["lon"],
                             "sog": p.get("sog"), "a_km_du_polygone": None if d is None else round(d, 2)})
        res[nom] = {"n": len(obs), "dedans": dedans, "taux": round(dedans / len(obs), 3) if obs else None,
                    "outliers": sorted(hors, key=lambda h: -(h["a_km_du_polygone"] or 0))}
    return res
