"""API du contrôle des versements : données, flux temps réel (SSE) et exports."""
import asyncio
import re
import json
import logging
import signal
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import comparaison, db, dfc, evolution, exports, manuels, releve, service, store, zones
from .config import get_settings

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s : %(message)s",
)
log = logging.getLogger("controle.api")

# Journal aussi écrit dans backend/logs/controle.log (lisible sans le terminal).
try:
    from logging.handlers import RotatingFileHandler
    from pathlib import Path as _Path
    _dossier_logs = _Path(__file__).resolve().parents[2] / "logs"
    _dossier_logs.mkdir(exist_ok=True)
    _fichier_log = RotatingFileHandler(_dossier_logs / "controle.log", maxBytes=5_000_000, backupCount=2,
                                       encoding="utf-8")
    _fichier_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] : %(message)s"))
    logging.getLogger().addHandler(_fichier_log)
except OSError:
    pass


# --------------------------------------------------------------------------- temps réel
class Diffuseur:
    """Envoie un message à tous les navigateurs connectés au flux SSE."""

    def __init__(self):
        self._abonnes: set[asyncio.Queue] = set()

    def abonner(self) -> asyncio.Queue:
        file = asyncio.Queue(maxsize=20)
        self._abonnes.add(file)
        return file

    def desabonner(self, file: asyncio.Queue):
        self._abonnes.discard(file)

    def publier(self, message: dict):
        for file in list(self._abonnes):
            with suppress(asyncio.QueueFull):
                file.put_nowait(message)

    @property
    def nb_abonnes(self) -> int:
        return len(self._abonnes)


diffuseur = Diffuseur()
etat = {"dernier_rafraichissement": None, "base_ok": None, "erreur": None}


def _rafraichir_sync(debut: date | None, fin: date | None):
    """Recharge Oracle -> DuckDB (appel bloquant).

    Sans période (boucle automatique) : le contrôle du jour est rechargé.
    Avec période (bouton « Actualiser ») : la période est seulement marquée à
    recharger ; chaque écran relance ensuite son chargement en arrière-plan.
    """
    if debut is None or fin is None:
        aujourd_hui = date.today()
        service.synchroniser(aujourd_hui, aujourd_hui, jeux=("controle",))
    else:
        store.invalider(debut, fin)


async def rafraichir(origine: str, debut: date | None = None, fin: date | None = None) -> dict:
    try:
        await asyncio.to_thread(_rafraichir_sync, debut, fin)
        if service.etat_oracle["ok"] is False:
            # Les données locales ont été servies à la place d'Oracle.
            etat["base_ok"] = False
            etat["erreur"] = ("Oracle est injoignable : les données affichées viennent de la base "
                              f"locale DuckDB. Détail : {service.etat_oracle['erreur']}")
        else:
            etat["base_ok"] = True
            etat["erreur"] = None
    except (db.BaseIndisponible, store.BaseLocaleIndisponible) as exc:
        etat["base_ok"] = False
        etat["erreur"] = str(exc)
        log.warning("Rafraîchissement impossible : %s", exc)
    except Exception as exc:  # une erreur ne doit jamais arrêter la boucle
        etat["base_ok"] = False
        etat["erreur"] = f"Erreur inattendue : {exc}"
        log.exception("Erreur pendant le rafraîchissement")
    etat["dernier_rafraichissement"] = datetime.now().isoformat(timespec="seconds")
    message = {"type": "refresh", "origine": origine, **etat,
               "intervalle_secondes": settings.refresh_seconds}
    diffuseur.publier(message)
    return message


async def _boucle_rafraichissement():
    while True:
        await rafraichir("automatique")
        await asyncio.sleep(settings.refresh_seconds)


async def _boucle_rapport_chutes():
    """Chaque soir à 23 h (HEURE_RAPPORT) : rapport Excel des secteurs en chute
    du jour, cumul du 1er au jour, dans backend/sorties_alertes. Le bandeau rouge
    passe au jour même à 23 h (rafraîchissement suivant). Au démarrage après
    23 h, le rapport du jour est écrit s'il manque."""
    while True:
        maintenant = datetime.now()
        cible = maintenant.replace(hour=service.HEURE_RAPPORT, minute=0, second=0, microsecond=0)
        deja = (service.DOSSIER_RAPPORTS / f"chutes_secteur_{maintenant.date().isoformat()}.xlsx").exists()
        if maintenant >= cible and deja:
            cible += timedelta(days=1)
        if cible > maintenant:
            await asyncio.sleep((cible - maintenant).total_seconds())
        jour = datetime.now().date()
        for _ in range(60):  # données encore en chargement : on réessaie chaque minute, 1 h au plus
            try:
                await asyncio.to_thread(service.generer_rapport_chutes, jour)
                break
            except service.ChargementEnCours:
                await asyncio.sleep(60)
            except Exception as exc:  # base indisponible, etc. : nouvel essai dans une minute
                log.warning("Rapport des chutes non écrit : %s", exc)
                await asyncio.sleep(60)
        await asyncio.sleep(60)


