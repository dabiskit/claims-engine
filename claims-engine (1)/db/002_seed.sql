-- =============================================================================
-- db/002_seed.sql — Jeu initial (release 1, entièrement en DRAFT)
--
-- AVERTISSEMENT DE CONTENU
-- * Les libellés « OFFICIEL_JOUE » ci-dessous sont saisis de mémoire. Ils restent en
--   DRAFT tant que l'expert ne les a pas comparés mot à mot à la version FR du
--   Règl. (UE) 432/2012 sur EUR-Lex. La publication passe par kb.publish_release().
-- * L'allégation Ashwagandha est une FIXTURE (is_fixture = true) : son libellé, son ID
--   et son seuil sont inventés pour les tests et ne reproduisent aucune entrée réelle
--   de la liste on-hold. Le schéma interdit de la publier. La vraie ligne viendra de
--   l'import de la liste on-hold et d'une traduction de travail validée.
-- =============================================================================

BEGIN;

INSERT INTO kb.experts (id, full_name, qualification) VALUES
  ('e0000000-0000-4000-8000-000000000001', 'EXPERT_DEMO', 'Compte de démonstration — à remplacer par l''expert réglementaire réel');

INSERT INTO kb.releases (id, label, changelog) VALUES
  (1, 'KB-2026.11.0', 'Release initiale : magnésium, vitamine B6, mélatonine. Ashwagandha en fixture de test.');

-- -----------------------------------------------------------------------------
-- Substances
-- -----------------------------------------------------------------------------
INSERT INTO kb.substances (id, code, kind, name_fr, scientific_name, botanical_family, nrv_value, nrv_unit, source_ref, introduced_in) VALUES
  ('10000000-0000-4000-8000-000000000001', 'MG', 'MINERAL', 'Magnésium', NULL, NULL, 375, 'mg',
   'VNR : Règl. (UE) 1169/2011, annexe XIII, partie A', 1),
  ('10000000-0000-4000-8000-000000000002', 'VIT_B6', 'VITAMINE', 'Vitamine B6', NULL, NULL, 1.4, 'mg',
   'VNR : Règl. (UE) 1169/2011, annexe XIII, partie A', 1),
  ('10000000-0000-4000-8000-000000000003', 'MELATONINE', 'AUTRE_SUBSTANCE', 'Mélatonine', NULL, NULL, NULL, NULL,
   'Règl. (UE) 432/2012, annexe', 1),
  ('10000000-0000-4000-8000-000000000004', 'WITHANIA_SOMNIFERA', 'PLANTE', 'Ashwagandha', 'Withania somnifera (L.) Dunal', 'Solanaceae', NULL, NULL,
   'Arrêté du 24 juin 2014, annexe I — ligne à contrôler par l''expert', 1);

INSERT INTO kb.substance_aliases (substance_id, alias, alias_kind, is_ambiguous, introduced_in) VALUES
  ('10000000-0000-4000-8000-000000000001', 'magnésium', 'NOM_COMMUN_FR', false, 1),
  ('10000000-0000-4000-8000-000000000001', 'magnésium marin', 'NOM_COMMERCIAL', false, 1),
  ('10000000-0000-4000-8000-000000000001', 'bisglycinate de magnésium', 'FORME_CHIMIQUE', false, 1),
  ('10000000-0000-4000-8000-000000000001', 'citrate de magnésium', 'FORME_CHIMIQUE', false, 1),
  ('10000000-0000-4000-8000-000000000001', 'oxyde de magnésium', 'FORME_CHIMIQUE', false, 1),
  ('10000000-0000-4000-8000-000000000002', 'vitamine B6', 'NOM_COMMUN_FR', false, 1),
  ('10000000-0000-4000-8000-000000000002', 'B6', 'ABREVIATION', false, 1),
  ('10000000-0000-4000-8000-000000000002', 'pyridoxine', 'FORME_CHIMIQUE', false, 1),
  ('10000000-0000-4000-8000-000000000002', 'pyridoxal-5-phosphate', 'FORME_CHIMIQUE', false, 1),
  ('10000000-0000-4000-8000-000000000003', 'mélatonine', 'NOM_COMMUN_FR', false, 1),
  ('10000000-0000-4000-8000-000000000004', 'ashwagandha', 'NOM_COMMUN_FR', false, 1),
  ('10000000-0000-4000-8000-000000000004', 'withania', 'SYNONYME_BOTANIQUE', false, 1),
  ('10000000-0000-4000-8000-000000000004', 'withania somnifera', 'SYNONYME_BOTANIQUE', false, 1),
  ('10000000-0000-4000-8000-000000000004', 'ginseng indien', 'NOM_VERNACULAIRE_ETRANGER', false, 1),
  ('10000000-0000-4000-8000-000000000004', 'KSM-66', 'NOM_COMMERCIAL', false, 1);

