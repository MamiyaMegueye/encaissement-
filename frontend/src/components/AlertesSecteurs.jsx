import { useEffect, useMemo, useRef, useState } from 'react';
import { getJSON, marquerSecteursVus } from '../api.js';
import { decaler, fmtDate, fmtEntier, fmtMontant, fmtPct, versISO } from '../format.js';
import { useData } from '../hooks/useData.js';
import DataTable from './DataTable.jsx';

/** Mêmes colonnes que le rapport chutes_secteur_AAAA-MM-JJ.xlsx. En-têtes datés :
 *  période en cours, et période de référence (une seule, ou M-1 « ou » M-2). */
function colonnesChutes(periode, lignes) {
  // « 01 au 20/10/2026 » : tri par mois (le plus récent d’abord).
  const refs = [...new Set(lignes.map((l) => l.periode_ref))].sort((a, b) => (b.slice(-4) + b.slice(-7, -5)).localeCompare(a.slice(-4) + a.slice(-7, -5)));
  const r = refs.length ? `du ${refs.join(' ou du ')}` : 'de référence';
  const p = periode ? `du ${periode}` : '';
  return [
    { cle: 'zone', libelle: 'Zone', type: 'texte' },
    { cle: 'centre', libelle: 'Centre', type: 'texte' },
    { cle: 'nom_centre', libelle: 'Nom du centre', type: 'texte' },
    { cle: 'secteur', libelle: 'Secteur', type: 'texte' },
    { cle: 'cumul_ref', libelle: `Encaissements ${r}`, type: 'montant', total: true },
    { cle: 'cumul', libelle: `Encaissements ${p}`, type: 'montant', total: true },
    { cle: 'ecart', libelle: 'Écart', type: 'montant', total: true },
    { cle: 'variation_pct', libelle: 'Variation (%)', type: 'pct' },
    { cle: 'nb_recus_ref', libelle: `Reçus ${r}`, type: 'entier' },
    { cle: 'nb_recus', libelle: `Reçus ${p}`, type: 'entier' },
    { cle: 'nb_abonnes_ref', libelle: `Abonnés payeurs ${r}`, type: 'entier' },
    { cle: 'nb_abonnes', libelle: `Abonnés payeurs ${p}`, type: 'entier' },
  ];
}

const COLONNES_ABONNES = [
  { cle: 'reference', libelle: 'Abonné', type: 'texte' },
  { cle: 'tarif', libelle: 'Tarif', type: 'texte' },
  { cle: 'statut_abonne', libelle: 'Statut', type: 'texte' },
  { cle: 'montant_actuel', libelle: 'Cumul du mois', type: 'montant', total: true },
  { cle: 'montant_ref', libelle: 'Cumul réf.', type: 'montant', total: true },
  { cle: 'ecart', libelle: 'Écart', type: 'montant', total: true },
  { cle: 'variation_pct', libelle: 'Variation', type: 'pct' },
];

const NOMS_MOIS = ['janv.', 'févr.', 'mars', 'avr.', 'mai', 'juin', 'juil.', 'août', 'sept.', 'oct.', 'nov.', 'déc.'];
const COLONNES_PLANIFIES = [
  { cle: 'centre', libelle: 'Centre', type: 'texte' },
  { cle: 'nom_centre', libelle: 'Nom du centre', type: 'texte' },
  ...NOMS_MOIS.map((n, i) => ({ cle: `m${String(i + 1).padStart(2, '0')}`, libelle: n, type: 'entier', total: true })),
];

const CLASSES = { SECT_CHUTE: 'sect-chute', SECT_BAISSE: 'sect-baisse' };
const aujourdHui = () => versISO(new Date());
// Jour par défaut : hier, ou aujourd’hui à partir de 23 h (journée close).
const jourDefaut = () => versISO(decaler(new Date(), new Date().getHours() >= 23 ? 0 : -1));
const jourVoisin = (jour, n) => versISO(decaler(new Date(`${jour}T00:00`), n));

/**
 * Encaissement de chaque jour du mois et du même jour du mois de référence.
 * Interactif : survol d’un jour → infobulle ; clic → jour sélectionné (ligne
 * surlignée dans le tableau) ; clic sur la légende → masquer/afficher une série.
 */
