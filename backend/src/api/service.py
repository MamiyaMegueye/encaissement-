"""Mise en forme des données : synchronisation Oracle -> DuckDB, statuts,
synthèse et rapprochement."""
import logging
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, dfc, evolution, exports, manuels, objectifs, queries, releve, store, zones
from .config import get_settings

log = logging.getLogger("controle.service")

# Écart toléré (en MRU) avant de signaler une anomalie.
TOLERANCE = 1.0
# Historique relu pour retrouver le solde de la veille de chaque caisse.
JOURS_HISTORIQUE = 120

LIBELLES_STATUT = {
    "OK": "Conforme",
    "RUPTURE": "Rupture de report",
    "JOUR_NON_VERSE": "Recette du jour non versée",
    "NON_VERSE": "Solde non versé",
    "SUR_VERSE": "Sur-versement",
    "NON_VALIDE": "Versement non validé",
    "ECART_SOURCES": "Écart entre sources",
}
# Du plus grave au moins grave : le premier trouvé devient le statut de la ligne.
_PRIORITE = ["RUPTURE", "JOUR_NON_VERSE", "NON_VERSE", "SUR_VERSE", "NON_VALIDE", "ECART_SOURCES"]


# --------------------------------------------------------------------------- outils
def _nettoyer(valeur):
    if isinstance(valeur, datetime):
        return valeur.date().isoformat()
    if isinstance(valeur, date):
        return valeur.isoformat()
    if isinstance(valeur, float):
        return round(valeur, 3)
    if isinstance(valeur, str):
        return valeur.strip()
    if valeur is not None and valeur.__class__.__name__ == "Decimal":
        return round(float(valeur), 3)
    return valeur


def _nettoyer_lignes(lignes: list[dict]) -> list[dict]:
    propres = []
    for ligne in lignes:
        propre = {cle: _nettoyer(v) for cle, v in ligne.items()}
        if propre.get("centre") is not None:
            propre["centre"] = str(propre["centre"]).strip()
        propres.append(propre)
    return propres


def _cle_centre(code) -> tuple:
    code = str(code or "")
    return (0, int(code), "") if code.isdigit() else (1, 0, code)


def _pct(part: float, total: float) -> float | None:
    if not total:
        return None
    return round(100.0 * part / total, 1)


def _params(date_debut: date, date_fin: date, centre: str | None, historique: bool = False) -> dict:
    d1 = datetime(date_debut.year, date_debut.month, date_debut.day)
    fin = date_fin if date_fin >= date_debut else date_debut
    d2 = datetime(fin.year, fin.month, fin.day) + timedelta(days=1)
    params = {"d1": d1, "d2": d2}
    if historique:
        params["d0"] = d1 - timedelta(days=JOURS_HISTORIQUE)
    if centre:
        params["centre"] = str(centre).strip()
    return params


def _plages(jours: list[date]) -> list[tuple[date, date]]:
    """Regroupe des jours en périodes continues d'au plus JOURS_PAR_REQUETE jours."""
    plages: list[list[date]] = []
    for jour in sorted(jours):
        if (plages and jour == plages[-1][1] + timedelta(days=1)
                and (jour - plages[-1][0]).days < JOURS_PAR_REQUETE):
            plages[-1][1] = jour
        else:
            plages.append([jour, jour])
    return [(d, f) for d, f in plages]


_SQL = {
    "controle": (queries.sql_controle, True),
    "encaissements": (queries.sql_encaissements, False),
    "versements": (queries.sql_versements, False),
    "cheques": (queries.sql_cheques, False),
    "annulations": (queries.sql_annulations, False),
    "non_valides": (queries.sql_non_valides, False),
    "reports": (queries.sql_reports, True),
}

# Une requête Oracle couvre au plus ce nombre de jours : une longue période est
# chargée morceau par morceau, et chaque morceau est enregistré dès qu'il arrive.
# Les morceaux d'une même période sont lus en parallèle, dans les fils dédiés à
# Oracle : une année se charge ainsi bien plus vite que mois après mois.
JOURS_PAR_REQUETE = 31
# Attente d'un chargement avant de répondre « chargement en cours » (secondes).
ATTENTE_COURTE = 3
# Attente maximale pour un export ou une comparaison (secondes).
ATTENTE_LONGUE = 1800
# Après un échec, délai avant de relancer la même requête sur Oracle (secondes).
DELAI_APRES_ECHEC = 60

_verrou_centres = threading.Lock()
# Dernier résultat d'un appel à Oracle, pour l'indicateur de l'en-tête.
etat_oracle = {"ok": None, "erreur": None}

# Les chargements Oracle tournent dans quelques fils dédiés : une demande de
# l'écran n'attend jamais la fin d'une longue requête, elle reçoit « chargement
# en cours » et redemande un peu plus tard.
_executeur: ThreadPoolExecutor | None = None
_travaux: dict[tuple[str, str], dict] = {}
_verrou_travaux = threading.Lock()
_local = threading.local()


class ChargementEnCours(Exception):
    """Les données demandées sont en cours de lecture dans Oracle."""

    def __init__(self, jeu: str, fait: int, total: int, secondes: float):
        self.jeu, self.fait, self.total, self.secondes = jeu, fait, total, secondes
        super().__init__(
            f"Lecture dans Oracle en cours : {fait} jour(s) sur {total} chargé(s), "
            f"depuis {int(secondes)} s."
        )


@contextmanager
def en_attente():
    """Dans ce bloc, les lectures attendent la fin des chargements (export,
    comparaison, rafraîchissement automatique) au lieu de répondre « en cours »."""
    precedent = getattr(_local, "attendre", False)
    _local.attendre = True
    try:
        yield
    finally:
        _local.attendre = precedent


def _nb_fils() -> int:
    """Nombre de lectures simultanées dans Oracle. Une connexion du pool reste
    libre pour la liste des centres et le diagnostic."""
    return max(1, get_settings().oracle_pool_max - 1)


def _get_executeur() -> ThreadPoolExecutor:
    global _executeur
    if _executeur is None:
        _executeur = ThreadPoolExecutor(max_workers=_nb_fils(), thread_name_prefix="oracle")
    return _executeur


def arreter():
    global _executeur
    with _verrou_travaux:
        travaux = list(_travaux.values())
    for travail in travaux:
        travail["annule"].set()
    if _executeur is not None:
        _executeur.shutdown(wait=False, cancel_futures=True)
        _executeur = None
    # Les morceaux encore en file ne seront jamais lus : on libère ceux qui attendent.
    for travail in travaux:
        with travail["verrou"]:
            if not travail["futur"].done():
                travail["fini"] = time.monotonic()
                travail["futur"].set_result(None)


def _lire_morceau(travail: dict, debut: date, fin: date):
    """Lit une période d'au plus JOURS_PAR_REQUETE jours dans Oracle et
    l'enregistre dans DuckDB."""
    jeu, centre = travail["jeu"], travail["centre"]
    construire, historique = _SQL[jeu]
    params = _params(debut, fin, centre, historique)
    t0 = time.perf_counter()
    lignes = _nettoyer_lignes(
        db.fetch_all(construire(get_settings().oracle_schema, bool(centre)), params))
    t1 = time.perf_counter()
    store.remplacer(jeu, debut, fin, lignes, centre)
    t2 = time.perf_counter()
    with travail["verrou"]:
        travail["oracle_s"] += t1 - t0
        travail["duckdb_s"] += t2 - t1
        travail["lignes"] += len(lignes)
        for i in range((fin - debut).days + 1):
            travail["restants"].discard(debut + timedelta(days=i))
    log.info("%s%s : %s ligne(s) du %s au %s (Oracle %.1f s, DuckDB %.1f s)",
             jeu, f" centre {centre}" if centre else "", len(lignes), debut, fin, t1 - t0, t2 - t1)


def _charger(travail: dict):
    """Lit le morceau suivant du chargement, puis rend le fil.

    Chaque passage ne lit qu'un morceau et se remet en file pour le suivant :
    un autre chargement (le contrôle du jour, un autre onglet) n'attend ainsi
    jamais plus d'un morceau, même pendant la lecture d'une année entière."""
    jeu = travail["jeu"]
    with travail["verrou"]:
        arret = travail["erreur"] or travail["annule"].is_set() or not travail["plages"]
        plage = None if arret else travail["plages"].popleft()
    if plage is not None:
        try:
            _lire_morceau(travail, *plage)
        except db.BaseIndisponible as exc:
            etat_oracle.update(ok=False, erreur=str(exc))
            travail["erreur"] = str(exc)
            log.warning("%s : %s", jeu, exc)
        except Exception as exc:  # ne jamais laisser un fil mourir sans trace
            travail["erreur"] = f"Erreur inattendue pendant le chargement : {exc}"
            log.exception("%s : erreur pendant le chargement", jeu)
    with travail["verrou"]:
        if travail["futur"].done():
            return  # le serveur s'arrête
        if plage is not None and travail["plages"] and not travail["erreur"] \
                and not travail["annule"].is_set():
            try:
                _get_executeur().submit(_charger, travail)
                return
            except RuntimeError:  # le serveur s'arrête
                pass
        travail["actifs"] -= 1
        if travail["actifs"] > 0:
            return
        if travail["annule"].is_set() and travail["plages"]:
            log.info("%s : chargement interrompu, une autre période est demandée", jeu)
        elif not travail["erreur"]:
            etat_oracle.update(ok=True, erreur=None)
        travail["fini"] = time.monotonic()
        travail["futur"].set_result(None)


def _lancer(jeu: str, centre: str | None, manquants: list[date]) -> dict | None:
    """Démarre un chargement, ou retourne celui qui tourne déjà pour ce jeu et
    ce centre. Retourne None si un échec récent interdit de relancer tout de suite."""
    cle = (jeu, centre or store.TOUS)
    with _verrou_travaux:
        travail = _travaux.get(cle)
        if travail is not None and not travail["futur"].done():
            if not set(manquants) <= travail["restants"]:
                # Une autre période est demandée : le chargement en cours s'arrête
                # après ses morceaux en cours, et la prochaine demande lancera le bon.
                travail["annule"].set()
            return travail
        if (travail is not None and travail["erreur"]
                and time.monotonic() - travail["fini"] < DELAI_APRES_ECHEC
                and set(manquants) <= travail["demandes"]):
            return None
        plages = deque(_plages(sorted(manquants)))
        executeur = _get_executeur()
        # Autant de morceaux en parallèle que de fils dédiés à Oracle.
        actifs = max(1, min(len(plages), _nb_fils()))
        travail = {
            "jeu": jeu, "centre": centre, "demandes": set(manquants), "restants": set(manquants),
            "total": len(manquants), "debut": time.monotonic(), "fini": None, "erreur": None,
            "annule": threading.Event(), "oracle_s": 0.0, "duckdb_s": 0.0, "lignes": 0,
            "plages": plages, "actifs": actifs, "verrou": threading.Lock(), "futur": Future(),
        }
        _travaux[cle] = travail
        for _ in range(actifs):
            executeur.submit(_charger, travail)
        return travail