INSERT INTO kb.plant_part_authorizations (substance_id, plant_part, authorized_fr, source_ref, introduced_in) VALUES
  ('10000000-0000-4000-8000-000000000004', 'RACINE', true,
   'Arrêté du 24 juin 2014, annexe I — partie autorisée à contrôler par l''expert', 1);

-- -----------------------------------------------------------------------------
-- Mentions obligatoires
-- -----------------------------------------------------------------------------
INSERT INTO kb.mandatory_notices (id, code, text_fr, text_status, source_ref, introduced_in, is_fixture) VALUES
  ('30000000-0000-4000-8000-000000000001', 'MELATONINE_COUCHER',
   'L''effet bénéfique est obtenu par la consommation de 1 mg de mélatonine peu de temps avant le coucher',
   'FORMULATION_DE_TRAVAIL_VALIDEE', 'Règl. (UE) 432/2012, annexe — condition d''utilisation de l''allégation mélatonine/endormissement', 1, false),
  ('30000000-0000-4000-8000-000000000002', 'NE_PAS_DEPASSER',
   'Ne pas dépasser la dose journalière recommandée',
   'FORMULATION_DE_TRAVAIL_VALIDEE', 'Décret n° 2006-352 du 20 mars 2006, art. 10', 1, false),
  ('30000000-0000-4000-8000-000000000003', 'HORS_PORTEE_ENFANTS',
   'Tenir hors de portée des jeunes enfants',
   'FORMULATION_DE_TRAVAIL_VALIDEE', 'Décret n° 2006-352 du 20 mars 2006, art. 10', 1, false),
  ('30000000-0000-4000-8000-000000000004', 'SUBSTITUT_REGIME',
   'Les compléments alimentaires ne doivent pas être utilisés comme substituts d''un régime alimentaire varié et équilibré et d''un mode de vie sain',
   'FORMULATION_DE_TRAVAIL_VALIDEE', 'Décret n° 2006-352 du 20 mars 2006, art. 10', 1, false),
  ('30000000-0000-4000-8000-000000000005', 'FIXTURE_WS_DECONSEILLE',
   'FIXTURE — avertissement de test, ne pas utiliser',
   'FORMULATION_DE_TRAVAIL_VALIDEE', 'FIXTURE', 1, true);

INSERT INTO kb.population_restrictions (substance_id, plant_part, populations, restriction_kind, notice_id, source_ref, introduced_in, is_fixture) VALUES
  ('10000000-0000-4000-8000-000000000004', 'RACINE', ARRAY['FEMMES_ENCEINTES', 'FEMMES_ALLAITANTES', 'ENFANTS']::kb.population[],
   'MENTION_OBLIGATOIRE', '30000000-0000-4000-8000-000000000005', 'FIXTURE', 1, true);

