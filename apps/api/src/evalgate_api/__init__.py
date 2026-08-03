"""EvalGate API service.

v0: register suites, submit runs, promote baselines, fetch case-level diffs, and
serve the gate verdict that `eval-gate.yml` turns into a merge decision.
"""

from .app import app, build_store, create_app
from .store import SCHEMA, Baseline, MemoryStore, PostgresStore, Store

__version__ = "0.1.0"

__all__ = [
    "SCHEMA",
    "Baseline",
    "MemoryStore",
    "PostgresStore",
    "Store",
    "__version__",
    "app",
    "build_store",
    "create_app",
]
