# Moteur de pré-analyse des allégations — dépôt de travail

## Application (webapp/)
`webapp/main.py` + `webapp/features.py` : essai gratuit à accès complet, pré-analyse (texte, HTML ou
URL) avec extraction IA, propositions de réécriture IA contrôlées par l'assembleur, décisions
horodatées (journal d'audit), rapport imprimable, base réglementaire commentable par l'expert,
export CSV, tarifs, banc d'essai lançable par l'administrateur.
IA : Claude Fable (`claude-fable-5-1`) par défaut via `ANTHROPIC_API_KEY` ; sans clé, le
déterministe seul tourne et l'application le dit. Variables : voir `.env.example`.
- En local : `pip install -r webapp/requirements.txt` puis `uvicorn webapp.main:app --reload`
- En ligne : `Dockerfile` (Render) + Postgres (Supabase) via `DATABASE_URL`
- Variables : `DATABASE_URL`, `SESSION_SECRET`, `ADMIN_EMAILS`, `APP_NAME`, `TRIAL_DAYS`
- Tests : `pytest tests/test_webapp.py`

## Ordre de chargement
1. `db/001_schema.sql` — schéma `kb` (base réglementaire) et `app` (analyses, audit trail)
2. `db/003_seed_support.sql` — file de revue, liste de travail botanique, motifs de risque
3. `seed/out/lot1.sql` — généré : `python3 seed/build_lot1.py && python3 seed/json_to_sql.py seed/out/lot1.json > seed/out/lot1.sql`

4. `db/004_public_referential.sql` — référentiel public : `pub.ingredient_page(code)`, `pub.silo_page(slug)` (mode preview = noindex)

`db/002_seed.sql` et `db/090_dev_smoke_test.sql` sont réservés au développement (fixtures).

## Statut du contenu réglementaire
Tout le contenu de `seed/` est un **brouillon rédigé par un assistant IA**, importé en DRAFT.
`kb.publish_release()` refuse de publier tant qu'une tâche de `kb.review_items` est ouverte
(`SELECT * FROM kb.v_release_blockers`). Aucune allégation botanique on-hold n'est importée :
voir `kb.botanical_import_worklist`.

## État des modules
| Module | État |
|---|---|
| `engine/textnorm.py`, `lexicon.py`, `frozen_zones.py`, `dom_extractor.py`, `mode_router.py` | testés |
| `engine/conditions.py` | testé sommairement (dose, équivalent plante) |
| `engine/rewrite_assembler.py` (suite) | désormais exercé par le banc : 9 attaques de sortie LLM rejetées |
| `eval/` : golden set (10 archétypes), banc `evaluate_pipeline.py`, auto-test | livrés |
| Prompts système (`prompts/`), JSON Schemas (`schemas/`), couche IA (`engine/ai.py`, `LivePipeline`) | livrés, testés avec un faux client (aucun appel réel ici) |
| Vérificateur local TypeScript, facturation Stripe, surveillance hebdomadaire | **non livrés** |

## Évaluation
```
python3 eval/build_golden_set.py          # régénère golden_set.json et replays.json
python3 eval/evaluate_pipeline.py         # auto-test du banc puis benchmark (mode replay)
pytest -m "not release_gate"              # CI à chaque commit : fiabilité du banc
pytest -m release_gate                    # avant release : barrières bloquantes
```
Mode replay : les sorties du réécrivain sont des fixtures écrites à la main ; les métriques de réécriture
y testent l'assembleur, le patch DOM et les validateurs, pas la qualité d'un LLM. Le coût n'est mesurable
qu'en mode live (adaptateur à écrire une fois les prompts livrés).