-- -----------------------------------------------------------------------------
-- Allégations (Règl. 432/2012) — libellés à vérifier contre le JOUE avant publication
-- -----------------------------------------------------------------------------
INSERT INTO kb.regulatory_claims (id, code, regime, substance_id, text_fr, text_fr_status, text_fr_source_ref,
                                  verb_fr, function_fr, function_label_fr, introduced_in) VALUES
  ('20000000-0000-4000-8000-000000000001', 'AUT-MG-FATIGUE', 'AUTHORIZED_432_2012', '10000000-0000-4000-8000-000000000001',
   'Le magnésium contribue à réduire la fatigue', 'OFFICIEL_JOUE', 'Règl. (UE) 432/2012, annexe, version FR', 'contribue', 'à réduire la fatigue', 'fatigue', 1),
  ('20000000-0000-4000-8000-000000000002', 'AUT-MG-SYSNERV', 'AUTHORIZED_432_2012', '10000000-0000-4000-8000-000000000001',
   'Le magnésium contribue au fonctionnement normal du système nerveux', 'OFFICIEL_JOUE', 'Règl. (UE) 432/2012, annexe, version FR', 'contribue', 'au fonctionnement normal du système nerveux', 'système nerveux', 1),
  ('20000000-0000-4000-8000-000000000003', 'AUT-MG-PSY', 'AUTHORIZED_432_2012', '10000000-0000-4000-8000-000000000001',
   'Le magnésium contribue à des fonctions psychologiques normales', 'OFFICIEL_JOUE', 'Règl. (UE) 432/2012, annexe, version FR', 'contribue', 'à des fonctions psychologiques normales', 'fonctions psychologiques', 1),
  ('20000000-0000-4000-8000-000000000004', 'AUT-MG-MUSCLE', 'AUTHORIZED_432_2012', '10000000-0000-4000-8000-000000000001',
   'Le magnésium contribue à une fonction musculaire normale', 'OFFICIEL_JOUE', 'Règl. (UE) 432/2012, annexe, version FR', 'contribue', 'à une fonction musculaire normale', 'fonction musculaire', 1),
  ('20000000-0000-4000-8000-000000000005', 'AUT-B6-FATIGUE', 'AUTHORIZED_432_2012', '10000000-0000-4000-8000-000000000002',
   'La vitamine B6 contribue à réduire la fatigue', 'OFFICIEL_JOUE', 'Règl. (UE) 432/2012, annexe, version FR', 'contribue', 'à réduire la fatigue', 'fatigue', 1),
  ('20000000-0000-4000-8000-000000000006', 'AUT-B6-SYSNERV', 'AUTHORIZED_432_2012', '10000000-0000-4000-8000-000000000002',
   'La vitamine B6 contribue au fonctionnement normal du système nerveux', 'OFFICIEL_JOUE', 'Règl. (UE) 432/2012, annexe, version FR', 'contribue', 'au fonctionnement normal du système nerveux', 'système nerveux', 1),
  ('20000000-0000-4000-8000-000000000007', 'AUT-B6-PSY', 'AUTHORIZED_432_2012', '10000000-0000-4000-8000-000000000002',
   'La vitamine B6 contribue à des fonctions psychologiques normales', 'OFFICIEL_JOUE', 'Règl. (UE) 432/2012, annexe, version FR', 'contribue', 'à des fonctions psychologiques normales', 'fonctions psychologiques', 1),
  ('20000000-0000-4000-8000-000000000008', 'AUT-MELA-ENDORM', 'AUTHORIZED_432_2012', '10000000-0000-4000-8000-000000000003',
   'La mélatonine contribue à réduire le temps d''endormissement', 'OFFICIEL_JOUE', 'Règl. (UE) 432/2012, annexe, version FR', 'contribue', 'à réduire le temps d''endormissement', 'endormissement', 1);

-- Régime botanique on-hold : FIXTURE (libellé, identifiant et seuil inventés).
INSERT INTO kb.regulatory_claims (id, code, regime, substance_id, plant_part, efsa_ids, text_en_source, text_fr,
                                  text_fr_status, text_fr_source_ref, verb_fr, function_fr, function_label_fr,
                                  introduced_in, is_fixture) VALUES
  ('20000000-0000-4000-8000-000000000009', 'FIXTURE-OH-WS-RELAX', 'BOTANICAL_ON_HOLD_ART28', '10000000-0000-4000-8000-000000000004',
   'RACINE', '{}', 'FIXTURE — test wording, does not reproduce any entry of the on-hold list',
   'L''ashwagandha contribue à la relaxation', 'TRADUCTION_DE_TRAVAIL_VALIDEE', 'FIXTURE — aucune source',
   'contribue', 'à la relaxation', 'relaxation', 1, true);

-- -----------------------------------------------------------------------------
-- Conditions d'utilisation
-- -----------------------------------------------------------------------------
-- « Source de » = 15 % de la VNR par portion journalière pour un complément alimentaire.
INSERT INTO kb.claim_conditions (claim_id, condition_type, params, source_ref, introduced_in)
SELECT c.id, 'DOSE_MIN_PORTION',
       jsonb_build_object('amount', round(s.nrv_value * 0.15, 4), 'unit', s.nrv_unit::text,
                          'basis', 'PORTION_JOURNALIERE', 'derivation', 'SOURCE_15_PCT_VNR',
                          'expressed_as', CASE WHEN s.kind = 'MINERAL' THEN 'ELEMENT' ELSE 'SUBSTANCE' END),
       'Règl. (UE) 432/2012 : au moins une « source de » au sens de l''annexe du Règl. (CE) 1924/2006 ; '
       || '15 % de la VNR (Règl. (UE) 1169/2011, annexe XIII) — base de calcul à confirmer par l''expert',
       1
