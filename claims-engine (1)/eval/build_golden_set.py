#!/usr/bin/env python3
"""
eval/build_golden_set.py — Les 10 archétypes fondateurs du golden set.

Sorties :
  eval/golden_set.json   vérité terrain (annotée par un assistant IA, expert_validated = false)
  eval/replays.json      sorties enregistrées du réécrivain (format « spans ») pour le mode replay

Principes d'annotation
  * La vérité terrain décrit ce que le DROIT exige, pas ce que le moteur sait faire aujourd'hui.
  * Chaque drapeau attendu indique la couche censée le détecter (detectable_by) :
      LEXIQUE        dictionnaires et motifs (déterministe)
      REGLES_KB      règles appliquées à la base (conditions, mentions, art. 10§3, vigilance)
      EXTRACTEUR_LLM allégation implicite ou paraphrasée, sans mot interdit
  * frozen_zones : chaînes exactes qui doivent survivre au caractère près, autant de fois qu'en entrée.
  * Les libellés du Règl. 432/2012 utilisés sont ceux du seed lot 1 (à vérifier sur EUR-Lex) ; les
    actifs des lots 2 et 3 (collagène, biotine, charbon actif, harpagophytum) sont référencés par des
    codes prévus mais absents de la base : requires_kb les liste.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
ANNOT = {"annotated_by": "assistant-ia (brouillon)", "expert_validated": False}


def comp(substance, label, amount, unit, plant_part=None, preparation=None, der_ratio=None):
    return {"substance": substance, "label": label, "amount": amount, "unit": unit, "plant_part": plant_part,
            "preparation": preparation, "der_ratio": der_ratio}


def flag(fid, severity, quote, basis, substance, reason, detectable_by, node_startswith=None, finding_code=None):
    return {"flag_id": fid, "severity": severity, "quote": quote, "legal_basis": basis, "substance": substance,
            "reason": reason, "detectable_by": detectable_by, "node_startswith": node_startswith,
            "finding_code": finding_code}


def rw(targets, expected=(), allowed=(), future=(), cofactor=None, contain_any=(), contain_all=(), must_not=(),
       notice_codes=()):
    return {"rewrite_expected": bool(targets), "rewrite_targets": [{"node_startswith": t} for t in targets],
            "expected_claim_ids": list(expected), "allowed_claim_ids": list(allowed),
            "future_claim_ids_when_kb_ready": list(future), "cofactor_strategy": cofactor,
            "seo_bridge": {"must_contain_any": list(contain_any), "must_contain_all": list(contain_all)},
            "must_not_contain": list(must_not), "mandatory_notice_required": bool(notice_codes),
            "mandatory_notice_codes": list(notice_codes)}


TOL_PAGE = [{"finding_code": "PRESENTATION_MEDICAMENTEUSE", "reason": "Agrégat de page attendu quand des rouges coexistent"}]

CASES = [
    # ------------------------------------------------------------------ 1
    {"test_id": "GS-001", "archetype": 1, "category": "THERAPEUTIQUE_FRONTALE_GARANTIE_CHIFFREE",
     "page_mode": "PRODUCT_PAGE", "page_url": "https://boutique.example/products/melatonine-pure",
     "target_seo_query": "mélatonine sommeil", "product_names": ["Mélatonine Pure"], "portions_per_day": 1,
     "product_composition": [comp("MELATONINE", "mélatonine", 1.9, "mg")], "linked_products": [],
     "input_html_nodes": """<div class="product__description rte">
  <h2>Mélatonine Pure 1,9 mg</h2>
  <p><strong>Soigne l'insomnie chronique dès la 1ère nuit</strong> et réduit le cortisol de 40 % grâce à notre formule <a href="/pages/notre-science">brevetée</a>.</p>
  <ul>
    <li>Prendre 2 gélules 30 minutes avant le coucher, soit 1,9 mg de mélatonine.</li>
    <li>Flacon de 60 gélules – <span class="price">24,90 €</span></li>
  </ul>