function Barres({ jours, selection, onSelection }) {
  const [survol, setSurvol] = useState(null);
  const [visibles, setVisibles] = useState({ actuel: true, ref: true });
  if (!jours.length) return null;
  const L = 640, H = 200, m = { g: 70, d: 12, h: 14, b: 26 };
  const valeurs = jours.flatMap((j) => [visibles.actuel ? j.montant_jour : 0, visibles.ref ? j.montant_jour_ref : 0]);
  const max = Math.max(1, ...valeurs);
  const pas = (L - m.g - m.d) / jours.length;
  const y = (v) => H - m.b - (v / max) * (H - m.h - m.b);
  const l = Math.max(1, pas / 2 - 1);
  const j = survol != null ? jours[survol] : null;
  const xInfo = survol != null ? m.g + survol * pas + pas / 2 : 0;
  const basculer = (cle) => setVisibles((v) => ({ ...v, [cle]: !v[cle] }));

  return (
    <div className="graphe-secteur">
      <div className="legende-courbes">
        <button type="button" className={`leg-actuel${visibles.actuel ? '' : ' leg-off'}`} onClick={() => basculer('actuel')}>
          {jours[0].mois}
        </button>
        <button type="button" className={`leg-ref${visibles.ref ? '' : ' leg-off'}`} onClick={() => basculer('ref')}>
          {jours[0].mois_ref} (même jour)
        </button>
        {selection != null && (
          <button type="button" className="leg-effacer" onClick={() => onSelection(null)}>
            Effacer la sélection
          </button>
        )}
      </div>
      <svg
        className="courbes-secteur"
        viewBox={`0 0 ${L} ${H}`}
        role="img"
        aria-label="Encaissement jour par jour"
        onMouseLeave={() => setSurvol(null)}
      >
        {[0, 0.5, 1].map((p) => (
          <g key={p}>
            <line x1={m.g} x2={L - m.d} y1={y(max * p)} y2={y(max * p)} className="courbes-grille" />
            <text x={m.g - 6} y={y(max * p) + 4} textAnchor="end" className="courbes-texte">
              {fmtMontant(Math.round(max * p))}
            </text>
          </g>
        ))}
        {jours.map((jj, i) => {
          const x = m.g + i * pas;
          const actif = selection === i || survol === i;
          return (
            <g key={jj.jour}>
              {actif && <rect x={x} y={m.h} width={pas} height={H - m.h - m.b} className="barre-fond-actif" />}
              {visibles.ref && (
                <rect x={x} y={y(jj.montant_jour_ref)} width={l} height={H - m.b - y(jj.montant_jour_ref)} className="barre-ref" />
              )}
              {visibles.actuel && (
                <rect
                  x={x + l}
                  y={y(jj.montant_jour)}
                  width={l}
                  height={H - m.b - y(jj.montant_jour)}
                  className={jj.statut_code === 'SECT_CHUTE' ? 'barre-chute' : 'barre-actuel'}
                />
              )}
              {(i % Math.ceil(jours.length / 10) === 0 || i === jours.length - 1) && (
                <text x={x + l} y={H - 8} textAnchor="middle" className="courbes-texte">
                  {jj.jour.slice(8, 10)}
                </text>
              )}
              {/* Zone de survol et de clic sur toute la hauteur du jour */}
              <rect
                x={x}
                y={0}
                width={pas}
                height={H}
                className="barre-zone"
                onMouseEnter={() => setSurvol(i)}
                onClick={() => onSelection(selection === i ? null : i)}
              />
            </g>
          );
        })}
      </svg>
      {j && (
        <div
          className="infobulle-graphe"
          style={{ left: `${(xInfo / L) * 100}%`, transform: `translateX(${xInfo > L * 0.7 ? '-100%' : xInfo < L * 0.3 ? '0' : '-50%'})` }}
        >
          <div className="infobulle-titre">{fmtDate(j.jour)}</div>
          <div>
            Encaissé : <strong>{fmtMontant(j.montant_jour)}</strong>
          </div>
          <div>
            {fmtDate(j.jour_ref)} : <strong>{fmtMontant(j.montant_jour_ref)}</strong>
          </div>
          <div>Variation du jour : {fmtPct(j.variation_jour_pct)}</div>
          <div>
            Cumul : {fmtMontant(j.cumul)} / {fmtMontant(j.cumul_ref)} ({fmtPct(j.variation_pct)})
          </div>
          <div className="infobulle-aide">Cliquer pour sélectionner ce jour</div>
        </div>
      )}
    </div>
  );
}

