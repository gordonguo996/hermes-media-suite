"""Make ``import agent...`` work no matter where pytest is started from.

1.2.215 shipped tests that only collected when pytest ran inside ``client/``;
running ``pytest tests client/tests`` from the repo root stopped at collection
with ``ModuleNotFoundError: agent``.  Pinning the package root here fixes the
order-dependent failure for every test file.
"""
import sys
from pathlib import Path

CLIENT_ROOT = Path(__file__).resolve().parents[1]
if str(CLIENT_ROOT) not in sys.path:
    sys.path.insert(0, str(CLIENT_ROOT))