def _assurer(jeu: str, date_debut: date, date_fin: date, centre: str | None):
    """Complète la base locale depuis Oracle si nécessaire.

    Un centre seul est chargé seul : inutile de lire tous les centres dans
    Oracle pour n'en afficher qu'un."""
    if date_fin < date_debut:
        date_fin = date_debut
    centre = str(centre).strip() if centre else None
    duree = get_settings().refresh_seconds
    attendre = getattr(_local, "attendre", False)
    limite = time.monotonic() + (ATTENTE_LONGUE if attendre else ATTENTE_COURTE)

    while True:
        manquants = store.jours_a_charger(jeu, date_debut, date_fin, duree, centre)
        if not manquants:
            break
        present = store.tout_present(jeu, date_debut, date_fin, centre)
        travail = _lancer(jeu, centre, manquants)
        if travail is None:
            # Échec récent : on sert la copie locale si elle couvre la période.
            if present:
                break
            raise db.BaseIndisponible(_travaux[(jeu, centre or store.TOUS)]["erreur"])
        try:
            travail["futur"].result(timeout=max(0.0, limite - time.monotonic()))
        except TimeoutError:
            if present:
                break  # données locales servies pendant que le rechargement continue
            raise ChargementEnCours(jeu, travail["total"] - len(travail["restants"]),
                                    travail["total"], time.monotonic() - travail["debut"])
        if travail["erreur"]:
            if present:
                log.warning("%s : Oracle indisponible, données locales servies", jeu)
                break
            raise db.BaseIndisponible(travail["erreur"])
        if travail["annule"].is_set():
            # Le chargement a été interrompu par une autre demande : on laisse
            # un court répit avant de relancer, pour ne pas tourner à vide.
            time.sleep(0.25)
        if time.monotonic() >= limite:
            if present:
                break
            raise ChargementEnCours(jeu, 0, len(manquants), 0)


def _lire(jeu: str, date_debut: date, date_fin: date, centre: str | None) -> list[dict]:
    """Complète la base locale depuis Oracle si nécessaire, puis lit DuckDB."""
    if date_fin < date_debut:
        date_fin = date_debut
    centre = str(centre).strip() if centre else None
    _assurer(jeu, date_debut, date_fin, centre)
    return store.lire(jeu, date_debut, date_fin, centre)


def synchroniser(date_debut: date, date_fin: date, jeux: tuple[str, ...] | None = None):
    """Marque la période comme à recharger, puis recharge tout de suite les
    jeux demandés (tous par défaut). Les autres se rechargent à l'affichage."""
    store.invalider(date_debut, date_fin)
    with en_attente():
        for jeu in jeux or tuple(_SQL):
            _assurer(jeu, date_debut, date_fin, None)


def etat_travaux() -> dict:
    """Chargements et verrous, lus sans attendre aucun verrou."""
    travaux = []
    for t in list(_travaux.values()):
        travaux.append({
            "jeu": t["jeu"], "centre": t["centre"], "total": t["total"], "restants": len(t["restants"]),
            "en_cours": not t["futur"].done(), "erreur": t["erreur"], "lignes": t["lignes"],
            "depuis_s": round(time.monotonic() - t["debut"], 1),
            "oracle_s": round(t["oracle_s"], 1), "duckdb_s": round(t["duckdb_s"], 1),
        })
    return {
        "verrou_travaux_pris": _verrou_travaux.locked(),
        "verrou_centres_pris": _verrou_centres.locked(),
        "base_locale": store.verrous(),
        "chargements": travaux,
    }


def diagnostic(jour: date) -> dict:
    """Mesures pour comprendre une lenteur : taille des tables, index présents
    et durée de quelques requêtes simples sur une journée."""
    schema = get_settings().oracle_schema
    tables = ["T_PIECE_REC", "T_PIECE_DETAIL", "S_VERS_ESPECE", "S_RECAP_CAISSE",
              "S_STRUCTURE", "S_PERSONNEL", "S_GROUPEMENT", "S_PORTEFEUILLE", "S_ABONNEMENT",
              "S_DEMANDE"]
    liste = ", ".join(f"'{t}'" for t in tables)
    d1 = datetime(jour.year, jour.month, jour.day)
    d2 = d1 + timedelta(days=1)
    resultat: dict = {"jour": jour.isoformat(), "mesures": []}

    def mesurer(nom: str, sql: str, params: dict | None = None, garder: bool = False):
        t0 = time.perf_counter()
        try:
            lignes = _nettoyer_lignes(db.fetch_all(sql, params or {}, delai_ms=90_000))
            mesure = {"requete": nom, "secondes": round(time.perf_counter() - t0, 2), "lignes": len(lignes)}
            if garder:
                mesure["resultat"] = lignes
            elif lignes:
                mesure["resultat"] = lignes[0]
        except db.BaseIndisponible as exc:
            lignes = []
            mesure = {"requete": nom, "secondes": round(time.perf_counter() - t0, 2), "erreur": str(exc)}
        resultat["mesures"].append(mesure)
        return lignes

    mesurer("tailles des tables",
            f"SELECT table_name, num_rows, last_analyzed FROM all_tables "
            f"WHERE owner = '{schema}' AND table_name IN ({liste}) ORDER BY table_name", garder=True)
    mesurer("index",
            f"SELECT table_name, index_name, column_position, column_name FROM all_ind_columns "
            f"WHERE table_owner = '{schema}' AND table_name IN ({liste}) "
            f"ORDER BY table_name, index_name, column_position", garder=True)
    mesurer("colonnes",
            f"SELECT table_name, column_name, data_type FROM all_tab_columns "
            f"WHERE owner = '{schema}' AND table_name IN ('T_PIECE_REC', 'T_PIECE_DETAIL', "
            f"'S_PORTEFEUILLE', 'S_ABONNEMENT', 'S_DEMANDE') ORDER BY table_name, column_id", garder=True)
    mesurer("e-paiements du jour",
            f"SELECT COUNT(*) AS nb, SUM(pr.PREC_MNT_PAY) AS montant FROM {schema}.T_PIECE_REC pr "
            f"WHERE pr.MP_ID = 99 AND pr.PREC_VLD = 1 AND pr.PREC_DATE >= :d1 AND pr.PREC_DATE < :d2",
            {"d1": d1, "d2": d2})
    recus = mesurer("reçus du jour par date comptable",
                    f"SELECT COUNT(*) AS nb, MIN(pr.PREC_ID) AS un_recu FROM {schema}.T_PIECE_REC pr "
                    f"WHERE pr.PREC_DATE_COMPTABLE >= :d1 AND pr.PREC_DATE_COMPTABLE < :d2",
                    {"d1": d1, "d2": d2})
    mesurer("reçus du jour par date du reçu",
            f"SELECT COUNT(*) AS nb FROM {schema}.T_PIECE_REC pr "
            f"WHERE pr.PREC_DATE >= :d1 AND pr.PREC_DATE < :d2", {"d1": d1, "d2": d2})
    if recus and recus[0].get("un_recu"):
        mesurer("détail d'un reçu",
                f"SELECT COUNT(*) AS nb FROM {schema}.T_PIECE_DETAIL pd WHERE pd.PREC_ID = :id",
                {"id": recus[0]["un_recu"]})
    mesurer("versements du jour",
            f"SELECT COUNT(*) AS nb FROM {schema}.S_VERS_ESPECE v "
            f"WHERE v.VERS_DATE >= :d1 AND v.VERS_DATE < :d2", {"d1": d1, "d2": d2})
    mesurer("récapitulatif du jour",
            f"SELECT COUNT(*) AS nb FROM {schema}.S_RECAP_CAISSE r "
            f"WHERE r.REC_DATE >= :d1 AND r.REC_DATE < :d2", {"d1": d1, "d2": d2})
    with _verrou_travaux:
        resultat["chargements"] = [
            {"jeu": t["jeu"], "centre": t["centre"], "total": t["total"],
             "restants": len(t["restants"]), "en_cours": not t["futur"].done(), "erreur": t["erreur"],
             "lignes": t["lignes"], "oracle_s": round(t["oracle_s"], 1), "duckdb_s": round(t["duckdb_s"], 1)}
            for t in _travaux.values()
        ]
    return resultat


# --------------------------------------------------------------------------- jeux de données
def centres() -> list[dict]:
    with _verrou_centres:
        if not store.centres_a_jour():
            schema = get_settings().oracle_schema
            d0 = datetime.combine(date.today() - timedelta(days=400), datetime.min.time())
            try:
                lignes = _nettoyer_lignes(db.fetch_all(queries.sql_centres(schema), {"d0": d0}))
                store.remplacer_centres(lignes)
            except db.BaseIndisponible:
                if not store.lire_centres():
                    raise
    return sorted(store.lire_centres(), key=lambda l: _cle_centre(l["centre"]))


_verrou_personnel = threading.Lock()
# Après un échec, délai avant de redemander la liste des agents à Oracle (secondes).
DELAI_PERSONNEL = 600
_personnel = {"echec": None}


def _assurer_personnel():
    """Met à jour, une fois par jour, la liste des agents (matricule et nom).

    Le nom n'est qu'un complément d'affichage : si Oracle ne répond pas, les
    onglets s'affichent quand même, avec les noms déjà connus, et la liste
    n'est redemandée qu'après un délai."""
    with _verrou_personnel:
        echec = _personnel["echec"]
        if echec is not None and time.monotonic() - echec < DELAI_PERSONNEL:
            return
        try:
            if store.personnel_a_jour():
                return
            lignes = db.fetch_all(queries.sql_personnel(get_settings().oracle_schema), {})
            store.remplacer_personnel(lignes)
            _personnel["echec"] = None
        except Exception as exc:
            _personnel["echec"] = time.monotonic()
            log.warning("Liste des agents non mise à jour : %s", exc)


_verrou_tarifs = threading.Lock()
_tarifs = {"fil": None, "depuis": None, "echec": None, "erreur": None}


