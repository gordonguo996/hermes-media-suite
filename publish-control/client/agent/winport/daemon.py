"""Resident daemon: 5-minute heartbeat (launchd StartInterval=300 parity).

Each round: downlink fetch -> bind -> (optional) collect -> report.  A round
failure is logged and audited; it never takes down the process, and the next
round retries.  Keep-awake is held for the lifetime of the process.
"""

from __future__ import annotations

import argparse
import time
import traceback
from pathlib import Path

from . import report as report_mod
from .errors import EXIT_OK
from .keepawake import KeepAwake
from .logutil import audit, get_logger
from .paths import Layout


class Daemon:
    def __init__(self, layout: Layout, downlink=None, collector=None,
                 keepawake=None, logger=None, settings=None, channels=None,
                 notify=None, tcp=None, channel_checker=None):
        self.layout = layout
        self.downlink = downlink
        self.collector = collector
        self.keepawake = keepawake or KeepAwake()
        self.logger = logger or get_logger("winport.daemon", layout.logs)
        self.settings = settings or {}
        self.channels = channels or []
        self.notify = notify
        self.tcp = tcp
        #: injected for tests: callable(running_channels) -> login-check rows
        self.channel_checker = channel_checker
        self.rounds = 0
        self.last_summary = None
        self.channel_rows = []
        #: last licence notice level we alerted on (so a threshold fires once)
        self.license_level = None

    @property
    def interval(self) -> int:
        return int((self.settings.get("downlink") or {}).get(
            "poll_interval_seconds", 300))

    def run_round(self) -> dict:
        self.rounds += 1
        summary = {"round": self.rounds, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "downlink": None, "collection": None, "report": None,
                   "errors": []}
        try:
            if self.downlink is not None:
                summary["downlink"] = self.downlink.run_once()
            if self.collector is not None:
                collection = self.collector()
                summary["collection"] = {
                    "row_count": collection["normalized"]["row_count"],
                    "column_count": collection["normalized"]["column_count"],
                    "header": collection["normalized"]["header"],
                }
            rep = report_mod.write_report(self.layout,
                                          collection=summary["collection"])
            summary["report"] = {"md": rep["md"], "xlsx": rep["xlsx"]}
        except Exception as exc:  # crash isolation: never die on one round
            summary["errors"].append(f"{type(exc).__name__}: {exc}")
            audit(self.layout.logs, "heartbeat_error",
                  detail=traceback.format_exc())
            self.logger.error("heartbeat error: %s", exc)
        # 按需取数心跳：只问「是否有取数指令」，不带任何数据；默认不发送。
        # 对客户不可见 —— 失败静默、不写客户可见日志、不落进 run/heartbeat.json。
        try:
            from .report_center import ReportCenter
            ReportCenter(self.layout, settings=self.settings).heartbeat()
        except Exception:                           # 心跳不得影响本地心跳/抓数
            pass
        try:
            summary["browsers"] = self._keep_browsers_alive()
        except Exception as exc:   # a browser launch must never stop the beat
            summary["browsers"] = {"error": f"{type(exc).__name__}: {exc}"}
        summary["channels"] = self.refresh_channels()
        summary["licence"] = self.check_licence()
        try:
            from . import cadence
            summary["cadence"] = cadence.run_confirmation_sweep(
                self.layout, settings=self.settings,
                notify=lambda title, body: self._manual_alert(title, body)
                if hasattr(self, "_manual_alert") else None)
        except Exception as exc:
            summary["cadence"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            from .publish_queue import run_once as publish_sweep
            from .settings import load_settings
            summary["publish_queue"] = publish_sweep(self.layout, load_settings(self.layout.settings_file))
        except Exception as exc:
            summary["publish_queue"] = {"error": f"{type(exc).__name__}: {exc}"}
        self.last_summary = summary
        # 1.2.5: a disk-full / permission error writing the audit or heartbeat
        # file must NOT take the resident loop down (GPT review 1.2.4 item 9).
        try:
            audit(self.layout.logs, "heartbeat", round=self.rounds,
                  downlink_ok=bool(summary["downlink"]),
                  errors=summary["errors"])
        except Exception as exc:
            self.logger.error("audit write failed (continuing): %s", exc)
        try:
            self._write_heartbeat(summary)
        except Exception as exc:
            self.logger.error("heartbeat write failed (continuing): %s", exc)
        return summary

    def _keep_browsers_alive(self) -> dict:
        """Relaunch the CDP browser of every environment that was really used.

        1.2.214: a package install or a machine power cycle kills every Chrome
        process, and only the environment touched next used to come back.
        Publishing into any other account then stopped with
        "浏览器没有启动成功" while its login was perfectly valid.

        Rules that keep this safe:
          * only environments that already reported ONLINE in a real login
            check are restored -- a profile directory or a cookies file is not
            proof of a signed-in account;
          * the launch itself goes through browser.open_environment, which
            holds the per-environment lock, so the daemon, the panel and a
            publish can never start the same account twice;
          * a launch is followed by a cooldown (5 min after failure, 2 min
            after success) measured on the monotonic clock, so a browser that
            dies right after starting cannot cause a launch loop and a window
            the customer closed is not reopened immediately;
          * nothing here may raise: the heartbeat must survive it.
        """
        from . import browser as browser_mod
        from . import chrome
        from .platforms import load_registry

        info = {"checked": 0, "restarted": [], "skipped": [], "errors": []}
        # Hard time budget for the whole round: a dead profile can burn the
        # 20s launch timeout, and N of them would stall the heartbeat.  Once the
        # budget is gone the remaining environments wait for the next round.
        budget = time.monotonic() + float(getattr(self, "browser_budget_seconds", 25.0))
        out_of_time = False
        state = getattr(self, "_browser_cooldown", None)
        if state is None:
            state = self._browser_cooldown = {}
        try:
            envs, _channels = load_registry(self.layout)
        except Exception as exc:
            info["errors"].append(f"registry: {type(exc).__name__}: {exc}")
            return info
        for env in envs:
            if not getattr(env, "enabled", True):
                continue
            port = getattr(env, "debug_port", None)
            if not port:
                continue
            info["checked"] += 1
            env_id = getattr(env, "env_id", "?")
            try:
                if chrome.port_open(port):
                    continue
                if time.monotonic() < float(state.get(env_id) or 0):
                    info["skipped"].append(env_id)
                    continue
                if not self._environment_was_signed_in(env):
                    info["skipped"].append(env_id)   # never signed in here
                    continue
                remaining = budget - time.monotonic()
                if remaining <= 1.0:
                    out_of_time = True
                    info["skipped"].append(env_id)
                    continue
                opened = browser_mod.open_environment(
                    self.layout, env, timeout=min(20.0, remaining))
                ok = bool(opened.get("opened") or opened.get("reused"))
                info["restarted"].append({"env_id": env_id, "ok": ok,
                                          "reason": opened.get("reason") or ""})
                state[env_id] = time.monotonic() + (120 if ok else 300)
                try:
                    audit(self.layout.logs, "browser_supervisor", env_id=env_id,
                          ok=ok, reason=opened.get("reason") or "")
                except Exception as exc:   # audit must not undo a good launch
                    info["errors"].append(f"audit {env_id}: {type(exc).__name__}: {exc}")
                if ok:
                    # One successful start per round: a 15s browser launch must
                    # never stack up N times inside a single heartbeat.  A
                    # failed/skipped environment is cheap, so keep going.
                    break
            except Exception as exc:      # never take the heartbeat down
                info["errors"].append(f"{env_id}: {type(exc).__name__}: {exc}")
                state[env_id] = time.monotonic() + 300
        if out_of_time:
            info["budget_exhausted"] = True
        return info

    def _environment_was_signed_in(self, env) -> bool:
        """True only when this environment produced a VERIFIED login before.

        A cookies file is not proof (an anonymous visit writes one too), so the
        supervisor only restores accounts that already reported ONLINE in a real
        login check -- the same evidence the channel lights are built from.
        """
        try:
            from . import health
            from .platforms import load_registry
            channels = load_registry(self.layout)[1]
            stored = (health.load_state(self.layout).get("channels") or {})
            for channel in channels:
                if getattr(channel, "env_id", None) != getattr(env, "env_id", None):
                    continue
                evidence = ((stored.get(channel.channel_id) or {}).get("evidence")
                            or {}).get("login_check") or {}
                if str(evidence.get("status") or "").upper() == "ONLINE":
                    return True
        except Exception:
            return False
        return False

    def check_licence(self) -> dict:
        """E: remind as the licence approaches expiry; never stop collect/data.

        1.2.5: the WHOLE body (including the licence-file read) is protected, so a
        corrupt / half-written license.json disables NEW publishes only and never
        kills the resident loop (GPT review 1.2.4 item 9).

        1.2.8 (GPT_REVIEW_1.2.7 必修 1): this used the SHARED
        ``licstate.unified_license_state`` (fixed trust root + machine/seat +
        clock) and now RETURNS ``reasons`` / ``clock_rollback`` / ``text`` as
        well -- the old version threw those away, so the display端 could not
        explain WHY publish was refused.
        """
        from .licstate import unified_license_state
        try:
            state = unified_license_state(self.layout)
        except Exception as exc:      # a licence fault must not stop the round
            self.logger.error("licence check failed: %s", exc)
            return {"status": "ERROR", "publish_allowed": False,
                    "collect_allowed": True, "error": str(exc),
                    "reasons": [f"授权状态异常：{exc}；请重新导入授权。"],
                    "clock_rollback": False,
                    "notices": ["授权文件异常：新增发布暂停，抓数与历史数据不受影响；"
                                "请重新导入授权。"]}
        level = state.get("expiry_level")
        if level in ("30", "7", "1", "grace", "expired", "invalid") \
                and level != self.license_level:
            self.license_level = level
            for line in state.get("notices", []):
                self._notify("授权提醒", line)
        return {"status": state.get("status"),
                "level": level,
                "publish_allowed": state.get("publish_allowed"),
                "collect_allowed": True,
                "days_left": state.get("days_left"),
                "notices": state.get("notices", []),
                "reasons": state.get("reasons", []),
                "clock_rollback": state.get("clock_rollback", False),
                "text": state.get("text", "")}

    def refresh_channels(self) -> list:
        """Refresh every platform light; alert ONLY the slots that dropped.

        Order matters: first re-verify the login of every slot whose browser is
        actually open (cookie + backend evidence), store that verdict, and only
        then compute the lights.  A genuine drop -- cookies gone, or the backend
        bouncing us to a login page -- is therefore what produces the alert, and
        the alert names just that one platform.
        """
        if not self.channels:
            return []
        from . import health, state_machine
        try:
            # Read the previous lights BEFORE re-checking: the drop alert is a
            # transition (was ONLINE -> now OFFLINE), so the "before" snapshot
            # must not already contain this round's verdict.
            previous = list((health.load_state(self.layout).get("channels")
                             or {}).values())
            # Classify the PREVIOUS lights from their own stored evidence, so a
            # first observation after an upgrade does not alert on every slot.
            previous_rows = state_machine.annotate(previous, previous={})
            prev_states = {row["channel_id"]: {"state": row["account_state"]}
                           for row in previous_rows}
            running = self._running_channels()
            if running:
                self._recheck(running)
            base_rows = health.probe_all(self.layout, self.channels, tcp=self.tcp)
            # D: one state machine; alerts are grouped per affected ENVIRONMENT.
            rows = state_machine.annotate(base_rows, previous=prev_states)
            alerts = state_machine.notify_state_changes(
                previous_rows, rows, self._notify, env_labels=self._env_labels())
            for alert in alerts:
                states = set(alert.get("states") or [])
                if states - {"VALID", "DISABLED"}:
                    self._notify_manual(alert)
            health.save_state(self.layout, rows)
            state_machine.persist_records(self.layout, rows)
            self.channel_rows = rows
            alerted = {r["channel_id"] for a in alerts for r in a["rows"]}
            for row in rows:
                if row.get("account_state") not in ("VALID", "DISABLED") \
                        and row["channel_id"] not in alerted:
                    self.logger.info("channel %s state=%s: %s", row["channel_id"],
                                     row.get("account_state"), row.get("reason"))
            return rows
        except Exception as exc:   # a light must never take down the heartbeat
            self.logger.error("channel refresh failed: %s", exc)
            audit(self.layout.logs, "channel_refresh_error",
                  detail=traceback.format_exc())
            return self.channel_rows

    def _env_labels(self) -> dict:
        try:
            envs, _ = __import__("winport.platforms", fromlist=["load_registry"]) \
                .load_registry(self.layout)
            return {e.env_id: e.label for e in envs}
        except Exception:
            return {}

    def _running_channels(self) -> list:
        """Slots whose browser is actually open (nothing to check otherwise)."""
        from . import health
        tcp = self.tcp or health.tcp_listening
        running = []
        for channel in self.channels:
            if not channel.enabled:
                continue
            if channel.supports_cdp:
                if tcp("127.0.0.1", channel.debug_port):
                    running.append(channel)
            else:
                from . import proc
                if proc.profile_processes(channel.profile_dir(self.layout)):
                    running.append(channel)
        return running

    def _recheck(self, running) -> None:
        from . import login_check
        if callable(self.channel_checker):
            rows = self.channel_checker(running)
        else:
            rows = [login_check.check_channel(self.layout, channel)
                    for channel in running]
        login_check.persist(self.layout, rows)
        for row in rows:
            self.logger.info("login re-check %s: %s (%s)", row["channel_id"],
                             row["status"], row.get("reason", ""))

    def _notify(self, title, message):
        if callable(self.notify):
            try:
                self.notify(title, message)
            except Exception as exc:  # a failed balloon must not kill a round
                self.logger.error("notify failed: %s", exc)
        else:
            self.logger.info("notify (no tray): %s - %s", title, message)

    def _notify_manual(self, alert):
        """Captcha / login-lost / risk-control -> Feishu asks for one PC action.

        Never fatal: a failed notification must not stop the heartbeat.
        """
        try:
            from .feishu_approval import notify_manual_intervention
            from .settings import load_settings
            states = set(alert.get("states") or [])
            if "BANNED" in states or "RATE_LIMITED" in states:
                kind = "risk"
            elif "DROPPED" in states or "NOT_LOGGED_IN" in states:
                kind = "login_lost"
            else:
                kind = "manual"
            settings = self.settings or load_settings(self.layout.settings_file)
            result = notify_manual_intervention(
                self.layout, settings, title=alert.get("title") or "需要处理",
                message=alert.get("message") or "需要到电脑前处理一次",
                kind=kind)
            self.logger.info("feishu manual alert: %s", result.get("status"))
        except Exception as exc:
            self.logger.error("feishu manual alert failed: %s", exc)

    def _write_heartbeat(self, summary) -> None:
        from .jsonio import write_json
        write_json(self.layout.run / "heartbeat.json", summary)

    def run_forever(self, *, max_rounds=None, stop_event=None) -> int:
        self.keepawake.acquire()
        try:
            while stop_event is None or not stop_event.is_set():
                try:
                    self.run_round()
                except Exception as exc:   # belt: one bad round must not be fatal
                    self.logger.error("round crashed (continuing): %s", exc)
                    # the audit write itself may hit a full disk / bad path --
                    # never let the crash handler take the loop down (item 12)
                    try:
                        audit(self.layout.logs, "round_crash",
                              detail=traceback.format_exc())
                    except Exception as audit_exc:
                        self.logger.error("audit write failed: %s", audit_exc)
                if max_rounds is not None and self.rounds >= max_rounds:
                    return EXIT_OK
                if stop_event is None:
                    time.sleep(self.interval)
                elif stop_event.wait(self.interval):
                    break
        except KeyboardInterrupt:
            self.logger.info("daemon interrupted; exiting cleanly")
            return EXIT_OK
        finally:
            self.keepawake.release()


def _default_downlink(layout, logger):
    """Build a downlink from settings if a source is configured, else None."""
    from .downlink import Downlink, FileSystemSource, PresignedHttpSource
    from .settings import load_settings
    settings = load_settings(layout.settings_file)
    downlink_cfg = settings.get("downlink") or {}
    source = None
    if downlink_cfg.get("local_bucket"):
        source = FileSystemSource(downlink_cfg["local_bucket"])
    elif downlink_cfg.get("token_endpoint"):
        source = PresignedHttpSource(downlink_cfg["token_endpoint"],
                                     {}, settings.get("customer_id", ""))
    if source is None:
        logger.info("no downlink source configured; heartbeat will only report")
        return None
    return Downlink(layout, source, settings, logger=logger)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="winport.daemon")
    parser.add_argument("--max-rounds", type=int, default=None)
    parser.add_argument("--data")
    args = parser.parse_args(argv)
    layout = Layout(data=Path(args.data) if args.data else None).ensure()
    logger = get_logger("winport.daemon", layout.logs)
    daemon = Daemon(layout, downlink=_default_downlink(layout, logger),
                    logger=logger)
    return daemon.run_forever(max_rounds=args.max_rounds)


if __name__ == "__main__":
    raise SystemExit(main())