</div>""",
     "expected_ground_truth": {
         "frozen_zones": ["1,9 mg", "2 gélules", "30 minutes avant le coucher", "60 gélules", "24,90 €"],
         "expected_flags": [
             flag("GS001-F1", "RED", "Soigne", "INCO_1169_2011_ART7_3", "MELATONINE",
                  "Propriété curative attribuée à un complément alimentaire.", "LEXIQUE"),
             flag("GS001-F2", "RED", "insomnie chronique", "INCO_1169_2011_ART7_3", "MELATONINE",
                  "Pathologie (CIM-10 G47.0).", "LEXIQUE"),
             flag("GS001-F3", "ORANGE", "dès la 1ère nuit", "CCONSO_PRATIQUE_TROMPEUSE", "MELATONINE",
                  "Promesse de délai d'action.", "LEXIQUE"),
             flag("GS001-F4", "RED", "réduit le cortisol de 40 %", "R1924_2006_ART10_1", "MELATONINE",
                  "Allégation de santé non autorisée, chiffrée, portant sur un biomarqueur.", "LEXIQUE"),
             flag("GS001-F5", "ORANGE", "", "VIGILANCE_SANITAIRE", "MELATONINE",
                  "Avertissements de vigilance mélatonine absents de la page.", "REGLES_KB",
                  finding_code="VIGILANCE_MENTION_ABSENTE")],
         "tolerated_extra_flags": TOL_PAGE + [{"finding_code": "REFERENCE_GENERALE_ART10_3", "quote": "nuit",
                                               "reason": "« nuit » est déjà couvert par GS001-F3"}],
         "expected_remediation_codes": [],
         "expected_rewrite_constraints": rw(["Soigne l'insomnie"], expected=["MELATONINE-ENDORM"],
                                            contain_any=["endormissement"],
                                            must_not=["soigne", "insomnie", "cortisol", "1ère nuit", "40 %"],
                                            notice_codes=["MELATONINE_COUCHER"])},
     "requires_kb": [],
     "annotation": {**ANNOT, "notes": "1,9 mg pour 2 gélules prises ensemble : une portion quantifiée, condition "
                                      "de 1 mg remplie. Seuil national de dose journalière à vérifier."}},
    # ------------------------------------------------------------------ 2
    {"test_id": "GS-002", "archetype": 2, "category": "SOUS_DOSAGE_CONDITION_VIOLEE",
     "page_mode": "PRODUCT_PAGE", "page_url": "https://boutique.example/products/magnesium-marin-plus",
     "target_seo_query": "magnésium fatigue", "product_names": ["Magnésium Marin+"], "portions_per_day": 1,
     "product_composition": [comp("MG", "magnésium marin", 40, "mg")], "linked_products": [],
     "input_html_nodes": """<div class="product__description">
  <p>Notre <strong>Magnésium Marin+</strong> est une formule simple : <strong>Le magnésium contribue à réduire la fatigue.</strong></p>
  <ul>
    <li>40 mg de magnésium marin par jour</li>
    <li>1 gélule par jour au petit-déjeuner</li>
    <li><span class="price">14,90 €</span> le flacon de 30 gélules</li>
  </ul>
</div>""",
     "expected_ground_truth": {
         "frozen_zones": ["Magnésium Marin+", "40 mg", "1 gélule par jour", "14,90 €", "30 gélules"],
         "expected_flags": [
             flag("GS002-F1", "RED", "Le magnésium contribue à réduire la fatigue", "R432_2012_CONDITIONS", "MG",
                  "Libellé exact, mais 40 mg < 56,25 mg (15 % de la VNR) : condition d'utilisation non remplie.",
                  "REGLES_KB")],
         "tolerated_extra_flags": [], "expected_remediation_codes": [],
         "expected_rewrite_constraints": rw(["Notre Magnésium Marin+"], contain_any=["magnésium"],
                                            must_not=["contribue à réduire la fatigue", "fatigue"])},
     "requires_kb": [], "annotation": {**ANNOT, "notes": "Alternative hors réécriture : reformuler à ≥ 56,25 mg."}},
    # ------------------------------------------------------------------ 3
    {"test_id": "GS-003", "archetype": 3, "category": "REFERENCE_GENERALE_ISOLEE_ART10_3",
     "page_mode": "PRODUCT_PAGE", "page_url": "https://boutique.example/products/acerola-bio-1000",
     "target_seo_query": "complément immunité", "product_names": ["Acérola Bio 1000"], "portions_per_day": 1,
     "product_composition": [comp("VIT_C", "vitamine C (acérola)", 80, "mg")], "linked_products": [],
     "input_html_nodes": """<div class="product-single__meta">
  <span class="product-badge">Votre allié détox, immunité et vitalité au quotidien</span>
  <h1>Acérola Bio 1000</h1>
  <p>Comprimés à croquer d'acérola bio, riches en vitamine C naturelle.</p>
  <ul><li>2 comprimés par jour</li><li>80 mg de vitamine C pour 2 comprimés (100 % des VNR)</li></ul>
