"""Requêtes Oracle du contrôle des versements.

Règles fixées et vérifiées sur la base CRM_SNDE :

- Contrôle (à verser, solde) : reçus de caisse de T_PIECE_REC, validés
  (PREC_VLD = 1) et non annulés (FLAGVLDANNUL différent de 1), hors e-paiements
  (MP_ID = 99), rattachés à la caisse qui a encaissé (pr.STR_ID) à la date
  comptable (PREC_DATE_COMPTABLE).
- Liste des encaissements (sql_encaissements) : même règle que l'état des
  encaissements du CRM. Tous les modes de paiement, e-paiements compris, chaque
  reçu étant rattaché au centre de l'abonné. Date comptable pour les caisses,
  date du reçu (PREC_DATE) pour les e-paiements.
  Statut d'un reçu : PREC_VLD = 1 validé, -1 annulé, 0 non validé. L'état du
  CRM ne compte que les validés ; les autres sont listés avec leur statut.
- Référence : ABN_ID de T_PIECE_DETAIL, sinon GRP_CODE de S_GROUPEMENT.
- Versements d'espèces : S_VERS_ESPECE (banque et compte du dépôt).
  Validé = VERS_FLAG_VLD = 1 et FLAG_ANNUL à 0 ou NULL.
- Dépôts de chèques : sur le reçu lui-même (VERS_BQ_ID, VERS_COMPTE, VERS_DATE_BORD).
- Report et solde : S_RECAP_CAISSE.
  solde = report + espèces + chèques - versé espèces - versé chèques - dépenses.
- Annulations : T_PIECE_REC avec PREC_VLD = -1.
- Reçus non validés (sql_non_valides) : T_PIECE_REC avec PREC_VLD = 0. Le CRM
  les affiche « Non Validé » dans l'état global des transactions. Ils sont
  saisis par un agent qui n'est pas le caissier du jour ; le compte du client
  (S_PORTEFEUILLE) est crédité, mais le récapitulatif de la caisse les ignore.

Paramètres : :d1 (début inclus), :d2 (fin exclue), :d0 (début de la fenêtre
d'historique pour retrouver le solde de la veille), :centre (optionnel).
"""

_RECU_VALIDE = """pr.PREC_VLD = 1
      AND (pr.FLAGVLDANNUL IS NULL OR pr.FLAGVLDANNUL != 1)
      AND (pr.MP_ID IS NULL OR pr.MP_ID != 99)"""

# Référence de chaque reçu, lue dans T_PIECE_DETAIL par une recherche directe
# sur PREC_ID (une recherche par reçu). NO_UNNEST empêche Oracle de transformer
# cette recherche en lecture complète de T_PIECE_DETAIL, qui est très grande.
_REF_DETAIL = """(SELECT /*+ NO_UNNEST */ MIN(pd.ABN_ID)
              FROM {S}.T_PIECE_DETAIL pd WHERE pd.PREC_ID = pr.PREC_ID) AS abn_id,
           (SELECT /*+ NO_UNNEST */ MIN(pd.GRP_ID)
              FROM {S}.T_PIECE_DETAIL pd WHERE pd.PREC_ID = pr.PREC_ID) AS grp_id,
           (SELECT /*+ NO_UNNEST */ MIN(pd.DMD_ID)
              FROM {S}.T_PIECE_DETAIL pd WHERE pd.PREC_ID = pr.PREC_ID) AS dmd_id"""

_RECAP_LAG = """recap_brut AS (
    SELECT r.STR_ID,
           r.REC_NUM_CAISSE,
           TRUNC(r.REC_DATE)          AS jour,
           NVL(r.REC_MNT_REPORT, 0)   AS report,
           NVL(r.REC_MNT_TOT_ESP, 0)  AS tot_esp,
           NVL(r.REC_MNT_TOT_CHQ, 0)  AS tot_chq,
           NVL(r.REC_MNT_TOT_TPE, 0)  AS tot_vir,
           NVL(r.REC_MNT_VERS_ESP, 0) AS vers_esp,
           NVL(r.REC_MNT_VERS_CHQ, 0) AS vers_chq,
           NVL(r.TOTALDEPENSE, 0)     AS depenses
    FROM {S}.S_RECAP_CAISSE r
    WHERE r.REC_DATE >= :d0
      AND r.REC_DATE <  :d2
      {F}
),
recap_solde AS (
    SELECT b.*,
           (b.report + b.tot_esp + b.tot_chq - b.vers_esp - b.vers_chq - b.depenses) AS solde
    FROM recap_brut b
),
recap_lag AS (
    SELECT c.*,
           LAG(c.solde) OVER (PARTITION BY c.STR_ID, c.REC_NUM_CAISSE ORDER BY c.jour) AS solde_veille,
           LAG(c.jour)  OVER (PARTITION BY c.STR_ID, c.REC_NUM_CAISSE ORDER BY c.jour) AS jour_precedent
    FROM recap_solde c
)"""


def _filtre_centre(avec_centre: bool) -> str:
    return "AND TRIM(s.STR_CODE) = :centre" if avec_centre else ""


def _filtre_str(schema: str, alias: str, avec_centre: bool) -> str:
    """Filtre sur le centre appliqué dès la lecture de la table, pour qu'Oracle
    ne traite que les lignes du centre demandé."""
    if not avec_centre:
        return ""
    return (f"AND {alias}.STR_ID IN (SELECT s0.STR_ID FROM {schema}.S_STRUCTURE s0 "
            "WHERE TRIM(s0.STR_CODE) = :centre)")


def sql_centres(schema: str) -> str:
    return f"""
SELECT s.STR_CODE   AS centre,
       s.STR_LIB_LT AS nom_centre
FROM {schema}.S_STRUCTURE s
WHERE s.STR_ID IN (
        SELECT r.STR_ID
        FROM {schema}.S_RECAP_CAISSE r
        WHERE r.REC_DATE >= :d0
      )
"""


def sql_personnel(schema: str) -> str:
    """Matricule et nom des agents, pour afficher le nom à côté du matricule."""
    return f"""
SELECT TRIM(p.MATRICULE)                                    AS matricule,
       TRIM(p.PERS_NOM || ' ' || NVL(p.PERS_PRENOM, ''))    AS nom
FROM {schema}.S_PERSONNEL p
WHERE p.MATRICULE IS NOT NULL
"""


