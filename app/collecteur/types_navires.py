"""Type Data Docked (anglais) -> libellé ANP -> catégorie modèle (VESSEL_TYPE_MAPPING).

VESSEL_TYPE_MAPPING est indexé par les libellés de la capitainerie (« PORTE CONTENEUR »…),
alors que Data Docked renvoie « Container Ship »… Ce module TRADUIT vers ces libellés, puis
applique VESSEL_TYPE_MAPPING, pour classer comme l'historique d'entraînement des modèles.
VESSEL_TYPE_MAPPING est une COPIE de la table du projet de modélisation (le collecteur est
autonome) ; un test du projet vérifie que les deux restent identiques.

Types relevés dans les réponses Data Docked reçues (2026-09 -> 11).
Périmètre v1 : TOUT LE COMMERCE. Exclus : remorqueurs, dragues, pêche, plaisance, militaire,
engins de travaux, servitude (pilote, VTS, sauvetage…).
"""
import re
from typing import Optional

# Copie de la table de classification du projet de modélisation (ne pas modifier ici seul).
VESSEL_TYPE_MAPPING = {
    "PORTE CONTENEUR": "LINER", "ROULIER": "LINER", "PASSAGERS": "LINER", "CROISIERE/PAQUEBOT": "LINER",
    "VRAQUIER": "VRAC", "MINERALIER": "VRAC", "TANKER": "VRAC", "PETROLIER": "VRAC", "CHIMIQUIER": "VRAC",
    "CONVENTIONEL": "SPECIAL", "VOITURIER": "SPECIAL", "REMORQUEUR": "SPECIAL", "NAVIRE DE GUERRE": "SPECIAL",
    "DRAGUE": "SPECIAL", "BARGE": "SPECIAL", "VOILIER": "SPECIAL", "YACHT": "SPECIAL", "PLAISANCE": "SPECIAL",
    "NAVIRE ECOLE": "SPECIAL", "VEDETTES": "SPECIAL", "PECHE": "SPECIAL", "ENGIN TRAVAUX MARITIMES": "SPECIAL",
    "AUTRES": "SPECIAL",
}

# Libellé Data Docked (normalisé en minuscules) -> libellé ANP.
DD_VERS_ANP = {
    "container ship": "PORTE CONTENEUR",
    "container ship (fully cellular)": "PORTE CONTENEUR",
    "container/ro-ro cargo ship": "PORTE CONTENEUR",
    "ro-ro cargo ship": "ROULIER",
    "ro-ro/passenger ship": "PASSAGERS",
    "passenger ship": "PASSAGERS",
    "passenger (cruise) ship": "CROISIERE/PAQUEBOT",
    "passenger/cruise": "CROISIERE/PAQUEBOT",
    "bulk carrier": "VRAQUIER",
    "ore carrier": "MINERALIER",
    "chemical/oil products tanker": "CHIMIQUIER",
    "chemical/products tanker": "CHIMIQUIER",
    "chemical tanker": "CHIMIQUIER",
    "oil products tanker": "PETROLIER",
    "crude oil tanker": "PETROLIER",
    "lpg tanker": "TANKER",
    "lng tanker": "TANKER",
    "bitumen tanker": "TANKER",
    "asphalt/bitumen tanker": "TANKER",
    "tanker": "TANKER",
    "general cargo ship": "CONVENTIONEL",
    "vehicles carrier": "VOITURIER",
    "livestock carrier": "AUTRES",
    "heavy load carrier": "AUTRES",
    "reefer": "AUTRES",
    "refrigerated cargo ship": "AUTRES",
    "cement carrier": "AUTRES",
}

# Non-commerce : jamais suivi (motif complété au fil des types relevés).
_EXCLUS = re.compile(
    r"tug|towing|pilot|search|rescue|law|military|naval|patrol|port tender|fishing|sailing|"
    r"pleasure|yacht|dredg|hopper|crane|vts|buoy|supply|offshore|research|training",
    re.I,
)
INCONNUS = {"", "unknown", "none", "null", "other", "other type"}
# Type inconnu : suivi seulement si la longueur connue atteste un navire de commerce.
LONGUEUR_MIN_INCONNU_M = 80.0


def classer(type_dd: Optional[str], longueur_m: Optional[float] = None) -> dict:
    """{'ship_type_anp', 'categorie', 'commerce': bool, 'motif'} pour un type Data Docked."""
    t = (type_dd or "").strip()
    tl = t.lower()
    if tl in INCONNUS:
        commerce = longueur_m is not None and longueur_m >= LONGUEUR_MIN_INCONNU_M
        return {"ship_type_anp": None, "categorie": "UNKNOWN", "commerce": commerce,
                "motif": f"type inconnu, longueur {longueur_m}" + (" >= 80 m" if commerce else " (< 80 m ou absente)")}
    if _EXCLUS.search(tl):
        return {"ship_type_anp": None, "categorie": "SPECIAL", "commerce": False, "motif": f"non-commerce : {t}"}
    anp = DD_VERS_ANP.get(tl)
    if anp is None:
        # Type nouveau, ni exclu ni connu : présumé commerce (le périmètre est « tout le
        # commerce »), catégorie SPECIAL comme le défaut de VESSEL_TYPE_MAPPING ; signalé.
        return {"ship_type_anp": None, "categorie": "SPECIAL", "commerce": True,
                "motif": f"type non répertorié présumé commerce : {t}"}
    return {"ship_type_anp": anp, "categorie": VESSEL_TYPE_MAPPING.get(anp, "SPECIAL"), "commerce": True,
            "motif": None}