</div>""",
     "expected_ground_truth": {
         "frozen_zones": ["Acérola Bio 1000", "2 comprimés par jour", "80 mg", "100 % des VNR"],
         "expected_flags": [
             flag("GS003-F1", "ORANGE", "détox", "DOCTRINE_ADMINISTRATIVE", None, "Terme doctrinal à risque.", "LEXIQUE"),
             flag("GS003-F2", "ORANGE", "immunité", "R1924_2006_ART10_3", "VIT_C",
                  "Référence générale sans allégation spécifique sur la page.", "REGLES_KB"),
             flag("GS003-F3", "ORANGE", "vitalité", "R1924_2006_ART10_3", None,
                  "Référence générale sans allégation spécifique possible (aucun ancrage « vitalité »).", "REGLES_KB")],
         "tolerated_extra_flags": [], "expected_remediation_codes": [],
         "expected_rewrite_constraints": rw(["Votre allié détox"], expected=["VIT_C-IMMUN"],
                                            contain_any=["immunité", "système immunitaire"],
                                            must_not=["détox", "vitalité"])},
     "requires_kb": [], "annotation": {**ANNOT, "notes": "Gravité ORANGE conforme au plan V2 (art. 10§3 = revue)."}},
    # ------------------------------------------------------------------ 4
    {"test_id": "GS-004", "archetype": 4, "category": "COLLAGENE_SANS_COFACTEUR",
     "page_mode": "PRODUCT_PAGE", "page_url": "https://boutique.example/products/collagene-marin-pur",
     "target_seo_query": "collagène marin", "product_names": ["Collagène Marin Pur"], "portions_per_day": 1,
     "product_composition": [comp("COLLAGENE_MARIN", "peptides de collagène marin hydrolysé", 10, "g")],
     "linked_products": [],
     "input_html_nodes": """<div class="product__description">
  <h2>Collagène Marin Pur</h2>
  <p>Notre collagène marin hydrolysé <strong>régénère les cartilages usés</strong> et comble les rides profondes.</p>
  <ul><li>Peptides de collagène marin de type I</li><li>10 g par jour, à diluer dans un verre d'eau</li><li><span class="price">34,90 €</span> la boîte de 30 sticks</li></ul>