def sql_tarifs(schema: str) -> str:
    """Tarif, secteur et statut de chaque abonnement.

    Tarif : priorité à S_TARIF_PR.TARP_CODE (lié par PFACT_ID) ; si PFACT_ID
    est NULL, on prend ACT_USAGE.TARIFA (lié par ACTUSAGE_ID).
    Secteur : S_SECTEUR.SECT_CODE (lié par SECT_ID).
    Statut : Résilié si ABN_RES = 1, Suspendu si ABN_RES = 0 et FLAG_SUS = 1,
    sinon Actif.
    Un seul tarif par PFACT_ID : la jointure ne dédouble jamais un abonnement."""
    return f"""
SELECT TO_CHAR(a.ABN_ID)  AS abn_id,
       g.GRP_CODE         AS grp_code,
       COALESCE(t.TARP_CODE, u.TARIFA) AS tarif,
       sec.SECT_CODE       AS secteur,
       TO_CHAR(a.SECT_ID)  AS sect_id,
       CASE WHEN a.ABN_RES = 1                    THEN 'Résilié'
            WHEN NVL(a.ABN_RES, 0) = 0 AND a.FLAG_SUS = 1 THEN 'Suspendu'
            ELSE 'Actif' END AS statut
FROM {schema}.S_ABONNEMENT a
LEFT JOIN (SELECT PFACT_ID, MIN(TARP_CODE) AS TARP_CODE
           FROM {schema}.S_TARIF_PR GROUP BY PFACT_ID) t ON t.PFACT_ID = a.PFACT_ID
LEFT JOIN {schema}.ACT_USAGE u   ON u.ACTUSAGE_ID = a.ACTUSAGE_ID
LEFT JOIN {schema}.S_SECTEUR sec ON sec.SECT_ID = a.SECT_ID
LEFT JOIN {schema}.S_GROUPEMENT g ON g.GRP_ID = a.GRP_ID
"""


def sql_controle(schema: str, avec_centre: bool) -> str:
    """Une ligne par centre et par jour : report, encaissé, versé, solde."""
    return f"""
WITH {_RECAP_LAG.format(S=schema, F=_filtre_str(schema, "r", avec_centre))},
recap AS (
    SELECT STR_ID,
           jour,
           COUNT(*)      AS nb_caisses,
           SUM(report)   AS report,
           SUM(tot_esp)  AS recap_especes,
           SUM(tot_chq)  AS recap_cheques,
           SUM(tot_vir)  AS recap_virements,
           SUM(vers_esp) AS recap_vers_especes,
           SUM(vers_chq) AS recap_vers_cheques,
           SUM(depenses) AS depenses,
           SUM(solde)    AS solde,
           SUM(CASE WHEN solde_veille IS NOT NULL THEN report - solde_veille ELSE 0 END) AS rupture_report
    FROM recap_lag
    WHERE jour >= :d1
    GROUP BY STR_ID, jour
),
enc AS (
    SELECT pr.STR_ID,
           TRUNC(pr.PREC_DATE_COMPTABLE) AS jour,
           COUNT(*) AS nb_recus,
           SUM(CASE WHEN UPPER(TRIM(mp.MP_LIBLT)) = 'ESPECE'           THEN pr.PREC_MNT_PAY ELSE 0 END) AS recus_especes,
           SUM(CASE WHEN UPPER(TRIM(mp.MP_LIBLT)) = 'CHEQUE'           THEN pr.PREC_MNT_PAY ELSE 0 END) AS recus_cheques,
           SUM(CASE WHEN UPPER(TRIM(mp.MP_LIBLT)) = 'CHEQUE DE TRESOR' THEN pr.PREC_MNT_PAY ELSE 0 END) AS recus_cheques_tresor,
           SUM(CASE WHEN UPPER(TRIM(mp.MP_LIBLT)) = 'VIREMENT'         THEN pr.PREC_MNT_PAY ELSE 0 END) AS recus_virements,
           SUM(pr.PREC_MNT_PAY) AS recus_total
    FROM {schema}.T_PIECE_REC pr
    LEFT JOIN {schema}.S_MODE_PAY mp ON mp.MP_ID = pr.MP_ID
    WHERE {_RECU_VALIDE}
      AND pr.PREC_DATE_COMPTABLE >= :d1
      AND pr.PREC_DATE_COMPTABLE <  :d2
      {_filtre_str(schema, "pr", avec_centre)}
    GROUP BY pr.STR_ID, TRUNC(pr.PREC_DATE_COMPTABLE)
),
vers AS (
    SELECT v.STR_ID,
           TRUNC(v.VERS_DATE) AS jour,
           COUNT(*) AS nb_versements,
           SUM(CASE WHEN v.VERS_FLAG_VLD = 1
                     AND (v.FLAG_ANNUL IS NULL OR v.FLAG_ANNUL = 0)
                    THEN v.VERS_MNT_VERS ELSE 0 END) AS verse_valide,
           SUM(CASE WHEN (v.VERS_FLAG_VLD IS NULL OR v.VERS_FLAG_VLD != 1)
                     AND (v.FLAG_ANNUL IS NULL OR v.FLAG_ANNUL = 0)
                    THEN v.VERS_MNT_VERS ELSE 0 END) AS verse_non_valide,
           SUM(CASE WHEN (v.VERS_FLAG_VLD IS NULL OR v.VERS_FLAG_VLD != 1)
                     AND (v.FLAG_ANNUL IS NULL OR v.FLAG_ANNUL = 0)
                    THEN 1 ELSE 0 END) AS nb_non_valides,
           SUM(CASE WHEN v.FLAG_ANNUL = 1 THEN v.VERS_MNT_VERS ELSE 0 END) AS verse_annule
    FROM {schema}.S_VERS_ESPECE v
    WHERE v.VERS_DATE >= :d1
      AND v.VERS_DATE <  :d2
      {_filtre_str(schema, "v", avec_centre)}
    GROUP BY v.STR_ID, TRUNC(v.VERS_DATE)
),
ann AS (
    SELECT pr.STR_ID,
           TRUNC(pr.PREC_DATE) AS jour,
           COUNT(*)             AS nb_annulations,
           SUM(pr.PREC_MNT_PAY) AS montant_annule
    FROM {schema}.T_PIECE_REC pr
    WHERE pr.PREC_VLD = -1
      AND (pr.MP_ID IS NULL OR pr.MP_ID != 99)
      AND pr.PREC_DATE >= :d1
      AND pr.PREC_DATE <  :d2
      {_filtre_str(schema, "pr", avec_centre)}
    GROUP BY pr.STR_ID, TRUNC(pr.PREC_DATE)
),
cles AS (
    SELECT STR_ID, jour FROM recap
    UNION
    SELECT STR_ID, jour FROM enc
    UNION
    SELECT STR_ID, jour FROM vers
    UNION
    SELECT STR_ID, jour FROM ann
)
SELECT
    s.STR_CODE                      AS centre,
    s.STR_LIB_LT                    AS nom_centre,
    k.jour                          AS jour,
    NVL(r.nb_caisses, 0)            AS nb_caisses,
    NVL(r.report, 0)                AS report,
    NVL(e.nb_recus, 0)              AS nb_recus,
    NVL(e.recus_total, 0)           AS recus_total,
    NVL(e.recus_especes, 0)         AS recus_especes,
    NVL(e.recus_cheques, 0)         AS recus_cheques,
    NVL(e.recus_cheques_tresor, 0)  AS recus_cheques_tresor,
    NVL(e.recus_virements, 0)       AS recus_virements,
    NVL(r.recap_especes, 0)         AS recap_especes,
    NVL(r.recap_cheques, 0)         AS recap_cheques,
    NVL(r.recap_virements, 0)       AS recap_virements,
    NVL(v.nb_versements, 0)         AS nb_versements,
    NVL(v.verse_valide, 0)          AS verse_especes_valide,
    NVL(v.verse_non_valide, 0)      AS verse_especes_non_valide,
    NVL(v.nb_non_valides, 0)        AS nb_versements_non_valides,
    NVL(v.verse_annule, 0)          AS verse_especes_annule,
    NVL(r.recap_vers_especes, 0)    AS recap_vers_especes,
    NVL(r.recap_vers_cheques, 0)    AS recap_vers_cheques,
    NVL(r.depenses, 0)              AS depenses,
    NVL(r.solde, 0)                 AS solde,
    NVL(r.rupture_report, 0)        AS rupture_report,
    NVL(a.nb_annulations, 0)        AS nb_annulations,
    NVL(a.montant_annule, 0)        AS montant_annule
FROM cles k
JOIN {schema}.S_STRUCTURE s ON s.STR_ID = k.STR_ID
LEFT JOIN recap r ON r.STR_ID = k.STR_ID AND r.jour = k.jour
LEFT JOIN enc e   ON e.STR_ID = k.STR_ID AND e.jour = k.jour
LEFT JOIN vers v  ON v.STR_ID = k.STR_ID AND v.jour = k.jour
LEFT JOIN ann a   ON a.STR_ID = k.STR_ID AND a.jour = k.jour
WHERE 1 = 1
  {_filtre_centre(avec_centre)}
"""


