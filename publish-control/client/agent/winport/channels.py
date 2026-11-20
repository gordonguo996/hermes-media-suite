"""Account -> engine -> profile -> CDP port registry.

Mac parity note (plan section 11.2 / appendix 1 item 13): channels.py on the
Mac side still lists firefox/douyin-firefox for account 2 while background.md
and publish-ops-rules say WebKit.  background.md wins, so this port ships
WebKit for account 2 from the start (W-P0 freeze item).

One account = one profile.  Two accounts must never share a profile
(Mac background section 1: mixing only ever yields account 1 data).
"""

from __future__ import annotations

from .errors import ConfigError
from .jsonio import read_json

# Product default (Mac plan section 10.0): upload + fill only, never click submit.
DEFAULT_OPERATION_MODE = "fill"
APPROVED_OPERATION_MODE = "approved_publish"

PIECE_TYPES = ("免费", "种草", "激励")

ENGINE_CHROME = "chrome"
ENGINE_WEBKIT = "webkit"
ENGINE_CHROMIUM = "chromium"

DEFAULT_CDP_PORT = 9224


class Account:
    def __init__(self, account_id, nickname, engine, profile_name,
                 cdp_port=None, ready=True, authorized=None):
        self.account_id = account_id
        self.nickname = nickname
        self.engine = engine
        self.profile_name = profile_name
        self.cdp_port = cdp_port
        self.ready = ready
        self.authorized = authorized or {}

    @property
    def profile_key(self) -> str:
        return f"profiles/{self.profile_name}"

    def profile_dir(self, layout):
        return layout.profiles / self.profile_name

    def cdp_url(self) -> str | None:
        if not self.cdp_port:
            return None
        return f"http://127.0.0.1:{self.cdp_port}"

    def __repr__(self):
        return f"<Account {self.account_id} {self.nickname} {self.engine}>"


def account_from_entry(entry: dict) -> Account:
    try:
        return Account(
            account_id=entry["account_id"],
            nickname=entry.get("nickname", entry["account_id"]),
            engine=entry.get("engine", ENGINE_CHROME),
            profile_name=entry.get("profile", entry["account_id"]),
            cdp_port=entry.get("cdp_port"),
            ready=bool(entry.get("ready", True)),
            authorized=entry.get("authorized"),
        )
    except KeyError as exc:
        raise ConfigError(f"accounts.json entry missing key: {exc}")


def load_accounts(path) -> list[Account]:
    raw = read_json(path, default=None)
    if raw is None:
        return []
    if isinstance(raw, dict):
        entries = raw.get("accounts", [])
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ConfigError(f"accounts.json must be an object or array: {path}")
    return [account_from_entry(e) for e in entries]


def account_by_id(accounts, account_id) -> Account:
    for acc in accounts:
        if acc.account_id == account_id:
            return acc
    raise ConfigError(f"unknown account_id: {account_id}")


def assert_account_ready(account: Account) -> None:
    """Account 2 must have written authorization before any stage-2 run.

    Mac parity: unapproved account 2 is rejected BEFORE a browser is opened
    (README_部署.md section 4c -> ACCOUNT_NOT_READY, exit 5).
    """
    if not account.ready:
        raise ConfigError(f"ACCOUNT_NOT_READY: {account.account_id} (ready=false)")
    if account.account_id.endswith("-002"):
        auth = account.authorized or {}
        required = ("auth_no", "expires_at", "usual_login_ip", "phone")
        missing = [k for k in required if not auth.get(k)]
        if missing:
            raise ConfigError(
                f"ACCOUNT_NOT_READY: {account.account_id} missing {missing}")