def _charger_tarifs():
    try:
        lignes = db.fetch_all(queries.sql_tarifs(get_settings().oracle_schema), {}, delai_ms=600_000)
        for l in lignes:
            l["grp_code"] = str(l["grp_code"]).strip() if l.get("grp_code") is not None else None
            l["tarif"] = str(l["tarif"]).strip() if l.get("tarif") is not None else None
        store.remplacer_tarifs(lignes)
        _tarifs["echec"], _tarifs["erreur"] = None, None
    except Exception as exc:
        _tarifs["echec"], _tarifs["erreur"] = time.monotonic(), str(exc)
        log.warning("Tarifs non chargés : %s", exc)
    finally:
        _tarifs["fil"] = None


def _assurer_tarifs():
    """Charge une fois par jour le tarif de tous les abonnements, en arrière-plan.
    Pendant le premier chargement, l'écran affiche « en cours » ; ensuite, les
    tarifs déjà connus restent servis pendant le rechargement. Après un échec,
    Oracle n'est redemandé qu'après un délai."""
    with _verrou_tarifs:
        if store.tarifs_a_jour():
            return
        connus = store.info_tarifs() is not None
        if _tarifs["fil"] is None:
            echec = _tarifs["echec"]
            if echec is None or time.monotonic() - echec > DELAI_PERSONNEL:
                _tarifs["depuis"] = time.monotonic()
                _tarifs["fil"] = threading.Thread(target=_charger_tarifs, name="tarifs", daemon=True)
                _tarifs["fil"].start()
            elif not connus:
                raise db.BaseIndisponible(f"Tarifs non chargés : {_tarifs['erreur']}")
        if not connus:
            secondes = time.monotonic() - _tarifs["depuis"]
            exc = ChargementEnCours("tarifs", 0, 1, secondes)
            exc.args = (f"Lecture des tarifs dans Oracle en cours, depuis {int(secondes)} s.",)
            raise exc


def tarifs(date_debut: date, date_fin: date, centre: str | None = None, recherche: str | None = None,
           tarif: str | None = None) -> dict:
    """Chaque référence encaissée sur la période avec son tarif, et la
    répartition des références par tarif (avec les pourcentages)."""
    if date_fin < date_debut:
        date_fin = date_debut
    centre = str(centre).strip() if centre else None
    _assurer_encaissements(date_debut, date_fin)
    _assurer_tarifs()
    r = store.references_tarifs(date_debut, date_fin, centre, recherche, tarif)
    nb = sum(x["nb_references"] for x in r["synthese"])
    recus = sum(x["nb_recus"] for x in r["synthese"])
    montant = sum(x["montant"] for x in r["synthese"])
    for x in r["synthese"]:
        x["pct_references"] = _pct(x["nb_references"], nb)
        x["pct_recus"] = _pct(x["nb_recus"], recus)
        x["pct_montant"] = _pct(x["montant"], montant)
    r["synthese"].sort(key=lambda x: -x["nb_references"])
    r["totaux"] = {"nb_references": nb, "nb_recus": recus, "montant": montant}
    r["tarifs"] = store.info_tarifs()
    r["tronque"] = r["total_lignes"] > len(r["lignes"])
    return r


def controle(date_debut: date, date_fin: date, centre: str | None = None) -> list[dict]:
    lignes = _lire("controle", date_debut, date_fin, centre)
    resultat = []
    for l in lignes:
        l = dict(l)
        l["a_verser_jour"] = round(l["recap_especes"] + l["recap_cheques"] - l["depenses"], 3)
        l["verse_jour"] = round(l["recap_vers_especes"] + l["recap_vers_cheques"], 3)
        l["ecart_jour"] = round(l["a_verser_jour"] - l["verse_jour"], 3)
        l["ecart_sources_especes"] = round(l["recus_especes"] - l["recap_especes"], 3)
        l["ecart_sources_versement"] = round(l["verse_especes_valide"] - l["recap_vers_especes"], 3)

        anomalies = []
        if abs(l["rupture_report"]) >= TOLERANCE:
            anomalies.append("RUPTURE")
        if l["ecart_jour"] >= TOLERANCE:
            anomalies.append("JOUR_NON_VERSE")
        if l["solde"] >= TOLERANCE:
            anomalies.append("NON_VERSE")
        if l["solde"] <= -TOLERANCE:
            anomalies.append("SUR_VERSE")
        if l["verse_especes_non_valide"] > 0:
            anomalies.append("NON_VALIDE")
        if (l["nb_caisses"] > 0 and (
                abs(l["ecart_sources_especes"]) >= TOLERANCE
                or abs(l["ecart_sources_versement"]) >= TOLERANCE)) or (
                l["nb_caisses"] == 0 and l["recus_total"] > 0):
            anomalies.append("ECART_SOURCES")

        code = next((c for c in _PRIORITE if c in anomalies), "OK")
        l["statut_code"] = code
        l["statut"] = LIBELLES_STATUT[code]
        l["anomalies"] = ", ".join(LIBELLES_STATUT[c] for c in anomalies)
        resultat.append(l)
    resultat.sort(key=lambda l: (l["jour"] or "", _cle_centre(l["centre"])))
    return resultat


def synthese(lignes: list[dict]) -> dict:
    """Indicateurs calculés à partir des lignes du contrôle."""
    def somme(champ):
        return round(sum(l[champ] for l in lignes), 3)

    especes = somme("recap_especes")
    cheques = somme("recap_cheques")
    virements = somme("recap_virements")
    encaisse = round(especes + cheques + virements, 3)
    depenses = somme("depenses")
    verse_especes = somme("recap_vers_especes")
    verse_cheques = somme("recap_vers_cheques")
    a_verser = round(especes + cheques - depenses, 3)
    verse = round(verse_especes + verse_cheques, 3)

    # Le solde est un stock : on retient celui du dernier jour de chaque centre.
    dernier: dict[str, dict] = {}
    for l in lignes:
        if l["nb_caisses"] == 0:
            continue
        actuel = dernier.get(l["centre"])
        if actuel is None or (l["jour"] or "") >= (actuel["jour"] or ""):
            dernier[l["centre"]] = l
    soldes = [
        {"centre": c, "nom_centre": l["nom_centre"], "jour": l["jour"], "solde": l["solde"]}
        for c, l in dernier.items()
    ]
    positifs = [s for s in soldes if s["solde"] >= TOLERANCE]
    negatifs = [s for s in soldes if s["solde"] <= -TOLERANCE]

    centres_total = len({l["centre"] for l in lignes})
    centres_anomalie = len({l["centre"] for l in lignes if l["statut_code"] != "OK"})
    lignes_rupture = [l for l in lignes if abs(l["rupture_report"]) >= TOLERANCE]

    non_valide = somme("verse_especes_non_valide")
    nb_non_valides = int(sum(l["nb_versements_non_valides"] for l in lignes))
    nb_versements = int(sum(l["nb_versements"] for l in lignes))
    montant_annule = somme("montant_annule")
    nb_annulations = int(sum(l["nb_annulations"] for l in lignes))
    nb_recus = int(sum(l["nb_recus"] for l in lignes))

    repartition_statuts = []
    for code in ["OK"] + _PRIORITE:
        n = sum(1 for l in lignes if l["statut_code"] == code)
        repartition_statuts.append({
            "code": code, "libelle": LIBELLES_STATUT[code],
            "nb": n, "pct": _pct(n, len(lignes)),
        })

    soldes.sort(key=lambda s: abs(s["solde"]), reverse=True)
    return {
        "nb_lignes": len(lignes),
        "nb_recus": nb_recus,
        "encaisse": encaisse,
        "especes": especes, "pct_especes": _pct(especes, encaisse),
        "cheques": cheques, "pct_cheques": _pct(cheques, encaisse),
        "virements": virements, "pct_virements": _pct(virements, encaisse),
        "depenses": depenses,
        "a_verser": a_verser,
        "verse": verse, "pct_verse": _pct(verse, a_verser),
        "verse_especes": verse_especes, "pct_verse_especes": _pct(verse_especes, especes),
        "verse_cheques": verse_cheques, "pct_verse_cheques": _pct(verse_cheques, cheques),
        "solde_positif": round(sum(s["solde"] for s in positifs), 3),
        "nb_centres_solde_positif": len(positifs),
        "pct_centres_solde_positif": _pct(len(positifs), len(soldes)),
        "solde_negatif": round(sum(s["solde"] for s in negatifs), 3),
        "nb_centres_solde_negatif": len(negatifs),
        "pct_centres_solde_negatif": _pct(len(negatifs), len(soldes)),
        "centres_total": centres_total,
        "centres_anomalie": centres_anomalie,
        "pct_centres_anomalie": _pct(centres_anomalie, centres_total),
        "nb_ruptures": len(lignes_rupture),
        "montant_ruptures": round(sum(abs(l["rupture_report"]) for l in lignes_rupture), 3),
        "nb_versements": nb_versements,
        "nb_versements_non_valides": nb_non_valides,
        "pct_nb_versements_non_valides": _pct(nb_non_valides, nb_versements),
        "verse_non_valide": non_valide,
        "pct_verse_non_valide": _pct(non_valide, non_valide + somme("verse_especes_valide")),
        "nb_annulations": nb_annulations,
        "pct_nb_annulations": _pct(nb_annulations, nb_annulations + nb_recus),
        "montant_annule": montant_annule,
        "pct_montant_annule": _pct(montant_annule, montant_annule + encaisse),
        "repartition_statuts": repartition_statuts,
        "soldes_par_centre": soldes,
    }


def _trier(lignes: list[dict], *champs: str) -> list[dict]:
    return sorted(
        lignes,
        key=lambda l: (_cle_centre(l.get("centre")),) + tuple(str(l.get(c) or "") for c in champs),
    )


def _assurer_encaissements(date_debut: date, date_fin: date):
    """Charge les encaissements de la période.

    Chaque reçu est rattaché au centre de l'abonné, que seule la requête
    complète peut déduire : la période est donc toujours chargée pour tous les
    centres, et le centre choisi est filtré dans la base locale."""
    _assurer("encaissements", date_debut, date_fin, None)


def _sans_agent_epaiement(lignes: list[dict]) -> list[dict]:
    """Un e-paiement ne passe par aucun agent : ni matricule ni nom ne sont
    affichés (la base y range l'abréviation de la banque, déjà donnée par la
    colonne Banque)."""
    for l in lignes:
        if (l.get("mode_paiement") or "").strip().lower() == "e-paiement":
            l["matricule"] = None
            l["nom_agent"] = None
    return lignes


