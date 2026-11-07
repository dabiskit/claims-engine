-- =============================================================================
-- db/004_public_referential.sql — Référentiel public (SEO programmatique)
--
-- Règles :
--   P1. Une page publique n'affiche que des lignes PUBLIÉES (validées par l'expert). Le mode
--       « preview » (lignes DRAFT) sert au staging interne : pages servies en noindex.
--   P2. Le verrouillage se fait CÔTÉ SERVEUR : les variantes et ponts verrouillés ne sortent
--       jamais de la base (seul leur nombre est exposé). Rien de « flouté » dans le HTML :
--       pas de contenu caché aux visiteurs mais servi aux robots.
--   P3. Les libellés officiels, conditions et sources restent en clair : c'est du droit public,
--       et c'est ce qui rend la page citable.
-- =============================================================================
BEGIN;

CREATE SCHEMA IF NOT EXISTS pub;

CREATE TABLE pub.silos (
  slug       text PRIMARY KEY CHECK (slug ~ '^[a-z0-9-]+$'),
  title_fr   text NOT NULL,
  topic_keys text[] NOT NULL CHECK (cardinality(topic_keys) > 0),
  sort_order integer NOT NULL
);
INSERT INTO pub.silos VALUES
  ('sommeil-stress', 'Allégations sommeil et stress', '{sommeil,decalage_horaire,relaxation,stress,systeme_nerveux,psychologique,humeur}', 10),
  ('energie-fatigue', 'Allégations énergie et fatigue', '{fatigue,energie,vitalite,performance}', 20),
  ('immunite', 'Allégations immunité', '{immunite}', 30),
  ('articulations', 'Allégations articulations et cartilages', '{cartilage}', 40),
  ('peau-beaute', 'Allégations peau et beauté', '{collagene_peau}', 50),
  ('muscles', 'Allégations muscles', '{muscle}', 60),
  ('sang', 'Allégations sang et globules rouges', '{globules_rouges}', 70);

CREATE TABLE pub.critical_cases (
  slug            text PRIMARY KEY CHECK (slug ~ '^[a-z0-9-]+$'),
  title_fr        text NOT NULL,
  substance_codes text[] NOT NULL DEFAULT '{}',
  silo_slugs      text[] NOT NULL DEFAULT '{}',
  published       boolean NOT NULL DEFAULT false      -- rédigé et relu par l'expert
);
INSERT INTO pub.critical_cases (slug, title_fr, substance_codes, silo_slugs) VALUES
  ('melatonine-seuils-mentions', 'Mélatonine : seuils, mentions obligatoires et vigilance', '{MELATONINE}', '{sommeil-stress}'),
  ('plantes-on-hold', 'Plantes : le régime transitoire des allégations « on-hold »',
   '{WITHANIA_SOMNIFERA,RHODIOLA_ROSEA,VALERIANA_OFFICINALIS,PASSIFLORA_INCARNATA,CROCUS_SATIVUS,PANAX_GINSENG}',
   '{sommeil-stress,energie-fatigue}'),
  ('collagene-sans-allegation', 'Collagène : vendre un actif sans allégation propre', '{VIT_C}', '{peau-beaute,articulations}'),
  ('article-blog-et-lien-produit', 'Article de blog et lien produit : ce qui change', '{}', '{}');

