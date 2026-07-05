import os
import sys
from pathlib import Path

import django


APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

RUNNING_IN_DOCKER = Path("/.dockerenv").exists()

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("DJANGO_DEBUG", "0")

if not RUNNING_IN_DOCKER:
    if os.getenv("POSTGRES_HOST") in {None, "", "db"}:
        os.environ["POSTGRES_HOST"] = "localhost"
    if os.getenv("POSTGRES_PORT") in {None, "", "5432"}:
        os.environ["POSTGRES_PORT"] = "5433"

collect_ignore_glob = [
    "tests/manual_*.py",
    "tests/verify_*.py",
    "tests/docling_*.py",
]


# Patch pgvector HnswIndex for SQLite compatibility in tests
try:
    import pgvector.django.indexes
    from django.db.models import Index
    
    original_create_sql = pgvector.django.indexes.HnswIndex.create_sql
    
    def sqlite_create_sql(self, model, schema_editor, using="", **kwargs):
        if schema_editor.connection.vendor == "sqlite":
            orig_opclasses = self.opclasses
            self.opclasses = []
            sql = Index.create_sql(self, model, schema_editor, using=using)
            self.opclasses = orig_opclasses
            return sql
        return original_create_sql(self, model, schema_editor, using=using, **kwargs)
        
    pgvector.django.indexes.HnswIndex.create_sql = sqlite_create_sql
except ImportError:
    pass

def pytest_configure(config):
    django.setup()
    config.addinivalue_line(
        "markers",
        "unit: marks fast tests that validate isolated logic",
    )
    config.addinivalue_line(
        "markers",
        "integration: marks tests that depend on real parser/model integration",
    )
    config.addinivalue_line(
        "markers",
        "manual: marks scripts intended for manual execution only",
    )