def sql_encaissements(schema: str, avec_centre: bool = False) -> str:
    """Une ligne par encaissement validé, tous modes de paiement, rattaché au
    centre de l'abonné comme dans l'état des encaissements du CRM.

    Le centre et le montant suivent les quatre cas de l'état :
    1. reçu d'un abonné : centre du mouvement de portefeuille (CMVT 0111) au
       moment du paiement, sinon centre actuel de l'abonnement ;
    2. paiement groupé : mouvements de portefeuille 01111 (centre du mouvement,
       ou centre majoritaire du groupement pour la structure 63) ;
    3. devis et frais : une ligne par ligne de détail liée à une demande ;
    4. reliquat d'un reçu sans abonné ni groupement : pour un e-paiement, centre
       du mouvement de portefeuille, sinon premier abonnement du payeur, sinon
       détail, demande, puis reçu ; pour une caisse, centre du reçu (ou de la
       demande pour la structure 63).
    Un abonnement sans nature de client ni demande n'est pas repris (absent de
    l'état). Le centre étant déduit, cette requête lit toujours tous les
    centres : `avec_centre` est ignoré, le filtre se fait dans la base locale.

    Les reçus annulés ou non validés ne sont pas dans l'état : ils sont ajoutés
    ici avec leur statut, une ligne par reçu pour son montant entier, rattachés
    au centre de l'abonné s'il est connu, sinon à la caisse du reçu.
    """
    S = schema
    detail = f"FROM {S}.T_PIECE_DETAIL pd WHERE pd.PREC_ID = pr.PREC_ID"
    portef = f"FROM {S}.S_PORTEFEUILLE pf WHERE pf.REF_MVT = TO_CHAR(pr.PREC_ID)"
    valide = "pr.PREC_VLD = 1 AND (pr.FLAGVLDANNUL IS NULL OR pr.FLAGVLDANNUL != 1)"
    pas_valide = "(pr.PREC_VLD IS NULL OR pr.PREC_VLD != 1 OR pr.FLAGVLDANNUL = 1)"
    return f"""
WITH recus AS (
    SELECT /*+ MATERIALIZE */
           pr.PREC_ID, pr.STR_ID, pr.PAY_ID, pr.MP_ID, pr.BQ_ID, pr.MAT_AGENT, pr.PREC_NUM_CAISE,
           pr.PREC_TIME, pr.PREC_MNT_PAY, pr.PREC_NAT_PAY, pr.PREC_REF_PAY, pr.PREC_NOM_PAY_CH,
           CASE WHEN pr.MP_ID = 99 THEN TRUNC(pr.PREC_DATE)
                ELSE TRUNC(NVL(pr.PREC_DATE_COMPTABLE, pr.PREC_DATE)) END AS jour,
           CASE WHEN {valide} THEN 'VALIDE'
                WHEN pr.PREC_VLD = -1 OR pr.FLAGVLDANNUL = 1 THEN 'ANNULE'
                ELSE 'NON VALIDE' END                                  AS statut,
           (SELECT /*+ NO_UNNEST */ MIN(pd.ABN_ID) {detail})           AS abn_id,
           (SELECT /*+ NO_UNNEST */ MIN(pd.GRP_ID) {detail})           AS grp_id,
           (SELECT /*+ NO_UNNEST */ MIN(pd.DMD_ID) {detail})           AS dmd_id,
           (SELECT /*+ NO_UNNEST */ SUM(pd.PDE_MNT_PAYE) {detail})     AS total_detail,
           (SELECT /*+ NO_UNNEST */ MIN(pd.STR_ID) {detail})           AS str_detail,
           (SELECT /*+ NO_UNNEST */ MIN(pf.STR_ID) {portef}
               AND pf.CMVT = '0111')                                   AS str_port_abonne,
           (SELECT /*+ NO_UNNEST */ MIN(pf.STR_ID) {portef}
               AND pf.CMVT IN ('0111', '01111', '015', '1012'))        AS str_port,
           (SELECT /*+ NO_UNNEST */ MIN(pf.GRP_ID) {portef}
               AND pf.CMVT = '01111')                                  AS grp_port
    FROM {S}.T_PIECE_REC pr
    WHERE (pr.MP_ID = 99
           AND pr.PREC_DATE >= :d1 AND pr.PREC_DATE < :d2)
       OR ((pr.MP_ID IS NULL OR pr.MP_ID != 99)
           AND pr.PREC_DATE_COMPTABLE >= :d1 AND pr.PREC_DATE_COMPTABLE < :d2)
       -- Reçu annulé ou non validé sans date comptable : date du reçu.
       OR ((pr.MP_ID IS NULL OR pr.MP_ID != 99)
           AND pr.PREC_DATE_COMPTABLE IS NULL AND {pas_valide}
           AND pr.PREC_DATE >= :d1 AND pr.PREC_DATE < :d2)
),
payeur AS (
    -- Premier abonnement du payeur, pour les reçus sans abonné ni groupement.
    SELECT /*+ MATERIALIZE */ a.PAY_ID,
           MIN(a.ABN_ID)                                           AS abn_id,
           MIN(a.STR_ID) KEEP (DENSE_RANK FIRST ORDER BY a.ABN_ID) AS str_id
    FROM {S}.S_ABONNEMENT a
    WHERE a.PAY_ID IN (SELECT r.PAY_ID FROM recus r
                       WHERE r.abn_id IS NULL AND r.grp_id IS NULL AND r.PAY_ID IS NOT NULL)
    GROUP BY a.PAY_ID
),
groupes AS (
    -- Mouvements de portefeuille des paiements groupés (avec leur reçu s'il existe).
    SELECT /*+ MATERIALIZE */ p.GRP_ID, p.STR_ID, p.DATE_MVT, p.MONTANT, p.REF_MVT,
           pr.PREC_ID, pr.MP_ID, pr.BQ_ID, pr.MAT_AGENT, pr.PREC_NUM_CAISE, pr.PREC_TIME,
           pr.PREC_REF_PAY, pr.PREC_NOM_PAY_CH
    FROM {S}.S_PORTEFEUILLE p
    LEFT JOIN {S}.T_PIECE_REC pr
      ON pr.PREC_ID = CASE WHEN REGEXP_LIKE(p.REF_MVT, '^[0-9]+$') THEN TO_NUMBER(p.REF_MVT) END
    WHERE p.CMVT = '01111'
      AND p.DATE_MVT >= :d1
      AND p.DATE_MVT <  :d2
      AND (pr.PREC_ID IS NULL OR ({valide}))
),
majoritaire AS (
    -- Centre où le groupement a le plus d'abonnements (structure 63 seulement).
    SELECT GRP_ID, STR_ID FROM (
        SELECT a.GRP_ID, a.STR_ID,
               ROW_NUMBER() OVER (PARTITION BY a.GRP_ID ORDER BY COUNT(*) DESC, a.STR_ID) AS rn
        FROM {S}.S_ABONNEMENT a
        WHERE a.GRP_ID IN (SELECT g.GRP_ID FROM groupes g WHERE g.STR_ID = 63)
        GROUP BY a.GRP_ID, a.STR_ID
    ) WHERE rn = 1
),
lignes AS (
    -- 1. Reçu d'un abonné
    SELECT TO_CHAR(r.PREC_ID) AS num_recu, r.MP_ID, r.BQ_ID, r.MAT_AGENT, r.PREC_NUM_CAISE,
           r.PREC_TIME, r.PREC_REF_PAY, r.PREC_NOM_PAY_CH,
           r.jour                                   AS jour,
           r.PREC_MNT_PAY                           AS montant,
           COALESCE(r.str_port_abonne, a.STR_ID)    AS str_centre,
           r.abn_id                                 AS abn_ref,
           COALESCE(r.grp_id, r.grp_port)           AS grp_ref,
           CAST(NULL AS NUMBER)                     AS dmd_ref,
           r.statut                                 AS statut
    FROM recus r
    JOIN {S}.S_ABONNEMENT a ON a.ABN_ID = r.abn_id
    WHERE r.statut = 'VALIDE'
    UNION ALL
    -- 3. Devis et frais : une ligne par ligne de détail
    SELECT /*+ LEADING(r) USE_NL(pd) */
           TO_CHAR(r.PREC_ID), r.MP_ID, r.BQ_ID, r.MAT_AGENT, r.PREC_NUM_CAISE,
           r.PREC_TIME, r.PREC_REF_PAY, r.PREC_NOM_PAY_CH,
           r.jour,
           pd.PDE_MNT_PAYE,
           CASE WHEN r.MP_ID = 99 THEN COALESCE(pd.STR_ID, dmd.STR_ID, r.STR_ID)
                ELSE COALESCE(dmd.STR_ID, r.STR_ID) END,
           COALESCE(r.abn_id, CASE WHEN r.dmd_id IS NULL AND r.grp_id IS NULL THEN py.abn_id END),
           COALESCE(r.grp_id, r.grp_port),
           pd.DMD_ID,
           r.statut
    FROM recus r
    JOIN {S}.T_PIECE_DETAIL pd     ON pd.PREC_ID = r.PREC_ID
    LEFT JOIN {S}.S_DEMANDE dmd    ON dmd.DMD_ID = pd.DMD_ID
    LEFT JOIN payeur py            ON py.PAY_ID = r.PAY_ID
    WHERE r.statut = 'VALIDE'
      AND pd.ABN_ID IS NULL
      AND pd.GRP_ID IS NULL
      AND (pd.DMD_ID IS NOT NULL OR r.PREC_NAT_PAY = 4)
    UNION ALL
    -- 4. Reliquat d'un reçu sans abonné ni groupement
    SELECT TO_CHAR(r.PREC_ID), r.MP_ID, r.BQ_ID, r.MAT_AGENT, r.PREC_NUM_CAISE,
           r.PREC_TIME, r.PREC_REF_PAY, r.PREC_NOM_PAY_CH,
           r.jour,
           r.PREC_MNT_PAY - NVL(r.total_detail, 0),
           CASE WHEN r.MP_ID = 99
                THEN COALESCE(r.str_port, py.str_id, r.str_detail, dmd.STR_ID, r.STR_ID)
                WHEN r.STR_ID = 63 THEN COALESCE(dmd.STR_ID, r.STR_ID)
                ELSE r.STR_ID END,
           CASE WHEN r.dmd_id IS NULL THEN py.abn_id END,
           r.grp_port,
           r.dmd_id,
           r.statut
    FROM recus r
    LEFT JOIN payeur py            ON py.PAY_ID = r.PAY_ID
    LEFT JOIN {S}.S_DEMANDE dmd    ON dmd.DMD_ID = r.dmd_id
    WHERE r.statut = 'VALIDE'
      AND r.abn_id IS NULL
      AND r.grp_id IS NULL
      AND r.PREC_MNT_PAY - NVL(r.total_detail, 0) != 0
      AND (r.MP_ID = 99 OR r.dmd_id IS NOT NULL)
    UNION ALL
    -- 2. Paiement groupé : un mouvement de portefeuille par ligne
    SELECT NVL(TO_CHAR(g.PREC_ID), g.REF_MVT), g.MP_ID, g.BQ_ID, g.MAT_AGENT, g.PREC_NUM_CAISE,
           g.PREC_TIME, g.PREC_REF_PAY, g.PREC_NOM_PAY_CH,
           TRUNC(g.DATE_MVT),
           -g.MONTANT,
           CASE WHEN g.STR_ID = 63 THEN COALESCE(m.STR_ID, g.STR_ID) ELSE g.STR_ID END,
           CAST(NULL AS NUMBER),
           g.GRP_ID,
           CAST(NULL AS NUMBER),
           'VALIDE'
    FROM groupes g
    LEFT JOIN majoritaire m ON m.GRP_ID = g.GRP_ID
    UNION ALL
    -- 5. Reçu annulé ou non validé : une ligne par reçu, pour son montant entier
    SELECT TO_CHAR(r.PREC_ID), r.MP_ID, r.BQ_ID, r.MAT_AGENT, r.PREC_NUM_CAISE,
           r.PREC_TIME, r.PREC_REF_PAY, r.PREC_NOM_PAY_CH,
           r.jour,
           r.PREC_MNT_PAY,
           COALESCE(r.str_port_abonne, a.STR_ID, dmd.STR_ID, r.STR_ID),
           r.abn_id,
           COALESCE(r.grp_id, r.grp_port),
           r.dmd_id,
           r.statut
    FROM recus r
    LEFT JOIN {S}.S_ABONNEMENT a   ON a.ABN_ID = r.abn_id
    LEFT JOIN {S}.S_DEMANDE dmd    ON dmd.DMD_ID = r.dmd_id
    WHERE r.statut != 'VALIDE'
)
SELECT
    s.STR_CODE                                          AS centre,
    s.STR_LIB_LT                                        AS nom_centre,
    l.PREC_NUM_CAISE                                    AS num_caisse,
    l.num_recu                                          AS num_recu,
    COALESCE(TO_CHAR(a.ABN_ID), TO_CHAR(g.GRP_CODE))    AS reference,
    CASE WHEN a.ABN_ID IS NOT NULL   THEN 'ABONNE'
         WHEN g.GRP_ID IS NOT NULL   THEN 'GROUPEMENT'
         WHEN l.dmd_ref IS NOT NULL  THEN 'DEMANDE'
         ELSE 'AUTRE' END                               AS type_reference,
    g.GRP_LIBLT                                         AS nom_groupement,
    TO_CHAR(l.dmd_ref)                                  AS demande,
    -- Pour un e-paiement, l'état affiche la banque à la place du matricule.
    CASE WHEN l.MP_ID = 99 THEN bq.BQ_ABRV ELSE pers.MATRICULE END AS matricule,
    l.jour                                              AS date_comptable,
    l.PREC_TIME                                         AS heure,
    mp.MP_LIBLT                                         AS mode_paiement,
    bq.BQ_CODE                                          AS code_banque,
    bq.BQ_LIBLT                                         AS nom_banque,
    TO_CHAR(NULLIF(l.PREC_REF_PAY, 0))                  AS num_piece,
    l.PREC_NOM_PAY_CH                                   AS tireur,
    l.montant                                           AS montant,
    l.statut                                            AS statut
FROM lignes l
JOIN {S}.S_STRUCTURE s            ON s.STR_ID = l.str_centre
LEFT JOIN {S}.S_ABONNEMENT a      ON a.ABN_ID = l.abn_ref
LEFT JOIN {S}.S_GROUPEMENT g      ON g.GRP_ID = l.grp_ref
LEFT JOIN {S}.S_BANQUE bq         ON bq.BQ_ID = l.BQ_ID
LEFT JOIN {S}.S_MODE_PAY mp       ON mp.MP_ID = l.MP_ID
LEFT JOIN {S}.S_PERSONNEL pers    ON pers.PERS_MAT = l.MAT_AGENT
WHERE l.statut != 'VALIDE'
   OR NOT (a.ABN_ID IS NOT NULL AND a.NCLT_ID IS NULL AND a.DMD_ID IS NULL)
"""


