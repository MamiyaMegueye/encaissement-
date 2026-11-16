"""
INTERFACE INTERACTIVE DU MODÈLE MULTINIVEAU
===========================================
    python interface.py

Fait tourner le modèle (mêmes données et réglages que lancer.py / parametres.py), puis écrit
sorties/objectifs_interactifs.html : une page à ouvrir dans le navigateur (double-clic), sans Internet.

Elle contient : réglage en direct de la règle du DG (coefficient x Pic, seuil des mois exceptionnels,
total du mois), encaissements mensuels par zone, distribution des centres, étapes du modèle,
fiche détaillée d'un centre, matrices de corrélation avec explication, tableau par zone avec totaux.
"""
import contextlib
import io
import json
import os
import sys

import numpy as np
import pandas as pd

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)
import modele_objectifs as M  # noqa: E402
import parametres as C  # noqa: E402

NOMS = {"encaissement": "Encaissement (cible)", "nb_gc": "Nb gros consommateurs", "ca_dom": "CA hors GC",
        "ca_gc": "CA gros consommateurs", "volume_int": "Volume produit", "nb_abonnes": "Nb abonnés (retiré)"}


def zones_centres():
    ch = os.path.join(ICI, C.DOSSIER_DONNEES, getattr(C, "FICHIER_CENTRES_PAR_ZONE", "centres_par_zone.xlsx"))
    if not os.path.exists(ch):
        return {}, []
    z = pd.read_excel(ch)
    z.columns = ["zone", "exp", "centre"][:len(z.columns)]
    z["exp"] = z["exp"].astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    z["zone"] = z["zone"].astype(str).str.strip()
    return z.set_index("exp")["zone"].to_dict(), list(dict.fromkeys(z["zone"]))


def nettoyer(x):
    if isinstance(x, dict):
        return {k: nettoyer(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [nettoyer(v) for v in x]
    if isinstance(x, (float, np.floating)):
        return None if not np.isfinite(x) else round(float(x), 4)
    if isinstance(x, np.integer):
        return int(x)
    return x


def main():
    print("  Calcul du modèle multiniveau ...")
    with contextlib.redirect_stdout(io.StringIO()):
        p = M.construire_panel()
        m, poids, p = M.estimer(p)
        v_tab, val = M.valider(p)
        res, info = M.calculer_objectifs(p, m)

    e = M.INFO["encaissements"]
    win = e[(e["mois"] >= C.MOIS_DEBUT_ENCAISSEMENTS) & (e["mois"] <= C.MOIS_FIN)]
    med = win.groupby("centre")["encaissement_brut"].median()
    mois12 = sorted(e[e["mois"] <= C.MOIS_FIN]["mois"].unique())[-12:]
    raw = e.pivot_table(index="centre", columns="mois", values="encaissement_brut", aggfunc="sum")
    m26 = [x for x in raw.columns if x.startswith(str(C.ANNEE_PIC)) and x <= C.MOIS_FIN]
    zmap, ordre = zones_centres()
    indice = M.INFO.get("saisonnalite", {}).get(C.MOIS_OBJECTIF[5:7], 1.0)

    centres = []
    for _, l in res.iterrows():
        c = str(l["centre"])
        r = raw.loc[c] if c in raw.index else pd.Series(dtype=float)
        centres.append(dict(
            c=c, n=str(l["nom_centre"]), z=str(zmap.get(c, l.get("zone", "À CLASSER"))),
            mod=float(l["objectif_calcule"] * l["indice_saisonnier"]), etat=float(l["objectif_etat"]),
            pot=float(l.get("potentiel_modele", np.nan)), base=float(l.get("base", np.nan)),
            med=float(med[c]) if c in med.index else None,
            p26=[None if pd.isna(r.get(x)) else float(r.get(x)) for x in m26],
            m12=[0.0 if pd.isna(r.get(x)) else float(r.get(x)) for x in mois12],
            obj_py=float(l["objectif_du_mois_total"])))

    corr = {}
    for g, zs in M.GROUPES.items():
        d = p[p["zone"].isin(zs)]
        cols = ["encaissement", "nb_gc", "ca_dom", "ca_gc"] + (["volume_int"] if g == "Intérieur" else []) + ["nb_abonnes"]
        cols = [k for k in cols if k in d.columns and d[k].notna().any() and d[k].std() > 0]
        corr[g] = dict(vars=[NOMS[k] for k in cols], keys=cols, nb=int(d["centre"].nunique()),
                       centres=d.groupby("centre")[cols].mean().corr().values.tolist(),
                       obs=d[cols].corr().values.tolist())
    inv = {v_: k for k, v_ in M.CRITERES.items()}
    pw = [dict(g=l["groupe"], k=l["critere"], key=inv.get(l["critere"], ""), v=float(l["poids_pct"]),
               p=float(l["p_value"])) for _, l in poids.iterrows()]
    r2 = {g: [mm.r2_marginal, mm.r2_conditionnel] for g, mm in m.groupes.items()}

    data = dict(
        mois_obj=C.MOIS_OBJECTIF, mois12=mois12, ordre=ordre, indice=indice, coef=C.COEF_PLAFOND,
        seuil=getattr(C, "SEUIL_EXCEPTIONNEL", 2.5), total=getattr(C, "OBJECTIF_MAX_MOIS", None),
        prog=C.PROGRESSION_MAX, cible=(C.OBJECTIF_NATIONAL or 0) / 12, nb_obs=int(len(p)),
        fenetre=[str(p["mois"].min()), str(p["mois"].max())], centres=centres, poids=pw, r2=r2, corr=corr,
        validation=val if isinstance(val, dict) else None)
    data = nettoyer(data)

    gabarit = open(os.path.join(ICI, "interface_modele.html"), encoding="utf-8").read()
    html = gabarit.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    sortie = os.path.join(ICI, C.DOSSIER_SORTIES, "objectifs_interactifs.html")
    os.makedirs(os.path.dirname(sortie), exist_ok=True)
    open(sortie, "w", encoding="utf-8").write(html)
    tot = sum(x["obj_py"] for x in centres)
    print(f"  Total {C.MOIS_OBJECTIF} : {tot:,.0f} MRU ({len(centres)} centres)")
    print(f"  ✅ {sortie}\n  Ouvrez ce fichier dans votre navigateur (double-clic).")
    if "--ouvrir" in sys.argv or os.name == "nt":
        try:
            import webbrowser
            webbrowser.open("file:///" + sortie.replace("\\", "/"))
        except Exception:
            pass


if __name__ == "__main__":
    main()