</div>""",
     "expected_ground_truth": {
         "frozen_zones": ["Collagène Marin Pur", "10 g par jour", "34,90 €", "30 sticks"],
         "expected_flags": [
             flag("GS004-F1", "RED", "régénère les cartilages usés", "R1924_2006_ART10_1", "COLLAGENE_MARIN",
                  "Allégation de santé non autorisée ; « usés » renvoie à une dégénérescence articulaire.", "EXTRACTEUR_LLM"),
             flag("GS004-F2", "RED", "comble les rides profondes", "R1924_2006_ART10_1", "COLLAGENE_MARIN",
                  "Allégation de santé non autorisée (aucune allégation sur le collagène).", "EXTRACTEUR_LLM")],
         "tolerated_extra_flags": [], "expected_remediation_codes": [],
         "expected_rewrite_constraints": rw(["Notre collagène marin hydrolysé"],
                                            cofactor="AUCUN_COFACTEUR : aucune allégation de santé possible, description factuelle",
                                            contain_any=["collagène marin"],
                                            must_not=["régénère", "cartilage", "rides", "comble"])},
     "requires_kb": ["COLLAGENE_MARIN"], "annotation": {**ANNOT, "notes": "Seul le nom de l'ingrédient est récupérable en SEO."}},
    # ------------------------------------------------------------------ 5
    {"test_id": "GS-005", "archetype": 5, "category": "SAUVETAGE_COFACTEUR",
     "page_mode": "PRODUCT_PAGE", "page_url": "https://boutique.example/products/collagene-beaute-articulations",
     "target_seo_query": "complément collagène peau", "product_names": ["Collagène Beauté & Articulations"],
     "portions_per_day": 1,
     "product_composition": [comp("COLLAGENE_MARIN", "collagène marin hydrolysé", 10, "g"),
                             comp("VIT_C", "vitamine C", 24, "mg"), comp("BIOTINE", "biotine", 50, "µg")],
     "linked_products": [],
     "input_html_nodes": """<div class="product__description">
  <h2>Collagène Beauté &amp; Articulations</h2>
  <p>Pour une peau ferme et des articulations souples, notre <strong>collagène marin</strong> régénère vos cartilages et repulpe la peau.</p>
  <ul><li>10 g de collagène marin hydrolysé, 24 mg de vitamine C (30 % des VNR) et 50 µg de biotine (100 % des VNR) par jour</li><li>1 stick par jour</li></ul>
</div>""",
     "expected_ground_truth": {
         "frozen_zones": ["Collagène Beauté & Articulations", "10 g", "24 mg", "30 % des VNR", "50 µg", "100 % des VNR",
                          "1 stick par jour"],
         "expected_flags": [
             flag("GS005-F1", "RED", "régénère vos cartilages", "R1924_2006_ART10_1", "COLLAGENE_MARIN",
                  "Effet attribué au collagène, sans allégation autorisée.", "EXTRACTEUR_LLM"),
             flag("GS005-F2", "RED", "repulpe la peau", "R1924_2006_ART10_1", "COLLAGENE_MARIN",
                  "Effet attribué au collagène, sans allégation autorisée.", "EXTRACTEUR_LLM"),
             flag("GS005-F3", "ORANGE", "peau ferme et des articulations souples", "R1924_2006_ART10_3", None,
                  "Références générales non accompagnées sur la page d'origine.", "REGLES_KB"),
             flag("GS005-F4", "ORANGE", "Articulations", "R1924_2006_ART1_3", None,
                  "Nom de produit évoquant une fonction : doit être accompagné d'une allégation (art. 1§3). "
                  "Ajouté après adjudication du premier run (relevé par le moteur, manqué par l'annotateur).",
                  "REGLES_KB", node_startswith="Collagène Beauté")],
         "tolerated_extra_flags": [], "expected_remediation_codes": [],
         "expected_rewrite_constraints": rw(["Pour une peau ferme"], expected=["VIT_C-COLL-PEAU"],
                                            allowed=["VIT_C-COLL-CARTILAGE"], future=["BIOTINE-PEAU"],
                                            cofactor="Effet porté par la vitamine C (et la biotine au lot 2), jamais par le collagène",
                                            contain_all=["collagène", "peau"],
                                            must_not=["régénère", "repulpe", "peau ferme", "collagène contribue",
                                                      "collagène, qui contribue", "collagène qui contribue"])},
     "requires_kb": ["COLLAGENE_MARIN", "BIOTINE"],
     "annotation": {**ANNOT, "notes": "La reprise exacte « complément collagène peau » en titre reste une décision "
                                      "de l'expert ; le test exige seulement que les deux termes survivent."}},
    # ------------------------------------------------------------------ 6
    {"test_id": "GS-006", "archetype": 6, "category": "PLANTE_ON_HOLD_VERBE_MEDICAL_VIGILANCE",
     "page_mode": "PRODUCT_PAGE", "page_url": "https://boutique.example/products/ashwagandha-ksm66",
     "target_seo_query": "ashwagandha stress", "product_names": ["Ashwagandha KSM-66"], "portions_per_day": 1,
     "product_composition": [comp("WITHANIA_SOMNIFERA", "extrait sec de racine d'ashwagandha", 600, "mg",
                                  plant_part="RACINE", preparation="EXTRAIT_SEC", der_ratio=12)],
     "linked_products": [],
     "input_html_nodes": """<div class="product__description">
  <p><strong>Anxiolytique naturel puissant</strong> contre le burn-out et l'anxiété : notre ashwagandha KSM-66 fait chuter le cortisol.</p>
  <ul><li>600 mg d'extrait sec de racine d'ashwagandha (ratio 12:1) par gélule</li><li>1 gélule par jour</li><li><span class="price">27,90 €</span></li></ul>
