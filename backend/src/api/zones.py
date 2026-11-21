"""Zones des centres (fichier « les centres par zone »).

Sert au filtre de zone des encaissements : Nouakchott seul, Nouakchott et
Nouadhibou ensemble, ou toute la Mauritanie.
"""
from __future__ import annotations

# code du centre -> (zone, nom du centre)
CENTRES: dict[str, tuple[str, str]] = {
    "86": ("NKTT NORD", "WAD NAGA"),
    "48": ("NKTT NORD", "DAR NAIM-SUD 2"),
    "96": ("NKTT NORD", "KSAR"),
    "47": ("NKTT NORD", "TEYARETT 1"),
    "52": ("NKTT NORD", "MELLAH 3"),
    "99": ("NKTT NORD", "DAR NAIM-SUD 1"),
    "98": ("NKTT NORD", "TOUJOUNINE"),
    "44": ("NKTT NORD", "DAR NAIM-NORD"),
    "41": ("NKTT NORD", "TEYARETT 2"),
    "91": ("NKTT OUEST", "TVZ NORD 1"),
    "93": ("NKTT OUEST", "SEBKHA"),
    "97": ("NKTT OUEST", "TVZ NORD 2"),
    "92": ("NKTT OUEST", "CAPITALE"),
    "43": ("NKTT OUEST", "TVZ SUD"),
    "55": ("NKTT OUEST", "TVZ NORD 3"),
    "46": ("NKTT SUD", "MELLAH 2"),
    "53": ("NKTT SUD", "RIYADH 2"),
    "42": ("NKTT SUD", "CARREFOUR 2"),
    "49": ("NKTT SUD", "TARHIL 1"),
    "90": ("NKTT SUD", "ARAFAT"),
    "50": ("NKTT SUD", "TARHIL 2"),
    "94": ("NKTT SUD", "EL MINA"),
    "45": ("NKTT SUD", "MELLAH 1"),
    "51": ("NKTT SUD", "TARHIL 3"),
    "89": ("NKTT SUD", "RIYAD"),
    "95": ("NKTT SUD", "CARREFOUR"),
    "60": ("NOUADHIBOU", "NOUADHIBOU 1"),
    "40": ("NOUADHIBOU", "NOUADHIBOU 3"),
    "80": ("NOUADHIBOU", "NOUADHIBOU 2"),
    "15": ("NOUADHIBOU", "CHAMI"),
    "33": ("INTERIEUR EST", "OULATA"),
    "101": ("INTERIEUR EST", "AIWNAT ZBEL"),
    "102": ("INTERIEUR EST", "TOUIL"),
    "84": ("INTERIEUR EST", "BASSIKNOU"),
    "71": ("INTERIEUR EST", "NEMA"),
    "38": ("INTERIEUR EST", "TERMESSA"),
    "81": ("INTERIEUR EST", "DJIGUENI"),
    "82": ("INTERIEUR EST", "KANKOUSSA"),
    "34": ("INTERIEUR EST", "AMOURJ"),
    "83": ("INTERIEUR EST", "KOUBENNI"),
    "70": ("INTERIEUR EST", "AIOUN EL ATROUSS"),
    "32": ("INTERIEUR EST", "TAMCHAKET"),
    "31": ("INTERIEUR EST", "BOUMDEID"),
    "85": ("INTERIEUR EST", "TINTANE NOUVEAU"),
    "72": ("INTERIEUR EST", "TIMBEDRA"),
    "37": ("INTERIEUR EST", "VASSALA"),
    "39": ("INTERIEUR EST", "KIFFA 2"),
    "30": ("INTERIEUR EST", "BARKEOL"),
    "73": ("INTERIEUR EST", "KIFFA 1"),
    "35": ("INTERIEUR EST", "ADEL BAGROU"),
    "74": ("INTERIEUR EST", "GUEROU"),
    "88": ("INTERIEUR EST", "N'BEIKETTE LEHWACH"),
    "104": ("INTERIEUR SUD", "MAAL"),
    "105": ("INTERIEUR SUD", "LEXEIBA"),
    "103": ("INTERIEUR SUD", "BOUHDIDA"),
    "29": ("INTERIEUR SUD", "OULD YENGE"),
    "67": ("INTERIEUR SUD", "ALEG"),
    "76": ("INTERIEUR SUD", "M'BOUT"),
    "27": ("INTERIEUR SUD", "MAGHAMA"),
    "77": ("INTERIEUR SUD", "MAGTALAHAJAR"),
    "106": ("INTERIEUR SUD", "WOMPOU"),
    "61": ("INTERIEUR SUD", "BOGHE"),
    "25": ("INTERIEUR SUD", "BABABE"),
    "65": ("INTERIEUR SUD", "KAEDI"),
    "87": ("INTERIEUR SUD", "CHOGGAR"),
    "75": ("INTERIEUR SUD", "SELIBABY"),
    "26": ("INTERIEUR SUD", "M'BAGNE"),
    "36": ("INTERIEUR SUD", "SANGRAVA"),
    "107": ("INTERIEUR SUD", "GHABOU"),
    "28": ("INTERIEUR SUD", "MONGUEL"),
    "66": ("INTERIEUR NORD", "ATAR"),
    "21": ("INTERIEUR NORD", "FDEIRICK"),
    "16": ("INTERIEUR NORD", "AOUJEFT"),
    "78": ("INTERIEUR NORD", "TIDJIKJA"),
    "17": ("INTERIEUR NORD", "CHINGUITY"),
    "14": ("INTERIEUR NORD", "BENICHAB"),
    "18": ("INTERIEUR NORD", "OUADANE"),
    "19": ("INTERIEUR NORD", "MOUDJERIA"),
    "20": ("INTERIEUR NORD", "TICHIT"),
    "62": ("INTERIEUR NORD", "AKJOUJT"),
    "64": ("INTERIEUR TRARZA", "ROSSO"),
    "68": ("INTERIEUR TRARZA", "BOUTILIMIT"),
    "69": ("INTERIEUR TRARZA", "MEDERDRA"),
    "23": ("INTERIEUR TRARZA", "RKIZ"),
    "24": ("INTERIEUR TRARZA", "KEUR MACENE"),
    "108": ("INTERIEUR TRARZA", "TEKANE"),
}

