import { useEffect, useMemo, useState } from 'react';
import { forcerRafraichissement, getJSON, urlExport } from './api.js';
import { COLONNES, ONGLETS } from './datasets.js';
import { fmtEntier, fmtMontant, periodes, versISO } from './format.js';
import { useData, useRetard } from './hooks/useData.js';
import { useLive } from './hooks/useLive.js';
import Tarifs from './components/Tarifs.jsx';
import Evolution from './components/Evolution.jsx';
import FiltreZones from './components/FiltreZones.jsx';
import AlertesSecteurs, { BandeauAlertesSecteurs } from './components/AlertesSecteurs.jsx';
import DataTable from './components/DataTable.jsx';
import logoSnde from './logo-snde.js';

function BoutonsExport({ jeu, filtres, libelle }) {
  return (
    <span className="exports">
      <a className="btn" href={urlExport(jeu, 'xlsx', filtres)} download>
        {libelle ? `${libelle} (Excel)` : 'Excel'}
      </a>
      <a className="btn" href={urlExport(jeu, 'csv', filtres)} download>
        CSV
      </a>
    </span>
  );
}

// Dans les encaissements, seuls les reçus validés entrent dans le total.
const estValide = (l) => l.statut === 'VALIDE';

// Les reçus peuvent être des centaines de milliers : le serveur fait la
// recherche et n'envoie que les premières lignes, avec le total de la sélection.
const JEUX_RECHERCHE_SERVEUR = new Set(['encaissements']);

function ContenuJeu({ id, filtres, version, recherche }) {
  const serveur = JEUX_RECHERCHE_SERVEUR.has(id);
  const texte = useRetard(serveur ? recherche.trim() : '');
  // La zone (Nouakchott…) ne concerne que les encaissements.
  const extra = {};
  if (serveur && texte) extra.recherche = texte;
  if (id === 'encaissements' && filtres.zone && filtres.zone !== 'tous') extra.zone = filtres.zone;
  if (id === 'encaissements' && filtres.segment && filtres.segment !== 'tous') extra.segment = filtres.segment;
  const { data, chargement, erreur, attente } = useData(id, filtres, version, Object.keys(extra).length ? extra : null);
  const info = attente && (
    <p className="info-chargement" role="status">
      {attente}
    </p>
  );

  if (erreur && !data) return <p className="alerte">{erreur}</p>;

  if (serveur) {
    const lignes = data?.lignes ?? [];
    const total = data?.total_lignes ?? lignes.length;
    return (
      <>
        {info}
        {data?.tronque && (
          <p className="info-chargement" role="status">
            {fmtEntier(total)} lignes dans la sélection : seules les {fmtEntier(lignes.length)} premières sont
            affichées, mais le total porte sur toutes. Pour voir les autres, choisissez un centre, réduisez la période
            ou utilisez la recherche. L’export contient toutes les lignes.
          </p>
        )}
        <DataTable
          colonnes={COLONNES[id]}
          lignes={lignes}
          chargement={chargement || texte !== recherche.trim()}
          attente={attente}
          totalLignes={total}
          totaux={data?.totaux}
          totalSi={estValide}
          libelleTotal="Total validés"
        />
      </>
    );
  }

  return (
    <>
      {info}
      <DataTable
        colonnes={COLONNES[id]}
        lignes={Array.isArray(data) ? data : []}
        recherche={recherche}
        chargement={chargement}
        attente={attente}
      />
    </>
  );
}

