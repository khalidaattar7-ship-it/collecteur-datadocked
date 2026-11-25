"""Constantes du collecteur temps réel Data Docked.

Aucun secret ici. Clé API : DATADOCKED_API_KEY ; base : COLLECTOR_DB_DSN ou SUPABASE_DB_DSN
(variables d'environnement — secrets GitHub en cron ; repli sur un fichier .env en local).
"""
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

API = "https://datadocked.com/api/vessels_operations"
ENV_FILE = Path(os.getenv("COLLECTOR_ENV_FILE", Path(__file__).resolve().parents[3] / ".env"))

PORT_LOCODE = "MACAS"
PORT = (33.6, -7.6167)                 # Casablanca
SCHEMA = "collecteur"
DERIVATION_VERSION = "v1_statut_navstatus"
# Clé du verrou consultatif PostgreSQL (pg_try_advisory_lock) : une seule passe à la fois.
CLE_VERROU = 0x0C011EC7

# ---- coûts MESURÉS sur le compte (débit = solde avant − après) ---------------------------
COUT = {
    "get-vessel-location": 1,
    "port-calls-by-port": 50,          # par PAGE
    "get-vessels-by-area": 10,
    "my-credits": 0,
}
# ---- débits annoncés (requêtes / min) -> intervalle minimal entre deux appels -------------
# Avec un espacement d, une fenêtre de 60 s peut contenir floor(60/d) + 1 requêtes : il faut
# donc d strictement > 60/limite.
INTERVALLE_S = {
    "get-vessel-location": 0.7,        # 100/min
    "port-calls-by-port": 4.5,         # 15/min
    "get-vessels-by-area": 1.3,        # 50/min
    "my-credits": 0.5,
}
TIMEOUT_S = {"port-calls-by-port": 120, "default": 60}   # 60 s n'a pas suffi pour port-calls
# Solde relu après l'appel : laisser au serveur le temps d'imputer le débit.
ATTENTE_AVANT_SOLDE_APRES_S = float(os.getenv("COLLECTOR_ATTENTE_SOLDE_S", "3.0"))
TENTATIVES_NON_EMISE = 3               # seules les requêtes JAMAIS parties sont réessayées
TENTATIVES_QUOTA_SANS_DEBIT = 3        # 429/5xx sans débit constaté : backoff puis nouvel essai
BACKOFF_S = (15, 30, 60)


def _env_int(nom: str, defaut: int) -> int:
    try:
        return int(os.getenv(nom, defaut))
    except (TypeError, ValueError):
        return defaut


@dataclass
class Plafonds:
    """Plafonds DURS sur la dépense RÉELLE lue dans le journal (base). En cron, ce sont eux
    l'autorisation permanente : aucune confirmation humaine par passe."""
    mois: int = field(default_factory=lambda: _env_int("COLLECTOR_PLAFOND_MOIS", 28_000))
    jour: int = field(default_factory=lambda: _env_int("COLLECTOR_PLAFOND_JOUR", 950))
    # Cible de la DÉGRADATION (pas un blocage).
    cible_mois: int = field(default_factory=lambda: _env_int("COLLECTOR_CIBLE_MOIS", 25_000))
    # Garde-fou anti-emballement d'UNE passe (plafond de session du client).
    tick: int = field(default_factory=lambda: _env_int("COLLECTOR_PLAFOND_TICK", 150))
    # SOLDE (sécurité permanente) : la dégradation vise à ce que la dépense projetée jusqu'au
    # prochain renouvellement de l'abonnement tienne dans (solde − réserve). Les crédits
    # d'abonnement ne se reportent pas : sans cette contrainte, la collecte s'arrête net à zéro.
    reserve_solde: int = field(default_factory=lambda: _env_int("COLLECTOR_RESERVE_SOLDE", 1_000))
    jour_renouvellement: int = field(default_factory=lambda: _env_int("COLLECTOR_JOUR_RENOUVELLEMENT", 18))


# Échelle de DÉGRADATION PRIORISÉE, du plus fin au plus large :
#   1) élargir d'abord les sweeps in-port (navire stable en rade / à quai), puis la rafale ;
#   2) puis l'approche LINER au large (> 150 km) ;
#   3) l'approche FINALE LINER (<= 150 km) en dernier ;
#   au-delà : blocage dur par le garde-fou (plafonds jour / mois).
# Rafale bornée à 10-15 min (précision de l'accostage).
NIVEAUX = (
    # Décision budget 18/11 -> 18/12 : in-port ADAPTATIF (à quai 60 min, en rade 30 min,
    # transition 12 min jusqu'au nouvel état). Approche LINER alignée sur la cadence de SERVICE du
    # modèle ETA de production (20 min à <= 150 km, 60 min au-delà, sur toute l'approche) : c'est
    # la cadence simulée à l'entraînement ; plus dense ne gagne rien et décale la distribution des
    # features de fenêtre (n_positions_6h, masque). Les escales en diagnostic dense gardent leur
    # cadence propre (diag_cadence_min).
    {"quai": 60, "rade": 30, "rafale": 12, "loin": 60, "proche": 20},     # 0 : nominal
    {"quai": 120, "rade": 60, "rafale": 12, "loin": 60, "proche": 20},    # 1 : sweeps in-port
    {"quai": 240, "rade": 120, "rafale": 15, "loin": 60, "proche": 20},   # 2 : sweeps + rafale
    {"quai": 240, "rade": 240, "rafale": 15, "loin": 60, "proche": 20},   # 3
    {"quai": 240, "rade": 240, "rafale": 15, "loin": 120, "proche": 20},  # 4 : approche au large
    {"quai": 240, "rade": 240, "rafale": 15, "loin": 180, "proche": 20},  # 5
    {"quai": 240, "rade": 240, "rafale": 15, "loin": 180, "proche": 40},  # 6 : approche finale, en dernier
    {"quai": 240, "rade": 240, "rafale": 15, "loin": 180, "proche": 60},  # 7
    # Niveaux de SURVIE (solde proche de zéro) : ralentir encore plutôt que s'arrêter net.
    {"quai": 480, "rade": 480, "rafale": 15, "loin": 360, "proche": 60},  # 8
    {"quai": 720, "rade": 720, "rafale": 15, "loin": 720, "proche": 120}, # 9
)


