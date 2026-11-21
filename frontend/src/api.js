function requete(filtres, extra = {}) {
  const p = new URLSearchParams();
  if (filtres?.dateDebut) p.set('date_debut', filtres.dateDebut);
  if (filtres?.dateFin) p.set('date_fin', filtres.dateFin);
  if (filtres?.centre) p.set('centre', filtres.centre);
  if (filtres?.zone && filtres.zone !== 'tous') p.set('zone', filtres.zone);
  if (filtres?.segment && filtres.segment !== 'tous') p.set('segment', filtres.segment);
  Object.entries(extra).forEach(([k, v]) => p.set(k, v));
  return p.toString();
}

export async function getJSON(chemin, filtres, signal, extra = {}) {
  const q = requete(filtres, extra);
  const rep = await fetch(`/api/${chemin}${q ? `?${q}` : ''}`, { signal });
  if (!rep.ok) {
    let detail = `Erreur ${rep.status}`;
    try {
      const corps = await rep.json();
      if (corps?.detail) detail = typeof corps.detail === 'string' ? corps.detail : JSON.stringify(corps.detail);
    } catch {
      /* réponse non JSON : on garde le code HTTP */
    }
    throw new Error(detail);
  }
  const corps = await rep.json();
  // 202 : les données sont en cours de lecture dans Oracle, il faut redemander.
  if (rep.status === 202) return { enChargement: true, message: corps?.message ?? 'Lecture dans Oracle en cours…' };
  return corps;
}

export function urlExport(jeu, format, filtres) {
  return `/api/export?${requete(filtres, { jeu, format })}`;
}

/** Export de l'onglet Rapprochement bancaire : tout l'onglet, ou un seul tableau (`partie`). */
export function urlExportReleve(filtres, partie = 'tout', format = 'xlsx', resultat = undefined) {
  const extra = { partie, format };
  if (resultat) extra.resultat = resultat; // une seule case des résultats
  return `/api/releve/export?${requete(filtres, extra)}`;
}

/** Note des reçus non validés comme consultés : l'alerte ne les signale plus. */
export async function marquerVus(recus) {
  const rep = await fetch('/api/non_valides/vus', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ recus }),
  });
  if (!rep.ok) throw new Error(`Erreur ${rep.status}`);
  return rep.json();
}

/** Valide (« c'est bien ») des secteurs en chute : le bandeau ne les montre plus. */
export async function marquerSecteursVus(jour, sectIds) {
  const rep = await fetch('/api/alertes_secteurs/vus', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jour, sect_ids: sectIds }),
  });
  if (!rep.ok) throw new Error(`Erreur ${rep.status}`);
  return rep.json();
}

/** Envoie un relevé bancaire (classeur Excel) : le serveur le lit et l'enregistre. */
export async function importerReleve(fichier) {
  const p = new URLSearchParams({ nom: fichier.name || 'releve.xlsx' });
  const rep = await fetch(`/api/releve?${p.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: fichier,
  });
  if (!rep.ok) {
    let detail = `Erreur ${rep.status}`;
    try {
      const corps = await rep.json();
      if (corps?.detail) detail = typeof corps.detail === 'string' ? corps.detail : JSON.stringify(corps.detail);
    } catch {
      /* réponse non JSON */
    }
    throw new Error(detail);
  }
  return rep.json();
}

/** Efface le relevé bancaire importé (bouton « Réinitialiser »). */
export async function supprimerReleve() {
  const rep = await fetch('/api/releve', { method: 'DELETE' });
  if (!rep.ok) throw new Error(`Erreur ${rep.status}`);
  return rep.json();
}

/** Envoie le classeur d'un centre saisi par la DFC : le numéro du centre est dans le nom du fichier. */
export async function importerDfc(fichier) {
  const p = new URLSearchParams({ nom: fichier.name || 'dfc.xlsx' });
  const rep = await fetch(`/api/dfc?${p.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: fichier,
  });
  if (!rep.ok) {
    let detail = `Erreur ${rep.status}`;
    try {
      const corps = await rep.json();
      if (corps?.detail) detail = typeof corps.detail === 'string' ? corps.detail : JSON.stringify(corps.detail);
    } catch {
      /* réponse non JSON */
    }
    throw new Error(detail);
  }
  return rep.json();
}

/** Efface la saisie de la DFC importée (bouton « Réinitialiser »). */
export async function supprimerDfc() {
  const rep = await fetch('/api/dfc', { method: 'DELETE' });
  if (!rep.ok) throw new Error(`Erreur ${rep.status}`);
  return rep.json();
}

/** Export du croisement avec la DFC : tout, ou une seule case des résultats. */
export function urlExportDfc(filtres, format = 'xlsx', resultat = undefined, partie = undefined) {
  const extra = { format };
  if (resultat) extra.resultat = resultat;
  if (partie) extra.partie = partie; // « mois » : le tableau par mois seul
  return `/api/dfc/export?${requete(filtres, extra)}`;
}

/** Envoie un fichier de reçus manuels (Excel ou CSV). */
export async function importerManuels(fichier) {
  const p = new URLSearchParams({ nom: fichier.name || 'recus_manuels.xlsx' });
  const rep = await fetch(`/api/manuels?${p.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: fichier,
  });
  if (!rep.ok) {
    let detail = `Erreur ${rep.status}`;
    try {
      const corps = await rep.json();
      if (corps?.detail) detail = typeof corps.detail === 'string' ? corps.detail : JSON.stringify(corps.detail);
    } catch {
      /* réponse non JSON */
    }
    throw new Error(detail);
  }
  return rep.json();
}

/** Efface les reçus manuels importés (bouton « Réinitialiser »). */
export async function supprimerManuels() {
  const rep = await fetch('/api/manuels', { method: 'DELETE' });
  if (!rep.ok) throw new Error(`Erreur ${rep.status}`);
  return rep.json();
}

/**
 * Export de la vérification des reçus manuels, avec les filtres de l'onglet
 * (reference, centre, du, au, statut), ou `partie: 'references'` pour les
 * références mal saisies.
 */
export function urlExportManuels(parametres = {}) {
  const p = new URLSearchParams();
  Object.entries(parametres).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') p.set(k, v);
  });
  const q = p.toString();
  return `/api/manuels/export${q ? `?${q}` : ''}`;
}

/** Export de l'évolution des encaissements sur l'année (année et centre du haut de la page). */
export function urlExportComparaisonMois(filtres, mois) {
  return `/api/encaissements/evolution/export?${requete({ centre: filtres?.centre, zone: filtres?.zone }, { mois })}`;
}

export function urlExportObjectifs(filtres, mois) {
  return `/api/encaissements/objectifs/export?${requete({ centre: filtres?.centre, zone: filtres?.zone, segment: filtres?.segment }, { mois })}`;
}

export function urlExportEvolution(filtres) {
  return `/api/encaissements/annee/export?${requete({ centre: filtres?.centre, zone: filtres?.zone, segment: filtres?.segment }, { annee: (filtres?.dateFin || '').slice(0, 4) })}`;
}

/** Recharge la période affichée depuis Oracle. */
export async function forcerRafraichissement(filtres) {
  const q = requete({ dateDebut: filtres?.dateDebut, dateFin: filtres?.dateFin });
  const rep = await fetch(`/api/refresh${q ? `?${q}` : ''}`, { method: 'POST' });
  if (!rep.ok) throw new Error(`Erreur ${rep.status}`);
  return rep.json();
}

/**
 * Envoie le fichier d'état de rapprochement au serveur.
 * Retourne le résultat (JSON) ou, avec format = 'xlsx', le classeur à télécharger.
 */
export async function comparerFichier(fichier, { tolerance = 5, etat = '', format = 'json' } = {}) {
  const p = new URLSearchParams({ tolerance: String(tolerance), format });
  if (etat) p.set('etat', etat);
  const rep = await fetch(`/api/rapprochement/comparer?${p.toString()}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: fichier,
  });
  if (!rep.ok) {
    let detail = `Erreur ${rep.status}`;
    try {
      const corps = await rep.json();
      if (corps?.detail) detail = typeof corps.detail === 'string' ? corps.detail : JSON.stringify(corps.detail);
    } catch {
      /* réponse non JSON */
    }
    throw new Error(detail);
  }
  return format === 'xlsx' ? rep.blob() : rep.json();
}
