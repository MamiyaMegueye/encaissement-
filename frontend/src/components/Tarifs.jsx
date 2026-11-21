import { useState } from 'react';
import { fmtEntier } from '../format.js';
import { useData, useRetard } from '../hooks/useData.js';
import DataTable from './DataTable.jsx';

const COLONNES_SYNTHESE = [
  { cle: 'tarif', libelle: 'Tarif', type: 'texte' },
  { cle: 'nb_references', libelle: 'Références', type: 'entier', total: true },
  { cle: 'pct_references', libelle: '% références', type: 'pct' },
  { cle: 'nb_recus', libelle: 'Reçus', type: 'entier', total: true },
  { cle: 'pct_recus', libelle: '% reçus', type: 'pct' },
  { cle: 'montant', libelle: 'Montant encaissé', type: 'montant', total: true },
  { cle: 'pct_montant', libelle: '% montant', type: 'pct' },
];

const COLONNES_REFERENCES = [
  { cle: 'reference', libelle: 'Référence', type: 'texte' },
  { cle: 'type_reference', libelle: 'Type', type: 'texte' },
  { cle: 'centre', libelle: 'Centre', type: 'texte' },
  { cle: 'nom_centre', libelle: 'Nom du centre', type: 'texte' },
  { cle: 'tarif', libelle: 'Tarif', type: 'texte' },
  { cle: 'statut', libelle: 'Statut', type: 'texte' },
  { cle: 'nb_recus', libelle: 'Reçus', type: 'entier', total: true },
  { cle: 'montant', libelle: 'Montant encaissé', type: 'montant', total: true },
];

/**
 * Onglet Tarifs : chaque référence encaissée sur la période (reçus validés)
 * avec son tarif, et la répartition des références par tarif.
 */
export default function Tarifs({ filtres, version, recherche }) {
  const [tarif, setTarif] = useState('');
  const texte = useRetard(recherche.trim());
  const extra = {};
  if (texte) extra.recherche = texte;
  if (tarif) extra.tarif = tarif;
  const { data, chargement, erreur, attente } = useData('tarifs', filtres, version, Object.keys(extra).length ? extra : null);

  if (erreur && !data) return <p className="alerte">{erreur}</p>;

  const synthese = data?.synthese ?? [];
  const lignes = data?.lignes ?? [];
  const total = data?.total_lignes ?? lignes.length;

  return (
    <>
      {attente && (
        <p className="info-chargement" role="status">
          {attente}
        </p>
      )}

      <h3 className="titre-section">Répartition par tarif</h3>
      <DataTable
        colonnes={COLONNES_SYNTHESE}
        lignes={synthese}
        chargement={chargement}
        attente={attente}
        onLigne={(l) => setTarif(l.tarif === tarif ? '' : l.tarif)}
        aideLigne="Cliquer pour ne garder que ce tarif"
      />

      <div className="barre-encaissements">
        <h3 className="titre-section">Références</h3>
        <label className="etiquette etiquette-ligne">
          Tarif
          <select className="champ" value={tarif} onChange={(e) => setTarif(e.target.value)} aria-label="Tarif">
            <option value="">Tous les tarifs</option>
            {synthese.map((s) => (
              <option key={s.tarif} value={s.tarif}>
                {s.tarif}
              </option>
            ))}
            {tarif && !synthese.some((s) => s.tarif === tarif) && <option value={tarif}>{tarif}</option>}
          </select>
        </label>
      </div>
      {data?.tronque && (
        <p className="info-chargement" role="status">
          {fmtEntier(total)} références dans la sélection : seules les {fmtEntier(lignes.length)} premières sont
          affichées. Choisissez un centre, un tarif ou utilisez la recherche.
        </p>
      )}
      <DataTable
        colonnes={COLONNES_REFERENCES}
        lignes={lignes}
        chargement={chargement || texte !== recherche.trim()}
        attente={attente}
        totalLignes={total}
      />
    </>
  );
}