function Panneau({ onglet, filtres, version, lignesControle, chargementControle, onCentre, onPeriode, onDates }) {
  const [recherche, setRecherche] = useState('');
  // Onglet Encaissements : la liste des reçus, ou l'évolution mensuelle par centre.
  const [vue, setVue] = useState('liste');
  const evolution = onglet.id === 'encaissements' && vue === 'evolution';
  // Zone des encaissements : toute la Mauritanie, Nouakchott, ou Nouakchott + Nouadhibou.
  const [zone, setZone] = useState('tous');
  // Clients : tous, privés (sans l'État) ou État.
  const [segment, setSegment] = useState('tous');
  const filtresOnglet = onglet.id === 'encaissements' ? { ...filtres, zone, segment } : filtres;

  return (
    <section className="panneau" role="tabpanel" aria-label={onglet.titre}>
      <div className="panneau-entete">
        {/* L'emplacement reste pour garder les outils à droite. */}
        <span />
        <div className="panneau-outils">
          <input
            type="search"
            className="champ"
            placeholder="Rechercher dans le tableau"
            value={recherche}
            onChange={(e) => setRecherche(e.target.value)}
            aria-label="Rechercher dans le tableau"
          />
          {onglet.id !== 'tarifs' && onglet.id !== 'alertes' && !evolution && <BoutonsExport jeu={onglet.id} filtres={filtresOnglet} />}
        </div>
      </div>
      {onglet.id === 'encaissements' && (
        <div className="barre-encaissements">
          <div className="segment vue-encaissements" role="group" aria-label="Vue des encaissements">
            <button
              type="button"
              className={`segment-btn${vue === 'liste' ? ' actif' : ''}`}
              aria-pressed={vue === 'liste'}
              onClick={() => setVue('liste')}
            >
              Liste des reçus
            </button>
            <button
              type="button"
              className={`segment-btn${vue === 'evolution' ? ' actif' : ''}`}
              aria-pressed={vue === 'evolution'}
              onClick={() => setVue('evolution')}
            >
              Évolution mensuelle par centre
            </button>
          </div>
          <label className="etiquette etiquette-ligne">
            Zone
            <select className="champ" value={zone} onChange={(e) => setZone(e.target.value)} aria-label="Zone des centres">
              <option value="tous">Toute la Mauritanie</option>
              <option value="nkt">Nouakchott</option>
              <option value="nkt_ndb">Nouakchott + Nouadhibou</option>
            </select>
          </label>
          <label className="etiquette etiquette-ligne">
            Clients
            <select className="champ" value={segment} onChange={(e) => setSegment(e.target.value)} aria-label="Clients">
              <option value="tous">Tous</option>
              <option value="prives">Privés</option>
              <option value="etat">État</option>
            </select>
          </label>
        </div>
      )}
      {evolution ? (
        <Evolution filtres={filtresOnglet} version={version} recherche={recherche} />
      ) : onglet.id === 'controle' ? (
        // Le contrôle est déjà chargé par la page : on ne le redemande pas.
        <DataTable
          colonnes={COLONNES.controle}
          lignes={lignesControle}
          recherche={recherche}
          chargement={chargementControle}
          onLigne={(l) => onCentre(l.centre)}
          aideLigne="Cliquer pour ne garder que ce centre"
        />
      ) : onglet.id === 'alertes' ? (
        <AlertesSecteurs filtres={filtres} version={version} recherche={recherche} />
      ) : onglet.id === 'tarifs' ? (
        <Tarifs filtres={filtres} version={version} recherche={recherche} />
      ) : (
        <ContenuJeu
          id={onglet.id}
          filtres={filtresOnglet}
          version={version}
          recherche={recherche}
        />
      )}
    </section>
  );
}