</div>""",
     "expected_ground_truth": {
         "frozen_zones": ["600 mg", "ratio 12:1", "1 gélule par jour", "27,90 €"],
         "expected_flags": [
             flag("GS006-F1", "RED", "Anxiolytique", "CSP_L5111_1_PRESENTATION", "WITHANIA_SOMNIFERA",
                  "Classe médicamenteuse.", "LEXIQUE"),
             flag("GS006-F2", "RED", "burn-out", "INCO_1169_2011_ART7_3", "WITHANIA_SOMNIFERA", "Pathologie.", "LEXIQUE"),
             flag("GS006-F3", "RED", "anxiété", "INCO_1169_2011_ART7_3", "WITHANIA_SOMNIFERA", "Pathologie.", "LEXIQUE"),
             flag("GS006-F4", "RED", "fait chuter le cortisol", "R1924_2006_ART10_1", "WITHANIA_SOMNIFERA",
                  "Allégation sur un biomarqueur, sans correspondance on-hold.", "EXTRACTEUR_LLM"),
             flag("GS006-F5", "ORANGE", "", "VIGILANCE_SANITAIRE", "WITHANIA_SOMNIFERA",
                  "Mentions de vigilance absentes (grossesse, allaitement, enfants, thyroïde…).", "REGLES_KB",
                  finding_code="VIGILANCE_MENTION_ABSENTE")],
         "tolerated_extra_flags": TOL_PAGE, "expected_remediation_codes": [],
         "expected_rewrite_constraints": rw(["Anxiolytique naturel"], future=["ONHOLD-WITHANIA (relaxation) — à importer"],
                                            contain_any=["ashwagandha"],
                                            must_not=["anxiolytique", "burn-out", "anxiété", "cortisol"])},
     "requires_kb": [],
     "annotation": {**ANNOT, "notes": "Équivalent racine : 600 mg × 12 = 7,2 g. Tant que la liste on-hold n'est "
                                      "pas importée, aucune allégation botanique n'est attendue : le moteur ne doit "
                                      "pas en inventer. Ashwagandha retenu plutôt que curcuma (lot 2)."}},
    # ------------------------------------------------------------------ 7
    {"test_id": "GS-007", "archetype": 7, "category": "CONDITION_DE_PRISE_TRONQUEE",
     "page_mode": "PRODUCT_PAGE", "page_url": "https://boutique.example/products/charbon-vegetal-active",
     "target_seo_query": "charbon actif ballonnements", "product_names": ["Charbon Végétal Activé"],
     "portions_per_day": 1, "product_composition": [comp("CHARBON_ACTIF", "charbon végétal activé", 1000, "mg")],
     "linked_products": [],
     "input_html_nodes": """<div class="product__description">
  <p>Le <strong>charbon végétal activé</strong> réduit l'excès de flatulences après le repas.</p>
  <ul><li>500 mg de charbon végétal activé par gélule</li><li>Prendre 2 gélules avant le repas</li><li><span class="price">9,90 €</span> les 60 gélules</li></ul>
