-- =============================================================================
-- db/001_schema.sql — Moteur de pré-analyse des allégations
-- Cible : PostgreSQL 16, pgvector >= 0.7, extensions pg_trgm et unaccent.
--
-- Deux schémas :
--   kb  : base réglementaire et SEO. Seule source de vérité. Le LLM n'y écrit jamais.
--   app : données clients, analyses, propositions, journal d'audit.
--
-- Règles d'or appliquées par contraintes et déclencheurs (pas par convention) :
--   R1. Le moteur ne lit que des lignes PUBLISHED, rattachées à une release publiée
--       et validées par un expert nommé.
--   R2. Une ligne publiée est immuable : on la retire (retired_in) et on en crée une
--       nouvelle dans une release ultérieure. L'audit trail reste rejouable.
--   R3. Une ligne « fixture » (tests) ne peut jamais être publiée.
--   R4. Une variante d'allégation nomme toujours la substance et ne contient aucun
--       chiffre hors identifiant de substance (B6, Q10, KSM-66…).
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE SCHEMA IF NOT EXISTS kb;
CREATE SCHEMA IF NOT EXISTS app;

-- -----------------------------------------------------------------------------
-- 0. Normalisation (miroir exact de engine/textnorm.py et frontend/localChecker.ts)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION kb.norm(t text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
  SELECT btrim(regexp_replace(
           translate(lower(public.unaccent('public.unaccent'::regdictionary, t)), '’‘ʼ`', ''''''''''),
           '[[:space:]\u00A0\u202F\u2009]+', ' ', 'g'))
$$;

CREATE OR REPLACE FUNCTION kb.norm_array(a text[]) RETURNS text[]
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
  SELECT coalesce(array_agg(kb.norm(x) ORDER BY o), '{}') FROM unnest(a) WITH ORDINALITY AS u(x, o)
$$;

-- -----------------------------------------------------------------------------
-- 1. Types énumérés
-- -----------------------------------------------------------------------------
CREATE TYPE kb.publication_status AS ENUM ('DRAFT', 'PUBLISHED');
CREATE TYPE kb.substance_kind AS ENUM ('VITAMINE', 'MINERAL', 'AUTRE_SUBSTANCE', 'PLANTE');
CREATE TYPE kb.plant_part AS ENUM (
  'RACINE', 'RHIZOME', 'FEUILLE', 'FLEUR', 'SOMMITE_FLEURIE', 'PARTIES_AERIENNES',
  'FRUIT', 'GRAINE', 'ECORCE', 'BULBE', 'RESINE', 'PLANTE_ENTIERE', 'AUTRE');
CREATE TYPE kb.preparation_kind AS ENUM (
  'POUDRE_PLANTE_SECHE', 'EXTRAIT_SEC', 'EXTRAIT_SEC_TITRE', 'EXTRAIT_LIQUIDE', 'HUILE', 'AUTRE');
CREATE TYPE kb.alias_kind AS ENUM (
  'NOM_COMMUN_FR', 'NOM_COMMERCIAL', 'SYNONYME_BOTANIQUE', 'NOM_VERNACULAIRE_ETRANGER',
  'FORME_CHIMIQUE', 'ABREVIATION');
CREATE TYPE kb.unit AS ENUM ('mg', 'µg', 'g', 'UI', 'UFC', 'PCT_VNR');
CREATE TYPE kb.claim_regime AS ENUM (
  'AUTHORIZED_432_2012',      -- art. 13§1 : liste du Règl. (UE) 432/2012 et modifications
  'AUTHORIZED_ART13_5',       -- art. 13§5 : autorisations individuelles (données scientifiques nouvelles)
  'AUTHORIZED_ART14',         -- art. 14 : réduction d'un risque de maladie (règlements individuels)
  'BOTANICAL_ON_HOLD_ART28',  -- botaniques en attente : régime transitoire art. 28§5-6
  'REJECTED_EU_REGISTER');    -- allégations non autorisées du registre de l'Union
CREATE TYPE kb.text_status AS ENUM (
  'OFFICIEL_JOUE',                   -- libellé de la version FR publiée au JOUE
  'TRADUCTION_DE_TRAVAIL_VALIDEE',   -- traduction interne validée par l'expert (aucun texte FR officiel)
  'FORMULATION_DE_TRAVAIL_VALIDEE',  -- formulation interne d'une exigence (information consommateur…)
  'REFERENTIEL_SOUS_LICENCE',        -- texte issu d'un référentiel tiers utilisé sous licence
  'NON_TRADUIT');
CREATE TYPE kb.variant_form AS ENUM (
  'PHRASE_COMPLETE',          -- « Le magnésium contribue à … »
  'GROUPE_NOMINAL_DEFINI',    -- « le magnésium, qui contribue à … »
  'GROUPE_NOMINAL_PARTITIF'); -- « du magnésium, qui contribue à … »
CREATE TYPE kb.condition_type AS ENUM (
  'DOSE_MIN_PORTION',          -- quantité minimale de substance (portion journalière ou quantifiée)
  'EQUIVALENT_PLANTE_MIN',     -- quantité minimale exprimée en plante sèche (extrait × DER)
  'TENEUR_MARQUEUR_MIN',       -- quantité minimale d'un marqueur d'extrait titré
  'PARTIE_PLANTE_REQUISE',     -- l'allégation ne vaut que pour certaines parties de plante
  'MENTION_OBLIGATOIRE',       -- information à afficher avec l'allégation
  'CONTEXTE_OBLIGATOIRE',      -- formule de contexte que la variante doit contenir
  'AUCUNE_CONDITION_CHIFFREE');-- la source ne fixe aucune condition chiffrée : statut « à documenter »
CREATE TYPE kb.population AS ENUM (
  'ENFANTS', 'ADOLESCENTS', 'FEMMES_ENCEINTES', 'FEMMES_ALLAITANTES',
  'PERSONNES_SOUS_TRAITEMENT', 'PERSONNES_AGEES', 'AUTRE');
CREATE TYPE kb.restriction_kind AS ENUM ('DECONSEILLE', 'MENTION_OBLIGATOIRE', 'INTERDIT');
CREATE TYPE kb.severity AS ENUM ('ROUGE', 'ORANGE', 'INFO', 'NEUTRE');
CREATE TYPE kb.lexical_category AS ENUM (
  'VERBE_THERAPEUTIQUE', 'PATHOLOGIE', 'VOCABULAIRE_PHARMACEUTIQUE',
  'ART12_NON_CONSOMMATION', 'ART12_PERTE_DE_POIDS', 'ART12_PROFESSIONNEL_SANTE',
  'DOCTRINAL_RISQUE', 'SUPERLATIF_PROMESSE',
  -- Catégories « de contrôle » : neutres en détection, utilisées par les validateurs
  -- du texte libre (réécriture et éditeur).
  'VERBE_EFFET', 'THEME_PHYSIOLOGIQUE', 'ATTRIBUT_FACTUEL');
CREATE TYPE kb.legal_basis AS ENUM (
  'INCO_1169_2011_ART7_3', 'R1924_2006_ART10_3', 'R1924_2006_ART12_A', 'R1924_2006_ART12_B',
  'R1924_2006_ART12_C', 'CSP_L5111_1_PRESENTATION', 'CCONSO_PRATIQUE_TROMPEUSE',
  'DOCTRINE_ADMINISTRATIVE', 'REGLE_INTERNE_MOTEUR');
CREATE TYPE kb.bridge_scope AS ENUM ('PRODUIT', 'ARTICLE', 'LES_DEUX');
CREATE TYPE kb.embedding_text_kind AS ENUM ('TEXTE_FR', 'TEXTE_EN_SOURCE', 'VARIANTE', 'ETIQUETTE_FONCTION');

-- -----------------------------------------------------------------------------
-- 2. Experts, releases et gabarit de versionnement
-- -----------------------------------------------------------------------------
CREATE TABLE kb.experts (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  full_name     text NOT NULL,
  qualification text NOT NULL,
  active        boolean NOT NULL DEFAULT true
);

CREATE TABLE kb.releases (
  id           integer PRIMARY KEY CHECK (id > 0),
  label        text NOT NULL UNIQUE,                -- ex. 'KB-2026.11.0'
  changelog    text NOT NULL,
  published_at timestamptz,
  approved_by  uuid REFERENCES kb.experts(id),
  CONSTRAINT release_published_requires_approver CHECK (published_at IS NULL OR approved_by IS NOT NULL)
);

-- Gabarit copié par LIKE dans chaque table versionnée (colonnes, défauts, CHECK).
-- Les clés étrangères et déclencheurs sont ajoutés par le bloc DO de la section 6.
CREATE TABLE kb._versioning_template (
  publication_status kb.publication_status NOT NULL DEFAULT 'DRAFT',
  introduced_in      integer,
  retired_in         integer,
  validated_by       uuid,
  validated_at       timestamptz,
  is_fixture         boolean NOT NULL DEFAULT false,
  CONSTRAINT v_published_requires_validation CHECK (
    publication_status = 'DRAFT'
    OR (validated_by IS NOT NULL AND validated_at IS NOT NULL AND introduced_in IS NOT NULL)),
  CONSTRAINT v_retired_after_introduced CHECK (
    retired_in IS NULL OR (introduced_in IS NOT NULL AND retired_in > introduced_in)),
  CONSTRAINT v_fixture_never_published CHECK (NOT (is_fixture AND publication_status = 'PUBLISHED'))
);
COMMENT ON TABLE kb._versioning_template IS 'Gabarit LIKE des colonnes de versionnement. Ne jamais alimenter.';

CREATE OR REPLACE FUNCTION kb.versioned_tables() RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
  SELECT ARRAY['substances', 'substance_aliases', 'mandatory_notices', 'plant_part_authorizations',
               'population_restrictions', 'regulatory_claims', 'claim_conditions', 'claim_variants',
               'lexical_entries', 'seo_semantic_bridges']
$$;

CREATE OR REPLACE FUNCTION kb.current_release() RETURNS integer
LANGUAGE sql STABLE AS $$
  SELECT max(id) FROM kb.releases WHERE published_at IS NOT NULL
$$;

-- Une ligne est utilisable à la release r si elle est publiée, introduite au plus tard
-- en r et non retirée en r. Les fixtures ne sont utilisables qu'en mode test explicite.
CREATE OR REPLACE FUNCTION kb.is_usable(
  s kb.publication_status, intro integer, retired integer, fixture boolean,
  r integer, include_fixtures boolean DEFAULT false) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
  SELECT (s = 'PUBLISHED' AND intro IS NOT NULL AND intro <= r AND (retired IS NULL OR retired > r))
      OR (include_fixtures AND fixture AND retired IS NULL)
$$;

-- -----------------------------------------------------------------------------
-- 3. Référentiel des substances
-- -----------------------------------------------------------------------------
CREATE TABLE kb.substances (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code             text NOT NULL CHECK (code ~ '^[A-Z0-9_]+$'),
  kind             kb.substance_kind NOT NULL,
  name_fr          text NOT NULL,                 -- 'Magnésium', 'Ashwagandha'
  scientific_name  text,                          -- binôme latin avec auteur pour les plantes
  botanical_family text,
  nrv_value        numeric CHECK (nrv_value > 0), -- VNR, Règl. (UE) 1169/2011 annexe XIII
  nrv_unit         kb.unit,
  source_ref       text NOT NULL,
  LIKE kb._versioning_template INCLUDING DEFAULTS INCLUDING CONSTRAINTS,
  CONSTRAINT substance_plant_has_binomial CHECK (kind <> 'PLANTE' OR scientific_name IS NOT NULL),
  CONSTRAINT substance_nrv_pair CHECK ((nrv_value IS NULL) = (nrv_unit IS NULL)),
  CONSTRAINT substance_nrv_only_vitmin CHECK (nrv_value IS NULL OR kind IN ('VITAMINE', 'MINERAL'))
);
CREATE UNIQUE INDEX substances_active_code_uq ON kb.substances (code) WHERE retired_in IS NULL;

-- Synonymes : noms communs, commerciaux (KSM-66), botaniques, formes chimiques
-- (bisglycinate de magnésium). Une dose saisie sur une forme chimique doit être
-- exprimée en élément : c'est l'écran des seuils qui le demande explicitement.
CREATE TABLE kb.substance_aliases (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  substance_id uuid NOT NULL REFERENCES kb.substances(id),
  alias        text NOT NULL,
  alias_norm   text GENERATED ALWAYS AS (kb.norm(alias)) STORED,
  alias_kind   kb.alias_kind NOT NULL,
  is_ambiguous boolean NOT NULL DEFAULT false,    -- ex. 'ginseng' : Panax ou Eleutherococcus
  note         text,
  LIKE kb._versioning_template INCLUDING DEFAULTS INCLUDING CONSTRAINTS
);
CREATE UNIQUE INDEX substance_aliases_active_uq ON kb.substance_aliases (substance_id, alias_norm) WHERE retired_in IS NULL;
CREATE INDEX substance_aliases_trgm ON kb.substance_aliases USING gin (alias_norm gin_trgm_ops);

CREATE TABLE kb.mandatory_notices (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code        text NOT NULL CHECK (code ~ '^[A-Z0-9_]+$'),
  text_fr     text NOT NULL,
  text_status kb.text_status NOT NULL,
  source_ref  text NOT NULL,
  LIKE kb._versioning_template INCLUDING DEFAULTS INCLUDING CONSTRAINTS
);
CREATE UNIQUE INDEX mandatory_notices_active_uq ON kb.mandatory_notices (code) WHERE retired_in IS NULL;

-- Parties de plante autorisées en France (arrêté du 24 juin 2014).
CREATE TABLE kb.plant_part_authorizations (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  substance_id     uuid NOT NULL REFERENCES kb.substances(id),
  plant_part       kb.plant_part NOT NULL,
  authorized_fr    boolean NOT NULL,
  source_ref       text NOT NULL,
  restriction_note text,
  LIKE kb._versioning_template INCLUDING DEFAULTS INCLUDING CONSTRAINTS
);
CREATE UNIQUE INDEX plant_part_auth_active_uq ON kb.plant_part_authorizations (substance_id, plant_part) WHERE retired_in IS NULL;

-- Restrictions de population : elles visent le produit (substance, partie), pas une
-- allégation. Elles sont donc hors de claim_conditions et produisent une alerte
-- « vigilance » sur la page, quelle que soit la réécriture.
CREATE TABLE kb.population_restrictions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  substance_id     uuid NOT NULL REFERENCES kb.substances(id),
  plant_part       kb.plant_part,
  populations      kb.population[] NOT NULL CHECK (cardinality(populations) > 0),
  restriction_kind kb.restriction_kind NOT NULL,
  notice_id        uuid REFERENCES kb.mandatory_notices(id),
  source_ref       text NOT NULL,
  LIKE kb._versioning_template INCLUDING DEFAULTS INCLUDING CONSTRAINTS,
  CONSTRAINT restriction_notice_required CHECK (restriction_kind <> 'MENTION_OBLIGATOIRE' OR notice_id IS NOT NULL)
);

-- -----------------------------------------------------------------------------
-- 4. Allégations, conditions, variantes, embeddings
-- -----------------------------------------------------------------------------
CREATE TABLE kb.regulatory_claims (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code               text NOT NULL CHECK (code ~ '^[A-Z0-9_-]+$'),  -- identifiant stable lisible
  regime             kb.claim_regime NOT NULL,
  substance_id       uuid NOT NULL REFERENCES kb.substances(id),
  plant_part         kb.plant_part,               -- obligatoire pour les botaniques on-hold
  eu_register_ref    text,                        -- renseigné par l'import du registre UE
  efsa_ids           text[] NOT NULL DEFAULT '{}',-- identifiants de la liste consolidée art. 13
  text_en_source     text,                        -- libellé source (registre / liste on-hold)
  text_fr            text,                        -- libellé FR (officiel JOUE ou traduction de travail)
  text_fr_status     kb.text_status NOT NULL,
  text_fr_source_ref text,
  license_ref        text,
  verb_fr            text,                        -- triplet : verbe ('contribue')
  function_fr        text,                        -- triplet : fonction ('à réduire la fatigue')
  function_label_fr  text NOT NULL,               -- étiquette courte ('fatigue')
  rejection_note     text,
  LIKE kb._versioning_template INCLUDING DEFAULTS INCLUDING CONSTRAINTS,
  CONSTRAINT claim_authorized_is_official CHECK (
    regime NOT IN ('AUTHORIZED_432_2012', 'AUTHORIZED_ART13_5', 'AUTHORIZED_ART14')
    OR text_fr_status = 'OFFICIEL_JOUE'),
  -- Il n'existe pas de libellé FR officiel pour la liste on-hold : statut officiel interdit.
  CONSTRAINT claim_onhold_shape CHECK (
    regime <> 'BOTANICAL_ON_HOLD_ART28'
    OR (plant_part IS NOT NULL AND text_en_source IS NOT NULL AND text_fr_status <> 'OFFICIEL_JOUE')),
  CONSTRAINT claim_license_ref CHECK (text_fr_status <> 'REFERENTIEL_SOUS_LICENCE' OR license_ref IS NOT NULL),
  CONSTRAINT claim_triplet_required CHECK (
    regime = 'REJECTED_EU_REGISTER' OR (verb_fr IS NOT NULL AND function_fr IS NOT NULL)),
  CONSTRAINT claim_usable_text CHECK (
    publication_status = 'DRAFT' OR regime = 'REJECTED_EU_REGISTER'
    OR (text_fr IS NOT NULL AND text_fr_status <> 'NON_TRADUIT')),
  CONSTRAINT claim_rejected_has_text CHECK (
    regime <> 'REJECTED_EU_REGISTER' OR text_en_source IS NOT NULL OR text_fr IS NOT NULL)
);
CREATE UNIQUE INDEX regulatory_claims_active_code_uq ON kb.regulatory_claims (code) WHERE retired_in IS NULL;
CREATE INDEX regulatory_claims_substance_idx ON kb.regulatory_claims (substance_id);

-- Conditions d'utilisation évaluées par engine/conditions.py, sans LLM.
-- La forme de params est imposée par type (CHECK ci-dessous).
CREATE TABLE kb.claim_conditions (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id       uuid NOT NULL REFERENCES kb.regulatory_claims(id),
  condition_type kb.condition_type NOT NULL,
  params         jsonb NOT NULL DEFAULT '{}'::jsonb,
  notice_id      uuid REFERENCES kb.mandatory_notices(id),
  source_ref     text NOT NULL,
  LIKE kb._versioning_template INCLUDING DEFAULTS INCLUDING CONSTRAINTS,
  CONSTRAINT condition_notice_pairing CHECK ((condition_type = 'MENTION_OBLIGATOIRE') = (notice_id IS NOT NULL)),
  CONSTRAINT condition_params_shape CHECK (
    CASE condition_type
      WHEN 'DOSE_MIN_PORTION' THEN
            params ?& ARRAY['amount', 'unit', 'basis']
        AND jsonb_typeof(params -> 'amount') = 'number'
        AND params ->> 'unit' IN ('mg', 'µg', 'g', 'UI', 'UFC', 'PCT_VNR')
        AND params ->> 'basis' IN ('PORTION_JOURNALIERE', 'PORTION_QUANTIFIEE')
      WHEN 'EQUIVALENT_PLANTE_MIN' THEN
            params ?& ARRAY['amount', 'unit', 'plant_part']
        AND jsonb_typeof(params -> 'amount') = 'number'
        AND params ->> 'unit' IN ('mg', 'g')
      WHEN 'TENEUR_MARQUEUR_MIN' THEN
            params ?& ARRAY['marker', 'amount', 'unit']
        AND jsonb_typeof(params -> 'amount') = 'number'
        AND params ->> 'unit' IN ('mg', 'µg', 'g')
      WHEN 'PARTIE_PLANTE_REQUISE' THEN
            jsonb_typeof(params -> 'plant_parts') = 'array'
      WHEN 'MENTION_OBLIGATOIRE' THEN
            params ->> 'placement' IN ('A_PROXIMITE', 'RENVOI_ASTERISQUE')
      WHEN 'CONTEXTE_OBLIGATOIRE' THEN
            jsonb_typeof(params -> 'context_fr') = 'string'
      WHEN 'AUCUNE_CONDITION_CHIFFREE' THEN
            params = '{}'::jsonb
    END)
);
CREATE INDEX claim_conditions_claim_idx ON kb.claim_conditions (claim_id);

-- Variantes pré-validées : une ligne par variante (et non un tableau JSONB) pour que
-- chaque variante ait son validateur, sa version, son compteur de rotation et puisse
-- être citée par clé étrangère dans le journal d'audit. La vue
-- kb.v_regulatory_claims_with_variations expose l'agrégat approved_variations (JSONB).
CREATE TABLE kb.claim_variants (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code                text NOT NULL CHECK (code ~ '^[A-Z0-9_-]+$'),
  claim_id            uuid NOT NULL REFERENCES kb.regulatory_claims(id),
  text_fr             text NOT NULL,              -- sans ponctuation finale (gérée par l'assembleur)
  text_norm           text GENERATED ALWAYS AS (kb.norm(text_fr)) STORED,
  syntactic_form      kb.variant_form NOT NULL,
  is_official_wording boolean NOT NULL DEFAULT false,
  equivalence_note    text,                       -- justification d'équivalence de sens (considérant 9)
  LIKE kb._versioning_template INCLUDING DEFAULTS INCLUDING CONSTRAINTS,
  CONSTRAINT variant_no_terminal_punct CHECK (text_fr !~ '[.!?;:…][[:space:]]*$'),
  CONSTRAINT variant_equivalence_justified CHECK (is_official_wording OR equivalence_note IS NOT NULL)
);
CREATE UNIQUE INDEX claim_variants_active_code_uq ON kb.claim_variants (code) WHERE retired_in IS NULL;
CREATE UNIQUE INDEX claim_variants_active_text_uq ON kb.claim_variants (claim_id, text_norm) WHERE retired_in IS NULL;
CREATE INDEX claim_variants_text_norm_idx ON kb.claim_variants (text_norm);

-- La dimension dépend du modèle d'embedding : on stocke le modèle, et la table est
-- séparée des allégations pour pouvoir ré-embarquer sans toucher aux lignes publiées.
CREATE TABLE kb.claim_embeddings (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_id        uuid NOT NULL REFERENCES kb.regulatory_claims(id),
  variant_id      uuid REFERENCES kb.claim_variants(id),
  text_kind       kb.embedding_text_kind NOT NULL,
  embedded_text   text NOT NULL,
  embedding_model text NOT NULL,
  embedding       vector(1536) NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT embedding_variant_pairing CHECK ((text_kind = 'VARIANTE') = (variant_id IS NOT NULL)),
  CONSTRAINT embedding_unique UNIQUE NULLS NOT DISTINCT (claim_id, text_kind, variant_id, embedding_model)
);
-- Index HNSW pour la recherche globale (substance non identifiée). Quand la substance
-- est connue, la recherche est exacte sur l'ensemble filtré (≤ quelques dizaines de lignes).
CREATE INDEX claim_embeddings_hnsw ON kb.claim_embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX claim_embeddings_claim_idx ON kb.claim_embeddings (claim_id);

-- -----------------------------------------------------------------------------
-- 5. Lexique et ponts sémantiques SEO
-- -----------------------------------------------------------------------------
CREATE TABLE kb.lexical_entries (
  id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code                            text NOT NULL CHECK (code ~ '^[A-Z0-9_-]+$'),
  lemma                           text NOT NULL,
  surface_forms                   text[] NOT NULL CHECK (cardinality(surface_forms) > 0),
  pattern_regex                   text,             -- motif sur texte normalisé (syntaxe commune Python/JS)
  category                        kb.lexical_category NOT NULL,
  legal_basis                     kb.legal_basis NOT NULL,
  severity_product                kb.severity NOT NULL,
  severity_article_with_bridge    kb.severity NOT NULL,
  severity_article_without_bridge kb.severity NOT NULL,
  icd10_code                      text,
  topic_key                       text,             -- relie un THEME_PHYSIOLOGIQUE aux ponts SEO
  neutralizing_left_context       text[] NOT NULL DEFAULT '{}',  -- ex. 'ne remplace pas un' devant 'traitement'
  note                            text,
  LIKE kb._versioning_template INCLUDING DEFAULTS INCLUDING CONSTRAINTS,
  CONSTRAINT lexical_pathology_icd CHECK (category <> 'PATHOLOGIE' OR icd10_code IS NOT NULL),
  CONSTRAINT lexical_topic_key CHECK (category <> 'THEME_PHYSIOLOGIQUE' OR topic_key IS NOT NULL),
  CONSTRAINT lexical_control_neutral CHECK (
    category NOT IN ('VERBE_EFFET', 'THEME_PHYSIOLOGIQUE', 'ATTRIBUT_FACTUEL')
    OR (severity_product = 'NEUTRE' AND severity_article_with_bridge = 'NEUTRE'
        AND severity_article_without_bridge = 'NEUTRE')),
  CONSTRAINT lexical_forms_normalized CHECK (surface_forms = kb.norm_array(surface_forms)),
  CONSTRAINT lexical_neutralizers_normalized CHECK (neutralizing_left_context = kb.norm_array(neutralizing_left_context))
);
CREATE UNIQUE INDEX lexical_entries_active_code_uq ON kb.lexical_entries (code) WHERE retired_in IS NULL;

CREATE VIEW kb.lexical_blacklists AS
  SELECT * FROM kb.lexical_entries
  WHERE category NOT IN ('VERBE_EFFET', 'THEME_PHYSIOLOGIQUE', 'ATTRIBUT_FACTUEL');

-- Pont : intention interdite à fort volume -> terme légal, utilisable seulement si l'une
-- des allégations d'ancrage est réellement présente dans le segment réécrit (art. 10§3)
-- et que le produit remplit ses conditions.
CREATE TABLE kb.seo_semantic_bridges (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code                 text NOT NULL CHECK (code ~ '^[A-Z0-9_-]+$'),
  illegal_intent_label text NOT NULL,              -- 'insomnie'
  illegal_query_terms  text[] NOT NULL,            -- termes à repérer dans les requêtes Search Console
  legal_term           text NOT NULL,              -- 'endormissement'
  legal_term_variants  text[] NOT NULL DEFAULT '{}',
  topic_key            text NOT NULL,              -- relie au lexique THEME_PHYSIOLOGIQUE
  search_volume_fr     integer CHECK (search_volume_fr >= 0),
  volume_source        text,
  volume_measured_at   date,
  usage_scope          kb.bridge_scope NOT NULL,
  LIKE kb._versioning_template INCLUDING DEFAULTS INCLUDING CONSTRAINTS,
  CONSTRAINT bridge_volume_sourced CHECK ((search_volume_fr IS NULL) = (volume_source IS NULL)),
  CONSTRAINT bridge_terms_normalized CHECK (illegal_query_terms = kb.norm_array(illegal_query_terms))
);
CREATE UNIQUE INDEX seo_bridges_active_code_uq ON kb.seo_semantic_bridges (code) WHERE retired_in IS NULL;

CREATE TABLE kb.seo_bridge_anchors (
  bridge_id uuid NOT NULL REFERENCES kb.seo_semantic_bridges(id),
  claim_id  uuid NOT NULL REFERENCES kb.regulatory_claims(id),
  PRIMARY KEY (bridge_id, claim_id)
);

-- -----------------------------------------------------------------------------
-- 6. Déclencheurs d'intégrité
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION kb.guard_published_rows() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  rel_published boolean;
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.publication_status = 'PUBLISHED' THEN
      RAISE EXCEPTION 'kb.%: suppression interdite d''une ligne publiée, utiliser retired_in', TG_TABLE_NAME;
    END IF;
    RETURN OLD;
  END IF;

  IF TG_OP = 'UPDATE' AND OLD.publication_status = 'PUBLISHED' THEN
    IF (to_jsonb(NEW) - 'retired_in') IS DISTINCT FROM (to_jsonb(OLD) - 'retired_in') THEN
      RAISE EXCEPTION 'kb.%: ligne publiée immuable, créer une nouvelle version', TG_TABLE_NAME;
    END IF;
    IF OLD.retired_in IS NOT NULL AND NEW.retired_in IS DISTINCT FROM OLD.retired_in THEN
      RAISE EXCEPTION 'kb.%: retrait déjà acté', TG_TABLE_NAME;
    END IF;
    IF NEW.retired_in IS NOT NULL THEN
      SELECT published_at IS NOT NULL INTO rel_published FROM kb.releases WHERE id = NEW.retired_in;
      IF rel_published THEN
        RAISE EXCEPTION 'kb.%: un retrait se rattache à une release non encore publiée', TG_TABLE_NAME;
      END IF;
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.publication_status = 'PUBLISHED' THEN
    SELECT published_at IS NOT NULL INTO rel_published FROM kb.releases WHERE id = NEW.introduced_in;
    IF rel_published THEN
      RAISE EXCEPTION 'kb.%: la release % est déjà publiée, rattacher la ligne à une nouvelle release',
        TG_TABLE_NAME, NEW.introduced_in;
    END IF;
  END IF;
  RETURN NEW;
END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY kb.versioned_tables() LOOP
    EXECUTE format('ALTER TABLE kb.%I ADD CONSTRAINT %I FOREIGN KEY (introduced_in) REFERENCES kb.releases(id)', t, t || '_introduced_fk');
    EXECUTE format('ALTER TABLE kb.%I ADD CONSTRAINT %I FOREIGN KEY (retired_in) REFERENCES kb.releases(id)', t, t || '_retired_fk');
    EXECUTE format('ALTER TABLE kb.%I ADD CONSTRAINT %I FOREIGN KEY (validated_by) REFERENCES kb.experts(id)', t, t || '_validated_by_fk');
    EXECUTE format('CREATE TRIGGER %I BEFORE INSERT OR UPDATE OR DELETE ON kb.%I FOR EACH ROW EXECUTE FUNCTION kb.guard_published_rows()', t || '_guard', t);
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION kb.guard_releases() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.published_at IS NOT NULL THEN
    RAISE EXCEPTION 'kb.releases: release % publiée, immuable', OLD.id;
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE TRIGGER releases_guard BEFORE UPDATE OR DELETE ON kb.releases
  FOR EACH ROW EXECUTE FUNCTION kb.guard_releases();

CREATE OR REPLACE FUNCTION kb.check_plant_part_substance() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF (SELECT kind FROM kb.substances WHERE id = NEW.substance_id) IS DISTINCT FROM 'PLANTE' THEN
    RAISE EXCEPTION 'kb.%: la substance % n''est pas une plante', TG_TABLE_NAME, NEW.substance_id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER plant_part_auth_is_plant BEFORE INSERT OR UPDATE ON kb.plant_part_authorizations
  FOR EACH ROW EXECUTE FUNCTION kb.check_plant_part_substance();

CREATE OR REPLACE FUNCTION kb.check_claim_substance() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.regime = 'BOTANICAL_ON_HOLD_ART28' OR NEW.plant_part IS NOT NULL)
     AND (SELECT kind FROM kb.substances WHERE id = NEW.substance_id) IS DISTINCT FROM 'PLANTE' THEN
    RAISE EXCEPTION 'kb.regulatory_claims %: régime botanique ou partie de plante sur une substance non végétale', NEW.code;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER regulatory_claims_substance_check BEFORE INSERT OR UPDATE ON kb.regulatory_claims
  FOR EACH ROW EXECUTE FUNCTION kb.check_claim_substance();

-- R4 : une variante nomme la substance et ne contient aucun chiffre hors identifiant.
CREATE OR REPLACE FUNCTION kb.check_variant() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_sub    uuid;
  v_regime kb.claim_regime;
  v_txt    text := kb.norm(NEW.text_fr);
  v_names  text[];
  v_name   text;
BEGIN
  SELECT substance_id, regime INTO v_sub, v_regime FROM kb.regulatory_claims WHERE id = NEW.claim_id;
  IF v_regime = 'REJECTED_EU_REGISTER' THEN
    RAISE EXCEPTION 'Variante % : une allégation rejetée n''a pas de variante', NEW.code;
  END IF;
  SELECT array_agg(n ORDER BY length(n) DESC) INTO v_names FROM (
    SELECT alias_norm AS n FROM kb.substance_aliases WHERE substance_id = v_sub AND retired_in IS NULL
    UNION SELECT kb.norm(name_fr) FROM kb.substances WHERE id = v_sub) s;
  IF NOT EXISTS (SELECT 1 FROM unnest(v_names) n WHERE position(n IN v_txt) > 0) THEN
    RAISE EXCEPTION 'Variante % : la substance de l''allégation n''est pas nommée', NEW.code;
  END IF;
  FOREACH v_name IN ARRAY v_names LOOP
    v_txt := replace(v_txt, v_name, ' ');
  END LOOP;
  IF v_txt ~ '[0-9]' THEN
    RAISE EXCEPTION 'Variante % : chiffre hors identifiant de substance', NEW.code;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER claim_variants_check BEFORE INSERT OR UPDATE ON kb.claim_variants
  FOR EACH ROW EXECUTE FUNCTION kb.check_variant();

CREATE OR REPLACE FUNCTION kb.guard_bridge_anchors() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_bridge uuid;
  v_claim  uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN v_bridge := OLD.bridge_id; ELSE v_bridge := NEW.bridge_id; v_claim := NEW.claim_id; END IF;
  IF (SELECT publication_status FROM kb.seo_semantic_bridges WHERE id = v_bridge) = 'PUBLISHED' THEN
    RAISE EXCEPTION 'Ancrages d''un pont publié immuables';
  END IF;
  IF v_claim IS NOT NULL AND (SELECT regime FROM kb.regulatory_claims WHERE id = v_claim) = 'REJECTED_EU_REGISTER' THEN
    RAISE EXCEPTION 'Un pont ne peut pas être ancré sur une allégation rejetée';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$;
CREATE TRIGGER seo_bridge_anchors_guard BEFORE INSERT OR UPDATE OR DELETE ON kb.seo_bridge_anchors
  FOR EACH ROW EXECUTE FUNCTION kb.guard_bridge_anchors();

-- -----------------------------------------------------------------------------
-- 7. Vues et fonctions de lecture utilisées par le moteur
-- -----------------------------------------------------------------------------
CREATE VIEW kb.v_regulatory_claims_with_variations AS
SELECT c.*,
       coalesce(jsonb_agg(jsonb_build_object(
                  'variant_code', v.code, 'text_fr', v.text_fr, 'form', v.syntactic_form,
                  'is_official', v.is_official_wording, 'status', v.publication_status)
                ORDER BY v.code) FILTER (WHERE v.id IS NOT NULL), '[]'::jsonb) AS approved_variations
FROM kb.regulatory_claims c
LEFT JOIN kb.claim_variants v ON v.claim_id = c.id AND v.retired_in IS NULL
GROUP BY c.id;

-- Publication d'une release : signature de l'expert sur toutes les lignes DRAFT
-- rattachées à cette release (hors fixtures), puis gel de la release.
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

-- Rapprochement : candidats d'une liste FERMÉE pour le reranker. Si la substance est
-- connue, la recherche est exacte sur les lignes de cette substance (pas d'approximation
-- d'index). Les rejetées remontent aussi : les reconnaître déclenche un rouge.
CREATE OR REPLACE FUNCTION kb.retrieve_claim_candidates(
  p_query            vector(1536),
  p_embedding_model  text,
  p_release          integer,
  p_substance_ids    uuid[] DEFAULT NULL,
  p_k                integer DEFAULT 5,
  p_include_fixtures boolean DEFAULT false)
RETURNS TABLE (claim_id uuid, claim_code text, regime kb.claim_regime, substance_id uuid,
               plant_part kb.plant_part, text_fr text, text_en_source text,
               function_label_fr text, distance double precision)
LANGUAGE sql STABLE AS $$
  WITH usable_claims AS (
    SELECT c.* FROM kb.regulatory_claims c
    WHERE kb.is_usable(c.publication_status, c.introduced_in, c.retired_in, c.is_fixture, p_release, p_include_fixtures)
      AND (p_substance_ids IS NULL OR c.substance_id = ANY (p_substance_ids))
  ), scored AS (
    SELECT e.claim_id, min(e.embedding <=> p_query) AS d
    FROM kb.claim_embeddings e
    JOIN usable_claims u ON u.id = e.claim_id
    LEFT JOIN kb.claim_variants v ON v.id = e.variant_id
    WHERE e.embedding_model = p_embedding_model
      AND (e.variant_id IS NULL
           OR kb.is_usable(v.publication_status, v.introduced_in, v.retired_in, v.is_fixture, p_release, p_include_fixtures))
    GROUP BY e.claim_id
  )
  SELECT u.id, u.code, u.regime, u.substance_id, u.plant_part, u.text_fr, u.text_en_source,
         u.function_label_fr, s.d
  FROM scored s JOIN usable_claims u ON u.id = s.claim_id
  ORDER BY s.d ASC, u.code ASC
  LIMIT p_k
$$;

-- Raccourci à coût nul : une phrase identique (normalisée) à une variante publiée.
CREATE OR REPLACE FUNCTION kb.match_exact_variant(p_sentence text, p_release integer, p_include_fixtures boolean DEFAULT false)
RETURNS TABLE (variant_code text, claim_code text)
LANGUAGE sql STABLE AS $$
  SELECT v.code, c.code
  FROM kb.claim_variants v JOIN kb.regulatory_claims c ON c.id = v.claim_id
  WHERE v.text_norm = kb.norm(p_sentence)
    AND kb.is_usable(v.publication_status, v.introduced_in, v.retired_in, v.is_fixture, p_release, p_include_fixtures)
    AND kb.is_usable(c.publication_status, c.introduced_in, c.retired_in, c.is_fixture, p_release, p_include_fixtures)
$$;

-- Règles compilées pour le vérificateur local de l'éditeur (0 € par frappe) et pour
-- engine/lexicon.py. Les motifs de zones gelées sont ajoutés par
-- engine/frozen_zones.export_patterns_for_frontend().
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
               'topic', l.topic_key, 'neutralizers', to_jsonb(l.neutralizing_left_context))
             ORDER BY l.code)
      FROM kb.lexical_entries l
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

-- -----------------------------------------------------------------------------
-- 8. Schéma app : clients, analyses, propositions, journal d'audit
-- -----------------------------------------------------------------------------
CREATE TYPE app.page_mode AS ENUM ('PRODUIT', 'ARTICLE');
CREATE TYPE app.finding_level AS ENUM ('ROUGE', 'ORANGE', 'CONDITIONNEL', 'INFO', 'AUCUN_RISQUE_DETECTE');
CREATE TYPE app.decision_kind AS ENUM (
  'ACCEPTEE', 'MODIFIEE', 'MAINTENUE_SOUS_RESPONSABILITE', 'CONDITION_CONFIRMEE', 'CONDITION_ACQUITTEE_NON_VERIFIEE');

CREATE TABLE app.workspaces (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.users (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES app.workspaces(id),
  email        text NOT NULL UNIQUE,
  full_name    text NOT NULL
);
CREATE TABLE app.brands (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id   uuid NOT NULL REFERENCES app.workspaces(id),
  name           text NOT NULL,
  primary_domain text NOT NULL
);
CREATE TABLE app.products (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  brand_id            uuid NOT NULL REFERENCES app.brands(id),
  name                text NOT NULL,
  canonical_url       text,
  portions_per_day    numeric CHECK (portions_per_day > 0),
  name_flagged_art1_3 boolean NOT NULL DEFAULT false   -- nom évocateur : signalé, jamais réécrit
);
CREATE TABLE app.product_ingredients (
  id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id               uuid NOT NULL REFERENCES app.products(id),
  substance_id             uuid REFERENCES kb.substances(id),   -- NULL si non rapproché
  raw_label                text NOT NULL,                       -- tel que déclaré
  plant_part               kb.plant_part,
  preparation              kb.preparation_kind,
  amount_per_daily_portion numeric CHECK (amount_per_daily_portion > 0),
  amount_unit              kb.unit,
  der_ratio                numeric CHECK (der_ratio > 0),       -- plante/extrait : 10 pour « 10:1 »
  marker_name              text,
  marker_pct               numeric CHECK (marker_pct > 0 AND marker_pct <= 100),
  data_origin              text NOT NULL CHECK (data_origin IN ('EXTRACTION_PAGE', 'SAISIE_UTILISATEUR', 'IMPORT')),
  CONSTRAINT ingredient_amount_pair CHECK ((amount_per_daily_portion IS NULL) = (amount_unit IS NULL))
);
-- Réponses binaires de l'écran des seuils groupés. Ajout seul : la plus récente fait foi.
CREATE TABLE app.threshold_answers (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  product_id   uuid NOT NULL REFERENCES app.products(id),
  condition_id uuid NOT NULL REFERENCES kb.claim_conditions(id),
  answer       text NOT NULL CHECK (answer IN ('OUI', 'NON', 'NE_SAIT_PAS')),
  answered_by  uuid NOT NULL REFERENCES app.users(id),
  answered_at  timestamptz NOT NULL DEFAULT now(),
  kb_release   integer NOT NULL REFERENCES kb.releases(id)
);
CREATE TABLE app.pages (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  brand_id    uuid NOT NULL REFERENCES app.brands(id),
  url         text NOT NULL,
  mode        app.page_mode NOT NULL,
  mode_source text NOT NULL CHECK (mode_source IN ('AUTO', 'UTILISATEUR')),
  product_id  uuid REFERENCES app.products(id),
  UNIQUE (brand_id, url)
);
CREATE TABLE app.analyses (
  id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  page_id                  uuid NOT NULL REFERENCES app.pages(id),
  kb_release               integer NOT NULL REFERENCES kb.releases(id),
  models                   jsonb NOT NULL,   -- {"extracteur": "...", "reranker": "...", "reecrivain": "...", "embedding": "..."}
  prompt_versions          jsonb NOT NULL,   -- empreintes SHA-256 des prompts et schémas utilisés
  rendered_dom_sha256      text NOT NULL,
  rendered_dom_storage_key text NOT NULL,
  started_at               timestamptz NOT NULL DEFAULT now(),
  finished_at              timestamptz,
  status                   text NOT NULL CHECK (status IN ('EN_COURS', 'TERMINEE', 'ECHEC'))
);
CREATE TABLE app.nodes (
  analysis_id     uuid NOT NULL REFERENCES app.analyses(id),
  node_key        text NOT NULL,             -- 'N14'
  zone            text NOT NULL,
  in_description  boolean NOT NULL,
  visibility      text NOT NULL CHECK (visibility IN ('V', 'H', 'C')),  -- visible, masqué, replié
  host_xpath      text NOT NULL,
  shopify_section text,
  is_global       boolean NOT NULL DEFAULT false,
  text_marked     text NOT NULL,
  text_plain      text NOT NULL,
  text_sha256     text NOT NULL,
  PRIMARY KEY (analysis_id, node_key)
);
CREATE TABLE app.findings (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  analysis_id      uuid NOT NULL REFERENCES app.analyses(id),
  node_keys        text[] NOT NULL,
  quote            text NOT NULL,
  origin           text NOT NULL CHECK (origin IN ('LEXIQUE', 'EXTRACTION_LLM', 'RAPPROCHEMENT', 'CONDITION',
                                                   'MODE_ARTICLE', 'AGREGATION_PAGE', 'CHAMP_SEO')),
  level            app.finding_level NOT NULL,
  category         text NOT NULL,
  legal_basis      kb.legal_basis,
  lexical_entry_id uuid REFERENCES kb.lexical_entries(id),
  claim_id         uuid REFERENCES kb.regulatory_claims(id),
  condition_id     uuid REFERENCES kb.claim_conditions(id),
  explanation_fr   text NOT NULL,
  remediations     jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE TABLE app.rewrite_proposals (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  analysis_id       uuid NOT NULL REFERENCES app.analyses(id),
  node_key          text NOT NULL,
  llm_output        jsonb NOT NULL,           -- sortie brute du réécrivain (spans)
  assembled_marked  text,
  assembled_plain   text,
  variant_ids       uuid[] NOT NULL DEFAULT '{}',
  notice_ids        uuid[] NOT NULL DEFAULT '{}',
  validation_report jsonb NOT NULL,
  status            text NOT NULL CHECK (status IN ('VALIDEE', 'REJETEE_VALIDATEUR', 'IMPOSSIBLE')),
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.decisions (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  finding_id  uuid NOT NULL REFERENCES app.findings(id),
  proposal_id uuid REFERENCES app.rewrite_proposals(id),
  decision    app.decision_kind NOT NULL,
  final_text  text,
  reason      text,
  decided_by  uuid NOT NULL REFERENCES app.users(id),
  decided_at  timestamptz NOT NULL DEFAULT now(),
  client_ip   inet,
  CONSTRAINT decision_reason_required CHECK (
    decision NOT IN ('MAINTENUE_SOUS_RESPONSABILITE', 'CONDITION_ACQUITTEE_NON_VERIFIEE')
    OR length(btrim(coalesce(reason, ''))) >= 10)
);
CREATE TABLE app.variant_usage (
  brand_id     uuid NOT NULL REFERENCES app.brands(id),
  variant_id   uuid NOT NULL REFERENCES kb.claim_variants(id),
  uses         integer NOT NULL DEFAULT 0 CHECK (uses >= 0),
  last_used_at timestamptz,
  PRIMARY KEY (brand_id, variant_id)
);
CREATE TABLE app.verification_ledger (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  page_id        uuid NOT NULL REFERENCES app.pages(id),
  analysis_id    uuid NOT NULL REFERENCES app.analyses(id),
  segment_sha256 text NOT NULL,
  kind           text NOT NULL CHECK (kind IN ('MOTEUR', 'MOTEUR_CACHE')),
  billable       boolean NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.verification_cache (
  cache_key  text PRIMARY KEY,   -- sha256(texte normalisé | contexte | release | modèles | prompts)
  kb_release integer NOT NULL REFERENCES kb.releases(id),
  result     jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION app.forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'app.%: table en ajout seul (journal d''audit)', TG_TABLE_NAME;
END $$;
CREATE TRIGGER decisions_append_only BEFORE UPDATE OR DELETE ON app.decisions
  FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();
CREATE TRIGGER threshold_answers_append_only BEFORE UPDATE OR DELETE ON app.threshold_answers
  FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();
CREATE TRIGGER verification_ledger_append_only BEFORE UPDATE OR DELETE ON app.verification_ledger
  FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();
CREATE TRIGGER rewrite_proposals_append_only BEFORE UPDATE OR DELETE ON app.rewrite_proposals
  FOR EACH ROW EXECUTE FUNCTION app.forbid_mutation();

-- Quota de re-vérifications moteur : 5 gratuites par page et par analyse, ensuite
-- décomptées (10 décomptées = 1 crédit d'analyse). Un résultat servi depuis le cache
-- n'est jamais décompté.
CREATE OR REPLACE FUNCTION app.register_engine_verification(
  p_page uuid, p_analysis uuid, p_segment_sha256 text, p_cache_hit boolean, p_free_per_page integer DEFAULT 5)
RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
  n        integer;
  billable boolean;
BEGIN
  IF p_cache_hit THEN
    INSERT INTO app.verification_ledger (page_id, analysis_id, segment_sha256, kind, billable)
    VALUES (p_page, p_analysis, p_segment_sha256, 'MOTEUR_CACHE', false);
    RETURN false;
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(p_page::text || ':' || p_analysis::text, 0));
  SELECT count(*) INTO n FROM app.verification_ledger
  WHERE page_id = p_page AND analysis_id = p_analysis AND kind = 'MOTEUR';
  billable := n >= p_free_per_page;
  INSERT INTO app.verification_ledger (page_id, analysis_id, segment_sha256, kind, billable)
  VALUES (p_page, p_analysis, p_segment_sha256, 'MOTEUR', billable);
  RETURN billable;
END $$;

CREATE VIEW app.v_verification_credits AS
SELECT page_id, analysis_id,
       count(*) FILTER (WHERE billable)                        AS billable_verifications,
       ceil(count(*) FILTER (WHERE billable) / 10.0)::integer  AS analysis_credits_consumed
FROM app.verification_ledger
GROUP BY page_id, analysis_id;

COMMIT;