def encaissements(date_debut: date, date_fin: date, centre: str | None = None,
                  zone: str | None = None, segment: str | None = None) -> list[dict]:
    """Encaissements de tous les modes de paiement, comme l'état du CRM.
    `zone` : groupe de zones (Nouakchott, Nouakchott + Nouadhibou) ;
    `segment` : privés, État ou tous."""
    if date_fin < date_debut:
        date_fin = date_debut
    centre = str(centre).strip() if centre else None
    _assurer_encaissements(date_debut, date_fin)
    _assurer_personnel()
    lignes = _sans_agent_epaiement(store.lire("encaissements", date_debut, date_fin, centre,
                                              centres=zones.centres_du_groupe(zone), segment=segment))
    return _trier(lignes, "date_comptable", "num_recu")


# Nombre maximal de reçus envoyés à l'écran. Une année pour tous les centres
# représente plusieurs centaines de milliers de reçus : les envoyer tous au
# navigateur bloquait le serveur pendant de longues minutes. Le nombre de lignes
# et le total, eux, portent toujours sur la sélection entière, et l'export
# contient toutes les lignes.
LIMITE_ECRAN = 50000


def encaissements_ecran(date_debut: date, date_fin: date, centre: str | None = None,
                        recherche: str | None = None, zone: str | None = None,
                        segment: str | None = None) -> dict:
    """Encaissements pour l'affichage : recherche, tri et coupe faits par DuckDB."""
    if date_fin < date_debut:
        date_fin = date_debut
    centre = str(centre).strip() if centre else None
    _assurer_encaissements(date_debut, date_fin)
    _assurer_personnel()
    # Par statut : l'état du CRM, et donc le total, ne comptent que les validés.
    dans_zone = zones.centres_du_groupe(zone)
    repartition = store.repartir("encaissements", "statut", "montant", date_debut, date_fin, centre, recherche,
                                 centres=dans_zone, segment=segment)
    nb = sum(n for _, n, _ in repartition)
    montant_tout = sum(m for _, _, m in repartition)
    ordre = {"VALIDE": 0, "ANNULE": 1, "NON VALIDE": 2}
    statuts = [
        {"statut": s, "nb": n, "pct_nb": _pct(n, nb), "montant": m, "pct_montant": _pct(m, montant_tout)}
        for s, n, m in sorted(repartition, key=lambda r: ordre.get(r[0], 9))
    ]
    valide = next((m for s, _, m in repartition if s == "VALIDE"), 0.0)
    lignes = store.lire(
        "encaissements", date_debut, date_fin, centre, recherche=recherche, limite=LIMITE_ECRAN,
        ordre="TRY_CAST(centre AS BIGINT) NULLS LAST, centre, date_comptable, num_recu", centres=dans_zone,
        segment=segment,
    )
    return {
        "lignes": _sans_agent_epaiement(lignes),
        "total_lignes": nb,
        "totaux": {"montant": valide},
        "statuts": statuts,
        "limite": LIMITE_ECRAN,
        "tronque": nb > len(lignes),
    }


def versements(date_debut: date, date_fin: date, centre: str | None = None) -> list[dict]:
    _assurer_personnel()
    lignes = _lire("versements", date_debut, date_fin, centre)
    return _trier(lignes, "date_versement", "num_versement")


def cheques(date_debut: date, date_fin: date, centre: str | None = None) -> list[dict]:
    lignes = _lire("cheques", date_debut, date_fin, centre)
    return _trier(lignes, "date_comptable", "num_recu")


def annulations(date_debut: date, date_fin: date, centre: str | None = None) -> list[dict]:
    lignes = _lire("annulations", date_debut, date_fin, centre)
    return _trier(lignes, "date_recu", "num_recu")


def non_valides(date_debut: date, date_fin: date, centre: str | None = None) -> list[dict]:
    """Reçus non validés à contrôler : le compte du client est crédité, mais
    l'argent n'est pas dans le versement de la caisse le jour du reçu.

    Sont écartés : les reçus sans crédit au compte client, ceux d'un jour où
    la caisse a versé plus que ses reçus validés (l'argent y est déposé), et
    ceux d'une journée que la caisse n'a pas encore validée.

    Un reçu naît en effet « non validé » et le reste jusqu'à la validation de
    la journée par le caissier : tant que la caisse n'a aucun reçu validé ce
    jour-là, ses reçus non validés sont normaux. Le cas à contrôler est le reçu
    resté non validé alors que les autres reçus de la caisse ont été validés."""
    resultat = []
    for l in _lire("non_valides", date_debut, date_fin, centre):
        l = dict(l)
        if not (l["caissier_jour"] or "").strip():
            continue  # journée pas encore validée par la caisse
        verse_en_plus = l["verse_especes"] - l["especes_validees"] - max(l["report"], 0.0)
        if l["credite"] <= 0 or verse_en_plus > TOLERANCE:
            continue
        l["constat"] = ("Aucun versement ce jour" if l["verse_especes"] == 0
                        else "Versement du jour égal aux reçus validés")
        postes = [p.strip() for p in (l["postes_caissier"] or "").split(",") if p.strip()]
        if not l["poste_recu"] or not postes:
            l["meme_poste"] = None
        else:
            l["meme_poste"] = "Oui" if l["poste_recu"] in postes else "Non"
        resultat.append(l)
    # Les plus récents d'abord : c'est une liste de surveillance.
    resultat.sort(key=lambda l: (l["date_recu"] or "", l["heure"] or "", l["num_recu"] or ""), reverse=True)
    return resultat


# L'alerte surveille les reçus des N derniers jours, tous centres confondus.
# Les trois derniers jours sont relus dans Oracle à chaque rafraîchissement ;
# les autres le sont une fois, ce qui couvre un arrêt de l'application.
JOURS_ALERTE = 31


def alerte_non_valides() -> dict:
    """Reçus non validés à contrôler qui n'ont pas encore été consultés."""
    fin = date.today()
    debut = fin - timedelta(days=JOURS_ALERTE - 1)
    vus = store.recus_vus()
    cas = non_valides(debut, fin, None)
    # Un reçu consulté qui n'est plus (ou pas encore) un cas à contrôler est
    # oublié : s'il le devient plus tard, l'alerte doit le signaler.
    perimes = vus - {l["num_recu"] for l in cas}
    if perimes:
        store.oublier_vus(sorted(perimes))
    nouveaux = [l for l in cas if l["num_recu"] not in vus]
    jours = sorted(l["date_recu"] for l in nouveaux if l["date_recu"])
    return {
        "nb": len(nouveaux),
        "montant": round(sum(l["montant"] or 0 for l in nouveaux), 3),
        "debut": jours[0] if jours else None,
        "fin": jours[-1] if jours else None,
        "centres": sorted({l["centre"] for l in nouveaux}, key=_cle_centre),
        "recus": [l["num_recu"] for l in nouveaux],
        "jours_surveilles": JOURS_ALERTE,
    }


def marquer_non_valides_vus(numeros: list[str]):
    """Note des reçus non validés comme consultés : l'alerte ne les signale plus."""
    store.marquer_vus([str(n) for n in numeros])


def reports(date_debut: date, date_fin: date, centre: str | None = None) -> list[dict]:
    lignes = _lire("reports", date_debut, date_fin, centre)
    resultat = []
    for l in lignes:
        l = dict(l)
        rupture = l.get("rupture")
        if rupture is not None and abs(rupture) >= TOLERANCE:
            l["constat"] = "Rupture : le report ne reprend pas le solde précédent"
        elif l["solde"] >= TOLERANCE:
            l["constat"] = "Solde non versé"
        elif l["solde"] <= -TOLERANCE:
            l["constat"] = "Sur-versement"
        else:
            l["constat"] = "Conforme"
        resultat.append(l)
    return _trier(resultat, "jour", "num_caisse")


def rapprochement(date_debut: date, date_fin: date, centre: str | None = None) -> dict:
    """Opérations à comparer au rapprochement bancaire : versements d'espèces
    et dépôts de chèques, présentés par banque, date et montant."""
    operations = []
    for v in versements(date_debut, date_fin, centre):
        operations.append({
            "date_operation": v["date_versement"],
            "type_operation": "VERSEMENT ESPECES",
            "code_banque": v["code_banque"],
            "nom_banque": v["nom_banque"],
            "compte": v["compte"],
            "centre": v["centre"],
            "nom_centre": v["nom_centre"],
            "num_caisse": v["num_caisse"],
            "matricule": v["matricule"],
            "num_piece": v["num_versement"],
            "num_cheque": None,
            "tireur": None,
            "reference": None,
            "montant": v["montant"],
            "statut": v["statut"],
        })
    for c in cheques(date_debut, date_fin, centre):
        depose = bool(c["date_depot"] and c["code_banque_depot"])
        operations.append({
            "date_operation": c["date_depot"] or c["date_comptable"],
            "type_operation": "REMISE CHEQUE",
            "code_banque": c["code_banque_depot"],
            "nom_banque": c["nom_banque_depot"],
            "compte": c["compte_depot"],
            "centre": c["centre"],
            "nom_centre": c["nom_centre"],
            "num_caisse": c["num_caisse"],
            "matricule": c["matricule"],
            "num_piece": c["num_recu"],
            "num_cheque": c["num_cheque"],
            "tireur": c["tireur"],
            "reference": c["reference"],
            "montant": c["montant"],
            "statut": "DEPOSE" if depose else "SANS DEPOT",
        })
    operations.sort(key=lambda o: (
        str(o["code_banque"] or "~"), str(o["compte"] or ""), str(o["date_operation"] or ""),
        o["type_operation"], _cle_centre(o["centre"]), str(o["num_piece"] or ""),
    ))

    # Synthèse par banque, compte et jour : seules les opérations abouties comptent.
    retenues = [o for o in operations if o["statut"] in ("VALIDE", "DEPOSE")]
    total = sum(o["montant"] or 0 for o in retenues)
    groupes: dict[tuple, dict] = {}
    for o in retenues:
        cle = (o["code_banque"], o["nom_banque"], o["compte"], o["date_operation"])
        g = groupes.setdefault(cle, {
            "code_banque": o["code_banque"], "nom_banque": o["nom_banque"],
            "compte": o["compte"], "date_operation": o["date_operation"],
            "nb_operations": 0, "especes": 0.0, "cheques": 0.0, "total": 0.0,
        })
        g["nb_operations"] += 1
        champ = "especes" if o["type_operation"] == "VERSEMENT ESPECES" else "cheques"
        g[champ] += o["montant"] or 0
        g["total"] += o["montant"] or 0
    banques = []
    for g in groupes.values():
        g["especes"] = round(g["especes"], 3)
        g["cheques"] = round(g["cheques"], 3)
        g["total"] = round(g["total"], 3)
        g["pct_total"] = _pct(g["total"], total)
        banques.append(g)
    banques.sort(key=lambda g: (str(g["code_banque"] or "~"), str(g["compte"] or ""),
                                str(g["date_operation"] or "")))
    return {"operations": operations, "banques": banques}


