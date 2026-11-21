import { useEffect, useRef, useState } from 'react';

/** Filtre Zone à choix multiple : liste déroulante de cases à cocher. */
export default function FiltreZones({ zones, valeur, onChange }) {
  const [ouvert, setOuvert] = useState(false);
  const boite = useRef(null);

  useEffect(() => {
    if (!ouvert) return undefined;
    const fermer = (e) => {
      if (boite.current && !boite.current.contains(e.target)) setOuvert(false);
    };
    const echap = (e) => e.key === 'Escape' && setOuvert(false);
    document.addEventListener('mousedown', fermer);
    window.addEventListener('keydown', echap);
    return () => {
      document.removeEventListener('mousedown', fermer);
      window.removeEventListener('keydown', echap);
    };
  }, [ouvert]);

  const basculer = (z) => onChange(valeur.includes(z) ? valeur.filter((x) => x !== z) : [...valeur, z]);
  const libelle = !valeur.length ? 'Toutes les zones' : valeur.length <= 2 ? valeur.join(', ') : `${valeur.length} zones`;

  return (
    <div className="etiquette filtre-zones" ref={boite}>
      Zone
      <button
        type="button"
        className="champ filtre-zones-bouton"
        aria-haspopup="listbox"
        aria-expanded={ouvert}
        onClick={() => setOuvert((o) => !o)}
      >
        <span>{libelle}</span>
        <span aria-hidden="true">▾</span>
      </button>
      {ouvert && (
        <div className="filtre-zones-liste" role="listbox" aria-multiselectable="true">
          <label className="filtre-zones-option">
            <input type="checkbox" checked={!valeur.length} onChange={() => onChange([])} />
            Toutes les zones
          </label>
          {zones.map((z) => (
            <label key={z} className="filtre-zones-option">
              <input type="checkbox" checked={valeur.includes(z)} onChange={() => basculer(z)} />
              {z}
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