</div>""",
     "expected_ground_truth": {
         "frozen_zones": ["500 mg", "2 gélules", "9,90 €", "60 gélules"],
         "expected_flags": [
             flag("GS007-F1", "RED", "réduit l'excès de flatulences après le repas", "R432_2012_CONDITIONS",
                  "CHARBON_ACTIF", "Information obligatoire absente (1 g au moins 30 min avant et 1 g peu après le repas).",
                  "REGLES_KB"),
             flag("GS007-F2", "ORANGE", "réduit", "R432_2012_CONDITIONS", "CHARBON_ACTIF",
                  "Formulation renforcée : « réduit » au lieu de « contribue à réduire ».", "EXTRACTEUR_LLM"),
             flag("GS007-F3", "ORANGE", "Prendre 2 gélules avant le repas", "R432_2012_CONDITIONS", "CHARBON_ACTIF",
                  "Posologie incompatible avec la condition (aucune prise après le repas).", "REGLES_KB")],
         "tolerated_extra_flags": [], "expected_remediation_codes": [],
         "expected_rewrite_constraints": rw(["Le charbon végétal activé"], expected=["CHARBON_ACTIF-FLATULENCE"],
                                            contain_any=["flatulence"], must_not=["réduit l'excès"],
                                            notice_codes=["CHARBON_ACTIF_REPAS"])},
     "requires_kb": ["CHARBON_ACTIF"],
     "annotation": {**ANNOT, "notes": "Libellé FR officiel de l'allégation charbon actif à vérifier (lot 3)."}},
    # ------------------------------------------------------------------ 8
    {"test_id": "GS-008", "archetype": 8, "category": "ARTICLE_BLOG_PONT_COMMERCIAL",
     "page_mode": "EDITORIAL_BLOG", "page_url": "https://boutique.example/blogs/conseils/arthrose-cervicale",
     "target_seo_query": "soulager arthrose cervicale naturellement", "product_names": ["Complexe Harpagophytum"],
     "portions_per_day": 1, "product_composition": [],
     "linked_products": [{"path": "/products/complexe-harpagophytum", "name": "Complexe Harpagophytum",
                          "composition": [comp("VIT_C", "vitamine C", 80, "mg"),
                                          comp("HARPAGOPHYTUM_PROCUMBENS", "harpagophytum", 600, "mg",
                                               plant_part="RACINE", preparation="POUDRE_PLANTE_SECHE")]}],
     "input_html_nodes": """<article class="article-template">
  <h1>Comment soulager l'arthrose cervicale naturellement ?</h1>
  <p>L'arthrose cervicale touche de nombreux adultes après 50 ans. Pour la soulager naturellement, <a href="/products/complexe-harpagophytum">découvrez notre cure articulaire</a>.</p>
  <h2>Bouger chaque jour</h2>
  <p>Une marche quotidienne de 30 minutes entretient la mobilité.</p>
</article>""",
     "expected_ground_truth": {
         "frozen_zones": ["30 minutes"],
         "expected_flags": [
             flag("GS008-F1", "RED", "soulager", "INCO_1169_2011_ART7_3", None, "Verbe thérapeutique dans un article relié à un produit.",
                  "LEXIQUE", node_startswith="Comment soulager"),
             flag("GS008-F2", "RED", "arthrose cervicale", "INCO_1169_2011_ART7_3", None, "Pathologie en titre d'un article commercial.",
                  "LEXIQUE", node_startswith="Comment soulager"),
             flag("GS008-F3", "RED", "soulager", "INCO_1169_2011_ART7_3", None, "Verbe thérapeutique dans le segment du lien produit.",
                  "LEXIQUE", node_startswith="L'arthrose cervicale"),
             flag("GS008-F4", "RED", "arthrose cervicale", "INCO_1169_2011_ART7_3", None, "Pathologie dans le segment du lien produit.",
                  "LEXIQUE", node_startswith="L'arthrose cervicale"),
             flag("GS008-F5", "RED", "", "CSP_L5111_1_PRESENTATION", None,
                  "Article ciblant une pathologie et relié à un produit : présentation d'ensemble commerciale.",
                  "LEXIQUE", finding_code="ARTICLE_CIBLE_PATHOLOGIE_AVEC_PONT")],
         "tolerated_extra_flags": TOL_PAGE,
         "expected_remediation_codes": ["SUPPRIMER_PONT", "RECADRER_VERS_PONT_SEMANTIQUE"],
         "expected_rewrite_constraints": rw(["L'arthrose cervicale", "Comment soulager"],
                                            allowed=["VIT_C-COLL-CARTILAGE"],
                                            contain_any=["articulations", "articulaire", "cartilage"],
                                            must_not=["soulager", "arthrose"])},
     "requires_kb": [],
     "annotation": {**ANNOT, "notes": "Double arbitrage : la suppression du lien est une action utilisateur (le "
                                      "réécrivain ne touche jamais au DOM) ; la réécriture teste l'option « pont "
                                      "sémantique » avec lien conservé."}},
    # ------------------------------------------------------------------ 9
    {"test_id": "GS-009", "archetype": 9, "category": "STRESS_TEST_DOM_ZONES_GELEES",
     "page_mode": "PRODUCT_PAGE", "page_url": "https://boutique.example/products/b12-gouttes",
     "target_seo_query": "vitamine B12 gouttes", "product_names": [], "portions_per_day": 2,
     "product_composition": [comp("VIT_B12", "méthylcobalamine", 150, "µg")], "linked_products": [],
     "input_html_nodes": """<div class="product-form__info" data-variant-id="409281" data-product-handle="b12-gouttes">
  <p class="price-wrapper"><span class="price price--sale">29,90 €</span> au lieu de <s class="price--compare">39,90 €</s> <span class="badge badge--sale">(-25%)</span></p>
  <p>Vitamine B12 méthylcobalamine : <strong>3 gouttes 2 fois par jour</strong>, ne pas dépasser 150 µg par jour. Booste votre énergie !</p>
  <p class="variant-sku" data-sku="B12-GTT-30ML">Flacon de 30 ml – 1 goutte = 25 µg</p>