# --------------------------------------------------------------------------- relevé bancaire
def importer_releve(nom_fichier: str, contenu: bytes) -> dict:
    """Lit le relevé envoyé et l'enregistre dans la base locale."""
    lignes = releve.lire_fichier(contenu)
    ajout = store.ajouter_releve(nom_fichier, lignes)
    info = info_releve()
    info["ajout"] = ajout
    return info


def supprimer_releve():
    """Efface le relevé importé : l'onglet revient à son état de départ."""
    store.supprimer_releve()


def info_releve() -> dict | None:
    info = store.info_releve()
    if info is None:
        return None
    connues = set(releve.GROUPES_RELEVE.values())
    banques = store.banques_releve()
    info["banques"] = banques
    # Banques du relevé avec des versements d'espèces mais sans équivalent dans le CRM.
    info["banques_inconnues"] = [b["banque"] for b in banques
                                 if b["groupe"] not in connues and b["nb_versements"] > 0]
    return info


def rapprochement_releve(date_debut: date, date_fin: date, centre: str | None = None) -> dict:
    """Croise les versements validés du CRM avec le relevé importé."""
    if date_fin < date_debut:
        date_fin = date_debut
    centre = str(centre).strip() if centre else None
    info = info_releve()
    if info is None:
        return {"releve": None}
    # La période rapprochée est celle du relevé. La période choisie en haut de
    # la page ne sert qu'à la réduire (un mois dans un relevé annuel) ; si elle
    # est en dehors du relevé, on prend tout le relevé.
    releve_debut = date.fromisoformat(info["date_min"])
    releve_fin = date.fromisoformat(info["date_max"])
    debut, fin = max(date_debut, releve_debut), min(date_fin, releve_fin)
    if debut > fin:
        debut, fin, origine = releve_debut, releve_fin, "releve"
    elif (debut, fin) == (date_debut, date_fin):
        origine = "page"
    else:
        origine = "partielle"
    date_debut, date_fin = debut, fin
    # Marges : un versement de fin de mois peut être déposé au début du suivant.
    d1 = date_debut - timedelta(days=releve.MARGE_CRM_AVANT)
    d2 = min(date_fin + timedelta(days=releve.MARGE_CRM_APRES), date.today())
    d2 = max(d2, date_fin)
    crm = versements(d1, d2, None)
    lignes = store.lire_releve(date_debut - timedelta(days=releve.MARGE_RELEVE),
                               date_fin + timedelta(days=releve.MARGE_RELEVE))
    resultat = releve.bilan(crm, lignes, date_debut, date_fin, centre, couverture=(releve_debut, releve_fin))
    resultat["releve"] = info
    resultat["periode"] = {"debut": date_debut.isoformat(), "fin": date_fin.isoformat(), "origine": origine}
    resultat["centre"] = centre
    return resultat


# --------------------------------------------------------------------------- croisement avec la DFC
def importer_dfc(nom_fichier: str, contenu: bytes) -> dict:
    """Lit le classeur d'un centre saisi par la DFC et l'enregistre. Le centre
    est le numéro contenu dans le nom du fichier (« 51.xlsx »)."""
    try:
        connus = {str(c["centre"]).strip() for c in centres()}
    except db.BaseIndisponible:
        connus = set()
    centre = dfc.centre_du_nom(nom_fichier, connus)
    lu = dfc.lire_fichier(contenu)
    ajout = store.remplacer_dfc(centre, nom_fichier, lu["jours"], lu["couverture"])
    info = store.info_dfc()
    info["ajout"] = ajout
    return info


def supprimer_dfc():
    store.supprimer_dfc()


def croisement_dfc(date_debut: date, date_fin: date, centre: str | None = None) -> dict:
    """Saisie de la DFC comparée au CRM, pour les centres dont un fichier a été importé.

    Le croisement porte sur les périodes couvertes par les fichiers importés,
    jusqu'à aujourd'hui : l'année pour un fichier annuel, un seul mois pour un
    fichier d'un mois. Les totaux et le tableau par mois portent sur toute
    cette période, pour qu'un mois non saisi se voie tout de suite quelle que
    soit la période choisie en haut de la page ; le détail par jour porte sur
    la période de la page."""
    if date_fin < date_debut:
        date_fin = date_debut
    centre = str(centre).strip() if centre else None
    info = store.info_dfc()
    if info is None:
        return {"dfc": None}
    importes = set(info["centres"])
    periode = {"debut": date_debut.isoformat(), "fin": date_fin.isoformat()}
    if centre and centre not in importes:
        return {"dfc": info, "centre": centre, "centre_sans_fichier": True, "periode": periode,
                "couverture": None, "lignes": [], **dfc.resumer([], TOLERANCE)}
    aujourd_hui = date.today()
    couvertures = {
        c: [(d, min(f, aujourd_hui)) for d, f in periodes if d <= aujourd_hui]
        for c, periodes in store.couvertures_dfc(centre).items()
    }
    bornes = [b for periodes in couvertures.values() for b in periodes]
    if not bornes:   # fichiers entièrement dans le futur
        return {"dfc": info, "centre": centre, "centre_sans_fichier": False, "periode": periode,
                "couverture": None, "lignes": [], **dfc.resumer([], TOLERANCE)}
    debut, fin = min(d for d, _ in bornes), max(f for _, f in bornes)

    def couvert(code_centre, jour) -> bool:
        """Vrai si un fichier importé pour ce centre couvre ce jour."""
        j = str(jour)[:10]
        return any(d.isoformat() <= j <= f.isoformat() for d, f in couvertures.get(str(code_centre), ()))

    saisies = [l for l in store.lire_dfc(debut, fin, centre) if couvert(l["centre"], l["jour"])]
    # Un seul centre importé : inutile de lire tous les centres dans Oracle.
    cible = centre or (next(iter(importes)) if len(importes) == 1 else None)
    lignes_controle = [l for l in controle(debut, fin, cible) if couvert(l["centre"], l["jour"])]
    lignes_versements = [l for l in versements(debut, fin, cible) if couvert(l["centre"], l["date_versement"])]
    try:
        noms = {str(c["centre"]).strip(): c["nom_centre"] for c in centres()}
    except db.BaseIndisponible:
        noms = {}
    toutes = dfc.croiser_jours(saisies, lignes_controle, lignes_versements, noms, TOLERANCE)
    ensemble = dfc.resumer(toutes, TOLERANCE)
    jours = [l for l in toutes if periode["debut"] <= l["jour"] <= periode["fin"]]
    detail = dfc.resumer(jours, TOLERANCE)
    return {
        "dfc": info, "centre": centre, "centre_sans_fichier": False,
        "couverture": {"debut": debut.isoformat(), "fin": fin.isoformat()},
        "totaux": ensemble["totaux"], "mois": ensemble["mois"],
        "periode": periode, "synthese": detail["synthese"], "totaux_periode": detail["totaux"], "lignes": jours,
        "toutes_les_lignes": toutes,
    }


# --------------------------------------------------------------------------- évolution mensuelle
NB_MOIS_HISTORIQUE = 12


def _garder_zone(valeurs: dict, zone: str | None, par_cle=False) -> dict:
    """Ne garde que les centres du groupe de zones (toute la Mauritanie sinon)."""
    codes = zones.centres_du_groupe(zone)
    if codes is None:
        return valeurs
    if par_cle:
        return {k: v for k, v in valeurs.items() if k[0] in codes}
    return {c: v for c, v in valeurs.items() if c in codes}


def evolution_encaissements(mois: str, centre: str | None = None, zone: str | None = None,
                            segment: str | None = None) -> dict:
    """Encaissé de chaque centre pour un mois, comparé au mois précédent et à la
    moyenne des trois mois d'avant, avec l'historique des douze derniers mois.

    Le mois en cours est comparé à date : du 1er au même jour dans chaque mois
    précédent. Les parts (%) portent toujours sur tous les centres."""
    centre = str(centre).strip() if centre else None
    aujourd_hui = date.today()
    annee, m = evolution.mois_de(mois)
    if (annee, m) > (aujourd_hui.year, aujourd_hui.month):
        annee, m = aujourd_hui.year, aujourd_hui.month
    en_cours = (annee, m) == (aujourd_hui.year, aujourd_hui.month)
    jour_max = aujourd_hui.day if en_cours else None
    a0, m0 = evolution.decaler(annee, m, -(NB_MOIS_HISTORIQUE - 1))
    debut = date(a0, m0, 1)
    fin = evolution.bornes(annee, m, jour_max)[1]
    # Tous les centres : chaque reçu est rattaché au centre de l'abonné.
    _assurer_encaissements(debut, fin)

    def periode(n):
        return evolution.bornes(*evolution.decaler(annee, m, n), jour_max)

    actuel = _garder_zone(store.encaisse_par_centre(*periode(0), segment), zone)
    precedent = _garder_zone(store.encaisse_par_centre(*periode(-1), segment), zone)
    avant = [_garder_zone(store.encaisse_par_centre(*periode(-k), segment), zone) for k in (1, 2, 3)]
    par_mois, noms = store.encaisse_par_mois(debut, fin, segment)
    par_mois = _garder_zone(par_mois, zone, par_cle=True)
    try:
        noms.update({str(c["centre"]).strip(): c["nom_centre"] for c in centres()})
    except db.BaseIndisponible:
        pass
    lignes = evolution.comparer(actuel, precedent, avant, noms)
    resume_tous = evolution.resumer(lignes)
    if centre:
        lignes = [l for l in lignes if l["centre"] == centre]
    liste_mois = [evolution.cle(*evolution.decaler(annee, m, -k)) for k in range(NB_MOIS_HISTORIQUE - 1, -1, -1)]
    codes = sorted({c for c, _ in par_mois}, key=_cle_centre)
    ap, mp = evolution.decaler(annee, m, -1)
    return {
        "mois": evolution.cle(annee, m), "libelle": evolution.libelle(annee, m),
        "mois_precedent": evolution.cle(ap, mp), "libelle_precedent": evolution.libelle(ap, mp),
        "en_cours": en_cours, "jour_max": jour_max,
        "periode": {"debut": periode(0)[0].isoformat(), "fin": periode(0)[1].isoformat()},
        "periode_precedente": {"debut": periode(-1)[0].isoformat(), "fin": periode(-1)[1].isoformat()},
        "seuils": {"chute": evolution.SEUIL_CHUTE, "baisse": evolution.SEUIL_BAISSE, "hausse": evolution.SEUIL_HAUSSE},
        "centre": centre,
        "lignes": lignes,
        "synthese": resume_tous["synthese"] if not centre else evolution.resumer(lignes)["synthese"],
        "totaux": resume_tous["totaux"],
        "historique": evolution.historique(par_mois, liste_mois, codes, noms),
    }


