"""Base locale DuckDB.

Les données lues dans Oracle sont enregistrées ici, jour par jour. L'API lit
ensuite DuckDB : un redémarrage (uvicorn --reload) retrouve tout de suite les
données déjà chargées, sans réinterroger Oracle.

- Une table par jeu de données (controle, encaissements, versements, cheques,
  annulations, reports) et une table `centres`.
- La table `sync` note, pour chaque jeu, chaque jour et chaque centre (ou
  `*` pour tous les centres), quand il a été chargé.
- Les jours récents sont rechargés à chaque intervalle de rafraîchissement ;
  les jours plus anciens sont gardés tels quels, sauf demande d'actualisation.

Le fichier n'est ouvert que par ce processus (DuckDB n'accepte qu'un seul
processus en écriture). Chaque fil d'exécution a son propre curseur : les
lectures ne sont jamais bloquées par une écriture en cours.
"""
import csv
import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

from .config import get_settings

log = logging.getLogger("controle.duckdb")

# À augmenter quand les colonnes changent : les tables sont alors recréées.
VERSION_SCHEMA = 3
# Les jours de moins de N jours sont considérés comme encore modifiables.
JOURS_RECENTS = 3
_LOT = 400

# jeu -> (colonne du jour, [(colonne, type)])
SCHEMAS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "controle": ("jour", [
        ("centre", "VARCHAR"), ("nom_centre", "VARCHAR"), ("jour", "DATE"),
        ("nb_caisses", "INTEGER"), ("report", "DOUBLE"), ("nb_recus", "INTEGER"),
        ("recus_total", "DOUBLE"), ("recus_especes", "DOUBLE"), ("recus_cheques", "DOUBLE"),
        ("recus_cheques_tresor", "DOUBLE"), ("recus_virements", "DOUBLE"),
        ("recap_especes", "DOUBLE"), ("recap_cheques", "DOUBLE"), ("recap_virements", "DOUBLE"),
        ("nb_versements", "INTEGER"), ("verse_especes_valide", "DOUBLE"),
        ("verse_especes_non_valide", "DOUBLE"), ("nb_versements_non_valides", "INTEGER"),
        ("verse_especes_annule", "DOUBLE"), ("recap_vers_especes", "DOUBLE"),
        ("recap_vers_cheques", "DOUBLE"), ("depenses", "DOUBLE"), ("solde", "DOUBLE"),
        ("rupture_report", "DOUBLE"), ("nb_annulations", "INTEGER"), ("montant_annule", "DOUBLE"),
    ]),
    "encaissements": ("date_comptable", [
        ("centre", "VARCHAR"), ("nom_centre", "VARCHAR"), ("num_caisse", "INTEGER"),
        ("num_recu", "VARCHAR"), ("reference", "VARCHAR"), ("type_reference", "VARCHAR"),
        ("nom_groupement", "VARCHAR"), ("demande", "VARCHAR"), ("matricule", "VARCHAR"),
        ("date_comptable", "DATE"), ("heure", "VARCHAR"), ("mode_paiement", "VARCHAR"),
        ("code_banque", "VARCHAR"), ("nom_banque", "VARCHAR"), ("num_piece", "VARCHAR"),
        ("tireur", "VARCHAR"), ("montant", "DOUBLE"), ("statut", "VARCHAR"),
    ]),
    "versements": ("date_versement", [
        ("centre", "VARCHAR"), ("nom_centre", "VARCHAR"), ("num_versement", "VARCHAR"),
        ("num_caisse", "INTEGER"), ("matricule", "VARCHAR"), ("date_versement", "DATE"),
        ("code_banque", "VARCHAR"), ("nom_banque", "VARCHAR"), ("compte", "VARCHAR"),
        ("montant", "DOUBLE"), ("statut", "VARCHAR"),
    ]),
    "cheques": ("date_comptable", [
        ("centre", "VARCHAR"), ("nom_centre", "VARCHAR"), ("num_caisse", "INTEGER"),
        ("num_recu", "VARCHAR"), ("reference", "VARCHAR"), ("matricule", "VARCHAR"),
        ("date_comptable", "DATE"), ("mode_paiement", "VARCHAR"), ("num_cheque", "VARCHAR"),
        ("tireur", "VARCHAR"), ("code_banque_cheque", "VARCHAR"), ("nom_banque_cheque", "VARCHAR"),
        ("date_depot", "DATE"), ("code_banque_depot", "VARCHAR"), ("nom_banque_depot", "VARCHAR"),
        ("compte_depot", "VARCHAR"), ("num_bordereau", "VARCHAR"), ("montant", "DOUBLE"),
    ]),
    "annulations": ("date_recu", [
        ("centre", "VARCHAR"), ("nom_centre", "VARCHAR"), ("num_caisse", "INTEGER"),
        ("num_recu", "VARCHAR"), ("reference", "VARCHAR"), ("matricule", "VARCHAR"),
        ("date_recu", "DATE"), ("heure", "VARCHAR"), ("mode_paiement", "VARCHAR"),
        ("montant", "DOUBLE"),
    ]),
    "non_valides": ("date_recu", [
        ("centre", "VARCHAR"), ("nom_centre", "VARCHAR"), ("num_recu", "VARCHAR"),
        ("date_recu", "DATE"), ("heure", "VARCHAR"), ("abonne", "VARCHAR"), ("client", "VARCHAR"),
        ("montant", "DOUBLE"), ("credite", "DOUBLE"), ("matricule", "VARCHAR"),
        ("caissier_jour", "VARCHAR"), ("poste_recu", "VARCHAR"), ("postes_caissier", "VARCHAR"),
        ("especes_validees", "DOUBLE"), ("report", "DOUBLE"), ("verse_especes", "DOUBLE"),
    ]),
    "reports": ("jour", [
        ("centre", "VARCHAR"), ("nom_centre", "VARCHAR"), ("jour", "DATE"),
        ("num_caisse", "INTEGER"), ("report", "DOUBLE"), ("especes", "DOUBLE"),
        ("cheques", "DOUBLE"), ("virements", "DOUBLE"), ("verse_especes", "DOUBLE"),
        ("verse_cheques", "DOUBLE"), ("depenses", "DOUBLE"), ("solde", "DOUBLE"),
        ("jour_precedent", "DATE"), ("solde_veille", "DOUBLE"), ("rupture", "DOUBLE"),
    ]),
}
JEUX = tuple(SCHEMAS)
# Règle de rattachement des encaissements enregistrés dans la base locale.
# Quand elle change, les encaissements déjà chargés sont effacés et relus dans
# Oracle ; les autres jeux sont gardés.
REGLE_ENCAISSEMENTS = "centre_abonne_statut"
COLONNES_RELEVE = [("ligne", "INTEGER"), ("banque", "VARCHAR"), ("groupe", "VARCHAR"),
                   ("date_operation", "DATE"), ("date_valeur", "DATE"), ("libelle", "VARCHAR"),
                   ("debit", "DOUBLE"), ("credit", "DOUBLE"), ("classe", "VARCHAR")]
_COLONNES_SYNC = [("jeu", "VARCHAR"), ("jour", "DATE"), ("centre", "VARCHAR"),
                  ("charge_le", "TIMESTAMP"), ("nb_lignes", "INTEGER")]

_base = None                     # connexion racine au fichier DuckDB
_generation = 0                  # change à chaque ouverture : les curseurs sont alors recréés
_ouverture = threading.Lock()    # protège l'ouverture et la fermeture
_ecriture = threading.Lock()     # une seule écriture à la fois
_local = threading.local()       # un curseur par fil d'exécution
# Date de chargement fictive qui signifie « à recharger dès que possible ».
_A_RECHARGER = datetime(1970, 1, 1)
_JEU_CENTRES = "centres"
_JEU_PERSONNEL = "personnel"
_JEU_TARIFS = "tarifs"
_JEU_PLANIF = "planif_secteurs"
# Jeux dont la colonne `matricule` est celle d'un agent : son nom est ajouté à
# la lecture (colonne `nom_agent`), depuis la petite table locale `personnel`.
# Les lignes déjà chargées n'ont ainsi pas à être relues dans Oracle.
JEUX_AVEC_AGENT = {"encaissements", "versements"}
# Valeur de la colonne `centre` de `sync` pour un chargement de tous les centres.
TOUS = "*"
_NUL = "\\N"                     # écriture d'une valeur vide dans le fichier d'import


class BaseLocaleIndisponible(RuntimeError):
    """Le fichier DuckDB ne peut pas être ouvert."""


# --------------------------------------------------------------------------- connexion
def _chemin() -> Path:
    chemin = Path(get_settings().duckdb_path)
    if not chemin.is_absolute():
        chemin = Path(__file__).resolve().parents[2] / chemin  # relatif au dossier backend
    chemin.parent.mkdir(parents=True, exist_ok=True)
    return chemin