def _env_date(nom: str) -> Optional[datetime]:
    """Date ISO 8601 avec fuseau ; vide ou « aucune » -> None."""
    v = (os.getenv(nom) or "").strip()
    if not v or v.lower() == "aucune":
        return None
    d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError(f"{nom} : fuseau horaire obligatoire ({v})")
    return d


@dataclass
class Cadences:
    """Cadences en minutes ; les cadences dégradables sont dans NIVEAUX."""
    loin_km: float = 150.0             # approche : au-delà = « loin », en deçà = « proche »
    mouvement_lent: int = 60           # rafale prolongée au-delà de rafale_max_h (dérive au large)
    confirmation_depart: int = 60      # appel de confirmation (atdUtc, lastPort = MACAS)
    rafale_max_h: float = 3.0
    confirmations_max: int = 3
    niveaux: tuple = NIVEAUX


@dataclass
class Frequences:
    expected_h: float = 24.0           # port-calls expected : quotidien (candidats LINER)
    arrival_h: float = 84.0            # port-calls arrival : tous les 3,5 j (page 1 couvre ~4,7 j)
    decouverte_h: float = 5.0          # get-vessels-by-area 50 km : toutes les 4-6 h
    rayon_decouverte_km: int = 50


@dataclass
class ReglesSuivi:
    # Zone portuaire = rayon de la découverte by-area (50 km) : un navire immobile ou en dérive
    # d'attente dans ce rayon compte comme présent (statut anchored) pour le module attente.
    rade_km: float = 50.0
    quai_km: float = 6.0               # Moored à moins de 6 km : à quai
    sog_mouvement: float = 1.0         # SOG > 1 kn = le navire bouge (déclenche la rafale)
    depart_km: float = 60.0            # départ de repli : au-delà, après les confirmations
    perime_h: float = 24.0             # position plus vieille que ça = périmée
    perime_obs: int = 6                # 6 relevés périmés consécutifs -> abandon
    vides_obs: int = 12                # 12 réponses sans position consécutives -> abandon
    approche_max_j: float = 10.0       # suivi d'un « attendu » jamais venu à < 150 km -> abandon
    dest_autre_obs: int = 2            # destination explicite vers un autre port, N fois de suite -> abandon


@dataclass
class Config:
    plafonds: Plafonds = field(default_factory=Plafonds)
    cadences: Cadences = field(default_factory=Cadences)
    frequences: Frequences = field(default_factory=Frequences)
    regles: ReglesSuivi = field(default_factory=ReglesSuivi)
    schema: str = SCHEMA
    # Fin du suivi in-port des navires NON-LINER (flux B, étude de recalibrage) : à partir de cette
    # date, seuls les LINER sont suivis ; les escales non-LINER ouvertes sont fermées. None = pas de fin.
    fin_flux_b: Optional[datetime] = field(default_factory=lambda: _env_date("COLLECTOR_FIN_FLUX_B"))
    # Durée max d'une passe : le cron passe toutes les 5 min, une passe doit finir avant.
    duree_tick_s: int = field(default_factory=lambda: _env_int("COLLECTOR_DUREE_TICK_S", 240))
    # Copie disque des réponses brutes (en plus d'api_raw) : utile en local seulement.
    dossier_brut: Optional[Path] = field(
        default_factory=lambda: Path(os.environ["COLLECTOR_BRUT_DIR"]) if os.getenv("COLLECTOR_BRUT_DIR") else None)


def lire_env(*cles: str) -> Optional[str]:
    for cle in cles:
        if os.getenv(cle):
            return os.getenv(cle)
    if ENV_FILE.exists():
        for ligne in ENV_FILE.read_text(encoding="utf-8").splitlines():
            for cle in cles:
                if ligne.startswith(f"{cle}="):
                    v = ligne.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
    return None


def cle_api() -> str:
    k = lire_env("DATADOCKED_API_KEY")
    if not k:
        raise SystemExit("DATADOCKED_API_KEY absente (variable d'environnement / secret)")
    return k


def dsn() -> str:
    d = lire_env("COLLECTOR_DB_DSN", "SUPABASE_DB_DSN")
    if not d:
        raise SystemExit("COLLECTOR_DB_DSN / SUPABASE_DB_DSN absente : définir la variable d'environnement "
                         "(secret GitHub en cron). Jamais en dur.")
    return d