# Un flux temps réel reste ouvert tant que le navigateur écoute. Or uvicorn
# attend la fin de toutes les connexions avant de s'arrêter : sans précaution,
# un redémarrage (--reload) ou un Ctrl+C resterait bloqué tant qu'une page est
# ouverte. Deux protections : le signal d'arrêt ferme les flux tout de suite, et
# chaque flux se termine de lui-même au bout de DUREE_FLUX secondes (le
# navigateur se reconnecte seul).
DUREE_FLUX = 25
arret = {"demande": False}


def _fermer_les_flux_a_l_arret():
    boucle = asyncio.get_running_loop()
    for nom in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, nom, None)
        if sig is None:
            continue
        try:
            precedent = signal.getsignal(sig)

            def relais(signum, frame, precedent=precedent):
                arret["demande"] = True
                boucle.call_soon_threadsafe(diffuseur.publier, {"type": "arret"})
                if callable(precedent):
                    precedent(signum, frame)

            signal.signal(sig, relais)
        except (ValueError, OSError):
            pass  # hors du fil principal : la durée limitée des flux suffit


@asynccontextmanager
async def lifespan(app: FastAPI):
    _fermer_les_flux_a_l_arret()
    tache = asyncio.create_task(_boucle_rafraichissement())
    tache_rapport = asyncio.create_task(_boucle_rapport_chutes())
    log.info("Rafraîchissement toutes les %s minute(s)", settings.refresh_interval_minutes)
    yield
    arret["demande"] = True
    tache.cancel()
    tache_rapport.cancel()
    with suppress(asyncio.CancelledError):
        await tache
    with suppress(asyncio.CancelledError):
        await tache_rapport
    # Fermer le pool interrompt les requêtes Oracle encore en cours : le
    # processus ne reste pas en vie à attendre leur fin.
    service.arreter()
    db.fermer_pool()
    store.fermer()


