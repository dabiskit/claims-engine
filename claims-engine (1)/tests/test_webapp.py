"""tests/test_webapp.py — parcours complet de la démo (essai gratuit -> analyse -> avis -> export)."""
import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(tempfile.mkdtemp()) / "test.db")
os.environ["ADMIN_EMAILS"] = "admin@exemple.fr"
os.environ["SESSION_SECRET"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import update  # noqa: E402

from webapp import main  # noqa: E402

client = TestClient(main.app)


def token(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def signup(c, email="expert@exemple.fr"):
    t = token(c.get("/essai").text)
    return c.post("/essai", data={"csrf": t, "name": "Expert", "email": email, "password": "motdepasse-long", "terms": "1"})


def test_health_and_landing():
    assert client.get("/sante").text == "ok"
    assert "Essai gratuit" in client.get("/").text


def test_full_trial_flow():
    c = TestClient(main.app)
    r = signup(c)
    assert r.status_code == 200 and "accès complet" in r.text
    page = c.get("/app?exemple=GS-001").text
    r = c.post("/app/analyser", data={"csrf": token(page), "source": "texte",
               "text": main.EXAMPLES[0]["text"], "mode": "PRODUCT_PAGE", "product_name": "Mélatonine Pure",
               "sub_1": "MELATONINE", "amount_1": "1,9", "unit_1": "mg"})
    assert r.status_code == 200
    assert "À revoir en priorité" in r.text and "<mark" in r.text
    assert "La mélatonine contribue à réduire le temps d&#39;endormissement" in r.text  # allégation utilisable proposée
    base = c.get("/app/base/MG").text
    assert "Le magnésium contribue à réduire la fatigue" in base
    r = c.post("/app/avis", data={"csrf": token(base), "target_type": "allegation", "target_code": "MG-FATIGUE",
               "decision": "A_CORRIGER", "comment": "Vérifier la ponctuation", "next": "/app/base/MG#MG-FATIGUE"})
    assert r.status_code == 200 and "À corriger" in r.text
    csv_text = c.get("/app/avis.csv").text
    assert "MG-FATIGUE" in csv_text and "Vérifier la ponctuation" in csv_text
    for path in ("/app/base", "/app/base/lexique", "/app/base/mentions", "/app/base/taches", "/app/base/WITHANIA_SOMNIFERA"):
        assert c.get(path).status_code == 200, path


def test_true_negative_and_underdose():
    c = TestClient(main.app)
    signup(c, "tn@exemple.fr")
    page = c.get("/app").text
    tn = next(e for e in main.EXAMPLES if e["id"] == "GS-010")
    r = c.post("/app/analyser", data={"csrf": token(page), "source": "texte", "text": tn["text"], "mode": "PRODUCT_PAGE",
               "sub_1": "MG", "amount_1": "300", "unit_1": "mg", "sub_2": "VIT_B6", "amount_2": "1,4", "unit_2": "mg"})
    assert "Aucun passage relevé" in r.text
    under = next(e for e in main.EXAMPLES if e["id"] == "GS-002")
    r = c.post("/app/analyser", data={"csrf": token(page), "source": "texte", "text": under["text"], "mode": "PRODUCT_PAGE",
               "sub_1": "MG", "amount_1": "40", "unit_1": "mg"})
    assert "condition d&#39;utilisation n&#39;est pas remplie" in r.text and "Non utilisable" in r.text


def test_security_and_expiry():
    c = TestClient(main.app)
    signup(c, "expire@exemple.fr")
    assert c.post("/app/analyser", data={"csrf": "faux", "source": "texte", "text": "x"}).url.path == "/app"
    page = c.get("/app").text
    r = c.post("/app/analyser", data={"csrf": token(page), "source": "url", "url": "http://127.0.0.1/admin"})
    assert "non autorisée" in r.text
    with main.ENGINE.begin() as conn:
        conn.execute(update(main.USERS).where(main.USERS.c.email == "expire@exemple.fr")
                     .values(trial_ends_at=main.now() - timedelta(days=1)))
    assert "Votre essai est terminé" in c.get("/app").text
    assert c.get("/admin").url.path == "/app"
    other = TestClient(main.app)
    assert other.get("/app/analyses/1").url.path == "/connexion"


def test_admin():
    c = TestClient(main.app)
    signup(c, "admin@exemple.fr")
    r = c.get("/admin")
    assert "Administration" in r.text and "expert@exemple.fr" in r.text
    assert c.get("/admin/avis.csv").status_code == 200
