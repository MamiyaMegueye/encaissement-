"""Exports Excel et CSV des jeux de données."""
import csv
import io
from datetime import date

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, GradientFill, PatternFill, Side
from openpyxl.utils import get_column_letter

# (clé, libellé, type) — types : texte, entier, montant, pct, date
COLONNES = {
    "controle": [
        ("jour", "Jour", "date"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("statut", "Statut", "texte"),
        ("anomalies", "Anomalies", "texte"),
        ("report", "Report", "montant"),
        ("nb_recus", "Nb reçus", "entier"),
        ("recap_especes", "Espèces encaissées", "montant"),
        ("recap_cheques", "Chèques encaissés", "montant"),
        ("recap_virements", "Virements", "montant"),
        ("depenses", "Dépenses", "montant"),
        ("a_verser_jour", "À verser du jour", "montant"),
        ("recap_vers_especes", "Versé espèces", "montant"),
        ("recap_vers_cheques", "Versé chèques", "montant"),
        ("ecart_jour", "Écart du jour", "montant"),
        ("solde", "Solde non versé", "montant"),
        ("rupture_report", "Rupture de report", "montant"),
        ("verse_especes_valide", "Versements validés (détail)", "montant"),
        ("verse_especes_non_valide", "Versements non validés", "montant"),
        ("verse_especes_annule", "Versements annulés", "montant"),
        ("recus_especes", "Espèces (reçus)", "montant"),
        ("recus_cheques", "Chèques (reçus)", "montant"),
        ("recus_cheques_tresor", "Chèques de Trésor (reçus)", "montant"),
        ("recus_virements", "Virements (reçus)", "montant"),
        ("nb_annulations", "Nb annulations", "entier"),
        ("montant_annule", "Montant annulé", "montant"),
    ],
    "encaissements": [
        ("date_comptable", "Date", "date"),
        ("heure", "Heure", "texte"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("matricule", "Matricule", "texte"),
        ("nom_agent", "Nom de l'agent", "texte"),
        ("num_recu", "N° reçu", "texte"),
        ("reference", "Référence", "texte"),
        ("mode_paiement", "Mode", "texte"),
        ("nom_banque", "Banque", "texte"),
        ("num_piece", "N° chèque / pièce", "texte"),
        ("statut", "Statut", "texte"),
        ("secteur", "Secteur", "texte"),
        ("tarif_abonne", "Tarif", "texte"),
        ("statut_abonne", "Statut abonné", "texte"),
        ("montant", "Montant", "montant"),
    ],
    "versements": [
        ("date_versement", "Date du versement", "date"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("matricule", "Matricule", "texte"),
        ("nom_agent", "Nom de l'agent", "texte"),
        ("num_versement", "N° versement", "texte"),
        ("code_banque", "Code banque", "texte"),
        ("nom_banque", "Banque du dépôt", "texte"),
        ("compte", "Compte", "texte"),
        ("statut", "Statut", "texte"),
        ("montant", "Montant", "montant"),
    ],
    "cheques": [
        ("date_comptable", "Date comptable", "date"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("num_caisse", "Caisse", "entier"),
        ("matricule", "Matricule", "texte"),
        ("num_recu", "N° reçu", "texte"),
        ("reference", "Référence", "texte"),
        ("mode_paiement", "Mode", "texte"),
        ("num_cheque", "N° chèque", "texte"),
        ("tireur", "Tireur", "texte"),
        ("code_banque_cheque", "Code banque du chèque", "texte"),
        ("nom_banque_cheque", "Banque du chèque", "texte"),
        ("date_depot", "Date du dépôt", "date"),
        ("code_banque_depot", "Code banque du dépôt", "texte"),
        ("nom_banque_depot", "Banque du dépôt", "texte"),
        ("compte_depot", "Compte du dépôt", "texte"),
        ("num_bordereau", "N° bordereau", "texte"),
        ("montant", "Montant", "montant"),
    ],
    "reports": [
        ("jour", "Jour", "date"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("num_caisse", "Caisse", "entier"),
        ("constat", "Constat", "texte"),
        ("report", "Report", "montant"),
        ("especes", "Espèces", "montant"),
        ("cheques", "Chèques", "montant"),
        ("virements", "Virements", "montant"),
        ("verse_especes", "Versé espèces", "montant"),
        ("verse_cheques", "Versé chèques", "montant"),
        ("depenses", "Dépenses", "montant"),
        ("solde", "Solde non versé", "montant"),
        ("jour_precedent", "Jour précédent", "date"),
        ("solde_veille", "Solde précédent", "montant"),
        ("rupture", "Rupture de report", "montant"),
    ],
    "non_valides": [
        ("date_recu", "Date", "date"),
        ("heure", "Heure", "texte"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("num_recu", "N° reçu", "texte"),
        ("abonne", "Abonné", "texte"),
        ("client", "Client", "texte"),
        ("montant", "Payé par le client", "montant"),
        ("credite", "Crédité au compte client", "montant"),
        ("matricule", "Agent qui a saisi le reçu", "texte"),
        ("especes_validees", "Espèces validées de la caisse", "montant"),
        ("verse_especes", "Versé à la banque ce jour", "montant"),
        ("constat", "Constat", "texte"),
    ],
    "annulations": [
        ("date_recu", "Date du reçu", "date"),
        ("heure", "Heure", "texte"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("num_caisse", "Caisse", "entier"),
        ("matricule", "Matricule", "texte"),
        ("num_recu", "N° reçu", "texte"),
        ("reference", "Référence", "texte"),
        ("mode_paiement", "Mode de paiement", "texte"),
        ("montant", "Montant annulé", "montant"),
    ],
    "rapprochement": [
        ("code_banque", "Code banque", "texte"),
        ("nom_banque", "Banque", "texte"),
        ("compte", "Compte", "texte"),
        ("date_operation", "Date de l'opération", "date"),
        ("type_operation", "Type d'opération", "texte"),
        ("statut", "Statut", "texte"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("num_caisse", "Caisse", "entier"),
        ("matricule", "Matricule", "texte"),
        ("num_piece", "N° versement / reçu", "texte"),
        ("num_cheque", "N° chèque", "texte"),
        ("tireur", "Tireur", "texte"),
        ("reference", "Référence", "texte"),
        ("montant", "Montant", "montant"),
    ],
    "banques": [
        ("code_banque", "Code banque", "texte"),
        ("nom_banque", "Banque", "texte"),
        ("compte", "Compte", "texte"),
        ("date_operation", "Date", "date"),
        ("nb_operations", "Nb opérations", "entier"),
        ("especes", "Versements espèces", "montant"),
        ("cheques", "Remises chèques", "montant"),
        ("total", "Total", "montant"),
        ("pct_total", "% du total", "pct"),
    ],
    "comparaison": [
        ("banque_etat", "Banque (état)", "texte"),
        ("compte_etat", "Compte (état)", "texte"),
        ("mois_etat", "État au", "texte"),
        ("cote_libelle", "Côté", "texte"),
        ("mois_origine", "Mois d'origine", "texte"),
        ("date", "Date", "date"),
        ("piece", "Pièce", "texte"),
        ("libelle", "Libellé du fichier", "texte"),
        ("type_libelle", "Type", "texte"),
        ("num_cheque", "N° chèque", "texte"),
        ("tiers", "Déposant / tireur", "texte"),
        ("montant", "Montant", "montant"),
        ("resultat", "Résultat", "texte"),
        ("lecture", "Lecture", "texte"),
        ("crm_date", "Date CRM", "date"),
        ("ecart_jours", "Écart (jours)", "entier"),
        ("crm_centre", "Centre CRM", "texte"),
        ("crm_nom_centre", "Nom du centre", "texte"),
        ("crm_matricule", "Matricule", "texte"),
        ("crm_num_piece", "N° versement / reçu", "texte"),
        ("crm_banque", "Banque CRM", "texte"),
        ("crm_compte", "Compte CRM", "texte"),
        ("crm_statut", "Statut CRM", "texte"),
        ("crm_montant", "Montant CRM", "montant"),
    ],
    "releve_synthese": [
        ("libelle", "Résultat", "texte"),
        ("nb", "Nb versements", "entier"),
        ("pct_nb", "% des versements", "pct"),
        ("montant", "Montant", "montant"),
        ("pct_montant", "% du montant", "pct"),
    ],
    "releve_absents": [
        ("date_versement", "Date CRM", "date"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("matricule", "Matricule", "texte"),
        ("num_versement", "N° versement", "texte"),
        ("code_banque", "Code banque", "texte"),
        ("nom_banque", "Banque déclarée", "texte"),
        ("montant", "Montant CRM", "montant"),
    ],
    "releve_banques": [
        ("banque", "Banque", "texte"),
        ("nb_crm", "Versements CRM", "entier"),
        ("montant_crm", "Montant CRM", "montant"),
        ("nb_trouves", "Retrouvés", "entier"),
        ("pct_trouves", "% retrouvés", "pct"),
        ("nb_a_verifier", "Dont à vérifier", "entier"),
        ("nb_non_retrouves", "Non retrouvés", "entier"),
        ("pct_non_retrouves", "% non retrouvés", "pct"),
        ("montant_non_retrouve", "Montant non retrouvé", "montant"),
        ("nb_releve", "Dépôts du relevé", "entier"),
        ("montant_releve", "Montant du relevé", "montant"),
        ("nb_sans_crm", "Dépôts sans versement CRM", "entier"),
        ("montant_sans_crm", "Montant sans versement CRM", "montant"),
    ],
    "releve_centres": [
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("nb", "Versements CRM", "entier"),
        ("montant", "Montant CRM", "montant"),
        ("nb_trouves", "Retrouvés", "entier"),
        ("nb_a_verifier", "Dont à vérifier", "entier"),
        ("nb_non_retrouves", "Non retrouvés", "entier"),
        ("pct_non_retrouves", "% non retrouvés", "pct"),
        ("montant_non_retrouve", "Montant non retrouvé", "montant"),
        ("pct_montant_non_retrouve", "% du montant", "pct"),
    ],
    "releve_lignes": [
        ("date_versement", "Date CRM", "date"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("matricule", "Matricule", "texte"),
        ("num_versement", "N° versement", "texte"),
        ("code_banque", "Code banque", "texte"),
        ("nom_banque", "Banque déclarée", "texte"),
        ("montant", "Montant CRM", "montant"),
        ("resultat", "Résultat", "texte"),
        ("date_banque", "Date banque", "date"),
        ("ecart_jours", "Écart (jours)", "entier"),
        ("montant_banque", "Montant banque", "montant"),
        ("ecart_montant", "Écart de montant", "montant"),
        ("banque_releve", "Banque du relevé", "texte"),
        ("libelle_releve", "Libellé du relevé", "texte"),
    ],
    "dfc_mois": [
        ("mois_libelle", "Mois", "texte"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("resultat", "Résultat", "texte"),
        ("nb_jours_dfc", "Jours saisis DFC", "entier"),
        ("nb_jours_crm", "Jours au CRM", "entier"),
        ("encaisse_dfc", "Encaissé DFC", "montant"),
        ("encaisse_crm", "Encaissé CRM", "montant"),
        ("ecart_encaisse", "Écart encaissé (CRM − DFC)", "montant"),
        ("verse_dfc", "Versé DFC", "montant"),
        ("verse_crm", "Versé CRM", "montant"),
        ("ecart_verse", "Écart versé (CRM − DFC)", "montant"),
        ("nb_jours_non_saisis", "Jours non saisis", "entier"),
        ("nb_jours_ecart", "Jours avec écart", "entier"),
    ],
    "dfc_lignes": [
        ("jour", "Jour", "date"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("resultat", "Résultat", "texte"),
        ("encaisse_dfc", "Encaissé DFC", "montant"),
        ("encaisse_crm", "Encaissé CRM", "montant"),
        ("ecart_encaisse", "Écart encaissé (CRM − DFC)", "montant"),
        ("verse_dfc", "Versé DFC", "montant"),
        ("verse_crm", "Versé CRM", "montant"),
        ("ecart_verse", "Écart versé (CRM − DFC)", "montant"),
        ("banque_dfc", "Banque DFC", "texte"),
        ("banque_crm", "Banque CRM", "texte"),
        ("depenses_dfc", "Dépenses DFC", "montant"),
        ("depenses_crm", "Dépenses CRM", "montant"),
        ("anomalies", "Détail", "texte"),
        ("observation", "Observation de la DFC", "texte"),
    ],
    "manuels": [
        ("resultat", "Résultat", "texte"),
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("date_recu", "Date du reçu manuel", "date"),
        ("num_recu_manuel", "N° reçu manuel", "texte"),
        ("reference", "Référence abonné", "texte"),
        ("montant", "Montant manuel", "montant"),
        ("num_recu_crm", "N° reçu CRM", "texte"),
        ("date_crm", "Date CRM", "date"),
        ("delai_jours", "Délai de saisie (jours)", "entier"),
        ("montant_crm", "Montant CRM retenu", "montant"),
        ("ecart_montant", "Écart (CRM − manuel)", "montant"),
        ("total_crm", "Total enregistré CRM pour la référence", "montant"),
        ("nb_transactions_crm", "Nb transactions CRM", "entier"),
        ("centre_saisie_crm", "Centre de saisie CRM", "texte"),
        ("statut_crm", "Statut CRM", "texte"),
        ("matricule", "Matricule", "texte"),
        ("nom_agent", "Nom de l'agent", "texte"),
        ("anomalie_reference", "Anomalie de référence", "texte"),
        ("nom_fichier", "Fichier", "texte"),
    ],
    "manuels_references": [
        ("source", "Source", "texte"),
        ("centre", "Centre / Caisse", "texte"),
        ("date", "Date", "date"),
        ("reference", "Référence saisie", "texte"),
        ("num_recu", "N° reçu", "texte"),
        ("montant", "Montant", "montant"),
        ("anomalie", "Anomalie", "texte"),
    ],
    "evolution": [
        ("centre", "Centre", "texte"),
        ("nom_centre", "Nom du centre", "texte"),
        ("resultat", "Résultat", "texte"),
        ("encaisse", "Encaissé du mois", "montant"),
        ("encaisse_precedent", "Encaissé mois précédent", "montant"),
        ("ecart", "Écart", "montant"),
        ("variation_pct", "Variation (%)", "pct"),
        ("moyenne_3_mois", "Moyenne 3 mois précédents", "montant"),
        ("variation_moyenne_pct", "Variation vs moyenne (%)", "pct"),
        ("part_pct", "Part du total (%)", "pct"),
        ("part_precedente_pct", "Part mois précédent (%)", "pct"),
        ("nb_recus", "Reçus du mois", "entier"),
        ("nb_recus_precedent", "Reçus mois précédent", "entier"),
    ],
    "releve_sans_crm": [
        ("banque", "Banque", "texte"),
        ("date_operation", "Date", "date"),
        ("libelle", "Libellé du relevé", "texte"),
        ("montant", "Montant", "montant"),
    ],
    "releve_annules": [
        ("banque", "Banque", "texte"),
        ("date_operation", "Date", "date"),
        ("libelle", "Libellé du relevé", "texte"),
        ("montant", "Montant retiré", "montant"),
    ],
    "comparaison_synthese": [
        ("libelle", "Résultat", "texte"),
        ("nb", "Nb lignes", "entier"),
        ("pct_nb", "% des lignes", "pct"),
        ("montant", "Montant", "montant"),
        ("pct_montant", "% du montant", "pct"),
    ],
}

TITRES = {
    "controle": "Contrôle par centre",
    "encaissements": "Encaissements",
    "versements": "Versements espèces",
    "cheques": "Dépôts chèques",
    "reports": "Reports",
    "annulations": "Annulations",
    "non_valides": "Reçus non validés",
    "rapprochement": "Rapprochement",
    "banques": "Synthèse banques",
    "manuels": "Vérification reçus",
    "evolution": "Comparaison",
    "evolution_historique": "Historique 12 mois",
    "manuels_references": "Références mal saisies",
    "dfc_mois": "Par mois",
    "dfc_lignes": "Par jour",
    "releve_synthese": "Synthèse",
    "releve_absents": "Absents du relevé",
    "releve_banques": "Par banque",
    "releve_centres": "Par centre",
    "releve_lignes": "Versements CRM",
    "releve_sans_crm": "Dépôts sans versement CRM",
    "releve_annules": "Versements annulés (banque)",
    "comparaison": "Comparaison",
    "comparaison_synthese": "Synthèse",
}

# Feuilles de l'export complet, dans cet ordre.
FEUILLES_COMPLET = ("encaissements", "versements", "cheques", "reports", "annulations")

# Colonnes additionnées dans la ligne TOTAL (les soldes et reports sont des
# stocks, donc jamais additionnés).
_SANS_TOTAL = {"report", "solde", "solde_veille", "rupture", "rupture_report", "pct_total", "crm_montant",
               "montant_banque"}

# Colonnes sans total dans une feuille donnée seulement (montants de contexte
# répétés sur chaque ligne).
_SANS_TOTAL_FEUILLE = {"non_valides": {"especes_validees", "verse_especes"}}

# Feuilles dont la ligne de total ne compte que certaines lignes :
# feuille -> (colonne du critère, valeur retenue, libellé de la ligne).
_TOTAL_SI = {"encaissements": ("statut", "VALIDE", "TOTAL validés")}

# Tout le classeur est en nuances de gris clair : en-tête un peu plus soutenu,
# lignes alternées en deux gris très clairs, total comme l'en-tête.
_FOND_ENTETE = PatternFill("solid", fgColor="D0D4D9")
_POLICE_ENTETE = Font(bold=True, color="22262A")
_BORD_ENTETE = Border(bottom=Side(style="thin", color="8A9099"))
_FOND_TOTAL = PatternFill("solid", fgColor="D0D4D9")
_BORD_TOTAL = Border(top=Side(style="thin", color="8A9099"))
_GRIS_LIGNE_PAIRE = "F6F7F8"
_GRIS_LIGNE_IMPAIRE = "E9EBEE"
_FORMAT_MONTANT = "#,##0.00"
_FORMAT_PCT = '0.0"%"'


def _en_date(valeur):
    if isinstance(valeur, str) and len(valeur) >= 10:
        try:
            return date.fromisoformat(valeur[:10])
        except ValueError:
            return valeur
    return valeur


def _lignes_grises(ws, plage: str):
    """Alterne deux gris clairs sur les lignes de la plage. La règle suit le
    numéro de ligne : l'alternance reste régulière après un tri."""
    for formule, couleur in (("MOD(ROW(),2)=0", _GRIS_LIGNE_PAIRE), ("MOD(ROW(),2)=1", _GRIS_LIGNE_IMPAIRE)):
        ws.conditional_formatting.add(plage, FormulaRule(
            formula=[formule],
            fill=PatternFill(start_color=couleur, end_color=couleur, fill_type="solid"),
        ))


def _feuille(ws, nom: str, lignes: list[dict]):
    colonnes = COLONNES[nom]
    ws.append([libelle for _, libelle, _ in colonnes])
    for cellule in ws[1]:
        cellule.fill = _FOND_ENTETE
        cellule.font = _POLICE_ENTETE
        cellule.border = _BORD_ENTETE
        cellule.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30

    for ligne in lignes:
        valeurs = []
        for cle, _, genre in colonnes:
            v = ligne.get(cle)
            if genre == "date":
                v = _en_date(v)
            valeurs.append(v)
        ws.append(valeurs)

    derniere = ws.max_row
    for i, (cle, libelle, genre) in enumerate(colonnes, start=1):
        lettre = get_column_letter(i)
        if genre in ("montant", "pct", "date") and derniere >= 2:
            fmt = {"montant": _FORMAT_MONTANT, "pct": _FORMAT_PCT, "date": "DD/MM/YYYY"}[genre]
            for cellule in ws[lettre][1:]:
                cellule.number_format = fmt
        largeur = len(libelle)
        for ligne in lignes[:500]:
            v = ligne.get(cle)
            largeur = max(largeur, len(f"{v:,.2f}") if isinstance(v, float) else len(str(v or "")))
        ws.column_dimensions[lettre].width = min(max(largeur + 3, 10), 48)

    ws.freeze_panes = "A2"
    if derniere >= 2:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(colonnes))}{derniere}"
        _lignes_grises(ws, f"A2:{get_column_letter(len(colonnes))}{derniere}")
        # Ligne TOTAL sous les données, en dehors du filtre.
        total = []
        a_un_total = False
        critere = _TOTAL_SI.get(nom)
        lettre_critere = None
        if critere:
            lettre_critere = get_column_letter([c for c, _, _ in colonnes].index(critere[0]) + 1)
        for i, (cle, _, genre) in enumerate(colonnes, start=1):
            if (genre == "montant" and cle not in _SANS_TOTAL
                    and cle not in _SANS_TOTAL_FEUILLE.get(nom, ())):
                # Le total est écrit comme un nombre et non comme une formule :
                # un fichier écrit par openpyxl n'a pas de résultat de formule
                # enregistré, et les lecteurs de téléphone affichent alors 0.
                somme = 0.0
                for ligne in lignes:
                    if critere and ligne.get(critere[0]) != critere[1]:
                        continue
                    v = ligne.get(cle)
                    if isinstance(v, (int, float)):
                        somme += v
                total.append(round(somme, 2))
                a_un_total = True
            else:
                total.append(None)
        if a_un_total:
            total[0] = critere[2] if critere else "TOTAL"
            ws.append(total)
            for i, cellule in enumerate(ws[derniere + 1], start=1):
                cellule.font = Font(bold=True)
                cellule.fill = _FOND_TOTAL
                cellule.border = _BORD_TOTAL
                if colonnes[i - 1][2] == "montant":
                    cellule.number_format = _FORMAT_MONTANT


def classeur(jeux: dict[str, list[dict]]) -> bytes:
    """Construit un classeur Excel : une feuille par jeu de données."""
    wb = Workbook()
    wb.remove(wb.active)
    for nom, lignes in jeux.items():
        ws = wb.create_sheet(TITRES[nom][:31])
        _feuille(ws, nom, lignes)
    tampon = io.BytesIO()
    wb.save(tampon)
    return tampon.getvalue()


def classeur_evolution(lignes: list[dict], historique: list[dict], colonnes_historique: list[tuple],
                       libelle: str, libelle_precedent: str) -> bytes:
    """Comparaison mensuelle par centre et historique des douze mois (une
    colonne par mois, construite à la demande)."""
    colonnes = [(c, l.replace("du mois", libelle).replace("mois précédent", libelle_precedent), g)
                for c, l, g in COLONNES["evolution"]]
    anciennes = COLONNES["evolution"], COLONNES.get("evolution_historique")
    COLONNES["evolution"], COLONNES["evolution_historique"] = colonnes, colonnes_historique
    try:
        return classeur({"evolution": lignes, "evolution_historique": historique})
    finally:
        COLONNES["evolution"] = anciennes[0]
        if anciennes[1] is None:
            COLONNES.pop("evolution_historique", None)


def classeur_dynamique(nom: str, titre: str, colonnes: list[tuple], lignes: list[dict]) -> bytes:
    """Classeur d'une feuille dont les colonnes sont construites à la demande."""
    COLONNES[nom], TITRES[nom] = colonnes, titre
    try:
        return classeur({nom: lignes})
    finally:
        COLONNES.pop(nom, None)
        TITRES.pop(nom, None)


def fichier_csv(nom: str, lignes: list[dict]) -> bytes:
    """CSV pour Excel en français : séparateur « ; », virgule décimale, UTF-8 avec BOM."""
    colonnes = COLONNES[nom]
    tampon = io.StringIO()
    ecrivain = csv.writer(tampon, delimiter=";", lineterminator="\r\n")
    ecrivain.writerow([libelle for _, libelle, _ in colonnes])
    for ligne in lignes:
        sortie = []
        for cle, _, genre in colonnes:
            v = ligne.get(cle)
            if v is None:
                sortie.append("")
            elif genre in ("montant", "pct") and isinstance(v, (int, float)):
                sortie.append(f"{v:.3f}".rstrip("0").rstrip(".").replace(".", ","))
            elif genre == "date" and isinstance(v, str) and len(v) >= 10:
                sortie.append(f"{v[8:10]}/{v[5:7]}/{v[0:4]}")
            elif genre == "texte" and isinstance(v, str) and v.isdigit() and len(v) > 11:
                # Empêche Excel de transformer un long numéro en notation scientifique.
                sortie.append(f'="{v}"')
            else:
                sortie.append(v)
        ecrivain.writerow(sortie)
    return tampon.getvalue().encode("utf-8-sig")


# --------------------------------------------------------------------------- format du rapprochement
_ENTETES_RAPPROCHEMENT = ["Date", "ARTICLE", "Débit", "Crédit", "PIECE",
                          "Centre", "Nom du centre", "Matricule", "N° chèque", "Statut"]
_LARGEURS_RAPPROCHEMENT = [12, 62, 16, 16, 18, 9, 28, 12, 14, 14]


def _article(op: dict) -> str:
    """Libellé rédigé comme sur le relevé de la banque."""
    agence = f" AG {op['nom_centre']}" if op.get("nom_centre") else ""
    if op["type_operation"] == "VERSEMENT ESPECES":
        return f"Versement effectué par {op.get('matricule') or '?'}{agence}"
    tireur = f" {op['tireur']}" if op.get("tireur") else ""
    numero = f" CH N° {op['num_cheque']}" if op.get("num_cheque") else " CH"
    return f"Remise ordre{tireur}{numero}{agence}"


def _nom_feuille(code, compte, deja: set) -> str:
    base = f"{code or 'SANS BANQUE'} {str(compte or '')[-12:]}".strip()
    for interdit in '[]:*?/\\':
        base = base.replace(interdit, " ")
    nom = base[:31] or "Banque"
    n = 2
    while nom in deja:
        nom = f"{base[:27]} ({n})"
        n += 1
    deja.add(nom)
    return nom


def classeur_format_rapprochement(operations: list[dict], debut: date, fin: date) -> bytes:
    """Une feuille par compte bancaire, présentée comme l'état de rapprochement :
    titre, numéro de compte, puis Date | ARTICLE | Débit | Crédit | PIECE, par mois."""
    wb = Workbook()
    wb.remove(wb.active)
    groupes: dict[tuple, list[dict]] = {}
    for op in operations:
        if op.get("statut") == "ANNULE":
            continue
        groupes.setdefault((op.get("code_banque"), op.get("nom_banque"), op.get("compte")), []).append(op)

    periode = f"du {debut:%d/%m/%Y} au {fin:%d/%m/%Y}"
    deja: set = set()
    if not groupes:
        groupes[(None, None, None)] = []

    for (code, nom, compte), ops in sorted(groupes.items(), key=lambda g: (str(g[0][0] or "~"), str(g[0][2] or ""))):
        ws = wb.create_sheet(_nom_feuille(code, compte, deja))
        nb_cols = len(_ENTETES_RAPPROCHEMENT)

        ws.append([f"ETAT DES VERSEMENTS DU CRM — {nom or 'Banque non renseignée'} — {periode}"])
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=nb_cols)
        ws.append([None, f"N° Cmpt Bancaire: {compte or 'non renseigné'}", None, None, None,
                   f"Code banque: {code or '-'}"])
        ws.append(_ENTETES_RAPPROCHEMENT)
        for ligne in (1, 3):
            for cellule in ws[ligne]:
                cellule.fill = _FOND_ENTETE
                cellule.font = _POLICE_ENTETE
                cellule.alignment = Alignment(vertical="center")
        ws.row_dimensions[1].height = 24
        for cellule in ws[2]:
            cellule.font = Font(bold=True)

        mois_courant = None
        premiere = ws.max_row + 1
        for op in sorted(ops, key=lambda o: (str(o.get("date_operation") or ""), str(o.get("num_piece") or ""))):
            jour = _en_date(op.get("date_operation"))
            mois = f"{jour.month}/{jour.year}" if isinstance(jour, date) else "Sans date"
            if mois != mois_courant:
                mois_courant = mois
                ws.append([None, mois])
                ws.cell(ws.max_row, 2).font = Font(bold=True)
            ws.append([jour, _article(op), op.get("montant"), None, op.get("num_piece"),
                       op.get("centre"), op.get("nom_centre"), op.get("matricule"),
                       op.get("num_cheque"), op.get("statut")])
            ws.cell(ws.max_row, 1).number_format = "DD/MM/YYYY"
            ws.cell(ws.max_row, 3).number_format = _FORMAT_MONTANT
        derniere = ws.max_row
        if derniere >= premiere:
            _lignes_grises(ws, f"A{premiere}:{get_column_letter(nb_cols)}{derniere}")

        # Totaux écrits comme des nombres (lisibles aussi sur téléphone).
        ws.append([None, "TOTAUX",
                   round(sum(o.get("montant") or 0 for o in ops if isinstance(o.get("montant"), (int, float))), 2),
                   0])
        for cellule in ws[ws.max_row]:
            cellule.font = Font(bold=True)
            cellule.fill = _FOND_TOTAL
            cellule.border = _BORD_TOTAL
        ws.cell(ws.max_row, 3).number_format = _FORMAT_MONTANT
        ws.cell(ws.max_row, 4).number_format = _FORMAT_MONTANT

        for i, largeur in enumerate(_LARGEURS_RAPPROCHEMENT, start=1):
            ws.column_dimensions[get_column_letter(i)].width = largeur
        ws.freeze_panes = "A4"

    tampon = io.BytesIO()
    wb.save(tampon)
    return tampon.getvalue()


# --------------------------------------------------------------------------- rapport des chutes (beige)
_BEIGE_ENTETE = GradientFill(type="linear", degree=90, stop=("F6EBD3", "E3CDA0"))
_BEIGE_TOTAL = GradientFill(type="linear", degree=90, stop=("EFE0C0", "DDC48F"))
_BEIGE_PAIRE = "FBF6EC"
_BEIGE_IMPAIRE = "F4EAD6"
_BRUN_TEXTE = "4A3B22"
_BRUN_BORD = Side(style="thin", color="C9B183")
_BRUN_FIN = Side(style="hair", color="E2D3B3")


def classeur_beige(titre: str, colonnes: list[tuple], lignes: list[dict]) -> bytes:
    """Une feuille présentée comme chutes_secteur_AAAA-MM-JJ.xlsx, en beige :
    en-tête et ligne TOTAL en dégradé beige, lignes alternées beige clair,
    écarts et variations négatifs en rouge."""
    wb = Workbook()
    ws = wb.active
    ws.title = titre[:31]
    ws.append([libelle for _, libelle, _ in colonnes])
    for cellule in ws[1]:
        cellule.fill = _BEIGE_ENTETE
        cellule.font = Font(bold=True, color=_BRUN_TEXTE)
        cellule.border = Border(top=_BRUN_BORD, bottom=_BRUN_BORD, left=_BRUN_FIN, right=_BRUN_FIN)
        cellule.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30

    for ligne in lignes:
        ws.append([_en_date(ligne.get(c)) if g == "date" else ligne.get(c) for c, _, g in colonnes])

    derniere = ws.max_row
    rouge = Font(color="B3261E")
    for i, (cle, libelle, genre) in enumerate(colonnes, start=1):
        lettre = get_column_letter(i)
        fmt = {"montant": _FORMAT_MONTANT, "pct": _FORMAT_PCT, "date": "DD/MM/YYYY", "entier": "#,##0"}.get(genre)
        for cellule in ws[lettre][1:]:
            if fmt:
                cellule.number_format = fmt
            cellule.border = Border(bottom=_BRUN_FIN)
            if genre in ("montant", "pct") and isinstance(cellule.value, (int, float)) and cellule.value < 0 \
                    and cle in ("ecart", "variation_pct"):
                cellule.font = rouge
        largeur = len(libelle)
        for ligne in lignes[:500]:
            v = ligne.get(cle)
            largeur = max(largeur, len(f"{v:,.2f}") if isinstance(v, float) else len(str(v or "")))
        ws.column_dimensions[lettre].width = min(max(largeur + 3, 10), 40)

    ws.freeze_panes = "A2"
    if derniere >= 2:
        plage = f"A2:{get_column_letter(len(colonnes))}{derniere}"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(colonnes))}{derniere}"
        for formule, couleur in (("MOD(ROW(),2)=0", _BEIGE_PAIRE), ("MOD(ROW(),2)=1", _BEIGE_IMPAIRE)):
            ws.conditional_formatting.add(plage, FormulaRule(
                formula=[formule], fill=PatternFill(start_color=couleur, end_color=couleur, fill_type="solid")))
        total = []
        for cle, _, genre in colonnes:
            if genre == "montant":
                total.append(round(sum(l.get(cle) or 0 for l in lignes if isinstance(l.get(cle), (int, float))), 2))
            else:
                total.append(None)
        total[0] = "TOTAL"
        ws.append(total)
        for i, cellule in enumerate(ws[derniere + 1], start=1):
            cellule.fill = _BEIGE_TOTAL
            cellule.font = Font(bold=True, color=_BRUN_TEXTE)
            cellule.border = Border(top=_BRUN_BORD, bottom=_BRUN_BORD)
            if colonnes[i - 1][2] == "montant":
                cellule.number_format = _FORMAT_MONTANT
    # Impression : paysage, toutes les colonnes sur la largeur d'une page.
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    tampon = io.BytesIO()
    wb.save(tampon)
    return tampon.getvalue()
