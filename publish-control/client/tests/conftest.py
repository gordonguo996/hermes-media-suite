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


import os

import pytest


@pytest.fixture(autouse=True)
def _no_real_trial_registry(monkeypatch):
    """Never read or write the developer's real HKCU trial keys from a test.

    On Windows the licence code keeps a one-time trial record and a
    "formal licence seen" tombstone under HKCU\\Software\\HuolongAgent\\Agent.
    Tests that create trials or activate licences used to WRITE that real key,
    and any machine that ever ran the product (or these tests) then failed
    every trial test with "tombstone/record is unavailable".  Tests get the
    non-Windows behaviour (no registry store); the product code is unchanged,
    so this cannot become a customer-side trial reset switch.
    """
    if os.name != "nt":
        return
    for name, module in list(sys.modules.items()):
        if module is not None and name.endswith("winport.license") and \
                hasattr(module, "_read_trial_registry"):
            monkeypatch.setattr(module, "_read_trial_registry", lambda: None)
            monkeypatch.setattr(module, "_write_trial_registry", lambda record: None)