def sql_versements(schema: str, avec_centre: bool) -> str:
    """Versements d'espèces, avec la banque et le compte du dépôt."""
    return f"""
SELECT
    s.STR_CODE                  AS centre,
    s.STR_LIB_LT                AS nom_centre,
    TO_CHAR(v.VERS_ID)          AS num_versement,
    v.VERS_NUM_CAISSE           AS num_caisse,
    pers.MATRICULE              AS matricule,
    v.VERS_DATE                 AS date_versement,
    bq.BQ_CODE                  AS code_banque,
    bq.BQ_LIBLT                 AS nom_banque,
    TO_CHAR(v.STBQ_NUM_COMPTE)  AS compte,
    v.VERS_MNT_VERS             AS montant,
    CASE WHEN v.FLAG_ANNUL = 1    THEN 'ANNULE'
         WHEN v.VERS_FLAG_VLD = 1 THEN 'VALIDE'
         ELSE 'NON VALIDE' END  AS statut
FROM {schema}.S_VERS_ESPECE v
JOIN {schema}.S_STRUCTURE s          ON s.STR_ID = v.STR_ID
LEFT JOIN {schema}.S_BANQUE bq       ON bq.BQ_ID = v.BQ_ID
LEFT JOIN {schema}.S_PERSONNEL pers  ON pers.PERS_MAT = v.MAT_AGENT
WHERE v.VERS_DATE >= :d1
  AND v.VERS_DATE <  :d2
  {_filtre_centre(avec_centre)}
"""