export default function App() {
  const aujourdHui = versISO(new Date());
  const [filtres, setFiltres] = useState({ dateDebut: aujourdHui, dateFin: aujourdHui, centre: '', zones: [] });
  const [ongletId, setOngletId] = useState('controle');
  const [centres, setCentres] = useState([]);
  const [manuel, setManuel] = useState(0);
  const [actualisation, setActualisation] = useState(false);

  const live = useLive();
  const version = live.version + manuel;
  const controle = useData('controle', filtres, version);

  useEffect(() => {
    getJSON('centres')
      .then(setCentres)
      .catch(() => setCentres([]));
  }, [live.baseOk]);

  const liste = useMemo(() => periodes(), []);
  const periodeActive = liste.find((p) => p.debut === filtres.dateDebut && p.fin === filtres.dateFin)?.id;
  const onglet = ONGLETS.find((o) => o.id === ongletId);

  const changer = (champs) =>
    setFiltres((f) => {
      const n = { ...f, ...champs };
      if (n.dateFin < n.dateDebut) {
        if ('dateDebut' in champs) n.dateFin = n.dateDebut;
        else n.dateDebut = n.dateFin;
      }
      return n;
    });

  // Filtre par mois : les 36 derniers mois. Un mois choisi règle « Du » et « Au »
  // sur le mois complet (jusqu'à aujourd'hui pour le mois en cours).
  const bornesMois = (m) => {
    const [annee, mois] = m.split('-').map(Number);
    const dernier = versISO(new Date(annee, mois, 0));
    return { dateDebut: `${m}-01`, dateFin: dernier > aujourdHui ? aujourdHui : dernier };
  };
  const listeMois = useMemo(() => {
    const maintenant = new Date();
    return Array.from({ length: 36 }, (_, i) => {
      const d = new Date(maintenant.getFullYear(), maintenant.getMonth() - i, 1);
      const libelle = d.toLocaleDateString('fr-FR', { month: 'long', year: 'numeric' });
      return { id: versISO(d).slice(0, 7), libelle: libelle.charAt(0).toUpperCase() + libelle.slice(1) };
    });
  }, []);
  const moisCandidat = filtres.dateDebut.slice(0, 7);
  const bornesCandidat = bornesMois(moisCandidat);
  const moisActif =
    filtres.dateDebut === bornesCandidat.dateDebut && filtres.dateFin === bornesCandidat.dateFin ? moisCandidat : '';

  // Filtre par année : l'année en cours et les trois précédentes. Une année
  // choisie règle « Du » et « Au » sur l'année complète (jusqu'à aujourd'hui
  // pour l'année en cours).
  const bornesAnnee = (annee) => {
    const dernier = `${annee}-12-31`;
    return { dateDebut: `${annee}-01-01`, dateFin: dernier > aujourdHui ? aujourdHui : dernier };
  };
  const listeAnnees = useMemo(() => {
    const courante = new Date().getFullYear();
    return Array.from({ length: 4 }, (_, i) => String(courante - i));
  }, []);
  const anneeCandidate = filtres.dateDebut.slice(0, 4);
  const bornesAnneeCandidate = bornesAnnee(anneeCandidate);
  const anneeActive =
    filtres.dateDebut === bornesAnneeCandidate.dateDebut && filtres.dateFin === bornesAnneeCandidate.dateFin
      ? anneeCandidate
      : '';

  const actualiser = async () => {
    setActualisation(true);
    try {
      await forcerRafraichissement(filtres);
    } catch {
      /* l'état de la base s'affiche déjà dans l'en-tête */
    } finally {
      // Sans flux temps réel, on recharge soi-même.
      if (!live.connecte) setManuel((m) => m + 1);
      setActualisation(false);
    }
  };

  const lignes = controle.data?.lignes ?? [];
  const erreur = controle.erreur || (live.baseOk === false ? live.erreur : null);

  return (
    <div className="page">
      <header className="entete">
        <div className="entete-titre">
          <img className="entete-logo" src={logoSnde} alt="Logo de la SNDE" width="52" height="52" />
          <div>
            <h1>Contrôle des versements</h1>
            <p>SNDE · Encaissements des caisses, versements bancaires et reports par centre</p>
          </div>
        </div>
        <BandeauAlertesSecteurs centre={filtres.centre} zone={filtres.zones.join(',')} version={version} />
      </header>

      <div className="filtres" role="group" aria-label="Filtres">
        <div className="segment" role="group" aria-label="Période">
          {liste.map((p) => (
            <button
              type="button"
              key={p.id}
              className={`segment-btn${periodeActive === p.id ? ' actif' : ''}`}
              aria-pressed={periodeActive === p.id}
              onClick={() => changer({ dateDebut: p.debut, dateFin: p.fin })}
            >
              {p.libelle}
            </button>
          ))}
        </div>
        <label className="etiquette">
          Du
          <input
            type="date"
            className="champ"
            value={filtres.dateDebut}
            max={aujourdHui}
            onChange={(e) => e.target.value && changer({ dateDebut: e.target.value })}
          />
        </label>
        <label className="etiquette">
          Au
          <input
            type="date"
            className="champ"
            value={filtres.dateFin}
            max={aujourdHui}
            onChange={(e) => e.target.value && changer({ dateFin: e.target.value })}
          />
        </label>
        <label className="etiquette">
          Mois
          <select
            className="champ"
            // « Tous les mois » : toute l'année de la période affichée.
            value={moisActif || (anneeActive ? 'tous' : '')}
            onChange={(e) => {
              const v = e.target.value;
              if (v === 'tous') changer(bornesAnnee(filtres.dateDebut.slice(0, 4)));
              else if (v) changer(bornesMois(v));
            }}
          >
            {!moisActif && !anneeActive && <option value="">Choisir un mois</option>}
            <option value="tous">Tous les mois</option>
            {listeMois.map((m) => (
              <option key={m.id} value={m.id}>
                {m.libelle}
              </option>
            ))}
            {moisActif && !listeMois.some((m) => m.id === moisActif) && <option value={moisActif}>{moisActif}</option>}
          </select>
        </label>
        <label className="etiquette">
          Année
          <select
            className="champ"
            value={anneeActive}
            onChange={(e) => e.target.value && changer(bornesAnnee(e.target.value))}
          >
            <option value="">Choisir une année</option>
            {listeAnnees.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
            {anneeActive && !listeAnnees.includes(anneeActive) && <option value={anneeActive}>{anneeActive}</option>}
          </select>
        </label>
        <FiltreZones
          zones={[...new Set(centres.map((c) => c.zone).filter(Boolean))].sort()}
          valeur={filtres.zones}
          onChange={(z) => {
            const garde = centres.some(
              (c) => String(c.centre) === String(filtres.centre) && (!z.length || z.includes(c.zone)),
            );
            changer({ zones: z, ...(garde ? {} : { centre: '' }) });
          }}
        />
        <label className="etiquette">
          Centre
          <select className="champ" value={filtres.centre} onChange={(e) => changer({ centre: e.target.value })}>
            <option value="">Tous les centres</option>
            {centres.filter((c) => !filtres.zones.length || filtres.zones.includes(c.zone)).map((c) => (
              <option key={c.centre} value={c.centre}>
                {c.centre} — {c.nom_centre}
              </option>
            ))}
            {filtres.centre && !centres.some((c) => String(c.centre) === String(filtres.centre)) && (
              <option value={filtres.centre}>{filtres.centre}</option>
            )}
          </select>
        </label>
        <span className="filtres-droite">
          <button type="button" className="btn" onClick={actualiser} disabled={actualisation}>
            {actualisation ? 'Actualisation…' : 'Actualiser'}
          </button>
          <a className="btn btn-principal" href={urlExport('complet', 'xlsx', filtres)} download>
            Export complet (Excel)
          </a>
        </span>
      </div>


      {erreur && (
        <p className="alerte" role="alert">
          {erreur}
        </p>
      )}

      {controle.attente && (
        <p className="info-chargement" role="status">
          {controle.attente}
        </p>
      )}

      <nav className="onglets" role="tablist" aria-label="Détails">
        {ONGLETS.map((o) => (
          <button
            type="button"
            key={o.id}
            role="tab"
            aria-selected={o.id === ongletId}
            className={`onglet${o.id === ongletId ? ' actif' : ''}`}
            onClick={() => setOngletId(o.id)}
          >
            {o.titre}
          </button>
        ))}
      </nav>

      <Panneau
        key={onglet.id}
        onglet={onglet}
        filtres={filtres}
        version={version}
        lignesControle={lignes}
        chargementControle={controle.chargement}
        onCentre={(c) => changer({ centre: String(c) })}

      />
    </div>
  );
}