def evolution_annee(annee: int, centre: str | None = None, zone: str | None = None,
                    segment: str | None = None) -> dict:
    """Encaissé de chaque centre mois par mois sur une année, avec la variation
    de chaque mois sur le précédent. Le mois en cours est comparé au mois
    précédent arrêté au même jour."""
    centre = str(centre).strip() if centre else None
    aujourd_hui = date.today()
    annee = min(annee, aujourd_hui.year)
    dernier_mois = 12 if annee < aujourd_hui.year else aujourd_hui.month
    debut = date(annee - 1, 12, 1)
    fin = min(date(annee, 12, 31), aujourd_hui)
    _assurer_encaissements(debut, fin)
    par_mois, noms = store.encaisse_par_mois(debut, fin, segment)
    par_mois = _garder_zone(par_mois, zone, par_cle=True)
    try:
        noms.update({str(c["centre"]).strip(): c["nom_centre"] for c in centres()})
    except db.BaseIndisponible:
        pass
    a_date = total_a_date = None
    if annee == aujourd_hui.year:
        ap, mp = evolution.decaler(annee, dernier_mois, -1)
        precedent = store.encaisse_par_centre(*evolution.bornes(ap, mp, aujourd_hui.day), segment)
        a_date = {c: m for c, (_, m) in _garder_zone(precedent, zone).items()}
        total_a_date = sum(a_date.values())
    r = evolution.annee(par_mois, annee, dernier_mois, a_date, total_a_date, noms, [centre] if centre else None)
    r["centre"] = centre
    r["jour_en_cours"] = aujourd_hui.day if a_date is not None else None
    return r


EL_MINA_1, EL_MINA_2 = "94", "56"


def _fusionner_el_mina(valeurs: dict[str, tuple[int, float]]) -> dict[str, tuple[int, float]]:
    """El Mina 2 (56) compté avec El Mina 1 (94), comme dans le rapport des objectifs."""
    if EL_MINA_2 not in valeurs:
        return valeurs
    sortie = dict(valeurs)
    n2, m2 = sortie.pop(EL_MINA_2)
    n1, m1 = sortie.get(EL_MINA_1, (0, 0.0))
    sortie[EL_MINA_1] = (n1 + n2, m1 + m2)
    return sortie


def objectifs_mois(mois: str, centre: str | None = None, zone: str | None = None) -> dict:
    """Encaissé de chaque centre sur un mois comparé à son objectif mensuel.

    Pour le mois en cours, `attendu_pct` est la part du mois déjà écoulée :
    un centre dont le taux d'atteinte est au-dessus est en avance sur son objectif."""
    import calendar
    centre = str(centre).strip() if centre else None
    aujourd_hui = date.today()
    annee, m = evolution.mois_de(mois)
    if (annee, m) > (aujourd_hui.year, aujourd_hui.month):
        annee, m = aujourd_hui.year, aujourd_hui.month
    nb_jours = calendar.monthrange(annee, m)[1]
    en_cours = (annee, m) == (aujourd_hui.year, aujourd_hui.month)
    debut, fin = evolution.bornes(annee, m, aujourd_hui.day if en_cours else None)
    _assurer_encaissements(debut, fin)
    # Comme le rapport REALISATION_DES_OBJECTIFS : l'objectif se compare aux
    # encaissements privés (sans l'État), El Mina 2 compté avec El Mina 1.
    encaisse = _garder_zone(_fusionner_el_mina(store.encaisse_par_centre(debut, fin, "prives")), zone)
    total = _garder_zone(_fusionner_el_mina(store.encaisse_par_centre(debut, fin)), zone)
    try:
        noms = {str(c["centre"]).strip(): c["nom_centre"] for c in centres()}
    except db.BaseIndisponible:
        noms = {}
    codes_zone = zones.centres_du_groupe(zone)
    attendu = round(100.0 * aujourd_hui.day / nb_jours, 1) if en_cours else 100.0
    table_objectifs, source_objectifs = objectifs.pour_mois(evolution.cle(annee, m))
    codes = {c for c in table_objectifs if codes_zone is None or c in codes_zone} | set(encaisse) | set(total)
    if centre:
        codes = {centre} & (codes | {centre})
    lignes = []
    for c in codes:
        zone_obj, nom_obj, objectif = table_objectifs.get(c, (None, None, None))
        nb, montant = encaisse.get(c, (0, 0.0))
        montant_total = total.get(c, (0, 0.0))[1]
        montant_etat = montant_total - montant
        if source_objectifs == "Modèle":
            # Les objectifs du modèle portent sur tous les encaissements (privés + État).
            montant = montant_total
        if not objectif and not montant_total:
            continue
        taux = round(100.0 * montant / objectif, 1) if objectif else None
        if taux is None:
            statut = "Sans objectif"
        elif taux >= 100:
            statut = "Objectif atteint"
        elif en_cours and taux >= attendu:
            statut = "En avance"
        elif en_cours:
            statut = "En retard"
        else:
            statut = "Non atteint"
        lignes.append({
            "zone": zone_obj or zones.zone_du_centre(c), "centre": c, "nom_centre": noms.get(c) or nom_obj,
            "objectif": round(objectif) if objectif else None, "encaisse": round(montant, 3),
            "taux_atteinte": taux, "attendu_pct": attendu if objectif else None,
            "reste": round(max(objectif - montant, 0.0)) if objectif else None,
            "depassement": round(max(montant - objectif, 0.0), 3) if objectif else None,
            "nb_recus": nb, "statut": statut,
            "etat": round(montant_etat, 3), "total": round(montant_total, 3),
        })
    lignes.sort(key=lambda l: (l["taux_atteinte"] is None, l["taux_atteinte"] or 0))
    total_obj = sum(l["objectif"] or 0 for l in lignes)
    total_enc = sum(l["encaisse"] for l in lignes if l["objectif"])
    statuts = {}
    for l in lignes:
        statuts[l["statut"]] = statuts.get(l["statut"], 0) + 1
    return {
        "mois": evolution.cle(annee, m), "libelle": evolution.libelle(annee, m), "en_cours": en_cours,
        "jour": aujourd_hui.day if en_cours else nb_jours, "nb_jours": nb_jours, "attendu_pct": attendu,
        "centre": centre, "lignes": lignes, "source_objectifs": source_objectifs,
        "totaux": {"objectif": round(total_obj, 2), "encaisse": round(total_enc, 3),
                   "etat": round(sum(l["etat"] for l in lignes), 3), "total": round(sum(l["total"] for l in lignes), 3),
                   "taux_atteinte": round(100.0 * total_enc / total_obj, 1) if total_obj else None,
                   "nb_centres": len(lignes),
                   "statuts": [{"statut": k, "nb": v, "pct": round(100.0 * v / len(lignes), 1)} for k, v in statuts.items()]},
    }


# --------------------------------------------------------------------------- reçus manuels
def importer_manuels(nom_fichier: str, contenu: bytes) -> dict:
    """Lit le fichier des reçus manuels (cahier des centres) et l'enregistre."""
    recus = manuels.lire_fichier(contenu, nom_fichier)
    ajout = store.remplacer_manuels(nom_fichier, recus)
    info = store.info_manuels()
    info["ajout"] = ajout
    return info


def supprimer_manuels():
    store.supprimer_manuels()


def verification_manuels() -> dict:
    """Tous les reçus manuels importés comparés aux encaissements en espèces du
    CRM, de quelques jours avant le premier reçu jusqu'à aujourd'hui. L'onglet a
    ses propres filtres : la période et le centre du haut de la page ne
    s'appliquent pas."""
    info = store.info_manuels()
    if info is None:
        return {"manuels": None}
    try:
        noms = {str(c["centre"]).strip(): c["nom_centre"] for c in centres()}
    except db.BaseIndisponible:
        noms = {}
    recus = store.lire_manuels()
    for r in recus:
        r["centre"] = manuels.centre_code(r["centre_fichier"], noms)
        r["nom_centre"] = noms.get(r["centre"])
        r["reference"] = r["reference"] or ""
        r["num_recu_manuel"] = r["num_recu_manuel"] or ""
    dates = [r["date_recu"] for r in recus if r["date_recu"]]
    aujourd_hui = date.today()
    if dates:
        debut = min(min(dates) - timedelta(days=manuels.JOURS_AVANT), aujourd_hui)
    else:
        debut = date(aujourd_hui.year, 1, 1)
    _assurer_encaissements(debut, aujourd_hui)
    _assurer_personnel()
    # Seuls les reçus du CRM des références du fichier sont lus dans la base locale.
    crm = store.especes_des_references(debut, aujourd_hui, [r["reference"] for r in recus if r["reference"]])
    lignes = manuels.rapprocher(recus, crm)
    base = store.base_especes(min(dates) if dates else debut, max(dates) if dates else aujourd_hui,
                              [r["centre"] for r in recus if r["centre"]])
    mal_saisies = manuels.references_mal_saisies(lignes, base["invalides"])
    resume = manuels.resumer(lignes, base, mal_saisies)
    return {
        "manuels": info,
        "fenetre_crm": {"debut": debut.isoformat(), "fin": aujourd_hui.isoformat()},
        "synthese": resume["synthese"], "totaux": resume["totaux"], "lignes": lignes,
        "references_mal_saisies": mal_saisies,
    }


JEUX = ("controle", "encaissements", "versements", "cheques", "reports",
        "annulations", "non_valides", "rapprochement", "banques")


def jeu(nom: str, date_debut: date, date_fin: date, centre: str | None = None,
        zone: str | None = None, segment: str | None = None) -> list[dict]:
    """Retourne les lignes d'un jeu de données par son nom (utilisé par l'export)."""
    if nom == "controle":
        return controle(date_debut, date_fin, centre)
    if nom == "encaissements":
        return encaissements(date_debut, date_fin, centre, zone, segment)
    if nom == "versements":
        return versements(date_debut, date_fin, centre)
    if nom == "cheques":
        return cheques(date_debut, date_fin, centre)
    if nom == "reports":
        return reports(date_debut, date_fin, centre)
    if nom == "annulations":
        return annulations(date_debut, date_fin, centre)
    if nom == "non_valides":
        return non_valides(date_debut, date_fin, centre)
    if nom == "rapprochement":
        return rapprochement(date_debut, date_fin, centre)["operations"]
    if nom == "banques":
        return rapprochement(date_debut, date_fin, centre)["banques"]
    raise KeyError(nom)


# --------------------------------------------------------------------------- alerte chutes d'encaissement par secteur

SEUIL_CHUTE_PCT = -20

def alerte_chutes_secteur() -> dict:
    """Secteurs dont l'encaissement du mois courant (jusqu'à aujourd'hui)
    est en baisse d'au moins 20 % par rapport à la même tranche de M-2."""
    from datetime import date as _date
    aujourd_hui = _date.today()
    toutes = store.chutes_secteur(aujourd_hui)
    chutes = [l for l in toutes if l["variation_pct"] is not None and l["variation_pct"] <= SEUIL_CHUTE_PCT]

    mois_ref = aujourd_hui.month - 2
    annee_ref = aujourd_hui.year
    if mois_ref <= 0:
        mois_ref += 12
        annee_ref -= 1
    mois_ref_label = f"{annee_ref}-{mois_ref:02d}"
    mois_actuel_label = f"{aujourd_hui.year}-{aujourd_hui.month:02d}"

    return {
        "nb": len(chutes),
        "jour_compare": aujourd_hui.day,
        "mois_actuel": mois_actuel_label,
        "mois_reference": mois_ref_label,
        "secteurs": chutes,
        "total": toutes,
    }


def detail_chutes_secteur(secteur: str) -> dict:
    """Détail des abonnés d'un secteur pour la comparaison M vs M-2."""
    from datetime import date as _date
    aujourd_hui = _date.today()
    lignes = store.chutes_secteur_detail(aujourd_hui, secteur)

    mois_ref = aujourd_hui.month - 2
    annee_ref = aujourd_hui.year
    if mois_ref <= 0:
        mois_ref += 12
        annee_ref -= 1

    return {
        "secteur": secteur,
        "jour_compare": aujourd_hui.day,
        "mois_actuel": f"{aujourd_hui.year}-{aujourd_hui.month:02d}",
        "mois_reference": f"{annee_ref}-{mois_ref:02d}",
        "abonnes": lignes,
    }



# --------------------------------------------------------------------------- alertes par secteur (jour par jour)
# Chaque jour J du mois M, chaque secteur planifié en M est comparé au même jour
# de son mois de référence : M-1 pour un mensuel, M-2 pour un bimensuel
# (planning PLANIF_RELEVE / PLANIFDETAILLE). L'alerte porte sur l'encaissement
# du jour ; le cumul du 1er au jour J est donné à côté.
import calendar as _calendrier

SEUIL_ALERTE_CHUTE = -15.0      # % : alerte rouge (baisse de 15 % ou plus)
SEUIL_ALERTE_BAISSE = -15.0     # % : même seuil que la chute (plus de catégorie orange)
MONTANT_MIN_REFERENCE = 10000.0  # MRU : seuls les secteurs dont l'encaissé de référence est >= 10 000 sont comparés

ALERTE_CHUTE = "SECT_CHUTE"
ALERTE_BAISSE = "SECT_BAISSE"
ALERTE_NORMAL = "SECT_NORMAL"
ALERTE_SANS_REF = "SECT_SANS_REF"
LIBELLES_ALERTE = {
    ALERTE_CHUTE: "Chute",
    ALERTE_BAISSE: "Baisse",
    ALERTE_NORMAL: "Normal",
    ALERTE_SANS_REF: "Sans référence",
}
_ORDRE_ALERTE = [ALERTE_CHUTE, ALERTE_BAISSE, ALERTE_NORMAL, ALERTE_SANS_REF]

_verrou_planif = threading.Lock()
_planif = {"fil": None, "depuis": None, "echec": None, "erreur": None}


def _charger_planif():
    try:
        aujourd_hui = date.today()
        params = {"d0": datetime(aujourd_hui.year - 1, 1, 1), "a0": aujourd_hui.year - 1}
        lignes = db.fetch_all(queries.sql_planif_secteurs(get_settings().oracle_schema), params,
                              delai_ms=900_000)
        for l in lignes:
            l["centre"] = str(l["centre"]).strip() if l.get("centre") is not None else None
            l["secteur"] = str(l["secteur"]).strip() if l.get("secteur") is not None else None
        store.remplacer_planif(lignes)
        _planif["echec"], _planif["erreur"] = None, None
        log.info("Planning des secteurs chargé : %s lignes", len(lignes))
    except Exception as exc:
        _planif["echec"], _planif["erreur"] = time.monotonic(), str(exc)
        log.warning("Planning des secteurs non chargé : %s", exc)
    finally:
        _planif["fil"] = None


def _assurer_planif():
    """Charge une fois par jour le planning des secteurs, en arrière-plan
    (même fonctionnement que les tarifs)."""
    with _verrou_planif:
        if store.planif_a_jour():
            return
        connu = store.info_planif() is not None
        if _planif["fil"] is None:
            echec = _planif["echec"]
            if echec is None or time.monotonic() - echec > DELAI_PERSONNEL:
                _planif["depuis"] = time.monotonic()
                _planif["fil"] = threading.Thread(target=_charger_planif, name="planif", daemon=True)
                _planif["fil"].start()
            elif not connu:
                raise db.BaseIndisponible(f"Planning des secteurs non chargé : {_planif['erreur']}")
        if not connu:
            secondes = time.monotonic() - _planif["depuis"]
            exc = ChargementEnCours("planif", 0, 1, secondes)
            exc.args = (f"Lecture du planning des secteurs dans Oracle en cours, depuis {int(secondes)} s.",)
            raise exc


HEURE_RAPPORT = 23  # heure du rapport quotidien des chutes (journée considérée comme close)


def _jour_alerte(jour: date | None) -> date:
    """Jour calculé par défaut : aujourd'hui à partir de 23 h (journée close),
    sinon hier. Jamais dans le futur."""
    maintenant = datetime.now()
    aujourd_hui = maintenant.date()
    defaut = aujourd_hui if maintenant.hour >= HEURE_RAPPORT else aujourd_hui - timedelta(days=1)
    return min(jour or defaut, aujourd_hui)


def _mois_ref(annee: int, mois: int, frequence: str) -> tuple[int, int]:
    """Mois de référence : M-1 pour un mensuel, M-2 pour un bimensuel."""
    n = annee * 12 + (mois - 1) - (1 if frequence == "MENSUEL" else 2)
    return n // 12, n % 12 + 1


def _meme_jour(annee: int, mois: int, jour: int) -> date:
    """Même jour dans un autre mois (le 31 devient le dernier jour du mois)."""
    return date(annee, mois, min(jour, _calendrier.monthrange(annee, mois)[1]))


def _statut_alerte(reference: float, variation: float | None) -> str:
    if reference < MONTANT_MIN_REFERENCE or variation is None:
        return ALERTE_SANS_REF
    if variation <= SEUIL_ALERTE_CHUTE:
        return ALERTE_CHUTE
    if variation <= SEUIL_ALERTE_BAISSE:
        return ALERTE_BAISSE
    return ALERTE_NORMAL


def _preparer_alertes(jour: date) -> date:
    """Charge ce qu'il faut ; retourne le premier jour d'encaissement nécessaire (1er de M-2)."""
    log.info("alertes_secteurs : tarifs…")
    _assurer_tarifs()
    log.info("alertes_secteurs : planning…")
    _assurer_planif()
    a, m = _mois_ref(jour.year, jour.month, "BIMENSUEL")
    debut = date(a, m, 1)
    log.info("alertes_secteurs : encaissements du %s au %s…", debut, jour)
    _assurer_encaissements(debut, jour)
    return debut


def _somme(montants: dict, sect: str, d1: date, d2: date) -> float:
    return sum(montants.get((sect, d1 + timedelta(days=i)), 0.0) for i in range((d2 - d1).days + 1))


def _periode(d1: date, d2: date) -> str:
    return f"{d1:%d}-{d2:%d/%m/%Y}"


def _ligne_alerte(p: dict, jour: date, montants: dict, agreg: dict | None = None) -> dict:
    """Un secteur au jour J : cumul du 1er au jour J, comparé au cumul du 1er
    au même jour du mois de référence (M-1 mensuel, M-2 bimensuel ou hors
    planning). L'alerte porte sur ce cumul ; l'encaissé du jour seul est donné
    à côté. `agreg` : {(d1, f1): {sect_id: (montant, reçus, abonnés)}}."""
    a, m = _mois_ref(jour.year, jour.month, p["frequence"] or "BIMENSUEL")
    jour_ref = _meme_jour(a, m, jour.day)
    d1, d1_ref = jour.replace(day=1), date(a, m, 1)
    du_jour = montants.get((p["sect_id"], jour), 0.0)
    du_jour_ref = montants.get((p["sect_id"], jour_ref), 0.0)
    cumul = _somme(montants, p["sect_id"], d1, jour)
    cumul_ref = _somme(montants, p["sect_id"], d1_ref, jour_ref)
    variation = _pct(cumul - cumul_ref, cumul_ref) if cumul_ref else None
    var_jour = _pct(du_jour - du_jour_ref, du_jour_ref) if du_jour_ref else None
    hors = p["secteur"] == store.SECTEUR_HORS
    # Hors secteur (groupements, devis, autres) : affiché pour que les totaux du
    # centre soient complets, mais jamais en alerte.
    statut = ALERTE_SANS_REF if hors else _statut_alerte(cumul_ref, variation)
    nb = ((agreg or {}).get((d1, jour)) or {}).get(p["sect_id"], (0, 0, 0))
    nb_ref = ((agreg or {}).get((d1_ref, jour_ref)) or {}).get(p["sect_id"], (0, 0, 0))
    return {
        "zone": zones.zone_du_centre(p["centre"]),
        "centre": p["centre"],
        "nom_centre": None,
        "sect_id": p["sect_id"],
        "secteur": "Hors secteur" if hors else p["secteur"],
        "frequence": p["frequence"] or "Hors planning",
        "planifie": "Oui" if p.get("planifie") else "Non",
        "mois": f"{jour.year}-{jour.month:02d}",
        "mois_ref": f"{a}-{m:02d}",
        "jour": jour.isoformat(),
        "jour_ref": jour_ref.isoformat(),
        "periode": _periode(d1, jour),
        "periode_ref": _periode(d1_ref, jour_ref),
        "date_plan": store._sortie(p["date_plan"]),
        "date_facturation": store._sortie(p["date_fact_reelle"]),
        "cumul": round(cumul, 2),
        "cumul_ref": round(cumul_ref, 2),
        "ecart": round(cumul - cumul_ref, 2),
        "variation_pct": variation,
        "nb_recus": nb[1],
        "nb_recus_ref": nb_ref[1],
        "nb_abonnes": nb[2],
        "nb_abonnes_ref": nb_ref[2],
        "montant_jour": round(du_jour, 2),
        "montant_jour_ref": round(du_jour_ref, 2),
        "variation_jour_pct": var_jour,
        "statut": "Hors secteur" if hors else LIBELLES_ALERTE[statut],
        "statut_code": statut,
    }