def _creer_tables(con):
    con.execute("CREATE TABLE IF NOT EXISTS meta (cle VARCHAR, valeur VARCHAR)")
    ligne = con.execute("SELECT valeur FROM meta WHERE cle = 'version'").fetchone()
    if ligne is None or ligne[0] != str(VERSION_SCHEMA):
        # La base locale n'est qu'une copie : en cas de changement de structure,
        # on repart de zéro et Oracle la remplit de nouveau.
        for nom in list(JEUX) + ["centres", "sync"]:
            con.execute(f"DROP TABLE IF EXISTS {nom}")
        con.execute("DELETE FROM meta WHERE cle = 'version'")
        con.execute("INSERT INTO meta VALUES ('version', ?)", [str(VERSION_SCHEMA)])
    con.execute(
        "CREATE TABLE IF NOT EXISTS sync ("
        "jeu VARCHAR, jour DATE, centre VARCHAR, charge_le TIMESTAMP, nb_lignes INTEGER)"
    )
    # Encaissements enregistrés avec une ancienne règle (rattachés à la caisse,
    # e-paiements à part, ou sans la colonne du statut) : la table est refaite
    # et se remplit de nouveau depuis Oracle. Les autres jeux sont gardés.
    regle = con.execute("SELECT valeur FROM meta WHERE cle = 'regle_encaissements'").fetchone()
    if regle is None or regle[0] != REGLE_ENCAISSEMENTS:
        con.execute("DROP VIEW IF EXISTS encaissements_tout")
        con.execute("DROP TABLE IF EXISTS epaiements")
        con.execute("DROP TABLE IF EXISTS encaissements")
        con.execute("DELETE FROM sync WHERE jeu IN ('encaissements', 'epaiements')")
        con.execute("DELETE FROM meta WHERE cle = 'regle_encaissements'")
        con.execute("INSERT INTO meta VALUES ('regle_encaissements', ?)", [REGLE_ENCAISSEMENTS])
    for nom, (_, colonnes) in SCHEMAS.items():
        definition = ", ".join(f"{c} {t}" for c, t in colonnes)
        con.execute(f"CREATE TABLE IF NOT EXISTS {nom} ({definition})")
    con.execute("CREATE TABLE IF NOT EXISTS centres (centre VARCHAR, nom_centre VARCHAR)")
    # Agents du CRM : une ligne par matricule (noms préfixés pour ne pas se
    # confondre avec les colonnes des jeux dans les jointures).
    con.execute("CREATE TABLE IF NOT EXISTS personnel (pers_matricule VARCHAR, pers_nom VARCHAR)")
    # Relevé bancaire importé (indépendant de la version des jeux Oracle : il
    # ne peut pas être relu ailleurs, on ne l'efface donc jamais ici).
    definition = ", ".join(f"{c} {t}" for c, t in COLONNES_RELEVE)
    con.execute(f"CREATE TABLE IF NOT EXISTS releve ({definition})")
    con.execute(
        "CREATE TABLE IF NOT EXISTS releve_info ("
        "nom_fichier VARCHAR, importe_le TIMESTAMP, nb_lignes INTEGER, date_min DATE, date_max DATE)"
    )
    # Plusieurs relevés peuvent être importés : une ligne par fichier, avec le
    # nombre de lignes réellement ajoutées (les autres étaient déjà présentes).
    con.execute("ALTER TABLE releve_info ADD COLUMN IF NOT EXISTS nb_ajoutees INTEGER")
    # Saisie de la DFC importée (un classeur par centre) : comme le relevé, elle
    # ne peut pas être relue ailleurs et n'est donc jamais effacée ici.
    con.execute(
        "CREATE TABLE IF NOT EXISTS dfc (centre VARCHAR, jour DATE, especes DOUBLE, cheques DOUBLE, "
        "virements DOUBLE, verse DOUBLE, banques VARCHAR, depenses DOUBLE, rem DOUBLE, observation VARCHAR)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS dfc_info (centre VARCHAR, nom_fichier VARCHAR, importe_le TIMESTAMP, "
        "nb_jours INTEGER, date_min DATE, date_max DATE, couverture_min DATE, couverture_max DATE)")
    # Reçus manuels importés (cahier des centres) : jamais effacés ici non plus.
    con.execute(
        "CREATE TABLE IF NOT EXISTS recus_manuels (nom_fichier VARCHAR, centre_fichier VARCHAR, date_recu DATE, "
        "num_recu_manuel VARCHAR, reference VARCHAR, montant DOUBLE)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS recus_manuels_info (nom_fichier VARCHAR, importe_le TIMESTAMP, nb_recus INTEGER)")
    # Reçus non validés déjà consultés : l'alerte ne signale que les autres.
    # Cette table n'est jamais effacée, elle ne peut pas être relue ailleurs.
    con.execute("CREATE TABLE IF NOT EXISTS non_valides_vus (num_recu VARCHAR, vu_le TIMESTAMP)")
    # Secteurs en chute déjà validés (« c'est bien ») pour un jour donné : le
    # bandeau ne les signale plus. Jamais effacée ici.
    con.execute("CREATE TABLE IF NOT EXISTS alertes_secteurs_vus (jour DATE, sect_id VARCHAR, vu_le TIMESTAMP)")
    # Tarif de chaque abonnement (S_TARIF_PR.TARP_CODE par PFACT_ID), avec le
    # code de son groupement : rechargé une fois par jour depuis Oracle.
    colonnes = [c[0] for c in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'tarifs'").fetchall()]
    if colonnes and ("statut" not in colonnes or "secteur" not in colonnes or "sect_id" not in colonnes):
        # Ancienne table sans le statut ou sans le secteur : recréée et rechargée.
        con.execute("DROP TABLE tarifs")
        con.execute("DELETE FROM sync WHERE jeu = 'tarifs'")
    con.execute("CREATE TABLE IF NOT EXISTS tarifs (abn_id VARCHAR, grp_code VARCHAR, tarif VARCHAR, secteur VARCHAR, "
                "statut VARCHAR, sect_id VARCHAR)")
    # Planning des relevés par secteur et jour réel de facturation : rechargé
    # une fois par jour depuis Oracle (PLANIF_RELEVE, PLANIFDETAILLE, S_FACTURE_M).
    con.execute(
        "CREATE TABLE IF NOT EXISTS planif_secteurs (centre VARCHAR, sect_id VARCHAR, secteur VARCHAR, "
        "frequence VARCHAR, annee INTEGER, mois INTEGER, date_releve_plan DATE, date_plan DATE, "
        "date_fact_reelle DATE, nb_factures INTEGER)")
    # Références de l'État (abonnements et groupements), rechargées à chaque
    # ouverture depuis le module etat : sert à séparer Privés et État.
    from . import etat
    con.execute("CREATE OR REPLACE TABLE etat_abn (ref VARCHAR)")
    con.execute("CREATE OR REPLACE TABLE etat_grp (ref VARCHAR)")
    con.executemany("INSERT INTO etat_abn VALUES (?)", [[r] for r in sorted(etat.REFERENCES_ABN)])
    con.executemany("INSERT INTO etat_grp VALUES (?)", [[r] for r in sorted(etat.REFERENCES_GRP)])


def _ouvrir():
    global _base, _generation
    with _ouverture:
        if _base is not None:
            return
        chemin = _chemin()
        derniere = None
        for _ in range(8):
            try:
                con = duckdb.connect(str(chemin))
                break
            except duckdb.IOException as exc:
                # Avec --reload, l'ancien processus peut mettre un instant à
                # libérer le fichier.
                derniere = exc
                time.sleep(0.5)
        else:
            raise BaseLocaleIndisponible(
                f"Base DuckDB inaccessible ({chemin}) : le fichier est ouvert par un autre "
                f"programme. Fermez-le puis relancez. Détail : {derniere}"
            )
        for fichier in chemin.parent.glob("_import_*.csv"):  # restes d'un arrêt brutal
            try:
                fichier.unlink()
            except OSError:
                pass
        _creer_tables(con)
        _generation += 1
        _base = con
        log.info("Base DuckDB ouverte : %s", chemin)


def _cur():
    """Curseur du fil courant. Chaque fil a le sien : une lecture n'attend
    jamais la fin d'une écriture faite par un autre fil."""
    if _base is None:
        _ouvrir()
    if getattr(_local, "generation", None) != _generation:
        _local.curseur = _base.cursor()
        _local.generation = _generation
    return _local.curseur


def fermer():
    global _base
    with _ouverture:
        if _base is not None:
            try:
                _base.close()
            except duckdb.Error:
                pass
            _base = None


# --------------------------------------------------------------------------- outils
def _jours(debut: date, fin: date) -> list[date]:
    return [debut + timedelta(days=i) for i in range((fin - debut).days + 1)]


def _en_date(valeur):
    if isinstance(valeur, datetime):
        return valeur.date()
    if isinstance(valeur, date):
        return valeur
    if isinstance(valeur, str) and len(valeur) >= 10:
        try:
            return date.fromisoformat(valeur[:10])
        except ValueError:
            return None
    return None


def _sortie(valeur):
    if isinstance(valeur, datetime):
        return valeur.date().isoformat()
    if isinstance(valeur, date):
        return valeur.isoformat()
    if isinstance(valeur, float):
        return round(valeur, 3)
    return valeur


def _texte_import(valeur, genre: str) -> str:
    if valeur is None:
        return _NUL
    if genre == "DATE":
        jour = _en_date(valeur)
        return jour.isoformat() if jour else _NUL
    if genre == "INTEGER":
        return str(int(valeur))
    if genre == "DOUBLE":
        return repr(float(valeur))
    return str(valeur)


def _inserer_par_fichier(cur, table: str, colonnes: list[tuple[str, str]], lignes: list[dict]):
    """Chargement en masse : les lignes sont écrites dans un fichier CSV
    temporaire que DuckDB lit en une seule instruction."""
    chemin = _chemin().parent / f"_import_{uuid.uuid4().hex}.csv"
    try:
        with open(chemin, "w", newline="", encoding="utf-8") as f:
            ecrivain = csv.writer(f, delimiter=",", quotechar='"', quoting=csv.QUOTE_MINIMAL,
                                  lineterminator="\n")
            for ligne in lignes:
                ecrivain.writerow([_texte_import(ligne.get(nom), genre) for nom, genre in colonnes])
        types = ", ".join(f"'{nom}': '{genre}'" for nom, genre in colonnes)
        source = str(chemin).replace("\\", "/").replace("'", "''")
        cur.execute(
            f"INSERT INTO {table} SELECT * FROM read_csv('{source}', header = false, delim = ',', "
            f"quote = '\"', escape = '\"', nullstr = '{_NUL}', auto_detect = false, "
            f"columns = {{{types}}})"
        )
    finally:
        try:
            chemin.unlink()
        except OSError:
            pass


def _inserer_par_lots(cur, table: str, colonnes: list[tuple[str, str]], lignes: list[dict]):
    motif = "(" + ",".join("?" * len(colonnes)) + ")"
    for i in range(0, len(lignes), _LOT):
        lot = lignes[i:i + _LOT]
        valeurs = []
        for ligne in lot:
            for nom, genre in colonnes:
                v = ligne.get(nom)
                if genre == "DATE":
                    v = _en_date(v)
                elif genre == "VARCHAR" and v is not None:
                    v = str(v)
                valeurs.append(v)
        cur.execute(f"INSERT INTO {table} VALUES " + ",".join([motif] * len(lot)), valeurs)


def _inserer(cur, table: str, colonnes: list[tuple[str, str]], lignes: list[dict]):
    if not lignes:
        return
    if len(lignes) <= 20:
        _inserer_par_lots(cur, table, colonnes, lignes)
    else:
        _inserer_par_fichier(cur, table, colonnes, lignes)


# Qui écrit en ce moment, et depuis quand : affiché par le diagnostic pour
# comprendre un blocage.
ecriture_en_cours = {"fil": None, "depuis": None, "etape": None}


@contextmanager
def _transaction():
    """Une écriture à la fois, validée d'un bloc ou annulée."""
    with _ecriture:
        ecriture_en_cours.update(fil=threading.current_thread().name, depuis=time.time(), etape="début")
        try:
            cur = _cur()
            cur.execute("BEGIN")
            try:
                ecriture_en_cours["etape"] = "écriture"
                yield cur
                ecriture_en_cours["etape"] = "validation"
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        finally:
            ecriture_en_cours.update(fil=None, depuis=None, etape=None)


def verrous() -> dict:
    """État des verrous de la base locale (sans rien attendre)."""
    depuis = ecriture_en_cours["depuis"]
    return {
        "ecriture_prise": _ecriture.locked(),
        "ecriture_par": ecriture_en_cours["fil"],
        "ecriture_etape": ecriture_en_cours["etape"],
        "ecriture_depuis_s": round(time.time() - depuis, 1) if depuis else None,
        "ouverture_prise": _ouverture.locked(),
        "base_ouverte": _base is not None,
    }


# --------------------------------------------------------------------------- synchronisation
def _cles_centre(centre: str | None) -> list[str]:
    """Un jour est couvert pour un centre s'il a été chargé pour tous les
    centres ou pour ce centre seul."""
    return [TOUS] if not centre else [TOUS, str(centre).strip()]


def _dernier_chargement(jeu: str, debut: date, fin: date, centre: str | None) -> dict[date, datetime]:
    cles = _cles_centre(centre)
    lignes = _cur().execute(
        "SELECT jour, MAX(charge_le) FROM sync WHERE jeu = ? AND jour BETWEEN ? AND ? "
        f"AND centre IN ({','.join('?' * len(cles))}) GROUP BY jour",
        [jeu, debut, fin, *cles],
    ).fetchall()
    return {jour: quand for jour, quand in lignes}


def jours_a_charger(jeu: str, debut: date, fin: date, duree_secondes: int,
                    centre: str | None = None) -> list[date]:
    """Jours absents de la base locale, ou récents et chargés depuis trop longtemps."""
    charges = _dernier_chargement(jeu, debut, fin, centre)
    maintenant = datetime.now()
    seuil_recent = date.today() - timedelta(days=JOURS_RECENTS)
    resultat = []
    for jour in _jours(debut, fin):
        quand = charges.get(jour)
        if quand is None or quand <= _A_RECHARGER:
            resultat.append(jour)
        elif jour >= seuil_recent and (maintenant - quand).total_seconds() >= duree_secondes:
            resultat.append(jour)
    return resultat


def tout_present(jeu: str, debut: date, fin: date, centre: str | None = None) -> bool:
    """Vrai si chaque jour de la période a déjà été chargé au moins une fois."""
    return len(_dernier_chargement(jeu, debut, fin, centre)) == (fin - debut).days + 1


def remplacer(jeu: str, debut: date, fin: date, lignes: list[dict], centre: str | None = None):
    """Remplace dans la base locale les lignes du jeu pour la période donnée :
    tous les centres, ou un seul si `centre` est précisé."""
    colonne_jour, colonnes = SCHEMAS[jeu]
    cle = str(centre).strip() if centre else TOUS
    par_jour: dict[date, int] = {}
    for ligne in lignes:
        jour = _en_date(ligne.get(colonne_jour))
        if jour is not None:
            par_jour[jour] = par_jour.get(jour, 0) + 1
    maintenant = datetime.now()
    suivi = [
        {"jeu": jeu, "jour": jour, "centre": cle, "charge_le": maintenant, "nb_lignes": par_jour.get(jour, 0)}
        for jour in _jours(debut, fin)
    ]
    with _transaction() as cur:
        if cle == TOUS:
            cur.execute(f"DELETE FROM {jeu} WHERE {colonne_jour} BETWEEN ? AND ?", [debut, fin])
            cur.execute("DELETE FROM sync WHERE jeu = ? AND jour BETWEEN ? AND ?", [jeu, debut, fin])
        else:
            cur.execute(f"DELETE FROM {jeu} WHERE {colonne_jour} BETWEEN ? AND ? AND centre = ?",
                        [debut, fin, cle])
            cur.execute("DELETE FROM sync WHERE jeu = ? AND jour BETWEEN ? AND ? AND centre = ?",
                        [jeu, debut, fin, cle])
        _inserer(cur, jeu, colonnes, lignes)
        _inserer_par_lots(cur, "sync", _COLONNES_SYNC, suivi)


# Encaissement de l'État : abonnement ou groupement de la liste de l'État.
EST_ETAT = ("((type_reference = 'ABONNE' AND reference IN (SELECT ref FROM etat_abn)) "
            "OR (type_reference = 'GROUPEMENT' AND reference IN (SELECT ref FROM etat_grp)))")


def condition_segment(segment: str | None) -> str:
    """Condition SQL (à ajouter par AND) d'un segment : privés, État ou tous."""
    if segment == "etat":
        return f" AND {EST_ETAT}"
    if segment == "prives":
        return f" AND NOT {EST_ETAT}"
    return ""


def _filtre(jeu: str, debut: date, fin: date, centre: str | None, recherche: str | None,
            centres: set[str] | None = None, segment: str | None = None):
    """Condition SQL commune aux lectures : période, centre (ou liste de centres
    d'une zone) et texte recherché."""
    colonne_jour, colonnes = SCHEMAS[jeu]
    # Préfixe pour éviter l'ambiguïté avec les tables jointes (tarifs).
    p = f"{jeu}." if jeu == "encaissements" else ""
    sql = f"WHERE {p}{colonne_jour} BETWEEN ? AND ?"
    params: list = [debut, fin]
    if centre:
        sql += f" AND {p}centre = ?"
        params.append(str(centre).strip())
    if centres is not None:
        sql += f" AND {p}centre IN (SELECT UNNEST(?::VARCHAR[]))"
        params.append(sorted(centres))
    if jeu == "encaissements":
        sql += condition_segment(segment)
    texte = (recherche or "").strip().lower()
    if texte:
        # Le texte est cherché dans toutes les colonnes ; une date se cherche
        # aussi bien sous la forme 2026-01-05 que 05/01/2026.
        # Préfixe pour éviter l'ambiguïté avec les colonnes des tables jointes.
        prefixe = f"{jeu}." if jeu == "encaissements" else ""
        morceaux = []
        for nom, genre in colonnes:
            col = f"{prefixe}{nom}"
            if genre == "VARCHAR":
                morceaux.append(f"contains(lower({col}), ?)")
                params.append(texte)
            elif genre == "DATE":
                morceaux.append(f"contains(CAST({col} AS VARCHAR), ?)")
                morceaux.append(f"contains(strftime({col}, '%d/%m/%Y'), ?)")
                params.extend([texte, texte])
            else:
                morceaux.append(f"contains(CAST({col} AS VARCHAR), ?)")
                params.append(texte)
        if jeu in JEUX_AVEC_AGENT:
            morceaux.append("contains(lower(pers_nom), ?)")
            params.append(texte)
        sql += " AND (" + " OR ".join(morceaux) + ")"
    return sql, params


_GRP_TARIFS = (
    "(SELECT grp_code AS _grp_code, "
    "string_agg(DISTINCT tarif, ', ' ORDER BY tarif) AS _grp_tarif, "
    "string_agg(DISTINCT secteur, ', ' ORDER BY secteur) AS _grp_secteur "
    "FROM tarifs WHERE grp_code IS NOT NULL GROUP BY grp_code) _tg"
)


def _source(jeu: str) -> str:
    """Table lue, avec le nom de l'agent quand le jeu en a un,
    et les infos tarif/secteur/statut pour les encaissements."""
    if jeu == "encaissements":
        return (
            f"{jeu} LEFT JOIN personnel ON pers_matricule = matricule"
            f" LEFT JOIN tarifs _ta ON _ta.abn_id = reference"
            f" LEFT JOIN {_GRP_TARIFS} ON _tg._grp_code = reference"
        )
    if jeu in JEUX_AVEC_AGENT:
        return f"{jeu} LEFT JOIN personnel ON pers_matricule = matricule"
    return jeu


def lire(jeu: str, debut: date, fin: date, centre: str | None = None,
         recherche: str | None = None, limite: int | None = None,
         ordre: str | None = None, centres: set[str] | None = None,
         segment: str | None = None) -> list[dict]:
    """Lignes du jeu. `recherche`, `ordre` et `limite` servent à l'affichage
    des très grandes listes : le tri et la coupe sont faits par DuckDB."""
    _, colonnes = SCHEMAS[jeu]
    noms = [c for c, _ in colonnes]
    condition, params = _filtre(jeu, debut, fin, centre, recherche, centres, segment)
    # Quand le jeu a des jointures, préfixer les colonnes pour éviter les ambiguïtés.
    if jeu == "encaissements":
        selection = ", ".join(f"encaissements.{c}" for c, _ in colonnes)
    else:
        selection = ", ".join(noms)
    if jeu in JEUX_AVEC_AGENT:
        selection += ", pers_nom"
        noms = noms + ["nom_agent"]
    if jeu == "encaissements":
        # Tarif, secteur et statut de l'abonné, joints depuis la table tarifs.
        # Jointures par égalité seule ; le type est testé dans le CASE.
        selection += (
            ", CASE WHEN type_reference = 'ABONNE' THEN _ta.tarif"
            "       WHEN type_reference = 'GROUPEMENT' THEN _tg._grp_tarif END"
            ", CASE WHEN type_reference = 'ABONNE' THEN _ta.secteur"
            "       WHEN type_reference = 'GROUPEMENT' THEN _tg._grp_secteur END"
            ", CASE WHEN type_reference = 'ABONNE' THEN _ta.statut END"
        )
        noms = noms + ["tarif_abonne", "secteur", "statut_abonne"]
    sql = f"SELECT {selection} FROM {_source(jeu)} {condition}"
    if ordre:
        sql += f" ORDER BY {ordre}"
    if limite:
        sql += f" LIMIT {int(limite)}"
    lignes = _cur().execute(sql, params).fetchall()
    return [{n: _sortie(v) for n, v in zip(noms, ligne)} for ligne in lignes]


def encaisse_par_centre(debut: date, fin: date, segment: str | None = None) -> dict[str, tuple[int, float]]:
    """Reçus validés et montant encaissé par centre sur la période
    (`segment` : privés, État ou tous)."""
    lignes = _cur().execute(
        "SELECT centre, COUNT(*), COALESCE(SUM(montant), 0) FROM encaissements "
        "WHERE date_comptable BETWEEN ? AND ? AND statut = 'VALIDE'" + condition_segment(segment)
        + " GROUP BY centre", [debut, fin]).fetchall()
    return {str(c): (int(n), float(m)) for c, n, m in lignes if c is not None}


def encaisse_par_mois(debut: date, fin: date, segment: str | None = None
                      ) -> tuple[dict[tuple[str, str], tuple[int, float]], dict[str, str]]:
    """Reçus validés et montant encaissé par centre et par mois (AAAA-MM),
    et le nom de chaque centre."""
    lignes = _cur().execute(
        "SELECT centre, strftime(date_comptable, '%Y-%m'), COUNT(*), COALESCE(SUM(montant), 0), "
        "MAX(nom_centre) FROM encaissements "
        "WHERE date_comptable BETWEEN ? AND ? AND statut = 'VALIDE'" + condition_segment(segment)
        + " GROUP BY 1, 2", [debut, fin]).fetchall()
    valeurs, noms = {}, {}
    for c, mois, n, m, nom in lignes:
        if c is None:
            continue
        valeurs[(str(c), mois)] = (int(n), float(m))
        if nom:
            noms[str(c)] = nom
    return valeurs, noms


def resumer(jeu: str, debut: date, fin: date, centre: str | None = None,
            recherche: str | None = None) -> tuple[int, dict[str, float]]:
    """Nombre de lignes et somme de chaque colonne de montant, sur toutes les
    lignes de la sélection (sans limite d'affichage)."""
    _, colonnes = SCHEMAS[jeu]
    montants = [c for c, t in colonnes if t == "DOUBLE"]
    condition, params = _filtre(jeu, debut, fin, centre, recherche)
    sommes = "".join(f", COALESCE(SUM({c}), 0)" for c in montants)
    ligne = _cur().execute(f"SELECT COUNT(*){sommes} FROM {_source(jeu)} {condition}", params).fetchone()
    return int(ligne[0]), {c: round(float(v), 3) for c, v in zip(montants, ligne[1:])}


def repartir(jeu: str, colonne: str, montant: str, debut: date, fin: date,
             centre: str | None = None, recherche: str | None = None,
             centres: set[str] | None = None, segment: str | None = None) -> list[tuple[str, int, float]]:
    """Nombre de lignes et montant par valeur d'une colonne (le statut, par
    exemple), sur toutes les lignes de la sélection."""
    noms = [c for c, _ in SCHEMAS[jeu][1]]
    if colonne not in noms or montant not in noms:
        raise KeyError(colonne)
    condition, params = _filtre(jeu, debut, fin, centre, recherche, centres, segment)
    # Préfixe pour éviter l'ambiguïté avec les tables jointes.
    p = f"{jeu}." if jeu == "encaissements" else ""
    lignes = _cur().execute(
        f"SELECT {p}{colonne}, COUNT(*), COALESCE(SUM({p}{montant}), 0) FROM {_source(jeu)} {condition} "
        f"GROUP BY {p}{colonne} ORDER BY 2 DESC", params).fetchall()
    return [(v, int(n), round(float(m), 3)) for v, n, m in lignes]


def invalider(debut: date, fin: date):
    """Marque la période comme à recharger depuis Oracle, pour tous les jeux.

    Les lignes déjà chargées sont gardées : si Oracle est injoignable au moment
    du rechargement, elles restent servies."""
    with _transaction() as cur:
        cur.execute(
            "UPDATE sync SET charge_le = ? WHERE jeu NOT IN (?, ?, ?, ?) AND jour BETWEEN ? AND ?",
            [_A_RECHARGER, _JEU_CENTRES, _JEU_PERSONNEL, _JEU_TARIFS, _JEU_PLANIF, debut, fin],
        )


# --------------------------------------------------------------------------- relevé bancaire
# Écart minimal, en jours sans aucune ligne, pour signaler un trou dans les relevés.
JOURS_TROU_RELEVE = 7


def _cle_ligne_releve(banque, jour, libelle, debit, credit) -> tuple:
    return (str(banque or ""), _en_date(jour), str(libelle or ""),
            round(float(debit or 0), 2), round(float(credit or 0), 2))


def ajouter_releve(nom_fichier: str, lignes: list[dict]) -> dict:
    """Ajoute un relevé à ceux déjà importés : un versement de fin de mois est
    souvent déposé au début du mois suivant, il faut donc plusieurs relevés.

    Une ligne déjà présente (même banque, date, libellé et montants) n'est pas
    ajoutée une seconde fois : réimporter un fichier, ou importer un relevé
    annuel après des relevés mensuels, ne crée pas de doublon. Deux lignes
    identiques dans un même fichier sont, elles, bien gardées toutes les deux.
    Retourne le nombre de lignes lues, ajoutées et déjà présentes."""
    jours = [l["date_operation"] for l in lignes]
    with _transaction() as cur:
        presentes: dict[tuple, int] = {}
        for banque, jour, libelle, debit, credit, nb in cur.execute(
                "SELECT banque, date_operation, libelle, debit, credit, COUNT(*) FROM releve "
                "GROUP BY banque, date_operation, libelle, debit, credit").fetchall():
            cle = _cle_ligne_releve(banque, jour, libelle, debit, credit)
            presentes[cle] = presentes.get(cle, 0) + nb
        numero = cur.execute("SELECT COALESCE(MAX(ligne), 0) FROM releve").fetchone()[0]
        vues: dict[tuple, int] = {}
        nouvelles = []
        for l in lignes:
            cle = _cle_ligne_releve(l["banque"], l["date_operation"], l["libelle"], l["debit"], l["credit"])
            vues[cle] = vues.get(cle, 0) + 1
            if vues[cle] <= presentes.get(cle, 0):
                continue  # déjà importée
            numero += 1
            nouvelles.append({**l, "ligne": numero})  # numéro unique sur tous les relevés
        if nouvelles:
            _inserer(cur, "releve", COLONNES_RELEVE, nouvelles)
        cur.execute("DELETE FROM releve_info WHERE nom_fichier = ?", [nom_fichier])
        cur.execute("INSERT INTO releve_info VALUES (?,?,?,?,?,?)",
                    [nom_fichier, datetime.now(), len(lignes), min(jours), max(jours), len(nouvelles)])
    return {"nom_fichier": nom_fichier, "nb_lues": len(lignes), "nb_ajoutees": len(nouvelles),
            "nb_deja_presentes": len(lignes) - len(nouvelles)}


def supprimer_releve():
    """Efface tous les relevés importés (bouton « Réinitialiser »)."""
    with _transaction() as cur:
        cur.execute("DELETE FROM releve")
        cur.execute("DELETE FROM releve_info")


def info_releve() -> dict | None:
    """Relevés importés, vus comme un seul : fichiers, période couverte et
    jours sans aucune ligne entre le premier et le dernier jour (un mois oublié)."""
    fichiers = _cur().execute(
        "SELECT nom_fichier, importe_le, nb_lignes, date_min, date_max, nb_ajoutees FROM releve_info "
        "ORDER BY date_min, importe_le").fetchall()
    if not fichiers:
        return None
    nb_lignes, nb_versements, nb_banques, date_min, date_max = _cur().execute(
        "SELECT COUNT(*), COUNT(*) FILTER (WHERE classe = 'VERSEMENT ESPECES'), COUNT(DISTINCT banque), "
        "MIN(date_operation), MAX(date_operation) FROM releve"
    ).fetchone()
    jours = [j for (j,) in _cur().execute(
        "SELECT DISTINCT date_operation FROM releve ORDER BY 1").fetchall()]
    trous = []
    for avant, apres in zip(jours, jours[1:]):
        if (apres - avant).days > JOURS_TROU_RELEVE:
            trous.append({"debut": (avant + timedelta(days=1)).isoformat(),
                          "fin": (apres - timedelta(days=1)).isoformat()})
    return {
        "nom_fichier": ", ".join(f[0] for f in fichiers),
        "importe_le": max(f[1] for f in fichiers).isoformat(timespec="seconds"),
        "nb_lignes": nb_lignes, "date_min": _sortie(date_min), "date_max": _sortie(date_max),
        "nb_versements": nb_versements, "nb_banques": nb_banques,
        "fichiers": [
            {"nom_fichier": nom, "importe_le": quand.isoformat(timespec="seconds") if quand else None,
             "nb_lignes": nb, "date_min": _sortie(dmin), "date_max": _sortie(dmax),
             "nb_ajoutees": nb if ajoutees is None else ajoutees}
            for nom, quand, nb, dmin, dmax, ajoutees in fichiers
        ],
        "trous": trous,
    }


def lire_releve(debut: date, fin: date) -> list[dict]:
    noms = [c for c, _ in COLONNES_RELEVE]
    lignes = _cur().execute(
        f"SELECT {', '.join(noms)} FROM releve WHERE date_operation BETWEEN ? AND ? ORDER BY ligne",
        [debut, fin],
    ).fetchall()
    return [dict(zip(noms, ligne)) for ligne in lignes]


def banques_releve() -> list[dict]:
    lignes = _cur().execute(
        "SELECT banque, groupe, COUNT(*), COUNT(*) FILTER (WHERE classe = 'VERSEMENT ESPECES') "
        "FROM releve GROUP BY banque, groupe ORDER BY 3 DESC").fetchall()
    return [{"banque": b, "groupe": g, "nb_lignes": n, "nb_versements": v} for b, g, n, v in lignes]


# --------------------------------------------------------------------------- saisie de la DFC
_COLONNES_DFC = [("centre", "VARCHAR"), ("jour", "DATE"), ("especes", "DOUBLE"), ("cheques", "DOUBLE"),
                 ("virements", "DOUBLE"), ("verse", "DOUBLE"), ("banques", "VARCHAR"), ("depenses", "DOUBLE"),
                 ("rem", "DOUBLE"), ("observation", "VARCHAR")]


def remplacer_dfc(centre: str, nom_fichier: str, jours: list[dict], couverture: tuple[date, date]) -> dict:
    """Enregistre la saisie de la DFC d'un centre, à la place de ce qui était
    déjà importé pour ce centre sur la période couverte par le fichier."""
    centre = str(centre).strip()
    debut, fin = couverture
    lignes = [{"centre": centre, "jour": j["jour"], "especes": j["especes"], "cheques": j["cheques"],
               "virements": j["virements"], "verse": j["verse"],
               "banques": json.dumps(j["banques"], ensure_ascii=False), "depenses": j["depenses"],
               "rem": j["rem"], "observation": j["observation"]} for j in jours]
    dates = [j["jour"] for j in jours]
    with _transaction() as cur:
        cur.execute("DELETE FROM dfc WHERE centre = ? AND jour BETWEEN ? AND ?", [centre, debut, fin])
        cur.execute("DELETE FROM dfc_info WHERE centre = ? AND couverture_min <= ? AND couverture_max >= ?",
                    [centre, fin, debut])
        if lignes:
            _inserer_par_lots(cur, "dfc", _COLONNES_DFC, lignes)
        cur.execute("INSERT INTO dfc_info VALUES (?,?,?,?,?,?,?,?)",
                    [centre, nom_fichier, datetime.now(), len(lignes),
                     min(dates) if dates else None, max(dates) if dates else None, debut, fin])
    return {"centre": centre, "nom_fichier": nom_fichier, "nb_jours": len(lignes),
            "date_min": _sortie(min(dates)) if dates else None, "date_max": _sortie(max(dates)) if dates else None}


def supprimer_dfc():
    """Efface toute la saisie de la DFC importée (bouton « Réinitialiser »)."""
    with _transaction() as cur:
        cur.execute("DELETE FROM dfc")
        cur.execute("DELETE FROM dfc_info")


def info_dfc() -> dict | None:
    fichiers = _cur().execute(
        "SELECT centre, nom_fichier, importe_le, nb_jours, date_min, date_max FROM dfc_info "
        "ORDER BY TRY_CAST(centre AS BIGINT), centre, date_min").fetchall()
    if not fichiers:
        return None
    nb_jours, date_min, date_max = _cur().execute("SELECT COUNT(*), MIN(jour), MAX(jour) FROM dfc").fetchone()
    return {
        "nb_jours": nb_jours, "date_min": _sortie(date_min), "date_max": _sortie(date_max),
        "centres": list(dict.fromkeys(f[0] for f in fichiers)),
        "fichiers": [{"centre": c, "nom_fichier": n, "importe_le": q.isoformat(timespec="seconds") if q else None,
                      "nb_jours": nb, "date_min": _sortie(dmin), "date_max": _sortie(dmax)}
                     for c, n, q, nb, dmin, dmax in fichiers],
    }


def lire_dfc(debut: date, fin: date, centre: str | None = None) -> list[dict]:
    noms = [c for c, _ in _COLONNES_DFC]
    sql = f"SELECT {', '.join(noms)} FROM dfc WHERE jour BETWEEN ? AND ?"
    params: list = [debut, fin]
    if centre:
        sql += " AND centre = ?"
        params.append(str(centre).strip())
    lignes = []
    for ligne in _cur().execute(sql, params).fetchall():
        l = dict(zip(noms, ligne))
        l["jour"] = _sortie(l["jour"])
        l["banques"] = json.loads(l["banques"] or "{}")
        lignes.append(l)
    return lignes


def couvertures_dfc(centre: str | None = None) -> dict[str, list[tuple[date, date]]]:
    """Périodes couvertes par les fichiers importés, par centre : un fichier
    d'une année couvre l'année, un fichier d'un seul mois ne couvre que ce mois."""
    sql, params = "SELECT centre, couverture_min, couverture_max FROM dfc_info", []
    if centre:
        sql += " WHERE centre = ?"
        params.append(str(centre).strip())
    periodes: dict[str, list[tuple[date, date]]] = {}
    for c, debut, fin in _cur().execute(sql, params).fetchall():
        if debut and fin:
            periodes.setdefault(c, []).append((debut, fin))
    return periodes


# --------------------------------------------------------------------------- reçus manuels
_COLONNES_MANUELS = [("nom_fichier", "VARCHAR"), ("centre_fichier", "VARCHAR"), ("date_recu", "DATE"),
                     ("num_recu_manuel", "VARCHAR"), ("reference", "VARCHAR"), ("montant", "DOUBLE")]


def remplacer_manuels(nom_fichier: str, recus: list[dict]) -> dict:
    """Enregistre les reçus manuels d'un fichier, à la place de ceux d'un
    fichier du même nom déjà importé."""
    lignes = [{"nom_fichier": nom_fichier, **{c: r[c] for c, _ in _COLONNES_MANUELS[1:]}} for r in recus]
    dates = [r["date_recu"] for r in recus if r["date_recu"]]
    with _transaction() as cur:
        cur.execute("DELETE FROM recus_manuels WHERE nom_fichier = ?", [nom_fichier])
        cur.execute("DELETE FROM recus_manuels_info WHERE nom_fichier = ?", [nom_fichier])
        if lignes:
            _inserer(cur, "recus_manuels", _COLONNES_MANUELS, lignes)
        cur.execute("INSERT INTO recus_manuels_info VALUES (?,?,?)", [nom_fichier, datetime.now(), len(lignes)])
    return {"nom_fichier": nom_fichier, "nb_recus": len(lignes),
            "montant": round(sum(r["montant"] for r in recus), 3),
            "date_min": _sortie(min(dates)) if dates else None, "date_max": _sortie(max(dates)) if dates else None}


_ESPECES = "UPPER(TRIM(COALESCE(mode_paiement, ''))) LIKE 'ESP%'"
_CENTRE_SAISIE = ("CASE WHEN regexp_full_match(num_recu, '[0-9]{13,}') "
                  "THEN substr(num_recu, 1, length(num_recu) - 12) ELSE '' END")


def especes_des_references(debut: date, fin: date, references: list[str]) -> list[dict]:
    """Reçus d'espèces du CRM de ces références seulement (vérification des
    reçus manuels) : DuckDB ne renvoie que les quelques lignes utiles."""
    if not references:
        return []
    noms = ["centre", "num_recu", "reference", "matricule", "date_comptable", "mode_paiement", "montant", "statut"]
    lignes = _cur().execute(
        f"SELECT {', '.join(noms)}, pers_nom FROM {_source('encaissements')} "
        f"WHERE date_comptable BETWEEN ? AND ? AND {_ESPECES} "
        "AND reference IN (SELECT UNNEST(?::VARCHAR[]))",
        [debut, fin, sorted(set(references))]).fetchall()
    return [{n: _sortie(v) for n, v in zip(noms + ["nom_agent"], l)} for l in lignes]


def base_especes(debut: date, fin: date, centres: list[str]) -> dict:
    """Reçus d'espèces non annulés saisis par les caisses de ces centres :
    nombre, abonnés distincts et références qui n'ont pas 8 chiffres."""
    condition = (f"WHERE date_comptable BETWEEN ? AND ? AND {_ESPECES} AND COALESCE(statut, '') <> 'ANNULE' "
                 f"AND ({_CENTRE_SAISIE}) IN (SELECT UNNEST(?::VARCHAR[]))")
    params = [debut, fin, sorted(set(centres))]
    nb, nb_abonnes = _cur().execute(
        f"SELECT COUNT(*), COUNT(DISTINCT reference) FROM encaissements {condition}", params).fetchone()
    invalides = _cur().execute(
        f"SELECT {_CENTRE_SAISIE}, date_comptable, COALESCE(reference, ''), num_recu, montant FROM encaissements "
        f"{condition} AND NOT regexp_full_match(COALESCE(reference, ''), '[0-9]{{8}}') "
        "ORDER BY date_comptable, num_recu LIMIT 5000", params).fetchall()
    return {"nb": int(nb), "nb_abonnes": int(nb_abonnes),
            "invalides": [{"centre": c, "date": _sortie(d), "reference": r, "num_recu": n, "montant": m}
                          for c, d, r, n, m in invalides]}


def supprimer_manuels():
    """Efface les reçus manuels importés (bouton « Réinitialiser »)."""
    with _transaction() as cur:
        cur.execute("DELETE FROM recus_manuels")
        cur.execute("DELETE FROM recus_manuels_info")


def info_manuels() -> dict | None:
    fichiers = _cur().execute(
        "SELECT nom_fichier, importe_le, nb_recus FROM recus_manuels_info ORDER BY importe_le").fetchall()
    if not fichiers:
        return None
    nb, montant, date_min, date_max, sans_date = _cur().execute(
        "SELECT COUNT(*), SUM(montant), MIN(date_recu), MAX(date_recu), COUNT(*) FILTER (WHERE date_recu IS NULL) "
        "FROM recus_manuels").fetchone()
    return {
        "nb_recus": nb, "montant": montant or 0.0, "date_min": _sortie(date_min), "date_max": _sortie(date_max),
        "nb_sans_date": sans_date,
        "fichiers": [{"nom_fichier": n, "importe_le": q.isoformat(timespec="seconds") if q else None, "nb_recus": k}
                     for n, q, k in fichiers],
    }


def lire_manuels() -> list[dict]:
    noms = [c for c, _ in _COLONNES_MANUELS]
    return [dict(zip(noms, l)) for l in _cur().execute(
        f"SELECT {', '.join(noms)} FROM recus_manuels ORDER BY date_recu NULLS LAST, centre_fichier").fetchall()]


# --------------------------------------------------------------------------- reçus non validés consultés
def recus_vus() -> set[str]:
    """Numéros des reçus non validés déjà consultés."""
    return {n for (n,) in _cur().execute("SELECT num_recu FROM non_valides_vus").fetchall()}


def marquer_vus(numeros: list[str]):
    """Note ces reçus comme consultés (sans doublon)."""
    deja = recus_vus()
    nouveaux = [{"num_recu": n, "vu_le": datetime.now()} for n in dict.fromkeys(numeros) if n not in deja]
    if not nouveaux:
        return
    with _transaction() as cur:
        _inserer_par_lots(cur, "non_valides_vus", [("num_recu", "VARCHAR"), ("vu_le", "TIMESTAMP")], nouveaux)


def oublier_vus(numeros: list[str]):
    """Retire ces reçus de la liste des reçus consultés."""
    numeros = list(dict.fromkeys(numeros))
    if not numeros:
        return
    with _transaction() as cur:
        for i in range(0, len(numeros), _LOT):
            lot = numeros[i:i + _LOT]
            cur.execute(f"DELETE FROM non_valides_vus WHERE num_recu IN ({','.join('?' * len(lot))})", lot)


# --------------------------------------------------------------------------- centres
def centres_a_jour() -> bool:
    n = _cur().execute(
        "SELECT COUNT(*) FROM sync WHERE jeu = ? AND jour = ?", [_JEU_CENTRES, date.today()]
    ).fetchone()[0]
    return n > 0


def remplacer_centres(lignes: list[dict]):
    with _transaction() as cur:
        cur.execute("DELETE FROM centres")
        _inserer_par_lots(cur, "centres", [("centre", "VARCHAR"), ("nom_centre", "VARCHAR")], lignes)
        cur.execute("DELETE FROM sync WHERE jeu = ?", [_JEU_CENTRES])
        cur.execute("INSERT INTO sync VALUES (?,?,?,?,?)",
                    [_JEU_CENTRES, date.today(), TOUS, datetime.now(), len(lignes)])


def lire_centres() -> list[dict]:
    lignes = _cur().execute("SELECT centre, nom_centre FROM centres").fetchall()
    return [{"centre": c, "nom_centre": n} for c, n in lignes]


# --------------------------------------------------------------------------- agents
def personnel_a_jour() -> bool:
    n = _cur().execute(
        "SELECT COUNT(*) FROM sync WHERE jeu = ? AND jour = ?", [_JEU_PERSONNEL, date.today()]
    ).fetchone()[0]
    return n > 0


def remplacer_personnel(lignes: list[dict]):
    """Enregistre les agents, une seule ligne par matricule : la jointure ne
    doit jamais dédoubler un reçu. Deux agents du même matricule sont réunis."""
    noms: dict[str, list[str]] = {}
    for l in lignes:
        matricule = str(l.get("matricule") or "").strip()
        nom = " ".join(str(l.get("nom") or "").split())
        if not matricule or not nom:
            continue
        if nom not in noms.setdefault(matricule, []):
            noms[matricule].append(nom)
    agents = [{"pers_matricule": m, "pers_nom": " / ".join(n)} for m, n in sorted(noms.items())]
    with _transaction() as cur:
        cur.execute("DELETE FROM personnel")
        _inserer_par_lots(cur, "personnel", [("pers_matricule", "VARCHAR"), ("pers_nom", "VARCHAR")], agents)
        cur.execute("DELETE FROM sync WHERE jeu = ?", [_JEU_PERSONNEL])
        cur.execute("INSERT INTO sync VALUES (?,?,?,?,?)",
                    [_JEU_PERSONNEL, date.today(), TOUS, datetime.now(), len(agents)])


# --------------------------------------------------------------------------- tarifs
_COLONNES_TARIFS = [("abn_id", "VARCHAR"), ("grp_code", "VARCHAR"), ("tarif", "VARCHAR"),
                    ("secteur", "VARCHAR"), ("statut", "VARCHAR"), ("sect_id", "VARCHAR")]


def tarifs_a_jour() -> bool:
    n = _cur().execute(
        "SELECT COUNT(*) FROM sync WHERE jeu = ? AND jour = ?", [_JEU_TARIFS, date.today()]
    ).fetchone()[0]
    return n > 0


def info_tarifs() -> dict | None:
    """Date du dernier chargement des tarifs et nombre d'abonnements, ou rien."""
    ligne = _cur().execute(
        "SELECT charge_le, nb_lignes FROM sync WHERE jeu = ? ORDER BY charge_le DESC LIMIT 1", [_JEU_TARIFS]
    ).fetchone()
    return {"charge_le": _sortie(ligne[0]), "nb_abonnements": ligne[1]} if ligne else None


def remplacer_tarifs(lignes: list[dict]):
    with _transaction() as cur:
        cur.execute("DELETE FROM tarifs")
        _inserer(cur, "tarifs", _COLONNES_TARIFS, lignes)
        cur.execute("DELETE FROM sync WHERE jeu = ?", [_JEU_TARIFS])
        cur.execute("INSERT INTO sync VALUES (?,?,?,?,?)",
                    [_JEU_TARIFS, date.today(), TOUS, datetime.now(), len(lignes)])


_SANS_TARIF = "Sans tarif"
_SANS_ABONNEMENT = "Sans abonnement (demande)"


def references_tarifs(debut: date, fin: date, centre: str | None = None, recherche: str | None = None,
                      tarif: str | None = None, limite: int = 5000) -> dict:
    """Chaque référence encaissée (reçus validés) sur la période, avec son tarif.

    Abonné : tarif de l'abonnement. Groupement : tarifs de ses abonnements.
    Demande et autre : pas de tarif (aucun abonnement)."""
    sql = "WHERE date_comptable BETWEEN ? AND ? AND statut = 'VALIDE' AND reference IS NOT NULL"
    params: list = [debut, fin]
    if centre:
        sql += " AND centre = ?"
        params.append(str(centre).strip())
    base = f"""
WITH refs AS (
    SELECT reference, type_reference, MIN(centre) AS centre, MIN(nom_centre) AS nom_centre,
           COUNT(*) AS nb_recus, SUM(montant) AS montant
    FROM encaissements {sql}
    GROUP BY reference, type_reference
),
grp AS (
    SELECT grp_code, string_agg(DISTINCT tarif, ', ' ORDER BY tarif) AS tarif
    FROM tarifs WHERE grp_code IS NOT NULL AND tarif IS NOT NULL
    GROUP BY grp_code
),
t AS (
    SELECT r.*, CASE WHEN r.type_reference NOT IN ('ABONNE', 'GROUPEMENT') THEN '{_SANS_ABONNEMENT}'
                     ELSE COALESCE(CASE WHEN r.type_reference = 'ABONNE' THEN a.tarif ELSE g.tarif END,
                                   '{_SANS_TARIF}') END AS tarif,
           CASE WHEN r.type_reference = 'ABONNE' THEN a.statut END AS statut
    FROM refs r
    -- Jointures sur la seule égalité : une condition en plus dans le ON fait
    -- comparer chaque référence à chaque abonnement (très lent). Le type est
    -- testé dans le CASE ci-dessus.
    LEFT JOIN tarifs a ON a.abn_id = r.reference
    LEFT JOIN grp g ON g.grp_code = r.reference
)"""
    cur = _cur()
    synthese = cur.execute(
        base + " SELECT tarif, COUNT(*), SUM(nb_recus), COALESCE(SUM(montant), 0) FROM t GROUP BY tarif",
        params).fetchall()
    filtre, p2 = "", list(params)
    if tarif:
        filtre += " AND tarif = ?"
        p2.append(tarif)
    texte = (recherche or "").strip().lower()
    if texte:
        filtre += " AND (contains(lower(reference), ?) OR contains(lower(COALESCE(nom_centre, '')), ?))"
        p2.extend([texte, texte])
    total = cur.execute(base + f" SELECT COUNT(*) FROM t WHERE TRUE {filtre}", p2).fetchone()[0]
    noms = ["reference", "type_reference", "centre", "nom_centre", "tarif", "statut", "nb_recus", "montant"]
    lignes = cur.execute(
        base + f" SELECT {', '.join(noms)} FROM t WHERE TRUE {filtre} "
        f"ORDER BY TRY_CAST(centre AS BIGINT) NULLS LAST, centre, type_reference, reference LIMIT {int(limite)}",
        p2).fetchall()
    return {
        "synthese": [{"tarif": t, "nb_references": int(n), "nb_recus": int(r), "montant": float(m)}
                     for t, n, r, m in synthese],
        "lignes": [{k: _sortie(v) for k, v in zip(noms, l)} for l in lignes],
        "total_lignes": int(total),
    }


# --------------------------------------------------------------------------- état
def etat() -> dict:
    """Résumé de la base locale, affiché par /api/health."""
    lignes = _cur().execute(
        "SELECT jeu, COUNT(DISTINCT jour), MIN(jour), MAX(jour), MAX(charge_le), "
        "COUNT(DISTINCT CASE WHEN centre != ? THEN centre END) "
        "FROM sync WHERE jeu NOT IN (?, ?, ?, ?) GROUP BY jeu ORDER BY jeu",
        [TOUS, _JEU_CENTRES, _JEU_PERSONNEL, _JEU_TARIFS, _JEU_PLANIF]
    ).fetchall()
    return {
        "fichier": str(_chemin()),
        "jeux": [
            {
                "jeu": jeu,
                "jours_charges": nb,
                "premier_jour": _sortie(premier),
                "dernier_jour": _sortie(dernier),
                "derniere_synchronisation": (
                    quand.isoformat(timespec="seconds") if quand and quand > _A_RECHARGER else None),
                "centres_charges_seuls": nb_centres,
            }
            for jeu, nb, premier, dernier, quand, nb_centres in lignes
        ],
    }


# --------------------------------------------------------------------------- chutes d'encaissement par secteur

def chutes_secteur(jour_ref: date) -> list[dict]:
    """Compare les encaissements par secteur du mois en cours (jusqu'au jour
    courant) avec la même tranche de jours du mois M-2 (même parité).

    Seuls les reçus de type ABONNE (secteur unique) sont pris en compte.
    Retourne une liste triée par variation croissante.
    """
    from dateutil.relativedelta import relativedelta
    import calendar

    mois_courant_debut = jour_ref.replace(day=1)
    jour_du_mois = jour_ref.day

    mois_ref_debut = mois_courant_debut - relativedelta(months=2)
    dernier_jour_ref = calendar.monthrange(mois_ref_debut.year, mois_ref_debut.month)[1]
    jour_fin_ref = min(jour_du_mois, dernier_jour_ref)
    mois_ref_fin = mois_ref_debut.replace(day=jour_fin_ref)

    sql = """
    WITH actuel AS (
        SELECT _ta.secteur AS secteur,
               SUM(encaissements.montant) AS montant
        FROM encaissements
        JOIN tarifs _ta ON _ta.abn_id = encaissements.reference
        WHERE encaissements.type_reference = 'ABONNE'
          AND encaissements.date_comptable BETWEEN ? AND ?
          AND encaissements.statut = 'VALIDE'
          AND _ta.secteur IS NOT NULL
        GROUP BY _ta.secteur
    ),
    ref AS (
        SELECT _ta.secteur AS secteur,
               SUM(encaissements.montant) AS montant
        FROM encaissements
        JOIN tarifs _ta ON _ta.abn_id = encaissements.reference
        WHERE encaissements.type_reference = 'ABONNE'
          AND encaissements.date_comptable BETWEEN ? AND ?
          AND encaissements.statut = 'VALIDE'
          AND _ta.secteur IS NOT NULL
        GROUP BY _ta.secteur
    )
    SELECT COALESCE(a.secteur, r.secteur) AS secteur,
           COALESCE(a.montant, 0) AS montant_actuel,
           COALESCE(r.montant, 0) AS montant_ref,
           CASE WHEN COALESCE(r.montant, 0) > 0
                THEN ROUND((COALESCE(a.montant, 0) - r.montant) / r.montant * 100, 1)
                ELSE NULL END AS variation_pct
    FROM actuel a
    FULL OUTER JOIN ref r ON r.secteur = a.secteur
    WHERE COALESCE(r.montant, 0) > 0
    ORDER BY variation_pct ASC NULLS LAST
    """
    lignes = _cur().execute(sql, [
        mois_courant_debut, jour_ref,
        mois_ref_debut, mois_ref_fin,
    ]).fetchall()
    return [
        {"secteur": s, "montant_actuel": a or 0, "montant_ref": r or 0, "variation_pct": v}
        for s, a, r, v in lignes
        if s is not None
    ]


def chutes_secteur_detail(jour_ref: date, secteur: str) -> list[dict]:
    """Détail par abonné pour un secteur donné : montant M vs M-2."""
    from dateutil.relativedelta import relativedelta
    import calendar

    mois_courant_debut = jour_ref.replace(day=1)
    jour_du_mois = jour_ref.day

    mois_ref_debut = mois_courant_debut - relativedelta(months=2)
    dernier_jour_ref = calendar.monthrange(mois_ref_debut.year, mois_ref_debut.month)[1]
    jour_fin_ref = min(jour_du_mois, dernier_jour_ref)
    mois_ref_fin = mois_ref_debut.replace(day=jour_fin_ref)

    sql = """
    WITH actuel AS (
        SELECT encaissements.reference AS reference,
               _ta.tarif AS tarif,
               _ta.statut AS statut_abn,
               SUM(encaissements.montant) AS montant
        FROM encaissements
        JOIN tarifs _ta ON _ta.abn_id = encaissements.reference
        WHERE encaissements.type_reference = 'ABONNE'
          AND _ta.secteur = ?
          AND encaissements.date_comptable BETWEEN ? AND ?
          AND encaissements.statut = 'VALIDE'
        GROUP BY encaissements.reference, _ta.tarif, _ta.statut
    ),
    ref AS (
        SELECT encaissements.reference AS reference,
               SUM(encaissements.montant) AS montant
        FROM encaissements
        JOIN tarifs _ta ON _ta.abn_id = encaissements.reference
        WHERE encaissements.type_reference = 'ABONNE'
          AND _ta.secteur = ?
          AND encaissements.date_comptable BETWEEN ? AND ?
          AND encaissements.statut = 'VALIDE'
        GROUP BY encaissements.reference
    )
    SELECT COALESCE(a.reference, r.reference) AS reference,
           a.tarif AS tarif,
           a.statut_abn AS statut_abn,
           COALESCE(a.montant, 0) AS montant_actuel,
           COALESCE(r.montant, 0) AS montant_ref,
           CASE WHEN COALESCE(r.montant, 0) > 0
                THEN ROUND((COALESCE(a.montant, 0) - r.montant) / r.montant * 100, 1)
                ELSE NULL END AS variation_pct
    FROM actuel a
    FULL OUTER JOIN ref r ON r.reference = a.reference
    ORDER BY variation_pct ASC NULLS LAST
    """
    lignes = _cur().execute(sql, [
        secteur, mois_courant_debut, jour_ref,
        secteur, mois_ref_debut, mois_ref_fin,
    ]).fetchall()
    return [
        {"reference": ref, "tarif": tarif, "statut_abn": statut, "montant_actuel": a or 0, "montant_ref": r or 0, "variation_pct": v}
        for ref, tarif, statut, a, r, v in lignes
    ]



# --------------------------------------------------------------------------- alertes secteurs (planning)
_COLONNES_PLANIF = [("centre", "VARCHAR"), ("sect_id", "VARCHAR"), ("secteur", "VARCHAR"),
                    ("frequence", "VARCHAR"), ("annee", "INTEGER"), ("mois", "INTEGER"),
                    ("date_releve_plan", "DATE"), ("date_plan", "DATE"),
                    ("date_fact_reelle", "DATE"), ("nb_factures", "INTEGER")]


def planif_a_jour() -> bool:
    n = _cur().execute(
        "SELECT COUNT(*) FROM sync WHERE jeu = ? AND jour = ?", [_JEU_PLANIF, date.today()]
    ).fetchone()[0]
    return n > 0


def info_planif() -> dict | None:
    ligne = _cur().execute(
        "SELECT charge_le, nb_lignes FROM sync WHERE jeu = ? ORDER BY charge_le DESC LIMIT 1", [_JEU_PLANIF]
    ).fetchone()
    return {"charge_le": _sortie(ligne[0]), "nb_lignes": ligne[1]} if ligne else None


def remplacer_planif(lignes: list[dict]):
    with _transaction() as cur:
        cur.execute("DELETE FROM planif_secteurs")
        _inserer(cur, "planif_secteurs", _COLONNES_PLANIF, lignes)
        cur.execute("DELETE FROM sync WHERE jeu = ?", [_JEU_PLANIF])
        cur.execute("INSERT INTO sync VALUES (?,?,?,?,?)",
                    [_JEU_PLANIF, date.today(), TOUS, datetime.now(), len(lignes)])


def secteurs_planifies(annee: int, mois: int, centre: str | None = None) -> list[dict]:
    """Secteurs planifiés du mois (planning PLANIFDETAILLE)."""
    sql = ("SELECT sect_id, centre, secteur, frequence, date_releve_plan, date_plan, date_fact_reelle "
           "FROM planif_secteurs WHERE annee = ? AND mois = ?")
    params: list = [annee, mois]
    if centre:
        sql += " AND centre = ?"
        params.append(str(centre).strip())
    noms = ["sect_id", "centre", "secteur", "frequence", "date_releve_plan", "date_plan", "date_fact_reelle"]
    return [dict(zip(noms, l)) for l in _cur().execute(sql, params).fetchall()]


def nb_secteurs_planifies(annee: int, centre: str | None = None) -> list[dict]:
    """Nombre de secteurs planifiés par centre et par mois (mensuels, bimensuels, facturés)."""
    sql = ("SELECT centre, mois, COUNT(DISTINCT sect_id), "
           "COUNT(DISTINCT CASE WHEN frequence = 'MENSUEL' THEN sect_id END), "
           "COUNT(DISTINCT CASE WHEN frequence = 'BIMENSUEL' THEN sect_id END), "
           "COUNT(DISTINCT CASE WHEN date_fact_reelle IS NOT NULL THEN sect_id END) "
           "FROM planif_secteurs WHERE annee = ?")
    params: list = [annee]
    if centre:
        sql += " AND centre = ?"
        params.append(str(centre).strip())
    sql += " GROUP BY centre, mois"
    return [{"centre": c, "mois": int(m), "nb": int(n), "nb_mensuels": int(nm),
             "nb_bimensuels": int(nb), "nb_factures": int(nf)}
            for c, m, n, nm, nb, nf in _cur().execute(sql, params).fetchall()]


def secteurs_vus(jour: date) -> set[str]:
    return {r[0] for r in _cur().execute(
        "SELECT DISTINCT sect_id FROM alertes_secteurs_vus WHERE jour = ?", [jour]).fetchall()}


def marquer_secteurs_vus(jour: date, sect_ids: list[str]):
    deja = secteurs_vus(jour)
    nouveaux = [s for s in dict.fromkeys(sect_ids) if s not in deja]
    if not nouveaux:
        return
    maintenant = datetime.now()
    with _transaction() as cur:
        cur.executemany("INSERT INTO alertes_secteurs_vus VALUES (?, ?, ?)",
                        [[jour, s, maintenant] for s in nouveaux])


# --------------------------------------------------------------------------- encaissements par secteur
# Clé d'un secteur : « centre-secteur », le centre étant celui de la ligne
# d'encaissement (comme dans l'onglet Encaissements) et le secteur celui de
# l'abonné (SECT_CODE). Ce qui n'est pas rattachable à un secteur (groupements,
# devis, autres, abonnés sans secteur) va dans « centre-HS » : la somme des
# lignes d'un centre est ainsi égale au total de l'onglet Encaissements.
SECTEUR_HORS = "HS"
_CLE_SECTEUR = (f"TRIM(e.centre) || '-' || CASE WHEN e.type_reference = 'ABONNE' AND NULLIF(TRIM(t.secteur), '') "
                f"IS NOT NULL THEN TRIM(t.secteur) ELSE '{SECTEUR_HORS}' END")
# Jointure simple sur l'abonné (une condition en plus dans le ON fait passer
# DuckDB en comparaison de toutes les paires, ce qui bloque le serveur) ; le
# type ABONNE est vérifié dans _CLE_SECTEUR.
_FROM_SECTEUR = ("FROM encaissements e LEFT JOIN tarifs t ON t.abn_id = e.reference "
                 "WHERE e.statut = 'VALIDE' AND e.centre IS NOT NULL")


def tous_secteurs(annee: int, mois: int, debut: date, fin: date) -> list[dict]:
    """Secteurs (clé centre-secteur) ayant des encaissements sur [debut, fin],
    avec leur fréquence (dernier planning connu) et leur passage du mois."""
    sql = f"""
    WITH k AS (
        SELECT DISTINCT {_CLE_SECTEUR} AS cle {_FROM_SECTEUR}
          AND e.date_comptable BETWEEN $debut AND $fin
    ),
    k2 AS (
        SELECT cle, split_part(cle, '-', 1) AS centre,
               substr(cle, length(split_part(cle, '-', 1)) + 2) AS secteur FROM k
    ),
    dern AS (
        SELECT TRIM(centre) AS centre, TRIM(secteur) AS secteur, frequence FROM planif_secteurs
        QUALIFY ROW_NUMBER() OVER (PARTITION BY TRIM(centre), TRIM(secteur) ORDER BY annee DESC, mois DESC) = 1
    ),
    moisc AS (
        SELECT TRIM(centre) AS centre, TRIM(secteur) AS secteur, date_plan, date_fact_reelle FROM planif_secteurs
        WHERE annee = $annee AND mois = $mois
        QUALIFY ROW_NUMBER() OVER (PARTITION BY TRIM(centre), TRIM(secteur) ORDER BY date_plan DESC NULLS LAST) = 1
    )
    SELECT k2.cle, k2.centre, k2.secteur, d.frequence, m.date_plan, m.date_fact_reelle,
           m.centre IS NOT NULL AS planifie
    FROM k2
    LEFT JOIN dern d  ON d.centre = k2.centre AND d.secteur = k2.secteur
    LEFT JOIN moisc m ON m.centre = k2.centre AND m.secteur = k2.secteur
    """
    noms = ["sect_id", "centre", "secteur", "frequence", "date_plan", "date_fact_reelle", "planifie"]
    lignes = _cur().execute(sql, {"annee": annee, "mois": mois, "debut": debut, "fin": fin}).fetchall()
    return [dict(zip(noms, l)) for l in lignes]


def encaissements_par_secteur_jour(debut: date, fin: date, cles: list[str] | None = None) -> dict:
    """{(clé secteur, jour): montant} de tous les reçus validés."""
    sql = f"SELECT {_CLE_SECTEUR} AS cle, e.date_comptable, SUM(e.montant) {_FROM_SECTEUR} AND e.date_comptable BETWEEN ? AND ?"
    params: list = [debut, fin]
    if cles is not None:
        if not cles:
            return {}
        sql += f" AND {_CLE_SECTEUR} IN ({','.join('?' * len(cles))})"
        params.extend(cles)
    sql += " GROUP BY 1, 2"
    return {(c, _en_date(j)): float(m or 0) for c, j, m in _cur().execute(sql, params).fetchall()}


def agreg_secteurs(debut: date, fin: date) -> dict:
    """{clé secteur: (montant, nb_recus, nb_payeurs)} des reçus validés sur [debut, fin]."""
    sql = (f"SELECT {_CLE_SECTEUR} AS cle, SUM(e.montant), COUNT(DISTINCT e.num_recu), COUNT(DISTINCT e.reference) "
           f"{_FROM_SECTEUR} AND e.date_comptable BETWEEN ? AND ? GROUP BY 1")
    return {c: (float(m or 0), int(n or 0), int(a or 0)) for c, m, n, a in _cur().execute(sql, [debut, fin]).fetchall()}


def encaissements_secteur_abonnes(cle: str, d1: date, f1: date, d2: date, f2: date) -> list[dict]:
    """Par abonné (ou référence) du secteur : montant de [d1, f1] et de la référence [d2, f2]."""
    sql = f"""
    SELECT e.reference, MIN(t.tarif), MIN(COALESCE(t.statut, e.type_reference)),
           SUM(CASE WHEN e.date_comptable BETWEEN $d1 AND $f1 THEN e.montant ELSE 0 END),
           SUM(CASE WHEN e.date_comptable BETWEEN $d2 AND $f2 THEN e.montant ELSE 0 END)
    {_FROM_SECTEUR} AND {_CLE_SECTEUR} = $cle
      AND ((e.date_comptable BETWEEN $d1 AND $f1) OR (e.date_comptable BETWEEN $d2 AND $f2))
    GROUP BY e.reference
    """
    lignes = _cur().execute(sql, {"cle": cle, "d1": d1, "f1": f1, "d2": d2, "f2": f2}).fetchall()
    return [{"reference": r, "tarif": t, "statut_abonne": st, "montant_actuel": float(a or 0),
             "montant_ref": float(b or 0)} for r, t, st, a, b in lignes]