def _charger_fichier() -> None:
    """Complète CENTRES avec le fichier Excel « backend/referentiel/centres_par_zone.xlsx »
    (colonnes ZONE, EXP, CENTRE), s'il existe. Le fichier fait foi : un centre
    qui y figure remplace celui de la liste ci-dessus. Modifier le fichier puis
    relancer le backend suffit à ajouter ou corriger un centre."""
    from pathlib import Path
    chemin = Path(__file__).resolve().parents[2] / "referentiel" / "centres_par_zone.xlsx"
    if not chemin.exists():
        return
    try:
        from openpyxl import load_workbook
        feuille = load_workbook(chemin, read_only=True, data_only=True).worksheets[0]
        lignes = feuille.iter_rows(values_only=True)
        entete = [str(v or "").strip().upper() for v in next(lignes)]
        i_zone, i_exp, i_centre = entete.index("ZONE"), entete.index("EXP"), entete.index("CENTRE")
        for ligne in lignes:
            zone, exp, nom = ligne[i_zone], ligne[i_exp], ligne[i_centre]
            if zone is None or exp is None:
                continue
            code = str(int(exp) if isinstance(exp, float) else exp).strip()
            propre = lambda v: " ".join(str(v or "").replace("\xa0", " ").split())
            CENTRES[code] = (propre(zone).upper(), propre(nom))
    except Exception as exc:  # fichier mal formé : on garde la liste intégrée
        import logging
        logging.getLogger("controle.zones").warning("Fichier des zones illisible (%s) : %s", chemin, exc)


_charger_fichier()

ZONES_NOUAKCHOTT = ("NKTT NORD", "NKTT OUEST", "NKTT SUD")
ZONE_NOUADHIBOU = "NOUADHIBOU"

GROUPES = {
    "tous": "Toute la Mauritanie",
    "nkt": "Nouakchott",
    "nkt_ndb": "Nouakchott + Nouadhibou",
}


def centres_du_groupe(groupe: str | None) -> set[str] | None:
    """Codes des centres d'un groupe de zones ; None pour toute la Mauritanie."""
    if not groupe or groupe == "tous":
        return None
    zones = set(ZONES_NOUAKCHOTT)
    if groupe == "nkt_ndb":
        zones.add(ZONE_NOUADHIBOU)
    return {code for code, (zone, _) in CENTRES.items() if zone in zones}


def zone_du_centre(code) -> str | None:
    return CENTRES.get(str(code).strip(), (None, None))[0]
