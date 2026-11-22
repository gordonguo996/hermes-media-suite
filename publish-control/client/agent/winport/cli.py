"""Unified CLI.  Every subcommand is scriptable, no bash required."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .errors import EXIT_CONFIG, EXIT_OK, WinPortError
from .errors import EXIT_GATE_RED, EXIT_SKIP
from .jsonio import read_json, read_jsonl, write_json
from .paths import Layout
from .version import VERSION
from . import xingtu_tasks

CLIENT_CAPABILITIES = tuple(xingtu_tasks.CLIENT_CAPABILITIES)


def _layout(args) -> Layout:
    return Layout(program=Path(args.program) if getattr(args, "program", None) else None,
                  data=Path(args.data) if getattr(args, "data", None) else None
                  ).ensure()


def _registry(layout):
    """(environments, channels) -- environments.json wins, legacy files still work."""
    from .platforms import load_registry
    return load_registry(layout)


def _channels(layout):
    return _registry(layout)[1]


def _envs(layout):
    return _registry(layout)[0]


def _select_channels(channels, args):
    channel_id = getattr(args, "channel", None)
    platform = getattr(args, "platform", None)
    if getattr(args, "all", False):
        return [c for c in channels if c.enabled]
    if channel_id:
        for channel in channels:
            if channel.channel_id == channel_id:
                return [channel]
        raise WinPortError(f"unknown channel: {channel_id}")
    env_id = getattr(args, "env", None)
    if env_id:
        hits = [c for c in channels if getattr(c, "env_id", None) == env_id]
        if not hits:
            raise WinPortError(f"unknown environment: {env_id}")
        return hits
    if platform:
        for channel in channels:
            if channel.platform == platform:
                return [channel]
        raise WinPortError(f"unknown platform: {platform}")
    raise WinPortError("give --channel <id>, --platform <name> or --all "
                       "(see `winport channels`)")


def cmd_env_probe(args):
    from .fingerprint import fingerprint
    from .selftest import run_selftest, print_selftest
    layout = _layout(args)
    print(json.dumps({"version": VERSION, "layout": layout.describe().splitlines(),
                      "fingerprint": fingerprint()}, ensure_ascii=False, indent=2))
    print_selftest(run_selftest(layout))
    return EXIT_OK


def cmd_fingerprint(args):
    from .fingerprint import fingerprint
    print(json.dumps(fingerprint(), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_named_op(args):
    """1.2.221: a bot operation that used to be shipped as ``python -c``."""
    from .named_ops import run
    return run(args.named_op, _layout(args), args.args_json)


def cmd_remote_assist(args):
    """Customer-side switch for remote assist (never callable remotely)."""

    from .remote_policy import assist_state, set_assist
    layout = _layout(args)
    if args.action == "on":
        # 1.2.225: no --hours means on until the customer switches it off
        state = set_assist(layout, True, hours=args.hours, actor="cli")
    elif args.action == "off":
        state = set_assist(layout, False, actor="cli")
    else:
        state = assist_state(layout)
    print(json.dumps(state, ensure_ascii=False))
    return EXIT_OK


def cmd_machine_code(args):
    """Self-service step 1: print + save the \u673a\u5668\u7801 the customer sends us."""
    from .onboard import machine_code_info, machine_code_text, write_machine_code
    layout = _layout(args)
    info = machine_code_info()
    path = write_machine_code(layout, out=args.out)
    if args.json:
        print(json.dumps({**info, "file": str(path)}, ensure_ascii=False, indent=2))
    else:
        print(machine_code_text(info, out_path=path))
    return EXIT_OK


def cmd_license_import(args):
    """Self-service step 3: install a vendor-signed license.json offline."""
    from .onboard import (default_license_sources, find_license_file,
                          import_license, license_import_text)
    layout = _layout(args)
    src = find_license_file(layout, explicit=args.src)
    if src is None:
        searched = [str(p) for p in default_license_sources(layout)]
        result = {"status": "NO_LICENSE_FILE", "installed": False,
                  "src": args.src or "", "searched": searched,
                  "license_file": str(layout.license_file),
                  "reason": "\u6ca1\u6709\u627e\u5230 license.json\uff0c\u8bf7\u628a\u6388\u6743\u6587\u4ef6\u653e\u5230\u4ee5\u4e0b\u4efb\u4e00\u4f4d\u7f6e\u540e\u91cd\u8bd5\u3002"}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(license_import_text(result))
        return EXIT_CONFIG
    # 1.2.5 round-3 (GPT review N1): validate-only must NOT install.  Pass
    # install=not dry_run so the file is written only in the real path.
    result = import_license(layout, src, install=not args.dry_run)
    if args.dry_run:
        result["dry_run"] = True
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(license_import_text(result))
    ok = result["installed"] or (args.dry_run
                                 and result.get("status") in ("OK", "GRACE", "EXPIRED"))
    return EXIT_OK if ok else EXIT_CONFIG


def cmd_license_check(args):
    from .fingerprint import fingerprint
    from .license import (account_quota, check_license,
                          require_vendor_public_key, authorized_accounts)
    license_obj = read_json(Path(args.license), default=None)
    if license_obj is None:
        raise WinPortError(f"licence not found: {args.license}")
    # 1.2.5 (GPT review item 1): fixed trust root, MANDATORY signature check and
    # the weak-fingerprint check.  There is no --pubkey / --machine-id /
    # --skip-signature switch on this production command any more, so it can no
    # longer be made to report a false green (diagnostic commands stay honest).
    public_key = require_vendor_public_key()
    fp = fingerprint()
    result = check_license(license_obj, public_key, fp["machine_id"],
                           verify_signature=True, fingerprint_info=fp)
    if getattr(args, "accounts", False):
        result["accounts_authorized"] = sorted(authorized_accounts(license_obj))
        result["account_quota"] = account_quota(license_obj)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK if result["status"] in ("OK", "GRACE") else EXIT_CONFIG


def cmd_downlink(args):
    from .fingerprint import fingerprint
    from .downlink import Downlink, FileSystemSource, PresignedHttpSource
    from .logutil import get_logger
    from .settings import load_settings
    layout = _layout(args)
    settings = load_settings(layout.settings_file)
    if args.token_endpoint:
        source = PresignedHttpSource(args.token_endpoint, {}, args.customer_id or "")
    else:
        source = FileSystemSource(args.bucket)
    downlink = Downlink(layout, source, settings,
                        # 1.2.5 round-3 (GPT review item 1): the ledger identity is
                        # the LOCAL machine fingerprint -- there is no --machine-id
                        # CLI override on the production download entry.
                        machine_id=fingerprint()["machine_id"],
                        customer_id=args.customer_id,
                        logger=get_logger("winport.downlink", layout.logs))
    result = downlink.run_once()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK if not result["errors"] else EXIT_CONFIG


def cmd_bind(args):
    from . import folder_bind
    manifest = read_json(Path(args.manifest))
    result = folder_bind.bind_folder(manifest, Path(args.folder), _layout(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK if result["status"] == folder_bind.READY else EXIT_CONFIG


def cmd_collect_offline(args):
    from .collect_douyin import collect_offline
    result = collect_offline(_layout(args), Path(args.xlsx),
                             allow_drift=args.allow_drift)
    summary = result["normalized"]
    print(json.dumps({"header": summary["header"],
                      "column_count": summary["column_count"],
                      "row_count": summary["row_count"],
                      "outputs": result["outputs"]},
                     ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_collect_all(args):
    """One command: collect Douyin + Xingtu, then build the boss report."""
    layout = _layout(args)
    do_collect = not args.report_only
    do_report = not args.collect_only
    collected = {}
    if do_collect:
        def _field(env, key, default=None):
            if isinstance(env, dict):
                return env.get(key, default)
            return getattr(env, key, default)
        try:
            if not args.skip_douyin:
                from .collect_douyin import collect_via_cdp
                envs = [e for e in _envs(layout)
                        if "douyin" in (_field(e, "platforms") or [])
                        and _field(e, "enabled", True)]
                ports = [_field(e, "debug_port") for e in envs if _field(e, "debug_port")] or [None]
                douyin_runs = []
                last_error = None
                for port in ports:
                    try:
                        result = collect_via_cdp(layout, day=args.day, port=port)
                        douyin_runs.append({
                            "port": port,
                            "rows": result["normalized"]["row_count"],
                            "output": result["outputs"]["json"],
                        })
                    except Exception as exc:
                        last_error = exc
                if not douyin_runs:
                    raise last_error or RuntimeError("douyin collect failed")
                collected["douyin"] = douyin_runs if len(douyin_runs) > 1 else douyin_runs[0]
            if not args.skip_xingtu:
                from .collect_xingtu import collect_via_cdp as collect_xingtu
                # 星图是跟抖音号登录态绑定的：每个 env 各采一次、各写一份文件。
                # 只采第一个号会导致第二个号的星图数据永远是 0。
                x_envs = [e for e in _envs(layout)
                          if _field(e, "enabled", True) and _field(e, "debug_port")]
                x_runs, x_err = [], None
                for xe in (x_envs or [None]):
                    eid = _field(xe, "env_id") if xe is not None else None
                    x_port = _field(xe, "debug_port") if xe is not None else None
                    suffix = ("_" + str(eid)) if eid else ""
                    try:
                        res = collect_xingtu(layout, port=x_port, day=args.day,
                                             suffix=suffix)
                        x_runs.append({
                            "env_id": eid,
                            "port": x_port,
                            "account": res["data"].get("name") or "",
                            "tasks": res["data"].get("task_count", 0),
                            "items": res["data"].get("item_count", 0),
                            "granularity": res["data"].get("granularity"),
                            "output": res["outputs"]["json"],
                        })
                    except Exception as exc:
                        x_err = exc
                if not x_runs:
                    raise x_err or RuntimeError("xingtu collect failed")
                first = dict(x_runs[0])
                first["all"] = x_runs
                collected["xingtu"] = first
        except Exception as exc:
            from .logutil import get_logger
            get_logger("winport.collect", layout.logs).error(
                "collect failed: %s", type(exc).__name__)
            print(json.dumps({
                "ok": False,
                "error": "\u53d6\u6570\u672a\u5b8c\u6210\uff0c\u8bf7\u786e\u8ba4\u5de5\u4f5c\u53f0\u5df2\u6253\u5f00\u4e14\u8d26\u53f7\u5df2\u767b\u5f55\u540e\u91cd\u8bd5\u3002",
                "collected": collected,
            }, ensure_ascii=False, indent=2))
            return EXIT_GATE_RED
    report = None
    if do_report:
        from .report_boss import write_boss_report
        report = write_boss_report(layout, day=args.day, out=args.out)
        try:
            from .report_two_account import build as build_two_account
            report["two_account"] = build_two_account(layout, day=args.day)
        except Exception as exc:
            report["two_account_error"] = type(exc).__name__
    print(json.dumps({"ok": True, "collected": collected, "report": report},
                     ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_report(args):
    from . import report as report_mod
    layout = _layout(args)
    collection = read_json(Path(args.collection_json)) if args.collection_json else None
    result = report_mod.write_report(layout, day=args.day, collection=collection)
    print(json.dumps({"md": result["md"], "xlsx": result["xlsx"]},
                     ensure_ascii=False, indent=2))
    return EXIT_OK


AUTO_SUBMIT_DEFAULT_NOTE = (
    "\u771f\u5b9e\u53d1\u5e03\u9a71\u52a8\u5df2\u63a5\u5165\uff1a\u9ed8\u8ba4\u53ea\u4e0a\u4f20+\u586b\u8868\uff0c\u4e0d\u4f1a\u70b9\u300c\u53d1\u5e03\u300d\u3002"
    "\u53ea\u6709\u663e\u5f0f\u52a0 --allow-submit \u4e14\u98de\u4e66/\u5ba1\u6279\u95e8\u7981\u901a\u8fc7\u540e\uff0c\u624d\u4f1a\u771f\u7684\u70b9\u51fb\u53d1\u5e03\u3002")


def _xingtu_task_or_empty(plan, mode=None):
    try:
        return xingtu_tasks.task_from_plan(plan, mode=mode) or {}
    except xingtu_tasks.XingtuError:
        return {}


def _xingtu_failure_result(plan, mode, exc):
    """Build one backward-compatible, code-driven Xingtu failure result."""
    envelope = xingtu_tasks.error_envelope(
        exc, client_version=VERSION, capabilities=CLIENT_CAPABILITIES,
        stage="preflight" if getattr(exc, "stage", "") == "preflight" else "confirm")
    task = _xingtu_task_or_empty(plan, mode)
    result = {
        "mode": mode,
        "plan_id": (plan or {}).get("plan_id") if isinstance(plan, dict) else None,
        "account_id": (plan or {}).get("account_id") if isinstance(plan, dict) else None,
        "filled": False,
        "published": False,
        "platform_work_id": None,
        "work_id": None,
        "evidence": [],
        "publish_mode": "xingtu",
        "xingtu_attached": False,
        "xingtu_order_id": task.get("order_id", ""),
        "xingtu_task_name": task.get("task_name", ""),
        "client_version": VERSION,
        "capabilities": list(CLIENT_CAPABILITIES),
    }
    result.update(envelope)
    code = envelope["code"]
    result["stopped_at"] = {
        "XINGTU_TASK_NOT_FOUND": "xingtu_task_not_found",
        "XINGTU_TASK_AMBIGUOUS": "xingtu_task_ambiguous",
        "XINGTU_TASK_NOT_ATTACHED": "xingtu_task_not_attached",
        "XINGTU_TASK_CHANGED": "xingtu_task_changed",
        "XINGTU_TASK_UNAVAILABLE": "xingtu_task_unavailable",
        "XINGTU_DUPLICATE_LOCAL": "xingtu_duplicate_local",
        "XINGTU_DUPLICATE_POSSIBLE_LOCAL": "xingtu_duplicate_possible_local",
        "XINGTU_DEDUP_LEDGER_ERROR": "xingtu_dedup_ledger_error",
        "XINGTU_PLAN_SCHEMA_INVALID": "xingtu_plan_schema_invalid",
        "XINGTU_ALREADY_SUBMITTED": "xingtu_duplicate",
        "XINGTU_DUPLICATE_CHECK_UNKNOWN": "xingtu_duplicate_check_unknown",
        "XINGTU_CLIENT_SIGNED_OUT": "xingtu_client_signed_out",
    }.get(code, "xingtu_failed")
    return result


def _publish_target(layout, plan):
    from .dryrun import resolve_targets
    return resolve_targets(plan, envs=_envs(layout), channels=_channels(layout))


def _real_publish_driver(layout, plan, args, *, env, channel):
    """Reuse the login/CDP launch path, then attach Playwright to Chrome."""
    from . import browser
    from .publish_exec import DOUYIN_UPLOAD_URL, PlaywrightDriver
    from .settings import load_settings
    if not getattr(env, "supports_cdp", False):
        raise WinPortError("\u8be5\u73af\u5883\u6ca1\u6709 CDP \u8c03\u8bd5\u7aef\u53e3\uff0c\u65e0\u6cd5\u9a71\u52a8\u771f Chrome \u53d1\u5e03",
                           exit_code=EXIT_GATE_RED)
    opened = browser.open_environment(layout, env, url=DOUYIN_UPLOAD_URL)
    if not opened.get("opened") and not opened.get("reused"):
        raise WinPortError(
            "\u771f Chrome \u53d1\u5e03\u9a71\u52a8\u672a\u80fd\u5c31\u7eea\uff1a" + (opened.get("message") or
                                               opened.get("reason") or "\u672a\u77e5\u539f\u56e0"),
            exit_code=EXIT_GATE_RED)
    settings = load_settings(layout.settings_file)
    return PlaywrightDriver(
        env.cdp_url(), layout=layout, env=env, channel=channel, plan=plan,
        allow_submit=bool(getattr(args, "allow_submit", False)),
        settings=settings,
        sms_code_file=getattr(args, "sms_code_file", None),
        sms_wait_seconds=getattr(args, "sms_wait_seconds", None),
        verification_detect_seconds=getattr(args, "verification_detect_seconds", None),
        qr_wait_seconds=getattr(args, "qr_wait_seconds", None))


def _execute_publish(layout, plan, args, ledger, *, env, channel, **kwargs):
    from .publish_exec import IdempotencyConsumed, run_publish
    from . import xingtu_tasks
    from .publish_guard import PublishHoldStore, PUBLISH_UNKNOWN
    from .errors import NoSubmitExit
    from .lock import FileLock
    from .hashing import sha256_bytes
    if channel.platform != "douyin" or not channel.enabled or not env or not env.enabled:
        raise WinPortError("\u53d1\u5e03\u76ee\u6807\u4e0d\u662f\u5df2\u542f\u7528\u7684\u6296\u97f3\u73af\u5883", exit_code=EXIT_GATE_RED)
    hold_store = PublishHoldStore(layout.run / "publish_holds.json")
    if hold_store.get(channel.channel_id):
        raise WinPortError("\u901a\u9053\u5df2\u51bb\u7ed3\uff0c\u987b\u663e\u5f0f\u4eba\u5de5\u89e3\u9664", exit_code=EXIT_GATE_RED)
    lock_name = sha256_bytes(str(env.env_id).encode())[:24]
    lock = FileLock(layout.run / "publish-locks" / (lock_name + ".lock"))
    if not lock.acquire():
        raise WinPortError("\u8be5\u73af\u5883\u5df2\u6709\u53d1\u5e03\u4efb\u52a1\u6267\u884c\u4e2d", exit_code=EXIT_GATE_RED)
    driver = None
    mode = str(kwargs.get("mode") or "fill")
    try:
        driver = _real_publish_driver(layout, plan, args, env=env, channel=channel)
        result = run_publish(plan, driver, ledger, **kwargs)
        task = xingtu_tasks.task_from_plan(plan, mode=mode)
        if task:
            result["xingtu_order_id"] = task.get("order_id")
            result["xingtu_task_name"] = task.get("task_name")
            result["publish_mode"] = "xingtu"
            if mode == "fill":
                # A fill run has not published anything, so it neither checks
                # nor writes the local used-task ledger.
                result["xingtu_submission_confirmed"] = False
                return result
            # 1.2.99: Xingtu rejects a duplicate task itself.  The publish
            # chain checks only the local used-task ledger before submit and
            # records it after success; it never reads the backend list back.
            return result
        result["publish_mode"] = "normal"
        return result
    except (xingtu_tasks.XingtuDuplicateLocal,
            xingtu_tasks.XingtuDedupLedgerError) as exc:
        return _xingtu_failure_result(plan, kwargs.get("mode") or "fill", exc)
    except xingtu_tasks.XingtuError as exc:
        return _xingtu_failure_result(plan, kwargs.get("mode") or "fill", exc)
    except IdempotencyConsumed:
        # A CONSUMED key is a known-success refusal, not an uncertain publish.
        # Freezing here would make the customer-confirmed republish exit
        # unreachable; the signed republish path still has to pass every other
        # gate (licence, account, freeze, canary, confirm-token, receipt).
        raise
    except NoSubmitExit:
        # Expected fill-only rehearsal: RESERVED was already recycled by
        # run_publish and the channel is not frozen.
        raise
    except BaseException as exc:
        # Deterministic idempotency refusals happen before any page
        # action; freezing the channel for them blocks unrelated
        # materials until a manual unfreeze.
        if b"refusing to republish" in str(exc).lower().encode():
            raise
        if driver is not None and getattr(driver, "_pre_submit_failed", False):
            raise
        try:
            if not hold_store.get(channel.channel_id):
                hold_store.persist_failure(
                    channel.channel_id, PUBLISH_UNKNOWN,
                    {"reason": str(exc), "step": "publish_unknown",
                     "source": "cli_execute_publish"},
                    reason=str(exc))
        except Exception:
            pass
        if driver is not None:
            try:
                driver._record("failure", note=str(exc), error=type(exc).__name__)
            except Exception:
                pass
        raise
    finally:
        try:
            if driver is not None:
                driver.close()
        finally:
            lock.release()


def cmd_publish_enqueue(args):
    from .dryrun import build_preview
    from .settings import load_settings
    from .publish_queue import enqueue
    layout = _layout(args)
    plan = read_json(Path(args.plan))
    receipt = read_json(Path(args.receipt))
    licence = _licence_gate(layout, plan=plan)
    preview = build_preview(plan, envs=_envs(layout), channels=_channels(layout),
                            settings=load_settings(layout.settings_file), license_info=licence)
    path = enqueue(layout, plan, preview, receipt)
    print(json.dumps({"queued": str(path), "enabled": bool(
        (load_settings(layout.settings_file).get("publish_queue") or {}).get("enabled"))}))
    return EXIT_OK


def cmd_publish_hold_clear(args):
    from .publish_guard import PublishHoldStore
    layout = _layout(args)
    result = PublishHoldStore(layout.run / "publish_holds.json").clear_manual(
        args.channel, reason=args.reason, actor=args.actor,
        reason_code=getattr(args, "reason_code", None))
    payload = {"channel": args.channel, **result}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return EXIT_OK


def cmd_publish_hold_status(args):
    from .publish_guard import PublishHoldStore
    layout = _layout(args)
    hold = PublishHoldStore(layout.run / "publish_holds.json").get(args.channel)
    print(json.dumps({"channel": args.channel, "hold": hold},
                     ensure_ascii=False, sort_keys=True))
    return EXIT_OK


def cmd_publish(args):
    """1.2.20 production publish entry: real Chrome + explicit submit switch.

    * default ``fill`` uploads and fills the real creator-center form, records
      evidence, and stops in front of the publish button;
    * ``approved_publish`` additionally requires the control-plane receipt and
      Feishu confirmation;
    * only ``--allow-submit`` permits the final real ``\u53d1\u5e03`` click.
    """
    layout = _layout(args)
    try:
        plan = read_json(Path(args.plan))
    except (ValueError, UnicodeDecodeError, OSError) as exc:
        raise WinPortError(
            f"\u53d1\u5e03\u8ba1\u5212\u6587\u4ef6\u65e0\u6cd5\u89e3\u6790\uff08{type(exc).__name__}\uff09\uff1a{args.plan}\uff1b"
            "\u62d2\u7edd\u53d1\u5e03\uff0c\u8bf7\u68c0\u67e5\u8ba1\u5212\u6587\u4ef6\u662f\u5426\u4e3a\u6709\u6548 UTF-8 JSON\u3002",
            exit_code=EXIT_GATE_RED)
    if not isinstance(plan, dict) or not plan:
        raise WinPortError(f"plan not found or unreadable: {args.plan}",
                           exit_code=EXIT_GATE_RED)

    try:
        ledger_path = xingtu_tasks.used_tasks_path(layout)
    except Exception:
        ledger_path = None
    validation = xingtu_tasks.validate_plan_contract(
        plan, mode=args.mode, ledger_path=ledger_path)
    if not validation["ok"]:
        problem = validation["problems"][0]
        exc = xingtu_tasks.XingtuPlanSchemaInvalid(
            problem["message"], code=problem["code"],
            details={"problems": validation["problems"]},
            failure_scope=problem.get("failure_scope", "batch"),
            action=problem.get("action", "regenerate_plan"),
            stage="preflight")
        failure = _xingtu_failure_result(plan, args.mode, exc)
        failure["problems"] = validation["problems"]
        failure["blocking_problem"] = problem
        print(json.dumps(failure, ensure_ascii=False, indent=2))
        return EXIT_GATE_RED

    from .publish_exec import MODE_APPROVED, MODE_FILL
    gate = _licence_gate(layout, license_path=getattr(args, "license", None),
                         plan=plan)
    result = {
        "mode": args.mode,
        "plan_id": plan.get("plan_id"),
        "account_id": plan.get("account_id"),
        "auto_submit": bool(getattr(args, "allow_submit", False)),
        "filled": False,
        "published": False,
        "platform_work_id": None,
        "work_id": None,
        "gate": gate,
        "note": AUTO_SUBMIT_DEFAULT_NOTE,
        "evidence": [],
        "client_version": VERSION,
        "capabilities": list(CLIENT_CAPABILITIES),
    }
    if gate.get("publish_allowed") is not True:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise WinPortError(
            "\u6388\u6743 / \u8d26\u53f7\u95e8\u7981\u672a\u901a\u8fc7\uff0c\u7981\u6b62\u53d1\u5e03\uff08\u6293\u6570\u4e0e\u5386\u53f2\u6570\u636e\u4e0d\u53d7\u5f71\u54cd\uff09\uff1a"
            + "\uff1b".join(gate.get("reasons") or ["\u672a\u77e5\u539f\u56e0"]),
            exit_code=EXIT_GATE_RED)

    try:
        env, channel = _publish_target(layout, plan)
    except Exception as exc:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise WinPortError(f"\u627e\u4e0d\u5230\u53d1\u5e03\u76ee\u6807\uff1a{exc}", exit_code=EXIT_GATE_RED)

    from .settings import load_settings
    from .stage2_ledger import Stage2Ledger

    ledger = Stage2Ledger(layout.stage2_ledger)

    if args.mode == MODE_FILL:
        publish_result = _execute_publish(layout, plan, args, ledger,
            env=env, channel=channel, mode=MODE_FILL)
        result.update(publish_result)
        problems = publish_result.get("problems") or []
        blocking = [item for item in problems
                    if not (isinstance(item, dict)
                            and item.get("code") == "XINGTU_READBACK_UNKNOWN")]
        if publish_result.get("filled") is not True or blocking:
            result["filled"] = False
            result["published"] = False
            result["auto_submit"] = False
            result["stopped_at"] = (publish_result.get("stopped_at")
                                    or "fill_failed")
            result["note"] = "\u4e0a\u4f20/\u586b\u8868\u672a\u5b8c\u6210\uff0c\u4e0d\u80fd\u8bb0\u4e3a fill \u6210\u529f\u3002"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return EXIT_GATE_RED
        result["filled"] = True
        result["published"] = False
        result["auto_submit"] = False
        result["stopped_at"] = "publish_button"
        result["note"] = ("\u771f\u5b9e Chrome \u5df2\u5b8c\u6210\u4e0a\u4f20+\u586b\u8868\u5e76\u7559\u8bc1\uff1b\u672a\u70b9\u51fb\u53d1\u5e03\u3002"
                          "\u8981\u771f\u7684\u53d1\u5e03\u8bf7\u4f7f\u7528 approved_publish \u5e76\u663e\u5f0f\u52a0 --allow-submit\u3002")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK

    if args.mode != MODE_APPROVED:
        raise WinPortError(f"unknown publish mode {args.mode}",
                           exit_code=EXIT_GATE_RED)

    # ---- approval chain (receipt + Feishu) -----------------------------
    if not getattr(args, "confirm_token", None):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise WinPortError("approved_publish \u5fc5\u987b\u5e26 --confirm-token\uff08\u786e\u8ba4\u51ed\u636e\uff09\uff1b"
                           "\u6ca1\u6709\u786e\u8ba4\u51ed\u636e\u4e0d\u8bb8\u786c\u53d1", exit_code=EXIT_GATE_RED)
    if not getattr(args, "receipt", None):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise WinPortError("approved_publish \u5fc5\u987b\u5e26 --receipt\uff08\u63a7\u5236\u9762\u7b7e\u53d1\u3001"
                           "\u7528\u56fa\u5b9a\u4fe1\u4efb\u6839\u9a8c\u7b7e\u7684\u5ba1\u6279\u5355\uff09", exit_code=EXIT_GATE_RED)
    from .approval import verify_receipt
    from .dryrun import check_confirmation, read_preview
    from .license import require_receipt_public_key
    try:
        pubkey = require_receipt_public_key()
    except Exception as exc:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise WinPortError(f"\u5ba1\u6279\u4fe1\u4efb\u6839\u4e0d\u53ef\u7528\uff0c\u62d2\u7edd\u53d1\u5e03\uff1a{exc}",
                           exit_code=EXIT_GATE_RED)
    receipt = read_json(Path(args.receipt), default=None)
    republish_confirmed = bool(getattr(args, "republish_confirmed", False))
    verdict = verify_receipt(receipt, plan, pubkey, operation_mode=MODE_APPROVED,
                             allow_republish=republish_confirmed)
    result["receipt"] = verdict
    result["republish"] = republish_confirmed
    stored = read_preview(layout, path=getattr(args, "preview", None))
    if not stored:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise WinPortError("\u6ca1\u6709\u627e\u5230 dry-run \u6e05\u5355\uff08\u5148\u8dd1 winport dry-run\uff09\uff1b"
                           "\u6ca1\u6709\u786e\u8ba4\u6e05\u5355\u4e0d\u8bb8\u786c\u53d1", exit_code=EXIT_GATE_RED)

    from .feishu_approval import gate as approval_gate
    from .feishu_approval import load_record, refresh
    approval_id = getattr(args, "approval_id", None)
    approval_record = None
    if approval_id:
        approval_record = load_record(layout, approval_id)
        if approval_record:
            approval_record = refresh(layout, approval_record,
                                      load_settings(layout.settings_file))
        if str((approval_record or {}).get("scope") or "") == "batch":
            from .feishu_approval import batch_used as _batch_used
            stored["batch_used"] = _batch_used(layout, approval_id)
        approval_verdict = approval_gate(approval_record, stored)
        result["approval"] = approval_verdict
        if not approval_verdict["ok"]:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            raise WinPortError("\u98de\u4e66\u53d1\u5e03\u786e\u8ba4\u672a\u901a\u8fc7\uff1a" + approval_verdict["reason"],
                               exit_code=EXIT_GATE_RED)
    check = check_confirmation(stored, plan, envs=_envs(layout),
                               channels=_channels(layout),
                               settings=load_settings(layout.settings_file),
                               confirm_token=args.confirm_token,
                               license_info=gate, publishing=True)
    result["confirmation"] = check
    if not verdict["ok"]:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise WinPortError("\u5ba1\u6279\u5355\u9a8c\u7b7e\u5931\u8d25\uff1a" + verdict["reason"],
                           exit_code=EXIT_GATE_RED)
    if not check["ok"]:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise WinPortError(check["reason"], exit_code=EXIT_GATE_RED)

    def final_check():
        current_gate = _licence_gate(layout, license_path=getattr(args, "license", None), plan=plan)
        current = check_confirmation(stored, plan, envs=_envs(layout), channels=_channels(layout),
            settings=load_settings(layout.settings_file), confirm_token=args.confirm_token,
            license_info=current_gate, publishing=True)
        if not current["ok"]:
            raise WinPortError(current["reason"], exit_code=EXIT_GATE_RED)
    def approval_refresh():
        record = load_record(layout, approval_id)
        return refresh(layout, record, load_settings(layout.settings_file))
    publish_result = _execute_publish(layout, plan, args, ledger,
        env=env, channel=channel, mode=MODE_APPROVED,
        receipt=receipt, verify_key=pubkey,
        canary_armed=bool(getattr(args, "canary_armed", False)),
        republish_confirmed=republish_confirmed,
        approval_record=approval_record,
        approval_preview=stored if approval_id else None,
        approval_refresh=approval_refresh if approval_id else None,
        final_check=final_check)
    result.update(publish_result)
    result["published"] = bool(publish_result.get("published"))
    if result["published"] and str((approval_record or {}).get("scope") or "") == "batch":
        from .feishu_approval import bump_batch_usage as _bump_batch
        result["batch_used"] = _bump_batch(layout, approval_id)
    result["auto_submit"] = bool(getattr(args, "allow_submit", False))
    if result["published"]:
        result["published_at"] = (result.get("published_at")
                                  or time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        try:
            from .report_center import ReportCenter
            ledger_row = ReportCenter(layout).record_published(
                plan, channel=channel, result=result)
            result["publish_ledger"] = ledger_row
        except Exception as exc:
            result["publish_ledger"] = {
                "recorded": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    if not result["auto_submit"]:
        result["note"] = ("\u5df2\u586b\u8868\u5e76\u7559\u8bc1\uff0c\u4f46 --allow-submit \u672a\u5f00\u542f\uff0c"
                          "\u6ca1\u6709\u70b9\u51fb\u53d1\u5e03\u6309\u94ae")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK if result.get("published") else EXIT_GATE_RED


def cmd_xingtu_tasks(args):
    """List writable star-order tasks from the signed-in Xingtu backend."""
    from . import browser, xingtu_tasks
    layout = _layout(args)
    envs = _envs(layout)
    env = None
    if getattr(args, "env", None):
        env = next((item for item in envs if item.env_id == args.env), None)
    elif getattr(args, "channel", None):
        channel = next((item for item in _channels(layout)
                        if item.channel_id == args.channel), None)
        if channel is not None:
            env = next((item for item in envs
                        if item.env_id == getattr(channel, "env_id", None)), None)
    else:
        env = next((item for item in envs if item.enabled), None)
    if env is None:
        result = {"ok": False, "error": "no enabled environment"}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return EXIT_CONFIG
    opened = browser.open_environment(layout, env, url=xingtu_tasks.CREATOR_URL)
    if not (opened.get("opened") or opened.get("reused")):
        result = {"ok": False, "env_id": env.env_id,
                  "error": opened.get("message") or opened.get("reason")
                  or "browser not ready"}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return EXIT_GATE_RED
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        pw = sync_playwright().start()
        try:
            cdp = env.cdp_url()
            browser_obj = pw.chromium.connect_over_cdp(cdp)
            contexts = getattr(browser_obj, "contexts", None) or []
            if not contexts:
                raise RuntimeError("browser context unavailable")
            tasks = xingtu_tasks.list_available_tasks(contexts[0],
                                                       max_tasks=args.limit)
        finally:
            pw.stop()
    except Exception as exc:
        result = {"ok": False, "env_id": env.env_id,
                  "error": "%s: %s" % (type(exc).__name__, exc)}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return EXIT_GATE_RED
    public = [{key: task.get(key) for key in
               ("task_name", "demander", "task_type", "remaining_quota",
                "order_id", "task_id")}
              for task in tasks]
    print(json.dumps({"ok": True, "env_id": env.env_id,
                      "version": VERSION, "tasks": public},
                     ensure_ascii=True, indent=2))
    return EXIT_OK


def _licence_gate(layout, *, license_path=None, plan=None,
                  allow_test_injection=False, pubkey_override=None,
                  machine_id_override=None, fingerprint_override=None):
    """Publish gate.  NEVER returns None: a missing licence / key / integrity
    failure is an explicit block, not a silent "allow" (GPT review item 1).

    Production callers pass nothing but ``layout`` / ``license_path`` / ``plan``:
      * the trust root is read from ``paths.install_root()`` (not ``--program``);
      * the machine code comes ONLY from the trusted local fingerprint;
      * the plan's account_id is mandatory and the per-seat check always runs;
      * the weak-fingerprint clone check is always on;
      * a clock rollback -- or an unreadable/unwritable clock state -- blocks;
      * a failed startup-chain integrity check blocks NEW publishes.

    ``allow_test_injection`` / ``*_override`` are for the static Linux harness
    ONLY (there is no CLI flag that reaches them), so production is isolated.
    """
    from .fingerprint import fingerprint
    from .license import (clock_guard, load_vendor_public_key, publish_gate,
                          require_vendor_public_key, trial_state,
                          STATUS_SEAT_DENIED)
    blocked = {"publish_allowed": False, "collect_allowed": True, "status": "",
               "license_status": "", "days_left": None, "level": "bad",
               "notices": [], "seat": None,
               "clock_rollback": False, "reasons": [],
               "tombstone_error": None}

    # ---- startup-chain integrity (item 5): fail -> no NEW publish -----------
    try:
        from .integrity import verify_program
        from .paths import install_root
        verdict = verify_program(install_root())
        if not verdict["ok"]:
            blocked["status"] = "INTEGRITY_FAILED"
            blocked["reasons"].append(
                "\u542f\u52a8\u94fe\u5b8c\u6574\u6027\u6821\u9a8c\u672a\u901a\u8fc7\uff0c\u62d2\u7edd\u53d1\u5e03\uff1a" + verdict.get("reason", ""))
            return blocked
    except Exception as exc:
        blocked["status"] = "INTEGRITY_FAILED"
        blocked["reasons"].append(f"\u542f\u52a8\u94fe\u5b8c\u6574\u6027\u6821\u9a8c\u65e0\u6cd5\u6267\u884c\uff0c\u62d2\u7edd\u53d1\u5e03\uff1a{exc}")
        return blocked

    # The fingerprint, account seat and clock are checked BEFORE the no-licence
    # trial branch.  A deleted/corrupt trial record is never recreated here.
    fp = fingerprint()
    if allow_test_injection and machine_id_override:
        machine_id = machine_id_override
        fp_for_check = fingerprint_override   # None on the static host
    else:
        machine_id = fp["machine_id"]
        fp_for_check = fp
    if fp_for_check is not None and fp_for_check.get("weak"):
        blocked["status"] = "WEAK_FINGERPRINT"
        blocked["license_status"] = "WEAK_FINGERPRINT"
        blocked["reasons"].append(
            "\u672c\u673a\u673a\u5668\u6307\u7eb9\u592a\u5f31\uff0c\u62d2\u7edd\u7ed1\u5b9a\uff1a\u65b0\u589e\u53d1\u5e03\u5df2\u6682\u505c\u3002")
        return blocked

    account_id = (plan or {}).get("account_id")
    if not account_id:
        blocked["status"] = "NO_ACCOUNT"
        blocked["license_status"] = "NO_ACCOUNT"
        blocked["reasons"].append("\u53d1\u5e03\u8ba1\u5212\u7f3a\u5c11 account_id\uff0c\u65e0\u6cd5\u505a\u5e2d\u4f4d\u6821\u9a8c\uff0c\u62d2\u7edd\u53d1\u5e03\u3002")
        return blocked

    try:
        if allow_test_injection and pubkey_override:
            key = load_vendor_public_key(pubkey_override)     # TEST ONLY
        else:
            key = require_vendor_public_key()                 # FIXED trust root
    except Exception as exc:
        blocked["status"] = "NO_VENDOR_KEY"
        blocked["reasons"].append(f"\u4f9b\u5e94\u5546\u516c\u94a5\u7f3a\u5931\u6216\u635f\u574f\uff0c\u62d2\u7edd\u53d1\u5e03\uff1a{exc}")
        return blocked

    # fail-CLOSED: an unreadable / unwritable clock state counts as a rollback
    clock = clock_guard(layout.run / "license-clock.json")
    rollback = bool(clock.get("rollback")) or not clock.get("state_ok", True)

    licence_path = license_path or layout.license_file
    if not Path(licence_path).exists():
        trial = trial_state(layout, account_id=account_id,
                            fingerprint_info=fp_for_check, clock=clock)
        if trial is not None:
            result = dict(blocked)
            result.update({k: v for k, v in trial.items()
                           if k not in ("reasons", "notices")})
            result["reasons"] = list(trial.get("reasons") or [])
            result["notices"] = list(trial.get("notices") or [])
            return result
        if rollback:
            blocked["status"] = clock.get("status") or "CLOCK_ROLLBACK"
            blocked["license_status"] = blocked["status"]
            blocked["clock_rollback"] = True
            blocked["reasons"].append(
                "\u65f6\u949f\u6821\u9a8c\u672a\u901a\u8fc7\uff08%s\uff09\uff1a\u4e3a\u9632\u7ed5\u8fc7\u6388\u6743\u5230\u671f\uff0c"
                "\u65b0\u589e\u53d1\u5e03\u5df2\u6682\u505c\uff1b\u8bf7\u6821\u51c6\u7cfb\u7edf\u65f6\u95f4\u540e\u91cd\u8bd5\u3002"
                % blocked["status"])
            return blocked
        blocked["status"] = "NOT_ACTIVATED"
        blocked["license_status"] = "NOT_ACTIVATED"
        blocked["reasons"].append("\u672a\u627e\u5230\u6388\u6743\u6587\u4ef6\uff08license.json\uff09\uff0c\u4e14\u65e0\u53ef\u7ee7\u7eed\u7684\u8bd5\u7528\u8bb0\u5f55\uff1a\u62d2\u7edd\u53d1\u5e03\u3002")
        blocked["notices"].append("\u8bf7\u53cc\u51fb\u684c\u9762\u300c\u5bfc\u5165\u6388\u6743\u300d\u5bfc\u5165\u670d\u52a1\u5546\u7b7e\u53d1\u7684 license.json\u3002")
        return blocked

    # 1.2.7 (GPT review 1.2.4 item 1 / item 9): an EXISTING but empty / corrupt
    # / non-UTF8 license.json must be an explicit BLOCK, not a raised
    # JSONDecodeError that only surfaces as a generic "command failed".  The old
    # code read with ``read_json`` (which raises on parse errors) and so an empty
    # file crashed the gate instead of rejecting it.
    try:
        obj = read_json(Path(licence_path), default=None)
    except (ValueError, UnicodeDecodeError, OSError) as exc:
        blocked["status"] = "LICENCE_UNREADABLE"
        blocked["reasons"].append(
            f"\u6388\u6743\u6587\u4ef6\u5df2\u635f\u574f\u6216\u65e0\u6cd5\u89e3\u6790\uff08{type(exc).__name__}\uff09\uff0c\u62d2\u7edd\u53d1\u5e03\uff1b"
            "\u8bf7\u91cd\u65b0\u5bfc\u5165\u670d\u52a1\u5546\u7b7e\u53d1\u7684 license.json\uff08\u539f\u6587\u4ef6\u53ef\u80fd\u88ab\u6e05\u7a7a\u6216\u975e UTF-8\uff09\u3002")
        return blocked
    if not isinstance(obj, dict) or not obj:
        blocked["status"] = "LICENCE_UNREADABLE"
        blocked["reasons"].append("\u6388\u6743\u6587\u4ef6\u4e3a\u7a7a\u6216\u4e0d\u662f\u6709\u6548 JSON\uff0c\u62d2\u7edd\u53d1\u5e03\u3002")
        return blocked

    gate = publish_gate(obj, key, machine_id, account_id=account_id,
                        fingerprint_info=fp_for_check,
                        clock_rollback=rollback)
    gate["clock"] = clock
    if gate.get("license_status") in ("OK", "GRACE", "EXPIRED"):
        try:
            from .license import mark_trial_license_seen
            mark_trial_license_seen(layout, fingerprint_info=fp_for_check)
        except Exception as exc:
            # A formal licence whose anti-replay tombstone cannot be written
            # must not publish: deleting license.json afterwards would grant a
            # fresh trial.  Returning a recorded error only was a fail-open.
            gate["tombstone_error"] = str(exc)
            gate["publish_allowed"] = False
            gate["status"] = "TRIAL_TOMBSTONE_FAILED"
            gate.setdefault("reasons", []).append(
                "已读取正式授权，但防回放墓碑写入失败：%s；新增发布已暂停，"
                "请检查磁盘/注册表权限后重试或联系服务商。" % exc)
    # 1.2.10 (GPT_REVIEW_1.2.8 \u5fc5\u4fee 2): ``publish_gate`` only flips
    # ``publish_allowed`` to False on a clock rollback -- it keeps the RAW
    # signature verdict in ``status`` (typically "OK").  A consumer that keyed
    # off the top-level ``status`` therefore showed a green "\u6388\u6743\u6709\u6548" for a
    # publish that was actually refused (only ``publish_allowed`` said no).  We
    # now consume the ONE shared verdict exactly like
    # ``licstate.unified_license_state``: keep the raw signature verdict in a
    # separate ``license_status`` and NAME the FINAL status with the blocker, so
    # a refused publish is NEVER reported as "OK".
    gate["license_status"] = gate.get("license_status") or gate.get("status")
    if rollback and gate["status"] in ("OK", "GRACE", STATUS_SEAT_DENIED):
        gate["status"] = clock.get("status") or "CLOCK_ROLLBACK"
        gate["publish_allowed"] = False
    return gate


def cmd_dry_run(args):
    """A: validate + describe what is about to be published (no browser)."""
    from .dryrun import build_preview, check_confirmation, read_preview, write_preview
    from .settings import load_settings
    layout = _layout(args)
    plan = read_json(Path(args.plan))
    if plan is None:
        raise WinPortError(f"plan not found: {args.plan}")
    try:
        ledger_path = xingtu_tasks.used_tasks_path(layout)
    except Exception:
        ledger_path = None
    validation = xingtu_tasks.validate_plan_contract(
        plan, mode="dry-run", ledger_path=ledger_path)
    if not validation["ok"]:
        result = {
            "ok": False,
            "json": None,
            "md": None,
            "problems": validation["problems"],
            "warnings": [],
            "client_version": VERSION,
            "capabilities": list(CLIENT_CAPABILITIES),
            "xingtu_preflight": "deferred",
            "code": validation["problems"][0].get("code"),
            "message": validation["problems"][0].get("message"),
            "failure_scope": validation["problems"][0].get("failure_scope"),
            "action": validation["problems"][0].get("action"),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_GATE_RED
    settings = load_settings(layout.settings_file)
    preview = build_preview(plan, envs=_envs(layout), channels=_channels(layout),
                            settings=settings,
                            license_info=_licence_gate(layout, license_path=getattr(args, 'license', None),
                                                       plan=plan))
    wrote = None
    if getattr(args, "confirm_token", None):
        check = check_confirmation(preview, plan, envs=_envs(layout),
                                   channels=_channels(layout), settings=settings,
                                   confirm_token=args.confirm_token,
                                   license_info=_licence_gate(layout, license_path=getattr(args, 'license', None),
                                                              plan=plan))
        preview["confirmation"] = {"ok": check["ok"], "reason": check["reason"]}
        if not check["ok"]:
            # a failed confirmation must NOT clobber the good stored preview
            print(json.dumps({"ok": False, "json": None, "md": None,
                              "confirm_token": preview["confirm_token"],
                              "confirm_ref": preview["confirm_ref"],
                              "problems": preview["problems"],
                              "warnings": preview["warnings"],
                              "confirmation": preview["confirmation"],
                              "idempotency_key": preview.get("idempotency_key"),
                              "client_version": VERSION,
                              "capabilities": list(CLIENT_CAPABILITIES),
                              "xingtu_preflight": "deferred"},
                             ensure_ascii=False, indent=2))
            return EXIT_GATE_RED
    if getattr(args, "no_write", False):
        wrote = {"json": None, "md": None, "written": False,
                 "confirm_token": preview["confirm_token"],
                 "confirm_ref": preview["confirm_ref"]}
    else:
        wrote = write_preview(layout, preview, out=args.out, out_md=args.out_md)
    print(json.dumps({"ok": preview["ok"], **wrote,
                      "problems": preview["problems"],
                      "warnings": preview["warnings"],
                      "confirmation": preview.get("confirmation"),
                      "idempotency_key": preview.get("idempotency_key"),
                      "client_version": VERSION,
                      "capabilities": list(CLIENT_CAPABILITIES),
                      "xingtu_preflight": "deferred"},
                     ensure_ascii=False, indent=2))
    return EXIT_OK if preview["ok"] else EXIT_GATE_RED


def _preview_for(layout, plan, *, license_path=None):
    from .dryrun import build_preview
    from .settings import load_settings
    settings = load_settings(layout.settings_file)
    preview = build_preview(
        plan, envs=_envs(layout), channels=_channels(layout),
        settings=settings,
        license_info=_licence_gate(layout, license_path=license_path,
                                   plan=plan))
    return preview, settings


def cmd_approval_submit(args):
    """1.2.13: after upload+fill, create a Feishu card instead of publishing.

    This command never clicks publish.  It writes the dry-run preview and the
    pending record locally, then asks the configured approval service to send
    the card.  The answer is read by ``approval-status``/``publish``.
    """
    from .dryrun import write_preview
    from .feishu_approval import STATUS_LABELS, submit_pending
    layout = _layout(args)
    plan = read_json(Path(args.plan))
    if not isinstance(plan, dict) or not plan:
        raise WinPortError(f"plan not found or unreadable: {args.plan}",
                           exit_code=EXIT_GATE_RED)
    preview, settings = _preview_for(layout, plan,
                                     license_path=getattr(args, "license", None))
    wrote = write_preview(layout, preview)
    record = submit_pending(layout, plan, preview, settings)
    result = {
        "ok": preview["ok"] and record.get("status") in ("pending", "confirmed"),
        "published": False,
        "status": record.get("status"),
        "status_label": STATUS_LABELS.get(record.get("status"),
                                           record.get("reason")),
        "approval_id": record.get("approval_id"),
        "expires_at": record.get("expires_at"),
        "timeout_minutes": record.get("timeout_minutes"),
        "confirmation": {
            "env": record.get("env_id"),
            "platform": record.get("platform"),
            "account_id": record.get("account_id"),
            "title": record.get("title"),
            "topics": record.get("topics"),
            "cover": record.get("cover"),
            "material_fingerprint": record.get("material_fingerprint"),
            "schedule_at": record.get("schedule_at"),
            "idempotency_key": record.get("idempotency_key"),
            "confirm_ref": record.get("confirm_ref"),
        },
        "feishu": record.get("feishu"),
        "preview": wrote,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK if result["ok"] else EXIT_GATE_RED


def cmd_approval_batch(args):
    """One Feishu card for a whole batch: confirm once, publish N works.

    The card carries scope=batch plus a cap, so each work in the batch passes
    the publish gate without asking again, and the cap stops runaway posting.
    """
    import hashlib
    from .feishu_approval import STATUS_LABELS, submit_pending
    layout = _layout(args)
    plans = []
    for path in (args.plans or []):
        plan = read_json(Path(path))
        if not isinstance(plan, dict) or not plan:
            raise WinPortError(f"plan not found or unreadable: {path}",
                               exit_code=EXIT_GATE_RED)
        plans.append((path, plan))
    if not plans:
        raise WinPortError("\u6ca1\u6709\u53ef\u6279\u91cf\u786e\u8ba4\u7684\u8ba1\u5212", exit_code=EXIT_GATE_RED)

    previews = []
    for path, plan in plans:
        preview, settings = _preview_for(layout, plan,
                                         license_path=getattr(args, "license", None))
        if not preview.get("ok"):
            raise WinPortError(f"\u8ba1\u5212\u672a\u901a\u8fc7\u9884\u68c0\uff0c\u4e0d\u80fd\u6279\u91cf\u786e\u8ba4\uff1a{path} "
                               f"{preview.get('problems')}", exit_code=EXIT_GATE_RED)
        previews.append(preview)

    accounts = {str(p.get("account_id") or "") for p in previews}
    if len(accounts) != 1:
        raise WinPortError("\u4e00\u6279\u53ea\u80fd\u5305\u542b\u540c\u4e00\u4e2a\u8d26\u53f7\u7684\u4f5c\u54c1", exit_code=EXIT_GATE_RED)
    account_id = accounts.pop()

    digest = hashlib.sha256("\n".join(
        sorted(str(p.get("idempotency_key") or "") for p in previews)).encode()).hexdigest()[:24]
    base = dict(previews[0])
    base["title"] = f"\u672c\u6279 {len(previews)} \u6761\u89c6\u9891"
    base["rows"] = [{"title": p.get("title"), "schedule_at": p.get("schedule_at"),
                     "account_id": p.get("account_id"),
                     "media_sha256": (p.get("rows") or [{}])[0].get("media_sha256")}
                    for p in previews]
    base["idempotency_key"] = "cadence:batch:" + digest
    base["confirm_token"] = "batch:" + digest
    base["batch_count"] = len(previews)
    plan0 = plans[0][1]

    cap = int(getattr(args, "cap", 0) or len(previews))
    hours = int(getattr(args, "hours", 24) or 24)
    extra = {"scope": "batch", "batch_cap": cap, "batch_count": len(previews),
             "account_id": account_id or base.get("account_id"),
             "timeout_minutes": max(1, hours * 60)}
    record = submit_pending(layout, plan0, base, settings, extra=extra)
    print(json.dumps({
        "ok": record.get("status") in ("pending", "confirmed"),
        "status": record.get("status"),
        "status_label": STATUS_LABELS.get(record.get("status"), record.get("reason")),
        "approval_id": record.get("approval_id"),
        "count": len(previews),
        "cap": cap,
        "hours": hours,
        "expires_at": record.get("expires_at"),
    }, ensure_ascii=False, indent=2))
    return EXIT_OK if record.get("status") in ("pending", "confirmed") else EXIT_GATE_RED


def cmd_approval_status(args):
    """1.2.13: read the Feishu answer and evaluate the publish gate."""
    from .feishu_approval import STATUS_LABELS, gate, load_record, refresh
    from .dryrun import read_preview
    from .settings import load_settings
    layout = _layout(args)
    settings = load_settings(layout.settings_file)
    record = load_record(layout, getattr(args, "approval_id", None))
    if not record:
        raise WinPortError("\u6ca1\u6709\u5f85\u786e\u8ba4\u5355\uff08\u5148\u8dd1 winport approval-submit\uff09",
                           exit_code=EXIT_GATE_RED)
    record = refresh(layout, record, settings)
    result = {
        "ok": record.get("status") == "confirmed",
        "status": record.get("status"),
        "status_label": STATUS_LABELS.get(record.get("status"),
                                           record.get("reason")),
        "approval_id": record.get("approval_id"),
        "expires_at": record.get("expires_at"),
        "decided_at": record.get("decided_at"),
        "decided_by": record.get("decided_by"),
        "published": False,
    }
    plan_path = getattr(args, "plan", None)
    if plan_path:
        plan = read_json(Path(plan_path))
        preview = _preview_for(layout, plan,
                               license_path=getattr(args, "license", None))[0]
        result["gate"] = gate(record, preview)
        result["ok"] = result["gate"]["ok"]
    else:
        stored = read_preview(layout, path=getattr(args, "preview", None))
        if stored:
            result["gate"] = gate(record, stored)
            result["ok"] = result["gate"]["ok"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK if result["ok"] else EXIT_GATE_RED


def cmd_evidence_list(args):
    from .evidence import EvidenceStore
    layout = _layout(args)
    store = EvidenceStore(layout)
    rows = store.list_entries()
    if args.limit:
        rows = rows[-int(args.limit):]
    print(json.dumps({"root": str(store.root),
                      "retention_days": store.retention_days,
                      "count": len(rows), "entries": rows},
                     ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_evidence_prune(args):
    from .evidence import EvidenceStore
    layout = _layout(args)
    store = EvidenceStore(layout)
    result = store.prune(days=args.days)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_report_record(args):
    """1.2.9: \u6bcf\u6b21\u63d0\u4ea4\u7d20\u6750\u8bb0\u4e00\u884c\u2014\u2014\u7d20\u6750\u540d + \u76ee\u6807\u6296\u97f3\u53f7\uff08\u540d + \u53f7 ID\uff09+ \u7ed3\u679c + \u5931\u8d25\u539f\u56e0\u3002"""
    from .report_center import ReportCenter
    layout = _layout(args)
    row = ReportCenter(layout).record(
        args.work, args.account_name, args.account_id,
        result=args.result, failure_reason=args.reason or "",
        source_folder=args.source_folder or "")
    print(json.dumps(row, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_report_export(args):
    """1.2.9: \u5bfc\u51fa\u6241\u5e73 Excel\uff08\u5355 Sheet\uff0c\u64ad\u653e\u91cf\u964d\u5e8f\uff0c\u672b\u884c\u5408\u8ba1\uff09\u3002"""
    from .report_center import ReportCenter
    layout = _layout(args)
    center = ReportCenter(layout)
    path = center.export_flat_xlsx(args.out)
    print(json.dumps({"xlsx": str(path), "rows": len(center.rows())},
                     ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_publish_key(args):
    """C: print the idempotency key (\u7d20\u6750\u6307\u7eb9 + \u73af\u5883 + \u65f6\u95f4\u7a97) for a plan."""
    from .idempotency import material_fingerprint, publish_key, time_window_bucket
    layout = _layout(args)
    plan = read_json(Path(args.plan))
    piece = (plan or {}).get("piece") or {}
    env_id = plan.get("env_id") or (str(plan.get("channel_id", "")).split(":", 1)[0]
                                    if ":" in str(plan.get("channel_id", "")) else None)
    schedule_at = plan.get("schedule_at")
    if not env_id:
        raise WinPortError("plan has no env_id/channel_id; cannot scope the key")
    fp = material_fingerprint(piece, account_id=plan.get("account_id", ""))
    key = publish_key(env_id, piece, schedule_at,
                      account_id=plan.get("account_id", ""),
                      window_minutes=args.window_minutes)
    print(json.dumps({"env_id": env_id, "material_fingerprint": fp,
                      "time_window": time_window_bucket(
                          schedule_at, window_minutes=args.window_minutes),
                      "window_minutes": args.window_minutes,
                      "idempotency_key": key}))
    return EXIT_OK


def cmd_state(args):
    """D: environment / account state machine from the latest lights."""
    from . import health, state_machine
    layout = _layout(args)
    envs, channels = _registry(layout)
    if args.env:
        channels = [c for c in channels if c.env_id == args.env]
    prev = state_machine.load_previous(layout)
    rows = state_machine.annotate(health.probe_all(layout, channels), previous=prev)
    if args.persist:
        state_machine.persist_records(layout, rows)
    view = state_machine.view(rows, envs=envs)
    alerts = state_machine.env_alerts(
        [{"channel_id": k, "account_state": v.get("state")} for k, v in prev.items()],
        rows, env_labels={e.env_id: e.label for e in envs})
    print(json.dumps({"counts": view["totals"], "client_version": VERSION,
                      "capabilities": list(CLIENT_CAPABILITIES), "environments": [
        {"env_id": e["env_id"], "label": e["label"],
         "state": e["state"], "state_label": e["state_label"],
         "platforms": [{"channel_id": r["channel_id"],
                        "platform": r.get("platform_name"),
                        "state": r["account_state"],
                        "label": r["account_state_label"],
                        "reason": r["account_state_reason"]}
                       for r in e["rows"]]}
        for e in view["environments"]], "alerts": alerts},
        ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_license_notices(args):
    """E: remaining days + thresholds + whether NEW publishes are allowed."""
    from .license import expiry_notices
    layout = _layout(args)
    licence_path = args.license or layout.license_file
    obj = read_json(Path(licence_path), default=None)
    if obj is None:
        raise WinPortError(f"licence not found: {licence_path}")
    # 1.2.5 (GPT review item 1): this command must run the EXACT gate the
    # publish entry runs -- startup-chain integrity + fixed trust root + local
    # fingerprint + weak-fingerprint check + clock rollback + per-account seat --
    # and must NOT print its own competing "publish allowed" flag (the expiry
    # notice now reports only whether EXPIRY blocks; it cannot authorise).
    out = {"licence": str(licence_path),
           "expiry": expiry_notices(obj),
           "publish_gate": _licence_gate(
               layout, license_path=licence_path,
               plan={"account_id": getattr(args, "account_id", None)})}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_autostart_plan(args):
    from .autostart import plan_autostart
    layout = _layout(args)
    plan = plan_autostart(layout, Path(args.launcher))
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_instance_check(args):
    from .instance import InstanceGuard
    layout = _layout(args)
    guard = InstanceGuard(layout)
    running = guard.already_running()
    print(json.dumps({"already_running": running,
                      "lock_file": str(layout.agent_lock)}, ensure_ascii=False))
    return EXIT_OK


def cmd_diag_export(args):
    from . import diag_bundle
    result = diag_bundle.export(_layout(args))
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get("ok") else 1


def cmd_selftest(args):
    from .selftest import main as selftest_main
    argv = []
    if args.data:
        argv += ["--data", args.data]
    if getattr(args, "program", None):
        argv += ["--program", args.program]
    return selftest_main(argv)


def cmd_integrity_check(args):
    """Re-hash the startup chain against MANIFEST.json (GPT review item 5)."""
    from .integrity import verify_program
    layout = _layout(args)
    verdict = verify_program(layout.program)
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return EXIT_OK if verdict["ok"] else EXIT_CONFIG


def cmd_channels(args):
    layout = _layout(args)
    envs, channels = _registry(layout)
    rows = [{"channel_id": c.channel_id, "env_id": c.env_id,
             "platform": c.platform, "platform_name": c.platform_name,
             "nickname": c.nickname, "account_id": c.account_id,
             "engine": c.engine, "profile": str(c.profile_dir(layout)),
             "debug_port": c.debug_port, "enabled": c.enabled,
             "login_url": c.login_url, "check_url": c.check_url,
             "url_verified": c.url_verified,
             "cookie_domains": c.cookie_domains} for c in channels]
    print(json.dumps({"config": str(layout.environments_file
                                    if envs else layout.channels_file),
                      "environments": [e.describe() for e in envs],
                      "channels": rows},
                     ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_environments(args):
    from .environments import validate_environments
    layout = _layout(args)
    envs, channels = _registry(layout)
    problems = validate_environments(envs) if envs else []
    print(json.dumps({"config": str(layout.environments_file),
                      "environments": [e.describe() for e in envs],
                      "channel_count": len(channels),
                      "problems": problems},
                     ensure_ascii=False, indent=2))
    return EXIT_CONFIG if problems else EXIT_OK


def cmd_env_open(args):
    from .browser import open_environment
    layout = _layout(args)
    envs, _channels_ = _registry(layout)
    if not envs:
        raise WinPortError("no environments configured (config/environments.json "
                           "missing); legacy channels use `login-open`")
    target = [e for e in envs if e.env_id == args.env]
    if not target:
        raise WinPortError(f"unknown environment: {args.env}")
    result = open_environment(layout, target[0], url=args.url,
                              dry_run=args.dry_run,
                              chrome_path=getattr(args, "chrome_path", None),
                              edge_path=getattr(args, "edge_path", None))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("reason") == "DRY_RUN":
        return EXIT_OK
    return EXIT_OK if result.get("opened") else EXIT_CONFIG


def cmd_chrome_probe(args):
    from . import chrome
    layout = _layout(args)
    report = chrome.detect(layout, explicit=args.chrome_path, store=True,
                           edge_explicit=getattr(args, "edge_path", None))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get("found"):
        return EXIT_CONFIG
    if report.get("version_ok") is False:
        return EXIT_GATE_RED
    if (report.get("policy") or {}).get("blocked"):
        return EXIT_GATE_RED
    return EXIT_OK


def cmd_chrome_regression(args):
    """Re-run the CDP selector probes against the REAL Chrome (real-machine gate).

    The in-package Chromium assumption is void (source/extra-requirements-chrome.md
    item 6); this command is the executable form of the re-regression, so it can
    be run on the customer machine and the JSON result attached to the report.
    1.2.2: only a real Google Chrome endpoint counts as ``live`` -- an Edge or
    Chromium endpoint is reported as a mismatch, never accepted in its place.
    """
    from .regression import run_regression
    layout = _layout(args)
    envs, channels = _registry(layout)
    if args.env:
        channels = [c for c in channels if c.env_id == args.env]
    report = run_regression(layout, channels, port=args.port,
                            timeout=args.timeout, live=args.live)
    out = layout.evidence / "chrome-selector-regression.json"
    from .jsonio import write_json
    write_json(out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"evidence: {out}")
    if report["mode"] == "live":
        return EXIT_OK if report["all_ok"] else EXIT_GATE_RED
    # offline: no CDP endpoint on this host -- a real-machine item, never "green"
    return EXIT_SKIP


def cmd_login_open(args):
    from .browser import open_channel
    layout = _layout(args)
    targets = _select_channels(_channels(layout), args)
    results = [open_channel(layout, channel, dry_run=args.dry_run,
                            processes=None,
                            chrome_path=getattr(args, "chrome_path", None),
                            edge_path=getattr(args, "edge_path", None))
               for channel in targets]
    print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    failed = [r for r in results if not (r["opened"] or r["reason"] == "DRY_RUN")]
    if failed:
        # PROFILE_IN_USE / disabled are "skipped", a missing browser is config.
        reasons = {r["reason"] for r in failed}
        return EXIT_SKIP if reasons <= {"PROFILE_IN_USE", "CHANNEL_DISABLED"} \
            else EXIT_CONFIG
    return EXIT_OK


def cmd_login_check(args):
    from .login_check import STATUS_DISABLED, STATUS_OFFLINE, STATUS_ONLINE
    from .login_check import check_all, persist, summary_text
    layout = _layout(args)
    channels = _select_channels(_channels(layout), args) if (
        args.channel or args.platform) else _channels(layout)
    rows = check_all(layout, channels)
    persist(layout, rows)
    if args.json:
        print(json.dumps({"channels": rows}, ensure_ascii=False, indent=2))
    else:
        print(summary_text(rows))
    bad = [r for r in rows if r["status"] == STATUS_OFFLINE]
    unsure = [r for r in rows if r["status"] not in (STATUS_ONLINE, STATUS_DISABLED)]
    if bad:
        return EXIT_GATE_RED
    if unsure and args.require_online:
        return EXIT_GATE_RED
    return EXIT_OK


def cmd_panel(args):
    layout = _layout(args)
    envs, channels = _registry(layout)
    if args.print_url_only:
        from .platforms import DEFAULT_PANEL_PORT
        print(f"http://127.0.0.1:{args.port or DEFAULT_PANEL_PORT}/")
        return EXIT_OK
    from .jsonio import write_json
    from .panel import make_server, open_panel, panel_url
    server, _handlers = make_server(layout, channels, port=args.port, envs=envs)
    url = panel_url(server.server_address)
    write_json(layout.run / "panel.json", {"url": url, "channels":
                                           [c.channel_id for c in channels]})
    print(f"panel serving at {url} (Ctrl+C to stop)")
    if args.open:
        open_panel(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("panel stopped")
    finally:
        server.server_close()
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="winport",
                                     description="publish-assistant Windows agent")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_paths(p):
        p.add_argument("--data")
        p.add_argument("--program")
        return p

    pq = with_paths(sub.add_parser("publish-enqueue"))
    pq.add_argument("--plan", required=True)
    pq.add_argument("--receipt", required=True)
    pq.set_defaults(func=cmd_publish_enqueue)
    hc = with_paths(sub.add_parser("publish-hold-clear"))
    hc.add_argument("--channel", required=True)
    hc.add_argument("--reason", required=True)
    hc.add_argument("--actor", choices=("human", "bot"), default="human")
    hc.add_argument("--reason-code", default=None)
    hc.set_defaults(func=cmd_publish_hold_clear)
    hs = with_paths(sub.add_parser("publish-hold-status"))
    hs.add_argument("--channel", required=True)
    hs.set_defaults(func=cmd_publish_hold_status)
    with_paths(sub.add_parser("env-probe")).set_defaults(func=cmd_env_probe)
    sub.add_parser("fingerprint").set_defaults(func=cmd_fingerprint)
    with_paths(sub.add_parser("diag-export")).set_defaults(func=cmd_diag_export)

    from .named_ops import OPERATIONS as _NAMED_OPS
    for _name in _NAMED_OPS:
        _np = with_paths(sub.add_parser(_name))
        _np.add_argument("--args-json", default="{}",
                         help="ASCII JSON object with the operation's arguments")
        _np.set_defaults(func=cmd_named_op, named_op=_name)
    ra = with_paths(sub.add_parser("remote-assist"))
    ra.add_argument("action", choices=("on", "off", "status"))
    ra.add_argument("--hours", type=float, default=None)
    ra.set_defaults(func=cmd_remote_assist)

    mc = with_paths(sub.add_parser("machine-code"))
    mc.add_argument("--out", default=None,
                    help="where to write the machine-code file (default <data>\\logs\\\u673a\u5668\u7801.txt)")
    mc.add_argument("--json", action="store_true")
    mc.set_defaults(func=cmd_machine_code)

    li = with_paths(sub.add_parser("license-import"))
    li.add_argument("--src", default=None,
                    help="the signed license.json (default: package license\\, package root, Desktop)")
    li.add_argument("--dry-run", action="store_true",
                    help="validate without installing")
    li.add_argument("--json", action="store_true")
    li.set_defaults(func=cmd_license_import)

    lic = with_paths(sub.add_parser("license-check"))
    lic.add_argument("--license", default=None)
    lic.add_argument("--accounts", action="store_true",
                     help="also print per-\u53f7 authorisation (one seat per account)")
    lic.set_defaults(func=cmd_license_check)

    dl = with_paths(sub.add_parser("downlink-once"))
    dl.add_argument("--bucket")
    dl.add_argument("--token-endpoint")
    dl.add_argument("--customer-id", default=None)
    dl.set_defaults(func=cmd_downlink)

    bind = with_paths(sub.add_parser("bind"))
    bind.add_argument("--manifest", required=True)
    bind.add_argument("--folder", required=True)
    bind.set_defaults(func=cmd_bind)

    col = with_paths(sub.add_parser("collect-offline"))
    col.add_argument("--xlsx", required=True)
    col.add_argument("--allow-drift", action="store_true")
    col.set_defaults(func=cmd_collect_offline)

    call = with_paths(sub.add_parser("collect-all"))
    only = call.add_mutually_exclusive_group()
    only.add_argument("--collect-only", action="store_true",
                      help="\u53ea\u53d6\u6570\uff0c\u4e0d\u51fa\u8001\u677f\u8868")
    only.add_argument("--report-only", action="store_true",
                      help="\u53ea\u7528\u5df2\u6709 JSON \u51fa\u8001\u677f\u8868\uff0c\u4e0d\u91cd\u65b0\u53d6\u6570")
    call.add_argument("--skip-douyin", action="store_true")
    call.add_argument("--skip-xingtu", action="store_true")
    call.add_argument("--day", default=None)
    call.add_argument("--out", default=None)
    call.set_defaults(func=cmd_collect_all)

    rep = with_paths(sub.add_parser("report"))
    rep.add_argument("--day", default=None)
    rep.add_argument("--collection-json", default=None)
    rep.set_defaults(func=cmd_report)

    pub = with_paths(sub.add_parser("publish"))
    pub.add_argument("--plan", required=True)
    pub.add_argument("--preview", default=None)
    pub.add_argument("--mode", choices=["fill", "approved_publish"], default="fill")
    pub.add_argument("--receipt", default=None,
                     help="control-plane, Ed25519-signed approval receipt")
    pub.add_argument("--canary-armed", action="store_true")
    pub.add_argument("--post-fill-green", action="store_true")
    pub.add_argument("--allow-submit", action="store_true",
                     help="\u663e\u5f0f\u5141\u8bb8\u5728\u5ba1\u6279\u901a\u8fc7\u540e\u771f\u7684\u70b9\u51fb\u300c\u53d1\u5e03\u300d\uff08\u9ed8\u8ba4\u5173\u95ed\uff09")
    pub.add_argument("--sms-code-file", default=None,
                     help="\u53d1\u5e03\u89e6\u53d1\u77ed\u4fe1\u4e8c\u6b21\u9a8c\u8bc1\u65f6\uff0c\u4ece\u8be5\u6587\u4ef6\u8bfb\u53d6\u5916\u90e8\u9a8c\u8bc1\u7801\uff1b"
                          "\u672a\u63d0\u4f9b\u5219\u4f7f\u7528 run/publish_sms_code.txt")
    pub.add_argument("--sms-wait-seconds", type=int, default=None,
                     help="\u7b49\u5f85\u77ed\u4fe1\u9a8c\u8bc1\u7801\u6587\u4ef6\u7684\u6700\u5927\u79d2\u6570\uff08\u9ed8\u8ba4\u53d6 settings \u6216 180\uff09")
    pub.add_argument("--verification-detect-seconds", type=int, default=None,
                     help="\u672c\u4eba\u9a8c\u8bc1\u5f39\u5c42\u51fa\u73b0\u540e\uff0c\u8bc6\u522b\u77ed\u4fe1/\u4e8c\u7ef4\u7801\u7684\u6700\u5927\u79d2\u6570"
                          "\uff08\u9ed8\u8ba4\u53d6 settings \u6216 30\uff09")
    pub.add_argument("--qr-wait-seconds", type=int, default=None,
                     help="\u4e8c\u7ef4\u7801\u672c\u4eba\u9a8c\u8bc1\u7b49\u5f85\u626b\u7801\u7684\u6700\u5927\u79d2\u6570\uff08\u9ed8\u8ba4\u53d6 settings \u6216 300\uff09")
    pub.add_argument("--confirm-token", default=None,
                     help="the dry-run confirm_token the operator confirmed "
                          "(mandatory for approved_publish)")
    pub.add_argument("--approval-id", default=None,
                     help="the Feishu approval id; must be confirmed before publish")
    pub.add_argument("--license", default=None)
    pub.add_argument("--republish-confirmed", action="store_true",
                     help="explicitly allow one customer-confirmed republish; "
                          "requires a receipt carrying the signed republish claim")
    pub.set_defaults(func=cmd_publish)

    xt = with_paths(sub.add_parser("xingtu-tasks"))
    xt.add_argument("--env", default=None)
    xt.add_argument("--channel", default=None)
    xt.add_argument("--limit", type=int, default=20)
    xt.set_defaults(func=cmd_xingtu_tasks)

    dr = with_paths(sub.add_parser("dry-run"))
    dr.add_argument("--plan", required=True)
    dr.add_argument("--out", default=None)
    dr.add_argument("--out-md", default=None)
    dr.add_argument("--no-write", action="store_true",
                    help="validate only; do not replace the stored preview files")
    dr.add_argument("--confirm-token", default=None,
                    help="verify a Feishu confirm token against a freshly derived preview")
    dr.add_argument("--license", default=None)
    dr.set_defaults(func=cmd_dry_run)

    aps = with_paths(sub.add_parser("approval-submit"))
    aps.add_argument("--plan", required=True)
    aps.add_argument("--license", default=None)
    aps.set_defaults(func=cmd_approval_submit)

    apb = with_paths(sub.add_parser("approval-batch"))
    apb.add_argument("--plans", nargs="+", required=True)
    apb.add_argument("--cap", type=int, default=0,
                     help="how many works this one confirmation covers")
    apb.add_argument("--hours", type=int, default=24,
                     help="how long the batch consent stays valid")
    apb.add_argument("--license", default=None)
    apb.set_defaults(func=cmd_approval_batch)

    ast = with_paths(sub.add_parser("approval-status"))
    ast.add_argument("--approval-id", default=None)
    ast.add_argument("--plan", default=None)
    ast.add_argument("--license", default=None)
    ast.set_defaults(func=cmd_approval_status)

    ev = with_paths(sub.add_parser("evidence-list"))
    ev.add_argument("--limit", type=int, default=None)
    ev.set_defaults(func=cmd_evidence_list)

    ep = with_paths(sub.add_parser("evidence-prune"))
    ep.add_argument("--days", type=int, default=None)
    ep.set_defaults(func=cmd_evidence_prune)

    pk = with_paths(sub.add_parser("publish-key"))
    pk.add_argument("--plan", required=True)
    pk.add_argument("--window-minutes", type=int, default=60)
    pk.set_defaults(func=cmd_publish_key)

    sm = with_paths(sub.add_parser("state"))
    sm.add_argument("--env", default=None)
    sm.add_argument("--persist", action="store_true")
    sm.set_defaults(func=cmd_state)

    ln = with_paths(sub.add_parser("license-notices"))
    ln.add_argument("--license", default=None)
    ln.add_argument("--account-id", default=None)
    ln.set_defaults(func=cmd_license_notices)

    auto = with_paths(sub.add_parser("autostart-plan"))
    auto.add_argument("--launcher", required=True)
    auto.set_defaults(func=cmd_autostart_plan)

    with_paths(sub.add_parser("instance-check")).set_defaults(func=cmd_instance_check)

    with_paths(sub.add_parser("integrity-check")).set_defaults(func=cmd_integrity_check)

    st = with_paths(sub.add_parser("selftest"))
    st.add_argument("--require-green", action="store_true")
    st.set_defaults(func=cmd_selftest)

    with_paths(sub.add_parser("channels")).set_defaults(func=cmd_channels)

    with_paths(sub.add_parser("environments")).set_defaults(func=cmd_environments)

    rr = with_paths(sub.add_parser("report-record"))
    rr.add_argument("--work", required=True)
    rr.add_argument("--account-name", dest="account_name", required=True)
    rr.add_argument("--account-id", dest="account_id", required=True)
    rr.add_argument("--result", default="\u5f85\u786e\u8ba4",
                    choices=["\u6210\u529f", "\u5931\u8d25", "\u5f85\u786e\u8ba4"])
    rr.add_argument("--reason", default="")
    rr.add_argument("--source-folder", dest="source_folder", default="")
    rr.set_defaults(func=cmd_report_record)

    rx = with_paths(sub.add_parser("report-export"))
    rx.add_argument("--out", default=None)
    rx.set_defaults(func=cmd_report_export)

    eo = with_paths(sub.add_parser("env-open"))
    eo.add_argument("--env", required=True)
    eo.add_argument("--url", default=None)
    eo.add_argument("--dry-run", action="store_true")
    eo.add_argument("--chrome-path", default=None)
    eo.add_argument("--edge-path", default=None,
                    help="override the detected Edge path (diagnostics only; "
                         "Edge never runs)")
    eo.set_defaults(func=cmd_env_open)

    cp = with_paths(sub.add_parser("chrome-probe"))
    cp.add_argument("--chrome-path", default=None)
    cp.add_argument("--edge-path", default=None,
                    help="override the detected Edge path (diagnostics only)")
    cp.set_defaults(func=cmd_chrome_probe)

    # 1.2.2 alias: Google Chrome is again the ONLY acceptable browser, but the
    # neutral name is kept so existing runbooks / the installer keep working.
    bp = with_paths(sub.add_parser("browser-probe"))
    bp.add_argument("--chrome-path", default=None)
    bp.add_argument("--edge-path", default=None,
                    help="override the detected Edge path (diagnostics only)")
    bp.set_defaults(func=cmd_chrome_probe)

    cr = with_paths(sub.add_parser("chrome-regression"))
    cr.add_argument("--env", default=None)
    cr.add_argument("--port", type=int, default=None)
    cr.add_argument("--timeout", type=float, default=5.0)
    cr.add_argument("--live", action="store_true",
                    help="actually drive CDP (real machine)")
    cr.set_defaults(func=cmd_chrome_regression)

    op = with_paths(sub.add_parser("login-open"))
    op.add_argument("--channel")
    op.add_argument("--platform")
    op.add_argument("--env")
    op.add_argument("--all", action="store_true")
    op.add_argument("--dry-run", action="store_true")
    op.add_argument("--chrome-path", default=None,
                    help="override the detected Chrome path (diagnostics)")
    op.add_argument("--edge-path", default=None,
                    help="override the detected Edge path (diagnostics only; "
                         "Edge never runs)")
    op.set_defaults(func=cmd_login_open)

    lc = with_paths(sub.add_parser("login-check"))
    lc.add_argument("--channel")
    lc.add_argument("--platform")
    lc.add_argument("--env")
    lc.add_argument("--json", action="store_true")
    lc.add_argument("--require-online", action="store_true",
                    help="treat UNVERIFIED as failure too (strict gate)")
    lc.set_defaults(func=cmd_login_check)

    pn = with_paths(sub.add_parser("panel"))
    pn.add_argument("--port", type=int, default=None)
    pn.add_argument("--open", action="store_true",
                    help="open the panel in the default browser")
    pn.add_argument("--print-url-only", action="store_true")
    pn.set_defaults(func=cmd_panel)
    return parser


def known_commands() -> set:
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def main(argv=None) -> int:
    # `python -m winport` with no subcommand (this is what launcher.vbs runs)
    # must start the RESIDENT AGENT, not print argparse usage.
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    if not raw or raw[0] not in known_commands():
        from .main import run as run_agent
        return run_agent(raw)
    parser = build_parser()
    args = parser.parse_args(raw)
    try:
        return args.func(args)
    except WinPortError as exc:
        print(f"ERROR({exc.exit_code}): {exc.message}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:      # 1.2.5: never a bare traceback + exit 0/1.
        print(f"ERROR({EXIT_CONFIG}): \u547d\u4ee4\u6267\u884c\u5931\u8d25\uff1a{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