function DetailSecteur({ ligne, jour, onFermer }) {
  const [etat, setEtat] = useState({ data: null, erreur: null });
  const [selection, setSelection] = useState(null);
  const lignesTableau = useRef({});

  useEffect(() => {
    if (selection != null) lignesTableau.current[selection]?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [selection]);

  useEffect(() => {
    const ctrl = new AbortController();
    let minuterie = null;
    const demander = () =>
      getJSON(`alertes_secteurs/detail/${encodeURIComponent(ligne.sect_id)}`, null, ctrl.signal, { jour })
        .then((r) => {
          if (r?.enChargement) minuterie = setTimeout(demander, 2500);
          else setEtat({ data: r, erreur: null });
        })
        .catch((e) => e.name !== 'AbortError' && setEtat({ data: null, erreur: e.message }));
    demander();
    return () => {
      ctrl.abort();
      if (minuterie) clearTimeout(minuterie);
    };
  }, [ligne.sect_id, jour]);

  useEffect(() => {
    const h = (e) => e.key === 'Escape' && onFermer();
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onFermer]);

  const d = etat.data;
  return (
    <div className="modal-fond" onClick={onFermer}>
      <div className="modal-chutes modal-large" onClick={(e) => e.stopPropagation()}>
        <div className="modal-entete">
          <h2>
            Centre {ligne.centre} · Secteur {ligne.secteur} ({ligne.frequence.toLowerCase()})
          </h2>
          <span className="modal-periode">
            {ligne.mois} comparé à {ligne.mois_ref}, jour par jour
          </span>
          <button type="button" className="modal-fermer" onClick={onFermer} aria-label="Fermer">
            ✕
          </button>
        </div>
        <div className="modal-corps">
          {etat.erreur && <p className="alerte">{etat.erreur}</p>}
          {!d && !etat.erreur && <p className="info-chargement">Chargement…</p>}
          {d && !d.trouve && <p className="alerte">Ce secteur n’est pas planifié ce mois-ci.</p>}
          {d?.trouve && (
            <>
              <Barres jours={d.jours} selection={selection} onSelection={setSelection} />
              <h3>Jour par jour</h3>
              <table className="tableau-chutes">
                <thead>
                  <tr>
                    <th>Jour</th>
                    <th className="num">Encaissé</th>
                    <th>Même jour réf.</th>
                    <th className="num">Encaissé réf.</th>
                    <th className="num">Variation du jour</th>
                    <th className="num">Cumul</th>
                    <th className="num">Cumul réf.</th>
                    <th className="num">Variation cumul</th>
                  </tr>
                </thead>
                <tbody>
                  {d.jours.map((j, i) => (
                    <tr
                      key={j.jour}
                      ref={(el) => {
                        lignesTableau.current[i] = el;
                      }}
                      className={`ligne-secteur ${CLASSES[j.statut_code] || ''}${selection === i ? ' jour-actif' : ''}`}
                      onClick={() => setSelection(selection === i ? null : i)}
                    >
                      <td>{fmtDate(j.jour)}</td>
                      <td className="num">{fmtMontant(j.montant_jour)}</td>
                      <td>{fmtDate(j.jour_ref)}</td>
                      <td className="num">{fmtMontant(j.montant_jour_ref)}</td>
                      <td className="num">{fmtPct(j.variation_jour_pct)}</td>
                      <td className="num">{fmtMontant(j.cumul)}</td>
                      <td className="num">{fmtMontant(j.cumul_ref)}</td>
                      <td className={`num${j.statut_code === 'SECT_CHUTE' ? ' negatif' : ''}`}>{fmtPct(j.variation_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <h3>Abonnés du {d.secteur.periode} (plus forte baisse en premier)</h3>
              <DataTable colonnes={COLONNES_ABONNES} lignes={d.abonnes} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function Planifies({ filtres, version, recherche }) {
  const [annee, setAnnee] = useState(new Date().getFullYear());
  const { data, chargement, erreur, attente } = useData('alertes_secteurs/planifies', { centre: filtres.centre }, version, {
    annee,
  });
  if (erreur && !data) return <p className="alerte">{erreur}</p>;
  return (
    <>
      <div className="barre-encaissements">
        <label className="etiquette etiquette-ligne">
          Année
          <select className="champ" value={annee} onChange={(e) => setAnnee(Number(e.target.value))}>
            {[0, 1, 2].map((i) => {
              const a = new Date().getFullYear() - i;
              return (
                <option key={a} value={a}>
                  {a}
                </option>
              );
            })}
          </select>
        </label>
      </div>
      <DataTable
        colonnes={COLONNES_PLANIFIES}
        lignes={data?.lignes ?? []}
        recherche={recherche}
        chargement={chargement}
        attente={attente}
      />
    </>
  );
}

/**
 * Onglet Alertes secteurs : chaque jour J du mois M, chaque secteur planifié
 * en M est comparé au même jour de M-1 (mensuel) ou de M-2 (bimensuel).
 */
export default function AlertesSecteurs({ filtres, version, recherche }) {
  const [vue, setVue] = useState('alertes');
  const [jour, setJour] = useState(jourDefaut);
  const [seulement, setSeulement] = useState(true);
  const [actif, setActif] = useState(null);
  const zone = (filtres.zones || []).join(',');
  const { data, chargement, erreur, attente } = useData('alertes_secteurs', { centre: filtres.centre }, version, {
    jour,
    ...(zone ? { zone_centre: zone } : {}),
  });

  const lignes = useMemo(() => {
    const toutes = data?.lignes ?? [];
    return seulement ? toutes.filter((l) => l.statut_code === 'SECT_CHUTE' || l.statut_code === 'SECT_BAISSE') : toutes;
  }, [data, seulement]);

  return (
    <div className="alertes-secteurs">
      <div className="segment vue-encaissements" role="group" aria-label="Vue">
        <button
          type="button"
          className={`segment-btn${vue === 'alertes' ? ' actif' : ''}`}
          aria-pressed={vue === 'alertes'}
          onClick={() => setVue('alertes')}
        >
          Alertes du jour
        </button>
        <button
          type="button"
          className={`segment-btn${vue === 'planifies' ? ' actif' : ''}`}
          aria-pressed={vue === 'planifies'}
          onClick={() => setVue('planifies')}
        >
          Secteurs planifiés par mois
        </button>
      </div>

      {vue === 'planifies' ? (
        <Planifies filtres={filtres} version={version} recherche={recherche} />
      ) : (
        <>
          {erreur && !data && <p className="alerte">{erreur}</p>}
          {attente && (
            <p className="info-chargement" role="status">
              {attente}
            </p>
          )}
          <div className="barre-encaissements">
            <label className="etiquette etiquette-ligne">
              Jour
              <input
                type="date"
                className="champ"
                value={jour}
                max={aujourdHui()}
                onChange={(e) => e.target.value && setJour(e.target.value)}
              />
            </label>
            <button type="button" className="btn btn-petit" onClick={() => setJour(jourVoisin(jour, -1))}>
              ◀ Jour précédent
            </button>
            <button type="button" className="btn btn-petit" disabled={jour >= aujourdHui()} onClick={() => setJour(jourVoisin(jour, 1))}>
              Jour suivant ▶
            </button>
            <a
              className="btn btn-petit"
              href={`/api/alertes_secteurs/export.xlsx?jour=${data?.jour ?? jour}${filtres.centre ? `&centre=${filtres.centre}` : ''}${zone ? `&zone_centre=${encodeURIComponent(zone)}` : ''}`}
            >
              Exporter Excel (chutes)
            </a>
            <label className="etiquette etiquette-ligne">
              <input type="checkbox" checked={seulement} onChange={(e) => setSeulement(e.target.checked)} />
              Seulement les chutes et baisses
            </label>
          </div>
          <DataTable
            colonnes={colonnesChutes(data?.periode, lignes)}
            lignes={lignes}
            recherche={recherche}
            chargement={chargement}
            attente={attente}
            onLigne={setActif}
            aideLigne="Cliquer pour voir le secteur jour par jour"
            classeLigne={(l) => CLASSES[l.statut_code] || ''}
          />
          {actif && <DetailSecteur ligne={actif} jour={data?.jour ?? jour} onFermer={() => setActif(null)} />}
        </>
      )}
    </div>
  );
}

/** Liste des secteurs en chute, ouverte depuis le bandeau. */
function ListeChutes({ data, zone, onValider, onFermer }) {
  const zoneExport = zone ? `&zone_centre=${encodeURIComponent(zone)}` : '';
  const [actif, setActif] = useState(null);
  const [envoi, setEnvoi] = useState(false);
  const chutes = (data.lignes ?? [])
    .filter((l) => l.statut_code === 'SECT_CHUTE')
    .sort((a, b) => Number(a.vu) - Number(b.vu));
  const nonVues = chutes.filter((l) => !l.vu);

  useEffect(() => {
    const h = (e) => e.key === 'Escape' && !actif && onFermer();
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onFermer, actif]);

  const valider = async () => {
    setEnvoi(true);
    try {
      await onValider(nonVues.map((l) => l.sect_id));
    } finally {
      setEnvoi(false);
    }
  };

  return (
    <div className="modal-fond" onClick={onFermer}>
      <div className="modal-chutes modal-large" onClick={(e) => e.stopPropagation()}>
        <div className="modal-entete">
          <h2>
            {chutes.length} secteur{chutes.length > 1 ? 's' : ''} en chute
            {nonVues.length !== chutes.length && ` · ${nonVues.length} non validé${nonVues.length > 1 ? 's' : ''}`}
          </h2>
          <span className="modal-periode">
            Cumul du {data.periode} · toute la Mauritanie
          </span>
          <button type="button" className="modal-fermer" onClick={onFermer} aria-label="Fermer">
            ✕
          </button>
        </div>
        <div className="modal-corps">
          <table className="tableau-chutes">
            <thead>
              <tr>
                {colonnesChutes(data.periode, chutes).map((c) => (
                  <th key={c.cle} className={c.type === 'texte' ? '' : 'num'}>
                    {c.libelle}
                  </th>
                ))}
                <th>Validé</th>
              </tr>
            </thead>
            <tbody>
              {chutes.map((l) => (
                <tr
                  key={l.sect_id}
                  className={`ligne-secteur${l.vu ? ' sect-vu' : ' sect-chute'}`}
                  onClick={() => setActif(l)}
                  title="Cliquer pour voir le secteur jour par jour"
                >
                  <td>{l.zone}</td>
                  <td>{l.centre}</td>
                  <td>{l.nom_centre}</td>
                  <td>{l.secteur}</td>
                  <td className="num">{fmtMontant(l.cumul_ref)}</td>
                  <td className="num">{fmtMontant(l.cumul)}</td>
                  <td className="num negatif">{fmtMontant(l.ecart)}</td>
                  <td className="num negatif">{fmtPct(l.variation_pct)}</td>
                  <td className="num">{fmtEntier(l.nb_recus_ref)}</td>
                  <td className="num">{fmtEntier(l.nb_recus)}</td>
                  <td className="num">{fmtEntier(l.nb_abonnes_ref)}</td>
                  <td className="num">{fmtEntier(l.nb_abonnes)}</td>
                  <td>{l.vu ? 'Oui' : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="modal-pied">
          <span />
          <a className="btn" href={`/api/alertes_secteurs/export.xlsx?jour=${data.jour}${zoneExport}`}>
            Exporter Excel
          </a>
          <button type="button" className="btn btn-alerte" disabled={envoi || !nonVues.length} onClick={valider}>
            {envoi ? 'Validation…' : 'C’est bien, j’ai vu'}
          </button>
        </div>
      </div>
      {actif && (
        <div onClick={(e) => e.stopPropagation()}>
          <DetailSecteur ligne={actif} jour={data.jour} onFermer={() => setActif(null)} />
        </div>
      )}
    </div>
  );
}

/**
 * Pastille rouge « N nouvelles alertes » dans l’en-tête, tant que des secteurs
 * en chute ne sont pas validés. Un clic ouvre leur liste ; « C’est bien » les valide.
 */
export function BandeauAlertesSecteurs({ centre, zone, version }) {
  const [relance, setRelance] = useState(0);
  const [ouvert, setOuvert] = useState(false);
  const { data } = useData('alertes_secteurs', { centre }, version + relance, zone ? { zone_centre: zone } : null);
  if (!data || !data.nb_chutes_non_vues) return null;

  const valider = async (ids) => {
    if (ids.length) await marquerSecteursVus(data.jour, ids);
    setOuvert(false);
    setRelance((n) => n + 1);
  };

  return (
    <>
      <button type="button" className="pastille-alertes" onClick={() => setOuvert(true)} title="Voir les secteurs en chute">
        <span aria-hidden="true">🔔</span>
        {data.nb_chutes_non_vues} nouvelle{data.nb_chutes_non_vues > 1 ? 's' : ''} alerte
        {data.nb_chutes_non_vues > 1 ? 's' : ''}
      </button>
      {ouvert && <ListeChutes data={data} zone={zone} onValider={valider} onFermer={() => setOuvert(false)} />}
    </>
  );
}
