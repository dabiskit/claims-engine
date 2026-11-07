-- =============================================================================
-- db/090_dev_smoke_test.sql — ENVIRONNEMENT DE DÉVELOPPEMENT UNIQUEMENT
-- Publie la release 1 avec le compte EXPERT_DEMO, crée des embeddings factices et
-- vérifie que les règles d'intégrité tiennent. Ne jamais exécuter en production.
-- =============================================================================
\set ON_ERROR_STOP on

SELECT kb.publish_release(1, 'e0000000-0000-4000-8000-000000000001') AS publication;

-- Embeddings factices (vecteurs « one-hot ») pour tester la mécanique de recherche.
INSERT INTO kb.claim_embeddings (claim_id, text_kind, embedded_text, embedding_model, embedding)
SELECT c.id, 'TEXTE_FR', c.text_fr, 'dev-onehot-1536',
       (SELECT ('[' || string_agg(CASE WHEN i = row_number_c THEN '1' ELSE '0' END, ',' ORDER BY i) || ']')::vector
        FROM generate_series(1, 1536) i)
FROM (SELECT c.*, row_number() OVER (ORDER BY code)::int AS row_number_c FROM kb.regulatory_claims c) c;

DO $$
DECLARE
  q vector(1536);
  n integer;
  code_found text;
BEGIN
  -- 1. Les fixtures ne sont jamais publiées.
  SELECT count(*) INTO n FROM kb.regulatory_claims WHERE is_fixture AND publication_status = 'PUBLISHED';
  ASSERT n = 0, 'Une fixture a été publiée';

  -- 2. Recherche filtrée par substance : seules les allégations du magnésium remontent.
  SELECT embedding INTO q FROM kb.claim_embeddings e JOIN kb.regulatory_claims c ON c.id = e.claim_id
  WHERE c.code = 'AUT-MG-PSY';
  SELECT claim_code INTO code_found FROM kb.retrieve_claim_candidates(q, 'dev-onehot-1536', 1,
    ARRAY['10000000-0000-4000-8000-000000000001'::uuid], 5) LIMIT 1;
  ASSERT code_found = 'AUT-MG-PSY', 'Rapprochement exact attendu, obtenu ' || coalesce(code_found, 'NULL');
  SELECT count(*) INTO n FROM kb.retrieve_claim_candidates(q, 'dev-onehot-1536', 1,
    ARRAY['10000000-0000-4000-8000-000000000001'::uuid], 10);
  ASSERT n = 4, 'Quatre allégations magnésium attendues, obtenu ' || n;

  -- 3. La fixture n'est visible qu'en mode test explicite.
  SELECT count(*) INTO n FROM kb.retrieve_claim_candidates(q, 'dev-onehot-1536', 1,
    ARRAY['10000000-0000-4000-8000-000000000004'::uuid], 5);
  ASSERT n = 0, 'Fixture visible hors mode test';
  SELECT count(*) INTO n FROM kb.retrieve_claim_candidates(q, 'dev-onehot-1536', 1,
    ARRAY['10000000-0000-4000-8000-000000000004'::uuid], 5, true);
  ASSERT n = 1, 'Fixture invisible en mode test';

  -- 4. Correspondance exacte d'une variante (coût nul).
  SELECT claim_code INTO code_found FROM kb.match_exact_variant('Le  magnésium contribue à réduire la fatigue', 1);
  ASSERT code_found = 'AUT-MG-FATIGUE', 'Variante exacte non reconnue';

  -- 5. Règles compilées pour l'éditeur.
  SELECT jsonb_array_length(kb.compile_rules(1) -> 'lexicon') INTO n;
  ASSERT n >= 20, 'Lexique compilé incomplet';
END $$;

-- 6. Une ligne publiée est immuable.
DO $$
BEGIN
  UPDATE kb.regulatory_claims SET text_fr = 'Le magnésium booste votre moral' WHERE code = 'AUT-MG-PSY';
  RAISE EXCEPTION 'ÉCHEC : modification d''une ligne publiée acceptée';
EXCEPTION WHEN raise_exception THEN
  IF SQLERRM LIKE 'ÉCHEC%' THEN RAISE; END IF;
END $$;

-- 7. Une variante sans la substance, ou avec un chiffre, est refusée.
DO $$
BEGIN
  INSERT INTO kb.releases (id, label, changelog) VALUES (2, 'KB-TEST-2', 'test');
  INSERT INTO kb.claim_variants (code, claim_id, text_fr, syntactic_form, equivalence_note, introduced_in)
  VALUES ('TEST-BAD-1', '20000000-0000-4000-8000-000000000003', 'Contribue à des fonctions psychologiques normales', 'PHRASE_COMPLETE', 'test', 2);
  RAISE EXCEPTION 'ÉCHEC : variante sans substance acceptée';
EXCEPTION WHEN raise_exception THEN
  IF SQLERRM LIKE 'ÉCHEC%' THEN RAISE; END IF;
END $$;
DO $$
BEGIN
  INSERT INTO kb.claim_variants (code, claim_id, text_fr, syntactic_form, equivalence_note, introduced_in)
  VALUES ('TEST-BAD-2', '20000000-0000-4000-8000-000000000003', 'Le magnésium contribue à 100 fonctions psychologiques', 'PHRASE_COMPLETE', 'test', 1);
  RAISE EXCEPTION 'ÉCHEC : variante chiffrée acceptée';
EXCEPTION WHEN raise_exception THEN
  IF SQLERRM LIKE 'ÉCHEC%' THEN RAISE; END IF;
END $$;

-- 8. Une allégation on-hold ne peut pas porter le statut OFFICIEL_JOUE.
DO $$
BEGIN
  INSERT INTO kb.regulatory_claims (code, regime, substance_id, plant_part, text_en_source, text_fr, text_fr_status,
    verb_fr, function_fr, function_label_fr, introduced_in)
  VALUES ('TEST-BAD-OH', 'BOTANICAL_ON_HOLD_ART28', '10000000-0000-4000-8000-000000000004', 'RACINE', 'x', 'x',
    'OFFICIEL_JOUE', 'x', 'x', 'x', 1);
  RAISE EXCEPTION 'ÉCHEC : on-hold « officiel » accepté';
EXCEPTION WHEN check_violation THEN NULL;
END $$;

SELECT 'SMOKE TEST OK' AS result;