</div>""",
     "expected_ground_truth": {
         "frozen_zones": ["29,90 €", "39,90 €", "-25%", "3 gouttes 2 fois par jour", "150 µg par jour", "30 ml",
                          "1 goutte", "25 µg"],
         "expected_flags": [flag("GS009-F1", "ORANGE", "Booste votre énergie", "DOCTRINE_ADMINISTRATIVE", "VIT_B12",
                                 "Exagération (« booste ») d'une fonction couverte par une allégation autorisée.", "LEXIQUE")],
         "tolerated_extra_flags": [{"finding_code": "REFERENCE_GENERALE_ART10_3", "quote": "énergie",
                                    "reason": "Couvert par GS009-F1"}],
         "expected_remediation_codes": [],
         "expected_rewrite_constraints": rw(["Vitamine B12 méthylcobalamine"], expected=["VIT_B12-ENERGIE"],
                                            contain_any=["énergétique"], must_not=["booste"])},
     "requires_kb": [], "annotation": {**ANNOT, "notes": "Seul le 2e paragraphe est réécrit ; prix, remise, SKU et "
                                                         "attributs data-* doivent survivre à l'identique."}},
    # ------------------------------------------------------------------ 10
    {"test_id": "GS-010", "archetype": 10, "category": "VRAI_NEGATIF_ANTI_PARANOIA",
     "page_mode": "PRODUCT_PAGE", "page_url": "https://boutique.example/products/magnesium-bisglycinate-b6",
     "target_seo_query": "magnésium B6", "product_names": [], "portions_per_day": 1,
     "product_composition": [comp("MG", "bisglycinate de magnésium (en magnésium)", 300, "mg"),
                             comp("VIT_B6", "vitamine B6", 1.4, "mg")], "linked_products": [],
     "input_html_nodes": """<div class="product__description">
  <h2>Magnésium Bisglycinate + B6</h2>
  <p>Formulé avec du magnésium, qui contribue à réduire la fatigue, et de la vitamine B6, qui contribue au fonctionnement normal du système nerveux. Une formule pensée pour <strong>vos journées bien remplies</strong>.</p>
  <ul>
    <li>300 mg de magnésium (80 % des VNR) et 1,4 mg de vitamine B6 (100 % des VNR) pour 2 gélules</li>
    <li>Conseils d'utilisation : 2 gélules par jour avec un grand verre d'eau</li>
  </ul>
  <p>Ne pas dépasser la dose journalière recommandée. Tenir hors de portée des jeunes enfants. Les compléments alimentaires ne doivent pas être utilisés comme substituts d'un régime alimentaire varié et équilibré et d'un mode de vie sain.</p>
