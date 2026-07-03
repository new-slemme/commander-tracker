"""Test package bootstrap — runs before any test imports ``app``.

The suite must NEVER touch the production database. Historically each test set
``SQLALCHEMY_DATABASE_URI`` in ``setUpClass`` *after* importing ``app``, but by
then Flask-SQLAlchemy had already created its engine bound to the production
path (importing ``app`` runs the schema migrations, which builds the engine).
The override was silently ignored and ``db.drop_all()`` hit ``/data/commander.db``.

Fix: bind to an isolated temp database via the ``COMMANDER_DB_URI`` env var
*here*, before ``app`` is ever imported, and hard-fail if anything would point
the suite at the production file.
"""

import os
import pathlib
import tempfile

_PROD_DB = "/data/commander.db"
# Per-process filename: two concurrent test invocations (e.g. parallel agents in
# separate worktrees) must not share one SQLite file, or they race on the engine
# bound at first import — the same failure class that once wiped production.
_test_db = pathlib.Path(tempfile.gettempdir()) / f"commander_test_suite_{os.getpid()}.db"

# Bind the engine to an isolated temp DB from the very first import of app.
os.environ.setdefault("COMMANDER_DB_URI", f"sqlite:///{_test_db}")
# Don't snapshot the throwaway test DB on import.
os.environ.setdefault("COMMANDER_SKIP_STARTUP_BACKUP", "1")

# Fail closed: refuse to run the suite against the production database.
_uri = os.environ["COMMANDER_DB_URI"]
if _PROD_DB in _uri:
    raise RuntimeError(
        f"Refusing to run tests against the production database ({_uri}). "
        "Tests must use an isolated COMMANDER_DB_URI."
    )
