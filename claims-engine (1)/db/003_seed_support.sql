-- =============================================================================
-- db/003_seed_support.sql — Prérequis du seed de production (lots 1 à 3)
--
-- 1. Nouvelles valeurs d'énumération (hors transaction : une valeur ajoutée ne peut
--    pas être utilisée dans la transaction qui l'ajoute).
-- 2. File de revue expert : chaque ligne générée hors expertise humaine (dont tout ce
--    que produit un assistant IA) ouvre une tâche. kb.publish_release() refuse de
--    publier une release tant qu'une tâche reste ouverte : la signature de l'expert ne
--    peut plus être un tampon global.
-- 3. Liste de travail botanique : plantes on-hold dont les entrées officielles restent
--    à importer (identifiants, libellé source, conditions) puis à traduire.
-- 4. Motifs de risque propres à une substance (allégations non autorisées typiques).
-- =============================================================================

ALTER TYPE kb.variant_form     ADD VALUE IF NOT EXISTS 'GROUPE_PREPOSITIONNEL';
ALTER TYPE kb.plant_part       ADD VALUE IF NOT EXISTS 'STIGMATE';
ALTER TYPE kb.lexical_category ADD VALUE IF NOT EXISTS 'TERME_MEDICAL';
ALTER TYPE kb.lexical_category ADD VALUE IF NOT EXISTS 'ALLEGATION_NON_AUTORISEE';
ALTER TYPE kb.legal_basis      ADD VALUE IF NOT EXISTS 'R1924_2006_ART10_1';

BEGIN;

CREATE TYPE kb.confidence AS ENUM ('HAUTE', 'MOYENNE', 'BASSE');

-- -----------------------------------------------------------------------------
-- File de revue
-- -----------------------------------------------------------------------------
CREATE TABLE kb.review_items (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  release_id      integer NOT NULL REFERENCES kb.releases(id),
  target_table    text NOT NULL,            -- 'regulatory_claims', 'GLOBAL', …
  target_code     text NOT NULL,
  confidence      kb.confidence NOT NULL,   -- confiance de l'AUTEUR du brouillon, pas de l'expert
  task_fr         text NOT NULL,
  author          text NOT NULL,            -- ex. 'assistant-ia', 'import-registre-ue'
  status          text NOT NULL DEFAULT 'OUVERT' CHECK (status IN ('OUVERT', 'RESOLU', 'REJETE')),
  resolved_by     uuid REFERENCES kb.experts(id),
  resolved_at     timestamptz,
  resolution_note text,
  CONSTRAINT review_resolution_complete CHECK (
    status = 'OUVERT' OR (resolved_by IS NOT NULL AND resolved_at IS NOT NULL
                          AND length(btrim(coalesce(resolution_note, ''))) >= 5))
);
CREATE INDEX review_items_open_idx ON kb.review_items (release_id) WHERE status = 'OUVERT';

-- -----------------------------------------------------------------------------
-- Compléments de tables existantes
-- -----------------------------------------------------------------------------
ALTER TABLE kb.population_restrictions ADD COLUMN detail_fr text;
ALTER TABLE kb.lexical_entries ADD COLUMN substance_id uuid REFERENCES kb.substances(id);
ALTER TABLE kb.lexical_entries ADD CONSTRAINT lexical_risk_pattern_has_substance
  CHECK (category::text <> 'ALLEGATION_NON_AUTORISEE' OR substance_id IS NOT NULL);

-- -----------------------------------------------------------------------------
-- Liste de travail botanique (hors moteur : jamais lue par le pipeline de réécriture)
-- -----------------------------------------------------------------------------
CREATE TABLE kb.botanical_import_worklist (
  id                             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code                           text NOT NULL UNIQUE CHECK (code ~ '^[A-Z0-9_-]+$'),
  release_id                     integer NOT NULL REFERENCES kb.releases(id),
  substance_id                   uuid NOT NULL REFERENCES kb.substances(id),
  plant_part                     kb.plant_part NOT NULL,
  theme_fr                       text NOT NULL,
  theme_confidence               kb.confidence NOT NULL,
  efsa_ids                       text[] NOT NULL DEFAULT '{}',
  efsa_id_status                 text NOT NULL DEFAULT 'TO_VERIFY_IN_EU_REGISTER'
                                 CHECK (efsa_id_status IN ('TO_VERIFY_IN_EU_REGISTER', 'VERIFIE')),
  market_phrasings_detection_only text[] NOT NULL DEFAULT '{}',
  note                           text,
  status                         text NOT NULL DEFAULT 'A_IMPORTER'
                                 CHECK (status IN ('A_IMPORTER', 'IMPORTE', 'ABANDONNE')),
  CONSTRAINT worklist_verified_ids CHECK (efsa_id_status <> 'VERIFIE' OR cardinality(efsa_ids) > 0)
);

-- Ancrage différé d'un pont SEO sur une allégation botanique pas encore importée.
CREATE TABLE kb.bridge_pending_anchors (
  bridge_id    uuid NOT NULL REFERENCES kb.seo_semantic_bridges(id),
  substance_id uuid NOT NULL REFERENCES kb.substances(id),
  theme_fr     text NOT NULL,
  PRIMARY KEY (bridge_id, substance_id, theme_fr)
);

-- -----------------------------------------------------------------------------
-- Publication : garde-fous supplémentaires
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION kb.publish_release(p_release integer, p_expert uuid) RETURNS jsonb
LANGUAGE plpgsql AS $$
DECLARE
  t      text;
  n      integer;
  report jsonb := '{}'::jsonb;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM kb.experts WHERE id = p_expert AND active) THEN
    RAISE EXCEPTION 'Expert inconnu ou inactif';
  END IF;
  IF p_release <> coalesce(kb.current_release(), 0) + 1 THEN
    RAISE EXCEPTION 'Les releases se publient dans l''ordre (attendu : %)', coalesce(kb.current_release(), 0) + 1;
  END IF;
  SELECT count(*) INTO n FROM kb.review_items WHERE release_id = p_release AND status = 'OUVERT';
  IF n > 0 THEN
    RAISE EXCEPTION 'Release % : % tâche(s) de revue encore ouverte(s)', p_release, n;
  END IF;
  SELECT count(*) INTO n FROM kb.seo_semantic_bridges b
  WHERE b.introduced_in = p_release AND b.publication_status = 'DRAFT' AND NOT b.is_fixture
    AND NOT EXISTS (SELECT 1 FROM kb.seo_bridge_anchors a WHERE a.bridge_id = b.id);
  IF n > 0 THEN
    RAISE EXCEPTION 'Release % : % pont(s) SEO sans allégation d''ancrage (reporter à une release ultérieure)', p_release, n;
  END IF;
  FOREACH t IN ARRAY kb.versioned_tables() LOOP
    EXECUTE format('UPDATE kb.%I SET publication_status = ''PUBLISHED'', validated_by = $1, validated_at = now()
                    WHERE introduced_in = $2 AND publication_status = ''DRAFT'' AND NOT is_fixture', t)
      USING p_expert, p_release;
    GET DIAGNOSTICS n = ROW_COUNT;
    report := report || jsonb_build_object(t, n);
  END LOOP;
  UPDATE kb.releases SET published_at = now(), approved_by = p_expert WHERE id = p_release;
  RETURN report;
END $$;

-- Règles compilées : ajoute le code substance des motifs de risque.
CREATE OR REPLACE FUNCTION kb.compile_rules(p_release integer DEFAULT kb.current_release(), p_include_fixtures boolean DEFAULT false)
RETURNS jsonb
LANGUAGE sql STABLE AS $$
  SELECT jsonb_build_object(
    'release', p_release,
    'lexicon', coalesce((
      SELECT jsonb_agg(jsonb_build_object(
               'code', l.code, 'category', l.category, 'legal_basis', l.legal_basis,
               'forms', to_jsonb(l.surface_forms), 'pattern', l.pattern_regex,
               'severity', jsonb_build_object(
                  'PRODUIT', l.severity_product,
                  'ARTICLE_AVEC_PONT', l.severity_article_with_bridge,
                  'ARTICLE_SANS_PONT', l.severity_article_without_bridge),
               'topic', l.topic_key, 'neutralizers', to_jsonb(l.neutralizing_left_context),
               'substance', s.code)
             ORDER BY l.code)
      FROM kb.lexical_entries l LEFT JOIN kb.substances s ON s.id = l.substance_id
      WHERE kb.is_usable(l.publication_status, l.introduced_in, l.retired_in, l.is_fixture, p_release, p_include_fixtures)), '[]'::jsonb),
    'variants', coalesce((
      SELECT jsonb_agg(jsonb_build_object('code', v.code, 'claim', c.code, 'norm', v.text_norm) ORDER BY v.code)
      FROM kb.claim_variants v JOIN kb.regulatory_claims c ON c.id = v.claim_id
      WHERE kb.is_usable(v.publication_status, v.introduced_in, v.retired_in, v.is_fixture, p_release, p_include_fixtures)
        AND kb.is_usable(c.publication_status, c.introduced_in, c.retired_in, c.is_fixture, p_release, p_include_fixtures)), '[]'::jsonb),
    'substance_aliases', coalesce((
      SELECT jsonb_agg(jsonb_build_object('substance', s.code, 'norm', a.alias_norm, 'ambiguous', a.is_ambiguous) ORDER BY s.code, a.alias_norm)
      FROM kb.substance_aliases a JOIN kb.substances s ON s.id = a.substance_id
      WHERE kb.is_usable(a.publication_status, a.introduced_in, a.retired_in, a.is_fixture, p_release, p_include_fixtures)), '[]'::jsonb)
  )
$$;

-- Vue de travail de l'expert : ce qui bloque la release.
CREATE VIEW kb.v_release_blockers AS
SELECT r.release_id, r.confidence, r.target_table, r.target_code, r.task_fr
FROM kb.review_items r
WHERE r.status = 'OUVERT'
ORDER BY r.release_id, r.confidence DESC, r.target_table, r.target_code;

COMMIT;