def sql_cheques(schema: str, avec_centre: bool) -> str:
    """Chèques encaissés (ordinaires et de Trésor), avec la banque du dépôt."""
    return f"""
WITH recus AS (
    SELECT /*+ MATERIALIZE */ pr.PREC_ID, pr.PREC_NUM_CAISE, pr.MAT_AGENT, pr.BQ_ID,
           pr.PREC_DATE_COMPTABLE, pr.PREC_MNT_PAY, pr.PREC_REF_PAY, pr.PREC_NOM_PAY_CH,
           pr.VERS_BQ_ID, pr.VERS_COMPTE, pr.VERS_DATE_BORD, pr.VERS_N_BORD,
           mp.MP_LIBLT, s.STR_CODE, s.STR_LIB_LT,
           {_REF_DETAIL.format(S=schema)}
    FROM {schema}.T_PIECE_REC pr
    JOIN {schema}.S_STRUCTURE s  ON s.STR_ID = pr.STR_ID
    JOIN {schema}.S_MODE_PAY mp  ON mp.MP_ID = pr.MP_ID
    WHERE {_RECU_VALIDE}
      AND UPPER(TRIM(mp.MP_LIBLT)) LIKE 'CHEQUE%'
      AND pr.PREC_DATE_COMPTABLE >= :d1
      AND pr.PREC_DATE_COMPTABLE <  :d2
      {_filtre_centre(avec_centre)}
)
SELECT
    r.STR_CODE                                          AS centre,
    r.STR_LIB_LT                                        AS nom_centre,
    r.PREC_NUM_CAISE                                    AS num_caisse,
    TO_CHAR(r.PREC_ID)                                  AS num_recu,
    COALESCE(TO_CHAR(r.abn_id), TO_CHAR(g.GRP_CODE))    AS reference,
    pers.MATRICULE                                      AS matricule,
    r.PREC_DATE_COMPTABLE                               AS date_comptable,
    r.MP_LIBLT                                          AS mode_paiement,
    TO_CHAR(NULLIF(r.PREC_REF_PAY, 0))                  AS num_cheque,
    r.PREC_NOM_PAY_CH                                   AS tireur,
    bq.BQ_CODE                                          AS code_banque_cheque,
    bq.BQ_LIBLT                                         AS nom_banque_cheque,
    r.VERS_DATE_BORD                                    AS date_depot,
    bv.BQ_CODE                                          AS code_banque_depot,
    bv.BQ_LIBLT                                         AS nom_banque_depot,
    TO_CHAR(NULLIF(r.VERS_COMPTE, 0))                   AS compte_depot,
    TO_CHAR(NULLIF(r.VERS_N_BORD, 0))                   AS num_bordereau,
    r.PREC_MNT_PAY                                      AS montant
FROM recus r
LEFT JOIN {schema}.S_GROUPEMENT g      ON g.GRP_ID = r.grp_id
LEFT JOIN {schema}.S_BANQUE bq         ON bq.BQ_ID = r.BQ_ID
LEFT JOIN {schema}.S_BANQUE bv         ON bv.BQ_ID = r.VERS_BQ_ID
LEFT JOIN {schema}.S_PERSONNEL pers    ON pers.PERS_MAT = r.MAT_AGENT
"""


def sql_annulations(schema: str, avec_centre: bool) -> str:
    """Reçus de caisse annulés (PREC_VLD = -1)."""
    return f"""
WITH recus AS (
    SELECT /*+ MATERIALIZE */ pr.PREC_ID, pr.PREC_NUM_CAISE, pr.MAT_AGENT, pr.MP_ID,
           pr.PREC_DATE, pr.PREC_TIME, pr.PREC_MNT_PAY,
           s.STR_CODE, s.STR_LIB_LT,
           {_REF_DETAIL.format(S=schema)}
    FROM {schema}.T_PIECE_REC pr
    JOIN {schema}.S_STRUCTURE s ON s.STR_ID = pr.STR_ID
    WHERE pr.PREC_VLD = -1
      AND (pr.MP_ID IS NULL OR pr.MP_ID != 99)
      AND pr.PREC_DATE >= :d1
      AND pr.PREC_DATE <  :d2
      {_filtre_centre(avec_centre)}
)
SELECT
    r.STR_CODE                                          AS centre,
    r.STR_LIB_LT                                        AS nom_centre,
    r.PREC_NUM_CAISE                                    AS num_caisse,
    TO_CHAR(r.PREC_ID)                                  AS num_recu,
    COALESCE(TO_CHAR(r.abn_id), TO_CHAR(g.GRP_CODE))    AS reference,
    pers.MATRICULE                                      AS matricule,
    r.PREC_DATE                                         AS date_recu,
    r.PREC_TIME                                         AS heure,
    mp.MP_LIBLT                                         AS mode_paiement,
    r.PREC_MNT_PAY                                      AS montant
FROM recus r
LEFT JOIN {schema}.S_GROUPEMENT g      ON g.GRP_ID = r.grp_id
LEFT JOIN {schema}.S_MODE_PAY mp       ON mp.MP_ID = r.MP_ID
LEFT JOIN {schema}.S_PERSONNEL pers    ON pers.PERS_MAT = r.MAT_AGENT
"""


def sql_non_valides(schema: str, avec_centre: bool) -> str:
    """Une ligne par reçu non validé (PREC_VLD = 0), avec ce qui a été crédité
    au compte du client, l'agent qui a saisi le reçu, le caissier du jour
    (agents des reçus validés de la même caisse), les postes utilisés et le
    récapitulatif de la caisse ce jour-là."""
    S = schema
    return f"""
WITH nv AS (
    SELECT /*+ MATERIALIZE */ pr.PREC_ID, pr.STR_ID, pr.MAT_AGENT, pr.ADR_IP,
           TRUNC(pr.PREC_DATE) AS jour, pr.PREC_TIME, pr.PREC_MNT_PAY,
           (SELECT /*+ NO_UNNEST */ MIN(pd.ABN_ID)
              FROM {S}.T_PIECE_DETAIL pd WHERE pd.PREC_ID = pr.PREC_ID) AS abn_id,
           (SELECT /*+ NO_UNNEST */ -SUM(pf.MONTANT)
              FROM {S}.S_PORTEFEUILLE pf WHERE pf.REF_MVT = TO_CHAR(pr.PREC_ID)) AS credite
    FROM {S}.T_PIECE_REC pr
    WHERE pr.PREC_VLD = 0
      AND pr.PREC_DATE >= :d1
      AND pr.PREC_DATE <  :d2
      {_filtre_str(S, "pr", avec_centre)}
),
recap AS (
    SELECT r.STR_ID,
           TRUNC(r.REC_DATE)                AS jour,
           SUM(NVL(r.REC_MNT_REPORT, 0))    AS report,
           SUM(NVL(r.REC_MNT_TOT_ESP, 0))   AS especes_validees,
           SUM(NVL(r.REC_MNT_VERS_ESP, 0))  AS verse_especes
    FROM {S}.S_RECAP_CAISSE r
    WHERE r.REC_DATE >= :d1
      AND r.REC_DATE <  :d2
      {_filtre_str(S, "r", avec_centre)}
    GROUP BY r.STR_ID, TRUNC(r.REC_DATE)
),
valides AS (
    -- Agents et postes des reçus validés, pour les seules caisses concernées.
    SELECT /*+ MATERIALIZE */ DISTINCT v.STR_ID, TRUNC(v.PREC_DATE_COMPTABLE) AS jour,
           v.MAT_AGENT, v.ADR_IP
    FROM {S}.T_PIECE_REC v
    WHERE v.PREC_VLD = 1
      AND (v.MP_ID IS NULL OR v.MP_ID != 99)
      AND v.PREC_DATE_COMPTABLE >= :d1
      AND v.PREC_DATE_COMPTABLE <  :d2
      AND v.STR_ID IN (SELECT n.STR_ID FROM nv n)
),
caissiers AS (
    SELECT c.STR_ID, c.jour,
           LISTAGG(c.MATRICULE, ', ') WITHIN GROUP (ORDER BY c.MATRICULE) AS matricules
    FROM (SELECT DISTINCT v.STR_ID, v.jour, p.MATRICULE
          FROM valides v
          JOIN {S}.S_PERSONNEL p ON p.PERS_MAT = v.MAT_AGENT) c
    GROUP BY c.STR_ID, c.jour
),
postes AS (
    SELECT i.STR_ID, i.jour,
           LISTAGG(i.ADR_IP, ', ') WITHIN GROUP (ORDER BY i.ADR_IP) AS postes
    FROM (SELECT DISTINCT v.STR_ID, v.jour, v.ADR_IP FROM valides v WHERE v.ADR_IP IS NOT NULL) i
    GROUP BY i.STR_ID, i.jour
)
SELECT
    s.STR_CODE                                        AS centre,
    s.STR_LIB_LT                                      AS nom_centre,
    TO_CHAR(nv.PREC_ID)                               AS num_recu,
    nv.jour                                           AS date_recu,
    nv.PREC_TIME                                      AS heure,
    TO_CHAR(nv.abn_id)                                AS abonne,
    TRIM(cl.CLT_NOM || ' ' || NVL(cl.CLT_PRENOM, '')) AS client,
    nv.PREC_MNT_PAY                                   AS montant,
    NVL(nv.credite, 0)                                AS credite,
    TO_CHAR(pa.MATRICULE)                             AS matricule,
    ca.matricules                                     AS caissier_jour,
    nv.ADR_IP                                         AS poste_recu,
    po.postes                                         AS postes_caissier,
    NVL(rc.especes_validees, 0)                       AS especes_validees,
    NVL(rc.report, 0)                                 AS report,
    NVL(rc.verse_especes, 0)                          AS verse_especes
FROM nv
JOIN {S}.S_STRUCTURE s          ON s.STR_ID = nv.STR_ID
LEFT JOIN {S}.S_PERSONNEL pa    ON pa.PERS_MAT = nv.MAT_AGENT
LEFT JOIN {S}.S_ABONNEMENT a    ON a.ABN_ID = nv.abn_id
LEFT JOIN {S}.S_CLIENT cl       ON cl.CLT_ID = a.CLI_ID
LEFT JOIN recap rc              ON rc.STR_ID = nv.STR_ID AND rc.jour = nv.jour
LEFT JOIN caissiers ca          ON ca.STR_ID = nv.STR_ID AND ca.jour = nv.jour
LEFT JOIN postes po             ON po.STR_ID = nv.STR_ID AND po.jour = nv.jour
"""


def sql_reports(schema: str, avec_centre: bool) -> str:
    """Report et solde par centre, caisse et jour, avec le solde de la veille."""
    return f"""
WITH {_RECAP_LAG.format(S=schema, F=_filtre_str(schema, "r", avec_centre))}
SELECT
    s.STR_CODE        AS centre,
    s.STR_LIB_LT      AS nom_centre,
    l.jour            AS jour,
    l.REC_NUM_CAISSE  AS num_caisse,
    l.report          AS report,
    l.tot_esp         AS especes,
    l.tot_chq         AS cheques,
    l.tot_vir         AS virements,
    l.vers_esp        AS verse_especes,
    l.vers_chq        AS verse_cheques,
    l.depenses        AS depenses,
    l.solde           AS solde,
    l.jour_precedent  AS jour_precedent,
    l.solde_veille    AS solde_veille,
    CASE WHEN l.solde_veille IS NULL THEN NULL
         ELSE l.report - l.solde_veille END AS rupture
FROM recap_lag l
JOIN {schema}.S_STRUCTURE s ON s.STR_ID = l.STR_ID
WHERE l.jour >= :d1
  {_filtre_centre(avec_centre)}
"""