CREATE OR REPLACE FUNCTION pub.slugify(t text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT btrim(regexp_replace(kb.norm(t), '[^a-z0-9]+', '-', 'g'), '-') $$;

CREATE OR REPLACE FUNCTION pub.fr_amount(x numeric) RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT replace(trim_scale(x)::text, '.', ',') $$;

CREATE OR REPLACE FUNCTION pub.visible(s kb.publication_status, intro integer, retired integer, fixture boolean,
                                       preview boolean) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT coalesce(CASE WHEN preview THEN retired IS NULL AND NOT fixture
                       ELSE kb.is_usable(s, intro, retired, fixture, kb.current_release()) END, false)
$$;

-- Thèmes d'un actif : ponts ancrés sur ses allégations + ancrages botaniques en attente.
CREATE OR REPLACE FUNCTION pub.substance_topics(p_preview boolean)
RETURNS TABLE (substance_id uuid, topic_key text)
LANGUAGE sql STABLE AS $$
  SELECT rc.substance_id, b.topic_key
  FROM kb.seo_semantic_bridges b
  JOIN kb.seo_bridge_anchors a ON a.bridge_id = b.id
  JOIN kb.regulatory_claims rc ON rc.id = a.claim_id
  WHERE pub.visible(b.publication_status, b.introduced_in, b.retired_in, b.is_fixture, p_preview)
    AND pub.visible(rc.publication_status, rc.introduced_in, rc.retired_in, rc.is_fixture, p_preview)
  UNION
  SELECT pa.substance_id, b.topic_key
  FROM kb.bridge_pending_anchors pa
  JOIN kb.seo_semantic_bridges b ON b.id = pa.bridge_id
  WHERE pub.visible(b.publication_status, b.introduced_in, b.retired_in, b.is_fixture, p_preview)
$$;

-- -----------------------------------------------------------------------------
-- Données d'une page actif : /guide-allegations/{slug}
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION pub.ingredient_page(p_code text, p_preview boolean DEFAULT false) RETURNS jsonb
LANGUAGE sql STABLE AS $$
WITH s AS (
  SELECT * FROM kb.substances
  WHERE code = p_code AND pub.visible(publication_status, introduced_in, retired_in, is_fixture, p_preview)
), c AS (
  SELECT rc.* FROM kb.regulatory_claims rc JOIN s ON rc.substance_id = s.id
  WHERE rc.regime <> 'REJECTED_EU_REGISTER'
    AND pub.visible(rc.publication_status, rc.introduced_in, rc.retired_in, rc.is_fixture, p_preview)
), cond AS (
  SELECT cc.claim_id,
         jsonb_agg(jsonb_build_object('type', cc.condition_type, 'amount', cc.params -> 'amount',
                   'unit', cc.params ->> 'unit', 'basis', cc.params ->> 'basis', 'notice', mn.text_fr,
                   'source', cc.source_ref) ORDER BY cc.condition_type) AS conditions
  FROM kb.claim_conditions cc
  JOIN c ON c.id = cc.claim_id
  LEFT JOIN kb.mandatory_notices mn ON mn.id = cc.notice_id
  WHERE pub.visible(cc.publication_status, cc.introduced_in, cc.retired_in, cc.is_fixture, p_preview)
  GROUP BY cc.claim_id
), v AS (
  SELECT cv.claim_id,
         (array_agg(cv.text_fr ORDER BY cv.code) FILTER (WHERE NOT cv.is_official_wording))[1] AS public_variant,
         greatest(count(*) FILTER (WHERE NOT cv.is_official_wording) - 1, 0) AS locked_variants
  FROM kb.claim_variants cv JOIN c ON c.id = cv.claim_id
  WHERE pub.visible(cv.publication_status, cv.introduced_in, cv.retired_in, cv.is_fixture, p_preview)
  GROUP BY cv.claim_id
), thr AS (
  SELECT (cc.params ->> 'amount')::numeric AS amount, cc.params ->> 'unit' AS unit
  FROM kb.claim_conditions cc JOIN c ON c.id = cc.claim_id
  WHERE cc.params ->> 'derivation' = 'SOURCE_15_PCT_VNR'
  LIMIT 1
), br AS (
  SELECT DISTINCT b.code, b.illegal_intent_label, b.legal_term, b.topic_key, b.search_volume_fr
  FROM kb.seo_semantic_bridges b
  JOIN kb.seo_bridge_anchors a ON a.bridge_id = b.id
  JOIN c ON c.id = a.claim_id
  WHERE pub.visible(b.publication_status, b.introduced_in, b.retired_in, b.is_fixture, p_preview)
), br_ranked AS (
  SELECT *, row_number() OVER (ORDER BY search_volume_fr DESC NULLS LAST, code) AS rn FROM br
), topics AS (
  SELECT st.topic_key FROM pub.substance_topics(p_preview) st JOIN s ON s.id = st.substance_id
), related AS (
  SELECT s2.code, s2.name_fr, pub.slugify(s2.name_fr) AS slug, count(DISTINCT st.topic_key) AS shared_topics
  FROM pub.substance_topics(p_preview) st
  JOIN kb.substances s2 ON s2.id = st.substance_id
  WHERE st.topic_key IN (SELECT topic_key FROM topics)
    AND s2.id <> (SELECT id FROM s)
    AND pub.visible(s2.publication_status, s2.introduced_in, s2.retired_in, s2.is_fixture, p_preview)
  GROUP BY s2.code, s2.name_fr
  ORDER BY shared_topics DESC, s2.name_fr
  LIMIT 6
), meta AS (
  SELECT (SELECT label FROM kb.releases WHERE id = kb.current_release()) AS release_label,
         (SELECT array_agg(DISTINCT e.full_name) FROM c JOIN kb.experts e ON e.id = c.validated_by) AS reviewers,
         (SELECT max(c.validated_at) FROM c) AS last_reviewed
)
SELECT jsonb_build_object(
  'substance', jsonb_build_object('code', s.code, 'name', s.name_fr, 'slug', pub.slugify(s.name_fr),
               'kind', s.kind, 'scientific_name', s.scientific_name,
               'nrv', CASE WHEN s.nrv_value IS NULL THEN NULL ELSE pub.fr_amount(s.nrv_value) || ' ' || s.nrv_unit END,
               'threshold', (SELECT pub.fr_amount(amount) || ' ' || unit FROM thr)),
  'seo', jsonb_build_object(
      'url', '/guide-allegations/' || pub.slugify(s.name_fr),
      'title', CASE WHEN (SELECT count(*) FROM c) > 0
                    THEN s.name_fr || ' : allégations autorisées et conditions'
                    ELSE s.name_fr || ' : allégations, statut et vigilance' END,
      'meta_description', CASE WHEN (SELECT count(*) FROM c) > 0
            THEN s.name_fr || ' : ' || (SELECT count(*) FROM c) || ' allégations autorisées'
                 || coalesce(', seuil de ' || (SELECT pub.fr_amount(amount) || ' ' || unit FROM thr) || ' par jour', '')
                 || ', formulations à risque et termes SEO recevables.'
            ELSE s.name_fr || ' : régime des allégations, formulations à risque et points de vigilance.' END,
      'h1', s.name_fr || ' : allégations, conditions d''utilisation et formulations à risque',
      'robots', CASE WHEN p_preview THEN 'noindex,nofollow' ELSE 'index,follow' END),
  'claims', coalesce((SELECT jsonb_agg(jsonb_build_object(
       'code', c.code, 'regime', c.regime, 'text_fr', c.text_fr, 'text_status', c.text_fr_status,
       'function_label', c.function_label_fr, 'source', c.text_fr_source_ref,
       'conditions', coalesce(cond.conditions, '[]'::jsonb),
       'public_variant', v.public_variant, 'locked_variants', coalesce(v.locked_variants, 0)) ORDER BY c.code)
     FROM c LEFT JOIN cond ON cond.claim_id = c.id LEFT JOIN v ON v.claim_id = c.id), '[]'::jsonb),
  'risk_phrases', coalesce((SELECT jsonb_agg(jsonb_build_object('phrase', l.surface_forms[1], 'basis', l.legal_basis,
                   'severity', l.severity_product) ORDER BY l.code)
     FROM kb.lexical_entries l
     WHERE l.substance_id = s.id AND l.category::text = 'ALLEGATION_NON_AUTORISEE'
       AND pub.visible(l.publication_status, l.introduced_in, l.retired_in, l.is_fixture, p_preview)), '[]'::jsonb),
  'seo_bridges_public', coalesce((SELECT jsonb_agg(jsonb_build_object('illegal_intent', illegal_intent_label,
                   'legal_term', legal_term, 'search_volume_fr', search_volume_fr) ORDER BY rn)
     FROM br_ranked WHERE rn <= 2), '[]'::jsonb),
  'seo_bridges_locked', (SELECT count(*) FROM br_ranked WHERE rn > 2),
  'vigilance', coalesce((SELECT jsonb_agg(jsonb_build_object('populations', pr.populations, 'kind', pr.restriction_kind,
                   'notice', mn.text_fr, 'detail', pr.detail_fr, 'source', pr.source_ref))
     FROM kb.population_restrictions pr LEFT JOIN kb.mandatory_notices mn ON mn.id = pr.notice_id
     WHERE pr.substance_id = s.id
       AND pub.visible(pr.publication_status, pr.introduced_in, pr.retired_in, pr.is_fixture, p_preview)), '[]'::jsonb),
  'onhold_pending', coalesce((SELECT jsonb_agg(DISTINCT w.theme_fr)
     FROM kb.botanical_import_worklist w WHERE w.substance_id = s.id AND w.status = 'A_IMPORTER'), '[]'::jsonb),
  'silos', coalesce((SELECT jsonb_agg(jsonb_build_object('slug', si.slug, 'title', si.title_fr,
                   'url', '/conformite-seo/' || si.slug) ORDER BY si.sort_order)
     FROM pub.silos si WHERE si.topic_keys && (SELECT coalesce(array_agg(topic_key), '{}') FROM topics)), '[]'::jsonb),
  'related', coalesce((SELECT jsonb_agg(jsonb_build_object('name', name_fr, 'url', '/guide-allegations/' || slug,
                   'shared_topics', shared_topics) ORDER BY shared_topics DESC, name_fr) FROM related), '[]'::jsonb),
  'critical_cases', coalesce((SELECT jsonb_agg(jsonb_build_object('title', cc.title_fr, 'url', '/cas-critiques/' || cc.slug))
     FROM pub.critical_cases cc WHERE s.code = ANY (cc.substance_codes) AND (cc.published OR p_preview)), '[]'::jsonb),
  'meta', jsonb_build_object('kb_release', (SELECT release_label FROM meta), 'preview', p_preview,
                             'reviewed_by', (SELECT reviewers FROM meta), 'last_reviewed', (SELECT last_reviewed FROM meta),
                             'disclaimer', 'Référentiel de pré-analyse, non exhaustif ; ne constitue pas un avis juridique.'))
FROM s
$$;

-- -----------------------------------------------------------------------------
-- Données d'une page silo : /conformite-seo/{slug}
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION pub.silo_page(p_slug text, p_preview boolean DEFAULT false) RETURNS jsonb
LANGUAGE sql STABLE AS $$
WITH si AS (SELECT * FROM pub.silos WHERE slug = p_slug),
members AS (
  SELECT DISTINCT s.id, s.code, s.name_fr, s.kind
  FROM pub.substance_topics(p_preview) st
  JOIN kb.substances s ON s.id = st.substance_id
  JOIN si ON st.topic_key = ANY (si.topic_keys)
  WHERE pub.visible(s.publication_status, s.introduced_in, s.retired_in, s.is_fixture, p_preview)
), member_claims AS (
  SELECT m.code, array_agg(DISTINCT rc.function_label_fr ORDER BY rc.function_label_fr) AS functions
  FROM members m
  JOIN kb.regulatory_claims rc ON rc.substance_id = m.id
  JOIN kb.seo_bridge_anchors a ON a.claim_id = rc.id
  JOIN kb.seo_semantic_bridges b ON b.id = a.bridge_id
  JOIN si ON b.topic_key = ANY (si.topic_keys)
  WHERE pub.visible(rc.publication_status, rc.introduced_in, rc.retired_in, rc.is_fixture, p_preview)
  GROUP BY m.code
), bridges AS (
  SELECT b.illegal_intent_label, b.legal_term, b.search_volume_fr, b.code,
         row_number() OVER (ORDER BY b.search_volume_fr DESC NULLS LAST, b.code) AS rn
  FROM kb.seo_semantic_bridges b JOIN si ON b.topic_key = ANY (si.topic_keys)
  WHERE pub.visible(b.publication_status, b.introduced_in, b.retired_in, b.is_fixture, p_preview)
    AND EXISTS (SELECT 1 FROM kb.seo_bridge_anchors a WHERE a.bridge_id = b.id)
)
SELECT jsonb_build_object(
  'silo', jsonb_build_object('slug', si.slug, 'title', si.title_fr, 'url', '/conformite-seo/' || si.slug),
  'seo', jsonb_build_object('title', si.title_fr || ' : ce qui est autorisé, actif par actif',
                            'robots', CASE WHEN p_preview THEN 'noindex,nofollow' ELSE 'index,follow' END),
  'ingredients', coalesce((SELECT jsonb_agg(jsonb_build_object(
        'name', m.name_fr, 'url', '/guide-allegations/' || pub.slugify(m.name_fr),
        'functions', coalesce(to_jsonb(mc.functions), '[]'::jsonb),
        'status', CASE WHEN mc.functions IS NULL THEN 'ALLEGATIONS_ON_HOLD_EN_COURS_D_INTEGRATION'
                       ELSE 'ALLEGATIONS_AUTORISEES' END) ORDER BY (mc.functions IS NULL), m.name_fr)
     FROM members m LEFT JOIN member_claims mc ON mc.code = m.code), '[]'::jsonb),
  'seo_bridges_public', coalesce((SELECT jsonb_agg(jsonb_build_object('illegal_intent', illegal_intent_label,
        'legal_term', legal_term) ORDER BY rn) FROM bridges WHERE rn <= 3), '[]'::jsonb),
  'seo_bridges_locked', (SELECT count(*) FROM bridges WHERE rn > 3),
  'critical_cases', coalesce((SELECT jsonb_agg(jsonb_build_object('title', cc.title_fr, 'url', '/cas-critiques/' || cc.slug))
     FROM pub.critical_cases cc WHERE si.slug = ANY (cc.silo_slugs) AND (cc.published OR p_preview)), '[]'::jsonb))
FROM si
$$;

COMMIT;
