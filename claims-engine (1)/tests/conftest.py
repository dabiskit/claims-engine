import os
import tempfile
from pathlib import Path

os.environ.setdefault("AUTO_SYNC", "0")      # pas d'appel réseau aux sources officielles pendant les tests
# base de test neuve à chaque session (jamais webapp/demo.db, qui garde les comptes d'une exécution à l'autre)
os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(Path(tempfile.mkdtemp()) / "test.db"))
os.environ.setdefault("ADMIN_EMAILS", "admin@exemple.fr")
os.environ.setdefault("SESSION_SECRET", "test-secret")