</div>""",
     "expected_ground_truth": {
         "frozen_zones": ["300 mg", "80 % des VNR", "1,4 mg", "100 % des VNR", "2 gélules",
                          "Ne pas dépasser la dose journalière recommandée", "Tenir hors de portée des jeunes enfants"],
         "expected_flags": [], "tolerated_extra_flags": [], "expected_remediation_codes": [],
         "expected_rewrite_constraints": rw([])},
     "requires_kb": [],
     "annotation": {**ANNOT, "notes": "Flexibilité syntaxique limitée à ce que la base valide : prédicats officiels "
                                      "intacts, sujet en apposition (« du magnésium, qui contribue… »)."}},
]


def S(kind, **kw):
    return {"kind": kind, **kw}


T, A, Z = (lambda t: S("TEXTE_LIBRE", text=t)), (lambda r: S("ALLEGATION", ref=r)), (lambda m: S("ZONE_GELEE", match=m))
O, C = (lambda r: S("BALISE_OUVRANTE", ref=r)), (lambda r: S("BALISE_FERMANTE", ref=r))

REPLAYS = {
    "GS-001": [("Soigne l'insomnie", [O("M1"), A("MELATONINE-ENDORM-V00"), C("M1"), T(" grâce à notre formule "),
                                      O("M2"), T("brevetée"), C("M2"), T(".")])],
    "GS-002": [("Notre Magnésium Marin+", [T("Notre "), O("M1"), Z("Magnésium Marin+"), C("M1"),
                                           T(" est une formule simple : "), O("M2"), T("du magnésium marin, tout simplement."),
                                           C("M2")])],
    "GS-003": [("Votre allié détox", [T("Votre allié immunité au quotidien, "), A("VIT_C-IMMUN-V03")])],
    "GS-004": [("Notre collagène marin hydrolysé", [T("Notre collagène marin hydrolysé "), O("M1"),
                                                    T("en peptides de type I"), C("M1"), T(".")])],
    "GS-005": [("Pour une peau ferme", [T("Notre complément collagène peau et articulations associe le "), O("M1"),
                                        T("collagène marin"), C("M1"), T(" à "), A("VIT_C-COLL-PEAU-V02"), T(". "),
                                        A("VIT_C-COLL-CARTILAGE-V00"), T(".")])],
    "GS-006": [("Anxiolytique naturel", [O("M1"), T("Notre "), Z("ashwagandha KSM-66"), C("M1"),
                                         T(" : un extrait de racine d'ashwagandha.")])],
    "GS-007": [],   # allégation charbon actif absente de la base : aucune sortie enregistrable
    "GS-008": [("L'arthrose cervicale", [T("Pour vos articulations, misez sur "), A("VIT_C-COLL-CARTILAGE-V02"),
                                         T(" : "), O("M1"), T("découvrez notre cure articulaire"), C("M1"), T(".")]),
               ("Comment soulager", [T("Comment prendre soin de vos articulations ?")])],
    "GS-009": [("Vitamine B12 méthylcobalamine", [T("Vitamine B12 méthylcobalamine : "), O("M1"),
                                                  Z("3 gouttes 2 fois par jour"), C("M1"), T(", ne pas dépasser "),
                                                  Z("150 µg par jour"), T(". "), A("VIT_B12-ENERGIE-V00"), T(".")])],
    "GS-010": [],
}

if __name__ == "__main__":
    golden = {"format": "golden-set-1.0", "description": "10 archétypes fondateurs (brouillon IA, à valider)",
              "cases": CASES}
    replays = {"format": "rewriter-spans-1.0",
               "recorded_with": "Fixtures manuelles : aucune sortie LLM réelle, aucun usage de tokens enregistré",
               "cases": {cid: {"targets": [{"node_startswith": n, "llm_output": {"status": "OK", "spans": spans}}
                                           for n, spans in targets], "usage": None}
                         for cid, targets in REPLAYS.items()}}
    (OUT / "golden_set.json").write_text(json.dumps(golden, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "replays.json").write_text(json.dumps(replays, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(CASES)} cas, {sum(len(c['expected_ground_truth']['expected_flags']) for c in CASES)} drapeaux attendus")
