"""EvalGate API service.

v0: register suites, submit runs, promote baselines, fetch case-level diffs, and
serve the gate verdict that `eval-gate.yml` turns into a merge decision.
"""

from .app import app, create_app
from .store import Baseline, MemoryStore, Store

__version__ = "0.1.0"

__all__ = ["Baseline", "MemoryStore", "Store", "__version__", "app", "create_app"]
