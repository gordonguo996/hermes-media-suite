"""Make ``import agent.winport`` work however pytest is started.

Several 1.2.204+ test files import ``agent.winport`` without touching
sys.path, so they only collected when pytest ran with cwd=client.  Running
publish-control/tests and client/tests in one session (the release gate)
failed at collection with ModuleNotFoundError: No module named "agent".
"""
import sys
from pathlib import Path

CLIENT = str(Path(__file__).resolve().parents[1])
if CLIENT not in sys.path:
    sys.path.insert(0, CLIENT)