def _sonde_blocage(nom: str, secondes: float = 45.0) -> threading.Timer:
    """Si `nom` dure plus de `secondes`, écrit dans le journal la pile de chaque
    fil d'exécution (pour trouver un blocage). À annuler à la fin du calcul."""
    def vider():
        import sys
        import traceback
        piles = []
        noms = {t.ident: t.name for t in threading.enumerate()}
        for ident, cadre in sys._current_frames().items():
            piles.append(f"--- fil {noms.get(ident, ident)}\n" + "".join(traceback.format_stack(cadre)))
        log.warning("%s dure plus de %.0f s, piles des fils :\n%s", nom, secondes, "\n".join(piles))
    minuterie = threading.Timer(secondes, vider)
    minuterie.daemon = True
    minuterie.start()
    return minuterie


def alertes_secteurs(jour: date | None = None, centre: str | None = None, zone: str | None = None) -> dict:
    """Alerte de chaque secteur au jour J (hier, ou aujourd'hui après 23 h) ;
    `zone` : une ou plusieurs zones de centres séparées par des virgules
    (NKTT NORD,NOUADHIBOU…)."""
    sonde = _sonde_blocage("alertes_secteurs")
    try:
        return _alertes_secteurs(jour, centre, (zone or "").strip() or None)
    finally:
        sonde.cancel()


def _alertes_secteurs(jour: date | None = None, centre: str | None = None, zone: str | None = None) -> dict:
    jour = _jour_alerte(jour)
    centre = str(centre).strip() if centre else None
    t0 = time.monotonic()
    log.info("alertes_secteurs %s : début", jour)
    debut = _preparer_alertes(jour)
    log.info("alertes_secteurs %s : données prêtes (%.1f s)", jour, time.monotonic() - t0)
    secteurs = store.tous_secteurs(jour.year, jour.month, debut, jour)
    log.info("alertes_secteurs %s : %s secteurs (%.1f s)", jour, len(secteurs), time.monotonic() - t0)
    if centre:
        secteurs = [p for p in secteurs if str(p["centre"]) == centre]
    if zone:
        choix = {z.strip() for z in zone.split(",") if z.strip()}
        secteurs = [p for p in secteurs if zones.zone_du_centre(p["centre"]) in choix]
    montants = store.encaissements_par_secteur_jour(debut, jour)
    # Reçus et abonnés payeurs : une période en cours et deux de référence (M-1, M-2).
    agreg = {(jour.replace(day=1), jour): store.agreg_secteurs(jour.replace(day=1), jour)}
    for freq in ("MENSUEL", "BIMENSUEL"):
        a, m = _mois_ref(jour.year, jour.month, freq)
        d1, f1 = date(a, m, 1), _meme_jour(a, m, jour.day)
        agreg[(d1, f1)] = store.agreg_secteurs(d1, f1)
    log.info("alertes_secteurs %s : montants calculés (%.1f s)", jour, time.monotonic() - t0)
    noms = {str(c["centre"]): c["nom_centre"] for c in centres()}
    lignes = []
    for p in secteurs:
        l = _ligne_alerte(p, jour, montants, agreg)
        # Secteur sans aucun encaissement ni ce mois ni au mois de référence : ignoré.
        if not l["cumul"] and not l["cumul_ref"]:
            continue
        l["nom_centre"] = noms.get(str(p["centre"]))
        lignes.append(l)
    vus = store.secteurs_vus(jour)
    for l in lignes:
        l["vu"] = l["sect_id"] in vus
    log.info("alertes_secteurs %s : terminé, %s lignes (%.1f s)", jour, len(lignes), time.monotonic() - t0)
    # Chutes d'abord, de la plus forte perte (écart) à la plus faible.
    lignes.sort(key=lambda l: (_ORDRE_ALERTE.index(l["statut_code"]), l["ecart"],
                               _cle_centre(l["centre"]), str(l["secteur"])))
    compte = {code: sum(1 for l in lignes if l["statut_code"] == code) for code in _ORDRE_ALERTE}
    return {
        "jour": jour.isoformat(),
        "periode": _periode(jour.replace(day=1), jour),
        "nb_chutes_non_vues": sum(1 for l in lignes if l["statut_code"] == ALERTE_CHUTE and not l["vu"]),
        "seuil_chute": SEUIL_ALERTE_CHUTE,
        "seuil_baisse": SEUIL_ALERTE_BAISSE,
        "seuil_reference": MONTANT_MIN_REFERENCE,
        "nb_chutes": compte[ALERTE_CHUTE],
        "nb_baisses": compte[ALERTE_BAISSE],
        "nb_secteurs": len(lignes),
        "nb_planifies": sum(1 for l in lignes if l["planifie"] == "Oui"),
        "nb_mensuels": sum(1 for l in lignes if l["frequence"] == "MENSUEL"),
        "nb_bimensuels": sum(1 for l in lignes if l["frequence"] == "BIMENSUEL"),
        "journee_en_cours": jour == date.today() and datetime.now().hour < HEURE_RAPPORT,
        "planning": store.info_planif(),
        "lignes": lignes,
    }


# --------------------------------------------------------------------------- rapport Excel des chutes (23 h)
DOSSIER_RAPPORTS = Path(__file__).resolve().parents[2] / "sorties_alertes"


def _libelle_ref(lignes: list[dict]) -> str:
    """Période de référence pour les en-têtes : unique si tous les secteurs ont
    le même mois de référence, sinon les deux périodes (M-1 et M-2)."""
    periodes = sorted({l["periode_ref"] for l in lignes}, reverse=True)
    return " ou ".join(periodes) if periodes else "réf."


def _colonnes_rapport(jour: date, lignes: list[dict]) -> list[tuple]:
    p = f"{jour.replace(day=1):%d}-{jour:%d/%m/%Y}"
    r = _libelle_ref(lignes)
    return [
        ("zone", "Zone", "texte"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("secteur", "Secteur", "texte"),
        ("cumul_ref", f"Encaissé {r}", "montant"),
        ("cumul", f"Encaissé {p}", "montant"),
        ("ecart", "Écart", "montant"),
        ("variation_pct", "Variation (%)", "pct"),
        ("nb_recus_ref", f"Reçus {r}", "entier"),
        ("nb_recus", f"Reçus {p}", "entier"),
        ("nb_abonnes_ref", f"Abonnés payeurs {r}", "entier"),
        ("nb_abonnes", f"Abonnés payeurs {p}", "entier"),
    ]


def rapport_chutes(jour: date | None = None, centre: str | None = None, zone: str | None = None) -> tuple[bytes, str]:
    """Classeur des secteurs en chute au jour J (cumul du 1er au jour J), même
    présentation que chutes_secteur_AAAA-MM-JJ.xlsx."""
    data = alertes_secteurs(jour, centre, zone)
    jour = date.fromisoformat(data["jour"])
    chutes = [l for l in data["lignes"] if l["statut_code"] == ALERTE_CHUTE]
    contenu = exports.classeur_beige(f"Chutes {jour:%d-%m-%Y}", _colonnes_rapport(jour, chutes), chutes)
    return contenu, f"chutes_secteur_{jour.isoformat()}.xlsx"


def generer_rapport_chutes(jour: date | None = None) -> Path:
    """Écrit le rapport du jour dans backend/sorties_alertes (appelé chaque soir à 23 h)."""
    contenu, nom = rapport_chutes(jour)
    DOSSIER_RAPPORTS.mkdir(parents=True, exist_ok=True)
    chemin = DOSSIER_RAPPORTS / nom
    chemin.write_bytes(contenu)
    log.info("Rapport des chutes écrit : %s", chemin)
    return chemin


def alerte_secteur_detail(sect_id: str, jour: date | None = None) -> dict:
    """Un secteur jour par jour du 1er au jour J, comparé au même jour du mois
    de référence, et le détail par abonné (du 1er au jour J)."""
    jour = _jour_alerte(jour)
    debut = _preparer_alertes(jour)
    p = next((x for x in store.tous_secteurs(jour.year, jour.month, debut, jour) if x["sect_id"] == sect_id), None)
    if p is None:
        return {"sect_id": sect_id, "jour": jour.isoformat(), "trouve": False, "jours": [], "abonnes": []}
    montants = store.encaissements_par_secteur_jour(debut, jour, [sect_id])
    jours = []
    for n in range(1, jour.day + 1):
        jours.append(_ligne_alerte(p, jour.replace(day=n), montants))
    a, m = _mois_ref(jour.year, jour.month, p["frequence"] or "BIMENSUEL")
    abonnes = store.encaissements_secteur_abonnes(sect_id, jour.replace(day=1), jour,
                                                  date(a, m, 1), _meme_jour(a, m, jour.day))
    for x in abonnes:
        x["ecart"] = round(x["montant_actuel"] - x["montant_ref"], 2)
        x["variation_pct"] = _pct(x["ecart"], x["montant_ref"]) if x["montant_ref"] else None
    abonnes.sort(key=lambda x: x["ecart"])
    return {"trouve": True, "jour": jour.isoformat(), "secteur": jours[-1], "jours": jours, "abonnes": abonnes}


def secteurs_planifies_annee(annee: int, centre: str | None = None) -> dict:
    """Nombre de secteurs planifiés par centre et par mois."""
    _assurer_planif()
    centre = str(centre).strip() if centre else None
    noms = {str(c["centre"]): c["nom_centre"] for c in centres()}
    par_centre: dict[str, dict] = {}
    for r in store.nb_secteurs_planifies(annee, centre):
        l = par_centre.setdefault(r["centre"], {"centre": r["centre"], "nom_centre": noms.get(str(r["centre"]))})
        l[f"m{r['mois']:02d}"] = r["nb"]
        l[f"m{r['mois']:02d}_detail"] = r
    lignes = sorted(par_centre.values(), key=lambda l: _cle_centre(l["centre"]))
    return {"annee": annee, "lignes": lignes, "planning": store.info_planif()}


def marquer_secteurs_vus(jour: date | None, sect_ids: list[str]):
    """Valide (« c'est bien ») les secteurs en chute du jour : le bandeau ne les montre plus."""
    store.marquer_secteurs_vus(_jour_alerte(jour), sect_ids)