app = FastAPI(title="Contrôle des versements SNDE", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list or ["http://localhost:5174"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


@app.exception_handler(db.BaseIndisponible)
async def _base_indisponible(request: Request, exc: db.BaseIndisponible):
    etat["base_ok"] = False
    etat["erreur"] = str(exc)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(service.ChargementEnCours)
async def _chargement_en_cours(request: Request, exc: service.ChargementEnCours):
    """Les données arrivent d'Oracle : l'écran redemande dans quelques secondes."""
    return JSONResponse(status_code=202, content={
        "chargement": True, "message": str(exc), "jeu": exc.jeu,
        "fait": exc.fait, "total": exc.total, "secondes": int(exc.secondes),
    })


@app.exception_handler(store.BaseLocaleIndisponible)
async def _base_locale_indisponible(request: Request, exc: store.BaseLocaleIndisponible):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# --------------------------------------------------------------------------- paramètres
def _periode(date_debut: date | None, date_fin: date | None) -> tuple[date, date]:
    debut = date_debut or date.today()
    fin = date_fin or debut
    if fin < debut:
        debut, fin = fin, debut
    if (fin - debut).days > 3 * 366:
        raise HTTPException(status_code=400, detail="La période ne peut pas dépasser trois ans")
    return debut, fin


def _centre(centre: str | None) -> str | None:
    centre = (centre or "").strip()
    return centre or None


_MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_DEBUT = Query(None, description="Date de début (AAAA-MM-JJ), aujourd'hui par défaut")
_SEGMENT = Query(None, pattern="^(tous|prives|etat)$",
                 description="Encaissements : tous, prives (sans l'État) ou etat")
_ZONE = Query(None, pattern="^(tous|nkt|nkt_ndb)$",
              description="Zones des encaissements : tous, nkt (Nouakchott) ou nkt_ndb (Nouakchott + Nouadhibou)")
_FIN = Query(None, description="Date de fin incluse (AAAA-MM-JJ)")
_CENTRE = Query(None, description="Code du centre ; vide pour tous les centres")


def _json(donnees) -> Response:
    """Réponse JSON écrite directement. Le passage par l'encodeur générique de
    FastAPI coûte plusieurs secondes sur des milliers de lignes ; ici les
    valeurs sont déjà des textes, des nombres ou vides."""
    return Response(
        content=json.dumps(donnees, ensure_ascii=False, separators=(",", ":"), default=str),
        media_type="application/json",
    )


# --------------------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    try:
        base_locale = store.etat()
    except store.BaseLocaleIndisponible as exc:
        base_locale = {"erreur": str(exc)}
    return {
        **etat,
        "intervalle_secondes": settings.refresh_seconds,
        "schema": settings.oracle_schema,
        "navigateurs_connectes": diffuseur.nb_abonnes,
        "duckdb": base_locale,
    }


@app.get("/api/centres")
def get_centres():
    # Zone de chaque centre (fichier des zones) : sert au filtre Zone de l'en-tête.
    return [{**c, "zone": zones.zone_du_centre(c["centre"])} for c in service.centres()]


@app.get("/api/controle")
def get_controle(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                 centre: str | None = _CENTRE):
    debut, fin = _periode(date_debut, date_fin)
    lignes = service.controle(debut, fin, _centre(centre))
    return _json({"synthese": service.synthese(lignes), "lignes": lignes})


@app.get("/api/encaissements")
def get_encaissements(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                      centre: str | None = _CENTRE,
                      recherche: str | None = Query(None, max_length=100,
                                                    description="Texte cherché dans toutes les colonnes"),
                      zone: str | None = _ZONE, segment: str | None = _SEGMENT):
    """Reçus pour l'écran : au plus `limite` lignes, mais le nombre de lignes et
    le total portent sur toute la sélection."""
    debut, fin = _periode(date_debut, date_fin)
    return _json(service.encaissements_ecran(debut, fin, _centre(centre), recherche, zone, segment))


@app.get("/api/versements")
def get_versements(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                   centre: str | None = _CENTRE):
    debut, fin = _periode(date_debut, date_fin)
    return _json(service.versements(debut, fin, _centre(centre)))


@app.get("/api/cheques")
def get_cheques(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                centre: str | None = _CENTRE):
    debut, fin = _periode(date_debut, date_fin)
    return _json(service.cheques(debut, fin, _centre(centre)))


@app.get("/api/reports")
def get_reports(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                centre: str | None = _CENTRE):
    debut, fin = _periode(date_debut, date_fin)
    return _json(service.reports(debut, fin, _centre(centre)))


@app.get("/api/annulations")
def get_annulations(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                    centre: str | None = _CENTRE):
    debut, fin = _periode(date_debut, date_fin)
    return _json(service.annulations(debut, fin, _centre(centre)))


@app.get("/api/non_valides")
def get_non_valides(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                    centre: str | None = _CENTRE):
    """Reçus non validés, crédités au compte du client et absents du versement."""
    debut, fin = _periode(date_debut, date_fin)
    return _json(service.non_valides(debut, fin, _centre(centre)))


@app.get("/api/non_valides/alerte")
def get_alerte_non_valides():
    """Nouveaux reçus non validés à contrôler, pas encore consultés."""
    return _json(service.alerte_non_valides())


@app.post("/api/non_valides/vus")
async def marquer_non_valides_vus(request: Request):
    """Note des reçus comme consultés. Corps attendu : {"recus": ["numéro", ...]}."""
    try:
        corps = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Corps JSON attendu") from exc
    recus = corps.get("recus") if isinstance(corps, dict) else None
    if not isinstance(recus, list) or len(recus) > 5000 or not all(isinstance(r, str) and len(r) <= 40 for r in recus):
        raise HTTPException(status_code=400, detail="Liste de numéros de reçus attendue")
    await asyncio.to_thread(service.marquer_non_valides_vus, recus)
    return _json({"ok": True, "nb": len(recus)})


@app.get("/api/chutes_secteur/alerte")
def get_alerte_chutes_secteur():
    """Secteurs en chute d'encaissement par rapport à M-2."""
    return _json(service.alerte_chutes_secteur())


@app.get("/api/chutes_secteur/detail/{secteur}")
def get_detail_chutes_secteur(secteur: str):
    """Détail par abonné d'un secteur en chute."""
    return _json(service.detail_chutes_secteur(secteur))

# --------------------------------------------------------------------------- alertes par secteur (planning)
_CLE_SECTEUR_OK = re.compile(r"[0-9A-Za-z][0-9A-Za-z _-]{0,39}")  # clé « centre-secteur »
_JOUR_ALERTE = Query(None, description="Jour de l'alerte (AAAA-MM-JJ) ; aujourd'hui par défaut")


@app.get("/api/alertes_secteurs")
def get_alertes_secteurs(jour: date | None = _JOUR_ALERTE, centre: str | None = _CENTRE,
                         zone_centre: str | None = Query(None, max_length=300)):
    """Tous les secteurs au jour J, comparés au même jour de M-1 (mensuel) ou M-2 (bimensuel, hors planning)."""
    return _json(service.alertes_secteurs(jour, _centre(centre), zone_centre))


@app.post("/api/alertes_secteurs/vus")
async def marquer_secteurs_vus(request: Request):
    """Valide des secteurs en chute. Corps : {"jour": "AAAA-MM-JJ", "sect_ids": ["123", ...]}."""
    try:
        corps = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Corps JSON attendu") from exc
    ids = corps.get("sect_ids") if isinstance(corps, dict) else None
    if not isinstance(ids, list) or len(ids) > 5000 or not all(isinstance(i, str) and _CLE_SECTEUR_OK.fullmatch(i) for i in ids):
        raise HTTPException(status_code=400, detail="Liste d'identifiants de secteurs attendue")
    try:
        jour = date.fromisoformat(corps["jour"]) if corps.get("jour") else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Jour invalide (AAAA-MM-JJ)") from exc
    await asyncio.to_thread(service.marquer_secteurs_vus, jour, ids)
    return _json({"ok": True, "nb": len(ids)})


@app.get("/api/alertes_secteurs/export.xlsx")
def export_alertes_secteurs(jour: date | None = _JOUR_ALERTE, centre: str | None = _CENTRE,
                            zone_centre: str | None = Query(None, max_length=300)):
    """Secteurs en chute au jour J, cumul du 1er au jour J (format chutes_secteur_AAAA-MM-JJ.xlsx)."""
    contenu, nom = service.rapport_chutes(jour, _centre(centre), zone_centre)
    return Response(contenu, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nom}"'})


@app.get("/api/alertes_secteurs/planifies")
def get_secteurs_planifies(annee: int | None = Query(None, ge=2000, le=2100), centre: str | None = _CENTRE):
    """Nombre de secteurs planifiés par centre et par mois."""
    return _json(service.secteurs_planifies_annee(annee or date.today().year, _centre(centre)))


@app.get("/api/alertes_secteurs/detail/{sect_id}")
def get_alerte_secteur_detail(sect_id: str, jour: date | None = _JOUR_ALERTE):
    """Un secteur jour par jour du 1er au jour J, et le détail par abonné."""
    if not _CLE_SECTEUR_OK.fullmatch(sect_id):
        raise HTTPException(status_code=400, detail="Identifiant de secteur invalide")
    return _json(service.alerte_secteur_detail(sect_id, jour))


@app.get("/api/rapprochement")
def get_rapprochement(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                      centre: str | None = _CENTRE):
    debut, fin = _periode(date_debut, date_fin)
    return _json(service.rapprochement(debut, fin, _centre(centre)))


@app.get("/api/export")
def export(jeu: str = Query(..., description="controle, encaissements, versements, cheques, "
                                             "reports, annulations, rapprochement, banques ou complet"),
           format: str = Query("xlsx", pattern="^(xlsx|csv)$"),
           date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
           centre: str | None = _CENTRE, zone: str | None = _ZONE, segment: str | None = _SEGMENT):
    debut, fin = _periode(date_debut, date_fin)
    centre = _centre(centre)
    if jeu not in ("complet", "format_rapprochement") and jeu not in service.JEUX:
        raise HTTPException(status_code=400, detail=f"Jeu de données inconnu : {jeu}")
    if jeu in ("complet", "format_rapprochement") and format == "csv":
        raise HTTPException(status_code=400, detail="Cet export n'existe qu'en Excel")

    suffixe = f"{debut:%Y%m%d}_{fin:%Y%m%d}" + (f"_centre{centre}" if centre else "")
    nom_fichier = f"controle_versements_{jeu}_{suffixe}.{format}"

    # Un export attend la fin des chargements : le fichier doit être complet.
    with service.en_attente():
        if format == "csv":
            contenu = exports.fichier_csv(jeu, service.jeu(jeu, debut, fin, centre, zone, segment))
            type_mime = "text/csv; charset=utf-8"
        elif jeu == "format_rapprochement":
            operations = service.rapprochement(debut, fin, centre)["operations"]
            contenu = exports.classeur_format_rapprochement(operations, debut, fin)
            type_mime = _MIME_XLSX
        else:
            noms = exports.FEUILLES_COMPLET if jeu == "complet" else (jeu,)
            contenu = exports.classeur({n: service.jeu(n, debut, fin, centre, zone, segment) for n in noms})
            type_mime = _MIME_XLSX
    return Response(
        content=contenu,
        media_type=type_mime,
        headers={"Content-Disposition": f'attachment; filename="{nom_fichier}"'},
    )


@app.post("/api/rapprochement/comparer")
async def comparer_rapprochement(
    request: Request,
    tolerance: int = Query(5, ge=0, le=31, description="Écart de date toléré, en jours"),
    etat: str | None = Query(None, description="Identifiant de l'état ; par défaut le plus récent de chaque feuille"),
    format: str = Query("json", pattern="^(json|xlsx)$"),
):
    """Reçoit le fichier Excel d'état de rapprochement (corps de la requête) et
    cherche chacune de ses lignes dans les versements et chèques du CRM."""
    contenu = await request.body()

    def comparer():
        with service.en_attente():
            return comparaison.comparer(contenu, tolerance, etat or None)

    try:
        resultat = await asyncio.to_thread(comparer)
    except comparaison.FichierInvalide as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if format == "xlsx":
        classeur = exports.classeur({
            "comparaison_synthese": resultat["synthese"],
            "comparaison": resultat["lignes"],
        })
        return Response(
            content=classeur,
            media_type=_MIME_XLSX,
            headers={"Content-Disposition": 'attachment; filename="comparaison_rapprochement.xlsx"'},
        )
    return resultat


# --------------------------------------------------------------------------- relevé bancaire
@app.get("/api/releve")
def get_releve():
    """Relevé bancaire actuellement importé (ou rien)."""
    return _json({"releve": service.info_releve()})


@app.post("/api/releve")
async def importer_releve(request: Request, nom: str = Query("releve.xlsx", max_length=200)):
    """Importe un relevé bancaire (classeur Excel envoyé tel quel dans le corps)."""
    contenu = await request.body()
    if not contenu:
        raise HTTPException(status_code=400, detail="Aucun fichier reçu")
    if len(contenu) > 60 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Fichier trop volumineux (60 Mo au plus)")
    try:
        info = await asyncio.to_thread(service.importer_releve, nom, contenu)
    except releve.ReleveInvalide as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _json({"releve": info})


@app.delete("/api/releve")
async def supprimer_releve():
    """Bouton « Réinitialiser » : efface le relevé bancaire importé."""
    await asyncio.to_thread(service.supprimer_releve)
    return _json({"releve": None})


@app.get("/api/releve/rapprochement")
def get_rapprochement_releve(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                             centre: str | None = _CENTRE):
    """Versements validés du CRM croisés avec le relevé importé, sur la période
    du relevé (réduite à la période demandée si elle est à l'intérieur)."""
    debut, fin = _periode(date_debut, date_fin)
    return _json(service.rapprochement_releve(debut, fin, _centre(centre)))


# Parties exportables de l'onglet Rapprochement bancaire : nom -> (feuille, titre du fichier).
_PARTIES_RELEVE = {
    "synthese": "releve_synthese", "absents": "releve_absents", "banques": "releve_banques",
    "centres": "releve_centres", "lignes": "releve_lignes", "sans_crm": "releve_sans_crm",
    "annules": "releve_annules",
}


@app.get("/api/releve/export")
def export_rapprochement_releve(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                                centre: str | None = _CENTRE,
                                partie: str = Query("tout", description="tout, synthese, absents, banques, "
                                                                        "centres, lignes, sans_crm ou annules"),
                                format: str = Query("xlsx", pattern="^(xlsx|csv)$"),
                                resultat: str | None = Query(None, description="Avec partie=lignes : un seul "
                                                                                 "résultat, par exemple AUTRE_BANQUE")):
    """Export de l'onglet Rapprochement bancaire : tout (un classeur, une feuille
    par tableau) ou un seul tableau, en Excel ou en CSV."""
    debut, fin = _periode(date_debut, date_fin)
    centre = _centre(centre)
    if partie != "tout" and partie not in _PARTIES_RELEVE:
        raise HTTPException(status_code=400, detail=f"Partie inconnue : {partie}")
    if partie == "tout" and format == "csv":
        raise HTTPException(status_code=400, detail="L'export de tout l'onglet n'existe qu'en Excel")
    with service.en_attente():
        r = service.rapprochement_releve(debut, fin, centre)
    if r.get("releve") is None:
        raise HTTPException(status_code=400, detail="Aucun relevé bancaire importé")
    donnees = {
        "releve_synthese": r["synthese"],
        "releve_absents": sorted((l for l in r["lignes"] if l["resultat_code"] == "NON_RETROUVE"),
                                 key=lambda l: -l["montant"]),
        "releve_banques": r["banques"], "releve_centres": r["centres"], "releve_lignes": r["lignes"],
        "releve_sans_crm": r["sans_crm"], "releve_annules": r["annules"],
    }
    # Le nom du fichier porte la période réellement rapprochée (celle du relevé).
    periode = r["periode"]
    suffixe = f"{periode['debut'].replace('-', '')}_{periode['fin'].replace('-', '')}" + (
        f"_centre{centre}" if centre else "")
    if partie == "tout":
        contenu, nom = exports.classeur(donnees), f"rapprochement_releve_{suffixe}.xlsx"
    else:
        feuille = _PARTIES_RELEVE[partie]
        nom = f"rapprochement_releve_{partie}_{suffixe}.{format}"
        if resultat and partie == "lignes":
            if resultat not in releve.LIBELLES:
                raise HTTPException(status_code=400, detail=f"Résultat inconnu : {resultat}")
            if resultat == "NON_RETROUVE":
                feuille = "releve_absents"       # mêmes lignes, sans les colonnes de la banque
            else:
                donnees[feuille] = [l for l in donnees[feuille] if l["resultat_code"] == resultat]
            nom = f"rapprochement_releve_{resultat.lower()}_{suffixe}.{format}"
        if format == "csv":
            contenu = exports.fichier_csv(feuille, donnees[feuille])
        else:
            contenu = exports.classeur({feuille: donnees[feuille]})
    return Response(
        content=contenu,
        media_type="text/csv; charset=utf-8" if format == "csv" else _MIME_XLSX,
        headers={"Content-Disposition": f'attachment; filename="{nom}"'},
    )


# --------------------------------------------------------------------------- évolution mensuelle
_MOIS = Query(None, pattern=r"^\d{4}-\d{2}$", description="Mois comparé (AAAA-MM), celui de la date de fin par défaut")


@app.get("/api/encaissements/evolution")
def get_evolution(mois: str | None = _MOIS, date_fin: date | None = _FIN, centre: str | None = _CENTRE,
                  zone: str | None = _ZONE, segment: str | None = _SEGMENT):
    """Encaissé par centre d'un mois comparé au mois précédent, et historique."""
    mois = mois or (date_fin or date.today()).strftime("%Y-%m")
    return _json(service.evolution_encaissements(mois, _centre(centre), zone, segment))


@app.get("/api/encaissements/evolution/export")
def export_evolution(mois: str | None = _MOIS, date_fin: date | None = _FIN, centre: str | None = _CENTRE,
                     zone: str | None = _ZONE, segment: str | None = _SEGMENT,
                     resultat: str | None = Query(None, description="Un seul résultat, par exemple EVO_CHUTE")):
    """Classeur : la comparaison par centre et l'historique des douze mois."""
    mois = mois or (date_fin or date.today()).strftime("%Y-%m")
    with service.en_attente():
        r = service.evolution_encaissements(mois, _centre(centre), zone, segment)
    lignes = r["lignes"]
    if resultat:
        if resultat not in evolution.LIBELLES:
            raise HTTPException(status_code=400, detail=f"Résultat inconnu : {resultat}")
        lignes = [l for l in lignes if l["resultat_code"] == resultat]
    h = r["historique"]
    garder = {l["centre"] for l in lignes}
    colonnes = [("centre", "Centre", "texte"), ("nom_centre", "Nom du centre", "texte")] + [
        (m["mois"], m["libelle"], "montant") for m in h["mois"]]
    histo = [{"centre": c, "nom_centre": v["nom_centre"], **{x["mois"]: x["encaisse"] for x in v["valeurs"]}}
             for c, v in h["centres"].items() if c in garder]
    nom = f"evolution_encaissements_{r['mois']}" + (f"_centre{r['centre']}" if r["centre"] else "") + ".xlsx"
    contenu = exports.classeur_evolution(lignes, histo, colonnes, r["libelle"], r["libelle_precedent"])
    return Response(content=contenu, media_type=_MIME_XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{nom}"'})


@app.get("/api/encaissements/annee")
def get_evolution_annee(annee: int | None = Query(None, ge=2000, le=2100), date_fin: date | None = _FIN,
                        centre: str | None = _CENTRE, zone: str | None = _ZONE, segment: str | None = _SEGMENT):
    """Encaissé par centre et par mois sur l'année, avec la variation de chaque mois."""
    return _json(service.evolution_annee(annee or (date_fin or date.today()).year, _centre(centre), zone, segment))


@app.get("/api/encaissements/annee/export")
def export_evolution_annee(annee: int | None = Query(None, ge=2000, le=2100), date_fin: date | None = _FIN,
                           centre: str | None = _CENTRE, zone: str | None = _ZONE,
                           segment: str | None = _SEGMENT):
    """Le tableau de l'évolution sur l'année en Excel."""
    with service.en_attente():
        r = service.evolution_annee(annee or (date_fin or date.today()).year, _centre(centre), zone, segment)
    colonnes = [("centre", "Centre", "texte"), ("nom_centre", "Nom du centre", "texte")]
    for m in r["mois"]:
        colonnes += [(f"m_{m['mois']}", f"Encaissé {m['libelle']} {r['annee']}", "montant"),
                     (f"v_{m['mois']}", f"Var. {m['libelle']} (%)", "pct")]
    colonnes += [("total", f"Total {r['annee']}", "montant"), ("evolution_pct", "Évolution sur l'année (%)", "pct")]

    def plat(c, nom, x):
        sortie = {"centre": c, "nom_centre": nom, "total": x["total"], "evolution_pct": x["evolution_pct"]}
        for v in x["valeurs"]:
            sortie[f"m_{v['mois']}"] = v["encaisse"]
            sortie[f"v_{v['mois']}"] = v["variation_pct"]
        return sortie

    # La ligne TOTAL du classeur additionne les montants de chaque mois.
    lignes = [plat(l["centre"], l["nom_centre"], l) for l in r["lignes"]]
    nom = f"evolution_encaissements_{r['annee']}" + (f"_centre{r['centre']}" if r["centre"] else "") + ".xlsx"
    contenu = exports.classeur_dynamique("evolution_annee", f"Évolution {r['annee']}", colonnes, lignes)
    return Response(content=contenu, media_type=_MIME_XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{nom}"'})


@app.get("/api/encaissements/objectifs")
def get_objectifs(mois: str | None = _MOIS, date_fin: date | None = _FIN, centre: str | None = _CENTRE,
                  zone: str | None = _ZONE):
    """Encaissé de chaque centre sur le mois comparé à son objectif mensuel."""
    mois = mois or (date_fin or date.today()).strftime("%Y-%m")
    return _json(service.objectifs_mois(mois, _centre(centre), zone))


@app.get("/api/encaissements/objectifs/export")
def export_objectifs(mois: str | None = _MOIS, date_fin: date | None = _FIN, centre: str | None = _CENTRE,
                     zone: str | None = _ZONE):
    """Le tableau des objectifs du mois en Excel."""
    mois = mois or (date_fin or date.today()).strftime("%Y-%m")
    with service.en_attente():
        r = service.objectifs_mois(mois, _centre(centre), zone)
    colonnes = [("zone", "Zone", "texte"), ("centre", "Centre", "texte"), ("nom_centre", "Nom du centre", "texte"),
                ("objectif", "Objectif du mois", "montant"),
                ("encaisse", "Encaissé privé" + (f" (au {r['jour']})" if r["en_cours"] else ""), "montant"),
                ("taux_atteinte", "Taux d'atteinte (%)", "pct"), ("reste", "Reste à encaisser", "montant"),
                ("depassement", "Dépassement", "montant"), ("etat", "État", "montant"),
                ("total", "Total (privé + État)", "montant"), ("statut", "Statut", "texte")]
    contenu = exports.classeur_dynamique("objectifs", f"Objectifs {r['libelle']}"[:31], colonnes, r["lignes"])
    nom = f"objectifs_encaissement_{r['mois']}" + (f"_centre{r['centre']}" if r["centre"] else "") + ".xlsx"
    return Response(content=contenu, media_type=_MIME_XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{nom}"'})


# --------------------------------------------------------------------------- tarifs
@app.get("/api/tarifs")
def get_tarifs(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN, centre: str | None = _CENTRE,
               recherche: str | None = Query(None, max_length=100),
               tarif: str | None = Query(None, max_length=100)):
    """Chaque référence encaissée sur la période avec son tarif (S_TARIF_PR.TARP_CODE)."""
    debut, fin = _periode(date_debut, date_fin)
    return _json(service.tarifs(debut, fin, _centre(centre), recherche, (tarif or "").strip() or None))


# --------------------------------------------------------------------------- reçus manuels
@app.get("/api/manuels")
def get_manuels():
    """Fichiers de reçus manuels actuellement importés (ou rien)."""
    return _json({"manuels": store.info_manuels()})


@app.post("/api/manuels")
async def importer_manuels(request: Request, nom: str = Query(..., max_length=200)):
    """Importe un fichier de reçus manuels (Excel ou CSV, envoyé tel quel dans le corps)."""
    contenu = await request.body()
    if not contenu:
        raise HTTPException(status_code=400, detail="Aucun fichier reçu")
    if len(contenu) > 60 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Fichier trop volumineux (60 Mo au plus)")
    try:
        info = await asyncio.to_thread(service.importer_manuels, nom, contenu)
    except manuels.FichierManuelsInvalide as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _json({"manuels": info})


@app.delete("/api/manuels")
async def supprimer_manuels():
    """Bouton « Réinitialiser » : efface les reçus manuels importés."""
    await asyncio.to_thread(service.supprimer_manuels)
    return _json({"manuels": None})


@app.get("/api/manuels/verification")
def get_verification_manuels():
    """Tous les reçus manuels importés comparés aux encaissements en espèces du CRM."""
    r = service.verification_manuels()
    for l in r.get("lignes", []):
        l["transactions_crm"] = l["transactions_crm"][:200]
    return _json(r)


@app.get("/api/manuels/export")
def export_verification_manuels(reference: str | None = None, centre: str | None = None,
                                du: date | None = None, au: date | None = None,
                                statut: str = Query("all", pattern="^(all|found|partial|missing|anomalies)$"),
                                partie: str = Query("recus", pattern="^(recus|references)$"),
                                format: str = Query("xlsx", pattern="^(xlsx|csv)$")):
    """Export de la vérification des reçus manuels, avec les filtres de l'onglet,
    ou la liste des références mal saisies."""
    with service.en_attente():
        r = service.verification_manuels()
    if r.get("manuels") is None:
        raise HTTPException(status_code=400, detail="Aucun fichier de reçus manuels importé")
    jour = date.today().isoformat()
    if partie == "references":
        nom = f"SNDE_references_INVALIDES_{jour}.{format}"
        lignes = r["references_mal_saisies"]
        contenu = (exports.fichier_csv("manuels_references", lignes) if format == "csv"
                   else exports.classeur({"manuels_references": lignes}))
    else:
        codes = {"found": {manuels.RETROUVE}, "partial": {manuels.MONTANT}, "missing": {manuels.ABSENT},
                 "anomalies": {manuels.MONTANT, manuels.ABSENT}}.get(statut)
        lignes = [
            l for l in r["lignes"]
            if (not reference or reference.strip() in l["reference"])
            and (not centre or l["centre"] == centre or l["centre_fichier"] == centre)
            and (not du or (l["date_recu"] or "") >= du.isoformat())
            and (not au or (l["date_recu"] or "9999") <= au.isoformat())
            and (codes is None or l["resultat_code"] in codes)
        ]
        nom = (f"SNDE_recus_NON_REMONTES_{jour}.{format}" if statut == "missing"
               else f"SNDE_verif_recus_{jour}.{format}")
        contenu = exports.fichier_csv("manuels", lignes) if format == "csv" else exports.classeur({"manuels": lignes})
    return Response(
        content=contenu,
        media_type="text/csv; charset=utf-8" if format == "csv" else _MIME_XLSX,
        headers={"Content-Disposition": f'attachment; filename="{nom}"'},
    )


# --------------------------------------------------------------------------- croisement avec la DFC
@app.get("/api/dfc")
def get_dfc():
    """Fichiers de la DFC actuellement importés (ou rien)."""
    return _json({"dfc": store.info_dfc()})


@app.post("/api/dfc")
async def importer_dfc(request: Request, nom: str = Query(..., max_length=200)):
    """Importe le classeur d'un centre saisi par la DFC (envoyé tel quel dans le
    corps). Le numéro du centre est lu dans le nom du fichier."""
    contenu = await request.body()
    if not contenu:
        raise HTTPException(status_code=400, detail="Aucun fichier reçu")
    if len(contenu) > 60 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Fichier trop volumineux (60 Mo au plus)")
    try:
        info = await asyncio.to_thread(service.importer_dfc, nom, contenu)
    except dfc.FichierDfcInvalide as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _json({"dfc": info})


@app.delete("/api/dfc")
async def supprimer_dfc():
    """Bouton « Réinitialiser » : efface la saisie de la DFC importée."""
    await asyncio.to_thread(service.supprimer_dfc)
    return _json({"dfc": None})


@app.get("/api/dfc/croisement")
def get_croisement_dfc(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                       centre: str | None = _CENTRE):
    """Saisie de la DFC comparée au CRM, par centre et par jour."""
    debut, fin = _periode(date_debut, date_fin)
    r = service.croisement_dfc(debut, fin, _centre(centre))
    r.pop("toutes_les_lignes", None)   # réservées à l'export complet
    return _json(r)


@app.get("/api/dfc/export")
def export_croisement_dfc(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN,
                          centre: str | None = _CENTRE,
                          format: str = Query("xlsx", pattern="^(xlsx|csv)$"),
                          resultat: str | None = Query(None, description="Un seul résultat, par exemple DFC_VERSE"),
                          partie: str = Query("tout", pattern="^(tout|mois|jours)$")):
    """Export du croisement avec la DFC : le classeur complet (par mois et par
    jour), ou un seul tableau en Excel ou en CSV."""
    debut, fin = _periode(date_debut, date_fin)
    centre = _centre(centre)
    with service.en_attente():
        r = service.croisement_dfc(debut, fin, centre)
    if r.get("dfc") is None:
        raise HTTPException(status_code=400, detail="Aucun fichier de la DFC importé")
    lignes = r["lignes"]
    suffixe = f"{debut:%Y%m%d}_{fin:%Y%m%d}" + (f"_centre{centre}" if centre else "")
    nom = f"croisement_dfc_{suffixe}.{format}"
    if resultat:
        if resultat not in dfc.LIBELLES:
            raise HTTPException(status_code=400, detail=f"Résultat inconnu : {resultat}")
        lignes = [l for l in lignes if l["resultat_code"] == resultat]
        nom = f"croisement_dfc_{resultat.lower().replace('dfc_', '')}_{suffixe}.{format}"
    if partie == "mois":
        nom = f"croisement_dfc_mois_{suffixe}.{format}"
        contenu = (exports.fichier_csv("dfc_mois", r["mois"]) if format == "csv"
                   else exports.classeur({"dfc_mois": r["mois"]}))
    elif format == "csv":
        contenu = exports.fichier_csv("dfc_lignes", lignes)
    elif resultat or partie == "jours":
        contenu = exports.classeur({"dfc_lignes": lignes})
    else:
        # Classeur complet : toute la période des fichiers importés, par mois et par jour.
        couverture = r.get("couverture") or {"debut": debut.isoformat(), "fin": fin.isoformat()}
        nom = (f"croisement_dfc_{couverture['debut'].replace('-', '')}_{couverture['fin'].replace('-', '')}"
               + (f"_centre{centre}" if centre else "") + ".xlsx")
        contenu = exports.classeur({"dfc_mois": r["mois"], "dfc_lignes": r.get("toutes_les_lignes", lignes)})
    return Response(
        content=contenu,
        media_type="text/csv; charset=utf-8" if format == "csv" else _MIME_XLSX,
        headers={"Content-Disposition": f'attachment; filename="{nom}"'},
    )


@app.get("/api/diagnostic")
def diagnostic(jour: date | None = Query(None, description="Jour mesuré, hier par défaut")):
    """Mesures de performance : taille des tables, index et durée de requêtes simples."""
    return service.diagnostic(jour or date.today() - timedelta(days=1))


@app.get("/api/diagnostic/fils")
async def diagnostic_fils():
    """Ce que fait chaque fil d'exécution en ce moment, avec l'état des verrous.

    Cette route ne passe pas par les fils de travail : elle répond même quand
    toutes les autres attendent, ce qui permet de voir où elles sont bloquées."""
    noms = {f.ident: f.name for f in threading.enumerate()}
    fils = []
    for ident, cadre in sys._current_frames().items():
        pile = traceback.extract_stack(cadre)
        fils.append({
            "fil": noms.get(ident, str(ident)),
            "pile": [f"{Path(c.filename).name}:{c.lineno} {c.name}" for c in pile[-9:]],
        })
    fils.sort(key=lambda f: f["fil"])
    return {"heure": datetime.now().isoformat(timespec="seconds"), "nb_fils": len(fils),
            **service.etat_travaux(), "fils": fils}


@app.post("/api/refresh")
async def forcer_rafraichissement(date_debut: date | None = _DEBUT, date_fin: date | None = _FIN):
    """Bouton « Actualiser » : recharge la période depuis Oracle et prévient
    tous les navigateurs."""
    debut, fin = _periode(date_debut, date_fin)
    if (fin - debut).days > 92:
        raise HTTPException(status_code=400, detail="L'actualisation forcée est limitée à 3 mois")
    return await rafraichir("manuel", debut, fin)


@app.get("/api/stream")
async def stream(request: Request):
    """Flux Server-Sent Events : un message « refresh » à chaque rafraîchissement."""
    file = diffuseur.abonner()

    async def evenements():
        debut = time.monotonic()
        try:
            bonjour = {"type": "hello", **etat, "intervalle_secondes": settings.refresh_seconds}
            yield f"retry: 1500\nevent: hello\ndata: {json.dumps(bonjour)}\n\n"
            while not arret["demande"] and time.monotonic() - debut < DUREE_FLUX:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(file.get(), timeout=5)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if message["type"] == "arret":
                    break
                yield f"event: {message['type']}\ndata: {json.dumps(message)}\n\n"
        finally:
            diffuseur.desabonner(file)

    return StreamingResponse(
        evenements(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )

