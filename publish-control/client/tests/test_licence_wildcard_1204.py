"""1.2.204: activation-issued licences may cover every local seat.

Accounts are created on the customer's own machine (``env_1:douyin``), so a
licence cannot list them in advance.  The vendor signs ``*`` (all platforms)
or ``<platform>:*``; exact ids must keep priority.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.winport.license import account_authorization, account_quota

FUTURE = "2099-01-01T00:00:00+00:00"


def licence(accounts):
    return {"expires_at": FUTURE, "accounts": accounts}


def test_wildcard_all_allows_local_account():
    verdict = account_authorization(licence([{"account_id": "*"}]), "env_1:douyin")
    assert verdict["authorized"] and verdict["enabled"]


def test_platform_wildcard_only_matches_that_platform():
    obj = licence([{"account_id": "douyin:*"}])
    assert account_authorization(obj, "env_1:douyin")["authorized"]
    assert not account_authorization(obj, "env_1:xingtu")["authorized"]


def test_exact_seat_wins_over_wildcard():
    obj = licence([
        {"account_id": "*", "enabled": True},
        {"account_id": "env_1:douyin", "enabled": False},
    ])
    verdict = account_authorization(obj, "env_1:douyin")
    assert verdict["authorized"] and verdict["enabled"] is False


def test_disabled_wildcard_keeps_everything_blocked():
    obj = licence([{"account_id": "*", "enabled": False}])
    assert account_authorization(obj, "env_1:douyin")["enabled"] is False


def test_missing_seat_still_reports_not_licensed():
    verdict = account_authorization(licence([{"account_id": "kuaishou:*"}]),
                                    "env_1:douyin")
    assert verdict["authorized"] is False
    assert "不在授权名单" in verdict["reason"]


def test_quota_still_counts_seats():
    quota = account_quota(licence([{"account_id": "*"}]))
    assert quota["seats_total"] == 1