def sql_planif_secteurs(schema: str) -> str:
    """Planning des relevés par secteur (PLANIF_RELEVE + PLANIFDETAILLE) et
    jour réel de facturation de chaque passage.

    - Un plan par secteur et par année ; s'il y en a plusieurs, le dernier
      PLAN_REL_ID est gardé. PLANIFDETAILLE : une ligne par mois planifié
      (6 par an pour un bimensuel, 11 ou 12 pour un mensuel ; TYPE = FLAG_MENS).
    - DATEDEBUTFACT : début du relevé prévu ; DATEDEBUTSEMAINE : facturation prévue.
    - Jour réel : jour où le secteur a le plus de factures (S_FACTURE_M) pour la
      période FACT_PERIODE = mois planifié (au moins 5 factures : les autres
      jours ne sont que des rattrapages). FACT_PERIODE n'a pas d'année : elle
      est déduite de la date de facture.
    Paramètres : :d0 (début de la fenêtre des factures), :a0 (première année du planning).
    """
    return f"""
WITH plan AS (
  SELECT p.SECT_ID, d.ANNEE, d.MOIS, d.TYPE AS flag_mens,
         d.DATEDEBUTFACT, d.DATEDEBUTSEMAINE,
         ROW_NUMBER() OVER (PARTITION BY p.SECT_ID, d.ANNEE, d.MOIS
                            ORDER BY p.PLAN_REL_ID DESC, d.IDDET DESC) AS rn
  FROM {schema}.PLANIFDETAILLE d
  JOIN {schema}.PLANIF_RELEVE p ON p.PLAN_REL_ID = d.PLAN_REL_ID
  WHERE d.ANNEE >= :a0
),
jours AS (
  SELECT f.SECT_ID, f.FACT_PERIODE AS mois, TRUNC(f.FACT_DATE) AS jour, COUNT(*) AS nb
  FROM {schema}.S_FACTURE_M f
  WHERE f.FACT_DATE >= :d0 AND f.FACT_PERIODE BETWEEN 1 AND 12
  GROUP BY f.SECT_ID, f.FACT_PERIODE, TRUNC(f.FACT_DATE)
  HAVING COUNT(*) >= 5
),
jours_a AS (
  SELECT j.SECT_ID, j.mois, j.jour, j.nb,
         CASE WHEN j.mois = 12 AND EXTRACT(MONTH FROM j.jour) = 1 THEN EXTRACT(YEAR FROM j.jour) - 1
              WHEN j.mois = 1 AND EXTRACT(MONTH FROM j.jour) = 12 THEN EXTRACT(YEAR FROM j.jour) + 1
              ELSE EXTRACT(YEAR FROM j.jour) END AS annee
  FROM jours j
),
pic AS (
  SELECT SECT_ID, mois, annee, jour, nb,
         ROW_NUMBER() OVER (PARTITION BY SECT_ID, mois, annee ORDER BY nb DESC, jour) AS rn
  FROM jours_a
)
SELECT st.STR_CODE                  AS centre,
       TO_CHAR(s.SECT_ID)           AS sect_id,
       s.SECT_CODE                  AS secteur,
       CASE WHEN pl.flag_mens = 1 THEN 'MENSUEL' ELSE 'BIMENSUEL' END AS frequence,
       pl.ANNEE                     AS annee,
       pl.MOIS                      AS mois,
       pl.DATEDEBUTFACT             AS date_releve_plan,
       pl.DATEDEBUTSEMAINE          AS date_plan,
       pc.jour                      AS date_fact_reelle,
       pc.nb                        AS nb_factures
FROM plan pl
JOIN {schema}.S_SECTEUR s    ON s.SECT_ID = pl.SECT_ID
JOIN {schema}.S_STRUCTURE st ON st.STR_ID = s.STR_ID
LEFT JOIN pic pc ON pc.SECT_ID = pl.SECT_ID AND pc.annee = pl.ANNEE
                AND pc.mois = pl.MOIS AND pc.rn = 1
WHERE pl.rn = 1
"""


def sql_compa_mensuel(schema: str) -> str:
    """Par centre et par mois : espèces encaissées (reçus validés, mode ESPECE,
    date comptable) et versements d'espèces (S_VERS_ESPECE, date de versement).
    Paramètres : :d1, :d2 (fin exclue)."""
    return f"""
WITH enc AS (
    SELECT pr.STR_ID, TRUNC(pr.PREC_DATE_COMPTABLE, 'MM') AS mois,
           SUM(pr.PREC_MNT_PAY) AS especes, COUNT(*) AS nb_recus
    FROM {schema}.T_PIECE_REC pr
    JOIN {schema}.S_MODE_PAY mp ON mp.MP_ID = pr.MP_ID
    WHERE {_RECU_VALIDE}
      AND UPPER(TRIM(mp.MP_LIBLT)) = 'ESPECE'
      AND pr.PREC_DATE_COMPTABLE >= :d1 AND pr.PREC_DATE_COMPTABLE < :d2
    GROUP BY pr.STR_ID, TRUNC(pr.PREC_DATE_COMPTABLE, 'MM')
),
vers AS (
    SELECT v.STR_ID, TRUNC(v.VERS_DATE, 'MM') AS mois,
           SUM(CASE WHEN v.VERS_FLAG_VLD = 1 AND (v.FLAG_ANNUL IS NULL OR v.FLAG_ANNUL = 0)
                    THEN v.VERS_MNT_VERS ELSE 0 END) AS verse,
           SUM(CASE WHEN (v.VERS_FLAG_VLD IS NULL OR v.VERS_FLAG_VLD != 1)
                     AND (v.FLAG_ANNUL IS NULL OR v.FLAG_ANNUL = 0)
                    THEN v.VERS_MNT_VERS ELSE 0 END) AS verse_non_valide,
           SUM(CASE WHEN v.VERS_FLAG_VLD = 1 AND (v.FLAG_ANNUL IS NULL OR v.FLAG_ANNUL = 0)
                    THEN 1 ELSE 0 END) AS nb_versements
    FROM {schema}.S_VERS_ESPECE v
    WHERE v.VERS_DATE >= :d1 AND v.VERS_DATE < :d2
    GROUP BY v.STR_ID, TRUNC(v.VERS_DATE, 'MM')
),
cles AS (SELECT STR_ID, mois FROM enc UNION SELECT STR_ID, mois FROM vers)
SELECT TRIM(s.STR_CODE) AS centre, s.STR_LIB_LT AS nom_centre,
       EXTRACT(MONTH FROM k.mois) AS mois,
       NVL(e.especes, 0) AS especes, NVL(e.nb_recus, 0) AS nb_recus,
       NVL(v.verse, 0) AS verse, NVL(v.verse_non_valide, 0) AS verse_non_valide,
       NVL(v.nb_versements, 0) AS nb_versements
FROM cles k
JOIN {schema}.S_STRUCTURE s ON s.STR_ID = k.STR_ID
LEFT JOIN enc e  ON e.STR_ID = k.STR_ID AND e.mois = k.mois
LEFT JOIN vers v ON v.STR_ID = k.STR_ID AND v.mois = k.mois
"""
