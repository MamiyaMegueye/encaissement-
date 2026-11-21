import { useEffect, useMemo, useState } from 'react';
import { fmtMontant, fmtPct } from '../format.js';
import { useData } from '../hooks/useData.js';
import DataTable from './DataTable.jsx';

const COLONNES = [
  { cle: 'zone', libelle: 'Zone', type: 'texte' },
  { cle: 'centre', libelle: 'Centre', type: 'texte' },
  { cle: 'nom_centre', libelle: 'Nom du centre', type: 'texte' },
  { cle: 'statut', libelle: 'Statut', type: 'statut', code: 'statut_code' },
  { cle: 'especes', libelle: 'Espèces encaissées', type: 'montant', total: true },
  { cle: 'verse', libelle: 'Versé', type: 'montant', total: true },
  { cle: 'ecart', libelle: 'Écart (encaissé − versé)', type: 'montant', total: true },
  { cle: 'ecart_pct', libelle: 'Écart (%)', type: 'pct' },
  { cle: 'nb_mois_sans_versement', libelle: 'Mois sans versement', type: 'entier' },
  { cle: 'mois_sans_versement', libelle: 'Lesquels', type: 'texte' },
];

// Rouge : gros écart non versé ; orange : mois sans versement.
const CLASSES = { rouge: 'compa-rouge', orange: 'compa-orange' };

function DetailCentre({ ligne, annee, onFermer }) {
  useEffect(() => {
    const h = (e) => e.key === 'Escape' && onFermer();
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onFermer]);

  return (
    <div className="modal-fond" onClick={onFermer}>
      <div className="modal-chutes modal-large" onClick={(e) => e.stopPropagation()}>
        <div className="modal-entete">
          <h2>
            Centre {ligne.centre} · {ligne.nom_centre}
          </h2>
          <span className="modal-periode">Espèces encaissées et versées, mois par mois ({annee})</span>
          <button type="button" className="modal-fermer" onClick={onFermer} aria-label="Fermer">
            ✕
          </button>
        </div>
        <div className="modal-corps">
          <table className="tableau-chutes">
            <thead>
              <tr>
                <th>Mois</th>
                <th className="num">Espèces encaissées</th>
                <th className="num">Versé</th>
                <th className="num">Écart du mois</th>
                <th className="num">Versements</th>
                <th className="num">Non validé</th>
                <th className="num">Écart cumulé</th>
                <th>Alerte</th>
              </tr>
            </thead>
            <tbody>
              {ligne.detail.map((m) => (
                <tr key={m.mois} className={m.sans_versement ? 'compa-orange' : ''}>
                  <td>{m.libelle_mois}</td>
                  <td className="num">{fmtMontant(m.especes)}</td>
                  <td className="num">{fmtMontant(m.verse)}</td>
                  <td className={`num${m.ecart > 0 ? ' negatif' : ''}`}>{fmtMontant(m.ecart)}</td>
                  <td className="num">{m.nb_versements}</td>
                  <td className="num">{fmtMontant(m.verse_non_valide)}</td>
                  <td className={`num${m.ecart_cumule > 0 ? ' negatif' : ''}`}>{fmtMontant(m.ecart_cumule)}</td>
                  <td>{m.statut}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

/**
 * Onglet Compa enca versement : pour chaque centre, espèces encaissées et
 * versées sur l’année ; mois clos sans versement et écart cumulé signalés.
 */
export default function CompaVersements({ filtres, version, recherche }) {
  const anneeCourante = new Date().getFullYear();
  const [annee, setAnnee] = useState(anneeCourante);
  const [seulement, setSeulement] = useState(true);
  const [actif, setActif] = useState(null);
  const zone = (filtres.zones || []).join(',');
  const { data, chargement, erreur, attente } = useData('compa_versements', { centre: filtres.centre }, version, {
    annee,
    ...(zone ? { zone_centre: zone } : {}),
  });

  const lignes = useMemo(() => {
    const toutes = data?.lignes ?? [];
    return seulement ? toutes.filter((l) => l.statut_code !== 'COMPA_OK') : toutes;
  }, [data, seulement]);

  return (
    <div className="alertes-secteurs">
      {erreur && !data && <p className="alerte">{erreur}</p>}
      <div className="barre-encaissements">
        <label className="etiquette etiquette-ligne">
          Année
          <select className="champ" value={annee} onChange={(e) => setAnnee(Number(e.target.value))}>
            {[0, 1, 2].map((i) => (
              <option key={i} value={anneeCourante - i}>
                {anneeCourante - i}
              </option>
            ))}
          </select>
        </label>
        <label className="etiquette etiquette-ligne">
          <input type="checkbox" checked={seulement} onChange={(e) => setSeulement(e.target.checked)} />
          Seulement les centres en anomalie
        </label>
      </div>
      <DataTable
        colonnes={COLONNES}
        lignes={lignes}
        recherche={recherche}
        chargement={chargement}
        attente={attente}
        onLigne={setActif}
        aideLigne="Cliquer pour voir le centre mois par mois"
        classeLigne={(l) => CLASSES[l.couleur] || ''}
      />
      {actif && <DetailCentre ligne={actif} annee={data?.annee ?? annee} onFermer={() => setActif(null)} />}
    </div>
  );
}