FROM kb.regulatory_claims c JOIN kb.substances s ON s.id = c.substance_id
WHERE c.code IN ('AUT-MG-FATIGUE', 'AUT-MG-SYSNERV', 'AUT-MG-PSY', 'AUT-MG-MUSCLE',
                 'AUT-B6-FATIGUE', 'AUT-B6-SYSNERV', 'AUT-B6-PSY');

INSERT INTO kb.claim_conditions (claim_id, condition_type, params, notice_id, source_ref, introduced_in) VALUES
  ('20000000-0000-4000-8000-000000000008', 'DOSE_MIN_PORTION',
   '{"amount": 1, "unit": "mg", "basis": "PORTION_QUANTIFIEE", "expressed_as": "SUBSTANCE"}', NULL,
   'Règl. (UE) 432/2012, annexe : au moins 1 mg de mélatonine par portion quantifiée', 1),
  ('20000000-0000-4000-8000-000000000008', 'MENTION_OBLIGATOIRE',
   '{"placement": "RENVOI_ASTERISQUE"}', '30000000-0000-4000-8000-000000000001',
   'Règl. (UE) 432/2012, annexe : information du consommateur obligatoire', 1);

INSERT INTO kb.claim_conditions (claim_id, condition_type, params, source_ref, introduced_in, is_fixture) VALUES
  ('20000000-0000-4000-8000-000000000009', 'PARTIE_PLANTE_REQUISE', '{"plant_parts": ["RACINE"]}', 'FIXTURE', 1, true),
  ('20000000-0000-4000-8000-000000000009', 'EQUIVALENT_PLANTE_MIN', '{"amount": 3000, "unit": "mg", "plant_part": "RACINE"}', 'FIXTURE — seuil inventé', 1, true);

