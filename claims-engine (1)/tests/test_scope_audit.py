"""tests/test_scope_audit.py — périmètre produit, composition détectée, base officielle fusionnée, audit en lot."""
import io
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(Path(tempfile.mkdtemp()) / "test.db"))
os.environ.setdefault("ADMIN_EMAILS", "admin@exemple.fr")
os.environ["SYNC_JOBS"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from tests.test_kb_sync import SEED, annex_html, reg_item  # noqa: E402
from webapp import audit, kb_sync, main  # noqa: E402

FIXTURE = (ROOT / "tests" / "fixtures_shop_page.html").read_text(encoding="utf-8")
URL = "https://boutique.exemple.fr/products/collagene-test"


def token(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def client_for(email):
    c = TestClient(main.app)
    t = token(c.get("/essai").text)
    c.post("/essai", data={"csrf": t, "name": "T", "email": email, "password": "motdepasse-long", "terms": "1"})
    return c


def official_seed():
    rows = kb_sync.parse_annex(annex_html())
    register = [kb_sync.register_record(kb_sync.flatten_register_item(
        reg_item("POL-HC-1", "HCCS_NON_AUTHORISED", "HCLBT_13_1", "Collagen hydrolysate", "Collagen helps skin elasticity", "999")))]
    ds = kb_sync.build_dataset("02012R0432-20250820", rows, register, SEED, "2026-11-07T12:00:00+00:00")
    return kb_sync.merge_seed(main.BASE_SEED, ds)


def test_url_analysis_is_scoped_to_product(monkeypatch):
    monkeypatch.setattr(main, "fetch_url", lambda url: FIXTURE)
    main.apply_seed(official_seed())
    try:
        c = client_for("scope@exemple.fr")
        page = c.get("/app").text
        r = c.post("/app/analyser", data={"csrf": token(page), "source": "url", "url": URL, "mode": "AUTO"})
        assert r.status_code == 200
        html = r.text
        assert "<h1 style=\"margin:4px 0\">Collagène Test</h1>" in html          # carte produit
        assert "Fiche produit détectée" in html and "MarqueTest" in html
        for menu_term in ("Coupe-faim", "Ventre plat", "cardiovasculaire", "renforcement musculaire", "Livraison offerte"):
            assert menu_term not in html, menu_term                              # menus, produits suggérés, bandeaux écartés
        assert "prévenir" in html                                                # le contenu produit reste analysé
        assert "Composition détectée dans la page" in html and "60 mg" in html
        assert "Vitamine A" in html and "120 µg" in html                       # substance issue de l'import officiel
        assert "Aucune allégation autorisée" in html and "Collagène" in html     # registre : demandes refusées
        assert "Avis clients (2)" in html and "arthrose" in html                 # avis présentés à part
        assert "version consolidée du 20/08/2025" in html                        # citation de la source
    finally:
        main.apply_seed(main.BASE_SEED)


def make_docx(paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_bulk_audit_urls_sitemap_and_files(monkeypatch):
    sitemap = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               '<url><loc>https://boutique.exemple.fr/products/a</loc></url><url><loc>https://boutique.exemple.fr/pages/cgv</loc></url>'
               '</urlset>')
    pages = {"https://boutique.exemple.fr/sitemap.xml": sitemap}
    monkeypatch.setattr(main, "fetch_url", lambda url: pages.get(url, FIXTURE))
    monkeypatch.setattr(audit.time, "sleep", lambda s: None)
    c = client_for("audit@exemple.fr")
    form = c.get("/app/audit").text
    docx = make_docx(["Notre tisane soigne l'insomnie et guérit l'anxiété.", "Conseils d'utilisation : 1 tasse le soir."])
    r = c.post("/app/audit", data={"csrf": token(form), "title": "Audit test", "urls": URL + "\nhttps://boutique.exemple.fr/products/b",
                                   "sitemap": "https://boutique.exemple.fr/sitemap.xml", "contains": "/products/", "max": "10", "mode": "AUTO"},
               files=[("files", ("fiche.docx", docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
                      ("files", ("liste.txt", b"https://boutique.exemple.fr/products/c\n", "text/plain"))])
    assert r.status_code == 200, r.text[:500]
    html = r.text
    assert "Audit test" in html and "5 / 5 page(s) traitée(s)" in html
    assert "fiche.docx" in html and "/pages/cgv" not in html                      # filtre du sitemap appliqué
    assert "soigne" in html                                                       # constat du document Word
    aid = re.search(r'/app/audits/(\d+)\.csv', html).group(1)
    csv_text = c.get(f"/app/audits/{aid}.csv").text
    assert csv_text.count("\n") == 6 and "fiche.docx" in csv_text
    other = client_for("intrus@exemple.fr")
    assert other.get(f"/app/audits/{aid}").status_code == 404                     # isolation entre comptes


def test_sources_and_rejected_pages():
    main.apply_seed(official_seed())
    try:
        c = client_for("sources@exemple.fr")
        r = c.get("/app/base/sources")
        assert r.status_code == 200 and "Règlement (UE) n° 432/2012" in r.text
        r = c.get("/app/base/refusees?q=collagen")
        assert "Collagen helps skin elasticity" in r.text
        base = c.get("/app/base").text
        assert "Import officiel" in base and "Vitamine A" in base
        assert c.get("/app/base/VITAMINE_A").status_code == 200
    finally:
        main.apply_seed(main.BASE_SEED)