-- -----------------------------------------------------------------------------
-- Variantes (V00 = libellé officiel ; les autres exigent la validation de l'expert)
-- -----------------------------------------------------------------------------
INSERT INTO kb.claim_variants (code, claim_id, text_fr, syntactic_form, is_official_wording, equivalence_note, introduced_in)
SELECT c.code || '-V00', c.id, c.text_fr, 'PHRASE_COMPLETE', true, NULL, 1
FROM kb.regulatory_claims c WHERE NOT c.is_fixture AND c.regime = 'AUTHORIZED_432_2012';

INSERT INTO kb.claim_variants (code, claim_id, text_fr, syntactic_form, equivalence_note, introduced_in) VALUES
  ('AUT-MG-PSY-V01', '20000000-0000-4000-8000-000000000003', 'le magnésium, qui contribue à des fonctions psychologiques normales', 'GROUPE_NOMINAL_DEFINI', 'Libellé officiel en apposition : sens inchangé', 1),
  ('AUT-MG-PSY-V02', '20000000-0000-4000-8000-000000000003', 'du magnésium, qui contribue à des fonctions psychologiques normales', 'GROUPE_NOMINAL_PARTITIF', 'Libellé officiel en apposition : sens inchangé', 1),
  ('AUT-MG-PSY-V03', '20000000-0000-4000-8000-000000000003', 'Le magnésium participe à des fonctions psychologiques normales', 'PHRASE_COMPLETE', '« participe à » proposé comme équivalent de « contribue à » — à arbitrer par l''expert', 1),
  ('AUT-MG-FATIGUE-V01', '20000000-0000-4000-8000-000000000001', 'le magnésium, qui contribue à réduire la fatigue', 'GROUPE_NOMINAL_DEFINI', 'Libellé officiel en apposition : sens inchangé', 1),
  ('AUT-MG-FATIGUE-V02', '20000000-0000-4000-8000-000000000001', 'du magnésium, qui contribue à réduire la fatigue', 'GROUPE_NOMINAL_PARTITIF', 'Libellé officiel en apposition : sens inchangé', 1),
  ('AUT-B6-FATIGUE-V01', '20000000-0000-4000-8000-000000000005', 'la vitamine B6, qui contribue à réduire la fatigue', 'GROUPE_NOMINAL_DEFINI', 'Libellé officiel en apposition : sens inchangé', 1),
  ('AUT-B6-FATIGUE-V02', '20000000-0000-4000-8000-000000000005', 'de la vitamine B6, qui contribue à réduire la fatigue', 'GROUPE_NOMINAL_PARTITIF', 'Libellé officiel en apposition : sens inchangé', 1),
  ('AUT-B6-SYSNERV-V01', '20000000-0000-4000-8000-000000000006', 'la vitamine B6, qui contribue au fonctionnement normal du système nerveux', 'GROUPE_NOMINAL_DEFINI', 'Libellé officiel en apposition : sens inchangé', 1);

INSERT INTO kb.claim_variants (code, claim_id, text_fr, syntactic_form, is_official_wording, equivalence_note, introduced_in, is_fixture) VALUES
  ('FIXTURE-OH-WS-RELAX-V00', '20000000-0000-4000-8000-000000000009', 'L''ashwagandha contribue à la relaxation', 'PHRASE_COMPLETE', true, NULL, 1, true),
  ('FIXTURE-OH-WS-RELAX-V01', '20000000-0000-4000-8000-000000000009', 'de l''ashwagandha, qui contribue à la relaxation', 'GROUPE_NOMINAL_PARTITIF', false, 'FIXTURE', 1, true);

-- -----------------------------------------------------------------------------
-- Lexique (formes déjà normalisées : minuscules, sans accents)
-- -----------------------------------------------------------------------------
INSERT INTO kb.lexical_entries (code, lemma, surface_forms, pattern_regex, category, legal_basis,
  severity_product, severity_article_with_bridge, severity_article_without_bridge,
  icd10_code, topic_key, neutralizing_left_context, introduced_in) VALUES
  ('LEX-SOIGNER', 'soigner', '{soigne,soignent,soigner,soignez,soignera,soignant}', NULL,
   'VERBE_THERAPEUTIQUE', 'INCO_1169_2011_ART7_3', 'ROUGE', 'ROUGE', 'ORANGE', NULL, NULL, '{}', 1),
  ('LEX-GUERIR', 'guérir', '{guerit,guerissent,guerir,gueri,guerie,gueris,guerison}', NULL,
   'VERBE_THERAPEUTIQUE', 'INCO_1169_2011_ART7_3', 'ROUGE', 'ROUGE', 'ORANGE', NULL, NULL, '{}', 1),
  ('LEX-SOULAGER', 'soulager', '{soulage,soulagent,soulager,soulagement}', NULL,
   'VERBE_THERAPEUTIQUE', 'INCO_1169_2011_ART7_3', 'ROUGE', 'ROUGE', 'ORANGE', NULL, NULL, '{}', 1),
  ('LEX-TRAITEMENT', 'traitement', '{traitement,traitements,traite,traiter}', NULL,
   'VERBE_THERAPEUTIQUE', 'INCO_1169_2011_ART7_3', 'ROUGE', 'ROUGE', 'ORANGE', NULL, NULL,
   '{"ne remplace pas un","ne se substitue pas a un","en cas de"}', 1),
  ('LEX-INSOMNIE', 'insomnie', '{insomnie,insomnies}', NULL,
   'PATHOLOGIE', 'INCO_1169_2011_ART7_3', 'ROUGE', 'ROUGE', 'INFO', 'G47.0', NULL, '{}', 1),
  ('LEX-ANXIETE', 'anxiété', '{anxiete,anxietes}', NULL,
   'PATHOLOGIE', 'INCO_1169_2011_ART7_3', 'ROUGE', 'ROUGE', 'INFO', 'F41.9', NULL, '{}', 1),
  ('LEX-BURNOUT', 'burn-out', '{burn-out,burnout,burn out}', NULL,
   'PATHOLOGIE', 'INCO_1169_2011_ART7_3', 'ROUGE', 'ROUGE', 'INFO', 'Z73.0', NULL, '{}', 1),
  ('LEX-ARTHROSE', 'arthrose', '{arthrose,arthroses}', NULL,
   'PATHOLOGIE', 'INCO_1169_2011_ART7_3', 'ROUGE', 'ROUGE', 'INFO', 'M19.9', NULL, '{}', 1),
  ('LEX-POSOLOGIE', 'posologie', '{posologie,posologies}', NULL,
   'VOCABULAIRE_PHARMACEUTIQUE', 'CSP_L5111_1_PRESENTATION', 'ORANGE', 'ORANGE', 'INFO', NULL, NULL, '{}', 1),
  ('LEX-DETOX', 'détox', '{detox,detoxifiant,detoxifiante,detoxification}', NULL,
   'DOCTRINAL_RISQUE', 'DOCTRINE_ADMINISTRATIVE', 'ORANGE', 'ORANGE', 'INFO', NULL, NULL, '{}', 1),
  ('LEX-BRULE-GRAISSE', 'brûle-graisse', '{brule-graisse,brule-graisses,brule graisse,fat burner}', NULL,
   'DOCTRINAL_RISQUE', 'DOCTRINE_ADMINISTRATIVE', 'ORANGE', 'ORANGE', 'INFO', NULL, NULL, '{}', 1),
  ('LEX-REPARATEUR', 'réparateur', '{reparateur,reparatrice,reparateurs,reparatrices}', NULL,
   'DOCTRINAL_RISQUE', 'DOCTRINE_ADMINISTRATIVE', 'ORANGE', 'ORANGE', 'INFO', NULL, NULL, '{}', 1),
  ('LEX-PERTE-POIDS-CHIFFREE', 'perte de poids chiffrée', '{perdez}',
   'perd[a-z]*\s+(?:jusqu''a\s+)?\d+(?:[.,]\d+)?\s*(?:kg|kilos?)',
   'ART12_PERTE_DE_POIDS', 'R1924_2006_ART12_B', 'ROUGE', 'ROUGE', 'ORANGE', NULL, NULL, '{}', 1),
  ('LEX-RECO-MEDECINS', 'recommandé par les médecins', '{recommande par les medecins}',
   '(?:recommande|conseille|prescrit|approuve)e?s?\s+par\s+(?:les\s+|des\s+|un\s+)?(?:medecins?|pharmaciens?|docteurs?)',
   'ART12_PROFESSIONNEL_SANTE', 'R1924_2006_ART12_C', 'ROUGE', 'ROUGE', 'ORANGE', NULL, NULL, '{}', 1),
  ('LEX-DES-PREMIERE', 'dès la première', '{des la premiere nuit}',
   'des\s+(?:la\s+)?premiere\s+(?:nuit|prise|semaine|utilisation)',
   'SUPERLATIF_PROMESSE', 'CCONSO_PRATIQUE_TROMPEUSE', 'ORANGE', 'ORANGE', 'INFO', NULL, NULL, '{}', 1),
  -- Catégories de contrôle (neutres en détection, bloquantes dans le texte libre réécrit)
  ('LEX-CTRL-VERBES-EFFET', 'verbes d''effet',
   '{contribue,contribuent,contribuer,aide,aident,aider,favorise,favorisent,favoriser,soutient,soutiennent,soutenir,reduit,reduisent,reduire,ameliore,ameliorent,ameliorer,booste,boostent,booster,apaise,apaisent,apaiser,calme,calment,calmer,renforce,renforcent,renforcer,optimise,optimisent,optimiser,stimule,stimulent,stimuler,maintient,maintenir,preserve,preserver,protege,protegent,proteger,lutte,lutter,combat,combattre,diminue,diminuer,augmente,augmenter,retrouver,retrouvez,regenere,regenerer,draine,drainer,brule,bruler,participe,participent,participer}',
   NULL, 'VERBE_EFFET', 'REGLE_INTERNE_MOTEUR', 'NEUTRE', 'NEUTRE', 'NEUTRE', NULL, NULL, '{}', 1),
  ('LEX-CTRL-THEME-SOMMEIL', 'sommeil', '{sommeil,endormissement,endormir,nuit,nuits}', NULL,
   'THEME_PHYSIOLOGIQUE', 'REGLE_INTERNE_MOTEUR', 'NEUTRE', 'NEUTRE', 'NEUTRE', NULL, 'sommeil', '{}', 1),
  ('LEX-CTRL-THEME-FATIGUE', 'fatigue', '{fatigue,energie,vitalite}', NULL,
   'THEME_PHYSIOLOGIQUE', 'REGLE_INTERNE_MOTEUR', 'NEUTRE', 'NEUTRE', 'NEUTRE', NULL, 'fatigue', '{}', 1),
  ('LEX-CTRL-THEME-PSY', 'fonctions psychologiques', '{psychologique,psychologiques,moral,mental,mentale}', NULL,
   'THEME_PHYSIOLOGIQUE', 'REGLE_INTERNE_MOTEUR', 'NEUTRE', 'NEUTRE', 'NEUTRE', NULL, 'psychologique', '{}', 1),
  ('LEX-CTRL-THEME-NERVEUX', 'système nerveux', '{systeme nerveux,nerveux,nerveuse}', NULL,
   'THEME_PHYSIOLOGIQUE', 'REGLE_INTERNE_MOTEUR', 'NEUTRE', 'NEUTRE', 'NEUTRE', NULL, 'systeme_nerveux', '{}', 1),
  ('LEX-CTRL-THEME-RELAX', 'relaxation', '{relaxation,detente,serenite}', NULL,
   'THEME_PHYSIOLOGIQUE', 'REGLE_INTERNE_MOTEUR', 'NEUTRE', 'NEUTRE', 'NEUTRE', NULL, 'relaxation', '{}', 1),
  ('LEX-CTRL-THEME-STRESS', 'stress', '{stress,stresse,stressee,anti-stress}', NULL,
   'THEME_PHYSIOLOGIQUE', 'REGLE_INTERNE_MOTEUR', 'NEUTRE', 'NEUTRE', 'NEUTRE', NULL, 'stress', '{}', 1),
  ('LEX-CTRL-ATTRIBUTS', 'attributs factuels',
   '{bio,biologique,naturel,naturelle,naturels,naturelles,vegan,vegetalien,fabrique en france,made in france,brevete,brevetee,cliniquement,prouve,prouvee,teste,testee,scientifiquement,laboratoire}',
   NULL, 'ATTRIBUT_FACTUEL', 'REGLE_INTERNE_MOTEUR', 'NEUTRE', 'NEUTRE', 'NEUTRE', NULL, NULL, '{}', 1);

-- -----------------------------------------------------------------------------
-- Ponts sémantiques SEO (volumes à mesurer : NULL tant que non sourcés)
-- -----------------------------------------------------------------------------
INSERT INTO kb.seo_semantic_bridges (id, code, illegal_intent_label, illegal_query_terms, legal_term,
  legal_term_variants, topic_key, usage_scope, introduced_in, is_fixture) VALUES
  ('40000000-0000-4000-8000-000000000001', 'BR-INSOMNIE-ENDORMISSEMENT', 'insomnie', '{insomnie,insomnies,insomniaque}',
   'endormissement', '{"temps d''endormissement","s''endormir"}', 'sommeil', 'LES_DEUX', 1, false),
  ('40000000-0000-4000-8000-000000000002', 'BR-ANXIETE-FONCTIONS-PSY', 'anxiété', '{anxiete,anxieux,anxieuse,anxiolytique}',
   'fonctions psychologiques', '{}', 'psychologique', 'LES_DEUX', 1, false),
  ('40000000-0000-4000-8000-000000000003', 'BR-BURNOUT-FATIGUE', 'burn-out', '{burn-out,burnout,surmenage}',
   'fatigue', '{}', 'fatigue', 'LES_DEUX', 1, false),
  ('40000000-0000-4000-8000-000000000004', 'FIXTURE-BR-ANXIETE-RELAXATION', 'anxiété', '{anxiete}',
   'relaxation', '{}', 'relaxation', 'LES_DEUX', 1, true);

INSERT INTO kb.seo_bridge_anchors (bridge_id, claim_id) VALUES
  ('40000000-0000-4000-8000-000000000001', '20000000-0000-4000-8000-000000000008'),
  ('40000000-0000-4000-8000-000000000002', '20000000-0000-4000-8000-000000000003'),
  ('40000000-0000-4000-8000-000000000002', '20000000-0000-4000-8000-000000000007'),
  ('40000000-0000-4000-8000-000000000003', '20000000-0000-4000-8000-000000000001'),
  ('40000000-0000-4000-8000-000000000003', '20000000-0000-4000-8000-000000000005'),
  ('40000000-0000-4000-8000-000000000004', '20000000-0000-4000-8000-000000000009');

COMMIT;
