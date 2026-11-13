"""Fail-closed Chrome CDP publishing.

1.2.30 accepts only the bound create_v2 receipt (HTTP 200, status_code=0,
19-digit item_id). Neither lists nor popup visibility decide success. Terminal
verification/disconnect faults remain latched until ledger consumption.
The endpoint and ID schema require real-account + SMS validation; local
Chromium fixtures do not prove platform or Windows acceptance.
"""

from __future__ import annotations

import asyncio
import ntpath
import os
import re
import sys
import time
import json
import hashlib
import tempfile
import shutil
import threading
from datetime import datetime, timezone
import unicodedata
from urllib.parse import parse_qs, urlparse
from dataclasses import asdict
from pathlib import Path

from .platform_receipt import PublishReceipt, install_monitor, VISIBLE_TEXT_SCRIPT
from .approval import require_receipt
from .errors import CanaryNotArmed, GateRed, NoSubmitExit
from .evidence import EvidenceStore
from .feishu_approval import gate as feishu_gate
from .humanize import Humanizer, get_pace_profile
from .stage2_ledger import Stage2Ledger, content_hash, idempotency_key
from .idempotency import material_fingerprint, publish_key
from . import xingtu_tasks
from .version import VERSION
from .publish_guard import (CAPTCHA, LOGIN_LOST, RISK_CONTROL, SMS_VERIFICATION,
                            PublishHoldStore, UploadAttempt, classify_fault,
                            make_upload_attempt, media_metadata, ffprobe_media_metadata,
                            transcode_failure_reasons, upload_failure_reasons,
                            validate_selectors,
                            is_ascii_mid,
                            PUBLISH_UNKNOWN)

#: Faults that must still freeze the channel when they happen *before* the
#: publish click.  A pre-submit failure normally only recycles its ledger
#: reservation (nothing was submitted, so retrying is safe), but a captcha,
#: an SMS verification challenge, a risk-control stop or a lost login blocks
#: every task queued behind it until a human fixes the account.
PRE_SUBMIT_FREEZE_KINDS = frozenset({CAPTCHA, RISK_CONTROL, LOGIN_LOST,
                                     SMS_VERIFICATION})

# Internal deadline accounting must not consume a test's patched
# ``time.monotonic`` side-effect sequence meant for publish logic.
_MONOTONIC = time.monotonic


UPLOAD_BODY_EVICTED_MARKERS = (
    "evicted", "unavailable", "no resource", "not found",
    "target closed", "has been closed", "has been navigat",
    "navigated away", "response body is unavailable",
)

UPLOAD_EVIDENCE_TIMEOUT_SECONDS = 600
UPLOAD_EVIDENCE_TIMEOUT_MAX_SECONDS = 900
UPLOAD_WAIT_POLL_INTERVAL_SECONDS = 2.0
UPLOAD_WAIT_LOG_INTERVAL_SECONDS = 10.0
UPLOAD_PROGRESS_LOG_INTERVAL_SECONDS = 10.0
COVER_PANEL_WAIT_SECONDS = 90.0
COVER_PANEL_POLL_INTERVAL_SECONDS = 0.5
COVER_CHECK_TEXTS = ("\u5c01\u9762\u68c0\u6d4b",)
COVER_CHECK_RECHECK_TEXTS = ("\u91cd\u65b0\u68c0\u6d4b",)
COVER_CHECK_OK_TEXTS = (
    "\u5c01\u9762\u68c0\u6d4b\u901a\u8fc7",
    "\u6682\u672a\u53d1\u73b0\u5c01\u9762\u4f4e\u8d28\u95ee\u9898",
)
COVER_CHECK_FAIL_TEXTS = (
    "\u4f4e\u8d28\u95ee\u9898\u8f83\u591a",
    "\u5c01\u9762\u68c0\u6d4b\u672a\u901a\u8fc7",
    "\u68c0\u6d4b\u4e0d\u901a\u8fc7",
    "\u5c01\u9762\u8d28\u91cf\u8f83\u4f4e",
)
COVER_MODAL_PASS_TEXTS = (
    "\u5c01\u9762\u68c0\u6d4b\u901a\u8fc7",
    "\u6682\u672a\u53d1\u73b0\u5c01\u9762\u4f4e\u8d28\u95ee\u9898",
)
COVER_MODAL_FAIL_TEXTS = (
    "\u4f4e\u8d28\u95ee\u9898\u8f83\u591a",
    "\u5c01\u9762\u68c0\u6d4b\u672a\u901a\u8fc7",
    "\u5c01\u9762\u8d28\u91cf\u8f83\u4f4e",
)
COVER_MODAL_FRAME_RATIO = 0.23
COVER_CHECK_CONTROL_WAIT_SECONDS = 20.0
COVER_CHECK_TIMEOUT_SECONDS = 180.0
COVER_CHECK_POLL_INTERVAL_SECONDS = 0.5
COVER_FRAME_SELECT_WAIT_SECONDS = 90.0
COVER_FRAME_POLL_INTERVAL_SECONDS = 2.0
COVER_FRAME_SCAN_LOG_INTERVAL_SECONDS = 10.0
COVER_FRAME_CLICK_RECHECK_SECONDS = 1.0
COVER_FRAME_MAX_ATTEMPTS = 3
COVER_FRAME_MIN_SIZE = 40
COVER_UPLOAD_TIMEOUT_SECONDS = 25.0
COVER_AI_TIMEOUT_SECONDS = 150.0

class _ElementScope:
    """A stable DOM element plus scoped queries; no index-based re-resolution."""
    def __init__(self, handle): self.handle = handle
    def __getattr__(self, name): return getattr(self.handle, name)
    def locator(self, selector): return _ElementQuery(self.handle, selector)


class _ElementQuery:
    def __init__(self, root, selector): self.root, self.selector = root, selector
    def _all(self): return self.root.query_selector_all(self.selector)
    def count(self): return len(self._all())
    def nth(self, index): return _ElementScope(self._all()[index])
    def evaluate_all(self, script):
        return self.root.eval_on_selector_all(self.selector, script)


MODE_FILL = "fill"
MODE_APPROVED = "approved_publish"

DOUYIN_UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
DOUYIN_MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"
ACCOUNT_INFO_URL = "https://creator.douyin.com/web/api/media/user/info/"

# Candidate selectors are deliberately lists: the platform changes its DOM, so
# the driver checks them in order and records exactly which one matched.
# 1.2.21: zero class-name locators and exact text/attribute semantics.  Any
# selector in this module is validated by ``publish_guard.validate_selectors``.
TITLE_SELECTORS = (
    "input[placeholder*=\"\u4f5c\u54c1\u6807\u9898\"]",
    "input[placeholder='\u4f5c\u54c1\u6807\u9898']",
    "input[placeholder='\u6807\u9898']",
    "textarea[placeholder='\u4f5c\u54c1\u6807\u9898']",
    "textarea[placeholder='\u6807\u9898']",
    "[contenteditable='true'][data-placeholder='\u6807\u9898']",
    "[contenteditable='true'][aria-label='\u6807\u9898']",
)
TOPIC_SELECTORS = (
    "input[placeholder='\u8bdd\u9898']",
    "textarea[placeholder='\u8bdd\u9898']",
    "[contenteditable='true'][data-placeholder='\u8bdd\u9898']",
    "[contenteditable='true'][aria-label='\u8bdd\u9898']",
)
COVER_BUTTON_SELECTORS = (
    "button:text-is('\u9009\u5c01\u9762')",
    "button:text-is('\u9009\u62e9\u5c01\u9762')",
    "[role='button'][aria-label='\u9009\u5c01\u9762']",
    "[role='button'][aria-label='\u9009\u62e9\u5c01\u9762']",
)
COVER_INPUT_SELECTORS = (
    "input[type=file][accept='image/*']",
    "input[type=file][aria-label='\u4e0a\u4f20\u5c01\u9762']",
    "input[type=file][aria-label='\u9009\u62e9\u5c01\u9762']",
)
PUBLISH_BUTTON_SELECTORS = (
    "button:text-is('\u7acb\u5373\u53d1\u5e03')",
    "button:text-is('\u53d1\u5e03')",
    "[role='button'][aria-label='\u53d1\u5e03']",
    "[data-e2e='publish']",
)
UPLOAD_PROGRESS_SELECTORS = (
    ":text-is('\u4e0a\u4f20\u4e2d')",
    ":text-is('\u5904\u7406\u4e2d')",
    ":text-is('\u89c6\u9891\u5904\u7406\u4e2d')",
    "[role='progressbar']",
)
# Success comes only from the bound platform receipt, never toast, page text,
# data-work-id, edit links or popup visibility.
TRANSCODE_DONE_SELECTORS = (
    ":text-is('\u4e0a\u4f20\u5b8c\u6210')",
    ":text-is('\u89c6\u9891\u5904\u7406\u5b8c\u6210')",
    ":text-is('\u8f6c\u7801\u5b8c\u6210')",
    "[data-e2e='transcode-done']",
)
# 1.2.96: Douyin's publish page does not always keep a transcode status node
# or a "transcode complete" label.  These are the page facts that prove the
# editor is ready: a real publish button plus no blocking status text.
# selector-ok: read-only
TRANSCODE_PUBLISH_BUTTON_SELECTORS = (
    "button:has-text('\u53d1\u5e03')",
    "button:text-is('\u4f5c\u54c1\u53d1\u5e03')",
    "[role='button']:text-is('\u53d1\u5e03')",
    "[role='button']:text-is('\u4f5c\u54c1\u53d1\u5e03')",
)
TRANSCODE_BLOCKING_TEXTS = (
    "\u8f6c\u7801\u4e2d",
    "\u4e0a\u4f20\u4e2d",
    "\u5904\u7406\u4e2d",
)
TRANSCODE_FAILED_TEXT = "\u8f6c\u7801\u5931\u8d25"
TRANSCODE_WAIT_TIMEOUT_SECONDS = 180.0
TRANSCODE_POLL_INTERVAL_SECONDS = 3.0
TRANSCODE_STAGE_LOG_INTERVAL_SECONDS = 15.0
EDIT_LINK_SELECTORS = (
    "a:text-is('\u7f16\u8f91\u4f5c\u54c1')",
    "a:text-is('\u7f16\u8f91')",
)
MANAGE_EMPTY_STATE_SELECTORS = (
    ":text-is('\u6682\u65e0\u4f5c\u54c1')",
    ":text-is('\u8fd8\u6ca1\u6709\u53d1\u5e03\u4f5c\u54c1')",
    ":text-is('\u6682\u65e0\u5185\u5bb9')",
)
MANAGE_NEXT_PAGE_SELECTORS = (
    "button:text-is('\u4e0b\u4e00\u9875')",
    "[role='button'][aria-label='\u4e0b\u4e00\u9875']",
    "a:text-is('\u4e0b\u4e00\u9875')",
)
PUBLISHED_AT_SELECTORS = (
    "time[datetime][aria-label='\u53d1\u5e03\u65f6\u95f4']",
    "[data-e2e='publish-time']",
    "[aria-label='\u53d1\u5e03\u65f6\u95f4']",
)
# P0 (2026-11-13): playwright refuses to stream >50MB to a browser that is
# not co-located; files above this go through CDP DOM.setFileInputFiles.
LARGE_UPLOAD_CDP_BYTES = 45 * 1000 * 1000

UPLOAD_RESOURCE_SELECTORS = (
    "[data-upload-id]",
    "[data-resource-id]",
    "[data-e2e='upload-id']",
)
BODY_SELECTORS = (
    "[contenteditable='true']",
    "[data-placeholder='\u6dfb\u52a0\u4f5c\u54c1\u7b80\u4ecb']",
    "[contenteditable='true'][data-placeholder='\u6dfb\u52a0\u4f5c\u54c1\u7b80\u4ecb']",
    "textarea[placeholder='\u4f5c\u54c1\u63cf\u8ff0']",
    "[contenteditable='true'][data-placeholder='\u4f5c\u54c1\u63cf\u8ff0']",
    "[contenteditable='true'][aria-label='\u4f5c\u54c1\u63cf\u8ff0']",
    "[contenteditable='true'][data-placeholder*='\u4f5c\u54c1\u63cf\u8ff0']",
)
ACCOUNT_SELECTORS = ("[data-e2e='current-account'][data-account-id]",)
ONBOARDING_DONE_SELECTORS = (
    "button:text-is('\u5b8c\u6210')",
    "[role='button'][aria-label='\u5b8c\u6210']",
)
VIDEO_PREVIEW_OK_SELECTORS = (
    "button:text-is('\u6211\u77e5\u9053\u4e86')",
    "[role='button'][aria-label='\u6211\u77e5\u9053\u4e86']",
)
COVER_HINT_DISMISS_SELECTORS = (
    "button:text-is('\u6682\u4e0d\u8bbe\u7f6e')",
    "[role='button'][aria-label='\u6682\u4e0d\u8bbe\u7f6e']",
)
# Preview-container selectors are a bonus signal only.  Douyin ships hashed
# class names after a build, so a selector miss must never hide a real preview.
# selector-ok: read-only
VIDEO_PREVIEW_CONTAINER_SELECTORS = (
    "[data-e2e*='preview']",
    "[data-testid*='preview']",
    "[data-upload-preview]",
    "[class*='preview']",
    "[id*='preview']",
    "[class*='videoPreview']",
    "[class*='video-preview']",
    "[class*='videoContainer']",
    "[class*='video-container']",
    "[class*='videoPlayer']",
    "[class*='video-player']",
    "[class*='videoInfo']",
    "[class*='video-info']",
    "[class*='material']",
)
VERIFICATION_CONTAINER_SELECTORS = (
    "[role='dialog']",
    "[aria-modal='true']",
    "dialog",
)
# Backwards-compatible name for callers/tests written against 1.2.24.  The
# value is now structural: a verification popup is a dialog container, never a
# single hard-coded sentence.
SMS_PANEL_SELECTORS = VERIFICATION_CONTAINER_SELECTORS
SMS_GET_CODE_SELECTORS = (
    "p:text-is('\u83b7\u53d6\u9a8c\u8bc1\u7801')",
    "button:text-is('\u83b7\u53d6\u9a8c\u8bc1\u7801')",
    "[role='button']:text-is('\u83b7\u53d6\u9a8c\u8bc1\u7801')",
)
SMS_CODE_INPUT_SELECTORS = (
    "input[placeholder*='\u9a8c\u8bc1\u7801']",
    "input[placeholder='\u8bf7\u8f93\u5165\u9a8c\u8bc1\u7801']",
    "input[placeholder='\u9a8c\u8bc1\u7801']",
    "input[aria-label*='\u9a8c\u8bc1\u7801']",
)
SMS_VERIFY_BUTTON_SELECTORS = (
    "button:text-is('\u9a8c\u8bc1')",
    "button:text-is('\u786e\u8ba4')",
    "button:text-is('\u786e\u5b9a')",
    "[role='button'][aria-label='\u9a8c\u8bc1']",
)
QR_ELEMENT_SELECTORS = ("canvas", "img")
VERIFICATION_QR_MARKERS = ("\u4e8c\u7ef4\u7801", "\u626b\u7801", "\u626b\u4e00\u626b", "\u626b\u63cf")
VERIFICATION_FAILURE_MARKERS = (
    "\u9a8c\u8bc1\u5931\u8d25", "\u4e8c\u7ef4\u7801\u5df2\u5931\u6548", "\u4e8c\u7ef4\u7801\u5931\u6548", "\u9a8c\u8bc1\u5df2\u53d6\u6d88", "\u64cd\u4f5c\u5df2\u53d6\u6d88",
    "\u8fde\u63a5\u5931\u8d25", "\u7f51\u7edc\u5f02\u5e38", "\u8bf7\u91cd\u8bd5", "\u53d1\u5e03\u5931\u8d25", "\u63d0\u4ea4\u5931\u8d25", "\u53d1\u5e03\u5df2\u53d6\u6d88", "\u5df2\u53d6\u6d88\u53d1\u5e03",
)

# Post-click success signals.  The bound create_v2 response is preferred, but
# the real creator page can show a success toast/manage page before (or without)
# a readable response body.  "Pending" is not a signal: only explicit success.
PUBLISH_SUCCESS_MARKERS = (
    "\u53d1\u5e03\u6210\u529f", "\u4f5c\u54c1\u53d1\u5e03\u6210\u529f", "\u53d1\u5e03\u5df2\u5b8c\u6210", "\u6295\u7a3f\u6210\u529f",
)
PUBLISH_FAILURE_MARKERS = (
    "\u53d1\u5e03\u5931\u8d25", "\u4f5c\u54c1\u53d1\u5e03\u5931\u8d25", "\u63d0\u4ea4\u5931\u8d25", "\u53d1\u5e03\u5f02\u5e38",
    "\u4e0a\u4f20\u5931\u8d25", "\u4f5c\u54c1\u5ba1\u6838\u672a\u901a\u8fc7",
)

VERIFICATION_SMS = "sms"
VERIFICATION_QR = "qr"
DEFAULT_VERIFICATION_DETECT_SECONDS = 30
DEFAULT_QR_WAIT_SECONDS = 300
DEFAULT_POST_PUBLISH_WAIT_SECONDS = 60
DEFAULT_PUBLISH_SUCCESS_WAIT_SECONDS = 25.0
DEFAULT_MANAGE_STABLE_SECONDS = 15.0
DEFAULT_MANAGE_LIST_READY_SECONDS = 20.0
DEFAULT_TOPIC_CONVERSION_SECONDS = 8.0

# Real Douyin publish page labels observed on 2026-10-21.  These are text
# labels, not invented data attributes: the old fake cover-attribute contract
# never existed on the live page.
COVER_SET_TEXTS = ("\u8bbe\u7f6e\u5c01\u9762",)
COVER_SELECT_TEXTS = ("\u9009\u62e9\u5c01\u9762",)
COVER_EDIT_TEXTS = ("\u7f16\u8f91\u5c01\u9762",)
COVER_ENTRY_TEXTS = COVER_SELECT_TEXTS + COVER_SET_TEXTS + COVER_EDIT_TEXTS
COVER_RECOMMEND_TEXTS = (
    "AI\u751f\u6210\u5c01\u9762",
    "Ai\u667a\u80fd\u63a8\u8350\u5c01\u9762",
    "AI\u667a\u80fd\u63a8\u8350\u5c01\u9762",
    "AI\u667a\u80fd\u63a8\u8350",
    "AI\u5c01\u9762",
)
# 1.2.94: the live panel may expose any one of these controls while the
# others are absent.  A hit on any label means the panel rendered.
COVER_PANEL_CONTROL_TEXTS = COVER_RECOMMEND_TEXTS + ("\u4e0a\u4f20\u5c01\u9762",)
COVER_CONTROL_QUERY_ATTEMPTS = 5
COVER_CONTROL_QUERY_RETRY_MS = 300
COVER_CONTROL_QUERY_TIMEOUT_SECONDS = 5.0
COVER_PANEL_SETTLE_MS = 2000
COVER_ENTRY_CLICK_TIMEOUT_MS = 8000
COVER_FRAME_VIEW_RETRY_ATTEMPTS = 3
COVER_FRAME_VIEW_RETRY_SECONDS = 2.0
COVER_PANEL_READY_TEXTS = ("\u4e0a\u4f20\u5c01\u9762", "\u5c01\u9762\u68c0\u6d4b",
                           "\u5b8c\u6210", "\u786e\u5b9a")
COVER_AI_TAB_TEXTS = ("AI\u5c01\u9762",)
COVER_AI_GENERATE_TEXTS = COVER_RECOMMEND_TEXTS
COVER_AI_BUSY_TEXTS = ("\u751f\u6210\u4e2d",)
COVER_AI_CANCEL_TEXTS = ("\u53d6\u6d88\u751f\u6210",)
COVER_UPLOAD_TEXTS = (
    "\u4e0a\u4f20\u5c01\u9762",
    "\u70b9\u51fb\u4e0a\u4f20\u6587\u4ef6\u6216\u62d6\u62fd\u6587\u4ef6\u5230\u8fd9\u91cc",
)
# Text labels define the recommended-frame blocks.  Never use the live
# page's hashed class names here: those change between Douyin releases.
COVER_FRAME_SECTION_SPECS = (
    {"name": "vertical", "markers": (
        "\u8bbe\u7f6e\u7ad6\u5c01\u9762",
        "\u7ad6\u5c01\u97623:4",
    )},
    {"name": "horizontal", "markers": (
        "\u8bbe\u7f6e\u6a2a\u5c01\u9762",
        "\u6a2a\u5c01\u97624:3",
    )},
)
COVER_PANEL_TEXTS = (
    "\u8bbe\u7f6e\u7ad6\u5c01\u9762",
    "\u8bbe\u7f6e\u6a2a\u5c01\u9762",
    "AI\u5c01\u9762",
    "AI\u751f\u6210\u5c01\u9762",
    "Ai\u667a\u80fd\u63a8\u8350\u5c01\u9762",
    "AI\u667a\u80fd\u63a8\u8350",
    "\u4e0a\u4f20\u5c01\u9762",
    "\u5c01\u9762\u68c0\u6d4b",
    "\u751f\u6210\u4e2d",
    "\u53d6\u6d88\u751f\u6210",
    "\u70b9\u51fb\u4e0a\u4f20\u6587\u4ef6\u6216\u62d6\u62fd\u6587\u4ef6\u5230\u8fd9\u91cc",
    "\u7ad6\u5c01\u9762\u9884\u89c8\uff083:4\uff09",
    "\u7ad6\u5c01\u9762\u9884\u89c8(3:4)",
    "\u6a2a\u5c01\u97624:3",
    "\u7ad6\u5c01\u97623:4",
    "\u8bbe\u7f6e\u6a2a\u5c01\u97624:3",
    "\u8bbe\u7f6e\u7ad6\u5c01\u97623:4",
    "\u8bbe\u7f6e\u6a2a\u5c01\u9762\uff084:3\uff09",
    "\u8bbe\u7f6e\u7ad6\u5c01\u9762\uff083:4\uff09",
)
COVER_HORIZONTAL_TEXTS = ("\u6a2a\u5c01\u97624:3",)
COVER_VERTICAL_TEXTS = ("\u7ad6\u5c01\u97623:4",)
COVER_DONE_TEXTS = ("\u5b8c\u6210", "\u786e\u5b9a")
# 1.2.95: the live panel renders the real control as a Semi primary button
# inside the footer.  Text scans also match its non-interactive label, so the
# done control is resolved structurally first and only then by text/role.
# 1.2.102: the vertical and horizontal cover editors each render a primary
# done button; the footer's last visible enabled one is the real control.
COVER_DONE_TEXT = "\u5b8c\u6210"
COVER_DONE_CLICK_TIMEOUT_MS = 5000
COVER_DONE_CLOSE_WAIT_SECONDS = 5.0
COVER_DONE_MAX_ROUNDS = 2
COVER_DETECT_ACTIVATE_ROUNDS = 3
COVER_DETECT_ACTIVATE_WAIT_SECONDS = 2.0
COVER_VERTICAL_ENTRY_TEXTS = (
    "\u8bbe\u7f6e\u7ad6\u5c01\u9762",
    "\u8bbe\u7f6e\u7ad6\u5c01\u9762 3:4",
    "\u8bbe\u7f6e\u7ad6\u5c01\u9762\uff083:4\uff09",
    "\u8bbe\u7f6e\u7ad6\u5c01\u9762(3:4)",
)
COVER_VERTICAL_PREVIEW_TEXTS = (
    "\u7ad6\u5c01\u9762\u9884\u89c8",
    "\u7ad6\u5c01\u9762\u9884\u89c8\uff083:4\uff09",
    "\u7ad6\u5c01\u9762\u9884\u89c8(3:4)",
)

# Real Xingtu picker labels observed in /tmp/modal.html on 2026-10-21.
XINGTU_PLACEHOLDER_TEXTS = ("\u8bf7\u9009\u62e9\u661f\u56fe\u4efb\u52a1",)
XINGTU_PICKER_OPEN_TEXTS = ("\u8bf7\u9009\u62e9\u661f\u56fe\u4efb\u52a1",
                            "\u9009\u62e9\u661f\u56fe\u4efb\u52a1")
XINGTU_REPLACE_TEXTS = ("\u66f4\u6362",)
XINGTU_SEARCH_SELECTOR = 'input[placeholder="\u8f93\u5165\u4efb\u52a1id/\u4efb\u52a1\u540d\u79f0"]'
# selector-ok: read-only
XINGTU_PORTAL_SELECTOR = 'div[class*="semi-portal"]'
#: The picker is one exact Semi Design portal.  Use the exact class for the
#: selection contract; ``class*=`` also matches inner wrappers and must never
#: be used to decide "there is only one portal".
XINGTU_PICKER_PORTAL_SELECTOR = 'div.semi-portal'
# selector-ok: read-only
XINGTU_CARD_SELECTOR = 'div[class*="card-container-"]'
XINGTU_CARD_CLASS_RE = (r"^card-container-(?!creator-layout(?:$|-))"
                        r"[A-Za-z0-9_-]{4,}$")
# selector-ok: read-only
XINGTU_TASK_NAME_SELECTOR = '[class*="task-name"]'
# selector-ok: read-only
XINGTU_TASK_TITLE_SELECTOR = ('[class*="task-name"], [class*="taskName"], '
                              '[class*="task-title"], [class*="task_title"]')
XINGTU_RADIO_SELECTOR = "label.semi-radio"
XINGTU_CONFIRM_SELECTOR = "button.semi-button-primary"


def _normalize_form_text(value):
    text = str(value or "").replace("\u200b", "").replace("\xa0", " ")
    return " ".join(text.split())


def _topic_name(topic):
    return str(topic or "").strip().lstrip("#").strip()


def _topic_token(topic):
    name = _topic_name(topic)
    return "#" + name if name else ""


def _topic_tokens_in_text(value):
    return re.findall(r"#[^\s#]+", str(value or ""))


def _unique_topic_tokens(values):
    tokens = []
    for value in values or ():
        token = _topic_token(value)
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _topic_close_enough(expected, observed):
    left = _topic_token(expected)
    right = _topic_token(observed)
    if not left or not right:
        return False
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(1 for a, b in zip(left, right) if a != b) <= 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    for index in range(len(longer)):
        if longer[:index] + longer[index + 1:] == shorter:
            return True
    return False


def _topic_token_in_text(token, text):
    """True only when the token stands alone in the text.

    A plain substring test would treat the half-typed "#温和" as if "#温和草本"
    had already landed, which would skip the commit and lose the tag.
    """
    name = _topic_token(token)
    body = str(text or "")
    if not name or not body:
        return False
    start = body.find(name)
    while start >= 0:
        tail = body[start + len(name):start + len(name) + 1]
        if tail == "" or tail.isspace():
            return True
        start = body.find(name, start + 1)
    return False


def _topic_distance(first, second):
    one = _topic_token(first)
    two = _topic_token(second)
    if one == two:
        return 0
    if not one or not two:
        return 99
    if len(one) == len(two):
        return sum(1 for a, b in zip(one, two) if a != b)
    shorter, longer = (one, two) if len(one) < len(two) else (two, one)
    if abs(len(one) - len(two)) == 1 and any(
            longer[:index] + longer[index + 1:] == shorter
            for index in range(len(longer))):
        return 1
    return 99


def _strip_topic_label(text, name, limit=1):
    """Remove standalone occurrences of one chip label, keeping partial words.

    ``limit`` equals the number of chips with that label: a tag that exists
    both as a chip and as plain text must keep its plain-text copy, otherwise
    the read-back loses a topic.
    """
    body = str(text or "")
    token = _topic_token(name)
    if not token or limit <= 0:
        return body
    removed = 0
    start = body.find(token)
    while start >= 0 and removed < limit:
        tail_pos = start + len(token)
        if tail_pos >= len(body) or body[tail_pos].isspace():
            body = body[:start] + " " + body[tail_pos:]
            removed += 1
            start = body.find(token, start + 1)
        else:
            start = body.find(token, start + 1)
    return body


def _match_topics(expected, observed):
    """Pair wanted tags with seen tags one-to-one (maximum cardinality).

    Exact spellings come first in each wanted tag's candidate list, and the
    pairing is a maximum bipartite matching, so neither a greedy pick nor the
    exact-locking of one tag can reject a complete pairing.  Returns
    (pairs, missing, unexpected); unexpected are observed tags left over.
    """
    pool = [str(item or "").strip() for item in (observed or []) if str(item or "").strip()]
    wanted = [str(item or "").strip() for item in (expected or []) if str(item or "").strip()]
    adjacency = {}
    for index, want in enumerate(wanted):
        exact = [pos for pos, seen in enumerate(pool)
                 if _topic_token(seen) == _topic_token(want)]
        near = [pos for pos, seen in enumerate(pool)
                if pos not in exact and _topic_distance(want, seen) <= 1]
        adjacency[index] = exact + near
    owner = {}

    def _augment(index, visited):
        for pos in adjacency.get(index, []):
            if pos in visited:
                continue
            visited.add(pos)
            if pos not in owner or _augment(owner[pos], visited):
                owner[pos] = index
                return True
        return False

    for index in range(len(wanted)):
        _augment(index, set())

    # Maximum cardinality is settled; now nudge the pairing towards exact
    # spellings by swapping a want out of a near-miss slot into its exact slot
    # whenever the displaced want can take the near-miss slot itself.
    def _exact_pairs(mapping):
        return sum(1 for pos, index in mapping.items()
                   if _topic_token(pool[pos]) == _topic_token(wanted[index]))

    best = _exact_pairs(owner)
    for _round in range(len(wanted) + 1):
        swapped = False
        for pos, index in list(owner.items()):
            if _topic_token(pool[pos]) == _topic_token(wanted[index]):
                continue
            for exact_pos in range(len(pool)):
                if _topic_token(pool[exact_pos]) != _topic_token(wanted[index]):
                    continue
                holder = owner.get(exact_pos)
                if holder is None or holder == index:
                    continue
                if _topic_distance(wanted[holder], pool[pos]) > 1:
                    continue
                trial = dict(owner)
                trial[exact_pos] = index
                trial[pos] = holder
                score = _exact_pairs(trial)
                if score > best:
                    owner = trial
                    best = score
                    swapped = True
                    break
            if swapped:
                break
        if not swapped:
            break

    pairs = [(_topic_token(wanted[index]), _topic_token(pool[pos]))
             for pos, index in sorted(owner.items(), key=lambda item: item[1])]
    paired = set(owner.values())
    missing = [_topic_token(wanted[i]) for i in range(len(wanted)) if i not in paired]
    unexpected = [_topic_token(item) for pos, item in enumerate(pool) if pos not in owner]
    return pairs, missing, unexpected


def _description_text_matches(actual, expected_body, expected_topics):
    """Accept the body alone (chip UI) or body plus plain-text topics."""
    actual_text = _normalize_form_text(actual)
    body = _normalize_form_text(expected_body)
    if actual_text == body:
        return True
    if not actual_text.startswith(body):
        return False
    rest = actual_text[len(body):].strip()
    if not rest:
        return True
    _, missing, unexpected = _match_topics(expected_topics or (), rest.split())
    return not missing and not unexpected


def _compact_text(value):
    text = str(value or "").replace("\u200b", "").replace("\u00a0", " ")
    return re.sub(r"\s+", "", text)


def _pause_page(page, milliseconds=250):
    """Wait without a fixed long sleep; this also pumps Playwright events."""
    try:
        page.wait_for_timeout(milliseconds)
    except Exception:
        time.sleep(max(0, milliseconds) / 1000.0)


def _page_url_value(page):
    """Best-effort URL for diagnostics; never let a closed page hide a fault."""
    try:
        return str(page.url or "")
    except Exception:
        return ""


def _is_creator_page_url(url):
    text = str(url or "").strip()
    if not text or text == "about:blank":
        return False
    try:
        parsed = urlparse(text)
    except Exception:
        return False
    return parsed.hostname == "creator.douyin.com"


def _host_of_url(url):
    """Hostname for tab bookkeeping; empty for blank/unparsable pages."""
    text = str(url or "").strip()
    if not text or text == "about:blank":
        return ""
    try:
        return (urlparse(text).hostname or "").lower()
    except Exception:
        return ""


def _creator_page_rank(page, preferred_url=""):
    url = _page_url_value(page)
    if preferred_url and url == preferred_url:
        return 0
    try:
        path = urlparse(url).path.rstrip("/")
    except Exception:
        path = ""
    if path.endswith("/content/upload"):
        return 1
    if path.endswith("/content/post/video"):
        return 2
    return 3


def _select_creator_page(pages, *, preferred_url=""):
    """Choose only a real creator page; never silently reuse another tab."""
    pages = list(pages or [])
    urls = [_page_url_value(page) for page in pages]
    candidates = [(index, page) for index, page in enumerate(pages)
                  if _is_creator_page_url(urls[index])]
    info = {
        "pages": urls,
        "creator_pages": [urls[index] for index, _page in candidates],
        "extra_tabs": max(0, len(pages) - 1),
        "ignored_tabs": len(pages) - len(candidates),
        "multiple_creator_pages": len(candidates) > 1,
    }
    if not candidates:
        return None, info
    index, page = min(
        candidates,
        key=lambda item: (_creator_page_rank(item[1], preferred_url), item[0]))
    info["selected"] = urls[index]
    info["selected_index"] = index
    return page, info


def _frame_url_value(frame):
    try:
        return str(frame.url or "")
    except Exception:
        return ""


# A single iframe must never decide the whole snapshot's runtime.  Playwright's
# sync ``locator.count()`` has no timeout argument, so check the frame state
# before the call and reject any frame whose query overruns this budget.
UPLOAD_FRAME_QUERY_TIMEOUT_SECONDS = 0.5


_LOG_PATH_KEYS = frozenset((
    "path", "file", "media", "screenshot", "html", "html_snapshot",
))
_MEDIA_EXTENSIONS = (
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".flv", ".wmv",
    ".webm", ".jpg", ".jpeg", ".png", ".webp", ".bmp",
)
_EDGE_PUNCTUATION = frozenset(
    "'\"`,;:，。；：、（）()[]【】<>!?…“”‘’《》「」『』")
_PATH_PREFIX_BOUNDARIES = frozenset("=:,;([{ \t\r\n")


def _log_basename(value):
    """Return only the file name for a path that may enter a log line."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    name = ntpath.basename(text.rstrip("/\\"))
    return name or "<redacted-path>"


def _starts_with_drive(text, index):
    return (
        index + 2 < len(text)
        and text[index].isalpha()
        and text[index + 1] == ":"
        and text[index + 2] in "\\/"
    )


def _starts_with_unc(text, index):
    return text.startswith("\\\\", index) or text.startswith("//", index)


def _is_root_path_start(text, index):
    if index >= len(text):
        return False
    return (_starts_with_drive(text, index)
            or _starts_with_unc(text, index)
            or text[index] == "/")


def _media_extension_end(text, start=0):
    """Return the end of a media extension followed by punctuation or EOF."""
    for index in range(start, len(text)):
        if text[index] != ".":
            continue
        for extension in _MEDIA_EXTENSIONS:
            end = index + len(extension)
            if end > len(text):
                continue
            if text[index:end].lower() != extension:
                continue
            if end == len(text) or text[end] in _EDGE_PUNCTUATION:
                return end
    return -1


def _find_path_start(text):
    """Find a path start without scanning backwards or rebuilding the text."""
    length = len(text)
    for index in range(length):
        if index and text[index - 1] not in _PATH_PREFIX_BOUNDARIES:
            continue
        if _is_root_path_start(text, index):
            return index

    separator = length
    for char in ("\\", "/"):
        position = text.find(char)
        if position != -1:
            separator = min(separator, position)
    if separator == length or _media_extension_end(text) < 0:
        return -1

    start = 0
    for index in range(1, separator + 1):
        if text[index - 1] in _PATH_PREFIX_BOUNDARIES:
            start = index
    return start


def _split_edge_punctuation(text):
    left = 0
    while left < len(text) and text[left] in _EDGE_PUNCTUATION:
        left += 1
    right = len(text)
    while right > left and text[right - 1] in _EDGE_PUNCTUATION:
        right -= 1
    return text[:left], text[left:right], text[right:]


def _basename_only(path):
    path = path.rstrip("\\/")
    cut = max(path.rfind("\\"), path.rfind("/"))
    return path[cut + 1:] if cut >= 0 else path


def _redact_candidate(candidate):
    if "://" in candidate:
        return candidate
    if ("?" in candidate
            and not (candidate.startswith("\\\\?\\")
                     and candidate.find("?", 3) == -1)):
        return candidate
    leading, body, trailing = _split_edge_punctuation(candidate)
    if not body:
        return candidate
    start = _find_path_start(body)
    if start < 0:
        return candidate

    always_redact = (_starts_with_drive(body, start)
                     or _starts_with_unc(body, start))
    extension_end = _media_extension_end(body, start)
    if not always_redact and extension_end < 0:
        return candidate

    path_end = extension_end if extension_end >= 0 else len(body)
    basename = _basename_only(body[start:path_end])
    if not basename:
        basename = "<redacted-path>"
    return leading + body[:start] + basename + body[path_end:] + trailing


def _is_path_continuation(token):
    if "://" in token or "=" in token or ";" in token:
        return False
    return (("\\" in token or "/" in token)
            or _media_extension_end(token) != -1)


def _spanned_token_end(text, token_end):
    """Extend one unquoted token over whitespace inside a path."""
    scan = token_end
    end = token_end
    while scan < len(text):
        while scan < len(text) and text[scan].isspace():
            scan += 1
        if scan >= len(text) or text[scan] in ("'", '"'):
            break
        part_start = scan
        while (scan < len(text) and not text[scan].isspace()
               and text[scan] not in ("'", '"')):
            scan += 1
        part = text[part_start:scan]
        if not _is_path_continuation(part):
            break
        part_start_index = _find_path_start(part)
        if (part_start_index >= 0
                and _is_root_path_start(part, part_start_index)):
            break
        end = scan
        if _media_extension_end(part, max(part_start_index, 0)) != -1:
            break
    return end


def _redact_log_text(value):
    """Replace filesystem absolute paths embedded in an error/log string."""
    if value is None:
        return None
    text = str(value)

    separators = frozenset(";；,，、。：！？…")
    api_prefixes = ("/api/", "/v1/", "/v2/", "/creator/", "/aweme/",
                    "/passport/")
    media_extensions = _MEDIA_EXTENSIONS
    edge_punctuation = _EDGE_PUNCTUATION
    prefix_boundaries = _PATH_PREFIX_BOUNDARIES
    url_prefixes = ("http://", "https://")

    def _starts_with_drive_at(text_value, position):
        return (
            position + 2 < len(text_value)
            and text_value[position].isalpha()
            and text_value[position + 1] == ":"
            and text_value[position + 2] in "\\/"
        )

    def _is_root_at(text_value, position):
        if position >= len(text_value):
            return False
        return (_starts_with_drive_at(text_value, position)
                or text_value[position] in "/\\")

    def _extension_end(text_value, start=0):
        for position in range(start, len(text_value)):
            if text_value[position] != ".":
                continue
            for extension in media_extensions:
                extension_end = position + len(extension)
                if extension_end > len(text_value):
                    continue
                if text_value[position:extension_end].lower() != extension:
                    continue
                if (extension_end == len(text_value)
                        or text_value[extension_end] in edge_punctuation):
                    return extension_end
        return -1

    def _find_start(text_value):
        length_value = len(text_value)
        for position in range(length_value):
            if (position
                    and text_value[position - 1] not in prefix_boundaries):
                continue
            if _is_root_at(text_value, position):
                return position

        separator = length_value
        for character in ("\\", "/"):
            found = text_value.find(character)
            if found != -1:
                separator = min(separator, found)
        if separator == length_value or _extension_end(text_value) < 0:
            return -1

        start = 0
        for position in range(1, separator + 1):
            if text_value[position - 1] in prefix_boundaries:
                start = position
        return start

    def _split_edges(piece):
        left = 0
        while left < len(piece) and piece[left] in edge_punctuation:
            left += 1
        right = len(piece)
        while right > left and piece[right - 1] in edge_punctuation:
            right -= 1
        return piece[:left], piece[left:right], piece[right:]

    def _is_plain_value(piece):
        if any(character.isalpha() for character in piece):
            return False
        if not any(character in "/\\" for character in piece):
            return True
        return all(character in "/\\" for character in piece)

    def _redact_piece(piece):
        if piece.startswith(url_prefixes):
            return piece

        leading, body, trailing = _split_edges(piece)
        if not body or _is_plain_value(body):
            return piece

        start = _find_start(body)
        if start < 0:
            return piece
        tail = body[start:]
        if tail.startswith(api_prefixes):
            return piece

        always_redact = _is_root_at(body, start)
        extension_end = _extension_end(body, start)
        if not always_redact and extension_end < 0:
            return piece

        path_end = extension_end if extension_end >= 0 else len(body)
        query_start = body.find("?", start)
        if (query_start >= 0
                and not (body.startswith("\\\\?\\") and query_start == 2)):
            path_end = min(path_end, query_start)
        basename = body[start:path_end].rstrip("\\/")
        cut = max(basename.rfind("\\"), basename.rfind("/"))
        if cut >= 0:
            basename = basename[cut + 1:]
        if not basename:
            basename = "<redacted-path>"
        return leading + body[:start] + basename + body[path_end:] + trailing

    def _redact_token(token):
        pieces = []
        start = 0
        for position, character in enumerate(token):
            if character not in separators:
                continue
            pieces.append(_redact_piece(token[start:position]))
            pieces.append(character)
            start = position + 1
        pieces.append(_redact_piece(token[start:]))
        return "".join(pieces)

    rendered = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in ("'", '"'):
            end = text.find(char, index + 1)
            if end != -1:
                inner = text[index + 1:end]
                if not inner.startswith(url_prefixes):
                    inner = _redact_token(inner)
                rendered.append(char + inner + char)
                index = end + 1
                continue
            rendered.append(char)
            index += 1
            continue
        if char.isspace():
            end = index + 1
            while end < length and text[end].isspace():
                end += 1
            rendered.append(text[index:end])
            index = end
            continue

        end = index + 1
        while (end < length and not text[end].isspace()
               and text[end] not in ("'", '"')):
            end += 1
        token = text[index:end]
        if not token.startswith(url_prefixes):
            start = _find_start(token)
            if start >= 0 and _is_root_at(token, start):
                tail = token[start:]
                if not tail.startswith(api_prefixes):
                    end = _spanned_token_end(text, end)
                    token = text[index:end]
            rendered.append(_redact_token(token))
        else:
            rendered.append(token)
        index = end
    return "".join(rendered)


def _log_field(key, value):
    if value is None:
        return None
    if key in _LOG_PATH_KEYS and isinstance(value, (str, Path)):
        return _log_basename(value)
    if isinstance(value, str):
        return _redact_log_text(value)
    if isinstance(value, dict):
        return {item_key: _log_field(item_key, item_value)
                for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_log_field(None, item) for item in value]
    return value


def _page_frames(page):
    """Return the page's frames, treating a non-Playwright fake as one frame."""
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    return frames or [page]


def _frame_query_state(frame, deadline):
    """Return ``(allowed, error)`` before touching a frame's locators."""
    is_detached = getattr(frame, "is_detached", None)
    if callable(is_detached):
        try:
            if is_detached():
                return False, "frame detached"
        except Exception as exc:
            return False, type(exc).__name__ + ": " + str(exc)
    if time.monotonic() >= deadline:
        return False, "frame query timeout"
    return True, None


def _bounded_locator_call(locator, method_name, *args, deadline):
    """Run one Playwright locator read with a cancellable deadline.

    Playwright's sync ``count()`` / ``get_attribute()`` have no timeout
    argument.  On a real Playwright locator the coroutine runs on the driver's
    own event loop and is cancelled with ``asyncio.wait_for``.  Fake/test
    locators run in a daemon worker so a renderer that never answers still
    cannot block past ``deadline``.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None, "frame query timeout"
    impl = getattr(locator, "_impl_obj", None)
    loop = getattr(locator, "_loop", None)
    dispatcher = getattr(locator, "_dispatcher_fiber", None)
    impl_method = getattr(impl, method_name, None) if impl is not None else None
    if callable(impl_method) and loop is not None and dispatcher is not None:
        try:
            value = _bounded_sync_coroutine(
                locator, impl_method(*args), remaining)
            if method_name == "count":
                value = int(value)
            return value, None
        except (asyncio.TimeoutError, TimeoutError):
            return None, "frame query timeout"
        except Exception as exc:
            return None, type(exc).__name__ + ": " + str(exc)

    result = {}
    done = threading.Event()

    def worker():
        try:
            value = getattr(locator, method_name)(*args)
            if method_name == "count":
                value = int(value)
            result["value"] = value
        except BaseException as exc:  # noqa: BLE001 - surfaced to caller
            result["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=worker, name="locator-query",
                              daemon=True)
    thread.start()
    if not done.wait(remaining):
        return None, "frame query timeout"
    if "error" in result:
        exc = result["error"]
        return None, type(exc).__name__ + ": " + str(exc)
    return result.get("value"), None


def _bounded_locator_count(locator, deadline):
    """Run ``locator.count()`` with a cancellable deadline."""
    return _bounded_locator_call(locator, "count", deadline=deadline)


def _upload_input_frame_snapshot(page, selectors, *, hard_deadline=None):
    """Count primary upload inputs in the main frame and every child frame.

    Runs on the Playwright-owning thread only.  Each frame has an independent
    deadline; a detached, throwing or slow iframe is skipped and the rest of
    the frames still contribute to the snapshot.  ``hard_deadline`` bounds the
    whole multi-frame scan as well as each individual query.
    """
    union = ", ".join(selectors)
    frames = _page_frames(page)
    hits = []
    total = 0
    query_errors = []
    for index, frame in enumerate(frames):
        now = time.monotonic()
        if hard_deadline is not None and now >= hard_deadline:
            query_errors.append({
                "frame_index": index,
                "error": "frame query timeout",
            })
            break
        deadline = now + UPLOAD_FRAME_QUERY_TIMEOUT_SECONDS
        if hard_deadline is not None:
            deadline = min(deadline, hard_deadline)
        allowed, state_error = _frame_query_state(frame, deadline)
        if not allowed:
            query_errors.append({
                "frame_index": index,
                "error": state_error,
            })
            continue
        try:
            union_locator = frame.locator(union)
        except Exception as exc:
            query_errors.append({
                "frame_index": index,
                "error": type(exc).__name__ + ": " + str(exc),
            })
            continue
        count, count_error = _bounded_locator_count(union_locator, deadline)
        if count_error is not None:
            query_errors.append({
                "frame_index": index,
                "error": count_error,
            })
            continue
        if count <= 0:
            continue
        per_selector = {}
        for selector in selectors:
            allowed, state_error = _frame_query_state(frame, deadline)
            if not allowed:
                per_selector[selector] = -1
                query_errors.append({
                    "frame_index": index,
                    "selector": selector,
                    "error": state_error,
                })
                continue
            try:
                selector_locator = frame.locator(selector)
            except Exception as exc:
                per_selector[selector] = -1
                query_errors.append({
                    "frame_index": index,
                    "selector": selector,
                    "error": type(exc).__name__ + ": " + str(exc),
                })
                continue
            value, value_error = _bounded_locator_count(
                selector_locator, deadline)
            if value_error is not None:
                per_selector[selector] = -1
                query_errors.append({
                    "frame_index": index,
                    "selector": selector,
                    "error": value_error,
                })
                continue
            per_selector[selector] = value
        hits.append({
            "frame_index": index,
            "frame_url": _frame_url_value(frame),
            "count": count,
            "selectors": per_selector,
        })
        total += count
    return {
        "selector": union,
        "count": total,
        "frames": len(frames),
        "iframes": max(0, len(frames) - 1),
        "frame_hits": hits,
        "query_errors": query_errors,
    }


def _fallback_upload_input_snapshot(page, *, hard_deadline=None):
    """Count generic file inputs whose accept attribute names video."""
    frames = _page_frames(page)
    hits = []
    file_inputs = 0
    query_errors = []
    for index, frame in enumerate(frames):
        now = time.monotonic()
        if hard_deadline is not None and now >= hard_deadline:
            query_errors.append({
                "frame_index": index,
                "error": "frame query timeout",
            })
            break
        deadline = now + UPLOAD_FRAME_QUERY_TIMEOUT_SECONDS
        if hard_deadline is not None:
            deadline = min(deadline, hard_deadline)
        allowed, state_error = _frame_query_state(frame, deadline)
        if not allowed:
            query_errors.append({
                "frame_index": index,
                "error": state_error,
            })
            continue
        try:
            locator = frame.locator("input[type=file]")
        except Exception as exc:
            query_errors.append({
                "frame_index": index,
                "error": type(exc).__name__ + ": " + str(exc),
            })
            continue
        count, count_error = _bounded_locator_count(locator, deadline)
        if count_error is not None:
            query_errors.append({
                "frame_index": index,
                "error": count_error,
            })
            continue
        file_inputs += count
        for position in range(count):
            allowed, state_error = _frame_query_state(frame, deadline)
            if not allowed:
                query_errors.append({
                    "frame_index": index,
                    "position": position,
                    "error": state_error,
                })
                continue
            try:
                item_locator = locator.nth(position)
            except Exception as exc:
                query_errors.append({
                    "frame_index": index,
                    "position": position,
                    "error": type(exc).__name__ + ": " + str(exc),
                })
                continue
            accept, accept_error = _bounded_locator_call(
                item_locator, "get_attribute", "accept",
                deadline=deadline)
            if accept_error is not None:
                query_errors.append({
                    "frame_index": index,
                    "position": position,
                    "error": accept_error,
                })
                continue
            accept = accept or ""
            if "video" in str(accept).lower():
                hits.append({
                    "frame_index": index,
                    "frame_url": _frame_url_value(frame),
                    "position": position,
                    "accept": str(accept),
                })
    return {
        "selector": "input[type=file][accept contains video]",
        "count": len(hits),
        "file_inputs": file_inputs,
        "frames": len(frames),
        "iframes": max(0, len(frames) - 1),
        "frame_hits": hits,
        "query_errors": query_errors,
    }


def _snapshot_timeout_result(state, selectors):
    return {
        "found": False,
        "ambiguous": False,
        "timeout": True,
        "selector": state.get("selector") or ", ".join(selectors),
        "count": state.get("count", 0),
        "frames": state.get("frames", 0),
        "iframes": state.get("iframes", 0),
        "frame_hits": state.get("frame_hits", []),
        "query_errors": state.get("query_errors", []),
    }


def _page_body_text(page):
    try:
        return str(page.locator("body").evaluate(
            "el => el ? (el.innerText || el.textContent || '') : ''",
            timeout=1000) or "")
    except Exception:
        # Never fall back to Page.inner_text("body") without a timeout: on a
        # heavy creator page (especially with the cover dialog open) that call
        # can block the main publish loop for the full default timeout.
        return ""


def _bounded_sync_coroutine(owner, coro, timeout):
    """Await a Playwright sync coroutine on its own greenlet with a deadline.

    Playwright's sync API has no timeout argument on ``Response.body()``.  A
    worker thread cannot be used because sync objects are greenlet-affine.
    This helper schedules the exact same coroutine on the driver's own event
    loop and cancels it with ``asyncio.wait_for`` when the deadline expires.
    """
    loop = getattr(owner, "_loop", None)
    dispatcher = getattr(owner, "_dispatcher_fiber", None)
    if loop is None or dispatcher is None or loop.is_closed():
        try:
            coro.close()
        except Exception:
            pass
        raise RuntimeError("Playwright sync dispatcher unavailable")
    try:
        import greenlet
    except Exception as exc:  # pragma: no cover - Playwright dependency
        try:
            coro.close()
        except Exception:
            pass
        raise RuntimeError("greenlet unavailable: %s" % exc) from exc

    greenlet_here = greenlet.getcurrent()
    budget = max(0.001, float(timeout or 0.001))

    async def runner():
        return await asyncio.wait_for(coro, budget)

    task = loop.create_task(runner())
    task.add_done_callback(lambda _task: greenlet_here.switch())
    while not task.done():
        dispatcher.switch()
    try:
        asyncio._set_running_loop(loop)
    except Exception:
        pass
    return task.result()


def _extract_upload_resource_ids(body):
    ids = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if (key in ("resource_id", "upload_id", "video_id", "vid")
                        and isinstance(item, (str, int))):
                    ids.add(str(item))
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(body)
    return sorted(ids)


def _video_preview_baseline(page):
    """Record the visible pre-existing videos (count + sources) without touching the DOM."""
    try:
        value = page.evaluate("""/* winport-video-baseline */ () => {
          const visible = el => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' ||
                Number(style.opacity || 1) === 0) return false;
            return !!(el.offsetWidth || el.offsetHeight ||
                      el.getClientRects().length);
          };
          const token = 'winport-' + Date.now() + '-' +
              Math.random().toString(36).slice(2);
          const sources = [];
          let count = 0;
          for (const video of Array.from(document.querySelectorAll('video'))) {
            if (!visible(video)) continue;
            count += 1;
            const source = String(video.currentSrc || video.src ||
                                  video.getAttribute('src') || '').trim();
            if (source) sources.push(source);
          }
          return {token: token, count: count,
                  sources: Array.from(new Set(sources))};
        }""")
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    token = str(value.get("token") or "").strip()
    try:
        count = int(value.get("count"))
    except (TypeError, ValueError):
        return None
    if not token or count < 0:
        return None
    sources = []
    for source in value.get("sources") or []:
        text = str(source or "").strip()
        if text and text not in sources:
            sources.append(text)
    return {"token": token, "count": count, "sources": sources}


def _completed_upload_request_ids(attempt, *, limit=None):
    """Return completed upload request ids in observation order, deduplicated."""
    seen = set()
    request_ids = []
    for raw in getattr(attempt, "request_ids", None) or ():
        request_id = str(raw or "")
        if not request_id or request_id in seen:
            continue
        seen.add(request_id)
        request_ids.append(request_id)
        if limit is not None and len(request_ids) >= int(limit):
            break
    return request_ids


def _upload_request_url_lines(attempt, *, limit=4):
    """Describe completed upload requests as ``method url status=...`` lines."""
    responses = {}
    for record in getattr(attempt, "response_records", None) or ():
        request_id = str(record.get("request_id") or "")
        if request_id:
            responses.setdefault(request_id, []).append(record)
    requests = {}
    for record in getattr(attempt, "request_records", None) or ():
        request_id = str(record.get("request_id") or "")
        if request_id and request_id not in requests:
            requests[request_id] = record

    lines = []
    for request_id in _completed_upload_request_ids(attempt, limit=limit):
        request_responses = responses.get(request_id, [])
        request = requests.get(request_id, {})
        response = request_responses[0] if request_responses else {}
        method = str(response.get("method") or request.get("method") or "?")
        url = str(response.get("url") or request.get("url") or
                  request.get("host") or "?")
        statuses = []
        for item in request_responses:
            try:
                statuses.append(str(int(item.get("status"))))
            except (TypeError, ValueError):
                statuses.append("?")
        status = "/".join(statuses) if statuses else "no_response"
        lines.append("%s %s status=%s" % (method, url, status))
    return lines


def _upload_binding_byte_size(attempt, request_id, response):
    """Best available request/response byte count for one upload request."""
    sizes = []
    for record in getattr(attempt, "request_records", None) or ():
        if str(record.get("request_id") or "") != request_id:
            continue
        for key in ("bytes", "request_body_bytes", "requestBodySize"):
            try:
                value = int(record.get(key))
            except (TypeError, ValueError):
                continue
            if value >= 0:
                sizes.append(value)
                break
        break
    for key in ("body_bytes", "response_bytes", "content_length", "bytes", "size"):
        try:
            value = int((response or {}).get(key))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            sizes.append(value)
            break
    if not sizes:
        return None
    try:
        local_size = int(attempt.size)
    except (TypeError, ValueError):
        return None
    return min(sizes, key=lambda value: abs(value - local_size))


def _select_upload_binding_request(attempt):
    """Pick the one completed request whose sole response is a 2xx upload.

    A page can issue multiple completed upload requests (for example a
    presign/chunk request plus a completion request).  Exactly one usable
    request binds directly.  With several usable requests, the one whose
    request/response byte count is uniquely closest to the local file wins;
    ties and missing sizes fail closed.
    """
    responses = {}
    for record in getattr(attempt, "response_records", None) or ():
        request_id = str(record.get("request_id") or "")
        if request_id:
            responses.setdefault(request_id, []).append(record)

    candidates = []
    for request_id in _completed_upload_request_ids(attempt):
        request_responses = responses.get(request_id, [])
        if len(request_responses) != 1:
            continue
        try:
            status = int(request_responses[0].get("status"))
        except (TypeError, ValueError):
            continue
        if 200 <= status < 300:
            candidates.append((request_id, request_responses[0]))

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) < 2:
        return None

    scored = []
    for request_id, response in candidates:
        byte_size = _upload_binding_byte_size(attempt, request_id, response)
        if byte_size is None:
            return None
        try:
            difference = abs(byte_size - int(attempt.size))
        except (TypeError, ValueError):
            return None
        scored.append((difference, request_id, response))
    scored.sort(key=lambda item: (item[0], item[1]))
    if scored[0][0] == scored[1][0]:
        return None
    return scored[0][1], scored[0][2]


def _fresh_video_preview_state(page, baseline):
    """Count visible videos added after the baseline on the post page.

    The preview container is identified structurally, not by class name:
    the page must be on ``/content/post/video``, visible videos must total
    ``baseline + 1``, and the added video must be visible, at least 80x80,
    and have a non-empty source that was not present in the baseline.
    ``VIDEO_PREVIEW_CONTAINER_SELECTORS`` remains a diagnostic bonus; a
    selector miss never forces the fresh count to zero.
    """
    if not isinstance(baseline, dict) or not baseline.get("token"):
        return {"count": 0, "sources": [], "unavailable": True}
    try:
        value = page.evaluate(r"""/* winport-video-fresh */
          ({sources, baselineCount, selectors}) => {
          const visible = el => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' ||
                Number(style.opacity || 1) === 0) return false;
            return el.offsetWidth > 0 && el.offsetHeight > 0;
          };
          const sourceOf = video => String(video.currentSrc || video.src ||
              video.getAttribute('src') || '').trim();
          const path = String((window.location &&
              window.location.pathname) || '').replace(/\/+$/, '');
          const onPostPage = path === '/creator-micro/content/post/video' ||
              path.endsWith('/content/post/video');
          const baselineSources = new Set(sources || []);
          const inPreviewBox = video => {
            for (let node = video.parentElement;
                 node && node !== document.body &&
                 node !== document.documentElement;
                 node = node.parentElement) {
              if ((selectors || []).some(selector => {
                    try { return node.matches(selector); }
                    catch (err) { return false; }
                  })) return true;
            }
            return false;
          };
          const visibleVideos = Array.from(
              document.querySelectorAll('video')).filter(visible);
          const baselineVisible = Number(baselineCount);
          const expected = (Number.isFinite(baselineVisible) ?
              baselineVisible : 0) + 1;
          const fresh = [];
          if (onPostPage && visibleVideos.length === expected) {
            for (const video of visibleVideos) {
              if (video.offsetWidth < 80 || video.offsetHeight < 80) continue;
              const source = sourceOf(video);
              if (!source || baselineSources.has(source)) continue;
              fresh.push(source);
            }
          }
          return {count: fresh.length,
                  sources: Array.from(new Set(fresh.filter(Boolean))),
                  on_post_page: onPostPage,
                  visible_videos: visibleVideos.length,
                  baseline_count: Number.isFinite(baselineVisible) ?
                      baselineVisible : 0,
                  selector_bonus: fresh.length === 1 &&
                      visibleVideos.some(inPreviewBox)};
        }""", {
            "sources": list(baseline.get("sources") or []),
            "baselineCount": int(baseline.get("count") or 0),
            "selectors": list(VIDEO_PREVIEW_CONTAINER_SELECTORS),
        })
    except Exception:
        return {"count": 0, "sources": [], "unavailable": True}
    if not isinstance(value, dict):
        return {"count": 0, "sources": [], "unavailable": True}
    try:
        count = int(value.get("count"))
    except (TypeError, ValueError):
        return {"count": 0, "sources": [], "unavailable": True}
    if count < 0:
        return {"count": 0, "sources": [], "unavailable": True}
    sources = [str(source) for source in value.get("sources") or []
               if str(source or "").strip()]
    return {"count": count, "sources": sources,
            "selector_bonus": bool(value.get("selector_bonus")),
            "on_post_page": bool(value.get("on_post_page"))}


def _visible_text_locator(page, text):
    """Return the first visible exact-text locator, or None."""
    try:
        matches = page.get_by_text(text, exact=True)
        count = matches.count()
    except Exception:
        return None
    for index in range(count):
        item = matches.nth(index)
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def _resilient_click(page, locator):
    """Force-click first; fall back to a DOM click when a mask intercepts."""
    try:
        locator.click(force=True, timeout=2000)
        return {"ok": True, "mode": "force"}
    except Exception as first_error:
        try:
            locator.evaluate("el => el.click()", timeout=2000)
            return {"ok": True, "mode": "dispatch"}
        except Exception as second_error:
            return {"ok": False,
                    "reason": "%s; %s" % (first_error, second_error)}


def _click_texts(page, texts, *, timeout=2.0):
    """Click the first visible exact label from ``texts`` using a bounded wait."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        for text in texts:
            locator = _visible_text_locator(page, text)
            if locator is None:
                continue
            result = _resilient_click(page, locator)
            if result.get("ok"):
                return result
        if time.monotonic() >= deadline:
            return {"ok": False, "reason": "text not found: " + ", ".join(texts)}
        _pause_page(page)


def _cover_frame_usable(frame):
    """Return False for a detached frame without throwing during a query."""
    is_detached = getattr(frame, "is_detached", None)
    if callable(is_detached):
        try:
            return not is_detached()
        except Exception:
            return False
    return True


_COVER_TEXT_MATCH_SCRIPT = r"""(root, label) => {
  const doc = (root && root.ownerDocument) || document;
  const clean = value => String(value || '')
      .replace(/\s+/g, '').replace(/\u200b/g, '');
  const wanted = clean(label);
  const visible = el => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden'
        && style.opacity !== '0'
        && !!(el.offsetWidth || el.offsetHeight ||
              el.getClientRects().length);
  };
  const exactLeaf = el => {
    if (!el || !el.tagName) return false;
    const tag = String(el.tagName || '').toUpperCase();
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') {
      return false;
    }
    if (!wanted || clean(el.innerText) !== wanted) return false;
    for (const child of el.children) {
      if (clean(child.innerText) === wanted) return false;
    }
    return true;
  };
  const scope = (root && root.querySelectorAll) ? root : doc;
  const matches = Array.from(scope.querySelectorAll('*')).filter(exactLeaf);
  const shown = matches.filter(visible);
  return {total: matches.length, visible: shown.length, items: shown};
}"""


def _cover_text_matches(root, text):
    """Return exact leaf matches for ``text`` inside one DOM root.

    1.2.110: every cover label is resolved with ``querySelectorAll('*')`` and
    an exact ``innerText.trim()`` leaf comparison.  The old panel-scoped
    finder could report zero while the label was still in the document.
    """
    label = str(text or "")
    result = {"total": 0, "visible": 0, "items": [], "error": ""}
    try:
        # Frame.evaluate_handle does not inject a DOM root, while
        # ElementHandle.evaluate_handle does.  Normalize frames through body
        # so the script always receives ``(element, label)``.
        target = root.locator("body") if hasattr(root, "locator") else root
        handle = target.evaluate_handle(_COVER_TEXT_MATCH_SCRIPT, label)
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
        return result
    try:
        total = int(handle.evaluate("value => value.total || 0") or 0)
        visible = int(handle.evaluate("value => value.visible || 0") or 0)
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
        return result
    result["total"] = max(0, total)
    result["visible"] = max(0, visible)
    items = []
    for index in range(min(result["visible"], 50)):
        try:
            item = handle.evaluate_handle(
                "(value, position) => value.items[position]", index)
            element = item.as_element()
        except Exception:
            element = None
        if element is not None:
            items.append(element)
    result["items"] = items
    return result


def _cover_visible_text_items(frame, text):
    """Every visible exact leaf match for ``text`` in one page/frame."""
    return list(_cover_text_matches(frame, text).get("items") or [])


def _cover_control_scan(page, texts, *, attempts=COVER_CONTROL_QUERY_ATTEMPTS,
                        retry_ms=COVER_CONTROL_QUERY_RETRY_MS,
                        timeout=COVER_CONTROL_QUERY_TIMEOUT_SECONDS):
    """Query control labels on the whole page, then every child frame.

    No container handle is used here: the live panel moved between DOM roots
    across Douyin releases and a stale handle is exactly what made every
    candidate count zero in 1.2.93.  Each attempt starts with a 300 ms render
    wait; the total scan is bounded to five seconds.
    """
    candidates = list(dict.fromkeys(str(text) for text in texts if str(text)))
    counts = {text: 0 for text in candidates}
    deadline = time.monotonic() + max(0.0, float(timeout))
    frames_checked = 0
    attempts = max(1, int(attempts))
    hits = []
    used_attempts = 0
    for used_attempts in range(1, attempts + 1):
        _pause_page(page, max(0, int(retry_ms)))
        frames = [frame for frame in _page_frames(page)
                  if _cover_frame_usable(frame)]
        frames_checked = max(frames_checked, len(frames))
        counts = {text: 0 for text in candidates}
        hits = []
        for text in candidates:
            for frame_index, frame in enumerate(frames):
                for item in _cover_visible_text_items(frame, text):
                    counts[text] += 1
                    hits.append((text, frame_index, item))
        if hits or time.monotonic() >= deadline:
            break
    return {"counts": counts, "hits": hits, "frames": frames_checked,
            "attempts": used_attempts}


def _cover_control_click(page, texts, *, attempts=COVER_CONTROL_QUERY_ATTEMPTS,
                         retry_ms=COVER_CONTROL_QUERY_RETRY_MS,
                         timeout=COVER_CONTROL_QUERY_TIMEOUT_SECONDS):
    """Click the first visible control found on the page or in a child frame."""
    scan = _cover_control_scan(page, texts, attempts=attempts,
                               retry_ms=retry_ms, timeout=timeout)
    errors = []
    click = {"via": "js", "hit": False, "rect": {}}
    for text, frame_index, item in scan["hits"]:
        click = _cover_click_element(_cover_clickable_element(item))
        if click.get("ok"):
            return {"ok": True, "clicked": text, "frame": frame_index,
                    "mode": click.get("mode"), "via": click.get("via"),
                    "hit": bool(click.get("hit")),
                    "rect": click.get("rect") or {},
                    "counts": scan["counts"],
                    "frames": scan["frames"], "attempts": scan["attempts"]}
        errors.append(click.get("reason") or "cover control click failed")
        if click.get("via") == "mouse":
            break
    return {"ok": False, "clicked": "", "counts": scan["counts"],
            "frames": scan["frames"], "attempts": scan["attempts"],
            "via": click.get("via"), "hit": False,
            "rect": click.get("rect") or {},
            "reason": "control text not found: " + ", ".join(texts),
            "errors": errors}


def _cover_element_diagnostics(item):
    """Return stable tag/class/rect/disabled evidence for one control."""
    try:
        info = item.evaluate("""el => {
          const rect = el.getBoundingClientRect();
          const klass = typeof el.className === 'string'
              ? el.className
              : String(el.getAttribute('class') || '');
          const disabled = !!el.disabled ||
              String(el.getAttribute('aria-disabled') || '').toLowerCase()
                  === 'true' ||
              /(^|\\s)(disabled|semi-button-disabled)(\\s|$)/.test(klass);
          return {tag: String(el.tagName || '').toLowerCase(),
                  klass: klass,
                  rect: {x: Math.round(rect.x || 0),
                         y: Math.round(rect.y || 0),
                         width: Math.round(rect.width || 0),
                         height: Math.round(rect.height || 0)},
                  disabled: disabled};
        }""")
    except Exception as exc:
        return {"tag": "", "klass": "", "rect": {},
                "disabled": True,
                "error": type(exc).__name__ + ": " + str(exc)}
    if not isinstance(info, dict):
        return {"tag": "", "klass": "", "rect": {}, "disabled": True}
    return info


def _cover_click_element(item, *, timeout_ms=2000):
    """Element-level click; coordinate mouse click is the final fallback.

    1.2.111: the live page renders a popover above the cover buttons.  A
    Playwright ``click`` still targets the button rectangle and is swallowed
    by that overlay even with ``force=True``.  DOM dispatch/evaluate works
    because it bypasses hit-testing.  Both JS forms run before any mouse
    click, and the coordinate fallback is attempted exactly once.
    """
    errors = []
    try:
        item.scroll_into_view_if_needed(timeout=max(500, int(timeout_ms)))
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    info = _cover_element_diagnostics(item)
    rect = info.get("rect") or {}
    if info.get("disabled"):
        return {"ok": False, "mode": "none", "via": "js", "hit": False,
                "rect": rect, "reason": info.get("error") or "disabled"}
    try:
        try:
            item.dispatch_event("click", timeout=2000)
        except TypeError:
            # ElementHandle.dispatch_event has no timeout parameter.
            item.dispatch_event("click")
        return {"ok": True, "mode": "dispatch_event", "via": "js",
                "hit": True, "rect": rect,
                "error": "; ".join(errors) or None}
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    try:
        item.evaluate("el => el.click()")
        return {"ok": True, "mode": "evaluate_click", "via": "js",
                "hit": True, "rect": rect,
                "error": "; ".join(errors) or None}
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    try:
        item.click(force=True, timeout=max(1, int(timeout_ms)))
        return {"ok": True, "mode": "force", "via": "mouse",
                "hit": True, "rect": rect,
                "error": "; ".join(errors) or None}
    except Exception as exc:
        errors.append(type(exc).__name__ + ": " + str(exc))
    return {"ok": False, "mode": "none", "via": "mouse", "hit": False,
            "rect": rect, "reason": "; ".join(errors) or "click failed"}


def _cover_click_done_element(item):
    """Click the resolved done control through the shared element click."""
    return _cover_click_element(item,
                                timeout_ms=COVER_DONE_CLICK_TIMEOUT_MS)


def _cover_clickable_element(item):
    """Resolve the clickable container around an exact text label.

    The live detection control is ``DIV.btn-*``.  Some releases wrap the copy
    in a ``SPAN``, so prefer a ``div[class*=btn]`` ancestor and otherwise use
    the span's parent.  The label itself remains the fallback.
    """
    try:
        # selector-ok: scoped-unique
        handle = item.evaluate_handle("""el => {
          const visible = node => {
            if (!node) return false;
            const style = window.getComputedStyle(node);
            return style.display !== 'none' && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && !!(node.offsetWidth || node.offsetHeight ||
                      node.getClientRects().length);
          };
          const button = el.closest
              ? el.closest('div[class*="btn"]') : null;
          if (button && visible(button)) return button;
          const tag = String(el.tagName || '').toUpperCase();
          const parent = el.parentElement;
          const broadRoot = parent && parent.matches &&
              parent.matches('[role="dialog"],[class*="modal"]');
          if (tag === 'SPAN' && parent && !broadRoot &&
              visible(parent)) {
            return el.parentElement;
          }
          return el;
        }""")
        element = handle.as_element()
    except Exception:
        element = None
    return element or item


def _cover_scope_text_item(scope, text):
    """Return the first visible exact leaf inside a diagnostic scope only."""
    items = _cover_text_matches(scope, text).get("items") or []
    return items[0] if items else None


def _cover_entry_click(page, text, *, timeout_ms=COVER_ENTRY_CLICK_TIMEOUT_MS):
    """Click the first exact entry label, main frame first then child frames."""
    for frame_index, frame in enumerate(_page_frames(page)):
        if not _cover_frame_usable(frame):
            continue
        items = _cover_visible_text_items(frame, text)
        if not items:
            continue
        click = _cover_click_element(
            _cover_clickable_element(items[0]), timeout_ms=timeout_ms)
        return {"ok": bool(click.get("ok")), "found": True,
                "clicked": text if click.get("ok") else "",
                "frame": frame_index, "mode": click.get("mode"),
                "via": click.get("via"), "hit": bool(click.get("hit")),
                "rect": click.get("rect") or {},
                "reason": click.get("reason")}
    return {"ok": False, "found": False, "clicked": "",
            "via": "js", "hit": False, "rect": {},
            "reason": "entry text not found: " + text}


def _wait_for(predicate, page, *, timeout=20.0):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            value = predicate()
        except Exception:
            value = None
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        _pause_page(page)


def _xingtu_modal_open(page):
    try:
        search = page.locator(XINGTU_SEARCH_SELECTOR)
        if search.count() and search.first.is_visible():
            return True
    except Exception:
        pass
    try:
        portals = page.locator(XINGTU_PORTAL_SELECTOR)
        if portals.count() and portals.first.is_visible():
            return True
    except Exception:
        pass
    return False


def _xingtu_card_rows(page):
    """SELECTION contract: cards inside the one visible star-task portal.

    Each card also keeps ``data-xt-idx`` as a human/debug handle; selection
    itself is bound to the portal/card identity markers below.

    The picker contract is deliberately separate from the mount contract in
    ``_xingtu_mount_contract``.  Selection cards live in ``.semi-portal``; if
    that portal is absent or ambiguous this returns [] and the caller fails
    closed.  It never falls back to the whole document: the publish form's
    layout wrapper also carries ``card-container-*`` and would be mistaken for
    a pickable task.
    """
    try:
        return page.evaluate("""({portalSel, cardSel, cardClassRe, radioSel, titleSel}) => {
""" + xingtu_tasks.XINGTU_VISIBLE_CARD_NAME_JS + """
          const titleText = (el, card) => {
            if (!el || !__xingtuVisible(el)) return '';
            if (__xingtuInsideControl(el, card)) return '';
            return __xingtuNormalize(__xingtuCardNameChunks(el).join(''));
          };
          const cardTitles = card => {
            const values = [];
            const add = el => {
              const value = titleText(el, card);
              if (value && values.indexOf(value) < 0) values.push(value);
            };
            const explicit = Array.from(card.querySelectorAll(titleSel));
            if (explicit.length) {
              explicit.forEach(add);
            } else {
              Array.from(card.querySelectorAll(
                  'div,span,p,h1,h2,h3,h4,h5,h6,a')).forEach(add);
            }
            return values;
          };
          // Exactly one visible portal whose text names the star-task picker.
          // Zero or multiple portals is ambiguous -> return no rows and let
          // the caller fail closed instead of clicking anywhere else.
          const portals = Array.from(document.querySelectorAll(portalSel))
              .filter(el => __xingtuVisible(el) &&
                  /\u661f\u56fe\u4efb\u52a1/.test(
                      el.innerText || el.textContent || ''));
          // Nested wrapper portals collapse to the one outermost instance.
          const roots = portals.filter(el => !portals.some(other =>
              other !== el && other.contains(el)));
          if (roots.length !== 1) return [];
          const scope = roots[0];
          // DOM-object identity survives reads, but never transfers to a new
          // portal/card even when a framework copies old data attributes.
          const newIdentity = () => {
            if (window.crypto && typeof window.crypto.randomUUID === 'function') {
              try { return window.crypto.randomUUID(); } catch (err) {}
            }
            let hex = '';
            for (let i = 0; i < 32; i++) {
              hex += Math.floor(Math.random() * 16).toString(16);
            }
            return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' +
                hex.slice(12, 16) + '-' + hex.slice(16, 20) + '-' +
                hex.slice(20, 32);
          };
          const identity = el => {
            if (!window.__xtIdentities) window.__xtIdentities = new WeakMap();
            if (!window.__xtIdentities.has(el))
              window.__xtIdentities.set(el, newIdentity());
            return window.__xtIdentities.get(el);
          };
          const portalId = identity(scope);
          scope.setAttribute('data-xt-portal-id', portalId);
          const cards = __xingtuTaskCards(scope, cardSel, cardClassRe).filter(card => {
            const text = card.innerText || '';
            return text.length < 800;
          });
          return cards.map((card, index) => {
            // Deterministic handle for the click step: a document-wide nth()
            // picks the page layout wrapper instead of the picker card.
            const cardId = identity(card);
            card.setAttribute('data-xt-idx', String(index));
            card.setAttribute('data-xt-card-id', cardId);
            const radio = card.querySelector(radioSel);
            const visible = __xingtuVisible(card);
            const name = __xingtuCardName(card);
            const orderIds = __xingtuCardOrderIds(card);
            return {
              index: index,
              portal_id: portalId,
              card_id: cardId,
              name: name,
              titles: cardTitles(card),
              text: __xingtuVisibleText(card),
              order_id: orderIds.length === 1 ? orderIds[0] : '',
              order_ids: orderIds,
              checked: !!(radio && String(radio.className || '').indexOf(
                  'semi-radio-checked') >= 0) || !!card.querySelector(
                  'input[type=radio]:checked, [aria-checked="true"], [aria-selected="true"]'),
              visible: visible
            };
          });
        }""", {"portalSel": XINGTU_PICKER_PORTAL_SELECTOR,
                "cardSel": XINGTU_CARD_SELECTOR,
                "cardClassRe": XINGTU_CARD_CLASS_RE,
                "radioSel": XINGTU_RADIO_SELECTOR,
                "titleSel": XINGTU_TASK_TITLE_SELECTOR})
    except Exception:
        return []


def _xingtu_task_label(value):
    return xingtu_tasks.normalize_task_name(value)


def _xingtu_stage(driver, stage, **fields):
    """Emit one PUBLISH_STAGE line, through the driver when available."""
    payload = {key: value for key, value in fields.items() if value is not None}
    progress = getattr(driver, "_progress", None) if driver is not None else None
    if callable(progress):
        try:
            progress(stage, **payload)
            return
        except Exception:
            pass
    try:
        line = {"stage": str(stage)}
        line.update(payload)
        print("PUBLISH_STAGE " + json.dumps(line, ensure_ascii=False,
                                            default=str),
              file=sys.stderr, flush=True)
    except Exception:
        pass


def _xingtu_mount_contract(page, task):
    """MOUNT contract: evidence from the publish form, never the picker.

    The published form renders the attached task as one ``card-container-*``
    block outside any picker portal, whose text starts with ``任务:`` and whose
    wrapper carries the ``更换`` / ``删除`` controls.  Mount is proven when that
    block is unique, its name relates to the expected one, and the
    ``请选择星图任务`` placeholder is gone.  A card inside the picker is NOT
    mount evidence.
    """
    expected = _xingtu_task_label((task or {}).get("task_name"))
    result = {"found": False, "actual": "", "expected": expected, "ok": False,
              "readback_via": "", "page_url": _page_url_value(page),
              "task_name_nodes": [], "page_has_expected": False}
    if not expected:
        return result
    try:
        # selector-ok: scoped-unique
        value = page.evaluate(r"""({expectedName}) => {
""" + xingtu_tasks.XINGTU_VISIBLE_CARD_NAME_JS + r"""
          const clean = value => String(value || '')
              .replace(/\u200b/g, '').replace(/\s+/g, '');
          const outsidePicker = el => !el.closest(
              '.semi-portal,[role="dialog"],.semi-modal');
          const placeholder = Array.from(document.querySelectorAll('div,span,p'))
              .some(el => __xingtuVisible(el) && outsidePicker(el) &&
                          clean(el.innerText) === '请选择星图任务');
          const expected = __xingtuNormalize(expectedName || '');
          const related = actual => !!actual && !!expected &&
              (actual === expected || actual.indexOf(expected) >= 0 ||
               expected.indexOf(actual) >= 0);
          const blocks = [];
          for (const card of document.querySelectorAll('[class*="card-container"]')) {
            if (!__xingtuVisible(card) || !outsidePicker(card)) continue;
            if (/creator-layout/.test(String(card.className || ''))) continue;
            const text = clean(card.innerText);
            if ((text.match(/任务[:：]/g) || []).length !== 1) continue;
            const nameEl = card.querySelector('[class*="task-name-"]') || card;
            const actual = __xingtuNormalize(nameEl.innerText || '');
            if (!actual) continue;
            let block = null;
            for (let node = card, depth = 0; node && depth < 3;
                 node = node.parentElement, depth++) {
              const t = clean(node.innerText);
              if (/更换/.test(t) && /删除/.test(t)) { block = node; break; }
            }
            if (!block) continue;
            blocks.push({actual, node: card});
          }
          const names = blocks.map(item => item.actual);
          const finish = (actual, via, ok, list) => ({found: !!actual,
              actual, ok, readback_via: via, task_name_nodes: list || [],
              page_has_expected: !!expected && (list || []).indexOf(expected) >= 0});
          if (placeholder) return finish('', 'placeholder_visible', false, names);
          if (blocks.length !== 1) return finish('', 'form_block_ambiguous', false, names);
          const winner = blocks[0];
          return finish(winner.actual, 'form_card', related(winner.actual), names);
        }""", {"expectedName": expected})
        if isinstance(value, dict):
            result.update(value)
    except Exception:
        pass
    return result


def _xingtu_page_task_readback(page, task):
    """Read the mounted form through the shared mount contract."""
    return _xingtu_mount_contract(page, task)


def _xingtu_fill_match_rows(rows, task):
    return xingtu_tasks.match_picker_rows(rows, task)


def _xingtu_picker_diagnostics(page, rows, attempt):
    visible = [row for row in rows if row.get("visible")]
    titles = []
    for row in visible[:10]:
        values = list(row.get("titles") or [])
        titles.append(values[0] if values else "")
    return {"attempt": attempt,
            "modal_visible": bool(_xingtu_modal_open(page)),
            "card_count": len(rows),
            "visible_card_count": len(visible),
            "titles": titles}


def _xingtu_click_texts(page, texts):
    for text in texts:
        for item in _cover_visible_text_items(page, text):
            if xingtu_tasks.click_picker_element(item):
                return True
    return False


def _xingtu_popup_dom(page, rows):
    visible = [row for row in rows if row.get("visible")]
    search_input = {"visible": False, "value": ""}
    try:
        items = page.locator(XINGTU_SEARCH_SELECTOR)
        for index in range(items.count()):
            item = items.nth(index)
            if item.is_visible():
                search_input = {"visible": True, "value": item.input_value()}
                break
    except Exception:
        pass
    return {"cards": len(visible),
            "card_texts": [row.get("text") or row.get("name") or ""
                           for row in visible[:20]],
            "order_ids": [value for row in visible
                          for value in row.get("order_ids") or []],
            "search_input": search_input}


def _xingtu_open_picker(page, timeout):
    if _xingtu_modal_open(page):
        return True
    if not (_xingtu_click_texts(page, XINGTU_PICKER_OPEN_TEXTS)
            or _xingtu_click_texts(page, XINGTU_REPLACE_TEXTS)):
        return False
    return bool(_wait_for(lambda: _xingtu_modal_open(page), page,
                          timeout=min(5.0, max(0.2, float(timeout)))))


def _xingtu_close_picker(page, timeout=2.0):
    if not _xingtu_modal_open(page):
        return True
    _xingtu_click_texts(page, ("\u53d6\u6d88",))
    if _wait_for(lambda: not _xingtu_modal_open(page), page,
                 timeout=min(2.0, float(timeout))):
        return True
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    if _wait_for(lambda: not _xingtu_modal_open(page), page, timeout=1.0):
        return True
    try:
        page.evaluate("""() => {
          const target = Array.from(document.querySelectorAll('button'))
              .find(el => String(el.innerText || el.textContent || '').trim()
                  === '\u53d6\u6d88');
          if (target) { target.click(); return true; }
          return false;
        }""")
    except Exception:
        pass
    return bool(_wait_for(lambda: not _xingtu_modal_open(page), page,
                          timeout=1.0))


def _xingtu_requires_live_enumeration(task):
    """Family tokens are only safe after a live uniqueness check.

    order_id and full_name are exact identities, so an already mounted block
    can be trusted for them.  A family token is a category and must still be
    enumerated to prove that exactly one live card exists.
    """
    try:
        selector = xingtu_tasks.task_selector(task)
    except Exception:
        return True
    return selector.get("kind") == "family_token"


def preflight_xingtu_selection(page, task, *, driver=None, timeout=20.0):
    """Read-only picker preflight: enumerate, resolve, close, never select.

    ``deferred`` means the picker is not available yet on the upload entry
    page.  The caller retries after upload and before the publish button, so
    no publish can happen without a successful preflight.
    """
    task = dict(task or {})
    task_name = str(task.get("task_name") or "").strip()
    order_id = str(task.get("order_id") or "").strip()
    family = str(task.get("family") or "")
    token = xingtu_tasks.canonical_task_name(family)
    expected = xingtu_tasks.normalize_task_name(task_name or order_id or token)
    if not expected:
        return {"status": "deferred", "available": False,
                "code": "XINGTU_TASK_PREFLIGHT_DEFERRED",
                "reason": "selector_missing", "resolved_identity": None,
                "candidates": []}

    already_attached = (_xingtu_page_shows_task(page, task)
                        or _xingtu_deeplink_attached(page, task))
    if already_attached and not _xingtu_requires_live_enumeration(task):
        identity = ({"kind": "order_id", "value": order_id,
                     "display_name": task_name or order_id}
                    if order_id else
                    {"kind": "full_name",
                     "value": xingtu_tasks.normalize_task_name(
                         task.get("task_name_full") or task_name),
                     "display_name": task_name})
        outcome = {"status": xingtu_tasks.RESOLVE_OK, "rows": [], "matches": [],
                   "code": "OK", "candidates": [], "reason": "already_attached",
                   "resolved_identity": identity, "available": True,
                   "already_attached": True}
        _xingtu_stage(driver, "xingtu.preflight", result="ok",
                      reason="already_attached", expected=expected)
        return outcome

    opened = _xingtu_open_picker(page, timeout)
    if not opened:
        _xingtu_stage(driver, "xingtu.preflight", result="deferred",
                      reason="picker_not_ready", expected=expected)
        return {"status": "deferred", "available": False,
                "code": "XINGTU_TASK_PREFLIGHT_DEFERRED",
                "reason": "picker_not_ready", "resolved_identity": None,
                "candidates": []}

    outcome = None
    try:
        _xingtu_fill_search(page, task_name or order_id)

        def resolved():
            outcome = xingtu_tasks.match_picker_rows(
                _xingtu_card_rows(page), task)
            if outcome.get("status") == xingtu_tasks.RESOLVE_NOT_FOUND:
                return None
            return outcome

        outcome = _wait_for(resolved, page, timeout=timeout)
        if outcome is None:
            try:
                search = page.locator(XINGTU_SEARCH_SELECTOR).first
                search.fill("")
                _pause_page(page)
            except Exception:
                pass
            outcome = _wait_for(resolved, page, timeout=min(8.0, float(timeout)))
        if outcome is None:
            outcome = xingtu_tasks.match_picker_rows(
                _xingtu_card_rows(page), task)
        outcome = dict(outcome or {})
        outcome["available"] = True
        _xingtu_stage(
            driver, "xingtu.preflight",
            result=outcome.get("status"), expected=expected,
            reason=outcome.get("reason"),
            count=len(outcome.get("candidates") or []),
            names=[item.get("task_name") for item in
                   (outcome.get("candidates") or [])[:8]])
        return outcome
    finally:
        closed = _xingtu_close_picker(page, timeout=min(5.0, float(timeout)))
        # Never mask the resolver failure with a cleanup failure.  Only a
        # clean preflight that cannot close its own modal is a new error.
        if (not closed and sys.exc_info()[0] is None
                and (outcome is None
                     or outcome.get("status") == xingtu_tasks.RESOLVE_OK)):
            raise xingtu_tasks.XingtuTaskChanged(
                "只读任务预检后无法关闭星图任务弹窗，已停止发布")


def _xingtu_fill_search(page, search_text):
    """Optionally type into the picker search; absence is not a failure."""
    if not search_text:
        return False
    try:
        search = page.locator(XINGTU_SEARCH_SELECTOR)
        if not search.count():
            return False
        item = search.first
        if not item.is_visible():
            return False
    except Exception:
        return False
    if not xingtu_tasks.click_picker_element(item):
        return False
    try:
        item.fill(search_text)
    except Exception:
        try:
            item.evaluate("""(el, value) => {
              el.value = value;
              el.dispatchEvent(new Event('input', {bubbles: true}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
            }""", search_text)
        except Exception:
            return False
    _pause_page(page)
    return True


def _xingtu_row_identity(row):
    """Stable identity for a picker row: order id, else task name.

    ``data-xt-idx`` is reassigned on every DOM read/re-render, so it is only a
    click handle, never an identity for selection/readback.
    """
    order = str((row or {}).get("order_id") or "").strip()
    if order:
        return ("order", order)
    name = _xingtu_task_label((row or {}).get("name"))
    if name:
        return ("name", name)
    return None


def _xingtu_row_matches(row, identity):
    if not identity:
        return False
    return _xingtu_row_identity(row) == identity


def _xingtu_current_row(page, row):
    """Re-resolve *row* by identity; None when it is gone or ambiguous."""
    identity = _xingtu_row_identity(row)
    if not identity:
        return None
    matches = [item for item in _xingtu_card_rows(page)
               if _xingtu_row_matches(item, identity)
               and row.get("portal_id") and row.get("card_id")
               and item.get("portal_id") == row["portal_id"]
               and item.get("card_id") == row["card_id"]]
    if len(matches) != 1:
        return None
    return matches[0]


def _xingtu_row_selected(page, row):
    current = _xingtu_current_row(page, row)
    return bool(current and current.get("checked"))


def _xingtu_marked_row(page, portal_id, card_id):
    """Read one card through the exact portal/card markers, never a fresh query."""
    import re
    values = (str(portal_id or ""), str(card_id or ""))
    if not all(re.fullmatch(r"[0-9a-f-]{36}", value) for value in values):
        return None
    try:
        value = page.evaluate("""({portalId, cardId, radioSel}) => {
""" + xingtu_tasks.XINGTU_VISIBLE_CARD_NAME_JS + r"""
          const portals = Array.from(
              document.querySelectorAll('[data-xt-portal-id]')).filter(
              el => el.getAttribute('data-xt-portal-id') === portalId);
          if (portals.length !== 1 || !__xingtuVisible(portals[0])) return null;
          const cards = Array.from(
              portals[0].querySelectorAll('[data-xt-card-id]')).filter(
              el => el.getAttribute('data-xt-card-id') === cardId);
          if (cards.length !== 1 || !__xingtuVisible(cards[0])) return null;
          const card = cards[0];
          if (!window.__xtIdentities ||
              window.__xtIdentities.get(portals[0]) !== portalId ||
              window.__xtIdentities.get(card) !== cardId) return null;
          const live = Array.from(document.querySelectorAll('.semi-portal'))
              .filter(el => __xingtuVisible(el) && /星图任务/.test(el.innerText || el.textContent || ''));
          const roots = live.filter(el => !live.some(other => other !== el && other.contains(el)));
          if (roots.length !== 1 || roots[0] !== portals[0]) return null;
          const radio = card.querySelector(radioSel);
          const orderIds = __xingtuCardOrderIds(card);
          return {
            portal_id: portalId,
            card_id: cardId,
            name: __xingtuCardName(card) || __xingtuNormalize(
                __xingtuVisibleText(card)),
            text: __xingtuVisibleText(card),
            order_id: orderIds.length === 1 ? orderIds[0] : '',
            order_ids: orderIds,
            checked: !!(radio && String(radio.className || '').indexOf(
                'semi-radio-checked') >= 0) || !!card.querySelector(
                'input[type=radio]:checked, [aria-checked="true"], [aria-selected="true"]'),
            visible: true
          };
        }""", {"portalId": values[0], "cardId": values[1],
                "radioSel": XINGTU_RADIO_SELECTOR})
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _xingtu_select_fill_row(page, row, *, wait_for_selection=True):
    # Resolve the row by identity immediately before clicking.  A refresh may
    # have reassigned every data-xt-idx; clicking the old number would hit the
    # wrong card or nothing.
    current = _xingtu_current_row(page, row)
    if current is None:
        return None
    import re
    portal_id, card_id = current["portal_id"], current["card_id"]
    if not all(re.fullmatch(r"[0-9a-f-]{36}", value) for value in (portal_id, card_id)):
        return None
    portal = page.locator('[data-xt-portal-id="%s"]' % portal_id)
    card = portal.locator('[data-xt-card-id="%s"]' % card_id)
    if portal.count() != 1 or card.count() != 1:
        return None
    card = card.element_handle()
    if card is None or not card.evaluate("""(el, ids) => el.isConnected &&
        window.__xtIdentities && window.__xtIdentities.get(el) === ids[1] &&
        window.__xtIdentities.get(el.closest('[data-xt-portal-id]')) === ids[0]
        """, [portal_id, card_id]):
        return None
    radio = card.query_selector(XINGTU_RADIO_SELECTOR)
    radio_label = card.query_selector("label.semi-radio")

    def selected_card():
        fresh = _xingtu_marked_row(page, portal_id, card_id)
        return fresh if (fresh and fresh.get("checked") and
                         _xingtu_row_identity(fresh) == _xingtu_row_identity(current)) else None

    def committed():
        """Checked radio, or the picker closed (selection committed)."""
        fresh = _wait_for(selected_card, page,
                          timeout=3.0 if wait_for_selection else 2.0)
        if fresh:
            return fresh
        if not _xingtu_modal_open(page):
            return current
        return None

    # The card container itself is not the activation target in the Douyin
    # picker: a real click on `div.card-container-*` leaves the wrapped
    # `label.semi-radio > input[type=radio]` unchecked and the modal open, and
    # fill mode used to accept that as success because it never re-read the
    # radio.  Click the radio label first (the only target that actually
    # toggles the input), then the radio, and the card last.
    for target in (radio_label, radio, card):
        if target is None:
            continue
        if xingtu_tasks.click_picker_element(target):
            fresh = committed()
            if fresh:
                return fresh
    return None


def _xingtu_confirm_picker(page, timeout, row=None):
    # Allow auto-close before looking for an optional confirmation button.
    close_timeout = min(1.5, max(0.0, float(timeout)))
    if _wait_for(lambda: not _xingtu_modal_open(page), page,
                 timeout=close_timeout):
        return True
    if not row or not _xingtu_current_row(page, row):
        return False
    portal = page.locator('[data-xt-portal-id="%s"]' % row["portal_id"])
    if portal.count() != 1:
        return False
    confirm = portal.locator('button')
    confirm_locator = None
    for index in range(confirm.count()):
        item = confirm.nth(index)
        try:
            if item.is_visible() and "\u786e\u5b9a" in str(item.inner_text() or ""):
                confirm_locator = item
                break
        except Exception:
            continue
    if confirm_locator is None:
        return False
    else:
        confirmed = {"ok": xingtu_tasks.click_picker_element(confirm_locator)}
    if not confirmed.get("ok"):
        return False
    _wait_for(lambda: not _xingtu_modal_open(page), page, timeout=close_timeout)
    return not _xingtu_modal_open(page)


def _select_publish_xingtu_task_fill(page, task, *, driver=None, timeout=20.0):
    """Fill mode: pick inside the publish-page popup, then read the name back."""
    task_name = str((task or {}).get("task_name") or "").strip()
    order_id = str((task or {}).get("order_id") or "").strip()
    expected = _xingtu_task_label(task_name)
    if not expected:
        _xingtu_stage(driver, "xingtu.attach", result="skipped", reason="no_task_name")
        return {"selected": False, "skipped": True, "reason": "no_task_name"}

    _xingtu_stage(driver, "xingtu.duplicate", result="skipped",
                  reason="fill_mode")
    readback = _xingtu_page_task_readback(page, task)
    if readback["ok"]:
        _xingtu_stage(driver, "xingtu.readback", expected=readback["expected"],
                      actual=readback["actual"], ok=True,
                      readback_via=readback["readback_via"])
        result = {"selected": False, "already_attached": True,
                  "task_name": task_name, "order_id": order_id}
        if driver is not None:
            driver._record("xingtu_task",
                           note="star task already visible on upload page",
                           extra=dict(result))
        return result

    last = {}
    failure = "xingtu.card_not_found"
    for attempt in (1, 2):
        if attempt > 1:
            _xingtu_close_picker(page, timeout=min(2.0, float(timeout)))
        opened = _xingtu_open_picker(page, timeout)
        rows = _xingtu_card_rows(page)
        popup = _xingtu_popup_dom(page, rows)
        snapshot = _xingtu_picker_diagnostics(page, rows, attempt)
        _xingtu_stage(driver, "xingtu.popup.dom", attempt=attempt, **popup)
        if opened:
            _xingtu_fill_search(page, task_name or order_id)

            def match():
                outcome = _xingtu_fill_match_rows(_xingtu_card_rows(page), task)
                if outcome.get("status") == xingtu_tasks.RESOLVE_NOT_FOUND:
                    return None
                return outcome

            outcome = _wait_for(match, page, timeout=timeout)
            rows = _xingtu_card_rows(page)
            popup = _xingtu_popup_dom(page, rows)
            # Freeze counts and titles before clicking; polling must not overwrite them.
            snapshot = _xingtu_picker_diagnostics(page, rows, attempt)
            snapshot.update(popup)
            if outcome:
                if outcome.get("status") != xingtu_tasks.RESOLVE_OK:
                    _xingtu_stage(driver, "xingtu.picker", **dict(
                        snapshot, reason=outcome.get("reason"),
                        code=outcome.get("code"),
                        candidates=outcome.get("candidates")))
                    xingtu_tasks.raise_match_error(outcome, task)
                rows_ok = list(outcome.get("rows") or [])
                if len(rows_ok) != 1:
                    xingtu_tasks.raise_match_error(
                        {"status": xingtu_tasks.RESOLVE_AMBIGUOUS,
                         "code": "XINGTU_TASK_AMBIGUOUS",
                         "reason": "non_unique_ok", "candidates": []}, task)
                row = rows_ok[0]
                _xingtu_stage(driver, "xingtu.pick",
                              mode=outcome.get("reason"), n=1,
                              resolved_identity=outcome.get("resolved_identity"))
                if not _xingtu_select_fill_row(page, row, wait_for_selection=False):
                    raise xingtu_tasks.XingtuTaskChanged(
                        "星图任务卡在选择时发生变化，已停止发布")
                else:
                    _xingtu_confirm_picker(page, timeout, row)

                    def read_form():
                        nonlocal readback
                        readback = _xingtu_page_task_readback(
                            page, dict(task, task_name=row.get("name") or task_name))
                        return readback["ok"]

                    _wait_for(read_form, page, timeout=min(15.0, max(0.0, float(timeout))))
                    _xingtu_stage(driver, "xingtu.readback",
                                  expected=readback["expected"],
                                  actual=readback["actual"], ok=bool(readback["ok"]),
                                  readback_via=readback["readback_via"])
                    if readback["ok"]:
                        _xingtu_stage(driver, "xingtu.picker", **dict(
                            snapshot, modal_visible=bool(_xingtu_modal_open(page)),
                            reason="selected"))
                        result = {"selected": True, "already_attached": False,
                                  "task_name": task_name, "order_id": order_id,
                                  "resolved_identity": outcome.get("resolved_identity")}
                        if driver is not None:
                            driver._xingtu_resolved_identity = outcome.get(
                                "resolved_identity")
                            driver._record("xingtu_task",
                                           note="star task selected inside publish page",
                                           extra=dict(result))
                        return result
                    failure = "xingtu.readback_mismatch"
        last = dict(snapshot)
        last.update(popup)
        last.update(reason=failure, actual=readback.get("actual", ""),
                    modal_visible=bool(_xingtu_modal_open(page)),
                    page_url=_page_url_value(page),
                    task_name_nodes=readback.get("task_name_nodes", []),
                    page_has_expected=readback.get("page_has_expected", False))
        _xingtu_stage(driver, "xingtu.picker", **last)

    raise xingtu_tasks.XingtuTaskNotAttached(
        "%s expected=%r attempts=2 last=%s"
        % (failure, expected, json.dumps(last, ensure_ascii=False, default=str)))


def _xingtu_page_shows_task(page, task):
    """MOUNT-ONLY predicate; never accepts a picker card as attached.

    See ``_xingtu_mount_contract`` for the three same-block facts.  Selection
    has its own contract in ``_xingtu_card_rows``; the two must not share a
    selector scope.
    """
    return bool(_xingtu_mount_contract(page, task).get("ok"))


def _xingtu_flat_text(value):
    """Normalize text for星图 task-name comparison (NFKC + dash + zero-width)."""
    s = unicodedata.normalize("NFKC", str(value or ""))
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        s = s.replace(ch, "")
    for dash in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212", "\uff0d"):
        s = s.replace(dash, "-")
    return "".join(s.split())


def _xingtu_deeplink_attached(page, task):
    """True when the deep-linked upload page already carries the task.

    ``_ensure_upload_page`` navigates to ``post/video?order_id=<id>``; Douyin
    then preselects that task and renders the attached block with the task
    name plus the 更换/删除 controls.  That block exposes no selected-state
    control, so the strict DOM probe reports "not attached", the flow opens
    the picker and dies with XINGTU_TASK_NOT_ATTACHED.

    Evidence stays narrow on purpose: the URL must carry exactly our
    ``order_id`` (parsed, not substring-matched, on creator.douyin.com) AND
    one real task card must contain the name, the same order id and the
    更换/删除 control.  Page-wide text is never accepted as card evidence.
    """
    order_id = str((task or {}).get("order_id") or "").strip()
    name = _xingtu_flat_text(
        xingtu_tasks.normalize_task_name((task or {}).get("task_name")))
    if not order_id or not name:
        return False
    try:
        url = str(_page_url_value(page) or "")
    except Exception:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if not str(parsed.netloc or "").lower().endswith("creator.douyin.com"):
        return False
    try:
        query = parse_qs(parsed.query or "")
    except Exception:
        query = {}
    if order_id not in [str(v).strip() for v in (query.get("order_id") or [])]:
        return False
    return _xingtu_page_shows_task(page, task)


def select_publish_xingtu_task(page, task, *, driver=None, mode="publish",
                               timeout=20.0):
    """Select the chosen task inside the Douyin publish page.

    The ``?order_id=`` upload URL is only an entry point.  A task counts as
    attached only after it is visible on the publish page after an explicit
    picker interaction (or was already visible before the picker opened).
    """
    task = dict(task or {})
    task_name = str(task.get("task_name") or "").strip()
    order_id = str(task.get("order_id") or "").strip()
    search_text = task_name or order_id
    if str(mode) == MODE_FILL:
        return _select_publish_xingtu_task_fill(
            page, task, driver=driver, timeout=timeout)
    if not search_text:
        raise xingtu_tasks.XingtuTaskNotAttached(
            "xingtu task has neither task_name nor order_id")

    if ((_xingtu_page_shows_task(page, task)
         or _xingtu_deeplink_attached(page, task))
            and not _xingtu_requires_live_enumeration(task)):
        result = {"selected": False, "already_attached": True,
                  "task_name": task_name, "order_id": order_id}
        if driver is not None:
            driver._record("xingtu_task", note="star task already visible on upload page",
                           extra=dict(result))
        return result

    open_result = {"ok": _xingtu_click_texts(page, XINGTU_PLACEHOLDER_TEXTS)}
    if not open_result.get("ok"):
        open_result = {"ok": _xingtu_click_texts(page, XINGTU_REPLACE_TEXTS)}
    if not open_result.get("ok"):
        raise xingtu_tasks.XingtuTaskNotAttached(
            "could not open the star task picker on the publish page")

    if not _wait_for(lambda: _xingtu_modal_open(page), page, timeout=min(5.0, timeout)):
        raise xingtu_tasks.XingtuTaskNotAttached(
            "star task picker did not open on the publish page")

    try:
        search = page.locator(XINGTU_SEARCH_SELECTOR).first
        xingtu_tasks.click_picker_element(search)
    except Exception:
        search = page.locator(XINGTU_SEARCH_SELECTOR).first
        search.evaluate("el => el.click()")
    try:
        search.fill(search_text)
    except Exception:
        search.evaluate("""(el, value) => {
          el.value = value;
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
        }""", search_text)
    _pause_page(page)

    expected = (xingtu_tasks.normalize_task_name(task_name) if task_name
                else xingtu_tasks.normalize_task_name(order_id))

    def _visible_cards():
        cards = _xingtu_card_rows(page)
        return [row for row in cards if row.get("visible")]

    def _card_names():
        return [_xingtu_task_label(row.get("name"))[:80]
                for row in _visible_cards()][:8]

    def matching_rows():
        cards = _xingtu_card_rows(page)
        outcome = _xingtu_fill_match_rows(cards, task)
        if outcome.get("status") != xingtu_tasks.RESOLVE_NOT_FOUND:
            return outcome

        # Compatibility fallback for picker rows whose visible label/text is
        # not exposed through the shared resolver's title fields.  Exact
        # matches stay ahead of containment, and the shared resolver still
        # decides ambiguity/unavailability for the narrowed candidate set.
        selector = xingtu_tasks.task_selector(task)
        expected = str(selector.get("value") or "")
        exact_candidates, contained_candidates = [], []
        for row in cards:
            if not row.get("visible"):
                continue
            order_ids = [xingtu_tasks.normalize_order_id(value)
                         for value in (row.get("order_ids") or []) if value]
            if not order_ids and row.get("order_id"):
                order_ids = [xingtu_tasks.normalize_order_id(row.get("order_id"))]
            if selector.get("kind") == "order_id":
                if expected in order_ids:
                    exact_candidates.append(row)
                continue
            labels = [xingtu_tasks.normalize_task_name(row.get("name"))]
            labels.extend(xingtu_tasks.normalize_task_name(value)
                          for value in (row.get("titles") or []) if value)
            labels = [value for value in labels if value]
            text = xingtu_tasks.normalize_task_name(row.get("text"))
            if expected and expected in labels:
                exact_candidates.append(row)
            elif expected and (expected in text or
                               any(expected in value for value in labels)):
                contained_candidates.append(row)

        def _resolve(candidates):
            if not candidates:
                return None
            resolved = _xingtu_fill_match_rows(candidates, task)
            if resolved.get("status") == xingtu_tasks.RESOLVE_NOT_FOUND:
                return None
            return resolved

        exact = _resolve(exact_candidates)
        contained = _resolve(contained_candidates)
        return exact or contained

    _xingtu_stage(driver, "xingtu.cards", expected=expected,
                  count=len(_visible_cards()), names=_card_names())
    outcome = _wait_for(matching_rows, page, timeout=timeout)
    if not outcome:
        # The picker search filters harder than its own list (it matches task
        # ids, not the category word).  Clear it and match the whole list.
        try:
            search.fill("")
            _pause_page(page)
        except Exception:
            pass
        outcome = _wait_for(matching_rows, page, timeout=min(8.0, float(timeout)))
        _xingtu_stage(driver, "xingtu.cards.after_clear", expected=expected,
                      count=len(_visible_cards()), names=_card_names())
    if not outcome:
        outcome = xingtu_tasks.match_picker_rows(_xingtu_card_rows(page), task)
        xingtu_tasks.raise_match_error(outcome, task)
    if outcome.get("status") != xingtu_tasks.RESOLVE_OK:
        _xingtu_stage(driver, "xingtu.pick.duplicates", expected=expected,
                      count=len(outcome.get("candidates") or []),
                      names=_card_names(), code=outcome.get("code"),
                      reason=outcome.get("reason"))
        xingtu_tasks.raise_match_error(outcome, task)
    rows = list(outcome.get("rows") or [])
    if len(rows) != 1:
        xingtu_tasks.raise_match_error(
            {"status": xingtu_tasks.RESOLVE_AMBIGUOUS,
             "code": "XINGTU_TASK_AMBIGUOUS", "reason": "non_unique_ok",
             "candidates": outcome.get("candidates") or []}, task)
    first_identity = outcome.get("resolved_identity")
    preflight_identity = getattr(driver, "_xingtu_resolved_identity", None) if driver else None
    if preflight_identity and first_identity != preflight_identity:
        raise xingtu_tasks.XingtuTaskChanged(
            "星图任务卡与上传前预检身份不一致，已停止发布")
    # The picker re-renders when its search filter applies, which reassigns
    # ``data-xt-idx``.  An index captured before that re-render points at the
    # wrong card (or none) and the radio click silently misses, surfacing as
    # "star task card did not become selected" although the card is visible.
    # Wait for the list to settle and re-resolve the row right before clicking.
    page.wait_for_timeout(1500)
    fresh_rows = matching_rows()
    if not fresh_rows:
        raise xingtu_tasks.XingtuTaskChanged(
            "星图任务卡在预检后消失，已停止发布")
    if fresh_rows.get("status") != xingtu_tasks.RESOLVE_OK:
        xingtu_tasks.raise_match_error(fresh_rows, task)
    if fresh_rows.get("resolved_identity") != first_identity:
        raise xingtu_tasks.XingtuTaskChanged(
            "星图任务卡在预检后身份发生变化，已停止发布")
    if (preflight_identity and
            fresh_rows.get("resolved_identity") != preflight_identity):
        raise xingtu_tasks.XingtuTaskChanged(
            "星图任务卡与上传前预检身份不一致，已停止发布")
    rows = list(fresh_rows.get("rows") or [])
    if len(rows) != 1:
        xingtu_tasks.raise_match_error(fresh_rows, task)
    row = rows[0]

    selected = _xingtu_select_fill_row(page, row)
    if not selected:
        page.wait_for_timeout(800)
        retry_rows = matching_rows()
        if not retry_rows:
            raise xingtu_tasks.XingtuTaskChanged(
                "星图任务卡在重试前消失，已停止发布")
        if retry_rows.get("status") != xingtu_tasks.RESOLVE_OK:
            xingtu_tasks.raise_match_error(retry_rows, task)
        retry_cards = list(retry_rows.get("rows") or [])
        if len(retry_cards) != 1:
            xingtu_tasks.raise_match_error(retry_rows, task)
        selected = _xingtu_select_fill_row(page, retry_cards[0])
        if not selected:
            raise xingtu_tasks.XingtuTaskChanged(
                "星图任务卡没有成功选中，已停止发布")
        row = retry_cards[0]
    if not selected or not selected.get("checked"):
        raise xingtu_tasks.XingtuTaskNotAttached(
            "star task card did not become selected")
    if order_id:
        selected_order = str((selected or {}).get("order_id") or "").strip()
        if not selected_order:
            raise xingtu_tasks.XingtuTaskNotAttached(
                "selected star task card has no readable order_id")
        if selected_order != order_id:
            raise xingtu_tasks.XingtuTaskNotAttached(
                "selected star task order_id %r does not match %r"
                % (selected_order, order_id))

    if not _xingtu_current_row(page, selected):
        raise xingtu_tasks.XingtuTaskNotAttached("selected portal/card was replaced")
    portal = page.locator('[data-xt-portal-id="%s"]' % selected["portal_id"])
    if portal.count() != 1:
        raise xingtu_tasks.XingtuTaskNotAttached("selected portal is ambiguous")
    confirm = portal.locator('button')
    confirm_locator = None
    for index in range(confirm.count()):
        item = confirm.nth(index)
        try:
            if item.is_visible() and "\u786e\u5b9a" in str(item.inner_text() or ""):
                confirm_locator = item
                break
        except Exception:
            continue
    if confirm_locator is None:
        raise xingtu_tasks.XingtuTaskNotAttached(
            "star task picker confirm button not found")
    confirmed = {"ok": xingtu_tasks.click_picker_element(confirm_locator)}
    if not confirmed.get("ok"):
        raise xingtu_tasks.XingtuTaskNotAttached(
            "star task picker confirm button could not be clicked")

    _wait_for(lambda: not _xingtu_modal_open(page), page, timeout=timeout)
    if _xingtu_modal_open(page):
        raise xingtu_tasks.XingtuTaskNotAttached(
            "star task picker did not close after confirm")

    if not _wait_for(lambda: _xingtu_page_shows_task(page, task), page,
                     timeout=timeout):
        raise xingtu_tasks.XingtuTaskNotAttached(
            "selected star task is not shown on the publish page; publish stopped")

    resolved_identity = fresh_rows.get("resolved_identity")
    result = {"selected": True, "already_attached": False,
              "task_name": task_name, "order_id": order_id,
              "resolved_identity": resolved_identity}
    if driver is not None:
        driver._xingtu_resolved_identity = resolved_identity
        driver._record("xingtu_task", note="star task selected inside publish page",
                       extra=dict(result))
    return result


class IdempotencyConsumed(GateRed):
    """The same material already succeeded; no new execution happened.

    Callers must not turn this known-success refusal into a channel freeze:
    the repair path is an explicit, signed customer-confirmed republish, not
    manual unfreezing.
    """


class VerificationQueryError(RuntimeError):
    """A DOM/CDP query failed; this is UNKNOWN, never 'popup disappeared'."""


def assert_selector_policy():
    """Raise at build/test time when a selector regresses to class/fuzzy text."""
    problems = validate_selectors((
        *TITLE_SELECTORS, *TOPIC_SELECTORS,
        *COVER_BUTTON_SELECTORS, *COVER_INPUT_SELECTORS,
        *PUBLISH_BUTTON_SELECTORS, *UPLOAD_PROGRESS_SELECTORS,
        *TRANSCODE_DONE_SELECTORS, *EDIT_LINK_SELECTORS,
        *UPLOAD_RESOURCE_SELECTORS, *BODY_SELECTORS,
        *ACCOUNT_SELECTORS, *ONBOARDING_DONE_SELECTORS,
        *VIDEO_PREVIEW_OK_SELECTORS, *VERIFICATION_CONTAINER_SELECTORS,
        *MANAGE_EMPTY_STATE_SELECTORS, *MANAGE_NEXT_PAGE_SELECTORS,
        *PUBLISHED_AT_SELECTORS,
        *SMS_GET_CODE_SELECTORS, *SMS_CODE_INPUT_SELECTORS,
        *SMS_VERIFY_BUTTON_SELECTORS, *QR_ELEMENT_SELECTORS,
    ))
    if problems:
        raise RuntimeError("publish selector policy violated: " + "; ".join(problems))


def is_valid_mid(value) -> bool:
    """Platform mids are exactly 19 ASCII decimal digits."""
    return is_ascii_mid(value)


def mid_from_url(url: str | None) -> str | None:
    """Extract and validate the ``mid=`` work id from an edit-link URL."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    for key in ("mid",):
        values = parse_qs(parsed.query).get(key) or []
        if len(values) == 1 and is_valid_mid(values[0]):
            return str(values[0]).strip()
    return None


PLATFORM_TIME_RE = re.compile(
    r"[0-9]{4}[-/.][0-9]{1,2}[-/.][0-9]{1,2}[ T][0-9]{1,2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?")


def parse_platform_time(value):
    """Parse a complete, explicit timestamp; preserve timezone and precision."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        match = PLATFORM_TIME_RE.fullmatch(text)
        if not match:
            return None
        date, rest = text[:10], text[10:]
        candidate = date.replace("/", "-").replace(".", "-") + rest
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        # Platform wall times without a timezone are not an exact instant.
        return None
    return parsed.timestamp()


def verification_method_from_signals(signals: dict | None) -> str | None:
    """Return the one verification path the page actually offers.

    SMS wins when both signals exist because the real evidence defines an SMS
    input as an actionable code checkpoint. QR is accepted only when the same
    verification container carries QR semantics and a visible QR canvas/image.
    No signal means the caller must time out and freeze, never guess from size.
    """
    if not isinstance(signals, dict):
        return None

    def count(name):
        try:
            return max(0, int(signals.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    if count("sms_inputs") > 0:
        return VERIFICATION_SMS
    if signals.get("qr_semantic") and count("qr_canvas") + count("qr_images") > 0:
        return VERIFICATION_QR
    return None


def publish_key_for(plan: dict) -> str:
    """C: material + environment + time window, plus the star task identity.

    A star order id is preferred.  When the task was selected by name on the
    publish page there is no order id, so the task name is used instead; it
    must never collapse to an empty string, which would make different tasks
    share one replay identity.
    """
    piece = plan.get("piece") or {}
    account_id = plan.get("account_id") or ""
    schedule_at = plan.get("schedule_at") or ""
    env_id = plan.get("env_id")
    if not env_id and plan.get("channel_id") and ":" in str(plan["channel_id"]):
        env_id = str(plan["channel_id"]).split(":", 1)[0]
    if env_id:
        key = publish_key(env_id, piece, schedule_at, account_id=account_id)
    else:
        chash = content_hash(account_id, piece.get("title"), piece.get("copy_body"),
                             piece.get("sha256"))
        key = idempotency_key(account_id, chash, schedule_at)
    task = xingtu_tasks.task_from_plan(plan)
    if task.get("order_id"):
        key += "|xingtu:" + str(task["order_id"])
    elif task.get("task_name"):
        key += "|xingtu:" + str(task["task_name"])
    return key


class NullDriver:
    """TEST-ONLY simulated driver.  Never used by the production entry point."""

    simulated = True

    def __init__(self, platform_work_id=None, fail_on_publish=False):
        self.calls = []
        self.platform_work_id = platform_work_id
        self.fail_on_publish = fail_on_publish

    def fill(self, plan):
        self.calls.append(("fill", plan.get("plan_id")))

    def click_publish(self, plan):
        self.calls.append(("click_publish", plan.get("plan_id")))
        if self.fail_on_publish:
            raise RuntimeError("simulated click failure")

    def read_back_work_id(self, plan):
        self.calls.append(("read_back", plan.get("plan_id")))
        return self.platform_work_id


class PlaywrightDriver:
    """Real driver attached to the customer's Google Chrome over CDP.

    It never launches a browser itself; ``cli.cmd_publish`` first reuses
    ``browser.open_environment`` (the same Chrome/CDP launch path used for
    login) and passes this class the environment's ``cdp_url``.
    """

    simulated = False
    allow_submit = False

    def __init__(self, cdp_url, *, layout=None, env=None, channel=None,
                 plan=None, allow_submit=False, settings=None, humanizer=None,
                 sms_code_file=None, sms_wait_seconds=None,
                 verification_detect_seconds=None, qr_wait_seconds=None):
        self.cdp_url = cdp_url
        self.layout = layout
        self.env = env
        self.channel = channel
        self.plan = plan or {}
        self.allow_submit = bool(allow_submit)
        self.settings = settings or {}
        self.sms_code_file = sms_code_file
        self.sms_wait_seconds = sms_wait_seconds
        self.verification_detect_seconds = verification_detect_seconds
        self.qr_wait_seconds = qr_wait_seconds
        self.last_evidence = []
        self._filled = False
        self.topic_mode = None
        self.topic_modes = []
        self._last_work_id = None
        self.upload_attempt: UploadAttempt | None = None
        self._last_work_candidates: list[dict] = []
        self._baseline_edit_mids: set[str] = set()
        self._baseline_work_candidates: list[dict] = []
        self._baseline_ready = False
        self._baseline_empty_confirmed = False
        self._last_edit_scan_complete = False
        self._verification_root = None
        self._last_publish_evidence: dict | None = None
        self.last_publish_evidence: dict | None = None
        self._publish_outcome: dict | None = None
        self._finalized_publish = None
        self._publish_start_url: str | None = None
        self._publish_start_success_marker: str | None = None
        self._publish_start_failure_marker: str | None = None
        self._publish_start_success_counts: dict[str, int] | None = None
        self._publish_start_failure_counts: dict[str, int] | None = None
        self._publish_observation_started_at: float | None = None
        self._publish_click_completed_at: float | None = None
        self._upload_request_ids: list[str] = []
        self._upload_resource_attrs: dict = {}
        self._upload_started_at: float | None = None
        self._upload_started_monotonic: float | None = None
        self._upload_progress_logged_at: float | None = None
        self._upload_evidence_finalized = False
        self._upload_evidence: dict | None = None
        self._cover_baseline: dict | None = None
        self._cover_bound_src: str | None = None
        self._cover_bound_at: float | None = None
        self._hold_store: PublishHoldStore | None = None
        if layout is not None:
            self._hold_store = PublishHoldStore(layout.run / "publish_holds.json")

        # Resolve only once run_publish supplies the actual operation mode.
        self._xingtu_task = {}
        self._xingtu_checked_key = None
        self._xingtu_readback = None
        env_id = (getattr(env, "env_id", None) if env else
                  self.plan.get("env_id") or "default")
        if humanizer is not None:
            self.humanizer = humanizer
        elif layout is not None:
            self.humanizer = Humanizer(get_pace_profile(layout, env_id), env_id=env_id,
                                      logger=self._log_action)
        else:
            self.humanizer = Humanizer(env_id=env_id)

        if layout is not None:
            self.evidence = EvidenceStore(layout)
        else:
            self.evidence = None

        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError as exc:  # pragma: no cover - real machine only
            raise GateRed(f"playwright \u4e0d\u53ef\u7528\uff0c\u65e0\u6cd5\u9a71\u52a8\u771f Chrome\uff1a{exc}") from exc
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:  # pragma: no cover - real machine only
            self._pw.stop()
            raise GateRed(f"\u65e0\u6cd5\u901a\u8fc7 CDP \u8fde\u63a5\u771f Chrome\uff08{cdp_url}\uff09\uff1a{exc}") from exc
        self._console = []
        try:
            self._page = self._find_page()
            self._page.on("console", lambda msg: self._console.append(
                {"type": msg.type, "text": msg.text[:2000]}) if len(self._console) < 100 else None)
        except BaseException:
            self._pw.stop()
            raise

    # ---- lifecycle ------------------------------------------------------
    def _context(self):
        contexts = getattr(self._browser, "contexts", None) or []
        if not contexts:
            raise GateRed("CDP browser has no context")
        return contexts[0]

    def _find_page(self):
        """每次任务都在同一个（已登录的）context 里开一个全新页。

        不复用现有页面：上一轮可能把页停在「发布配置页」且带着旧视频，
        复用它会让上传步骤对着不可交互的输入框干等 30 秒超时。
        也不去关别人开的页：本轮自己的页在 ``close()`` 里关掉。
        """
        contexts = getattr(self._browser, "contexts", None) or []
        if not contexts:
            raise GateRed("\u6ca1\u6709\u53ef\u7528\u7684\u6d4f\u89c8\u5668 context\uff0c\u65e0\u6cd5\u627f\u63a5\u767b\u5f55\u6001")
        try:
            page = contexts[0].new_page()
        except Exception as exc:
            # 不能退到 browser.new_page()（新 context 没有账号 cookie）
            raise GateRed("\u65b0\u5efa\u53d1\u5e03\u9875\u5931\u8d25\uff1a" + type(exc).__name__ + ": " + str(exc))
        self._progress("page.new_clean_page")
        return page

    def _close_duplicate_creator_pages(self):
        """Keep exactly one creator.douyin.com tab before a fill starts.

        A real customer profile accumulated three identical post/video tabs.
        Leaving them open makes a later publish race another upload and also
        points ``_find_page`` at an arbitrary duplicate.  Non-creator tabs are
        left alone; only duplicate creator tabs are closed.
        """
        result = {"kept": "", "closed": [], "errors": [], "creator_pages": []}
        try:
            context = self._context()
            pages = list(getattr(context, "pages", None) or [])
        except Exception as exc:
            result["error"] = type(exc).__name__ + ": " + str(exc)
            return result

        creator_pages = [(index, page) for index, page in enumerate(pages)
                         if page is getattr(self, "_page", None)
                         and _is_creator_page_url(_page_url_value(page))]
        result["creator_pages"] = [_page_url_value(page)
                                   for _index, page in creator_pages]
        if len(creator_pages) <= 1:
            if creator_pages:
                result["kept"] = _page_url_value(creator_pages[0][1])
            return result

        selected = getattr(self, "_page", None)
        keep = (selected if any(page is selected for _index, page in creator_pages)
                else creator_pages[0][1])
        result["kept"] = _page_url_value(keep)
        for _index, page in creator_pages:
            if page is keep:
                continue
            url = _page_url_value(page)
            try:
                page.close()
            except Exception as exc:
                result["errors"].append({
                    "url": url,
                    "error": type(exc).__name__ + ": " + str(exc),
                })
            else:
                result["closed"].append(url)
        self._progress("page.cleanup.duplicates", kept=result["kept"],
                       closed=result["closed"],
                       creator_pages=result["creator_pages"],
                       errors=result["errors"])
        return result

    #: hosts this program itself drives; a stray duplicate there is ours
    AUTOMATION_HOSTS = ("creator.douyin.com", "www.xingtu.cn")

    def _close_duplicate_site_pages(self, hosts=None):
        """Keep at most one tab per automation host and leave every other tab.

        Every run used to launch Chrome with its target URL, so the already
        running instance opened one more window each time; runs that failed
        before the fill stage never cleaned up.  Only the hosts this program
        drives are touched -- the customer's own tabs stay open.
        """
        result = {"kept": {}, "closed": [], "errors": []}
        try:
            context = self._context()
            pages = list(getattr(context, "pages", None) or [])
        except Exception as exc:
            result["error"] = type(exc).__name__ + ": " + str(exc)
            return result
        current = getattr(self, "_page", None)
        for host in tuple(hosts or self.AUTOMATION_HOSTS):
            same = [page for page in pages
                    if page is current
                    and _host_of_url(_page_url_value(page)) == host]
            if len(same) <= 1:
                if same:
                    result["kept"][host] = _page_url_value(same[0])
                continue
            keep = next((page for page in same if page is current), same[0])
            result["kept"][host] = _page_url_value(keep)
            for page in same:
                if page is keep:
                    continue
                url = _page_url_value(page)
                try:
                    page.close()
                except Exception as exc:
                    result["errors"].append({
                        "url": url,
                        "error": type(exc).__name__ + ": " + str(exc),
                    })
                else:
                    result["closed"].append(url)
        if result["closed"] or result["errors"]:
            self._progress("page.cleanup.tabs", **result)
        return result

    def close(self):
        if getattr(self, "_staged_dir", None) is not None:
            self._staged_dir.cleanup()
        self._unregister_upload_request_listener()
        self.finish_publish_observation()
        try:
            self._close_duplicate_site_pages()
        except Exception:
            pass
        try:
            page = getattr(self, "_page", None)
            if page is not None and not page.is_closed():
                page.close()
                self._progress("page.closed_own_page")
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass

    # ---- helpers --------------------------------------------------------
    def _raw_page_url(self) -> str:
        """Read the cached page URL without any Playwright dispatch.

        ``Page.url`` is a cached local property, so reading it never
        dispatches work to Playwright's driver.
        """
        try:
            return str(getattr(self._page, "url", "") or "")
        except Exception:
            return ""

    def _upload_page_call_timeout_seconds(self) -> float:
        raw = (getattr(self, "settings", None) or {}).get(
            "upload_page_call_timeout_seconds", 2.0)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 2.0
        return min(10.0, max(0.05, value))

    def _bounded_call(self, fn, timeout, label):
        """Run a non-Playwright callable under a hard deadline.

        Kept as a generic utility for CPU/IO work.  The upload path must not
        use it: Playwright sync objects are thread-affine and every upload
        query belongs on the single driver thread.
        """
        budget = max(0.001, float(timeout or 0))
        label = str(label)
        started = _MONOTONIC()

        # Nested bounded calls inherit the outer thread's deadline instead of
        # spawning a second worker (and a second Playwright greenlet).
        if getattr(threading.current_thread(), "_winport_bounded_call", False):
            try:
                return {"ok": True, "timeout": False, "label": label,
                        "elapsed": 0.0, "value": fn()}
            except BaseException as exc:  # noqa: BLE001 - returned to caller
                return {"ok": False, "timeout": False, "label": label,
                        "elapsed": round(_MONOTONIC() - started, 3),
                        "error": exc}

        outcome = {}

        def worker():
            threading.current_thread()._winport_bounded_call = True
            try:
                outcome["value"] = fn()
                outcome["ok"] = True
            except BaseException as exc:  # noqa: BLE001 - returned to caller
                outcome["error"] = exc
                outcome["ok"] = False

        thread = threading.Thread(
            target=worker, name="bounded-" + label, daemon=True)
        thread.start()
        thread.join(budget)
        elapsed = round(_MONOTONIC() - started, 3)
        if "ok" not in outcome:
            self._progress(
                "bounded_call.timeout", label=label, elapsed=elapsed,
                timeout_seconds=round(budget, 3), url=self._raw_page_url())
            return {"ok": False, "timeout": True, "label": label,
                    "elapsed": elapsed, "timeout_seconds": round(budget, 3)}
        if not outcome.get("ok"):
            return {"ok": False, "timeout": False, "label": label,
                    "elapsed": elapsed, "error": outcome.get("error")}
        return {"ok": True, "timeout": False, "label": label,
                "elapsed": elapsed, "value": outcome.get("value")}

    def _strict_page_url(self, label="page.url", timeout=None) -> str | None:
        """Upload-stage URL read on the driver thread.

        ``None`` preserves the old timeout-shaped API for callers, but no
        cross-thread call is made any more.
        """
        budget = (self._upload_page_call_timeout_seconds()
                  if timeout is None else float(timeout))
        started = _MONOTONIC()
        try:
            value = str(self._raw_page_url() or "")
        except Exception:
            return ""
        if _MONOTONIC() - started > max(0.001, budget):
            self._progress("upload.page_url.timeout", label=label,
                           timeout_seconds=round(budget, 3))
            return None
        return value

    def _safe_page_text(self) -> str:
        try:
            return str(self._page.locator("body").evaluate(
                "el => el ? (el.innerText || el.textContent || '') : ''",
                timeout=1000) or "")
        except Exception:
            return ""

    def _record(self, step, *, note, error=None, extra=None):
        if self.evidence is None:
            return None
        screenshot = html_snapshot = None
        extra = dict(extra or {})
        extra["console"] = list(getattr(self, "_console", []))
        extra["url"] = self._safe_page_url()
        if not extra["url"]:
            extra["url_unavailable"] = True
        if self.upload_attempt:
            try:
                extra["attempt"] = asdict(self.upload_attempt)
            except Exception as exc:
                extra["attempt_unavailable"] = type(exc).__name__
        try:
            screenshot = self._page.screenshot(full_page=True, timeout=8000)
        except Exception as exc:
            extra["screenshot_unavailable"] = type(exc).__name__
        try:
            # Page.content() has no timeout.  A cover dialog can keep the
            # renderer busy long enough to hang evidence collection, which in
            # turn hides the original failure from the CLI.  Read the markup
            # through a bounded Locator call instead.
            html_snapshot = str(self._page.locator("html").evaluate(
                "el => el ? el.outerHTML : ''", timeout=3000) or "")
        except Exception as exc:
            extra["html_unavailable"] = type(exc).__name__
        try:
            record = self.evidence.capture(
                step, env_id=getattr(self.env, "env_id", None),
                channel_id=getattr(self.channel, "channel_id", None),
                platform=getattr(self.channel, "platform", None),
                error=error, note=note, screenshot=screenshot, html=html_snapshot,
                extra=extra)
            self.last_evidence.append(record)
            return record
        except Exception:
            # Evidence collection is best-effort.  A broken evidence directory
            # must never prevent the persistent hold from being written.
            return None

    def _safe_record(self, step, *, note, error=None, extra=None):
        try:
            return self._record(step, note=note, error=error, extra=extra)
        except Exception:
            return None

    def _progress(self, stage, **fields):
        """Write an observable stage line so a real-machine hang is locatable."""
        now = time.time()
        payload = {"stage": str(stage), "ts": round(now, 3)}
        last = getattr(self, "_progress_last_ts", None)
        if last is not None:
            payload["dt"] = round(now - last, 2)
        self._progress_last_ts = now
        payload.update({key: _log_field(key, value)
                        for key, value in fields.items()
                        if value is not None})
        try:
            print("PUBLISH_STAGE " + json.dumps(payload, ensure_ascii=False,
                                                default=str),
                  file=sys.stderr, flush=True)
        except Exception:
            pass

    def _upload_timeout_seconds(self):
        plan = getattr(self, "plan", None) or {}
        settings = getattr(self, "settings", None) or {}
        raw = plan.get("upload_timeout_seconds")
        if raw is None:
            raw = settings.get("upload_timeout_seconds",
                               UPLOAD_EVIDENCE_TIMEOUT_SECONDS)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = float(UPLOAD_EVIDENCE_TIMEOUT_SECONDS)
        return min(float(UPLOAD_EVIDENCE_TIMEOUT_MAX_SECONDS),
                   max(1.0, value))

    def _upload_lookup_timeout_seconds(self):
        """Hard upper bound for the whole ``upload.input.lookup`` step."""
        try:
            value = float((getattr(self, "settings", None) or {}).get(
                "upload_lookup_timeout_seconds", 20))
        except (TypeError, ValueError):
            value = 20.0
        return min(60.0, max(0.1, value))

    def _wait_usable_upload_input(self, timeout=60.0,
                                  stop_event=None):
        """Poll every frame for a usable video file input, never by position.

        Client evidence (2026-11-10, douyin creator):
          * clean /content/upload page has exactly ONE input[type=file]
            (accept=video, enabled, 1x1px inside the drop zone);
          * /content/post/video config page has TWO inputs
            (index 0 = cover image, index 1 = video).
        Picking by index therefore either waits forever for an index that does
        not exist or grabs the cover input. Filter by accept+enabled instead;
        if several survive, prefer the only visible one, otherwise fail closed.
        """
        budget = max(float(timeout or 0.0), 1.0)
        deadline = time.monotonic() + budget
        snapshot = {}
        while True:
            frames = _page_frames(self._page)
            candidates = []
            for frame_index, frame in enumerate(frames):
                try:
                    items = frame.locator("input[type=file]")
                    total = items.count()
                except Exception:
                    continue
                for index in range(total):
                    node = items.nth(index)
                    try:
                        accept = str(node.get_attribute("accept") or "").lower()
                    except Exception:
                        continue
                    if "video" not in accept:
                        continue
                    try:
                        if not node.is_enabled():
                            continue
                    except Exception:
                        continue
                    try:
                        box = node.bounding_box()
                    except Exception:
                        box = None
                    visible = bool(box and box.get("width", 0) > 0
                                   and box.get("height", 0) > 0)
                    candidates.append({"frame": frame_index, "index": index,
                                       "node": node, "visible": visible,
                                       "accept": accept[:48]})
            snapshot = {
                "frames": len(frames),
                "candidates": [{"frame": item["frame"],
                                "index": item["index"],
                                "visible": item["visible"],
                                "accept": item["accept"]}
                               for item in candidates],
            }
            if len(candidates) == 1:
                self._progress("upload.input.scanned", total=1,
                               picked=candidates[0]["index"])
                self._upload_input_pick = {"frame": candidates[0]["frame"],
                                           "index": candidates[0]["index"]}
                return candidates[0]["node"], "input[type=file]", snapshot
            if len(candidates) > 1:
                visible = [item for item in candidates if item["visible"]]
                if len(visible) == 1:
                    self._progress("upload.input.scanned",
                                   total=len(candidates),
                                   picked=visible[0]["index"],
                                   visible_only=True)
                    self._upload_input_pick = {"frame": visible[0]["frame"],
                                               "index": visible[0]["index"]}
                    return visible[0]["node"], "input[type=file]", snapshot
                self._progress("upload.input.ambiguous", **snapshot)
                self._fail("\u9875\u9762\u4e0a\u6709\u591a\u4e2a\u53ef\u7528\u7684"
                           "\u89c6\u9891\u4e0a\u4f20\u6846\uff0c\u4e0d\u731c\u662f"
                           "\u54ea\u4e00\u4e2a\uff1b\u8fd9\u6b21\u6ca1\u4e0a\u4f20"
                           "\u6210\u529f\uff0c\u5df2\u81ea\u52a8\u6539\u5230\u51e0"
                           "\u5206\u949f\u540e\u91cd\u53d1\u3002", step="upload")
            if stop_event is not None and stop_event.is_set():
                self._fail("\u4e0a\u4f20\u6b65\u9aa4\u5df2\u88ab\u53d6\u6d88",
                           step="upload")
            if time.monotonic() >= deadline:
                self._progress("upload.input.none_usable", **snapshot)
                self._fail("\u9875\u9762\u7684\u89c6\u9891\u4e0a\u4f20\u6846\u8fd8"
                           "\u6ca1\u5c31\u7eea\uff08\u5f53\u4e0b\u4e0d\u53ef\u7528\uff09"
                           "\uff0c\u8fd9\u6b21\u6ca1\u4e0a\u4f20\u6210\u529f\uff0c"
                           "\u5df2\u81ea\u52a8\u6539\u5230\u51e0\u5206\u949f\u540e"
                           "\u91cd\u53d1\u3002", step="upload")
            # wait-ok: poll
            time.sleep(0.5)

    @staticmethod
    def _cdp_find_node_id(node, marker):
        """Walk a CDP node tree (shadow roots + iframes) for our marker."""
        attrs = (node or {}).get("attributes") or []
        for pos in range(0, len(attrs) - 1, 2):
            if attrs[pos] == "data-hl-upload" and attrs[pos + 1] == marker:
                return node.get("nodeId")
        for key in ("children", "shadowRoots"):
            for child in (node or {}).get(key) or []:
                found = PlaywrightDriver._cdp_find_node_id(child, marker)
                if found:
                    return found
        document = (node or {}).get("contentDocument")
        if document:
            return PlaywrightDriver._cdp_find_node_id(document, marker)
        return None

    def _set_input_files_cdp(self, locator, media):
        """Attach a local file through CDP so size is not capped at 50MB.

        ``locator.set_input_files`` streams the whole payload through the
        playwright driver, which refuses anything above 50MB when the browser
        is not co-located ("Cannot transfer files larger than 50Mb").  The
        browser runs on this same machine, so CDP ``DOM.setFileInputFiles``
        can hand it the path directly and Chrome reads the file itself.

        The node is located by tagging the element through the same locator
        playwright already resolved, then finding that marker in the pierced
        CDP tree -- no selector re-query, so shadow DOM and iframes are safe.
        """
        pick = getattr(self, "_upload_input_pick", None) or {}
        frames = _page_frames(self._page)
        frame_index = int(pick.get("frame") or 0)
        if frame_index >= len(frames):
            raise RuntimeError("cdp_frame_missing")
        frame = frames[frame_index]
        marker = "hl%d" % int(time.time() * 1000)
        handle = locator.element_handle()
        if handle is None:
            raise RuntimeError("cdp_handle_missing")
        session = None
        try:
            handle.evaluate(
                "(el, m) => el.setAttribute('data-hl-upload', m)", marker)
            session = self._page.context.new_cdp_session(frame)
            session.send("DOM.enable")
            doc = session.send("DOM.getDocument", {"depth": -1,
                                                  "pierce": True})
            node_id = self._cdp_find_node_id((doc or {}).get("root"), marker)
            if not node_id:
                raise RuntimeError("cdp_input_missing")
            session.send("DOM.setFileInputFiles",
                         {"files": [os.path.abspath(str(media))],
                          "nodeId": node_id})
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass
            try:
                handle.evaluate(
                    "(el) => el.removeAttribute('data-hl-upload')")
            except Exception:
                pass

    def _upload_navigation_timeout_seconds(self):
        settings = getattr(self, "settings", None) or {}
        raw = settings.get("upload_navigation_timeout_seconds")
        if raw is None:
            return min(45.0, self._upload_timeout_seconds())
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 45.0
        return min(120.0, max(0.5, value))

    def _fail(self, reason, *, step="publish", code=None):
        reason = _redact_log_text(reason)
        extra = {"code": code}
        # Hold first: page/evidence reads may fail exactly when the page closed.
        self._apply_hold(reason, step, extra)
        try:
            self._record(step, note=reason, error=reason, extra=extra)
        except Exception:
            pass
        raise GateRed(reason)

    @staticmethod
    def _classify_hold_kind(reason, step, extra, kind=None):
        """Resolve the fault kind, feeding page evidence into the classifier.

        ``_fail`` callers often keep the page text (which is what actually
        proves a captcha / SMS / risk-control stop) inside ``extra``; the
        classifier must see it or every such stop degrades to
        ``publish_unknown``.
        """
        if kind:
            return kind
        evidence = {"reason": str(reason), "step": step}
        for source in (extra, (extra or {}).get("extra") if isinstance(extra, dict) else None):
            if not isinstance(source, dict):
                continue
            for key in ("page_text", "html", "console", "body_text", "url", "error"):
                value = source.get(key)
                if value and key not in evidence:
                    evidence[key] = value
        try:
            return classify_fault(evidence) or PUBLISH_UNKNOWN
        except Exception:
            return PUBLISH_UNKNOWN

    def _apply_hold(self, reason, step, extra, *, kind=None):
        kind = self._classify_hold_kind(reason, step, extra, kind)
        if getattr(self, "_pre_submit", False) and kind not in PRE_SUBMIT_FREEZE_KINDS:
            # Before the publish click nothing was submitted, so an ordinary
            # fill failure only recycles its ledger reservation and leaves the
            # channel open.  Account-level stops are the exception above: they
            # must hold the channel even though the reservation is recycled.
            return
        if (getattr(self, "_hold_store", None) is None
                or getattr(getattr(self, "channel", None), "channel_id", None) is None):
            return
        # Persist before any page query/evidence collection.
        self.last_hold_result = self._hold_store.persist_failure(
            getattr(self.channel, "channel_id"), kind,
            {"reason": str(reason), "step": step, "extra": extra or {}},
            reason=str(reason))

    def freeze_unknown(self, reason, *, step="publish_unknown"):
        """Persist a hold for any uncertain post-click result.

        ``run_publish`` calls this for every exception after SUBMIT_INTENT and
        the driver calls it from every explicit UNKNOWN branch.
        """
        reason = str(reason or "publish result UNKNOWN")
        try:
            self._apply_hold(reason, step, {"code": "UNKNOWN"},
                             kind=PUBLISH_UNKNOWN)
        except Exception:
            pass
        try:
            self._record(step, note=reason, error=reason,
                         extra={"code": "UNKNOWN"})
        except Exception:
            pass

    def _log_action(self, label, record):
        if self.layout is not None:
            from .jsonio import append_jsonl
            attempt_id = self.upload_attempt.attempt_id if self.upload_attempt else "pre-upload"
            append_jsonl(self.layout.evidence / "actions" / f"{attempt_id}.jsonl",
                         {**record, "env_id": getattr(self.env, "env_id", None),
                          "attempt_id": attempt_id})

    def check_hold(self):
        channel_id = getattr(self.channel, "channel_id", None) or self.plan.get("channel_id")
        if self._hold_store is not None:
            hold = self._hold_store.get(channel_id)
            if hold:
                raise GateRed("\u53d1\u5e03\u5df2\u51bb\u7ed3\uff0c\u987b\u4eba\u5de5\u5904\u7406\u5e76\u663e\u5f0f\u89e3\u9664\uff1a" + str(hold.get("reason")))

    def _visible_locator(self, selectors, timeout=30.0, *, root=None):
        return self._unique_locator(selectors, timeout=timeout, root=root)

    def _unique_locator(self, selectors, timeout=30.0, *, visible=True,
                        root=None):
        # CSS selector lists are a UNION of DOM elements (one node matching two
        # selectors is counted once). Never select .first from separate matches.
        selector = ", ".join(selectors)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            matches = (root.locator(selector) if root is not None
                       else self._page.locator(selector))
            candidates = [matches.nth(i) for i in range(matches.count())
                          if not visible or matches.nth(i).is_visible()]
            if len(candidates) == 1:
                return candidates[0], selector
            if len(candidates) > 1:
                raise GateRed(f"AMBIGUOUS:{len(candidates)} elements: {selector}")
            if time.monotonic() >= deadline:
                return None, "timeout: " + selector
            self.humanizer.poll_pause()

    def _visible_candidates(self, selectors, timeout=30.0, *, root=None):
        deadline = time.monotonic() + max(0.0, timeout)
        selector = ", ".join(selectors)
        while True:
            out = []
            scan_complete = True
            try:
                locators = (root.locator(selector) if root is not None
                            else self._page.locator(selector))
                count = locators.count()
                for index in range(count):
                    item = locators.nth(index)
                    if item.is_visible():
                        out.append((item, selector, index))
            except Exception:
                scan_complete = False
            self._last_visible_scan_complete = scan_complete
            if out:
                return out
            # A zero timeout means "scan exactly once", never "skip the DOM".
            if time.monotonic() >= deadline:
                return out
            self.humanizer.poll_pause()

    def _click_first_visible(self, selectors, *, reason, timeout=2.0, root=None):
        candidates = self._visible_candidates(selectors, timeout=timeout, root=root)
        if not getattr(self, "_last_visible_scan_complete", False):
            self._fail("\u9a8c\u8bc1\u6309\u94ae\u67e5\u8be2\u4e0d\u5b8c\u6574\uff1b\u7ed3\u679c UNKNOWN\uff0c\u672a\u6267\u884c\u70b9\u51fb", step="verification_unknown")
        if not candidates:
            return False
        locator, selector, _index = candidates[0]
        result = self._click_with_retries(locator, reason=reason)
        return bool(result.get("ok"))

    def _overlay_container(self, locator):
        handle = locator.evaluate_handle("""el => {
          for(let n=el.parentElement; n && n!==document.body; n=n.parentElement){
            if(n.matches("[role='dialog'],[aria-modal='true'],dialog") ||
               ['fixed','absolute'].includes(getComputedStyle(n).position)) return n;
          }
          return null;
        }""")
        element = handle.as_element()
        if element is None:
            raise VerificationQueryError("cannot bind dismiss button to overlay")
        return element

    def _click_with_retries(self, locator, *, reason, before_click=None):
        """At most two attempts on the same locator; never force or switch paths."""
        for attempt in range(2):
            receipt = getattr(self, "_publish_receipt", None)
            if attempt and before_click and receipt is not None:
                # The previous click may have landed even when the observer saw
                # no request.  Never start a second click over a visible success
                # signal; only a pending result may use the retry.
                outcome = self._detect_publish_outcome()
                if outcome.get("result") == "published":
                    self._remember_publish_outcome(outcome)
                    return {"ok": True, "published": True,
                            "signal": outcome.get("signal"),
                            "work_id": outcome.get("work_id")}
                if outcome.get("result") == "failed":
                    self._fail("\u53d1\u5e03\u5931\u8d25\uff1a" + str(outcome.get("reason")),
                               step="publish")
            if before_click and receipt is not None:
                # A dispatched create_v2 is never sent again.  Give the
                # platform a bounded chance to show success before freezing.
                if receipt.requests or receipt.network:
                    outcome = self._wait_for_publish_success()
                    if outcome and outcome.get("result") == "published":
                        return {"ok": True, "published": True,
                                "signal": outcome.get("signal"),
                                "work_id": outcome.get("work_id")}
                    if outcome and outcome.get("result") == "failed":
                        self._fail("\u53d1\u5e03\u5931\u8d25\uff1a" + str(outcome.get("reason")),
                                   step="publish")
                    self._fail("\u63d0\u4ea4\u8bf7\u6c42\u5df2\u53d1\u51fa\uff1b\u7b49\u5f85\u6210\u529f\u4fe1\u53f7\u8d85\u65f6\uff0c\u7ed3\u679c UNKNOWN\uff0c\u7981\u6b62\u91cd\u53d1",
                               step="publish_unknown")
            callback_failed = False
            def authorized_click():
                nonlocal callback_failed
                try:
                    before_click()
                except BaseException:
                    callback_failed = True
                    raise
            try:
                kwargs = {"reason": reason}
                if before_click is not None:
                    kwargs["before_click"] = authorized_click
                result = self.humanizer.click(self._page, locator, **kwargs)
            except GateRed:
                raise
            except Exception as exc:
                if callback_failed:
                    raise
                result = {"ok": False, "reason": str(exc)}
            if result.get("ok"):
                return result
            self._record("click", note=f"{reason} \u70b9\u51fb\u5931\u8d25/\u5173\u95ed\u5931\u8d25",
                         extra={"attempt": attempt + 1, "limit": 2,
                                "reason": str(result.get("reason"))})
        self._click_failed = True
        self._fail(f"{reason} \u70b9\u51fb\u4e24\u6b21\u4ecd\u5931\u8d25\uff1b\u7ed3\u679c UNKNOWN\uff0c\u5df2\u51bb\u7ed3", step="publish_unknown")

    def _dismiss_page_overlays(self, *, retries=2):
        """Only close confirmed known onboarding dialogs before interaction.

        The compatibility argument cannot raise the two-click limit. Unknown
        geometry and residual masks are left to normal click actionability.
        """
        dismissed = []
        for selectors, label in ((ONBOARDING_DONE_SELECTORS, "\u53d1\u6587\u52a9\u624b\u5b8c\u6210"),
                                 (VIDEO_PREVIEW_OK_SELECTORS, "\u89c6\u9891\u9884\u89c8\u8bf4\u660e\u6211\u77e5\u9053\u4e86"),
                                 (COVER_HINT_DISMISS_SELECTORS, "\u6a2a\u5c01\u9762\u5f15\u5bfc\u6682\u4e0d\u8bbe\u7f6e")):
            candidates = self._visible_candidates(selectors, timeout=0)
            if not getattr(self, "_last_visible_scan_complete", False):
                self._fail("\u9875\u9762\u8fde\u63a5/\u67e5\u8be2\u5931\u8d25\uff1b\u7ed3\u679c UNKNOWN", step="publish_unknown")
            for locator, _selector, _index in candidates:
                try:
                    self._overlay_container(locator)
                except VerificationQueryError:
                    # Same text outside a known dialog is not a close control.
                    continue
                self._click_with_retries(locator, reason=label)
                dismissed.append(label)
                self.humanizer.step_pause(reason=label)
        if dismissed:
            self._record("overlay", note="\u5df2\u70b9\u51fb\u5df2\u77e5\u9875\u9762\u5f15\u5bfc\u5173\u95ed\u6309\u94ae",
                         extra={"dismissed": dismissed})
        return dismissed

    def _verification_containers(self, timeout=0.5):
        """Keep stable handles for explicit dialog containers; never infer from CSS."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                handles = self._page.locator(", ".join(VERIFICATION_CONTAINER_SELECTORS)).element_handles()
                visible = [h for h in handles if h.is_visible()]
                # Nested wrappers represent one modal; separate modals remain ambiguous.
                roots = [h for h in visible if not any(
                    other != h and other.evaluate("(el, child) => el.contains(child)", h)
                    for other in visible)]
                if roots:
                    return [(_ElementScope(h), "structural_modal", i) for i, h in enumerate(roots)]
            except Exception as exc:
                raise VerificationQueryError(str(exc)) from exc
            if time.monotonic() >= deadline:
                return []
            self.humanizer.poll_pause()

    def _sms_panel_visible(self, timeout=0.5):
        """Compatibility probe: True only for a structured verification popup."""
        return bool(self._verification_containers(timeout=timeout))

    def _sms_code_path(self):
        path = getattr(self, "sms_code_file", None)
        if path:
            return Path(str(path))
        if self.layout is not None:
            return self.layout.run / "publish_sms_code.txt"
        return None

    def _read_sms_code(self):
        path = self._sms_code_path()
        if path is None:
            self._fail("\u53d1\u5e03\u89e6\u53d1\u77ed\u4fe1\u4e8c\u6b21\u9a8c\u8bc1\uff0c\u4f46\u6ca1\u6709\u53ef\u7528\u7684\u9a8c\u8bc1\u7801\u6587\u4ef6\u8def\u5f84")
        wait_seconds = getattr(self, "sms_wait_seconds", None)
        try:
            wait_seconds = int(wait_seconds)
        except (TypeError, ValueError):
            wait_seconds = None
        if wait_seconds is None or wait_seconds <= 0:
            try:
                wait_seconds = int(self.settings.get("sms_wait_seconds", 180))
            except (TypeError, ValueError, AttributeError):
                wait_seconds = 180
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if path.exists():
                try:
                    raw = path.read_text(encoding="utf-8-sig").strip()
                except OSError:
                    raw = ""
                code = re.sub(r"\s+", "", raw)
                if re.fullmatch(r"\d{4,8}", code or ""):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    return code, str(path)
            if not hasattr(self, "_sms_code_wait_announced"):
                self._sms_code_wait_announced = True
                self._record("sms_verification",
                             note="\u7b49\u5f85\u5916\u90e8\u63d0\u4f9b\u77ed\u4fe1\u9a8c\u8bc1\u7801",
                             extra={"sms_code_file": str(path),
                                    "wait_seconds": wait_seconds})
            self.humanizer.poll_pause()
        self._fail(f"\u77ed\u4fe1\u4e8c\u6b21\u9a8c\u8bc1\u7801\u672a\u5728 {wait_seconds} \u79d2\u5185\u5199\u5165 {path}\uff1b\u8fd9\u662f\u660e\u786e\u7684\u4eba\u5de5\u4ecb\u5165\u70b9\uff0c\u672a\u5047\u88c5\u5168\u81ea\u52a8")

    def _fill_sms_code(self, code, *, root=None):
        locator = None
        try:
            locator, _ = self._unique_locator(SMS_CODE_INPUT_SELECTORS,
                                              timeout=5.0, root=root)
        except GateRed:
            locator = None
        if locator is None:
            locator = self._fallback_sms_input(root=root)
        if locator is None:
            self._fail("\u627e\u4e0d\u5230\u552f\u4e00\u77ed\u4fe1\u9a8c\u8bc1\u7801\u8f93\u5165\u6846\uff1b\u62d2\u7edd\u731c\u6d4b")
        result = self._click_with_retries(locator, reason="\u805a\u7126\u77ed\u4fe1\u9a8c\u8bc1\u7801\u8f93\u5165\u6846")
        if not result.get("ok"):
            self._fail("\u805a\u7126\u77ed\u4fe1\u9a8c\u8bc1\u7801\u8f93\u5165\u6846\u5931\u8d25")
        self._page.keyboard.press("Control+A")
        self._page.keyboard.press("Backspace")
        self.humanizer.type_into(self._page, code)

    def _fallback_sms_input(self, *, root=None):
        """Look for exactly one visible input whose placeholder/aria-label mentions \u9a8c\u8bc1\u7801.

        This is a runtime DOM inspection, not a fuzzy CSS selector, and it is used
        only after the evidence-backed exact selectors miss.
        """
        try:
            scope = root if root is not None else self._page
            items = scope.locator("input").evaluate_all("""els => els
              .filter(el => {
                if (!el.offsetParent) return false;
                const label = ((el.getAttribute('placeholder') || '') + ' ' +
                               (el.getAttribute('aria-label') || ''));
                return label.includes('\u9a8c\u8bc1\u7801');
              })
              .map(el => ({placeholder: el.getAttribute('placeholder'),
                            ariaLabel: el.getAttribute('aria-label'),
                            index: els.indexOf(el)}))""")
        except Exception:
            items = []
        visible = [it for it in (items or []) if it]
        if len(visible) != 1:
            return None
        return scope.locator("input").nth(visible[0]["index"])

    def _verification_seconds(self, attr, key, default):
        value = getattr(self, attr, None)
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = None
        if value is None or value <= 0:
            try:
                value = int(self.settings.get(key, default))
            except (TypeError, ValueError, AttributeError):
                value = default
        return max(1, value)

    def _verification_signals(self, panel):
        """Collect every verification signal inside one already-chosen container."""
        try:
            return panel.evaluate("""el => {
              const visible = (node) => {
                const rect = node.getBoundingClientRect();
                const style = window.getComputedStyle(node);
                return !!(rect.width > 0 && rect.height > 0 &&
                          style.display !== 'none' &&
                          style.visibility !== 'hidden' &&
                          style.opacity !== '0');
              };
              const labelOf = (node) => [
                node.getAttribute('placeholder') || '',
                node.getAttribute('aria-label') || '',
                node.getAttribute('title') || '',
                node.getAttribute('name') || ''
              ].join(' ');
              const smsInput = (node) => {
                if (node.disabled) return false;
                if ((node.getAttribute('type') || '').toLowerCase() === 'hidden') return false;
                return labelOf(node).includes('\u9a8c\u8bc1\u7801');
              };
              const inputs = Array.from(el.querySelectorAll('input')).filter(visible);
              const sms = inputs.filter(smsInput);
              const buttonText = (node) => (node.innerText || '').trim();
              const getButtons = Array.from(el.querySelectorAll("button,[role='button']"))
                .filter(visible).filter(node => /\u83b7\u53d6\u9a8c\u8bc1\u7801/.test(buttonText(node)));
              const verifyButtons = Array.from(el.querySelectorAll("button,[role='button']"))
                .filter(visible).filter(node => /^(\u9a8c\u8bc1|\u786e\u8ba4|\u786e\u5b9a)$/.test(buttonText(node)));
              const semanticText = (el.innerText || '') + ' ' +
                (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '');
              const qrText = /(\u4e8c\u7ef4\u7801|\u626b\u7801|\u626b\u4e00\u626b|\u626b\u63cf)/.test(semanticText);
              const dedicatedQr = !!el.querySelector(
                "[aria-label*='\u4e8c\u7ef4\u7801'],[aria-label*='\u626b\u7801'],[data-e2e*='qr'],[data-testid*='qr']");
              const canvases = Array.from(el.querySelectorAll('canvas')).filter(visible);
              const images = Array.from(el.querySelectorAll('img')).filter(visible)
                .filter(node => {
                  const marker = ((node.getAttribute('alt') || '') + ' ' +
                                  (node.getAttribute('title') || '') + ' ' +
                                  (node.getAttribute('src') || '')).toLowerCase();
                  return /(qr|\u4e8c\u7ef4\u7801|\u626b\u7801)/.test(marker) || qrText || dedicatedQr;
                });
              return {container: true, container_tag: (el.tagName || '').toLowerCase(),
                      sms_inputs: sms.length, sms_get_buttons: getButtons.length,
                      sms_verify_buttons: verifyButtons.length,
                      qr_canvas: canvases.length, qr_images: images.length,
                      qr_semantic: !!(qrText || dedicatedQr), scope: 'container'};
            }""")
        except Exception as exc:
            raise VerificationQueryError(str(exc)) from exc

    def _detect_verification_method(self):
        candidates = self._verification_containers(timeout=0.5)
        if len(candidates) != 1:
            return None, ({"container_count": len(candidates), "ambiguous": True}
                          if candidates else None)
        panel, _selector, _index = candidates[0]
        signals = self._verification_signals(panel)
        signals["container_count"] = 1
        self._verification_root = panel if signals else None
        return verification_method_from_signals(signals), signals

    def _wait_verification_method(self, timeout=None):
        seconds = (self._verification_seconds(
            "verification_detect_seconds", "verification_detect_seconds",
            DEFAULT_VERIFICATION_DETECT_SECONDS)
            if timeout is None else max(1, int(timeout)))
        deadline = time.monotonic() + seconds
        announced = False
        signals = None
        while True:
            method, signals = self._detect_verification_method()
            if method is not None:
                return method, signals
            if time.monotonic() >= deadline:
                return None, signals
            if not announced:
                announced = True
                self._record("verification",
                             note="\u672c\u4eba\u9a8c\u8bc1\u5f39\u5c42\u5df2\u51fa\u73b0\uff0c\u6b63\u5728\u8bc6\u522b\u77ed\u4fe1\u8f93\u5165\u6846\u6216\u4e8c\u7ef4\u7801",
                             extra={"detect_seconds": seconds})
            self.humanizer.poll_pause()

    def _run_sms_verification(self, root=None):
        """Existing SMS path: the code is external and never fetched/cached."""
        root = root if root is not None else getattr(self, "_verification_root", None)
        self._record("sms_verification", note="\u68c0\u6d4b\u5230\u53d1\u5e03\u77ed\u4fe1\u4e8c\u6b21\u9a8c\u8bc1\uff1a\u4e3a\u786e\u4fdd\u662f\u672c\u4eba\u64cd\u4f5c")
        if not self._click_first_visible(SMS_GET_CODE_SELECTORS,
                                         reason="\u70b9\u51fb\u83b7\u53d6\u9a8c\u8bc1\u7801", timeout=2.0,
                                         root=root):
            self._fail("\u70b9\u51fb\u300c\u83b7\u53d6\u9a8c\u8bc1\u7801\u300d\u5931\u8d25\uff1b\u4e0d\u7ee7\u7eed\u7b49\u5f85\u4e00\u4e2a\u672a\u53d1\u9001\u7684\u9a8c\u8bc1\u7801",
                       step="sms_verification")
        code, code_path = self._read_sms_code()
        self._fill_sms_code(code, root=root)
        self._record("sms_verification",
                     note="\u5df2\u586b\u5165\u5916\u90e8\u77ed\u4fe1\u9a8c\u8bc1\u7801\uff0c\u51c6\u5907\u70b9\u51fb\u9a8c\u8bc1",
                     extra={"sms_code_length": len(code), "sms_code_file": code_path})
        self._click_text_or_selector(SMS_VERIFY_BUTTON_SELECTORS,
                                     reason="\u63d0\u4ea4\u77ed\u4fe1\u9a8c\u8bc1\u7801", root=root)
        self.humanizer.step_pause(reason="\u77ed\u4fe1\u9a8c\u8bc1\u63d0\u4ea4")

    def _verification_failure_reason(self, root=None):
        texts = []
        if root is not None:
            try:
                texts.append(str(root.evaluate(VISIBLE_TEXT_SCRIPT) or ""))
            except Exception as exc:
                raise VerificationQueryError(str(exc)) from exc
        try:
            texts.append(str(self._page.locator("body").evaluate(VISIBLE_TEXT_SCRIPT, timeout=1000) or ""))
        except Exception as exc:
            raise VerificationQueryError(str(exc)) from exc
        blob = "\n".join(texts)
        for marker in VERIFICATION_FAILURE_MARKERS:
            if marker in blob:
                return marker
        return None

    def _wait_verification_container_cleared(self, wait_seconds, *, step):
        """Compatibility name: completion requires a receipt, not a cleared dialog."""
        deadline = time.monotonic() + max(0, float(wait_seconds))
        while True:
            outcome = self._detect_publish_outcome()
            if outcome.get("result") == "published":
                self._remember_publish_outcome(outcome)
                return True
            if outcome.get("result") == "failed":
                self._remember_publish_outcome(outcome)
                self._fail("\u53d1\u5e03\u5931\u8d25\uff1a" + str(outcome.get("reason")),
                           step="publish")
            self._check_publish_observation()
            receipt = getattr(self, "_publish_receipt", None)
            if receipt and self._receipt_proof_with_fallback(receipt):
                self._check_publish_observation()
                return True
            if time.monotonic() >= deadline:
                self._fail("\u7b49\u5f85\u672c\u6b21\u5e73\u53f0\u56de\u6267\u8d85\u65f6\uff1b\u7ed3\u679c UNKNOWN\uff0c\u5df2\u51bb\u7ed3\uff0c\u672a\u91cd\u53d1",
                           step="verification_unknown")
            self.humanizer.poll_pause()

    def _wait_qr_verification(self):
        """Wait for the owner's scan and platform receipt; never infer from the UI."""
        return self._wait_verification_container_cleared(
            self._verification_seconds("qr_wait_seconds", "qr_wait_seconds",
                                       DEFAULT_QR_WAIT_SECONDS), step="qr_verification")

    def _publish_success_timeout(self):
        raw = (getattr(self, "settings", None) or {}).get(
            "publish_success_timeout_seconds", DEFAULT_PUBLISH_SUCCESS_WAIT_SECONDS)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = DEFAULT_PUBLISH_SUCCESS_WAIT_SECONDS
        return max(0.0, min(value, 120.0))

    def _safe_page_url(self):
        try:
            return str(self._raw_page_url() or "")
        except Exception:
            return ""

    def _safe_visible_text(self):
        page = getattr(self, "_page", None)
        if page is None:
            return ""
        try:
            locator = page.locator("body")
            try:
                return str(locator.evaluate(VISIBLE_TEXT_SCRIPT, timeout=1000) or "")
            except TypeError:
                return str(locator.inner_text(timeout=1000) or "")
        except Exception:
            return ""

    @staticmethod
    def _first_marker(text, markers):
        blob = str(text or "")
        for marker in markers:
            if marker in blob:
                return marker
        return None

    @staticmethod
    def _marker_counts(text, markers):
        blob = str(text or "")
        return {marker: blob.count(marker) for marker in markers}

    @classmethod
    def _new_marker(cls, text, markers, before_counts):
        """Return a marker whose occurrence count increased after the click."""
        blob = str(text or "")
        before = before_counts or {}
        for marker in markers:
            if blob.count(marker) > int(before.get(marker, 0)):
                return marker
        return None

    def _manage_url_hit(self):
        """Only a post-click navigation to this deployment's manage URL counts."""
        current = self._safe_page_url()
        if not current:
            return False
        try:
            cur = urlparse(current)
            manage = urlparse(DOUYIN_MANAGE_URL)
        except Exception:
            return False
        if not cur.path or not manage.path:
            return False
        if cur.netloc and manage.netloc and cur.netloc != manage.netloc:
            return False
        current_path = cur.path.rstrip("/")
        manage_path = manage.path.rstrip("/")
        if not (current_path == manage_path or current_path.startswith(manage_path + "/")):
            return False
        start = getattr(self, "_publish_start_url", None)
        # URL is only evidence when this attempt recorded the pre-click page.
        # A direct readback on an already-open manage page is not a transition.
        if not start:
            return False
        try:
            if urlparse(str(start)).path.rstrip("/") == current_path:
                return False
        except Exception:
            pass
        return True

    @staticmethod
    def _work_id_from_url(url):
        try:
            query = parse_qs(urlparse(str(url or "")).query)
        except Exception:
            return None
        for key in ("mid", "item_id", "aweme_id", "work_id"):
            for value in query.get(key, []):
                if is_ascii_mid(value):
                    return value
        return None

    @staticmethod
    def _receipt_explicit_failure(receipt):
        fault = str(getattr(receipt, "fault", None) or "")
        if not fault:
            return None
        if fault.startswith("SUBMIT_HTTP_"):
            return fault
        # A parsed 200 body with a non-zero/missing business code is an explicit
        # platform rejection, not an unknown transport result.
        if "requires status_code=0" in fault:
            return fault
        return None

    @staticmethod
    def _receipt_binding_failure(receipt):
        fault = str(getattr(receipt, "fault", None) or "")
        return fault if fault.startswith("REQUEST_BINDING_FAILED") else None

    def _fresh_page_marker(self, text, markers, counts_attr):
        """A DOM marker is evidence only if it appeared after observation started."""
        if getattr(self, "_publish_observation_started_at", None) is None:
            return None
        before = getattr(self, counts_attr, None)
        if before is None:
            return None
        return self._new_marker(text, markers, before)

    def _detect_publish_outcome(self):
        """Return one of published / failed / pending for the current instant.

        Strongest-first order: bound create_v2 success (with or without a work
        ID), explicit create_v2 failure, request-binding failure, explicit page
        failure, ID-less create_v2 success, fresh success toast, manage-page
        navigation.  A page failure outranks an ID-less API success.  A binding
        failure stays UNKNOWN and cannot be overwritten by a later page signal.
        """
        receipt = getattr(self, "_publish_receipt", None)
        receipt_fault = None
        if receipt is not None:
            try:
                proof = self._receipt_proof(receipt)
            except Exception:
                proof = None
            if proof:
                return {"result": "published", "signal": "create_v2",
                        "work_id": proof.get("work_id"), "receipt": proof}
            failure = self._receipt_explicit_failure(receipt)
            if failure:
                return {"result": "failed", "signal": "create_v2_error",
                        "reason": failure, "work_id": None}
            binding_failure = self._receipt_binding_failure(receipt)
            if binding_failure:
                return {"result": "pending", "signal": "request_binding_failed",
                        "reason": binding_failure}

        text = self._safe_visible_text()
        failure_marker = self._fresh_page_marker(
            text, PUBLISH_FAILURE_MARKERS, "_publish_start_failure_counts")
        if failure_marker:
            return {"result": "failed", "signal": "page_failure",
                    "reason": failure_marker, "work_id": None}

        if receipt is not None:
            success = None
            observe = getattr(receipt, "success_without_id", None)
            if callable(observe):
                try:
                    success = observe()
                except Exception:
                    success = None
            if success:
                return {"result": "published", "signal": "create_v2_status_ok",
                        "work_id": None, "receipt": None,
                        "observation": success}
            if getattr(receipt, "fault", None):
                # A transport/body fault is not a platform failure and must
                # not hide a later explicit page success.  Page signals are
                # evaluated below before this is reported as still pending.
                receipt_fault = str(receipt.fault)

        success_marker = self._fresh_page_marker(
            text, PUBLISH_SUCCESS_MARKERS, "_publish_start_success_counts")
        url = self._safe_page_url()
        if success_marker:
            return {"result": "published", "signal": "success_toast",
                    "work_id": self._work_id_from_url(url), "receipt": None}
        if self._manage_url_hit():
            return {"result": "published", "signal": "manage_url",
                    "work_id": self._work_id_from_url(url), "receipt": None}
        if receipt_fault:
            return {"result": "pending", "signal": "receipt_fault",
                    "reason": receipt_fault}
        return {"result": "pending", "signal": None}

    def _remember_publish_outcome(self, outcome):
        if not outcome:
            return
        previous = getattr(self, "_publish_outcome", None)
        self._publish_outcome = outcome
        if previous and previous.get("result") == outcome.get("result") and \
                previous.get("signal") == outcome.get("signal"):
            return
        if outcome.get("result") == "published":
            self._safe_record(
                "publish",
                note="\u5df2\u8bc6\u522b\u53d1\u5e03\u6210\u529f\u4fe1\u53f7\uff1a" + str(outcome.get("signal")),
                extra={"signal": outcome.get("signal"),
                       "work_id": outcome.get("work_id"),
                       "receipt_bound": bool(outcome.get("receipt"))})
        elif outcome.get("result") == "failed":
            self.last_publish_evidence = {
                "ok": False, "result": "FAILED",
                "signal": outcome.get("signal"),
                "reason": outcome.get("reason"), "receipt": None}
            self._last_publish_evidence = self.last_publish_evidence
            self._safe_record(
                "publish", note="\u5df2\u8bc6\u522b\u660e\u786e\u5931\u8d25\u4fe1\u53f7\uff1a" + str(outcome.get("reason")),
                error=str(outcome.get("reason")),
                extra={"signal": outcome.get("signal"), "result": "FAILED"})

    def _wait_for_publish_success(self, timeout=None):
        """Poll bounded success/failure signals; None means neither appeared."""
        seconds = self._publish_success_timeout() if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + seconds
        while True:
            outcome = self._detect_publish_outcome()
            if outcome.get("result") in ("published", "failed"):
                self._remember_publish_outcome(outcome)
                return outcome
            if time.monotonic() >= deadline:
                return None
            pause = getattr(getattr(self, "humanizer", None), "poll_pause", None)
            if callable(pause):
                pause()

    def _wait_post_publish_state(self):
        """Wait for a receipt, or offer a recognised verification interaction."""
        seconds = float(getattr(self, "settings", {}).get(
            "publish_receipt_timeout_seconds", DEFAULT_POST_PUBLISH_WAIT_SECONDS))
        deadline = time.monotonic() + seconds
        while True:
            outcome = self._detect_publish_outcome()
            if outcome.get("result") == "published":
                self._remember_publish_outcome(outcome)
                return "published", None, outcome
            if outcome.get("result") == "failed":
                self._remember_publish_outcome(outcome)
                return "failed", None, outcome
            self._check_publish_observation()
            receipt = getattr(self, "_publish_receipt", None)
            if receipt and receipt.proof():
                self._check_publish_observation()
                return "manage", None, None  # historical return name, receipt only
            try:
                method, signals = self._detect_verification_method()
            except VerificationQueryError as exc:
                if not receipt:
                    self._fail(f"\u9875\u9762\u8fde\u63a5/\u67e5\u8be2\u5931\u8d25\uff1a{exc}", step="publish_unknown")
                receipt.observe_page_failure("DOM_QUERY_FAILED:" + str(exc))
                # Navigation may finish the response while destroying this query.
                # Re-check the transport/proof before interpreting any DOM result.
                self._check_publish_observation()
                if receipt.proof():
                    return "manage", None, None
                method, signals = None, None
            if method in (VERIFICATION_SMS, VERIFICATION_QR):
                return "verification", method, signals
            # Unknown/ambiguous dialogs do not select a path or veto a receipt.
            if receipt and self._receipt_proof_with_fallback(receipt):
                self._check_publish_observation()
                return "manage", None, None
            if time.monotonic() >= deadline:
                return "timeout", None, None
            self.humanizer.poll_pause()

    def _handle_publish_verification(self):
        """One recognised SMS/QR path; success remains the bound receipt."""
        state, method, signals = self._wait_post_publish_state()
        if state == "manage":
            return False
        if state == "published":
            return False
        if state == "failed":
            reason = (signals or {}).get("reason") if isinstance(signals, dict) else None
            self._fail("\u53d1\u5e03\u5931\u8d25\uff1a" + str(reason or "platform error"),
                       step="publish")
        if state == "timeout":
            self._fail("\u7b49\u5f85\u672c\u6b21\u5e73\u53f0\u56de\u6267\u8d85\u65f6\uff1b\u7ed3\u679c UNKNOWN\uff0c\u5df2\u51bb\u7ed3\uff0c\u672a\u91cd\u53d1", step="verification_unknown")
        if state != "verification" or method not in (VERIFICATION_SMS, VERIFICATION_QR):
            return self._require_manage_after_verification()
        if method == VERIFICATION_SMS:
            self._record("verification", note="\u8bc6\u522b\u9a8c\u8bc1\u65b9\u5f0f\uff1a\u77ed\u4fe1\u9a8c\u8bc1\u7801",
                         extra={"method": method, "signals": signals})
            self._run_sms_verification(root=self._verification_root)
            return self._require_manage_after_verification()
        self._record("qr_verification", note="\u7b49\u5f85\u672c\u4eba\u626b\u7801",
                     extra={"method": method, "signals": signals})
        print("\u7b49\u5f85\u672c\u4eba\u626b\u7801\uff1a\u8bf7\u5728\u624b\u673a\u6296\u97f3\u4e2d\u5b8c\u6210\u672c\u4eba\u9a8c\u8bc1\uff1b\u7a0b\u5e8f\u4e0d\u4f1a\u8bfb\u53d6\u6216\u63d0\u4ea4\u9a8c\u8bc1\u7801\u3002", flush=True)
        self._wait_qr_verification()
        return True

    def _require_manage_after_verification(self):
        """Compatibility name: no URL, quiet window or popup success requirement."""
        return self._wait_verification_container_cleared(
            float(getattr(self, "settings", {}).get(
                "publish_receipt_timeout_seconds", DEFAULT_POST_PUBLISH_WAIT_SECONDS)),
            step="publish_unknown")

    def _register_upload_request_listener(self):
        self._upload_request_ids = []
        self._pending_uploads = {}
        self._finished_upload_requests = []
        self._upload_response_objects = {}
        self._upload_bytes_pool = None
        self._upload_probe_events = []
        attempt = self.upload_attempt

        def on_request(request):
            method = str(getattr(request, "method", "") or "")
            if method not in ("POST", "PUT"):
                return
            # Event callbacks run on Playwright's single event thread.  Only
            # cheap, locally cached fields may be touched here; size/header
            # measurements happen later in the drain loop.
            self._pending_uploads[request] = str(id(request))

        def on_response(response):
            request = response.request
            rid = self._pending_uploads.get(request)
            if not rid:
                return
            try:
                status = int(response.status)
            except (TypeError, ValueError):
                status = 0
            try:
                method = str(getattr(request, "method", "") or "")
            except Exception:
                method = ""
            try:
                url = str(getattr(response, "url", "") or "")
            except Exception:
                url = ""
            record = {"request_id": rid, "status": status,
                      "method": method, "url": url,
                      # Body parsing is deferred and bounded.  In particular
                      # JSON decoding must never run in this callback.
                      "body_state": "pending" if 200 <= status < 300 else "not_read"}
            attempt.response_records.append(record)
            if record["body_state"] == "pending":
                self._upload_response_objects[id(record)] = response

        self._upload_request_handler = on_request

        def on_finished(request):
            rid = self._pending_uploads.get(request)
            if not rid:
                return
            # No sizes(), all_headers() or body/text call in the callback.
            self._finished_upload_requests.append(
                {"request": request, "request_id": rid})

        self._parse_upload_response = on_response
        self._upload_response_handler = on_finished
        self._page.on("request", on_request)
        self._page.on("response", on_response)
        self._page.on("requestfinished", on_finished)

    def _upload_response_body_timeout_seconds(self):
        raw = (getattr(self, "settings", None) or {}).get(
            "upload_response_body_timeout_seconds", 3.0)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 3.0
        return min(15.0, max(0.05, value))

    def _read_upload_response_body(self, record):
        """Read one candidate 2xx body after the fact, with a hard deadline.

        A browser may evict the response body while the upload page navigates.
        That is an expected, non-fatal state: completion is decided by page
        facts, not by whether a resource id can still be parsed.
        """
        if not isinstance(record, dict) or record.get("body_state") != "pending":
            return record
        rid = str(record.get("request_id") or "")
        refs = getattr(self, "_upload_response_objects", None) or {}
        response = refs.get(id(record))
        if response is None:
            response = refs.get(rid)
        if response is None:
            record["body_state"] = "body_evicted"
            return record

        timeout = self._upload_response_body_timeout_seconds()
        started = _MONOTONIC()
        try:
            impl = getattr(response, "_impl_obj", None)
            if impl is not None and hasattr(impl, "body"):
                raw = _bounded_sync_coroutine(
                    response, impl.body(), timeout)
            else:
                # Test doubles and older wrappers expose json() directly and
                # are not Playwright-greenlet bound, so a worker + join gives
                # the same hard deadline without crossing a Playwright thread.
                outcome = {}

                def worker():
                    try:
                        outcome["value"] = response.json()
                    except BaseException as exc:  # noqa: BLE001 - re-raised
                        outcome["error"] = exc

                thread = threading.Thread(
                    target=worker, name="upload-body-read", daemon=True)
                thread.start()
                thread.join(timeout)
                if thread.is_alive():
                    raise TimeoutError("response body read timed out")
                if "error" in outcome:
                    raise outcome["error"]
                parsed = outcome.get("value")
                raw = json.dumps(parsed).encode("utf-8")
            if isinstance(raw, (bytes, bytearray)):
                text = bytes(raw).decode("utf-8")
            elif isinstance(raw, str):
                text = raw
            else:
                raise ValueError("response body is not bytes or text")
            body = json.loads(text) if text.strip() else {}
            record["resource_ids"] = _extract_upload_resource_ids(body)
            record["body_state"] = "read"
        except TimeoutError as exc:
            record["body_state"] = "body_timeout"
            record["body_error"] = str(exc)[:300] or "timeout"
        except json.JSONDecodeError as exc:
            record["body_state"] = "body_parse_error"
            record["body_error"] = str(exc)[:300]
        except Exception as exc:
            message = str(exc)
            lowered = message.lower()
            if any(marker in lowered for marker in UPLOAD_BODY_EVICTED_MARKERS):
                record["body_state"] = "body_evicted"
            else:
                record["body_state"] = "body_unavailable"
            record["body_error"] = (type(exc).__name__ + ": " + message)[:300]
        record["body_elapsed_ms"] = int(max(0.0, _MONOTONIC() - started) * 1000)
        if record["body_state"] != "read":
            self._progress("upload.response_body.unavailable",
                           request_id=rid, state=record["body_state"],
                           elapsed_ms=record.get("body_elapsed_ms"))
        return record

    def _resolve_upload_response_bodies(self, bound):
        for record in bound or ():
            if record.get("body_state") == "pending":
                self._read_upload_response_body(record)

    def _drain_upload_events(self):
        """Measure finished uploads outside event callbacks.

        1.2.90 keeps ``requestfinished`` to an object reference only.  Size
        and header reads happen here on the driver thread, never in the
        Playwright event callback.
        """
        attempt = self.upload_attempt
        while self._finished_upload_requests:
            item = self._finished_upload_requests.pop(0)
            if isinstance(item, dict):
                request = item.get("request")
                rid = item.get("request_id")
                sizes = item.get("sizes")
                headers = item.get("headers")
            else:
                request = item
                rid = self._pending_uploads.get(request)
                sizes = headers = None
            if not rid:
                continue
            try:
                if sizes is None:
                    sizes = request.sizes()
                if headers is None:
                    headers = request.all_headers()
                body_bytes = int(sizes.get("requestBodySize") or 0)
                size_source = "playwright_request_size"
                if body_bytes <= 0:
                    body_bytes = int(headers.get("content-length") or 0)
                    size_source = "request_content_length_header"
                content_type = headers.get("content-type", "").lower()
                host = urlparse(request.url).hostname or ""
                method = str(getattr(request, "method", "") or "")
                probe = getattr(self, "_upload_probe_events", None)
                if probe is None:
                    probe = []
                    self._upload_probe_events = probe
                if len(probe) < 12:
                    probe.append({"method": method, "host": host,
                                  "ct": content_type[:60], "bytes": body_bytes})
                    self._progress("upload.probe", method=method, host=host,
                                   ct=content_type[:60], bytes=body_bytes,
                                   url=str(request.url)[:180])
                looks_upload = any(t in content_type for t in
                                   ("video/", "application/octet-stream",
                                    "multipart/form-data")) or any(
                    h in host for h in ("vod", "upload", "bytedanceapi"))
                if not looks_upload:
                    continue
                if body_bytes >= attempt.size:
                    attempt.request_ids.append(rid)
                    attempt.request_observed = True
                    attempt.request_records.append({"request_id": rid, "method": request.method,
                        "bytes": body_bytes, "size_source": size_source,
                        "selected_file_sha256": attempt.sha256,
                        "proof": "request_id+request_size+selected_file_sha256",
                        "host": host,
                        "url": str(request.url)})
                    continue
                if body_bytes > 0:
                    pool = getattr(self, "_upload_bytes_pool", None)
                    if pool is None:
                        pool = {"total": 0, "ids": [], "host": "", "url": ""}
                        self._upload_bytes_pool = pool
                    pool["total"] += body_bytes
                    pool["ids"].append(rid)
                    pool["host"] = pool["host"] or host
                    pool["url"] = pool["url"] or str(request.url)
            except Exception as exc:
                attempt.extra["request_measurement_error"] = str(exc)[:300]

    def _unregister_upload_request_listener(self):
        for event, name in (("request", "_upload_request_handler"),
                            ("response", "_parse_upload_response"),
                            ("requestfinished", "_upload_response_handler")):
            handler = getattr(self, name, None)
            if handler is not None:
                try:
                    self._page.remove_listener(event, handler)
                except Exception:
                    pass
            setattr(self, name, None)
        self._upload_response_objects = {}

    def _bind_upload_response(self, attempt, response, *, resource_ids=(),
                              video_preview=False):
        """Record the completed upload request_id that owns this response."""
        rid = str((response or {}).get("request_id") or "")
        if not rid:
            return
        resource_ids = [str(value) for value in (resource_ids or ()) if value]
        proof = "request_id_intersection+request_size+selected_file_sha256+response_2xx"
        if resource_ids:
            proof += "+response_resource_id"
        elif video_preview:
            proof += "+video_preview"
        for record in attempt.request_records:
            if str(record.get("request_id") or "") != rid:
                continue
            record.update({
                "response_request_id": rid,
                "response_status": int(response.get("status") or 0),
                "response_resource_ids": list(resource_ids),
                "proof": proof,
            })
        attempt.extra["upload_response_binding"] = {
            "request_id": rid,
            "status": int(response.get("status") or 0),
            "resource_ids": list(resource_ids),
            "video_preview": bool(video_preview),
        }

    def _click_text_or_selector(self, selectors, *, reason, timeout=30.0,
                                root=None):
        locator, selector = self._visible_locator(selectors, timeout=timeout,
                                                  root=root)
        if locator is None:
            self._fail(f"\u627e\u4e0d\u5230\u53ef\u70b9\u51fb\u5143\u7d20\uff08{reason}\uff09", step=reason)
        result = self._click_with_retries(locator, reason=reason)
        if not result.get("ok"):
            self._fail(f"\u70b9\u51fb\u5931\u8d25\uff08{reason}\uff09\uff1a{result.get('reason')}",
                       step=reason)
        return locator, selector

    @staticmethod
    def _is_temp_name(name):
        return str(name).startswith(".") or str(name).endswith(".incoming")

    @staticmethod
    def _file_sha256(path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _unique_media(paths):
        """Collapse one file reached twice (inbox junction + desktop folder).

        Prefer the path that is not behind a reparse point so the secure copy
        path keeps working.
        """
        from .secure_io import is_reparse_point
        best = {}
        order = []
        for path in paths:
            try:
                key = os.path.normcase(os.path.realpath(str(path)))
            except OSError:
                key = os.path.normcase(str(path))
            if key not in best:
                best[key] = path
                order.append(key)
                continue
            try:
                if is_reparse_point(best[key]) and not is_reparse_point(path):
                    best[key] = path
            except OSError:
                pass
        return [best[key] for key in order]

    def _adopt_media(self, path):
        """Copy a video dropped outside the inbox into it, safely.

        The destination name carries the content digest, so two different
        files with the same name can never overwrite or impersonate each
        other.  The copy also hydrates OneDrive placeholders, which the secure
        hashing/copy path cannot read directly.
        """
        if self.layout is None:
            return path
        from .secure_io import is_reparse_point
        inbox = Path(self.layout.inbox)
        try:
            inside = os.path.normcase(os.path.realpath(str(path))).startswith(
                os.path.normcase(os.path.realpath(str(inbox))) + os.sep)
            reparse = is_reparse_point(path)
        except OSError:
            inside, reparse = False, False
        if inside and not reparse:
            return path
        staged = None
        try:
            inbox.mkdir(parents=True, exist_ok=True)
            staged = inbox / (".%s.%d.incoming" % (path.name, os.getpid()))
            digest = hashlib.sha256()
            with open(path, "rb") as src, open(staged, "wb") as dst:
                while True:
                    block = src.read(1 << 20)
                    if not block:
                        break
                    digest.update(block)
                    dst.write(block)
            dest = inbox / ("%s-%s%s" % (path.stem, digest.hexdigest()[:12],
                                        path.suffix))
            if dest.exists():
                if self._file_sha256(dest) != digest.hexdigest():
                    staged.unlink()
                    self._fail("\u7d20\u6750\u7f13\u5b58\u6587\u4ef6\u6821\u9a8c\u5931\u8d25\uff0c\u8bf7\u5220\u9664\u7d20\u6750\u76ee\u5f55\u91cd\u8bd5")
                staged.unlink()
                return dest
            os.replace(str(staged), str(dest))
            return dest
        except OSError as exc:
            if staged is not None:
                try:
                    staged.unlink()
                except OSError:
                    pass
            self._fail("\u7d20\u6750\u590d\u5236\u5931\u8d25\uff1a" + str(exc))

    def _verified_media(self, path, sha):
        adopted = self._adopt_media(path)
        if sha:
            from .hashing import sha256_bytes
            try:
                actual = sha256_bytes(Path(adopted).read_bytes())
            except OSError as exc:
                self._fail("\u8bfb\u53d6\u5f85\u53d1\u5e03\u89c6\u9891\u5931\u8d25\uff1a" + str(exc))
            if actual != sha:
                self._fail("\u5f85\u53d1\u5e03\u89c6\u9891 SHA256 \u4e0e\u8ba1\u5212\u4e0d\u4e00\u81f4\uff0c\u62d2\u7edd\u53d1\u5e03")
        return adopted

    def _resolve_media_path(self, plan, key="sha256"):
        piece = plan.get("piece") or {}
        sha = piece.get(key)
        for candidate in (piece.get("file_path"), piece.get("media_path"),
                          plan.get("file_path"), plan.get("media_path")):
            if candidate and Path(str(candidate)).exists() \
                    and not self._is_temp_name(Path(str(candidate)).name):
                return self._verified_media(Path(str(candidate)), sha)
        file_name = piece.get("file_name")
        if file_name and self.layout is not None:
            from .desktop import media_roots
            wanted = str(file_name)
            hits = self._unique_media([p for root in media_roots(self.layout)
                                       for p in root.rglob("*")
                                       if p.is_file() and p.name == wanted
                                       and not self._is_temp_name(p.name)])
            if len(hits) == 1:
                return self._verified_media(hits[0], sha)
            if len(hits) > 1:
                self._fail("\u540c\u540d\u89c6\u9891\u4e0d\u552f\u4e00\uff0c\u8bf7\u5728\u8ba1\u5212\u4e2d\u6307\u5b9a\u7edd\u5bf9\u8def\u5f84")
        if sha and self.layout is not None:
            from .desktop import media_roots
            for path in self._unique_media([q for root in media_roots(self.layout)
                                            for q in root.rglob("*")]):
                if not path.is_file() or self._is_temp_name(path.name):
                    continue
                try:
                    from .hashing import sha256_bytes
                    payload = path.read_bytes()
                except OSError:
                    try:
                        payload = Path(self._adopt_media(path)).read_bytes()
                    except OSError:
                        continue
                if sha256_bytes(payload) == sha:
                    return self._verified_media(path, sha)
        return None

    def _ensure_upload_page(self):
        task = xingtu_tasks.task_from_plan(
            self.plan, mode=getattr(self, "_operation_mode", None))
        target_url = DOUYIN_UPLOAD_URL
        if task:
            try:
                target_url = xingtu_tasks.xingtu_upload_url(task)
            except xingtu_tasks.XingtuError as exc:
                raise xingtu_tasks.XingtuTaskNotAttached(str(exc)) from exc
        try:
            self._page.goto(target_url, wait_until="domcontentloaded")
        except Exception as exc:
            self._fail(f"\u7f51\u7edc\u5bfc\u822a\u5931\u8d25\uff1a{exc}", step="navigation")
        self.humanizer.page_load_pause()
        url = self._page.url or ""
        if urlparse(url).netloc != urlparse(target_url).netloc:
            self._fail("\u4e0a\u4f20\u9875\u8df3\u8f6c\u5230\u975e\u9884\u671f\u7ad9\u70b9\uff0c\u62d2\u7edd\u7ee7\u7eed")
        if "login" in url or "passport" in url:
            self._fail("\u521b\u4f5c\u8005\u4e2d\u5fc3\u8df3\u8f6c\u5230\u4e86\u767b\u5f55\u9875\u3002\u9996\u6b21\u5b89\u88c5\u987b\u7531\u8d26\u53f7\u672c\u4eba\u5b8c\u6210\u4e00\u6b21\u767b\u5f55\u548c\u8eab\u4efd\u9a8c\u8bc1\uff08\u5237\u8138\u6216\u539f\u8bbe\u5907\u626b\u7801\uff09\uff1bprofile \u53ea\u4fdd\u5b58\u767b\u5f55\u6001\uff0c\u5e73\u53f0\u4ecd\u53ef\u80fd\u6309\u98ce\u9669\u7b56\u7565\u518d\u6b21\u8981\u6c42\u9a8c\u8bc1\u3002\u7a0b\u5e8f\u4e0d\u4f1a\u5c1d\u8bd5\u7ed5\u8fc7")
        self._dismiss_page_overlays()
        self._record("browser", note="\u5df2\u6253\u5f00\u6296\u97f3\u521b\u4f5c\u8005\u4e2d\u5fc3\u4e0a\u4f20\u9875\uff0c\u5e76\u5904\u7406\u53ef\u89c1\u9875\u9762\u5f15\u5bfc\u6d6e\u5c42")

    def _empty_state_visible(self, timeout=0):
        candidates = self._visible_candidates(
            MANAGE_EMPTY_STATE_SELECTORS, timeout=timeout)
        if not getattr(self, "_last_visible_scan_complete", False):
            raise VerificationQueryError("manage empty-state query failed")
        return bool(candidates)

    def _has_next_page(self):
        candidates = self._visible_candidates(
            MANAGE_NEXT_PAGE_SELECTORS, timeout=0)
        if not getattr(self, "_last_visible_scan_complete", False):
            raise VerificationQueryError("manage pagination query failed")
        for locator, _selector, _index in candidates:
            try:
                if locator.is_enabled():
                    return True
            except Exception:
                return True
        return False

    def _wait_manage_list_ready(self, timeout=None):
        """Wait for a positively identified management list state.

        A successful DOM query returning zero links is NOT an empty baseline.
        The page must expose either (a) a stable visible edit-link set across two
        real scans, or (b) an explicit "no works" state across two real scans.
        """
        seconds = (DEFAULT_MANAGE_LIST_READY_SECONDS if timeout is None
                   else max(0.0, float(timeout)))
        deadline = time.monotonic() + seconds
        previous = None
        while True:
            links = self._visible_candidates(EDIT_LINK_SELECTORS, timeout=0)
            if not getattr(self, "_last_visible_scan_complete", False):
                raise VerificationQueryError("manage edit-link query failed")
            count = len(links)
            empty = False
            if count == 0:
                empty = self._empty_state_visible(timeout=0)
            scanned = self._real_work_candidates(timeout=0)
            if not self._last_edit_scan_complete:
                self._last_manage_list_ready = False
                return False
            snapshot = (tuple(sorted(c["work_id"] for c in scanned)), empty)
            if snapshot == previous and (count > 0 or empty):
                self._last_manage_list_ready = True
                return True
            previous = snapshot
            if time.monotonic() >= deadline:
                self._last_manage_list_ready = False
                return False
            self.humanizer.poll_pause()

    def _scan_manage_works(self, plan, timeout=30.0):
        """Retired list API: never inspect, navigate, scroll or certify a view.

        Kept as a refusal for legacy callers. No publication path calls it.
        Pagination/filter/virtual views therefore cannot become evidence.
        """
        self._last_edit_scan_complete = False
        return None

    # ---- upload / form --------------------------------------------------
    def _assert_xingtu_task_attached(self):
        task = xingtu_tasks.task_from_plan(
            getattr(self, "plan", {}) or {},
            mode=getattr(self, "_operation_mode", None))
        if not task:
            return True
        for _ in range(6):
            try:
                if getattr(self, "_operation_mode", None) == MODE_FILL:
                    attached = _xingtu_page_task_readback(self._page, task)["ok"]
                else:
                    attached = (_xingtu_page_shows_task(self._page, task)
                            or _xingtu_deeplink_attached(self._page, task))
            except Exception:
                attached = False
            if attached:
                self._record("xingtu_task", note="selected star task control is attached on the upload page",
                             extra={"order_id": task.get("order_id"),
                                    "task_name": task.get("task_name")})
                return True
            # wait-ok: poll
            time.sleep(0.8)
            try:
                self._page.wait_for_timeout(800)
            except Exception:
                pass
        raise xingtu_tasks.XingtuTaskNotAttached(
            "selected star task is not shown on the upload page; publish stopped before submit")

    def _preflight_xingtu_page(self, plan, operation_mode, *, require=False):
        """Run read-only live preflight, before upload when possible."""
        task = xingtu_tasks.task_from_plan(
            plan, mode=getattr(self, "_operation_mode", operation_mode))
        if not task:
            return {"status": "skipped", "available": False}
        outcome = preflight_xingtu_selection(
            self._page, task, driver=self,
            timeout=getattr(self, "_xingtu_preflight_timeout", 20.0))
        self._xingtu_preflight_outcome = outcome
        status = outcome.get("status")
        if status == "deferred":
            if require:
                raise xingtu_tasks.XingtuTaskUnavailable(
                    "上传完成后仍无法进行星图任务只读预检，已停止发布",
                    details={"reason": outcome.get("reason")})
            self._record(
                "xingtu_task", note="read-only task preflight deferred until upload",
                extra={"reason": outcome.get("reason")})
            return outcome
        if status != xingtu_tasks.RESOLVE_OK:
            xingtu_tasks.raise_match_error(outcome, task)
        self._xingtu_resolved_identity = outcome.get("resolved_identity")
        self._record(
            "xingtu_task", note="read-only task preflight passed before upload",
            extra={"resolved_identity": self._xingtu_resolved_identity})
        return outcome

    def _upload_video_with_preflight(self, media, plan, operation_mode):
        """Gate upload on a read-only task preflight.

        When the publish entry page does not expose the picker yet, the
        deferred outcome is retried after transcode (see ``fill``), before the
        cover/task selection and long before the publish button.
        """
        outcome = self._preflight_xingtu_page(plan, operation_mode)
        self._upload_video(media)
        return outcome

    def _xingtu_ledger_path(self):
        """Resolve the local used-task ledger path from the data layout."""
        override = getattr(self, "_xingtu_used_tasks_path", None)
        if override:
            return Path(override)
        return xingtu_tasks.used_tasks_path(getattr(self, "layout", None))

    def preflight_xingtu(self, plan, *, mode=None):
        """Check the local used-task ledger before reserving/submitting.

        Xingtu rejects duplicate submissions itself, so the publish chain must
        not read the backend task list.  Only the local record of names we have
        already published decides the pre-submit gate.  An unreadable ledger is
        a hard stop, never an implicit "not used before".
        """
        task = xingtu_tasks.task_from_plan(plan, mode=mode)
        if not task:
            return None
        if str(mode or "") == MODE_FILL:
            self._progress("xingtu.duplicate", result="skipped",
                           reason="fill_mode")
            return task
        task_name = str(task.get("task_name") or "").strip()
        order_id = str(task.get("order_id") or "").strip()
        key = "%s|%s|%s" % (
            task_name, order_id,
            str((plan.get("piece") or {}).get("sha256")))
        if getattr(self, "_xingtu_checked_key", None) == key:
            return task

        try:
            path = self._xingtu_ledger_path()
            media_sha = str((plan.get("piece") or {}).get("sha256") or "")
            matched, matched_by = xingtu_tasks.find_used_task(
                path, task, media_sha)
        except xingtu_tasks.XingtuDedupLedgerError as exc:
            _xingtu_stage(self, "xingtu.dedup", result="ledger_error",
                          code=exc.code, error=str(exc),
                          name=task_name or None, order_id=order_id or None)
            self._record(
                "xingtu_task", note="local used-task ledger unreadable",
                error=str(exc),
                extra={"code": exc.code, "order_id": order_id or None,
                       "task_name": task_name or None})
            raise
        except Exception as exc:
            error = xingtu_tasks.XingtuDedupLedgerError(
                "%s: %s" % (type(exc).__name__, exc))
            _xingtu_stage(self, "xingtu.dedup", result="ledger_error",
                          code=error.code, error=str(error),
                          name=task_name or None, order_id=order_id or None)
            self._record(
                "xingtu_task", note="local used-task ledger unreadable",
                error=str(error),
                extra={"code": error.code, "order_id": order_id or None,
                       "task_name": task_name or None})
            raise error from exc

        if matched:
            _xingtu_stage(
                self, "xingtu.dedup", result="blocked",
                name=task_name or order_id,
                matched=matched.get("task_name") or matched.get("order_id"),
                matched_by=matched_by,
                duplicate_kind=matched.get("duplicate_kind"),
                published_at=matched.get("published_at"),
                media_sha256_prefix=matched.get("media_sha256_prefix"),
                matched_order_id=matched.get("order_id") or None)
            error_cls = (xingtu_tasks.XingtuDuplicatePossibleLocal
                         if "possible" in str(matched_by)
                         else xingtu_tasks.XingtuDuplicateLocal)
            raise error_cls(task_name or order_id, matched,
                            details=dict(matched, task_name=task_name or order_id,
                                         matched_by=matched_by))

        self._xingtu_checked_key = key
        _xingtu_stage(self, "xingtu.dedup", result="ok",
                      name=task_name or None, order_id=order_id or None)
        self._record("xingtu_task", note="local used-task check passed",
                     extra={"order_id": order_id or None,
                            "task_name": task_name or None})
        return task

    def readback_xingtu(self, plan, result=None):
        """Deprecated: post-publish backend read-back is removed in 1.2.99."""
        task = xingtu_tasks.task_from_plan(plan)
        if not task:
            return {"ok": True, "status": "NOT_APPLICABLE"}
        status = {"status": "NOT_APPLICABLE",
                  "reason": "post-publish backend read-back removed",
                  "order_id": task.get("order_id"),
                  "task_name": task.get("task_name")}
        self._xingtu_readback = status
        self._record("xingtu_readback", note="backend read-back skipped",
                     extra={"status": status.get("status"),
                            "order_id": task.get("order_id")})
        return status

    def record_xingtu_publish(self, plan, result=None):
        """Append a confirmed publish to the local used-task ledger."""
        task = xingtu_tasks.task_from_plan(plan)
        if not task:
            return None
        identity = getattr(self, "_xingtu_resolved_identity", None)
        identity = identity if isinstance(identity, dict) else {}
        if identity.get("kind") == "order_id" and identity.get("value"):
            task["order_id"] = str(identity["value"])
            task["task_name_full"] = str(identity.get("display_name") or "")
        elif identity.get("kind") == "full_name" and identity.get("value"):
            task["task_name_full"] = str(identity["value"])
        attempt_id = str(getattr(getattr(self, "upload_attempt", None),
                                 "attempt_id", "") or "")
        if identity.get("kind") == "order_id" and task.get("order_id"):
            matched_by = "order_id+media"
        elif task.get("task_name_full"):
            matched_by = "task_name_full+media"
        else:
            matched_by = "legacy_family+media"
        try:
            entry = xingtu_tasks.append_used_task(
                self._xingtu_ledger_path(), task, attempt_id=attempt_id,
                media_sha256=str((plan.get("piece") or {}).get("sha256") or ""),
                client_version=VERSION,
                matched_by=matched_by)
        except xingtu_tasks.XingtuDedupLedgerError as exc:
            _xingtu_stage(self, "xingtu.dedup", result="ledger_error",
                          phase="record", code=exc.code, error=str(exc),
                          name=task.get("task_name") or None,
                          order_id=task.get("order_id") or None)
            self._record(
                "xingtu_task", note="local used-task ledger append failed",
                error=str(exc),
                extra={"code": exc.code, "order_id": task.get("order_id"),
                       "task_name": task.get("task_name")})
            raise
        self._record("xingtu_task", note="local used-task ledger appended",
                     extra=dict(entry))
        return entry

    def fill(self, plan):
        self.plan = plan
        self.check_hold()
        self._close_duplicate_creator_pages()
        self._filled = False
        self._publish_click_completed_at = None
        self._publish_outcome = None
        self._finalized_publish = None
        self._publish_start_url = None
        self._publish_start_success_marker = None
        self._publish_start_failure_marker = None
        self._publish_start_success_counts = None
        self._publish_start_failure_counts = None
        self._publish_observation_started_at = None
        self.last_publish_evidence = None
        self._last_publish_evidence = None
        piece = plan.get("piece") or {}
        title = str(piece.get("title") or "")
        if not title.strip():
            self._fail("\u7d20\u6750\u6807\u9898\u4e3a\u7a7a\uff0c\u4e0d\u80fd\u586b\u8868")
        if piece.get("poi"):
            self._fail("\u672c\u7248\u4e0d\u652f\u6301 POI\uff1b\u987b\u8c03\u6574\u8ba1\u5212\u540e\u91cd\u65b0\u5ba1\u6279")
        operation_mode = str(getattr(self, "_operation_mode", MODE_APPROVED))
        if xingtu_tasks.skip_fill_task(plan, operation_mode):
            self._progress("xingtu.attach", result="skipped", reason="no_task_name")
        if operation_mode != MODE_FILL:
            self.preflight_xingtu(plan)
        media = self._resolve_media_path(plan)
        if media is None:
            self._fail("\u627e\u4e0d\u5230\u5f85\u4e0a\u4f20\u89c6\u9891\u6587\u4ef6\uff08plan.piece.file_path/media_path "
                       "\u6216 inbox \u6309 file_name/sha256 \u68c0\u7d22\u5747\u672a\u547d\u4e2d\uff09")

        from .content_binding import validate_local_content
        from .secure_io import copy_hashed_file
        validate_local_content(plan, media)
        self._source_media = media
        if getattr(self, "_staged_dir", None) is not None:
            self._staged_dir.cleanup()
        self._staged_dir = tempfile.TemporaryDirectory(prefix="publish-input-",
            dir=str(self.layout.run) if self.layout else None)
        staged = Path(self._staged_dir.name) / ("video" + media.suffix)
        try:
            copy_hashed_file(
                media, staged, expected_sha256=piece.get("sha256"))
        except OSError as exc:
            self._fail("\u5f85\u4e0a\u4f20\u89c6\u9891\u8def\u5f84\u4e0d\u5b89\u5168\uff1a" + str(exc))
        except ValueError:
            self._fail("\u5f85\u4e0a\u4f20\u89c6\u9891\u5728\u590d\u5236\u65f6\u53d1\u751f\u53d8\u5316")
        validate_local_content(plan, staged)
        self._cover_paths = {}
        for kind, path in (piece.get("covers") or {}).items():
            if path:
                dest = Path(self._staged_dir.name) / (kind + Path(path).suffix)
                try:
                    copy_hashed_file(
                        path, dest,
                        expected_sha256=piece["cover_sha256"][kind])
                except OSError as exc:
                    self._fail("\u5c01\u9762\u8def\u5f84\u4e0d\u5b89\u5168\uff1a" + str(exc))
                except ValueError:
                    self._fail("\u5c01\u9762\u5728\u51bb\u7ed3\u8f93\u5165\u65f6\u53d1\u751f\u53d8\u5316")
                self._cover_paths[kind] = dest
        media = staged
        self._ensure_upload_page()
        self._verify_account(plan)
        self._dismiss_page_overlays()
        self._preflight_xingtu_page(plan, operation_mode)
        self._upload_video(media)
        self._dismiss_page_overlays()
        self._fill_title(title)
        self._fill_description(piece)
        self._finalize_upload_evidence()
        self._wait_transcode()
        if (getattr(self, "_xingtu_preflight_outcome", {}) or {}).get(
                "status") == "deferred":
            # Upload finished; the form now exists.  A second read-only
            # preflight must pass before cover/task selection and, critically,
            # before the publish button.
            self._preflight_xingtu_page(plan, operation_mode, require=True)
        self._choose_cover(piece.get("covers") or {})
        task = xingtu_tasks.task_from_plan(plan, mode=operation_mode)
        if task:
            select_publish_xingtu_task(
                self._page, task, driver=self,
                mode=MODE_FILL if operation_mode == MODE_FILL else "publish")
        self._dismiss_page_overlays()
        self.verify_form(plan)
        self._filled = True
        self._record("fill", note="\u4e0a\u4f20\u3001\u6807\u9898\u3001\u8bdd\u9898\u3001\u5c01\u9762\u3001\u8f6c\u7801\u7b49\u5f85\u5df2\u5b8c\u6210\uff1b\u505c\u5728\u53d1\u5e03\u6309\u94ae\u524d",
                     extra={"media": str(media), "title": title,
                            "topic_mode": self.topic_mode,
                            "topic_modes": list(self.topic_modes)})
        return {"filled": True, "media": str(media),
                "topic_mode": self.topic_mode,
                "topic_modes": list(self.topic_modes)}

    def _upload_page_signal(self):
        budget = self._upload_page_call_timeout_seconds()
        started = _MONOTONIC()
        url = self._raw_page_url()
        if _MONOTONIC() - started > budget:
            return {"call_timeout": "upload.page_url"}
        path = urlparse(url).path.rstrip("/")
        if (path == "/creator-micro/content/post/video"
                or path.endswith("/content/post/video")):
            return {"signal": "url", "url": url}
        started = _MONOTONIC()
        try:
            count = int(self._page.locator(
                ", ".join(UPLOAD_RESOURCE_SELECTORS)).count())
        except Exception as exc:
            self._progress("upload.signal_error",
                           error=type(exc).__name__ + ": " + str(exc))
            count = 0
        if _MONOTONIC() - started > budget:
            return {"call_timeout": "upload.signal_count", "url": url}
        if count:
            return {"signal": "resource_node", "url": url,
                    "resource_nodes": count}
        return None

    def _wait_upload_signal(self, timeout):
        timeout = max(0.0, float(timeout))
        started = time.monotonic()
        deadline = started + timeout
        last_report = 0.0
        while True:
            # Same thread as the driver: no locator/count call is dispatched
            # to a helper thread while the upload is in flight.
            signal = self._upload_page_signal()
            cause = ((signal or {}).get("call_timeout")
                     if isinstance(signal, dict) else None)
            if cause:
                self._progress(
                    "upload.trigger.timeout",
                    timeout_seconds=timeout,
                    elapsed=round(time.monotonic() - started, 1),
                    url=self._strict_page_url(
                        "upload.trigger.timeout.url",
                        max(0.05, deadline - time.monotonic())) or "",
                    cause=cause)
                return None
            if signal:
                return signal
            now = time.monotonic()
            if now - last_report >= 2.0:
                last_report = now
                url = self._strict_page_url(
                    "upload.wait_trigger.url",
                    max(0.05, deadline - time.monotonic()))
                if url is None:
                    self._progress(
                        "upload.trigger.timeout", timeout_seconds=timeout,
                        elapsed=round(time.monotonic() - started, 1), url="",
                        cause="upload.wait_trigger.url")
                    return None
                self._progress(
                    "upload.wait_trigger", url=url,
                    elapsed=round(now - started, 1),
                    remaining=round(max(0.0, deadline - now), 1))
            if now >= deadline:
                self._progress(
                    "upload.trigger.timeout", timeout_seconds=timeout,
                    elapsed=round(now - started, 1),
                    url=self._strict_page_url(
                        "upload.trigger.timeout.url",
                        max(0.05, deadline - time.monotonic())) or "")
                return None
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
            try:
                self._drain_upload_events()
            except Exception as exc:
                self._progress("upload.event_drain_error",
                               error=type(exc).__name__ + ": " + str(exc))

    def _upload_video(self, media: Path):
        account_id = (getattr(self.channel, "account_id", None)
                      or self.plan.get("account_id") or "")
        env_id = (getattr(self.env, "env_id", None)
                  or self.plan.get("env_id") or "default")
        attempt = make_upload_attempt(env_id, account_id, media)
        self._progress("upload.metadata.start", media=str(media))
        try:
            meta = media_metadata(media, probe=ffprobe_media_metadata)
        except Exception as exc:
            self._progress("upload.metadata.failed",
                           error=type(exc).__name__ + ": " + str(exc))
            self._fail("\u8bfb\u53d6\u672c\u5730\u89c6\u9891\u5143\u6570\u636e\u5931\u8d25\uff1a"
                       + type(exc).__name__ + ": " + str(exc), step="upload")
        attempt.size = meta["size"]
        attempt.sha256 = meta["sha256"]
        attempt.duration_known = meta["duration_known"]
        attempt.duration_ms = meta["duration_ms"]
        attempt.encoding_known = meta["encoding_known"]
        attempt.encoding = meta["encoding"]
        self.upload_attempt = attempt
        self._progress("upload.metadata.ok", size=attempt.size,
                       sha256=attempt.sha256,
                       duration_ms=attempt.duration_ms,
                       encoding=attempt.encoding)

        if not attempt.duration_known or not attempt.encoding_known:
            self._fail("\u65e0\u6cd5\u8bfb\u53d6\u672c\u5730\u89c6\u9891\u65f6\u957f/\u7f16\u7801\uff1b\u62d2\u7edd\u58f0\u79f0\u4e0a\u4f20\u9a8c\u8bc1\u5b8c\u6210",
                       step="upload")
        preexisting, _ = self._unique_locator(UPLOAD_RESOURCE_SELECTORS, timeout=0, visible=False)
        if preexisting is not None:
            attempt.title_preexisting = True
            self._fail("\u4e0a\u4f20\u524d\u5df2\u6709\u8d44\u6e90\uff0c\u62d2\u7edd\u4f7f\u7528\u65e7\u8868\u5355",
                       step="upload")
        lookup_timeout = self._upload_lookup_timeout_seconds()
        try:
            open_pages = [_page_url_value(page)
                          for page in (self._context().pages or [])]
        except Exception:
            open_pages = [self._safe_page_url()]
        self._progress("upload.input.scan.start", timeout_seconds=lookup_timeout,
                       pages=open_pages)
        locator, selector, scan = self._wait_usable_upload_input(lookup_timeout)
        self._progress("upload.input.found", selector=selector)
        video_baseline = _video_preview_baseline(self._page)
        if video_baseline is None:
            self._fail("\u4e0a\u4f20\u524d\u65e0\u6cd5\u8bb0\u5f55\u53ef\u89c1\u89c6\u9891\u57fa\u7ebf\uff1b"
                       "\u62d2\u7edd\u5c06\u672a\u77e5\u89c6\u9891\u5f53\u4f5c\u672c\u6b21\u4e0a\u4f20\u9884\u89c8",
                       step="upload")
        attempt.extra["upload_video_baseline"] = {
            "count": video_baseline["count"],
            "sources": list(video_baseline["sources"]),
        }
        self._progress("upload.video_baseline.ok",
                       count=video_baseline["count"],
                       sources=len(video_baseline["sources"]))
        self._register_upload_request_listener()
        try:
            set_timeout_ms = int(min(self._upload_timeout_seconds(), 30.0) * 1000)
            attempt.input_set_at = time.time()
            self._progress("upload.set_input_files.start",
                           path=_log_basename(media),
                           timeout_ms=set_timeout_ms)
            try:
                if int(attempt.size or 0) > LARGE_UPLOAD_CDP_BYTES:
                    # >50MB cannot ride the playwright protocol; hand the
                    # local path to chrome over CDP instead.
                    self._progress("upload.set_input_files.cdp",
                                   path=_log_basename(media),
                                   bytes=attempt.size)
                    self._set_input_files_cdp(locator, media)
                else:
                    locator.set_input_files(str(media), timeout=set_timeout_ms)
            except Exception as exc:
                error_text = _redact_log_text(
                    type(exc).__name__ + ": " + str(exc))
                self._progress("upload.set_input_files.failed",
                               error=error_text)
                self._fail("\u4e0a\u4f20\u6587\u4ef6\u8f93\u5165\u5931\u8d25\uff08set_input_files\uff09\uff1a"
                           + error_text, step="upload")
            attempt.input_set = True
            self._progress("upload.set_input_files.ok",
                           path=_log_basename(media))

            started_at = time.time()
            started_monotonic = time.monotonic()
            self._upload_started_at = started_at
            self._upload_started_monotonic = started_monotonic
            self._upload_progress_logged_at = None
            self._upload_media = media
            self._upload_video_baseline = video_baseline
            self._upload_evidence_finalized = False
            self._upload_evidence = None
            attempt.extra["upload_started"] = {
                "file": str(media),
                "bytes": attempt.size,
                "at": started_at,
            }
            self._progress("upload.started", file=str(media),
                           bytes=attempt.size, at=round(started_at, 3))
            self._record(
                "upload",
                note="upload started; form filling continues in parallel",
                extra=dict(attempt.extra["upload_started"]))
            return {"started": True, "file": str(media),
                    "bytes": attempt.size, "at": started_at}
        except BaseException:
            self._unregister_upload_request_listener()
            raise

    def _upload_snapshot(self):
        """Collect upload progress without waiting for a preview."""
        attempt = getattr(self, "upload_attempt", None)
        started = getattr(self, "_upload_started_monotonic", None)
        elapsed = (round(max(0.0, time.monotonic() - started), 1)
                   if started is not None else 0.0)
        snapshot = {"elapsed": elapsed, "requests": 0, "responses": 0,
                    "2xx": 0, "bytes": 0, "request_observed": False,
                    "two_xx": []}
        if attempt is None:
            return snapshot
        try:
            self._drain_upload_events()
        except Exception as exc:
            self._progress("upload.event_drain_error",
                           error=type(exc).__name__ + ": " + str(exc))
        two_xx = []
        valid_ids = {str(value) for value in
                     (getattr(attempt, "request_ids", []) or ()) if value}
        valid_ids.update(str(record.get("request_id") or "")
                         for record in
                         (getattr(attempt, "request_records", []) or ())
                         if record.get("request_id"))
        for record in list(getattr(attempt, "response_records", []) or ()):
            try:
                status = int(record.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            rid = str(record.get("request_id") or "")
            if 200 <= status < 300 and (not valid_ids or rid in valid_ids):
                two_xx.append(record)
        byte_count = 0
        for record in list(getattr(attempt, "request_records", []) or ()):
            try:
                byte_count += int(record.get("bytes") or 0)
            except (TypeError, ValueError):
                pass
        snapshot.update({
            "requests": len(getattr(attempt, "request_records", []) or ()),
            "responses": len(getattr(attempt, "response_records", []) or ()),
            "2xx": len(two_xx),
            "two_xx": two_xx,
            "bytes": byte_count,
            "request_observed": bool(getattr(attempt, "request_observed", False)),
        })
        return snapshot

    def _upload_heartbeat(self, *, force=False):
        """Emit at most one upload.progress line per ten seconds."""
        if getattr(self, "_upload_started_monotonic", None) is None:
            return None
        now = time.monotonic()
        last = getattr(self, "_upload_progress_logged_at", None)
        if (not force and last is not None and
                now - last < UPLOAD_PROGRESS_LOG_INTERVAL_SECONDS):
            return None
        snapshot = self._upload_snapshot()
        self._upload_progress_logged_at = now
        self._progress("upload.progress", elapsed=snapshot["elapsed"],
                       requests=snapshot["requests"], responses=snapshot["responses"],
                       **{"2xx": snapshot["2xx"]}, bytes=snapshot["bytes"],
                       request_observed=snapshot["request_observed"])
        return snapshot

    def _finalize_upload_evidence(self):
        """Wait for this attempt's bindable 2xx upload response.

        The upload runs in the background while the form is filled.  Polling
        must therefore continue after the form is complete: a large file can
        still be transferring.  Every two seconds we drain finished upload
        events on the driver thread and log at most one progress line per ten
        seconds.  Only the configured hard deadline turns "not yet" into
        ``upload.no_2xx``.
        """
        if getattr(self, "_upload_evidence_finalized", False):
            return getattr(self, "_upload_evidence", None)
        attempt = getattr(self, "upload_attempt", None)
        timeout = self._upload_timeout_seconds()
        started = getattr(self, "_upload_started_monotonic", None)
        if started is None:
            started = time.monotonic()
            self._upload_started_monotonic = started
        deadline = started + timeout
        snapshot = self._upload_snapshot()
        while True:
            if attempt is not None and snapshot.get("two_xx"):
                break
            now = time.monotonic()
            if attempt is None or now >= deadline:
                elapsed = round(max(0.0, now - started), 1)
                self._progress("upload.evidence", result="failed",
                               code="upload.no_2xx",
                               requests=snapshot["requests"],
                               responses=snapshot["responses"],
                               bytes=snapshot["bytes"],
                               elapsed=elapsed, timeout=timeout)
                self._unregister_upload_request_listener()
                self._fail(
                    "upload.no_2xx: \u672c\u6b21\u4e0a\u4f20\u672a\u6536\u96c6\u5230\u4efb\u4f55 2xx \u54cd\u5e94"
                    " (elapsed=%.1f, timeout=%.1f)" % (elapsed, timeout),
                    step="upload", code="upload.no_2xx")
            self._upload_heartbeat()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            # time.sleep blocks the Playwright dispatcher thread: events that
            # arrive while an upload is still transferring would never be
            # delivered.  ``wait_for_timeout`` pumps that dispatcher, so the
            # following snapshot can actually observe late 2xx responses.
            _pause_page(self._page,
                        int(round(min(UPLOAD_WAIT_POLL_INTERVAL_SECONDS,
                                      remaining) * 1000)))
            snapshot = self._upload_snapshot()
        two_xx = list(snapshot.get("two_xx") or ())
        bound = None
        request_ids = {str(value) for value in
                       (getattr(attempt, "request_ids", []) or ()) if value}
        sizes = {}
        for record in getattr(attempt, "request_records", []) or ():
            rid = str(record.get("request_id") or "")
            try:
                sizes[rid] = int(record.get("bytes") or 0)
            except (TypeError, ValueError):
                sizes[rid] = 0
        candidates = [response for response in two_xx
                      if str(response.get("request_id") or "") in sizes]
        local_size = getattr(attempt, "size", None)
        if candidates and local_size:
            bound = min(
                candidates,
                key=lambda response: abs(
                    sizes.get(str(response.get("request_id") or ""), 0)
                    - local_size))
        elif candidates:
            bound = candidates[0]
        if bound is None:
            for response in two_xx:
                rid = str(response.get("request_id") or "")
                if rid and (not request_ids or rid in request_ids):
                    bound = response
                    break
        if bound is None:
            bound = two_xx[0]
        rid = str(bound.get("request_id") or "")
        if rid and rid not in request_ids:
            attempt.request_ids.append(rid)
        attempt.request_observed = True
        resource_ids = [str(value) for value in
                        (bound.get("resource_ids") or ()) if value]
        if not attempt.resource_id:
            attempt.resource_id = (resource_ids[0] if resource_ids
                                   else "upload:" + (rid or "2xx"))
            attempt.resource_node_id = attempt.resource_id
        attempt.resource_observed_at = time.time()
        attempt.size_matched = True
        if rid:
            self._bind_upload_response(attempt, bound,
                                       resource_ids=resource_ids)
        pool = getattr(self, "_upload_bytes_pool", None) or {}
        if rid and not attempt.request_records and int(pool.get("total") or 0) > 0:
            attempt.request_records.append({
                "request_id": rid,
                "method": str(bound.get("method") or "POST"),
                "bytes": int(pool.get("total") or 0),
                "size_source": "chunked_aggregate",
                "selected_file_sha256": attempt.sha256,
                "proof": "aggregate_request_size+selected_file_sha256+response_2xx",
                "host": pool.get("host") or "",
                "url": pool.get("url") or str(bound.get("url") or ""),
                "chunk_count": len(pool.get("ids") or ()),
            })
            self._progress("upload.evidence.aggregate",
                           bytes=int(pool.get("total") or 0),
                           chunks=len(pool.get("ids") or ()),
                           host=pool.get("host") or "")
        evidence = {
            "result": "ok",
            "requests": snapshot["requests"],
            "responses": snapshot["responses"],
            "2xx": snapshot["2xx"],
            "bytes": snapshot["bytes"],
            "request_id": rid or None,
            "video_required": False,
            "elapsed": round(max(0.0, time.monotonic() - started), 1),
            "at": time.time(),
        }
        attempt.extra["upload_final_evidence"] = evidence
        self._upload_evidence = evidence
        self._upload_evidence_finalized = True
        self._unregister_upload_request_listener()
        self._progress("upload.evidence", result="ok",
                       requests=evidence["requests"],
                       responses=evidence["responses"],
                       **{"2xx": evidence["2xx"]}, bytes=evidence["bytes"],
                       video_required=False)
        self._record("upload",
                     note="upload 2xx evidence confirmed; video preview not required",
                     extra=evidence)
        return evidence

    def _read_account_ids(self):
        """Return the identifiers the creator centre currently reports.

        1.2.35: the live page carries no ``data-account-id`` attribute (that
        selector only ever existed in the fixture), so ask the same-origin API
        the page itself calls.  The DOM selector stays as a fallback.
        """
        ids = {}
        try:
            resp = self._page.request.get(ACCOUNT_INFO_URL, timeout=15000)
            if getattr(resp, "ok", False):
                user = (resp.json() or {}).get("user") or {}
                for key in ("uid", "sec_uid"):
                    value = user.get(key)
                    if value:
                        ids[key] = str(value)
                # 顺手记下登录账号的真实昵称：机器人据此显示「1号（漫半拍）」，
                # 不再回头问客户「这个号叫什么」。只存昵称，不碰任何凭据。
                nickname = (user.get("nickname") or user.get("name")
                            or user.get("unique_id"))
                if nickname and self.layout is not None:
                    self._observed_nickname = str(nickname)
                    try:
                        from .account_names import record
                        record(self.layout, getattr(self.env, "env_id", ""),
                               nickname)
                    except Exception:
                        pass
        except Exception:
            ids = {}
        if not ids:
            account, _ = self._unique_locator(ACCOUNT_SELECTORS, timeout=3, visible=False)
            value = account.get_attribute("data-account-id") if account is not None else None
            if value:
                ids["dom"] = str(value)
        return ids

    def _verify_account(self, plan):
        expected = str((plan.get("piece") or {}).get("platform_account_id") or "")
        if ":" in expected:
            # ``prepare`` labels the target as ``env_x:platform``; that is a
            # routing label, not a Douyin uid.  Treat it as "bind to the
            # account this profile is signed in as" instead of failing the
            # comparison against the live uid.
            expected = ""
        ids = self._read_account_ids()
        if not ids:
            self._fail("\u5b9e\u9645\u5e73\u53f0\u8d26\u53f7\u65e0\u6cd5\u786e\u8ba4\uff08\u9875\u9762\u672a\u8fd4\u56de\u8d26\u53f7\u4fe1\u606f\uff09", step="account")
        observed = ids.get("uid") or next(iter(ids.values()))
        if not expected:
            # 1.2.35: plans built by the cadence layer carry no platform id yet;
            # bind to the account this profile is actually signed in as and say so.
            self._record("account", note="\u8ba1\u5212\u672a\u5e26\u5e73\u53f0\u8d26\u53f7 ID\uff0c\u5df2\u6309\u5f53\u524d\u767b\u5f55\u8d26\u53f7\u81ea\u52a8\u7ed1\u5b9a\uff1a" + observed)
            self._observed_account = observed
            return observed
        if expected not in ids.values():
            self._fail("\u5b9e\u9645\u5e73\u53f0\u8d26\u53f7\u65e0\u6cd5\u786e\u8ba4\u6216\u4e0e\u8ba1\u5212\u4e0d\u4e00\u81f4\uff1a"
                       + ", ".join(f"{k}={v}" for k, v in ids.items()), step="account")
        self._observed_account = observed
        return observed

    def _fill_text(self, selectors, value, reason):
        self._upload_heartbeat()
        self._dismiss_page_overlays()
        locator, _ = self._unique_locator(selectors, timeout=10)
        if locator is None:
            self._fail(f"\u627e\u4e0d\u5230\u552f\u4e00{reason}\u8f93\u5165\u6846")
        result = self._click_with_retries(locator, reason=reason)
        if not result.get("ok"):
            self._fail(f"\u805a\u7126{reason}\u5931\u8d25")
        self._page.keyboard.press("Control+A")
        self._page.keyboard.press("Backspace")
        self.humanizer.type_into(self._page, value)
        return locator

    def verify_form(self, plan):
        """Fresh DOM readback; caller flags never substitute for this evidence."""
        self.check_hold()
        if (getattr(self, "upload_attempt", None) is not None and
                getattr(self, "_upload_started_monotonic", None) is not None):
            self._finalize_upload_evidence()
        self._assert_xingtu_task_attached()
        self._verify_account(plan)
        piece = plan["piece"]
        expected_topics = list(piece.get("topics") or [])
        expected_topic_tokens = [_topic_token(topic) for topic in expected_topics
                                 if _topic_token(topic)]

        title_loc, _ = self._unique_locator(TITLE_SELECTORS, timeout=0)
        actual_title = (title_loc.evaluate(
            "el => ('value' in el ? el.value : el.innerText)")
            if title_loc is not None else None)
        if actual_title != piece["title"]:
            self._fail("\u6807\u9898\u56de\u8bfb\u4e0d\u4e00\u81f4")

        body_loc, _ = self._unique_locator(BODY_SELECTORS, timeout=0)
        expected_body = piece.get("copy_body") or ""
        actual_body = (body_loc.evaluate(
            "el => ('value' in el ? el.value : el.innerText)")
            if body_loc is not None else "")
        if body_loc is None and _normalize_form_text(expected_body):
            self._fail("\u63cf\u8ff0\u56de\u8bfb\u4e0d\u4e00\u81f4")

        chip_topic_tokens = [_topic_token(item) for item in self._topic_chip_texts()]
        chip_topic_tokens = [item for item in chip_topic_tokens if item]
        # A chip label also shows up in the editor innerText, so strip it there
        # before collecting plain-text tags (each tag must count once).
        body_text = str(actual_body or "")
        chip_counts = {}
        for token in chip_topic_tokens:
            chip_counts[token] = chip_counts.get(token, 0) + 1
        for token, count in chip_counts.items():
            body_text = _strip_topic_label(body_text, token, count)
        text_topic_tokens = [token for token in _topic_tokens_in_text(body_text) if token]
        observed_topic_tokens = chip_topic_tokens + text_topic_tokens
        # Douyin rewrites a tag to the canonical one when it recognises a typo,
        # so a one-character difference is a correction, not a mismatch.
        pairs, missing_topics, _unused = _match_topics(
            expected_topic_tokens, observed_topic_tokens)
        _chip_pairs, _planned_without_chip, unexpected_chips = _match_topics(
            expected_topic_tokens, chip_topic_tokens)
        if expected_topic_tokens and (missing_topics or unexpected_chips):
            self._progress("form.topics.mismatch",
                           expected=expected_topic_tokens,
                           chips=chip_topic_tokens,
                           text=text_topic_tokens,
                           missing=missing_topics,
                           unexpected=unexpected_chips)
            self._fail("\u8bdd\u9898\u56de\u8bfb\u4e0d\u4e00\u81f4")
        else:
            corrected = sorted({(wanted, seen) for wanted, seen in pairs
                                if wanted != seen})
            if corrected:
                self._progress("form.topics.corrected", corrected=corrected)
        if not _description_text_matches(actual_body, expected_body,
                                         expected_topics):
            self._fail("\u63cf\u8ff0\u56de\u8bfb\u4e0d\u4e00\u81f4")
        cover_state = self._cover_bound_verify()
        self._progress("cover.verify",
                       bound_src_prefix=cover_state.get("bound_src_prefix", ""),
                       found=bool(cover_state.get("found")),
                       visible=bool(cover_state.get("visible")),
                       fallback_used=bool(cover_state.get("fallback_used")))
        if not cover_state.get("ok"):
            self._cover_fail("cover is not confirmed on the publish page")
        from .content_binding import validate_local_content
        validate_local_content(plan, Path(self.upload_attempt.local_path))
        validate_local_content(plan, self._source_media)
        page_fact = self._transcode_page_fact()
        if page_fact["failed"]:
            self._fail("transcode.failed: \u9875\u9762\u51fa\u73b0\u300c\u8f6c\u7801\u5931\u8d25\u300d",
                       step="transcode", code="transcode.failed")
        page_ready = bool(page_fact["ready"])
        resource, _ = self._unique_locator(UPLOAD_RESOURCE_SELECTORS, timeout=0, visible=False)
        if resource is not None:
            rid = (resource.get_attribute("data-resource-id")
                   or resource.get_attribute("data-upload-id"))
            state = resource.get_attribute("data-transcode-status")
            if state in ("failed", "error"):
                self._fail("transcode.failed: \u672c\u6b21\u8d44\u6e90\u8f6c\u7801\u5931\u8d25",
                           step="transcode", code="transcode.failed")
            # 1.2.96: a stale/absent status attribute no longer vetoes a page
            # that already exposes an enabled publish button with no blocking
            # text.  A changed resource id still fails closed.
            if rid != self.upload_attempt.resource_id or (
                    state != "done" and not page_ready):
                self._fail("\u4e34\u95e8\u8f6c\u7801\u8d44\u6e90/\u5b8c\u6210\u6001\u53d1\u751f\u53d8\u5316")
        elif not page_ready:
            # No resource node is not a video-count gate: a 2xx upload
            # response bound in this run is the acceptance evidence.
            if not str(self.upload_attempt.resource_id or "").startswith("upload:"):
                self._fail("\u4e34\u95e8\u8f6c\u7801\u8d44\u6e90/\u5b8c\u6210\u6001\u53d1\u751f\u53d8\u5316")
        problems = transcode_failure_reasons(self.upload_attempt)
        if problems:
            self._fail("\u8868\u5355\u4e0a\u4f20/\u8f6c\u7801\u8bc1\u636e\u672a\u901a\u8fc7\uff1a" + ", ".join(problems))
        return True

    def _fill_title(self, title):
        self._fill_text(TITLE_SELECTORS, title, "\u6807\u9898")
        self._progress("form.title", chars=len(str(title or "")))
        self._upload_heartbeat()
        self.humanizer.step_pause(reason="\u6807\u9898\u8f93\u5165\u5b8c\u6210")

    def _fill_description(self, piece):
        """Clear the description first, then add only the configured topics.

        With no body text and no topics the editor is left untouched (a normal
        publish may have no description at all).  Whenever there is anything to
        write, Control+A/Backspace runs first so a draft cannot leak through.
        """
        if piece.get("copy_body") or piece.get("topics"):
            self._fill_text(BODY_SELECTORS, str(piece.get("copy_body") or ""),
                            "\u4f5c\u54c1\u63cf\u8ff0")
        self._add_topics(piece.get("topics") or [])
        self._progress("form.desc", chars=len(str(piece.get("copy_body") or "")),
                       topics=len(piece.get("topics") or []))
        self._upload_heartbeat()

    def _topic_chip_texts(self):
        try:
            values = self._page.locator("[data-topic], [data-mention]").evaluate_all(
                "els => els.map(el => el.getAttribute('data-topic') "
                "|| el.getAttribute('data-mention') || el.textContent || '')")
        except Exception:
            return []
        return [str(v).strip() for v in (values or []) if str(v).strip()]

    def _plain_editor_text(self, editor):
        """Editor text with every chip label removed (chips are counted apart)."""
        text = self._editor_text(editor)
        try:
            chips = self._topic_chip_texts()
        except Exception:
            chips = []
        counts = {}
        for chip in chips:
            counts[chip] = counts.get(chip, 0) + 1
        for chip, count in counts.items():
            text = _strip_topic_label(text, chip, count)
        return text

    def _topic_present(self, editor, topic):
        name = _topic_name(topic)
        if not name:
            return ""
        for chip in self._topic_chip_texts():
            if _topic_close_enough(name, chip):
                return "chip"
        if _topic_token_in_text(name, self._plain_editor_text(editor)):
            return "text"
        return ""

    def _new_chip(self, before, topic):
        """The chip this very entry produced, or "" when nothing is new."""
        name = _topic_name(topic)
        if not name:
            return ""
        try:
            chips = self._topic_chip_texts()
        except Exception:
            return ""
        outstanding = {}
        for chip in (before or []):
            outstanding[chip] = outstanding.get(chip, 0) + 1
        for chip in reversed(chips):
            if not _topic_close_enough(name, chip):
                continue
            if outstanding.get(chip, 0) > 0:
                outstanding[chip] -= 1
                continue
            return chip
        return ""

    def _wait_topic_chip(self, editor, topic, before=None, timeout=None):
        """Wait until THIS entry became a chip (an older chip does not count)."""
        limit = self._topic_conversion_timeout() if timeout is None else timeout
        deadline = time.monotonic() + max(0.0, limit)
        while True:
            chip = self._new_chip(before, topic)
            if chip:
                return chip
            if time.monotonic() >= deadline:
                return ""
            self.humanizer.poll_pause()

    def _wait_topic_present(self, editor, topic, timeout=None):
        limit = self._topic_conversion_timeout() if timeout is None else timeout
        deadline = time.monotonic() + max(0.0, limit)
        while True:
            found = self._topic_present(editor, topic)
            if found:
                return found
            if time.monotonic() >= deadline:
                return ""
            self.humanizer.poll_pause()

    def _add_topics(self, topics):
        """Enter every topic, then confirm it actually landed in the editor.

        Field failure (2026-11-11): four topics were requested, only the first
        became a chip and the publish then failed the read-back with three
        missing tags.  Each tag is now committed and verified on its own, and a
        tag that does not stick is retried once before the publish continues.
        """
        self.topic_modes = []
        self.topic_mode = "none"
        self.topic_missing = []
        if not topics:
            return self.topic_mode
        pending = [t for t in topics if _topic_token(t)]
        claimed = []
        self._topic_claimed = claimed
        for _attempt in (1, 2):
            if not pending:
                break
            missing = []
            for topic in pending:
                # A converted chip re-renders the editor, so re-acquire it.
                try:
                    editor, _ = self._unique_locator(BODY_SELECTORS, timeout=5)
                except Exception:
                    editor = None
                if editor is None:
                    missing.append(topic)
                    continue
                token = _topic_token(topic)
                try:
                    chips = [_topic_token(item) for item in self._topic_chip_texts()]
                except Exception:
                    chips = []
                # Exact chip comparison here: a near-miss chip (#草本 vs #草木)
                # must not be mistaken for this tag and skipped.
                if chips.count(token) > claimed.count(token):
                    self.topic_modes.append("chip")
                    claimed.append(token)
                    continue
                try:
                    if _topic_token_in_text(token, self._plain_editor_text(editor)):
                        # Plain text from an earlier pass is already on the page.
                        self.topic_modes.append("text")
                        continue
                except Exception:
                    pass
                if not self._focus_editor_end(editor):
                    missing.append(topic)
                    continue
                self._ensure_editor_separator(editor, force=True)
                mode = self._select_topic_or_fallback(editor, topic)
                if mode and mode != "text":
                    self.topic_modes.append("chip")
                    claimed.append(_topic_token(mode))
                elif mode == "text":
                    self.topic_modes.append("text")
                else:
                    self.topic_modes.append("")
                self.humanizer.step_pause(reason="\u8bdd\u9898\u8f93\u5165")
                if not mode:
                    missing.append(topic)
            pending = missing
        self.topic_missing = [_topic_token(t) for t in pending]
        if self.topic_missing:
            self._progress("form.topics.missing", missing=list(self.topic_missing))
        self.topic_mode = "text" if "text" in self.topic_modes else "chip"
        return self.topic_mode

    def _editor_text(self, editor):
        try:
            return str(editor.evaluate(
                "el => ('value' in el ? el.value : el.innerText)") or "")
        except Exception:
            return ""

    def _focus_editor_end(self, editor):
        focused = False
        try:
            result = self.humanizer.click(
                self._page, editor, reason="\u4f5c\u54c1\u63cf\u8ff0")
            focused = bool(result.get("ok"))
        except Exception:
            focused = False
        if not focused:
            try:
                editor.focus()
                focused = True
            except Exception:
                focused = False
        if not focused:
            return False
        try:
            self._page.keyboard.press("Control+End")
        except Exception:
            pass
        return True

    def _ensure_editor_separator(self, editor, *, force=False):
        text = self._editor_text(editor)
        if force and (not text or not text[-1].isspace()):
            self._page.keyboard.insert_text(" ")
        elif text and not text[-1].isspace():
            self._page.keyboard.insert_text(" ")

    def _topic_conversion_timeout(self):
        try:
            return max(0.0, float(self.settings.get(
                "topic_conversion_timeout_seconds",
                DEFAULT_TOPIC_CONVERSION_SECONDS)))
        except (TypeError, ValueError):
            return DEFAULT_TOPIC_CONVERSION_SECONDS

    def _topic_chip_count(self, editor):
        try:
            value = editor.evaluate(
                "el => (el.matches('[data-mention], [data-topic]') ? 1 : 0)"
                " + el.querySelectorAll('[data-mention], [data-topic]').length")
            return int(value or 0)
        except Exception:
            return None

    def _wait_for_topic_chip(self, editor, before_count):
        deadline = time.monotonic() + self._topic_conversion_timeout()
        while True:
            count = self._topic_chip_count(editor)
            if count is not None and count > before_count:
                return True
            if time.monotonic() >= deadline:
                return False
            self.humanizer.poll_pause()

    def _select_topic_or_fallback(self, editor, topic):
        """Type the tag by hand ("#name") and commit it with Space or Enter."""
        text = _topic_token(topic) or str(topic or "").strip()
        if not text:
            return ""
        try:
            before = self._topic_chip_texts()
        except Exception:
            before = []
        self.humanizer.type_into(self._page, text)
        try:
            self._page.keyboard.press("Space")
        except Exception:
            pass
        chip = self._wait_topic_chip(editor, topic, before)
        if chip:
            return chip
        # The text is still raw, so Enter may be the only way to convert it.
        try:
            still_typing = _topic_token_in_text(text, self._plain_editor_text(editor))
        except Exception:
            still_typing = False
        if still_typing:
            try:
                editor.press("Enter")
            except Exception:
                still_typing = False
            if still_typing:
                chip = self._wait_topic_chip(editor, topic, before)
                if chip:
                    return chip
        try:
            return "text" if _topic_token_in_text(text, self._plain_editor_text(editor)) else ""
        except Exception:
            return ""

    def _cover_wait_seconds(self):
        try:
            return max(0.2, float(getattr(self, "_cover_timeout", 10.0)))
        except (TypeError, ValueError):
            return 10.0

    def _cover_panel_wait_seconds(self):
        settings = getattr(self, "settings", None) or {}
        raw = settings.get("cover_panel_timeout_seconds")
        if raw is None:
            override = getattr(self, "_cover_timeout", None)
            if override is not None:
                try:
                    override_value = float(override)
                except (TypeError, ValueError):
                    override_value = COVER_PANEL_WAIT_SECONDS
                if override_value < COVER_PANEL_WAIT_SECONDS:
                    raw = override_value
        if raw is None:
            raw = COVER_PANEL_WAIT_SECONDS
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = COVER_PANEL_WAIT_SECONDS
        return min(COVER_PANEL_WAIT_SECONDS, max(0.2, value))

    def _cover_check_timeout_seconds(self):
        settings = getattr(self, "settings", None) or {}
        raw = settings.get("cover_check_timeout_seconds")
        if raw is None:
            override = getattr(self, "_cover_timeout", None)
            if override is not None:
                try:
                    override_value = float(override)
                except (TypeError, ValueError):
                    override_value = COVER_CHECK_TIMEOUT_SECONDS
                if override_value < COVER_CHECK_TIMEOUT_SECONDS:
                    raw = override_value
        if raw is None:
            raw = COVER_CHECK_TIMEOUT_SECONDS
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = COVER_CHECK_TIMEOUT_SECONDS
        return min(COVER_CHECK_TIMEOUT_SECONDS, max(0.2, value))

    def _cover_check_control_wait_seconds(self):
        """Wait for a late-rendered check control, capped at 20 seconds."""
        settings = getattr(self, "settings", None) or {}
        raw = settings.get("cover_check_control_timeout_seconds")
        if raw is None:
            override = getattr(self, "_cover_timeout", None)
            if override is not None:
                try:
                    raw = min(COVER_CHECK_CONTROL_WAIT_SECONDS,
                              max(0.2, float(override)))
                except (TypeError, ValueError):
                    raw = COVER_CHECK_CONTROL_WAIT_SECONDS
        if raw is None:
            raw = COVER_CHECK_CONTROL_WAIT_SECONDS
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = COVER_CHECK_CONTROL_WAIT_SECONDS
        return min(COVER_CHECK_CONTROL_WAIT_SECONDS, max(0.2, value))

    def _cover_frame_wait_seconds(self):
        """Wait for the panel's recommended frames to render (max 90s)."""
        settings = getattr(self, "settings", None) or {}
        raw = settings.get("cover_frame_timeout_seconds")
        if raw is None:
            raw = getattr(self, "_cover_frame_timeout",
                          COVER_FRAME_SELECT_WAIT_SECONDS)
        override = getattr(self, "_cover_timeout", None)
        if override is not None and raw is not None:
            try:
                raw = min(float(raw), max(0.2, float(override)))
            except (TypeError, ValueError):
                pass
        if raw is None:
            raw = COVER_FRAME_SELECT_WAIT_SECONDS
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = COVER_FRAME_SELECT_WAIT_SECONDS
        return min(COVER_FRAME_SELECT_WAIT_SECONDS, max(0.2, value))

    def _cover_warn(self, reason):
        self._cover_warning = str(reason)
        try:
            self._safe_record("cover", note="WARN: " + self._cover_warning,
                              extra={"warn": True})
        except Exception:
            pass

    def _cover_eval_timeout_ms(self):
        # Cover dialogs can keep the renderer busy while thumbnails load.
        # Every cover DOM read is bounded so the publish loop can report a
        # clear failure instead of blocking on an unbounded Page.evaluate().
        return int(min(3.0, max(0.5, self._cover_wait_seconds())) * 1000)

    def _cover_dom(self, expression, arg=None, *, timeout_ms=None):
        return self._page.locator("body").evaluate(
            expression, arg,
            timeout=(self._cover_eval_timeout_ms() if timeout_ms is None
                     else max(100, int(timeout_ms))))

    def _cover_dom_handle(self, expression, arg=None):
        return self._page.locator("body").evaluate_handle(
            expression, arg, timeout=self._cover_eval_timeout_ms())

    def _cover_image_sources(self, root=None, *, exclude_dialogs=False,
                             timeout_ms=None, as_list=False):
        """Collect every visible cover image source, not just ``img.src``.

        Live cover previews and recommended frames are DOM elements with a
        ``background-image``.  Looking only at ``<img>`` was why 1.2.103
        reported ``new_srcs=0`` even when the upload had visibly succeeded.
        """
        script = r"""(root, options) => {
          const visible = el => {
            if (!el || !el.getBoundingClientRect) return false;
            const style = window.getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && !!(el.offsetWidth || el.offsetHeight ||
                      el.getClientRects().length);
          };
          const inDialog = el => {
            for (let node = el; node && node !== document.body;
                 node = node.parentElement) {
              const role = String(node.getAttribute('role') || '')
                  .toLowerCase();
              const klass = String(node.getAttribute('class') || '')
                  .toLowerCase();
              if (role === 'dialog' || klass.indexOf('modal') >= 0 ||
                  klass.indexOf('dialog') >= 0) {
                return true;
              }
            }
            return false;
          };
          const urls = [];
          const addBackground = value => {
            const text = String(value || '');
            if (!text || text === 'none') return;
            const pattern = /url\((?:"|')?(.*?)(?:"|')?\)/g;
            let match = null;
            while ((match = pattern.exec(text))) {
              const src = String(match[1] || '').trim();
              if (src) urls.push(src);
            }
          };
          const nodes = [];
          if (root && root.nodeType === 1) nodes.push(root);
          if (root && root.querySelectorAll) {
            nodes.push.apply(nodes, Array.from(root.querySelectorAll('*')));
          }
          for (const el of nodes) {
            if (!el || el.nodeType !== 1 || !visible(el)) continue;
            if (options && options.excludeDialogs && inDialog(el)) continue;
            const rect = el.getBoundingClientRect();
            const width = rect.width || el.clientWidth ||
                          Number(el.getAttribute('width') || 0);
            const height = rect.height || el.clientHeight ||
                           Number(el.getAttribute('height') || 0);
            if (width < 40 || height < 40) continue;
            if (String(el.tagName || '').toLowerCase() === 'img') {
              const src = String(el.currentSrc || el.src ||
                                el.getAttribute('src') || '').trim();
              if (src) urls.push(src);
            }
            addBackground(window.getComputedStyle(el).backgroundImage);
          }
          return urls;
        }"""
        options = {"excludeDialogs": bool(exclude_dialogs)}
        try:
            if root is None:
                value = self._cover_dom(script, options,
                                        timeout_ms=timeout_ms)
            elif hasattr(root, "evaluate"):
                value = root.evaluate(script, options)
            else:
                return set()
        except Exception:
            return set()
        sources = [str(src) for src in (value or []) if str(src).strip()]
        return sources if as_list else set(sources)

    def _cover_panel_image_srcs(self, panel=None, *, timeout_ms=None):
        panel = panel or self._cover_panel_handle()
        if panel is None:
            return []
        return sorted(self._cover_image_sources(panel, timeout_ms=timeout_ms))

    def _cover_panel_visible(self):
        return self._cover_panel_handle() is not None

    def _cover_lookup(self, text, *, scope="auto", log=True):
        """Find a cover label document-wide and report the old scoped count.

        1.2.110: the live page keeps ``封面检测`` in the document while the
        resolved panel handle does not contain it.  The actual lookup is
        therefore always document-wide; ``scoped`` exists only as a
        diagnostic so the next failure is obvious at a glance.
        """
        label = str(text or "")
        panel = None
        if isinstance(scope, str) and scope == "auto":
            try:
                panel = self._cover_panel_handle()
            except Exception:
                panel = None
        elif scope is not None:
            panel = scope

        global_count = 0
        visible_count = 0
        scoped_count = 0
        hits = []
        errors = []
        frames_checked = 0
        for frame_index, frame in enumerate(_page_frames(self._page)):
            if not _cover_frame_usable(frame):
                continue
            frames_checked += 1
            result = _cover_text_matches(frame, label)
            if result.get("error"):
                errors.append(str(result.get("error")))
                continue
            global_count += int(result.get("total") or 0)
            items = list(result.get("items") or [])
            visible_count += len(items)
            hits.extend((frame_index, item) for item in items)

        if panel is not None:
            scoped = _cover_text_matches(panel, label)
            if not scoped.get("error"):
                scoped_count = int(scoped.get("total") or 0)

        lookup = {"text": label,
                  "global": max(0, global_count),
                  "scoped": max(0, scoped_count),
                  "visible": max(0, visible_count),
                  "hits": hits,
                  "frames": frames_checked,
                  "error": "; ".join(errors[:2])}
        if log:
            self._progress("cover.lookup",
                           text=label,
                           scoped=lookup["scoped"],
                           visible=lookup["visible"],
                           frames=frames_checked,
                           error=lookup["error"] or None,
                           **{"global": lookup["global"]})
        return lookup

    def _cover_page_text(self, limit=4000):
        """Visible body text from the main document and every child frame."""
        chunks = []
        for frame in _page_frames(self._page):
            if not _cover_frame_usable(frame):
                continue
            try:
                text = frame.locator("body").inner_text(
                    timeout=self._cover_eval_timeout_ms())
            except Exception:
                continue
            if text:
                chunks.append(str(text))
        return "\n".join(chunks)[:max(1, int(limit))]

    def _cover_close_wait_seconds(self):
        try:
            value = float(getattr(self, "_cover_close_wait",
                                   COVER_DONE_CLOSE_WAIT_SECONDS))
        except (TypeError, ValueError):
            value = COVER_DONE_CLOSE_WAIT_SECONDS
        return max(0.2, value)

    def _cover_panel_state(self):
        """Return modal/select-cover visibility used to decide a close.

        1.2.95 treats the panel as closed when every ``[class*=modal-]``
        element is invisible or when the ``选择封面`` copy is gone.  Both raw
        counts are kept for diagnostics because the live page leaves hidden
        modal nodes in the DOM after a successful close.
        """
        # selector-ok: read-only
        script = """() => {
          const visible = el => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && !!(el.offsetWidth || el.offsetHeight ||
                      el.getClientRects().length);
          };
          const modals = Array.from(
              document.querySelectorAll('[class*=modal-]')).filter(visible);
          const text = String(document.body ? document.body.innerText : '')
              .replace(/\\s+/g, '');
          return {modal_visible: modals.length,
                  select_visible:
                      text.indexOf('\u9009\u62e9\u5c01\u9762') >= 0 ? 1 : 0,
                  preview_visible:
                      text.indexOf('\u7ad6\u5c01\u9762\u9884\u89c8') >= 0
                          ? 1 : 0};
        }"""
        modal_visible = 0
        select_visible = 0
        preview_visible = 0
        read_frames = 0
        frames = [frame for frame in _page_frames(self._page)
                  if _cover_frame_usable(frame)]
        for frame in frames:
            try:
                value = frame.locator("body").evaluate(
                    script, timeout=self._cover_eval_timeout_ms())
            except Exception:
                continue
            if not isinstance(value, dict):
                continue
            read_frames += 1
            try:
                modal_visible += int(value.get("modal_visible") or 0)
            except (TypeError, ValueError):
                pass
            try:
                select_visible = max(select_visible,
                                     int(value.get("select_visible") or 0))
            except (TypeError, ValueError):
                pass
            try:
                preview_visible = max(
                    preview_visible,
                    int(value.get("preview_visible") or 0))
            except (TypeError, ValueError):
                pass
        return {"modal_visible": modal_visible,
                "select_visible": select_visible,
                "preview_visible": preview_visible,
                "read_ok": bool(frames) and read_frames == len(frames),
                "closed": modal_visible == 0 or select_visible == 0}

    def _cover_panel_closed(self):
        state = self._cover_panel_state()
        if not state.get("read_ok", False):
            return False
        preview_gone = (getattr(self, "_cover_done_preview_gone", False)
                        and not state.get("preview_visible"))
        # Some forms retain their entry link after closing the editor. Keep
        # the positive result of this done click; never veto it using a
        # freshly resolved (potentially stale) panel_visible/container.
        confirmed_close = (getattr(self, "_cover_done_closed_ok", False)
                           and not state.get("preview_visible")
                           and state.get("modal_visible") == 0)
        return bool(preview_gone or confirmed_close or
                    (state.get("select_visible") == 0
                     and state.get("modal_visible") == 0))

    def _cover_done_closed(self, preview_before=False, panel_before=None):
        """Read back done: preview text disappeared or panel root is hidden."""
        state = self._cover_panel_state()
        try:
            panel = (panel_before if panel_before is not None
                     else self._cover_panel_handle())
            panel_visible = panel is not None and panel.is_visible()
        except Exception:
            panel_visible = True
        preview_visible = bool(state.get("preview_visible"))
        preview_gone = bool(preview_before and state.get("read_ok", False)
                            and not preview_visible)
        closed = (panel_before is not None and not panel_visible) or preview_gone
        return {"closed": closed,
                "panel_visible": panel_visible,
                "preview_visible": preview_visible,
                "preview_gone": preview_gone}

    def _cover_panel_handle(self):
        """Locate the cover dialog in the main document or any child frame."""
        expression = """(body, markers) => {
              const clean = value => String(value || '')
                  .replace(/\\s+/g, '').replace(/\\u200b/g, '');
              const visible = el => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && style.opacity !== '0'
                    && !!(el.offsetWidth || el.offsetHeight ||
                          el.getClientRects().length);
              };
              const wanted = new Set(markers);
              let best = null;
              let bestHits = 0;
              let bestLength = Infinity;
              const walker = document.createTreeWalker(
                  document.body, NodeFilter.SHOW_TEXT);
              while (walker.nextNode()) {
                const labelText = clean(walker.currentNode.nodeValue);
                if (!labelText) continue;
                let el = walker.currentNode.parentElement;
                let matched = null;
                for (let depth = 0; el && el !== document.body && depth < 3;
                     depth += 1, el = el.parentElement) {
                  if (wanted.has(clean(el.textContent))) {
                    matched = el;
                    break;
                  }
                }
                if (!matched || !visible(matched)) continue;
                for (let node = matched.parentElement;
                     node && node !== document.body; node = node.parentElement) {
                  const text = clean(node.textContent);
                  if (text.length > 20000) break;
                  let hits = 0;
                  for (const marker of markers) {
                    if (text.indexOf(marker) >= 0) hits += 1;
                  }
                  if (hits < 2) continue;
                  const klass = String(node.className || '').toLowerCase();
                  const role = String(node.getAttribute('role') || '').toLowerCase();
                  const modalish = role === 'dialog' ||
                      klass.indexOf('modal') >= 0 ||
                      klass.indexOf('dialog') >= 0 ||
                      klass.indexOf('wrap') >= 0;
                  if (modalish) return node;
                  if (hits > bestHits ||
                      (hits === bestHits && text.length < bestLength)) {
                    best = node;
                    bestHits = hits;
                    bestLength = text.length;
                  }
                }
              }
              return best;
            }"""
        markers = list(COVER_PANEL_TEXTS)
        timeout = self._cover_eval_timeout_ms()
        frames = _page_frames(self._page)
        for index, frame in enumerate(frames):
            if not _cover_frame_usable(frame):
                continue
            try:
                if index == 0:
                    handle = self._cover_dom_handle(expression, markers)
                else:
                    handle = frame.locator("body").evaluate_handle(
                        expression, markers, timeout=timeout)
                panel = handle.as_element()
            except Exception:
                continue
            if panel is not None:
                return panel
        return None

    def _cover_panel_text(self, limit=300):
        """Visible panel text for stage diagnostics, bounded by ``limit``."""
        panel = self._cover_panel_handle()
        if panel is None:
            return ""
        try:
            text = panel.inner_text()
        except Exception:
            return ""
        text = _normalize_form_text(text)
        return text[:max(1, int(limit))]

    def _cover_panel_contains(self, markers):
        text = self._cover_panel_text(limit=4000)
        return any(str(marker) in text for marker in markers)

    def _cover_panel_click_report(self, texts):
        candidates = list(dict.fromkeys(str(text) for text in texts if str(text)))
        report = _cover_control_click(self._page, candidates)
        return {"ok": bool(report.get("ok")),
                "clicked": str(report.get("clicked") or ""),
                "counts": report.get("counts") or
                {text: 0 for text in candidates},
                "frames": int(report.get("frames") or 0),
                "frame": report.get("frame"),
                "attempts": int(report.get("attempts") or 0),
                "reason": report.get("reason") or ""}

    def _cover_panel_click(self, texts):
        return bool(self._cover_panel_click_report(texts).get("ok"))

    def _cover_act(self, action, result):
        """Record one cover operation with element-click evidence."""
        result = result or {}
        self._progress("cover.act",
                       action=str(action),
                       via=str(result.get("via") or "js"),
                       hit=bool(result.get("hit")),
                       rect=result.get("rect") or {})
        return result

    def _cover_profile_cover_count(self):
        """Visible ``profileCover`` nodes currently mounted in every frame."""
        # selector-ok: read-only
        expression = r"""() => {
          const visible = el => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && !!(el.offsetWidth || el.offsetHeight ||
                      el.getClientRects().length);
          };
          return Array.from(
              document.querySelectorAll('div[class*="profileCover"]')
          ).filter(visible).length;
        }"""
        total = 0
        timeout = self._cover_eval_timeout_ms()
        for frame in _page_frames(self._page):
            if not _cover_frame_usable(frame):
                continue
            try:
                value = frame.locator("body").evaluate(
                    expression, timeout=timeout)
            except Exception:
                continue
            if isinstance(value, bool):
                continue
            try:
                total += max(0, int(value or 0))
            except (TypeError, ValueError):
                continue
        return total

    def _cover_panel_ready(self):
        """True when the cover view exposes frames or a real action control."""
        if self._cover_profile_cover_count() > 0:
            return True
        try:
            scan = _cover_control_scan(
                self._page, COVER_PANEL_READY_TEXTS, attempts=1,
                retry_ms=0, timeout=0.0)
        except Exception as exc:
            self._progress("cover.panel.probe_error",
                           error=type(exc).__name__ + ": " + str(exc))
            return False
        return any(int(value or 0) > 0
                   for value in (scan.get("counts") or {}).values())

    def _cover_panel_choice_handles(self, panel=None):
        """Visible ``选择封面`` controls inside the cover panel, vertical first."""
        panel = panel if panel is not None else self._cover_panel_handle()
        if panel is None:
            return []
        expression = r"""(root, labels) => {
          const clean = value => String(value || '')
              .replace(/\s+/g, '').replace(/\u200b/g, '');
          const visible = el => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && !!(el.offsetWidth || el.offsetHeight ||
                      el.getClientRects().length);
          };
          const wanted = new Set(labels.map(clean));
          const found = [];
          for (const el of root.querySelectorAll('*')) {
            if (!visible(el) || !wanted.has(clean(el.textContent))) continue;
            let childMatch = false;
            for (const child of el.children) {
              if (visible(child) &&
                  wanted.has(clean(child.textContent))) {
                childMatch = true;
                break;
              }
            }
            if (childMatch) continue;
            let context = '';
            for (let node = el.parentElement, depth = 0;
                 node && node !== root && depth < 3;
                 node = node.parentElement, depth += 1) {
              const text = clean(node.textContent);
              if (text && text.length <= 300) {
                context = text;
                break;
              }
            }
            let rank = 1;
            if (/竖封面|3:4/.test(context)) rank = 0;
            else if (/横封面|4:3/.test(context)) rank = 2;
            found.push({el: el, rank: rank, order: found.length});
          }
          found.sort((left, right) => (left.rank - right.rank) ||
                                      (left.order - right.order));
          return found.map(item => item.el);
        }"""
        try:
            handle = panel.evaluate_handle(expression,
                                           list(COVER_SELECT_TEXTS))
            count = int(handle.evaluate("items => items.length") or 0)
        except Exception:
            return []
        result = []
        for index in range(max(0, count)):
            try:
                item = handle.evaluate_handle(
                    "(items, index) => items[index]", index).as_element()
            except Exception:
                continue
            if item is not None:
                result.append(item)
        return result

    def _open_cover_panel(self, *, fail_closed=True):
        started = time.monotonic()
        candidates = tuple(COVER_ENTRY_TEXTS)
        counts = _cover_control_scan(
            self._page, candidates, attempts=1, retry_ms=0,
            timeout=0.0)["counts"]
        timeout_ms = min(COVER_ENTRY_CLICK_TIMEOUT_MS,
                         max(500, int(self._cover_wait_seconds() * 1000)))
        profile_covers = self._cover_profile_cover_count()
        clicked = ""
        clicked_frame = None
        attempts = 0
        mouse_fallback_used = False

        def success():
            self._progress("cover.panel.open",
                           candidates=counts,
                           clicked=clicked or None,
                           clicked_frame=clicked_frame,
                           panel_visible=True,
                           profile_covers=profile_covers,
                           wait_seconds=self._cover_panel_wait_seconds(),
                           elapsed=round(time.monotonic() - started, 2))
            self._progress("cover.panel.open.result", ok=True,
                           reason=None, attempts=attempts,
                           profile_covers=profile_covers,
                           elapsed=round(time.monotonic() - started, 2))
            return True

        if profile_covers > 0:
            self._progress("cover.panel.entry", clicked=None,
                           candidates=counts,
                           profile_covers_after=profile_covers)
            self._progress("cover.frame.view",
                           profile_covers=profile_covers)
            return success()

        panel = self._cover_panel_handle()
        panel_ready = self._cover_panel_ready()
        choices = self._cover_panel_choice_handles(panel)
        if not choices and (panel is None or not panel_ready):
            for text in candidates:
                attempt = _cover_entry_click(self._page, text,
                                             timeout_ms=timeout_ms)
                self._cover_act(text, attempt)
                if not attempt.get("found"):
                    continue
                clicked = str(attempt.get("clicked") or "")
                clicked_frame = attempt.get("frame")
                mouse_fallback_used = attempt.get("via") == "mouse"
                # A hit on the highest-priority label ends the search even
                # when the element click itself failed.  Do not
                # silently open a different dialog.
                break
            if clicked or clicked_frame is not None:
                _pause_page(self._page, COVER_PANEL_SETTLE_MS)
            profile_covers = self._cover_profile_cover_count()
            self._progress("cover.panel.entry", clicked=clicked or None,
                           candidates=counts,
                           profile_covers_after=profile_covers)
            panel = self._cover_panel_handle()
            panel_ready = self._cover_panel_ready()
            choices = self._cover_panel_choice_handles(panel)
        else:
            self._progress("cover.panel.entry", clicked=None,
                           candidates=counts,
                           profile_covers_after=profile_covers)

        if profile_covers > 0:
            self._progress("cover.frame.view",
                           profile_covers=profile_covers)
            return success()

        retry_ms = int(COVER_FRAME_VIEW_RETRY_SECONDS * 1000)
        while (choices and attempts < COVER_FRAME_VIEW_RETRY_ATTEMPTS
               and not mouse_fallback_used):
            index = attempts % len(choices)
            attempts += 1
            click = _cover_click_element(choices[index])
            self._cover_act(COVER_SELECT_TEXTS[0], click)
            if click.get("ok"):
                clicked = COVER_SELECT_TEXTS[0]
            _pause_page(self._page, retry_ms)
            profile_covers = self._cover_profile_cover_count()
            self._progress("cover.panel.entry",
                           clicked=(COVER_SELECT_TEXTS[0]
                                    if click.get("ok") else None),
                           candidates=counts,
                           profile_covers_after=profile_covers,
                           attempt=attempts,
                           click_mode=click.get("mode") or None)
            if profile_covers > 0:
                self._progress("cover.frame.view",
                               profile_covers=profile_covers)
                return success()
            panel = self._cover_panel_handle()
            choices = self._cover_panel_choice_handles(panel)
            if click.get("via") == "mouse":
                break

        self._progress("cover.panel.open",
                       candidates=counts,
                       clicked=clicked or None,
                       clicked_frame=clicked_frame,
                       panel_visible=panel is not None,
                       profile_covers=profile_covers,
                       wait_seconds=self._cover_panel_wait_seconds(),
                       elapsed=round(time.monotonic() - started, 2))
        if attempts:
            reason = "no_frame_view"
            self._progress("cover.panel.open.result", ok=False,
                           reason=reason, attempts=attempts,
                           profile_covers=profile_covers,
                           elapsed=round(time.monotonic() - started, 2))
            self._cover_warning = reason
            if fail_closed:
                self._fail("cover.no_frame_view: \u5c01\u9762\u9009\u5e27\u89c6\u56fe\u672a\u6253\u5f00",
                           step="cover", code=reason)
            return False

        if not choices and panel_ready and panel is not None:
            # Upload/AI tabs do not expose a frame grid until they are used.
            # Returning here keeps that route available; the frame selector
            # still polls and fails closed when no frame ever appears.
            return success()

        self._progress("cover.panel.open.result", ok=False,
                       reason="panel_unavailable", attempts=attempts,
                       profile_covers=profile_covers,
                       elapsed=round(time.monotonic() - started, 2))
        self._progress("cover.panel.open.unavailable",
                       code="cover.panel_unavailable",
                       elapsed=round(time.monotonic() - started, 2))
        self._cover_warning = "cover.panel_unavailable"
        if fail_closed:
            self._fail("cover.panel_unavailable: \u5c01\u9762\u9762\u677f\u63a7\u4ef6\u672a\u51fa\u73b0",
                       step="cover", code="cover.panel_unavailable")
        return False

    def _cover_check_text(self):
        """Document-wide visible result text, fixed control labels removed."""
        text = self._cover_page_text(limit=4000)
        controls = (set(COVER_DONE_TEXTS) | set(COVER_CHECK_TEXTS)
                    | set(COVER_CHECK_RECHECK_TEXTS) | {
            "\u91cd\u65b0\u68c0\u6d4b", "\u68c0\u6d4b\u4e2d", "\u4e0a\u4f20\u5c01\u9762",
            "AI\u5c01\u9762", "AI\u751f\u6210\u5c01\u9762",
            "Ai\u667a\u80fd\u63a8\u8350\u5c01\u9762", "\u6a2a\u5c01\u97624:3",
            "\u7ad6\u5c01\u97623:4", "\u8bbe\u7f6e\u7ad6\u5c01\u9762",
            "\u8bbe\u7f6e\u6a2a\u5c01\u9762",
        })
        raw_lines = [line for line in str(text or "").replace(
            "\r", "\n").split("\n") if _normalize_form_text(line)]
        # A lone "完成" can be the result text itself.  When it appears with
        # other panel text it is the fixed done button and must not count.
        if len(raw_lines) == 1:
            only = _normalize_form_text(raw_lines[0])
            if only in COVER_DONE_TEXTS:
                return only
        lines = []
        for line in raw_lines:
            line = _normalize_form_text(line)
            if line and line not in controls:
                lines.append(line)
        return " ".join(lines)

    def _cover_check_status(self):
        text = self._cover_check_text()
        # "无异常" is a pass phrase and must not trip the bare "异常" marker.
        fail_probe = text.replace("\u65e0\u5f02\u5e38", "")
        if any(marker in fail_probe for marker in COVER_CHECK_FAIL_TEXTS):
            return "failed"
        if any(marker in text for marker in COVER_CHECK_OK_TEXTS):
            return "ok"
        return "pending"

    def _cover_check_scan(self, texts, *, timeout=0.0):
        """Scan every document for exact control labels; never panel-only."""
        candidates = list(dict.fromkeys(str(text) for text in texts if str(text)))
        counts = {text: 0 for text in candidates}
        global_counts = {text: 0 for text in candidates}
        scoped_counts = {text: 0 for text in candidates}
        hits = []
        attempts = 0
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            attempts += 1
            counts = {text: 0 for text in candidates}
            global_counts = {text: 0 for text in candidates}
            scoped_counts = {text: 0 for text in candidates}
            hits = []
            try:
                panel = self._cover_panel_handle()
            except Exception:
                panel = None
            for text in candidates:
                lookup = self._cover_lookup(text, scope=panel, log=True)
                counts[text] = int(lookup.get("visible") or 0)
                global_counts[text] = int(lookup.get("global") or 0)
                scoped_here = int(lookup.get("scoped") or 0)
                scoped_counts[text] = scoped_here
                if (text in COVER_CHECK_RECHECK_TEXTS and panel is not None
                        and scoped_here <= 0):
                    # The 发文助手 quick-check panel also renders 重新检测.
                    # A document-wide hit there is NOT proof that the cover
                    # check was already clicked; treating it as such skipped
                    # the real click and always ended in cover.detect timeout.
                    self._progress("cover.check.recheck_ignored",
                                   text=text, scoped=scoped_here,
                                   visible=int(lookup.get("visible") or 0))
                    continue
                for frame_index, item in lookup.get("hits") or []:
                    hits.append((text, frame_index, item))
            if hits:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._upload_heartbeat()
            time.sleep(min(COVER_CHECK_POLL_INTERVAL_SECONDS, remaining))
        return {"counts": counts, "global_counts": global_counts,
                "scoped_counts": scoped_counts, "hits": hits,
                "attempts": attempts}

    def _cover_detect_result(self, result, elapsed, **fields):
        self._progress("cover.detect.result", result=result,
                       elapsed=round(float(elapsed), 1), **fields)

    def _cover_activate_vertical_view(self):
        """Click ``设置竖封面`` once so the detection control can render."""
        for text in COVER_VERTICAL_ENTRY_TEXTS:
            report = _cover_control_click(
                self._page, (text,), attempts=1, retry_ms=0, timeout=0.0)
            self._cover_act(text, report)
            if report.get("ok") or report.get("via") == "mouse":
                return {"ok": bool(report.get("ok")), "clicked": text,
                        "frame": report.get("frame"),
                        "mode": report.get("mode"),
                        "via": report.get("via"),
                        "hit": report.get("hit"),
                        "rect": report.get("rect") or {},
                        "counts": report.get("counts") or {}}
        return {"ok": False, "clicked": None, "via": "js", "hit": False,
                "rect": {},
                "reason": "settings text not found"}

    def _cover_modal_visible(self, timeout=0.0):
        """True when the 设置封面 modal (with the frame timeline) is open."""
        end = time.monotonic() + max(0.0, float(timeout))
        # selector-ok: read-only
        probe = ("() => [...document.querySelectorAll("
                 "\"div[class*=modal]\")].some(e => e.offsetParent && "
                 "/\u8bbe\u7f6e\u7ad6\u5c01\u9762/.test(e.innerText||\"\"))")
        while True:
            try:
                if self._page.evaluate(probe):
                    return True
            except Exception:
                pass
            if time.monotonic() >= end:
                return False
            # wait-ok: poll
            time.sleep(0.4)

    def _cover_modal_open(self):
        if self._cover_modal_visible():
            return True
        for label in ("\u9009\u62e9\u5c01\u9762", "\u8bbe\u7f6e\u5c01\u9762",
                      "\u7f16\u8f91\u5c01\u9762"):
            try:
                _cover_entry_click(self._page, label, timeout_ms=6000)
            except Exception:
                pass
            if self._cover_modal_visible(timeout=25.0):
                return True
        return self._cover_modal_visible(timeout=1.0)

    def _cover_modal_text(self):
        """Whole-page visible text.

        The cover verdict is rendered in a fixed-position portal, whose
        offsetParent is None, so scoping to the modal node or filtering by
        offsetParent loses it. The publish page body is small (~1k chars).
        """
        try:
            return str(self._page.evaluate(
                "() => (document.body.innerText || '').slice(0, 8000)") or "")
        except Exception:
            return ""

    def _cover_modal_check(self):
        """Click 封面检测/重新检测 inside the modal and wait for the verdict."""
        clicked = False
        for text in ("\u5c01\u9762\u68c0\u6d4b", "\u91cd\u65b0\u68c0\u6d4b"):
            try:
                loc = self._page.get_by_text(text, exact=True)
                if loc.count():
                    loc.last.click(timeout=3000)
                    clicked = True
                    break
            except Exception:
                continue
        text = ""
        deadline = time.monotonic() + COVER_CHECK_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            text = self._cover_modal_text()
            for marker in COVER_MODAL_FAIL_TEXTS:
                if marker in text:
                    return "failed", text
            for marker in COVER_MODAL_PASS_TEXTS:
                if marker in text:
                    return "passed", text
            self._upload_heartbeat()
            # wait-ok: poll
            time.sleep(0.5)
        if not clicked:
            return "missing", text
        if "\u68c0\u6d4b\u4e2d" in text:
            return "pending", text
        return "missing", text

    def _wait_cover_frame_selected(self, box, timeout_ms=2000):
        """Wait until the clicked frame reports a selection marker.

        Replaces the fixed 0.8 s pause: on a slow machine detection began while
        the previous frame was still the selected one. The probe only reads the
        DOM and is bounded, so a site change cannot hang the publish.
        """
        # selector-ok: read-only
        probe = ("() => { const w=[...document.querySelectorAll("
                 "\"[class*=preview-frames]\")].find(e => e.offsetParent); "
                 "if(!w) return false; return !![...w.querySelectorAll("
                 "'[aria-selected=\"true\"],[class*=selected],[class*=active]')]"
                 ".find(e => e.offsetParent); }")
        for _ in range(max(1, int(timeout_ms / 100))):
            try:
                if self._page.evaluate(probe):
                    return True
            except Exception:
                pass
            try:
                self._page.wait_for_timeout(100)
            except Exception:
                break
        return False

    def _cover_modal_pick(self, ratio=0.0):
        """Pick a frame on the modal timeline and run 封面检测.

        1.2.126: the frames shown on the publish page are decorative
        (``pointer-events: none``) and never react to clicks.  The only
        working path is: open 设置封面 -> click the timeline -> check.
        """
        if not self._cover_modal_open():
            return {"result": "missing", "reason": "cover_modal_not_open"}
        box = None
        try:
            # selector-ok: read-only
            box = self._page.evaluate(
                "() => { const w=[...document.querySelectorAll("
                "\"[class*=preview-frames]\")].find(e => e.offsetParent); "
                "if (!w) return null; const r=w.getBoundingClientRect(); "
                "return [r.x, r.y, r.width, r.height]; }")
        except Exception:
            box = None
        if box:
            frac = float(ratio or 0.0)
            if frac <= 0.0 or frac >= 1.0:
                frac = COVER_MODAL_FRAME_RATIO
            x = box[0] + box[2] * frac
            y = box[1] + box[3] / 2.0
            try:
                self._page.mouse.move(x, y)
                self._page.mouse.down()
                self._page.mouse.up()
                self._wait_cover_frame_selected(box)
            except Exception:
                pass
        status, text = self._cover_modal_check()
        try:
            self._progress("cover.modal.result", result=status,
                           opened=bool(self._cover_modal_visible()),
                           has_track=bool(box), text=str(text)[-120:])
        except Exception:
            pass
        # Leave the modal open on purpose: the caller's 完成/readback
        # sequence owns closing it.  Clicking 完成 here makes that sequence
        # find no button (candidates {完成: 0}) and abort the publish.
        return {"result": status, "text": text[-200:]}

    def _run_cover_check(self):
        """Click a late-rendered cover check and wait up to 60s for a result."""
        def result_base():
            return {"clicked": clicked or None,
                    "counts": dict(counts),
                    "attempts": attempts,
                    "already_clicked": bool(already_clicked)}

        def click_hits(hits):
            last = None
            for text, _frame_index, item in hits:
                last = _cover_click_element(_cover_clickable_element(item))
                if last.get("ok") or last.get("via") == "mouse":
                    return text, last
            return "", last or {"ok": False}

        control_started = time.monotonic()
        control_deadline = (control_started +
                            self._cover_check_control_wait_seconds())
        clicked = ""
        attempts = 0
        counts = {text: 0 for text in COVER_CHECK_TEXTS}
        global_counts = {text: 0 for text in COVER_CHECK_TEXTS}
        scoped_counts = {text: 0 for text in COVER_CHECK_TEXTS}
        already_clicked = False
        executed = False
        click_errors = []
        activation_rounds = 0
        activation_mouse_used = False
        while True:
            status = self._cover_check_status()
            elapsed = round(time.monotonic() - control_started, 1)
            if status == "failed":
                self._progress("cover.check", result="failed",
                               elapsed=elapsed, code="cover.check_failed",
                               **result_base())
                self._cover_detect_result("failed", elapsed, **result_base())
                self._cover_fail(
                    "detect: cover.check_failed \u5c01\u9762\u68c0\u6d4b\u672a\u901a\u8fc7",
                    code="cover.check_failed")
            if status == "ok":
                already_clicked = True
                self._cover_check_ok = True
                self._progress("cover.check", result="ok", elapsed=elapsed,
                               **result_base())
                self._cover_detect_result("passed", elapsed,
                                          **result_base())
                return {"result": "passed", "status": "ok",
                        "elapsed": elapsed,
                        "clicked": None, "counts": dict(counts),
                        "global_counts": dict(global_counts),
                        "scoped_counts": dict(scoped_counts),
                        "attempts": attempts, "already_clicked": True}

            try:
                panel = self._cover_panel_handle()
            except Exception:
                panel = None
            lookup = self._cover_lookup("\u5c01\u9762\u68c0\u6d4b",
                                        scope=panel, log=True)
            if (not lookup.get("error") and
                    int(lookup.get("visible") or 0) == 0 and
                    not activation_mouse_used and
                    activation_rounds < COVER_DETECT_ACTIVATE_ROUNDS):
                activation_rounds += 1
                activation = self._cover_activate_vertical_view()
                activation_mouse_used = activation.get("via") == "mouse"
                self._progress("cover.detect.activate",
                               round=activation_rounds,
                               ok=bool(activation.get("ok")),
                               clicked=activation.get("clicked"),
                               frame=activation.get("frame"),
                               counts=activation.get("counts") or {},
                               global_count=int(lookup.get("global") or 0),
                               scoped_count=int(lookup.get("scoped") or 0),
                               visible=int(lookup.get("visible") or 0))
                if activation.get("ok"):
                    _pause_page(
                        self._page,
                        int(COVER_DETECT_ACTIVATE_WAIT_SECONDS * 1000))
                continue

            scan = self._cover_check_scan(
                tuple(COVER_CHECK_RECHECK_TEXTS) + tuple(COVER_CHECK_TEXTS))
            attempts += int(scan.get("attempts") or 0)
            scan_counts = scan.get("counts") or {}
            counts = {text: int(scan_counts.get(text) or 0)
                      for text in COVER_CHECK_TEXTS}
            scan_global = scan.get("global_counts") or {}
            scan_scoped = scan.get("scoped_counts") or {}
            global_counts = {text: int(scan_global.get(text) or 0)
                             for text in COVER_CHECK_TEXTS}
            scoped_counts = {text: int(scan_scoped.get(text) or 0)
                             for text in COVER_CHECK_TEXTS}
            hits = scan.get("hits") or []
            recheck_hits = [hit for hit in hits
                            if hit[0] in COVER_CHECK_RECHECK_TEXTS]
            check_hits = [hit for hit in hits
                          if hit[0] in COVER_CHECK_TEXTS]
            if recheck_hits:
                # The previous click already changed the label.  Never click
                # the same check twice; just wait for the result text.
                already_clicked = True
                executed = True
                break
            if check_hits:
                clicked_text, click_result = click_hits(check_hits)
                self._cover_act(clicked_text or COVER_CHECK_TEXTS[0],
                                click_result)
                if click_result.get("ok"):
                    clicked = clicked_text
                    executed = True
                    break
                click_errors.append(click_result.get("reason") or
                                    "cover check click failed")
                if click_result.get("via") == "mouse":
                    break

            remaining = control_deadline - time.monotonic()
            if remaining <= 0:
                break
            self._upload_heartbeat()
            time.sleep(min(COVER_CHECK_POLL_INTERVAL_SECONDS, remaining))

        base = result_base()
        if click_errors:
            base["errors"] = click_errors
        if not executed:
            if not click_errors:
                self._cover_act(COVER_CHECK_TEXTS[0],
                                {"via": "js", "hit": False, "rect": {}})
            self._progress("cover.check", result="missing",
                           elapsed=round(time.monotonic() - control_started, 1),
                           **base)
            self._cover_detect_result(
                "missing", time.monotonic() - control_started, **base)
            return {"result": "missing", "clicked": None,
                    "counts": dict(counts), "attempts": attempts}

        started = time.monotonic()
        deadline = started + self._cover_check_timeout_seconds()
        timeout_seconds = self._cover_check_timeout_seconds()
        while True:
            status = self._cover_check_status()
            elapsed = round(time.monotonic() - started, 1)
            if status == "failed":
                self._progress("cover.check", result="failed", elapsed=elapsed,
                               code="cover.check_failed", **base)
                self._cover_detect_result("failed", elapsed, **base)
                self._cover_fail(
                    "detect: cover.check_failed \u5c01\u9762\u68c0\u6d4b\u672a\u901a\u8fc7",
                    code="cover.check_failed")
            if status == "ok":
                self._cover_check_ok = True
                self._progress("cover.check", result="ok", elapsed=elapsed,
                               **base)
                self._cover_detect_result("passed", elapsed, **base)
                return {"result": "passed", "status": "ok",
                        "elapsed": elapsed,
                        "clicked": clicked or None, "counts": dict(counts),
                        "global_counts": dict(global_counts),
                        "scoped_counts": dict(scoped_counts),
                        "activation_rounds": activation_rounds,
                        "attempts": attempts,
                        "already_clicked": bool(already_clicked)}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._progress("cover.check", result="timeout",
                               elapsed=elapsed, timeout=timeout_seconds,
                               **base)
                self._cover_detect_result("timeout", elapsed,
                                          timeout=timeout_seconds, **base)
                return {"result": "timeout", "elapsed": elapsed,
                        "clicked": clicked or None, "counts": dict(counts),
                        "global_counts": dict(global_counts),
                        "scoped_counts": dict(scoped_counts),
                        "activation_rounds": activation_rounds,
                        "attempts": attempts,
                        "already_clicked": bool(already_clicked)}
            self._upload_heartbeat()
            time.sleep(min(COVER_CHECK_POLL_INTERVAL_SECONDS, remaining))

    def _cover_reopen_for_detect(self):
        """Close and reopen the cover panel once when the check is missing."""
        try:
            done = self._click_cover_done() or {}
        except Exception as exc:
            done = {"closed": False,
                    "reason": type(exc).__name__ + ": " + str(exc)}
        closed = bool(done.get("closed"))
        if not closed:
            try:
                closed = bool(self._cover_panel_closed())
            except Exception:
                closed = False
        if not closed:
            self._progress("cover.detect.reopen", ok=False, closed=False,
                           reason=done.get("reason") or "panel_not_closed")
            return False
        try:
            opened = bool(self._open_cover_panel(fail_closed=False))
        except Exception as exc:
            self._progress("cover.detect.reopen", ok=False, closed=True,
                           reason=type(exc).__name__ + ": " + str(exc))
            return False
        self._progress("cover.detect.reopen", ok=opened, closed=True)
        return opened

    def _cover_file_candidate(self, covers):
        paths = getattr(self, "_cover_paths", None) or {}
        for orientation, cover in (covers or {}).items():
            if not cover:
                continue
            path = paths.get(orientation)
            candidate = Path(str(path)) if path else Path(str(cover))
            if candidate.is_file():
                return str(orientation), candidate
        return "", None

    def _cover_requested_paths(self, covers):
        """Normalize the plan's cover fields without inventing a new schema.

        ``piece.covers`` is the current contract.  The two direct fields are
        accepted as compatibility input when the plan supplied no cover map;
        their values still have to resolve to a real local file.
        """
        requested = {key: value for key, value in (covers or {}).items()
                     if key in ("vertical", "horizontal") and value}
        if requested:
            return requested
        for key in ("cover_path", "cover_image"):
            value = (covers or {}).get(key)
            if value:
                return {"vertical": value}
        plan = getattr(self, "plan", None) or {}
        piece = plan.get("piece") or {}
        for source in (piece, plan):
            for key in ("cover_path", "cover_image"):
                value = source.get(key)
                if value:
                    return {"vertical": value}
        return {}

    def _cover_ai_enabled(self, cover_ai=None):
        """AI covers are opt-in: only an explicit plan value enables them."""
        if cover_ai is not None:
            return cover_ai is True
        plan = getattr(self, "plan", None) or {}
        piece = plan.get("piece") or {}
        return piece.get("cover_ai") is True or plan.get("cover_ai") is True

    def _cover_upload_enabled(self):
        """Customer-cover upload is disabled unless the plan explicitly opts in.

        The live "upload cover" button opens a native Windows file chooser.
        Playwright cannot close that chooser, so the default path must never
        click the button or touch any image input.
        """
        plan = getattr(self, "plan", None) or {}
        piece = plan.get("piece") or {}
        return piece.get("cover_upload") is True

    def _cover_upload_skip(self, covers):
        """Record the policy decision without touching the page."""
        requested = any(bool(value) for value in (covers or {}).values())
        result = {"ok": False, "attempted": False, "skipped": True,
                  "reason": "disabled_by_policy",
                  "cover_requested_but_policy_skips": requested}
        self._progress("cover.upload.result", result="skipped",
                       ok=False, attempted=False,
                       reason="disabled_by_policy",
                       cover_requested_but_policy_skips=requested)
        return result

    def _dismiss_cover_native_dialog(self):
        """Send one Escape after the panel opens; a native chooser is outside DOM.

        There is no reliable Playwright probe for a Windows file chooser that
        was left open by an earlier run.  Escape is sent once unconditionally
        and logged so the trace proves the flow did not continue while the
        chooser still held the foreground.
        """
        result = {"pressed": False, "dismissed": False, "count": 0,
                  "method": "escape", "reason": None}
        try:
            self._page.keyboard.press("Escape")
            result.update(pressed=True, dismissed=True, count=1,
                          reason="escape_sent")
        except Exception as exc:
            result["reason"] = type(exc).__name__ + ": " + str(exc)
        self._progress("cover.dialog.dismissed", **result)
        return result

    def _cover_current_srcs(self):
        srcs = set(self._cover_signature().get("srcs") or [])
        panel = self._cover_panel_handle()
        srcs.update(self._cover_panel_image_srcs(panel))
        return srcs

    def _cover_upload_file(self, covers):
        """Use the legacy file-input path only when the plan opts in."""
        started = time.monotonic()
        if not self._cover_upload_enabled():
            return self._cover_upload_skip(covers)
        orientation, candidate = self._cover_file_candidate(covers)
        self._progress("cover.upload.start",
                       orientation=orientation or None,
                       file=str(candidate) if candidate else None,
                       file_exists=candidate is not None,
                       click_upload_button=False,
                       policy="cover_upload=true")
        if candidate is None:
            self._progress("cover.upload", attempted=False,
                           reason="cover file is not available")
            self._progress("cover.upload.result", ok=False, attempted=False,
                           reason="file missing")
            return {"ok": False, "attempted": False, "reason": "file missing"}
        before = self._cover_progress_state()
        before_snapshot = self._cover_upload_snapshot()
        self._progress("cover.sources.count",
                       baseline=len(before.get("srcs") or []),
                       new=0, scope="upload",
                       frame_bg=before_snapshot.get("frame_bg") or None,
                       preview_bgs=len(before_snapshot.get("preview_bgs") or []),
                       profile_covers=before_snapshot.get("frame_count"))
        input_locator = None
        # The live panel has six hidden inputs.  The customer-cover input is
        # the image-only one; the video input and the image+video editor
        # inputs must not be chosen.
        selector = 'input[type=file][accept*="image"]:not([accept*="video"])'
        panel = self._cover_panel_handle()
        if panel is not None:
            try:
                input_locator = panel.query_selector(selector)
            except Exception:
                input_locator = None
        if input_locator is None:
            for frame in _page_frames(self._page):
                if not _cover_frame_usable(frame):
                    continue
                try:
                    matches = frame.locator(selector)
                    if matches.count():
                        input_locator = matches.first
                        break
                except Exception:
                    continue
        if input_locator is None:
            self._progress("cover.upload", attempted=True,
                           candidates={text: 0 for text in COVER_UPLOAD_TEXTS},
                           clicked=None,
                           input_found=False,
                           elapsed=round(time.monotonic() - started, 2))
            self._progress("cover.upload.input", found=False,
                           clicked=None)
            self._progress("cover.upload.result", ok=False, attempted=True,
                           reason="cover file input not found")
            self._cover_keep_panel_open("upload_input_missing")
            return {"ok": False, "attempted": True,
                    "reason": "cover file input not found"}
        self._progress("cover.upload.input", found=True,
                       clicked=None,
                       file=str(candidate), selector=selector)

        try:
            input_locator.set_input_files(str(candidate), timeout=5000)
        except Exception as exc:
            self._progress("cover.preview.wait",
                           result="error",
                           error=type(exc).__name__ + ": " + str(exc))
            self._progress("cover.upload.preview", result="error",
                           error=type(exc).__name__ + ": " + str(exc))
            self._progress("cover.upload.result", ok=False, attempted=True,
                           reason="set_input_files failed: " + str(exc))
            self._cover_keep_panel_open("set_input_files_failed")
            return {"ok": False, "attempted": True,
                    "reason": "set_input_files failed: " + str(exc)}

        timeout = self._cover_upload_timeout_seconds()
        deadline = time.monotonic() + timeout
        preview = False
        now = before
        now_snapshot = before_snapshot
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            budget_ms = int(max(100, min(self._cover_eval_timeout_ms(),
                                         remaining * 1000)))
            now = self._cover_progress_state(timeout_ms=budget_ms)
            now_snapshot = self._cover_upload_snapshot(timeout_ms=budget_ms)
            frame_bg_changed = (now_snapshot.get("frame_bg") !=
                                before_snapshot.get("frame_bg"))
            preview_bg_changed = (now_snapshot.get("preview_bgs") !=
                                  before_snapshot.get("preview_bgs"))
            new_profile_covers = max(
                0, int(now_snapshot.get("frame_count") or 0) -
                int(before_snapshot.get("frame_count") or 0))
            if (now["srcs"] - before["srcs"] or
                    int(now.get("count") or 0) >
                    int(before.get("count") or 0) or
                    frame_bg_changed or preview_bg_changed or
                    new_profile_covers):
                preview = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _pause_page(self._page,
                        max(20, min(250, int(remaining * 1000))))
        changed = sorted(now["srcs"] - before["srcs"])
        frame_bg_changed = (now_snapshot.get("frame_bg") !=
                            before_snapshot.get("frame_bg"))
        preview_bg_changed = (now_snapshot.get("preview_bgs") !=
                              before_snapshot.get("preview_bgs"))
        new_profile_covers = max(
            0, int(now_snapshot.get("frame_count") or 0) -
            int(before_snapshot.get("frame_count") or 0))
        evidence = []
        if changed or int(now.get("count") or 0) > int(
                before.get("count") or 0):
            evidence.append("srcs")
        if frame_bg_changed:
            evidence.append("frame_bg")
        if preview_bg_changed:
            evidence.append("preview_bg")
        if new_profile_covers:
            evidence.append("profile_cover")
        self._progress("cover.sources.count",
                       baseline=len(before.get("srcs") or []),
                       new=len(changed), scope="preview",
                       frame_bg_changed=bool(frame_bg_changed),
                       preview_bg_changed=bool(preview_bg_changed),
                       profile_covers_before=before_snapshot.get(
                           "frame_count"),
                       profile_covers_after=now_snapshot.get("frame_count"),
                       profile_covers_new=new_profile_covers,
                       frame_bg=now_snapshot.get("frame_bg") or None,
                       evidence=evidence,
                       elapsed=round(time.monotonic() - started, 2))
        self._progress("cover.preview.wait",
                       result="ok" if preview else "timeout",
                       timeout=timeout,
                       new_srcs=len(changed),
                       frame_bg_changed=bool(frame_bg_changed),
                       preview_bg_changed=bool(preview_bg_changed),
                       profile_covers_new=new_profile_covers,
                       evidence=evidence,
                       elapsed=round(time.monotonic() - started, 2))
        result = {"ok": bool(preview), "attempted": True,
                  "orientation": orientation, "file": str(candidate),
                  "new_srcs": changed,
                  "evidence": evidence,
                  "reason": "" if preview else "cover preview did not change"}
        self._progress("cover.upload.preview",
                       result="ok" if preview else "timeout",
                       timeout=timeout,
                       new_srcs=len(changed),
                       evidence=evidence,
                       elapsed=round(time.monotonic() - started, 2))
        self._progress("cover.upload.result", ok=bool(preview),
                       attempted=True,
                       new_srcs=len(changed),
                       evidence=evidence,
                       reason=result["reason"] or None)
        if not preview:
            # A failed upload must not discard the panel: the caller's frame
            # fallback and the later cover check both still need it.
            self._cover_keep_panel_open("upload_preview_timeout")
        return result

    def _cover_keep_panel_open(self, reason):
        """Reopen the cover panel when a failed upload closed it."""
        try:
            if self._cover_panel_handle() is not None:
                return True
        except Exception:
            pass
        self._progress("cover.panel.reopen", reason=reason)
        try:
            return bool(self._open_cover_panel())
        except Exception as exc:
            self._progress("cover.panel.reopen", reason=reason,
                           result="error",
                           error=type(exc).__name__ + ": " + str(exc))
            return False

    def _cover_upload_timeout_seconds(self):
        try:
            value = float(getattr(self, "_cover_upload_timeout",
                                  COVER_UPLOAD_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            value = COVER_UPLOAD_TIMEOUT_SECONDS
        return min(COVER_UPLOAD_TIMEOUT_SECONDS, max(0.2, value))

    def _cover_ai_timeout_seconds(self):
        try:
            value = float(getattr(self, "_cover_ai_timeout",
                                  COVER_AI_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            value = COVER_AI_TIMEOUT_SECONDS
        return min(COVER_AI_TIMEOUT_SECONDS, max(0.2, value))

    def _cover_new_srcs(self, before):
        now = self._cover_progress_state()
        return sorted(set(now.get("srcs") or []) -
                      set((before or {}).get("srcs") or []))

    def _cover_check_marker_seen(self):
        text = self._cover_page_text(limit=4000)
        return ("\u91cd\u65b0\u68c0\u6d4b" in text or
                "\u5c01\u9762\u68c0\u6d4b\u901a\u8fc7" in text)

    def _cover_ai_busy(self):
        counts = _cover_control_scan(
            self._page,
            tuple(COVER_AI_BUSY_TEXTS + COVER_AI_CANCEL_TEXTS),
            attempts=1, retry_ms=0, timeout=0.0)["counts"]
        return any(counts.values()), counts

    def _cover_ai_generate(self):
        """Open the AI tab; finish early on a new image or a check marker."""
        started = time.monotonic()
        tab_report = self._cover_panel_click_report(COVER_AI_TAB_TEXTS)
        tab_counts = tab_report.get("counts") or {
            text: 0 for text in COVER_AI_TAB_TEXTS}
        self._progress("cover.ai.tab",
                       candidates=tab_counts,
                       clicked=tab_report.get("clicked") or None,
                       ok=bool(tab_report.get("ok")))
        before = self._cover_progress_state()
        generate_report = self._cover_panel_click_report(COVER_AI_GENERATE_TEXTS)
        self._progress("cover.ai.generate",
                       candidates=generate_report.get("counts"),
                       clicked=generate_report.get("clicked") or None,
                       ok=bool(generate_report.get("ok")))
        if not generate_report.get("ok"):
            fallback = _cover_control_scan(
                self._page, COVER_PANEL_CONTROL_TEXTS,
                attempts=1, retry_ms=0, timeout=0.0)
            available = [text for text, count in fallback["counts"].items()
                         if count]
            self._progress("cover.ai.controls",
                           available=available,
                           frames=fallback["frames"])
            return {"ok": False, "started": False,
                    "panel_usable": bool(available),
                    "reason": "AI generate control missing; default frame"}

        saw_busy = {"value": False}

        def finished():
            new_srcs = self._cover_new_srcs(before)
            if new_srcs:
                return {"exit": "new_src", "new_srcs": len(new_srcs)}
            if self._cover_check_marker_seen():
                return {"exit": "check_marker", "new_srcs": 0}
            busy, _ = self._cover_ai_busy()
            saw_busy["value"] = saw_busy["value"] or busy
            if busy:
                return None
            if saw_busy["value"]:
                return {"exit": "busy_finished", "new_srcs": 0}
            return None

        wait_budget = max(
            0.2, self._cover_ai_timeout_seconds() -
            (time.monotonic() - started))
        completed = _wait_for(finished, self._page, timeout=wait_budget)
        if completed:
            exit_reason = (completed.get("exit")
                           if isinstance(completed, dict) else "completed")
            self._progress("cover.ai.generation",
                           result="completed",
                           exit=exit_reason,
                           new_srcs=(completed.get("new_srcs")
                                     if isinstance(completed, dict) else 0),
                           elapsed=round(time.monotonic() - started, 2))
            return {"ok": True, "started": True, "timed_out": False,
                    "cancelled": False, "exit": exit_reason}

        # Keep the panel open after a timeout: the cover check and the done
        # button still need it.  Closing it here was what made 1.2.101 report
        # every downstream control as missing.
        self._progress("cover.ai.cancel",
                       result="skipped",
                       reason="timeout_keep_panel_open")
        self._progress("cover.ai.generation",
                       result="timeout",
                       timeout=self._cover_ai_timeout_seconds(),
                       elapsed=round(time.monotonic() - started, 2))
        return {"ok": False, "started": bool(saw_busy["value"]),
                "timed_out": True,
                "cancelled": False,
                "reason": "generation timed out"}

    def _cover_frame_scan(self, state, frame_index, *, elapsed=None,
                          diagnostics=None):
        """Record the global frame scan, including an empty result."""
        rects = []
        for item in (state.get("items") or [])[:5]:
            rect = item.get("rect") or {}
            rects.append([int(rect.get("x") or 0),
                          int(rect.get("y") or 0),
                          int(rect.get("width") or 0),
                          int(rect.get("height") or 0)])
        fields = {"count": int(state.get("count") or 0),
                  "scope": "global",
                  "frame": frame_index,
                  "sample_rects": rects,
                  "mode": state.get("mode") or None,
                  "section": state.get("section") or None,
                  "sections": state.get("sections") or
                  {"vertical": 0, "horizontal": 0}}
        if elapsed is not None:
            fields["elapsed"] = round(float(elapsed), 2)
        if diagnostics:
            fields.update(diagnostics)
        self._progress("cover.frame.scan", **fields)

    def _cover_frame_diagnostics(self, state):
        """Page facts recorded when the frame panel never renders."""
        page_url = _page_url_value(self._page)
        frames = [frame for frame in _page_frames(self._page)
                  if _cover_frame_usable(frame)]
        video_count = 0
        frame_urls = []
        for frame in frames:
            frame_urls.append(_frame_url_value(frame))
            try:
                value = frame.locator("body").evaluate(
                    "() => document.querySelectorAll('video').length",
                    timeout=self._cover_eval_timeout_ms())
                video_count += int(value or 0)
            except Exception:
                continue
        try:
            panel_visible = bool(self._cover_panel_visible())
        except Exception:
            panel_visible = False
        return {"page_url": page_url,
                "panel_visible": panel_visible,
                "iframes": max(0, len(frames) - 1),
                "frames": len(frames),
                "frame_urls": frame_urls,
                "has_video": bool(video_count),
                "video_count": video_count,
                "count": int(state.get("count") or 0)}

    def _cover_frame_state(self, panel=None, *, log=True):
        """Return recommended frames from a document-wide scan.

        1.2.105: the live frames sit in the right-hand ``verCoverContainer``
        preview, not under the ``设置竖封面`` text block.  ``panel`` is kept
        for call-site compatibility only; candidates always come from
        ``document.querySelectorAll('div[class*="profileCover"]')``.
        """
        empty = {"count": 0, "selected_index": -1, "items": [],
                 "section": "", "mode": "", "frame": 0,
                 "sections": {"vertical": 0, "horizontal": 0}}
        # selector-ok: scoped-unique
        script = r"""(root, specs) => {
          const doc = (root && root.ownerDocument) || document;
          const clean = value => String(value || '')
              .replace(/\s+/g, '').replace(/\u200b/g, '');
          const visible = el => {
            if (!el) return false;
            const style = window.getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && !!(el.offsetWidth || el.offsetHeight ||
                      el.getClientRects().length);
          };
          const klassOf = el => String(
              (el && el.getAttribute) ? (el.getAttribute('class') || '') : '');
          const isCanvas = el => String(el.tagName || '').toLowerCase() ===
              'canvas';
          const inCanvas = el => !!(el && el.closest && el.closest('canvas'));
          const isLikeBackground = el =>
              /(^|[\s_-])like-bg([\s_-]|$)/i.test(klassOf(el));
          const sectionFromContainer = el => {
            if (!el || !el.closest) return '';
            const vertical = el.closest('[class*="verCoverContainer"]');
            const horizontal = el.closest('[class*="horCoverContainer"]');
            if (!vertical) return horizontal ? 'horizontal' : '';
            if (!horizontal) return 'vertical';
            return vertical.contains(horizontal) ? 'horizontal' : 'vertical';
          };
          const markerSelected = node => {
            for (let el = node, depth = 0;
                 el && el !== doc.body && depth < 3;
                 el = el.parentElement, depth += 1) {
              if (String(el.getAttribute('aria-selected') || '')
                      .toLowerCase() === 'true') return true;
              if (String(el.getAttribute('aria-checked') || '')
                      .toLowerCase() === 'true') return true;
              if (String(el.getAttribute('data-selected') || '')
                      .toLowerCase() === 'true') return true;
              if (/(^|[\s_-])(active|selected|checked|current)([\s_-]|$)/i
                      .test(klassOf(el))) return true;
              try {
                if (el.querySelector && el.querySelector('input:checked')) {
                  return true;
                }
              } catch (error) {}
            }
            return false;
          };
          const styleSignature = node => {
            const parts = [];
            for (let el = node, depth = 0;
                 el && el !== doc.body && depth < 3;
                 el = el.parentElement, depth += 1) {
              const style = window.getComputedStyle(el);
              parts.push([
                klassOf(el),
                String(el.getAttribute('aria-selected') || ''),
                String(el.getAttribute('aria-checked') || ''),
                String(el.getAttribute('data-selected') || ''),
                style.borderTopColor, style.borderRightColor,
                style.borderBottomColor, style.borderLeftColor,
                style.borderTopWidth, style.borderRightWidth,
                style.borderBottomWidth, style.borderLeftWidth,
                style.boxShadow, style.outlineColor, style.outlineWidth
              ].join('|'));
            }
            return parts.join('||');
          };
          const backgroundUrls = el => {
            const text = String(window.getComputedStyle(el).backgroundImage || '');
            if (!text || text === 'none') return [];
            const found = [];
            const pattern = /url\((?:"|')?(.*?)(?:"|')?\)/g;
            let match = null;
            while ((match = pattern.exec(text))) {
              const src = String(match[1] || '').trim();
              if (src) found.push(src);
            }
            return found;
          };
          const sourceInfo = el => {
            let imgSrc = '';
            if (String(el.tagName || '').toLowerCase() === 'img') {
              imgSrc = String(el.currentSrc || el.src ||
                              el.getAttribute('src') || '').trim();
            }
            const backgrounds = backgroundUrls(el);
            const bg = backgrounds.length ? backgrounds[0] : '';
            return {src: imgSrc || bg, image_src: imgSrc, bg: bg};
          };
          const headings = [];
          const seenHeadings = new Set();
          if (doc.body) {
            const walker = document.createTreeWalker(
                doc.body, NodeFilter.SHOW_TEXT);
            while (walker.nextNode()) {
              const text = clean(walker.currentNode.nodeValue);
              if (!text) continue;
              const el = walker.currentNode.parentElement;
              if (!el || !visible(el) || seenHeadings.has(el)) continue;
              const spec = specs.find(item => item.markers.some(
                  marker => text.indexOf(clean(marker)) >= 0));
              if (!spec) continue;
              seenHeadings.add(el);
              headings.push({name: spec.name, el: el});
            }
          }
          const precedes = (left, right) => !!(left &&
              (left.compareDocumentPosition(right) &
               Node.DOCUMENT_POSITION_FOLLOWING));
          const sectionFor = node => {
            let owner = null;
            for (const heading of headings) {
              if (heading.el === node || !precedes(heading.el, node)) continue;
              if (!owner || precedes(owner.el, heading.el)) owner = heading;
            }
            return owner ? owner.name : '';
          };
          const rectOf = el => {
            const rect = el.getBoundingClientRect();
            const width = rect.width || el.clientWidth ||
                          Number(el.getAttribute('width') || 0);
            const height = rect.height || el.clientHeight ||
                           Number(el.getAttribute('height') || 0);
            return {x: Math.round(rect.x || 0), y: Math.round(rect.y || 0),
                    width: Math.round(width), height: Math.round(height),
                    raw_width: width, raw_height: height};
          };
          const entries = [];
          const seen = new Set();
          const addEntry = (el, kind) => {
            if (!el || seen.has(el) || !visible(el)) return;
            if (isCanvas(el) || inCanvas(el) || isLikeBackground(el)) return;
            const rect = rectOf(el);
            if (rect.raw_width <= 0 || rect.raw_height <= 0) return;
            seen.add(el);
            const source = sourceInfo(el);
            const containerSection = sectionFromContainer(el);
            entries.push({
              el: el, kind: kind,
              rect: {x: rect.x, y: rect.y, width: rect.width,
                     height: rect.height},
              width: rect.raw_width, height: rect.raw_height,
              section: containerSection || sectionFor(el),
              container_section: containerSection,
              src: source.src, image_src: source.image_src, bg: source.bg,
              class_name: klassOf(el),
              parent_class: klassOf(el.parentElement),
              borderColor: window.getComputedStyle(el).borderTopColor,
              boxShadow: window.getComputedStyle(el).boxShadow,
              style_signature: styleSignature(el),
              selected: markerSelected(el)
            });
          };
          doc.querySelectorAll('[data-cover-frame-candidate]').forEach(
              el => el.removeAttribute('data-cover-frame-candidate'));

          // Primary live-DOM contract: one global query, no panel/text scope.
          Array.from(doc.querySelectorAll('div[class*="profileCover"]'))
              .forEach(el => {
                if (!visible(el) || isCanvas(el) || inCanvas(el) ||
                    isLikeBackground(el)) return;
                const rect = rectOf(el);
                if (rect.raw_width < 40 || rect.raw_height < 50) return;
                addEntry(el, 'profileCover');
              });
          let mode = entries.length ? 'profileCover' : '';

          if (!entries.length) {
            // Hashed class changed: accept douyinpic background elements that
            // fit the observed 63x84 thumbnail envelope.
            Array.from(doc.querySelectorAll('*')).forEach(el => {
              if (isCanvas(el) || inCanvas(el) || isLikeBackground(el) ||
                  !visible(el)) return;
              const backgrounds = backgroundUrls(el);
              if (!backgrounds.some(src => /douyinpic\.com/i.test(src))) {
                return;
              }
              const rect = rectOf(el);
              if (rect.raw_width < 40 || rect.raw_width > 140 ||
                  rect.raw_height < 40 || rect.raw_height > 160) return;
              addEntry(el, 'background');
            });
            mode = entries.length ? 'background' : '';
          }

          if (!entries.length) {
            // Compatibility fallback for the labelled <img> frame block used
            // by older Douyin builds and local fixtures.
            Array.from(doc.querySelectorAll('img')).forEach(img => {
              if (!visible(img) || isCanvas(img) || isLikeBackground(img)) {
                return;
              }
              const rect = rectOf(img);
              if (rect.raw_width < 40 || rect.raw_width > 140 ||
                  rect.raw_height < 50 || rect.raw_height > 160) return;
              const section = sectionFor(img);
              if (!section) return;
              addEntry(img, 'legacy');
            });
            const vertical = entries.filter(entry => entry.section ===
                                                     'vertical');
            const horizontal = entries.filter(entry => entry.section ===
                                                       'horizontal');
            const pool = vertical.length ? vertical : horizontal;
            if (pool.length) {
              entries.length = 0;
              pool.forEach(entry => entries.push(entry));
              mode = vertical.length ? 'legacy_vertical'
                  : 'legacy_horizontal';
            } else {
              entries.length = 0;
              mode = '';
            }
          }

          entries.forEach((entry, index) => {
            entry.index = index;
            entry.el.setAttribute('data-cover-frame-candidate',
                                  String(index));
          });
          let selectedIndex = -1;
          entries.forEach((entry, index) => {
            if (selectedIndex < 0 && entry.selected) selectedIndex = index;
          });
          const section = entries.length
              ? (entries[0].section || (mode.indexOf('legacy_') === 0
                    ? mode.slice('legacy_'.length) : 'global'))
              : '';
          return {
            count: entries.length,
            selected_index: selectedIndex,
            section: section,
            mode: mode,
            sections: {
              vertical: entries.filter(entry => entry.section ===
                                                'vertical').length,
              horizontal: entries.filter(entry => entry.section ===
                                                  'horizontal').length
            },
            items: entries.map(entry => ({
              index: entry.index, src: entry.src, image_src: entry.image_src,
              bg: entry.bg, rect: entry.rect, section: entry.section,
              container_section: entry.container_section,
              kind: entry.kind, width: Math.round(entry.width),
              height: Math.round(entry.height), selected: entry.selected,
              class_name: entry.class_name,
              parent_class: entry.parent_class,
              borderColor: entry.borderColor, boxShadow: entry.boxShadow,
              style_signature: entry.style_signature
            }))
          };
        }"""
        timeout = self._cover_eval_timeout_ms()
        last = dict(empty)
        for frame_index, frame in enumerate(_page_frames(self._page)):
            if not _cover_frame_usable(frame):
                continue
            try:
                value = frame.locator("body").evaluate(
                    script, list(COVER_FRAME_SECTION_SPECS),
                    timeout=timeout)
            except Exception:
                continue
            if not isinstance(value, dict):
                continue
            try:
                value["count"] = int(value.get("count") or 0)
                raw_index = value.get("selected_index", -1)
                value["selected_index"] = (-1 if raw_index is None
                                           else int(raw_index))
            except (TypeError, ValueError):
                continue
            value.setdefault("section", "")
            value.setdefault("mode", "")
            value.setdefault("sections", {"vertical": 0, "horizontal": 0})
            value.setdefault("items", [])
            value["frame"] = frame_index
            if value["count"]:
                if log:
                    self._cover_frame_scan(value, frame_index)
                return value
            last = value
        last["frame"] = int(last.get("frame") or 0)
        if log:
            self._cover_frame_scan(last, int(last.get("frame") or 0))
        return last

    def _cover_frame_candidate(self, frame_index, index):
        """Resolve a marked frame inside the frame that produced the scan."""
        frames = _page_frames(self._page)
        if frame_index < 0 or frame_index >= len(frames):
            return None
        frame = frames[frame_index]
        if not _cover_frame_usable(frame):
            return None
        selector = '[data-cover-frame-candidate="%d"]' % int(index)
        try:
            matches = frame.locator(selector)
            if not int(matches.count()):
                return None
            return matches.first
        except Exception:
            return None

    def _select_default_cover_frame(self, *, fail_closed=True):
        """Click recommended frames; selection state is best effort.

        1.2.108: the live panel gives no observable selected state after a
        frame click (class, aria, border and shadow all stay unchanged).  A
        successful click is therefore enough to continue to the cover check;
        ``selected`` stays ``None`` when no selection signal can be read.
        """
        started = time.monotonic()
        try:
            self._open_cover_panel(fail_closed=False)
        except Exception as exc:
            self._progress("cover.frame.view.ensure_error",
                           error=type(exc).__name__ + ": " + str(exc))
        deadline = started + self._cover_frame_wait_seconds()
        state = {"count": 0, "selected_index": -1, "items": [],
                 "section": "", "sections": {"vertical": 0,
                                             "horizontal": 0}, "frame": 0}
        last_log_at = None
        while True:
            state = self._cover_frame_state(log=False)
            elapsed = round(time.monotonic() - started, 2)
            count = int(state.get("count") or 0)
            selected_index = int(state.get("selected_index", -1))
            if count or selected_index >= 0:
                frame_index = int(state.get("frame") or 0)
                if count > 0:
                    self._progress("cover.frame.view",
                                   profile_covers=count)
                self._cover_frame_scan(state, frame_index, elapsed=elapsed)
                self._progress("cover.frame.ready",
                               count=count,
                               selected_index=selected_index,
                               frame=frame_index,
                               scope="global",
                               elapsed=elapsed)
                break
            if (last_log_at is None or
                    elapsed - last_log_at >=
                    COVER_FRAME_SCAN_LOG_INTERVAL_SECONDS):
                self._cover_frame_scan(
                    state, int(state.get("frame") or 0), elapsed=elapsed)
                last_log_at = elapsed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._upload_heartbeat()
            time.sleep(min(COVER_FRAME_POLL_INTERVAL_SECONDS, remaining))

        if (int(state.get("count") or 0) <= 0 and
                int(state.get("selected_index", -1)) < 0):
            elapsed = round(time.monotonic() - started, 2)
            diagnostics = self._cover_frame_diagnostics(state)
            self._progress("cover.frame.diagnostics",
                           elapsed=elapsed,
                           timeout=self._cover_frame_wait_seconds(),
                           **diagnostics)

        tried = []
        clicked_indices = []
        click_mode = ""

        def item_at(items, index):
            for entry in items or []:
                if int(entry.get("index", -1)) == int(index):
                    return entry
            return {}

        def next_index():
            items = state.get("items") or []
            ordered = ([int(item.get("index", -1)) for item in items
                        if item.get("section") == "vertical"] +
                       [int(item.get("index", -1)) for item in items
                        if item.get("section") != "vertical"])
            for index in ordered:
                if index >= 0 and index not in tried:
                    return index
            return None

        def frame_fields(index):
            item = item_at(state.get("items") or [], index)
            rect = item.get("rect") or {}
            src = str(item.get("src") or "")
            bg = str(item.get("bg") or "")
            section = str(item.get("section") or
                          state.get("section") or "")
            summary = "index=%s, rect=%s, bg=%s" % (
                index, rect, bg or src)
            return item, rect, src, bg, section, summary

        def selected_result():
            selected_index = int(state.get("selected_index", -1))
            item, rect, src, bg, section, summary = frame_fields(
                selected_index)
            index = int(item.get("index", selected_index))
            self._progress("cover.frame.selected",
                           selected=True,
                           already_selected=not tried,
                           clicked=bool(tried),
                           click_mode=click_mode or None,
                           attempts=len(tried),
                           candidates=int(state.get("count") or 0),
                           section=section or None,
                           sections=state.get("sections"),
                           index=index,
                           rect=rect,
                           src=src,
                           bg=bg,
                           mode=state.get("mode") or None,
                           summary=summary,
                           item=item,
                           elapsed=round(time.monotonic() - started, 2))
            return {"ok": True, "selected": True,
                    "already_selected": not tried,
                    "attempts": len(tried),
                    "candidates": int(state.get("count") or 0),
                    "index": index, "rect": rect, "src": src, "bg": bg,
                    "section": section, "state": state}

        def best_effort_result(index):
            item, rect, src, bg, section, summary = frame_fields(index)
            index = int(item.get("index", index))
            reason = "no_selection_signal"
            self._progress("cover.frame.selected",
                           selected=None,
                           clicked=True,
                           click_mode=click_mode or None,
                           reason=reason,
                           attempts=len(tried),
                           clicked_indices=list(clicked_indices),
                           candidates=int(state.get("count") or 0),
                           section=section or None,
                           sections=state.get("sections"),
                           index=index,
                           rect=rect,
                           src=src,
                           bg=bg,
                           mode=state.get("mode") or None,
                           summary=summary,
                           item=item,
                           elapsed=round(time.monotonic() - started, 2))
            return {"ok": True, "selected": None, "clicked": True,
                    "already_selected": False,
                    "attempts": len(tried),
                    "candidates": int(state.get("count") or 0),
                    "index": index, "rect": rect, "src": src, "bg": bg,
                    "section": section, "sections": state.get("sections"),
                    "reason": reason, "clicked_indices": list(clicked_indices),
                    "state": state}

        while len(tried) < COVER_FRAME_MAX_ATTEMPTS:
            state = self._cover_frame_state(log=False)
            if tried and int(state.get("selected_index", -1)) >= 0:
                return selected_result()
            index = next_index()
            if index is None:
                break
            item_info = item_at(state.get("items") or [], index)
            item = self._cover_frame_candidate(
                int(state.get("frame") or 0), index)
            if item is None:
                tried.append(index)
                self._progress("cover.frame.attempt",
                               index=index,
                               rect=item_info.get("rect") or {},
                               bg=item_info.get("bg") or None,
                               clicked=False,
                               candidates=int(state.get("count") or 0),
                               mode=state.get("mode") or None,
                               reason="candidate_missing")
                break
            result = _cover_click_element(item)
            tried.append(index)
            if result.get("ok"):
                clicked_indices.append(index)
                click_mode = str(result.get("mode") or click_mode or "")
            self._upload_heartbeat()
            time.sleep(COVER_FRAME_CLICK_RECHECK_SECONDS)
            state = self._cover_frame_state(log=False)
            after_info = item_at(state.get("items") or [], index)
            style_changed = bool(
                item_info and after_info and
                item_info.get("style_signature") !=
                after_info.get("style_signature"))
            class_changed = bool(
                item_info and after_info and
                item_info.get("class_name") != after_info.get("class_name"))
            parent_class_changed = bool(
                item_info and after_info and
                item_info.get("parent_class") !=
                after_info.get("parent_class"))
            if style_changed or (after_info.get("selected") and
                                 not item_info.get("selected")):
                # Attribute the hit to the frame that was actually clicked.
                # A shared ancestor marker can otherwise make the first
                # sibling look selected and hide the real third-frame hit.
                state["selected_index"] = index
                after_info["selected"] = True
            selected_now = int(state.get("selected_index", -1)) >= 0
            self._progress("cover.frame.attempt",
                           index=index,
                           rect=item_info.get("rect") or {},
                           bg=item_info.get("bg") or None,
                           clicked=bool(result.get("ok")),
                           click_mode=result.get("mode") or None,
                           error=result.get("reason") or None,
                           candidates=int(state.get("count") or 0),
                           section=state.get("section") or None,
                           mode=state.get("mode") or None,
                           selected=selected_now,
                           clicked_indices=list(clicked_indices),
                           style_changed=style_changed,
                           class_changed=class_changed,
                           parent_class_changed=parent_class_changed,
                           border_color_changed=bool(
                               item_info and after_info and
                               item_info.get("borderColor") !=
                               after_info.get("borderColor")),
                           box_shadow_changed=bool(
                               item_info and after_info and
                               item_info.get("boxShadow") !=
                               after_info.get("boxShadow")),
                           border_before={
                               "borderColor": item_info.get("borderColor"),
                               "boxShadow": item_info.get("boxShadow")},
                           border_after={
                               "borderColor": after_info.get("borderColor"),
                               "boxShadow": after_info.get("boxShadow")},
                           reason=(None if selected_now
                                   else "selection_not_detected"))
            if int(state.get("selected_index", -1)) >= 0:
                return selected_result()

        if clicked_indices:
            return best_effort_result(clicked_indices[-1])

        failure = {"ok": False, "selected": False,
                   "already_selected": False,
                   "clicked": False, "click_mode": click_mode or None,
                   "attempts": len(tried), "tried": list(tried),
                   "candidates": int(state.get("count") or 0),
                   "section": state.get("section") or None,
                   "mode": state.get("mode") or None,
                   "state": state,
                   "reason": "cover.frame_not_selected"}
        diagnostics = self._cover_frame_diagnostics(state)
        self._progress("cover.frame.selected",
                       selected=False,
                       already_selected=False,
                       clicked=False,
                       click_mode=click_mode or None,
                       attempts=len(tried),
                       tried=list(tried),
                       candidates=int(state.get("count") or 0),
                       section=state.get("section") or None,
                       reason="cover.frame_not_selected",
                       elapsed=round(time.monotonic() - started, 2),
                       **diagnostics)
        if fail_closed:
            self._cover_fail("cover.frame_not_selected",
                             code="cover.frame_not_selected")
        return failure

    def _cover_done_candidates(self, scan):
        """Resolve the real done button, structure first and last match."""
        details = []
        seen = {}

        def record(item, source, frame_index, text=None):
            info = _cover_element_diagnostics(item)
            try:
                visible = bool(item.is_visible())
            except Exception:
                visible = False
            entry = {
                "source": source,
                "text": text,
                "frame": frame_index,
                "tag": str(info.get("tag") or ""),
                "class": str(info.get("klass") or ""),
                "rect": info.get("rect") or {},
                "disabled": bool(info.get("disabled")),
                "visible": visible,
                "chosen": False,
            }
            key = (frame_index, entry["tag"], entry["class"],
                   tuple(sorted(entry["rect"].items())), entry["disabled"])
            if key in seen:
                return seen[key]
            seen[key] = entry
            details.append(entry)
            return entry

        # Keep the text-level hits in diagnostics: on the live page this is
        # where the non-interactive duplicate label appears.
        for text, frame_index, item in scan.get("hits") or []:
            record(item, "text", frame_index, text)

        frames = [frame for frame in _page_frames(self._page)
                  if _cover_frame_usable(frame)]
        chosen_item = None
        chosen_entry = None
        chosen_selector = ""
        # 1.2.111: resolve done document-wide by the real primary button and
        # then by role.  Panel scoping can miss the footer on the live DOM.
        for selector in ("button.semi-button-primary", "[role=button]"):
            tier = []
            for frame_index, frame in enumerate(frames):
                try:
                    locator = frame.locator(selector)
                    count = int(locator.count())
                except Exception:
                    continue
                for index in range(max(count, 0)):
                    try:
                        item = locator.nth(index)
                    except Exception:
                        continue
                    try:
                        label = _normalize_form_text(item.inner_text())
                    except Exception:
                        label = ""
                    if "完成" not in label:
                        continue
                    entry = record(item, selector, frame_index, "完成")
                    if not entry["visible"] or entry["disabled"]:
                        continue
                    try:
                        if not item.is_enabled():
                            continue
                    except Exception:
                        pass
                    tier.append((item, entry))
            if tier:
                # The footer button bar comes after other controls, so the
                # last enabled match is the real "完成".
                chosen_item, chosen_entry = tier[-1]
                chosen_selector = selector
                break
        if chosen_entry is not None:
            chosen_entry["chosen"] = True
        return {"item": chosen_item, "entry": chosen_entry,
                "selector": chosen_selector, "details": details}

    def _click_cover_done(self):
        """Click done by structure, then verify the panel really closed."""
        attempts = 0
        clicked_text = ""
        errors = []
        counts = {text: 0 for text in COVER_DONE_TEXTS}
        details = []
        click_mode = ""
        click_via = ""
        click_hit = False
        click_rect = {}
        close_wait = self._cover_close_wait_seconds()
        self._cover_done_preview_gone = False
        self._cover_done_closed_ok = False
        preview_before = bool(
            self._cover_panel_state().get("preview_visible"))
        panel_before = self._cover_panel_handle()
        for round_index in range(COVER_DONE_MAX_ROUNDS):
            scan = _cover_control_scan(
                self._page, COVER_DONE_TEXTS,
                attempts=1, retry_ms=0, timeout=0.0)
            counts = scan.get("counts") or counts
            selection = self._cover_done_candidates(scan)
            details = selection.get("details") or details
            item = selection.get("item")
            if item is None:
                errors.append("done control missing")
                self._cover_act(COVER_DONE_TEXT,
                                {"ok": False, "via": "js", "hit": False,
                                 "rect": {}})
                break
            attempts = round_index + 1
            clicked_text = COVER_DONE_TEXT
            result = _cover_click_done_element(item)
            self._cover_act(COVER_DONE_TEXT, result)
            click_mode = str(result.get("mode") or "")
            click_via = str(result.get("via") or "")
            click_hit = bool(result.get("hit"))
            click_rect = result.get("rect") or {}
            if not result.get("ok"):
                errors.append(result.get("reason") or "done click failed")
                if result.get("via") == "mouse":
                    break
                if round_index < COVER_DONE_MAX_ROUNDS - 1:
                    _pause_page(self._page, 500)
                continue
            if _wait_for(
                    lambda: self._cover_done_closed(
                        preview_before, panel_before).get("closed"),
                         self._page, timeout=close_wait):
                break
            if result.get("via") == "mouse":
                break
        done_state = self._cover_done_closed(preview_before, panel_before)
        closed = click_hit and bool(done_state.get("closed"))
        self._cover_done_closed_ok = closed
        self._cover_done_preview_gone = bool(
            click_hit and done_state.get("preview_gone"))
        self._progress("cover.done.click",
                       candidates=counts,
                       clicked=clicked_text or None,
                       attempts=attempts,
                       panel_closed=closed,
                       click_mode=click_mode or None,
                       click_via=click_via or None,
                       click_hit=click_hit,
                       click_rect=click_rect or None,
                       candidate_details=details,
                       errors=errors or None)
        self._progress("cover.done.result",
                       result=("closed" if closed else "open"),
                       clicked=clicked_text or None,
                       attempts=attempts,
                       closed=closed,
                       panel_closed=closed,
                       panel_visible=bool(done_state.get("panel_visible")),
                       preview_visible=bool(
                           done_state.get("preview_visible")),
                       preview_gone=bool(done_state.get("preview_gone")),
                       click_mode=click_mode or None)
        return {"ok": closed, "attempts": attempts,
                "clicked": clicked_text or None, "closed": closed,
                "counts": counts, "candidate_details": details,
                "click_mode": click_mode or None,
                "click_via": click_via or None,
                "click_hit": click_hit,
                "click_rect": click_rect or {}}

    def _set_cover_frame_range(self):
        """Move the frame slider and prove both the value and preview changed."""
        panel = self._cover_panel_handle()
        if panel is None:
            return None
        try:
            changed = self._cover_dom("""(body, root) => {
              const visible = el => {
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && !!(el.offsetWidth || el.offsetHeight ||
                          el.getClientRects().length);
              };
              const previewSrc = () => {
                let best = null;
                let area = -1;
                for (const img of root.querySelectorAll('img')) {
                  if (!visible(img)) continue;
                  const rect = img.getBoundingClientRect();
                  const size = (rect.width || img.clientWidth || 0) *
                               (rect.height || img.clientHeight || 0);
                  if (size <= area) continue;
                  area = size;
                  best = String(img.currentSrc || img.src || '').trim();
                }
                return best || '';
              };
              for (const el of root.querySelectorAll('input[type=range]')) {
                if (!visible(el)) continue;
                const min = Number(el.min || 0);
                const max = Number(el.max || 100);
                const target = min + (max - min) / 2;
                const before = Number(el.value);
                const beforeSrc = previewSrc();
                el.value = String(target);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return {found: true, old: before, target: target,
                        value: Number(el.value), before_src: beforeSrc,
                        preview_src: previewSrc()};
              }
            return {found: false};
            }""", panel)
        except Exception as exc:
            self._cover_fail("frame slider could not be moved: " + str(exc))
            return None
        if not isinstance(changed, dict) or not changed.get("found"):
            self._cover_fail("frame slider was not found in the cover panel")
            return None
        old = float(changed.get("old"))
        target = float(changed.get("target"))
        before_src = str(changed.get("before_src") or "")
        if abs(old - target) <= 0.5:
            self._cover_fail(
                "frame slider did not move: initial value already equals target")

        def readback():
            try:
                return self._cover_dom("""(body, root) => {
              const visible = el => {
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && !!(el.offsetWidth || el.offsetHeight ||
                          el.getClientRects().length);
              };
              const previewSrc = () => {
                let best = null;
                let area = -1;
                for (const img of root.querySelectorAll('img')) {
                  if (!visible(img)) continue;
                  const rect = img.getBoundingClientRect();
                  const size = (rect.width || img.clientWidth || 0) *
                               (rect.height || img.clientHeight || 0);
                  if (size <= area) continue;
                  area = size;
                  best = String(img.currentSrc || img.src || '').trim();
                }
                return best || '';
              };
              for (const el of root.querySelectorAll('input[type=range]')) {
                if (visible(el)) {
                  return {value: Number(el.value), preview_src: previewSrc()};
                }
              }
              return null;
            }""", panel)
            except Exception as exc:
                self._cover_fail("frame slider read-back failed: " + str(exc))
                return None

        def poll():
            state = readback()
            if not isinstance(state, dict) or state.get("value") is None:
                return None
            try:
                value = float(state.get("value"))
            except (TypeError, ValueError):
                return None
            preview = str(state.get("preview_src") or "")
            if abs(value - target) > 0.5 or not preview or preview == before_src:
                return None
            return state

        actual = _wait_for(poll, self._page,
                           timeout=min(3.0, self._cover_wait_seconds()))
        if not actual:
            self._cover_fail(
                "frame slider value or preview image did not change after moving")
        value = float(actual.get("value"))
        if abs(value - old) <= 0.5 or abs(value - target) > 0.5:
            self._cover_fail("frame slider value was not confirmed by read-back")
        after_src = str(actual.get("preview_src") or "")
        if not after_src or after_src == before_src:
            self._cover_fail("cover preview image did not change with the frame slider")
        return {"old": old, "target": target, "value": value,
                "before_src": before_src, "after_src": after_src}

    def _cover_orientation(self):
        panel = self._cover_panel_handle()
        if panel is None:
            return ""
        try:
            return str(self._cover_dom("""(body, root) => {
              const clean = value => String(value || '')
                  .replace(/\\s+/g, '').replace(/\\u200b/g, '');
              const visible = el => {
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && !!(el.offsetWidth || el.offsetHeight ||
                          el.getClientRects().length);
              };
              const declared = clean(root.getAttribute('data-orientation') ||
                                     root.getAttribute('data-cover-orientation'));
              if (declared.indexOf('\u7ad6') >= 0 || declared.toLowerCase() === 'vertical') {
                return 'vertical';
              }
              if (declared.indexOf('\u6a2a') >= 0 || declared.toLowerCase() === 'horizontal') {
                return 'horizontal';
              }
              const marks = ['checked', 'selected', 'active', 'current'];
              const labels = new Set(['\u7ad6\u5c01\u97623:4',
                                      '\u6a2a\u5c01\u97624:3']);
              const walker = document.createTreeWalker(
                  root, NodeFilter.SHOW_TEXT);
              while (walker.nextNode()) {
                if (!clean(walker.currentNode.nodeValue)) continue;
                let node = walker.currentNode.parentElement;
                let text = '';
                for (let depth = 0; node && node !== root.parentElement && depth < 3;
                     depth += 1, node = node.parentElement) {
                  const candidate = clean(node.textContent);
                  if (labels.has(candidate)) {
                    text = candidate;
                    break;
                  }
                }
                if (!text) continue;
                for (let depth = 0; node && depth < 4;
                     depth += 1, node = node.parentElement) {
                  const klass = String(node.className || '').toLowerCase();
                  const aria = String(node.getAttribute('aria-checked') || '') +
                      String(node.getAttribute('aria-selected') || '');
                  if (aria === 'true' ||
                      marks.some(mark => klass.indexOf(mark) >= 0)) {
                    return text.indexOf('\u7ad6') === 0 ? 'vertical' : 'horizontal';
                  }
                  if (node === root) break;
                }
              }
              for (const img of root.querySelectorAll('img')) {
                if (!visible(img)) continue;
                const width = img.clientWidth || img.naturalWidth || 0;
                const height = img.clientHeight || img.naturalHeight || 0;
                if (!width || !height) continue;
                const ratio = width / height;
                if (Math.abs(ratio - 0.75) <= 0.08) return 'vertical';
                if (Math.abs(ratio - (4 / 3)) <= 0.12) return 'horizontal';
              }
              return '';
            }""", panel) or "")
        except Exception:
            return ""

    def _select_cover_orientation(self, orientation):
        texts = (COVER_VERTICAL_TEXTS if orientation == "vertical"
                 else COVER_HORIZONTAL_TEXTS)
        if not self._cover_panel_click(texts):
            self._cover_fail("cover orientation control %s was not found" % orientation)
        _wait_for(lambda: self._cover_orientation() == orientation, self._page,
                  timeout=min(2.0, self._cover_wait_seconds()))
        actual = self._cover_orientation()
        if actual != orientation:
            self._cover_fail("cover orientation read-back is %r, expected %r"
                             % (actual or "unknown", orientation))
        return actual

    def _select_recommended_cover(self, orientation="vertical"):
        clicked = self._cover_panel_click(COVER_RECOMMEND_TEXTS)
        if not clicked:
            self._cover_fail("cover generation control was not found in the cover panel")
        self._select_cover_orientation(orientation)
        if self._set_cover_frame_range() is None:
            self._cover_fail("cover frame was not moved and read back")
        _wait_for(lambda: self._cover_readback().get("ok"), self._page,
                  timeout=self._cover_wait_seconds())
        return True

    def _close_cover_panel(self):
        self._click_cover_done()
        _pause_page(self._page, 150)

    def _cover_fail(self, reason, *, code=None):
        message = "cover is not confirmed: " + str(reason)
        self._cover_warning = str(reason)
        try:
            self._safe_record("cover", note="FAIL: " + message,
                              extra={"error": True, "reason": str(reason),
                                     "code": code})
        except Exception:
            pass
        fail = getattr(self, "_fail", None)
        if callable(fail):
            fail(message, step="cover", code=code)
        raise GateRed(message)

    def _cover_bound_verify(self):
        """Check that the src bound by ``_choose_cover`` is still on the page.

        This is deliberately a presence check, not a second baseline diff:
        verifying the form must not reinterpret the already accepted cover as
        the new baseline.  Query-string changes are ignored by comparing the
        part before ``?`` as a prefix.
        """
        bound = str(getattr(self, "_cover_bound_src", None) or "").strip()
        prefix = bound.split("?", 1)[0].strip()
        result = {"ok": False, "bound_src_prefix": prefix,
                  "found": False, "visible": False, "fallback_used": False}
        if prefix:
            try:
                images = self._cover_dom("""(body) => Array.from(
                    body.querySelectorAll('img')).map(img => {
                  const style = window.getComputedStyle(img);
                  const visible = style.display !== 'none'
                      && style.visibility !== 'hidden'
                      && style.opacity !== '0'
                      && !!(img.offsetWidth || img.offsetHeight
                            || img.getClientRects().length);
                  return {src: String(img.currentSrc || img.src || '').trim(),
                          visible: !!visible};
                })""")
            except Exception as exc:
                images = []
                result["bound_error"] = type(exc).__name__
            for image in images or []:
                if not isinstance(image, dict):
                    continue
                src = str(image.get("src") or "").strip()
                if not src:
                    continue
                if src.split("?", 1)[0].startswith(prefix):
                    result["found"] = True
                    if image.get("visible"):
                        result["visible"] = True
            if result["found"] and result["visible"]:
                result["ok"] = True
                return result

        result["fallback_used"] = True
        state = self._cover_readback()
        result["fallback_ok"] = bool(state.get("ok"))
        if state.get("ok"):
            result["ok"] = True
        elif getattr(self, "_cover_check_ok", False):
            # The live panel explicitly reported a passing cover check.  This
            # is auxiliary evidence when the page does not expose a new src.
            result["ok"] = True
            result["source"] = "cover_check"
        else:
            result["reason"] = state.get("reason") or state.get("state")
        return result

    def _cover_readback(self, *, require_bound=True):
        """Cover evidence: selected control state plus an image bound to this run.

        A visible ``<img>`` anywhere on the page proves nothing.  The selected
        state must expose replace/delete next to the thumbnail, must not be the
        cover dialog itself, and (during publishing) the thumbnail source must
        differ from the state captured before this upload started.
        """
        try:
            state = self._cover_dom("""(body) => {
              const clean = value => String(value || '')
                  .replace(/\\s+/g, '').replace(/\\u200b/g, '');
              const visible = el => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && !!(el.offsetWidth || el.offsetHeight ||
                          el.getClientRects().length);
              };
              const inDialog = el => {
                for (let node = el; node && node !== document.body;
                     node = node.parentElement) {
                  const text = clean(node.textContent);
                  if (text.indexOf('\u786e\u5b9a') >= 0 &&
                      (text.indexOf('\u6a2a\u5c01\u97624:3') >= 0 ||
                       text.indexOf('\u7ad6\u5c01\u97623:4') >= 0 ||
                       text.indexOf('\u63a8\u8350\u5c01\u9762') >= 0)) {
                    return true;
                  }
                }
                return false;
              };
              const anchors = [];
              const labels = new Set(['\u8bbe\u7f6e\u5c01\u9762',
                  '\u9009\u62e9\u5c01\u9762', '\u66f4\u6362', '\u5220\u9664']);
              const walker = document.createTreeWalker(
                  document.body, NodeFilter.SHOW_TEXT);
              while (walker.nextNode()) {
                if (!clean(walker.currentNode.nodeValue)) continue;
                let el = walker.currentNode.parentElement;
                let matched = null;
                for (let depth = 0; el && el !== document.body && depth < 3;
                     depth += 1, el = el.parentElement) {
                  if (labels.has(clean(el.textContent))) {
                    matched = el;
                    break;
                  }
                }
                if (matched && visible(matched) && !inDialog(matched)) {
                  anchors.push(matched);
                }
              }
              let sawEmpty = false;
              let sawCandidate = false;
              for (const anchor of anchors) {
                let node = anchor;
                for (let depth = 0; node && node !== document.body && depth < 5;
                     depth += 1, node = node.parentElement) {
                  if (!visible(node) || inDialog(node)) continue;
                  const text = clean(node.textContent);
                  const selected = text.indexOf('\u66f4\u6362') >= 0 ||
                                   text.indexOf('\u5220\u9664') >= 0;
                  if (!selected) {
                    if (text.indexOf('\u8bbe\u7f6e\u5c01\u9762') >= 0 ||
                        text.indexOf('\u9009\u62e9\u5c01\u9762') >= 0) {
                      sawEmpty = true;
                    }
                    continue;
                  }
                  const coverContext = text.indexOf('\u5c01\u9762') >= 0 ||
                      !!node.querySelector("input[type=file][accept*='image']");
                  if (!coverContext) continue;
                  sawCandidate = true;
                  const controls = Array.from(node.querySelectorAll(
                      'button, a, [role=button], [tabindex]'));
                  const hasSelectedControl = controls.some(control => {
                    if (!visible(control)) return false;
                    const label = clean(control.textContent);
                    return label === '\u66f4\u6362' || label === '\u5220\u9664';
                  }) || (anchor !== node && (
                      clean(anchor.textContent) === '\u66f4\u6362' ||
                      clean(anchor.textContent) === '\u5220\u9664'));
                  if (!hasSelectedControl) continue;
                  const images = Array.from(node.querySelectorAll('img'))
                      .filter(img => {
                        const src = String(img.currentSrc || img.src || '').trim();
                        if (!src || !visible(img)) return false;
                        const rect = img.getBoundingClientRect();
                        const width = rect.width || img.clientWidth ||
                                      Number(img.getAttribute('width') || 0);
                        const height = rect.height || img.clientHeight ||
                                       Number(img.getAttribute('height') || 0);
                        if (width < 80 || height < 80) return false;
                        const ratio = width / height;
                        return Math.abs(ratio - 0.75) <= 0.08 ||
                               Math.abs(ratio - (4 / 3)) <= 0.12;
                      });
                  if (!images.length) continue;
                  return {ok: true, state: 'selected', source: 'img',
                          src: String(images[0].currentSrc || images[0].src || ''),
                          text: String(node.textContent || '').slice(0, 300)};
                }
              }
              return {ok: false, state: sawEmpty ? 'empty' : 'unknown',
                      reason: sawCandidate
                          ? 'cover thumbnail is missing or smaller than 80x80 / not 3:4 or 4:3'
                          : 'no selected cover thumbnail control was found'};
            }""")
        except Exception as exc:
            return {"ok": False, "state": "unknown",
                    "reason": type(exc).__name__ + ": " + str(exc)}
        if not isinstance(state, dict):
            return {"ok": False, "state": "unknown"}
        if not state.get("ok"):
            baseline = getattr(self, "_cover_baseline", None)
            baseline_srcs = set((baseline or {}).get("srcs") or ())
            new_sources = sorted(self._cover_image_sources(
                exclude_dialogs=True) - baseline_srcs)
            if not new_sources:
                return state
            state = {"ok": True, "state": "selected",
                     "source": "background", "src": new_sources[0],
                     "srcs": new_sources}
        state["bound"] = True
        if require_bound:
            baseline = getattr(self, "_cover_baseline", None)
            baseline_srcs = set((baseline or {}).get("srcs") or ())
            bound_src = getattr(self, "_cover_bound_src", None)
            if baseline is not None and state.get("src") in baseline_srcs \
                    and state.get("src") != bound_src:
                state["ok"] = False
                state["bound"] = False
                state["reason"] = "cover thumbnail predates this upload"
            elif baseline is None and bound_src \
                    and state.get("src") != bound_src:
                state["ok"] = False
                state["bound"] = False
                state["reason"] = "cover thumbnail is not the one bound in this run"
        return state

    def _cover_signature(self, *, timeout_ms=None):
        """Return visible page image evidence outside any open cover dialog."""
        srcs = sorted(self._cover_image_sources(
            exclude_dialogs=True, timeout_ms=timeout_ms))
        return {"srcs": srcs, "count": len(srcs), "anchors": len(srcs)}

    def _cover_progress_state(self, *, timeout_ms=None):
        values = self._cover_image_sources(timeout_ms=timeout_ms, as_list=True)
        srcs = set(values)
        return {
            "srcs": srcs,
            "page_srcs": set(srcs),
            "count": len(values),
            "anchors": len(srcs),
        }

    def _cover_upload_snapshot(self, *, timeout_ms=None):
        """Capture upload evidence beyond image URLs.

        The live uploader may keep the same preview URL while replacing the
        right-hand cover frames.  1.2.104 only compared ``src`` sets and thus
        reported ``new=0`` after a successful upload.  This snapshot tracks the
        first frame background, preview-container backgrounds and the number
        of ``profileCover`` nodes.
        """
        # selector-ok: read-only
        script = r"""() => {
          const visible = el => {
            if (!el || !el.getBoundingClientRect) return false;
            const style = window.getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden'
                && style.opacity !== '0'
                && !!(el.offsetWidth || el.offsetHeight ||
                      el.getClientRects().length);
          };
          const klassOf = el => String(
              (el && el.getAttribute) ? (el.getAttribute('class') || '') : '');
          const rectOf = el => el.getBoundingClientRect();
          const backgrounds = el => {
            const text = String(
                window.getComputedStyle(el).backgroundImage || '');
            if (!text || text === 'none') return [];
            const found = [];
            const pattern = /url\((?:"|')?(.*?)(?:"|')?\)/g;
            let match = null;
            while ((match = pattern.exec(text))) {
              const src = String(match[1] || '').trim();
              if (src) found.push(src);
            }
            return found;
          };
          const frames = Array.from(
              document.querySelectorAll('div[class*="profileCover"]'))
              .filter(el => {
                if (!visible(el)) return false;
                if (/(^|[\s_-])like-bg([\s_-]|$)/i.test(klassOf(el))) {
                  return false;
                }
                const rect = rectOf(el);
                return rect.width >= 40 && rect.height >= 50;
              });
          let frameBg = '';
          for (const frame of frames) {
            const urls = backgrounds(frame);
            if (urls.length) { frameBg = urls[0]; break; }
          }
          const selectors = [
            '[class*="verCoverContainer"]',
            '[class*="previewVer"]',
            '[class*="previewphone"]',
            '[class*="previewCover"]',
            '[class*="verCover"]'
          ];
          const previewNodes = new Set();
          for (const selector of selectors) {
            document.querySelectorAll(selector).forEach(el => {
              if (visible(el)) previewNodes.add(el);
            });
          }
          const previewBgs = [];
          for (const node of previewNodes) {
            for (const src of backgrounds(node)) {
              if (previewBgs.indexOf(src) < 0) previewBgs.push(src);
            }
          }
          return {frame_bg: frameBg,
                  frame_count: frames.length,
                  preview_bgs: previewBgs.slice(0, 10)};
        }"""
        try:
            value = self._cover_dom(script, timeout_ms=timeout_ms)
        except Exception:
            value = None
        if not isinstance(value, dict):
            return {"frame_bg": "", "frame_count": 0, "preview_bgs": []}
        return {"frame_bg": str(value.get("frame_bg") or ""),
                "frame_count": int(value.get("frame_count") or 0),
                "preview_bgs": [str(src) for src in
                                (value.get("preview_bgs") or [])]}

    def _cover_progress_changed(self, before, *, timeout_ms=None):
        now = self._cover_progress_state(timeout_ms=timeout_ms)
        return bool(now["srcs"] - before["srcs"] or
                    now["page_srcs"] - before["page_srcs"] or
                    now["count"] > before["count"])

    def _cover_publish_readback(self, baseline):
        current = self._cover_signature()
        baseline_srcs = set((baseline or {}).get("srcs") or [])
        current_srcs = [str(src) for src in current.get("srcs") or []]
        new_srcs = [src for src in current_srcs if src not in baseline_srcs]
        if not new_srcs:
            return None
        panel = self._cover_panel_state()
        closed = self._cover_panel_closed()
        return {"ok": True, "state": "selected", "source": "img",
                "src": new_srcs[0], "srcs": current_srcs,
                "new_srcs": new_srcs, "panel_closed": closed,
                "modal_visible": int(panel.get("modal_visible") or 0),
                "select_visible": int(panel.get("select_visible") or 0),
                "readback": "ok" if closed else "panel_stuck"}

    def _cover_confirm(self, baseline, route="default", check_result=None):
        """Confirm a new cover src or an explicit cover-check pass."""
        check_ok = str((check_result or {}).get("result") or "") in (
            "passed", "ok")
        self._cover_check_ok = check_ok
        timeout = min(5.0, max(1.0, self._cover_wait_seconds()))
        state = None
        if not check_ok:
            state = _wait_for(lambda: self._cover_publish_readback(baseline),
                              self._page, timeout=timeout)
        current = self._cover_signature()
        baseline_srcs = set((baseline or {}).get("srcs") or [])
        current_srcs = [str(src) for src in current.get("srcs") or []]
        new_srcs = [src for src in current_srcs if src not in baseline_srcs]
        panel = self._cover_panel_state()
        panel_closed = self._cover_panel_closed()
        if new_srcs:
            result = "ok" if panel_closed else "panel_stuck"
            if state is None:
                state = {"ok": True, "state": "selected", "source": "img",
                         "src": new_srcs[0], "srcs": current_srcs,
                         "new_srcs": new_srcs}
            state["ok"] = True
            state["state"] = ("selected" if panel_closed
                              else "panel_stuck")
            state["panel_closed"] = panel_closed
            state["modal_visible"] = int(panel.get("modal_visible") or 0)
            state["select_visible"] = int(panel.get("select_visible") or 0)
            state["readback"] = result
            state["evidence"] = "src"
        elif check_ok:
            result = "ok" if panel_closed else "panel_stuck"
            state = {"ok": True, "state": "checked", "source": "check",
                     "src": None, "srcs": current_srcs, "new_srcs": [],
                     "panel_closed": panel_closed,
                     "modal_visible": int(panel.get("modal_visible") or 0),
                     "select_visible": int(panel.get("select_visible") or 0),
                     "readback": result, "evidence": "check"}
        else:
            result = "missing"
            state = None
        code = None
        if result == "missing":
            code = "cover.readback=missing"
        elif result == "panel_stuck":
            code = "cover.readback=panel_stuck"
        self._progress("cover.readback",
                       result=result,
                       ok=result == "ok",
                       preview_gone=bool(getattr(self, "_cover_done_preview_gone", False)
                                         and panel.get("read_ok")
                                         and not panel.get("preview_visible")),
                       code=code,
                       panel_closed=panel_closed,
                       modal_visible=int(panel.get("modal_visible") or 0),
                       select_visible=int(panel.get("select_visible") or 0),
                       baseline_srcs=len(baseline_srcs),
                       current_srcs=len(current_srcs),
                       new_srcs=len(new_srcs),
                       check_ok=check_ok,
                       route=route)
        if result == "missing":
            self._cover_fail("cover.readback=missing")
        if result == "panel_stuck":
            self._cover_warn(
                "cover panel did not close; new cover is already applied")
        return state

    def _wait_upload_blocking_clear(self, timeout=180.0):
        """Wait while the page still says 上传中/转码中/处理中.

        Douyin cannot return a cover verdict before it has the media, so
        starting detection during upload only burns the detect timeout.  This
        wait is best-effort: it never fails the publish on its own.
        """
        started = time.monotonic()
        deadline = started + max(0.0, float(timeout))
        last_log = 0.0
        while True:
            self._upload_heartbeat()
            fact = self._transcode_page_fact()
            blocking = str(fact.get("blocking_text") or "")
            if fact.get("failed"):
                return False
            if not blocking:
                self._progress("upload.blocking.clear",
                               elapsed=round(time.monotonic() - started, 1),
                               publish_enabled=bool(fact.get("publish_enabled")))
                return True
            now = time.monotonic()
            if now >= deadline:
                self._progress("upload.blocking.timeout",
                               elapsed=round(now - started, 1), blocking=blocking)
                return False
            if now - last_log >= 15.0:
                last_log = now
                self._progress("upload.blocking.wait",
                               elapsed=round(now - started, 1), blocking=blocking)
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))

    def _choose_cover(self, covers, *, cover_ai=None):
        """Recommended frame by default; legacy upload/AI only when opted in."""
        covers = self._cover_requested_paths(covers)
        if (not any(bool(value) for value in (covers or {}).values())
                and not self._cover_upload_enabled()
                and not self._cover_ai_enabled(cover_ai)):
            # 2026-10-29: no cover requested -> never open the cover panel or
            # run Douyin's cover check.  With nothing to check the verdict can
            # stall until timeout and freeze the channel.
            self._cover_warning = None
            self._cover_bound_src = None
            self._cover_bound_at = None
            self._cover_check_ok = True
            self._progress("cover.skipped", reason="no_cover_requested")
            self._safe_record("cover", note="SKIP: no cover requested")
            return {"ok": True, "skipped": True,
                    "reason": "no_cover_requested"}
        # Only a real cover request needs the upload/transcode settle wait.
        # The main publish chain still performs its own transcode gate.
        self._wait_upload_blocking_clear()
        self._dismiss_page_overlays()
        self._cover_warning = None
        self._cover_bound_src = None
        self._cover_bound_at = None
        self._cover_check_ok = False
        cover_ai_enabled = self._cover_ai_enabled(cover_ai)
        self._cover_baseline = self._cover_signature()
        baseline = self._cover_baseline
        self._progress("cover.baseline",
                       srcs=len(baseline.get("srcs") or []),
                       images=int(baseline.get("count") or 0))
        self._progress("cover.sources.count",
                       baseline=len(baseline.get("srcs") or []),
                       new=0, scope="baseline")

        opened = self._open_cover_panel()
        if not opened:
            self._fail("cover.panel_unavailable: \u5c01\u9762\u9762\u677f\u672a\u5c31\u7eea",
                       step="cover", code="cover.panel_unavailable")
        self._dismiss_cover_native_dialog()
        try:
            panel_still_open = self._cover_panel_handle() is not None
        except Exception:
            panel_still_open = True
        if not panel_still_open:
            self._progress("cover.dialog.reopen", reason="escape_closed_panel")
            if not self._open_cover_panel():
                self._fail(
                    "cover.panel_unavailable: \u5c01\u9762\u9762\u677f\u672a\u5c31\u7eea",
                    step="cover", code="cover.panel_unavailable")

        _, candidate = self._cover_file_candidate(covers)
        cover_requested = any(bool(value) for value in (covers or {}).values())
        upload_enabled = self._cover_upload_enabled()
        route = ("upload" if upload_enabled and candidate is not None
                 else "default")
        upload_result = None
        if not upload_enabled:
            upload_result = self._cover_upload_file(covers)
        elif cover_requested and candidate is None:
            first = next((str(value) for value in (covers or {}).values()
                          if value), "")
            self._cover_warn("cover file missing: " + first)
        controls = _cover_control_scan(self._page,
                                       COVER_PANEL_CONTROL_TEXTS)
        control_counts = controls.get("counts") or {}
        upload_counts = {text: int(control_counts.get(text) or 0)
                         for text in COVER_UPLOAD_TEXTS}
        ai_counts = {text: int(control_counts.get(text) or 0)
                     for text in COVER_AI_GENERATE_TEXTS}
        self._progress("cover.route.select",
                       route=route,
                       cover_ai_enabled=cover_ai_enabled,
                       cover_requested=cover_requested,
                       cover_upload_enabled=upload_enabled,
                       cover_requested_but_policy_skips=bool(
                           cover_requested and not upload_enabled),
                       click_upload_button=False,
                       file=_log_basename(candidate) if candidate else None,
                       upload_candidates=upload_counts,
                       ai_candidates=ai_counts,
                       frames=int(controls.get("frames") or 0),
                       attempts=int(controls.get("attempts") or 0),
                       panel_text=self._cover_panel_text())

        selected_route = ""
        frame_result = None
        ai_result = None
        if route == "upload":
            upload_result = upload_result or self._cover_upload_file(covers)
            if upload_result.get("ok"):
                selected_route = "upload"
            else:
                self._cover_warn(
                    "customer cover was not uploaded; trying recommended "
                    "frame: " + str(upload_result.get("reason") or "unknown"))

        modal_result = {}
        if not selected_route:
            try:
                modal_result = self._cover_modal_pick()
            except Exception as exc:
                self._cover_warn("cover modal path failed: " + str(exc))
        modal_passed = modal_result.get("result") == "passed"

        if not modal_passed and not selected_route:
            frame_result = self._select_default_cover_frame(
                fail_closed=False)
            if frame_result.get("ok"):
                selected_route = "default"
            elif cover_ai_enabled:
                self._progress("cover.route.fallback",
                               reason=(frame_result.get("reason") or
                                       "recommended frame unavailable"),
                               to="ai")
                ai_result = self._cover_ai_generate()
                if ai_result.get("ok"):
                    selected_route = "ai"
                else:
                    self._cover_warn(
                        "AI cover was not generated; continuing to cover "
                        "check: " + str(ai_result.get("reason") or "unknown"))
                    selected_route = "default"
            else:
                self._cover_warn(
                    "recommended frame was not clicked; continuing to cover "
                    "check: " + str(frame_result.get("reason") or "unknown"))
                selected_route = "default"

        selected_fields = {"route": selected_route,
                           "cover_ai_enabled": cover_ai_enabled}
        if upload_result is not None:
            selected_fields["new_srcs"] = len(upload_result.get("new_srcs")
                                              or [])
        if frame_result is not None:
            selected_fields["frame_index"] = frame_result.get("index")
            selected_fields["frame_section"] = frame_result.get("section")
            selected_fields["frame_ok"] = bool(frame_result.get("ok"))
            selected_fields["frame_clicked"] = bool(
                frame_result.get("clicked"))
            selected_fields["frame_selected"] = frame_result.get("selected")
        if ai_result is not None:
            selected_fields.update({
                "ai_started": bool(ai_result.get("started")),
                "ai_timed_out": bool(ai_result.get("timed_out")),
                "ai_cancelled": bool(ai_result.get("cancelled")),
            })
        self._progress("cover.route.selected", **selected_fields)

        if not modal_result:
            try:
                modal_result = self._cover_modal_pick()
            except Exception as exc:
                self._cover_warn("cover modal path failed: " + str(exc))
        if modal_result.get("result") == "passed":
            self._cover_check_ok = True
            counts = {"\u5c01\u9762\u68c0\u6d4b": 1}
            self._progress("cover.check", result="ok", elapsed=0.0,
                           modal=True, counts=counts, attempts=1)
            self._cover_detect_result("passed", 0.0, clicked="modal",
                                      counts=counts, attempts=1)
            check_result = {"result": "passed", "clicked": "modal",
                            "counts": counts, "attempts": 1}
        else:
            check_result = self._run_cover_check() or {}
        if str(check_result.get("result") or "") in ("missing", "unavailable"):
            self._progress("cover.detect.missing", phase="first",
                           attempts=check_result.get("attempts"),
                           counts=check_result.get("counts"))
            reopened = self._cover_reopen_for_detect()
            if reopened:
                check_result = self._run_cover_check() or {}
            if str(check_result.get("result") or "") in (
                    "missing", "unavailable"):
                self._progress("cover.detect.missing", phase="retry",
                               attempts=check_result.get("attempts"),
                               counts=check_result.get("counts"))
                self._cover_fail(
                    "detect: cover.detect.missing \u5c01\u9762\u68c0\u6d4b\u6309\u94ae\u7f3a\u5931",
                    code="cover.detect.missing")
        last_event = ((getattr(self, "stage_events", None) or [None])[-1])
        if not (isinstance(last_event, dict) and
                last_event.get("stage") == "cover.detect.result" and
                last_event.get("result") == check_result.get("result")):
            self._cover_detect_result(
                check_result.get("result") or "unknown",
                check_result.get("elapsed") or 0.0,
                clicked=check_result.get("clicked"),
                counts=check_result.get("counts"),
                attempts=check_result.get("attempts"))

        detect_result = str(check_result.get("result") or "")
        if detect_result not in ("passed", "ok"):
            self._cover_fail(
                "detect: cover.detect.result=%s" %
                (detect_result or "unknown"),
                code="cover.detect.%s" % (detect_result or "unknown"))

        done_result = self._click_cover_done() or {}
        if not done_result.get("clicked"):
            self._cover_fail("done: \u5b8c\u6210\u6309\u94ae\u672a\u70b9\u51fb",
                             code="cover.done.missing")
        if not done_result.get("closed"):
            self._cover_fail("closed: \u5b8c\u6210\u540e\u9762\u677f\u672a\u5173\u95ed",
                             code="cover.panel_stuck")
        state = self._cover_confirm(baseline, route=selected_route,
                                    check_result=check_result)

        self._cover_bound_src = state.get("src")
        self._cover_bound_at = time.time()
        self._record("cover", note="cover selected and bound to this upload",
                     extra={"cover": state, "route": selected_route})
        return state

    def _publish_button(self):
        locator, selector = self._unique_locator(PUBLISH_BUTTON_SELECTORS,
                                                 timeout=5.0)
        if locator is None:
            return None, selector
        try:
            if not locator.is_enabled():
                return None, selector
        except Exception:
            return None, selector
        return locator, selector

    def _transcode_done(self):
        return self._visible_locator(TRANSCODE_DONE_SELECTORS, timeout=2.0)[0]

    def _upload_progress_visible(self):
        return self._visible_locator(UPLOAD_PROGRESS_SELECTORS, timeout=2.0)[0]

    def _transcode_publish_button(self):
        selector = ", ".join(dict.fromkeys(
            tuple(PUBLISH_BUTTON_SELECTORS)
            + tuple(TRANSCODE_PUBLISH_BUTTON_SELECTORS)))
        try:
            matches = self._page.locator(selector)
            count = matches.count()
        except Exception as exc:
            self._progress("transcode.publish_probe.error",
                           error=type(exc).__name__ + ": " + str(exc))
            return None, selector
        for index in range(count):
            item = matches.nth(index)
            try:
                if item.is_visible() and item.is_enabled():
                    return item, selector
            except Exception:
                continue
        return None, selector

    def _transcode_page_fact(self):
        text = self._safe_visible_text()
        blocking = [label for label in TRANSCODE_BLOCKING_TEXTS
                    if label in text]
        button, selector = self._transcode_publish_button()
        failed = TRANSCODE_FAILED_TEXT in text
        publish_enabled = button is not None
        return {
            "failed": failed,
            "publish_enabled": publish_enabled,
            "blocking_text": "\u3001".join(blocking),
            "ready": publish_enabled and not blocking and not failed,
            "selector": selector,
        }

    def _log_transcode_wait(self, started, fact, last_log_at):
        elapsed = time.monotonic() - started
        if (last_log_at is not None
                and elapsed - last_log_at < TRANSCODE_STAGE_LOG_INTERVAL_SECONDS):
            return last_log_at
        self._progress("transcode.wait",
                       elapsed=round(elapsed, 1),
                       publish_enabled=bool(fact.get("publish_enabled")),
                       blocking_text=str(fact.get("blocking_text") or ""))
        return elapsed

    def _mark_transcode_ready(self, attempt, fact, elapsed):
        attempt.transcode_done = True
        attempt.transcode_done_at = time.time()
        attempt.transcode_failed = False
        attempt.progress_visible = False
        attempt.extra["transcode_page_fact"] = {
            "publish_enabled": bool(fact.get("publish_enabled")),
            "blocking_text": str(fact.get("blocking_text") or ""),
            "elapsed": round(elapsed, 1),
        }

    def _wait_transcode(self):
        attempt = self.upload_attempt
        started = time.monotonic()
        deadline = started + TRANSCODE_WAIT_TIMEOUT_SECONDS
        last_log_at = None
        last_fact = None
        while True:
            self._upload_heartbeat()
            fact = self._transcode_page_fact()
            last_fact = fact
            last_log_at = self._log_transcode_wait(started, fact, last_log_at)
            elapsed = time.monotonic() - started
            if fact["failed"]:
                attempt.transcode_failed = True
                attempt.transcode_done = False
                attempt.progress_visible = False
                self._fail("transcode.failed: \u9875\u9762\u51fa\u73b0\u300c\u8f6c\u7801\u5931\u8d25\u300d",
                           step="transcode", code="transcode.failed")
            if fact["ready"]:
                self._mark_transcode_ready(attempt, fact, elapsed)
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(TRANSCODE_POLL_INTERVAL_SECONDS, remaining))

        # Final page-fact check at the deadline.  A completion signal is not
        # required, so a ready page must never be reported as a timeout.
        final_fact = self._transcode_page_fact()
        last_log_at = self._log_transcode_wait(started, final_fact, last_log_at)
        elapsed = time.monotonic() - started
        if final_fact["failed"]:
            attempt.transcode_failed = True
            attempt.transcode_done = False
            attempt.progress_visible = False
            self._fail("transcode.failed: \u9875\u9762\u51fa\u73b0\u300c\u8f6c\u7801\u5931\u8d25\u300d",
                       step="transcode", code="transcode.failed")
        if final_fact["ready"]:
            self._mark_transcode_ready(attempt, final_fact, elapsed)
            return True
        # Defensive only: readiness is returned above, so this branch must not
        # be reachable.  Keep it distinct from a genuine timeout for diagnosis.
        if last_fact and last_fact.get("ready"):
            self._fail("transcode.ready_but_no_signal: \u9875\u9762\u5df2\u5c31\u7eea\u4f46\u7b49\u5f85\u5224\u5b9a\u672a\u8fd4\u56de",
                       step="transcode", code="transcode.ready_but_no_signal")
        attempt.transcode_failed = False
        attempt.progress_visible = bool(final_fact.get("blocking_text"))
        self._fail("transcode.timeout: \u7b49\u5f85\u672c\u6b21\u8d44\u6e90\u8f6c\u7801\u8d85\u65f6\uff08180 \u79d2\uff09",
                   step="transcode", code="transcode.timeout")

    def click_publish(self, plan):
        if not self.allow_submit or not self._filled:
            raise GateRed("\u672a\u5f00\u542f\u63d0\u4ea4\u6216\u8868\u5355\u672a\u5b8c\u6210")
        self.humanizer.think_pause(reason="\u63d0\u4ea4\u524d\u6700\u7ec8\u786e\u8ba4")
        self.humanizer.pre_submit_pause()
        self.verify_form(plan)
        self._dismiss_page_overlays()
        button, selector = self._unique_locator(PUBLISH_BUTTON_SELECTORS, timeout=5.0)
        if button is None:
            self._fail("\u627e\u4e0d\u5230\u552f\u4e00\u4e14\u53ef\u7528\u7684\u53d1\u5e03\u6309\u94ae\uff1a" + str(selector))
        self._record("publish", note="\u63d0\u4ea4\u524d\u6700\u7ec8\u8868\u5355\u622a\u56fe")
        before_submit = getattr(self, "before_submit", None)
        if not callable(before_submit):
            raise GateRed("\u7f3a\u5c11\u4e34\u95e8\u6388\u6743\u4e0e\u6301\u4e45\u63d0\u4ea4\u610f\u56fe\u56de\u8c03")
        # The humanizer calls this AFTER mouse movement/hover/trial, immediately
        # before the only real click. No random pause follows authorization.
        def commit():
            self.verify_form(plan)
            before_submit()
            if getattr(self, "_publish_receipt", None) is None:
                self._start_publish_observation(plan)
                self.upload_attempt.submit_started_at = time.time()
        result = self._click_with_retries(button, reason="\u70b9\u51fb\u53d1\u5e03", before_click=commit)
        if not result.get("ok"):
            self._fail("\u70b9\u51fb\u53d1\u5e03\u5931\u8d25\uff1a" + str(result.get("reason")))
        self._publish_click_completed_at = time.time()
        self._record("publish", note="\u5df2\u6267\u884c\u4e00\u6b21\u53d1\u5e03\u70b9\u51fb\uff0c\u7ed3\u679c\u4ecd\u5f85\u672c\u6b21\u5e73\u53f0\u56de\u6267")
        existing = getattr(self, "_publish_outcome", None)
        if existing and existing.get("result") == "published":
            return
        outcome = self._wait_for_publish_success()
        if outcome and outcome.get("result") == "published":
            return
        if outcome and outcome.get("result") == "failed":
            self._fail("\u53d1\u5e03\u5931\u8d25\uff1a" + str(outcome.get("reason")),
                       step="publish")
        self._handle_publish_verification()

    def _start_publish_observation(self, plan):
        if getattr(self, "_publish_receipt", None) is not None:
            raise GateRed("publish observation already armed; retry forbidden")
        attempt = self.upload_attempt
        self._publish_start_url = self._safe_page_url()
        self._publish_observation_started_at = time.time()
        start_text = self._safe_visible_text()
        self._publish_start_success_marker = self._first_marker(
            start_text, PUBLISH_SUCCESS_MARKERS)
        self._publish_start_failure_marker = self._first_marker(
            start_text, PUBLISH_FAILURE_MARKERS)
        self._publish_start_success_counts = self._marker_counts(
            start_text, PUBLISH_SUCCESS_MARKERS)
        self._publish_start_failure_counts = self._marker_counts(
            start_text, PUBLISH_FAILURE_MARKERS)
        self._publish_outcome = None
        self._finalized_publish = None
        baseline = set(self._baseline_edit_mids) if getattr(self, "_baseline_ready", False) else set()
        self._publish_receipt = PublishReceipt(
            self._page, DOUYIN_UPLOAD_URL, attempt.attempt_id, plan["account_id"],
            material_fingerprint(plan["piece"], account_id=plan["account_id"]),
            [attempt.resource_id] if attempt.resource_id else [],
            bind_submission=getattr(self, "bind_submission", None),
            page_work_probe=getattr(self, "_read_page_work_receipt", None),
            baseline_work_ids=baseline)
        self._publish_receipt._allow_page_fallback = False
        if self._publish_receipt.fault:
            self._fail(self._publish_receipt.fault, step="publish_unknown")
        install_monitor(self._page, self._publish_receipt, VERIFICATION_FAILURE_MARKERS)

    @staticmethod
    def _receipt_proof(receipt, *, allow_page_fallback=False):
        """Call old and new receipt objects without changing their API."""
        if receipt is None:
            return None
        if allow_page_fallback:
            try:
                receipt._allow_page_fallback = True
            except Exception:
                pass
        try:
            return receipt.proof()
        finally:
            if allow_page_fallback:
                try:
                    receipt._allow_page_fallback = False
                except Exception:
                    pass

    def _verification_interaction_visible(self):
        root = getattr(self, "_verification_root", None)
        if root is None:
            return False
        try:
            return bool(root.is_visible())
        except Exception as exc:
            self._fail("\u53d1\u5e03\u72b6\u6001\u672a\u77e5\uff1a\u9a8c\u8bc1\u5f39\u5c42\u72b6\u6001\u4e0d\u53ef\u67e5\uff1a" + str(exc),
                       step="publish_unknown")

    def _receipt_proof_with_fallback(self, receipt):
        """Use page fallback only when no SMS/QR interaction is still active."""
        proof = self._receipt_proof(receipt)
        if proof or getattr(receipt, "fault", None):
            return proof
        if self._verification_interaction_visible():
            return None
        return self._receipt_proof(receipt, allow_page_fallback=True)

    def _check_publish_observation(self, *, allow_verification=False):
        receipt = getattr(self, "_publish_receipt", None)
        try:
            # Pump events even with a proof: transport disconnects still latch.
            # DOM text and DOM query changes are diagnostics, never proof vetoes.
            query_error = None
            try:
                failure = self._verification_failure_reason()
            except Exception as exc:
                failure, query_error = None, exc
            if failure:
                if receipt:
                    receipt.observe_page_failure("VERIFICATION_FAILURE:" + failure)
                else:
                    raise VerificationQueryError(failure)
            if receipt:
                if query_error:
                    receipt.observe_page_failure("DOM_QUERY_FAILED:" + str(query_error))
                proof = receipt.proof()
                for reason in receipt.page_failures:
                    logged = getattr(self, "_logged_page_failures", set())
                    if reason not in logged:
                        self._record("publish_diagnostic", note=reason,
                                     extra={"diagnostic_only": True, "receipt_ready": bool(proof)})
                        logged.add(reason)
                        self._logged_page_failures = logged
                if receipt.fault:
                    raise VerificationQueryError(receipt.fault)
                if proof:
                    return
            if query_error and not receipt:
                raise VerificationQueryError(str(query_error))
            # A visible failure without a receipt waits for the existing receipt
            # deadline. A later bound success wins; expiry still freezes UNKNOWN.
        except Exception as exc:
            if receipt: receipt.fail(str(exc))
            self._fail(f"\u53d1\u5e03\u72b6\u6001\u672a\u77e5\uff1a{exc}", step="verification_unknown")

    def confirm_publish_evidence(self, plan, work_id):
        self._check_publish_observation()
        receipt = getattr(self, "_publish_receipt", None)
        proof = receipt.proof() if receipt else None
        evidence = getattr(self, "last_publish_evidence", None) or {}
        if (not proof or proof["work_id"] != work_id or not evidence.get("ok") or
                evidence.get("receipt") != proof or
                proof["attempt_id"] != self.upload_attempt.attempt_id or
                proof["account_id"] != plan["account_id"] or
                proof["fingerprint"] != material_fingerprint(plan["piece"], account_id=plan["account_id"])):
            self._fail("\u7f3a\u5c11\u7ed1\u5b9a\u672c\u6b21\u63d0\u4ea4\u7684\u5e73\u53f0\u56de\u6267\uff1b\u7ed3\u679c UNKNOWN", step="publish_unknown")
        return True

    def finish_publish_observation(self):
        receipt = getattr(self, "_publish_receipt", None)
        if receipt: receipt.close()

    def _real_work_candidates(self, timeout=5, *, click_through=False):
        """Read every edit-link href in a real DOM scan; never click list rows."""
        candidates = {}
        complete = True
        try:
            links = self._page.locator(", ".join(EDIT_LINK_SELECTORS))
            # evaluate_all is one atomic DOM snapshot, including hidden links.
            hrefs = links.evaluate_all("els => els.map(el => el.href || '')")
            for href in hrefs:
                mid = mid_from_url(href)
                if not mid:
                    complete = False
                    continue
                candidates.setdefault(mid, {"work_id": mid, "href": href,
                                            "source": "edit_link_href"})
        except Exception:
            complete = False
        self._last_edit_scan_complete = complete
        return list(candidates.values())

    def _return_to_manage_list(self):
        try:
            self._page.goto(DOUYIN_MANAGE_URL, wait_until="domcontentloaded")
            self.humanizer.page_load_pause()
            return True
        except Exception:
            return False

    def _read_published_at(self):
        """Read publication metadata, never an arbitrary date elsewhere on page."""
        try:
            values = self._page.locator(", ".join(PUBLISHED_AT_SELECTORS)).evaluate_all(
                """els => els.filter(el => el.getClientRects().length).map(el =>
                    el.getAttribute('datetime') || el.getAttribute('data-published-at') ||
                    el.getAttribute('title') || el.innerText || '')""")
        except Exception as exc:
            raise VerificationQueryError(str(exc)) from exc
        parsed = [parse_platform_time(v) for v in values]
        if not parsed or any(v is None for v in parsed) or len(set(parsed)) != 1:
            return None, {"reason": "PUBLISH_TIME_MISSING_OR_AMBIGUOUS", "raw": values}
        return parsed[0], {"raw": values}

    def _read_work_metadata(self, plan, candidate):
        """Read title/time only from the exact new mid, reject redirects/mismatch."""
        from urllib.parse import urljoin
        href = urljoin(DOUYIN_MANAGE_URL, str(candidate.get("href") or ""))
        mid = candidate.get("work_id")
        if (mid_from_url(href) != mid or
                urlparse(href).netloc != urlparse(DOUYIN_MANAGE_URL).netloc):
            return {"title": None, "published_at": None, "reason": "WORK_LINK_MISMATCH"}
        self._check_publish_observation()
        try:
            self._page.goto(href, wait_until="domcontentloaded")
            self.humanizer.page_load_pause()
            self._check_publish_observation()
            if mid_from_url(self._page.url) != mid or urlparse(self._page.url).netloc != urlparse(href).netloc:
                raise VerificationQueryError("work metadata redirected to another work/origin")
            self._verify_account(plan)
            failure = self._verification_failure_reason()
            if failure:
                raise VerificationQueryError("verification/failure present on metadata page")
            locator, _ = self._unique_locator(TITLE_SELECTORS, timeout=5)
            title = locator.evaluate("el => ('value' in el ? el.value : el.innerText)") if locator else None
            published_at, time_evidence = self._read_published_at()
            return {"title": title, "published_at": published_at,
                    "time_evidence": time_evidence, "href": self._page.url}
        except Exception as exc:
            return {"title": None, "published_at": None,
                    "reason": "WORK_METADATA_QUERY_FAILED", "detail": str(exc)}

    def _read_page_work_metadata(self, plan, candidate):
        """Read one exact edit page without treating the list as success."""
        from urllib.parse import urljoin
        href = urljoin(DOUYIN_MANAGE_URL, str(candidate.get("href") or ""))
        mid = candidate.get("work_id")
        if (mid_from_url(href) != mid or
                urlparse(href).netloc != urlparse(DOUYIN_MANAGE_URL).netloc):
            return None
        try:
            self._page.goto(href, wait_until="domcontentloaded")
            self.humanizer.page_load_pause()
            if (mid_from_url(self._page.url) != mid or
                    urlparse(self._page.url).netloc != urlparse(href).netloc):
                return None
            self._verify_account(plan)
            if self._verification_failure_reason():
                return None
            title_loc, _ = self._unique_locator(TITLE_SELECTORS, timeout=5)
            if title_loc is None:
                return None
            title = title_loc.evaluate("el => ('value' in el ? el.value : el.innerText)")
            body_loc, _ = self._unique_locator(BODY_SELECTORS, timeout=5)
            copy_body = (body_loc.evaluate("el => ('value' in el ? el.value : el.innerText)")
                         if body_loc is not None else None)
            topic_values = []
            for selector in ("[data-topic]", "[data-mention]"):
                try:
                    topic_values.extend(self._page.locator(selector).evaluate_all(
                        "els => els.map(el => el.getAttribute('data-topic') || "
                        "el.getAttribute('data-mention') || el.textContent || '')"))
                except Exception:
                    return None
            cover_state = self._cover_readback(require_bound=False)
            published_at, _ = self._read_published_at()
            return {"title": title, "copy_body": copy_body,
                    "topic_values": topic_values, "cover": cover_state,
                    "published_at": published_at, "href": self._page.url}
        except Exception:
            return None

    def _page_work_material_matches(self, plan, metadata):
        """Require the page fields to identify the uploaded material."""
        piece = plan.get("piece") or {}
        attempt = self.upload_attempt
        if not attempt:
            return False
        expected_title = _normalize_form_text(piece.get("title"))
        actual_title = _normalize_form_text(metadata.get("title"))
        if not expected_title or actual_title != expected_title:
            return False
        expected_body = piece.get("copy_body") or ""
        expected_topics = list(piece.get("topics") or [])
        if not _description_text_matches(metadata.get("copy_body"), expected_body,
                                         expected_topics):
            return False
        actual_topics = {_topic_name(_topic_token(value))
                         for value in (metadata.get("topic_values") or [])
                         if _topic_token(value)}
        actual_topics.update(_topic_name(token) for token in
                             _topic_tokens_in_text(metadata.get("copy_body") or ""))
        expected_topics_set = {_topic_name(value) for value in expected_topics
                               if _topic_name(value)}
        if expected_topics_set and actual_topics != expected_topics_set:
            return False
        if piece.get("covers") or piece.get("cover_sha256"):
            if not (metadata.get("cover") or {}).get("ok"):
                return False
        media_sha = piece.get("sha256") or piece.get("media_sha256")
        if not media_sha or media_sha != attempt.sha256:
            return False
        # Rebuild only fields the page exposed plus the already-bound upload
        # bytes; the resulting value must equal the approval fingerprint.
        observed_piece = dict(piece, title=piece.get("title") or "",
                              copy_body=expected_body,
                              topics=expected_topics,
                              sha256=media_sha,
                              cover_sha256=dict(piece.get("cover_sha256") or {}))
        return (material_fingerprint(observed_piece, account_id=plan["account_id"]) ==
                material_fingerprint(piece, account_id=plan["account_id"]))

    def _read_page_work_receipt(self, context):
        """Page fallback for an id-less create response; fail closed on doubt."""
        if getattr(self, "_page_work_probe_active", False):
            return None
        plan = getattr(self, "plan", None)
        attempt = getattr(self, "upload_attempt", None)
        if not isinstance(plan, dict) or not plan.get("piece") or not attempt:
            return None
        submitted_at = context.get("submitted_at")
        if not isinstance(submitted_at, (int, float)):
            return None
        baseline = {str(value) for value in (context.get("baseline_work_ids") or [])}
        self._page_work_probe_active = True
        try:
            self._page.goto(DOUYIN_MANAGE_URL, wait_until="domcontentloaded")
            self.humanizer.page_load_pause()
            if not self._wait_manage_list_ready(timeout=5):
                return None
            candidates = self._real_work_candidates(timeout=2)
            if not self._last_edit_scan_complete:
                return None
            matches = []
            for candidate in candidates[:10]:
                mid = candidate.get("work_id")
                if not is_valid_mid(mid) or mid in baseline:
                    continue
                metadata = self._read_page_work_metadata(plan, candidate)
                if not metadata:
                    continue
                published_at = metadata.get("published_at")
                if published_at is None or float(published_at) < float(submitted_at):
                    continue
                if not self._page_work_material_matches(plan, metadata):
                    continue
                matches.append((candidate, metadata))
                if len(matches) > 1:
                    break
            if len(matches) != 1:
                return None
            candidate, metadata = matches[0]
            return {"work_id": candidate["work_id"], "new_after_submit": True,
                    "fingerprint": context.get("fingerprint"),
                    "creation_id": context.get("creation_id"),
                    "video_id": context.get("video_id"),
                    "observed_at": time.time(), "page_url": metadata.get("href")}
        except Exception as exc:
            self._record("publish_diagnostic",
                         note="PAGE_RECEIPT_QUERY_FAILED:" + str(exc)[:300],
                         extra={"diagnostic_only": True})
            return None
        finally:
            self._page_work_probe_active = False

    def read_back_work_id(self, plan):
        outcome = getattr(self, "_publish_outcome", None)
        if outcome and outcome.get("result") == "published":
            return self._finalize_publish_outcome(plan, outcome)
        receipt = getattr(self, "_publish_receipt", None)
        deadline = time.monotonic() + max(0, float(getattr(self, "settings", {}).get("publish_receipt_timeout_seconds", 60)))
        proof = None
        while receipt:
            outcome = self._detect_publish_outcome()
            if outcome.get("result") == "published":
                self._remember_publish_outcome(outcome)
                return self._finalize_publish_outcome(plan, outcome)
            if outcome.get("result") == "failed":
                self._remember_publish_outcome(outcome)
                self._fail("\u53d1\u5e03\u5931\u8d25\uff1a" + str(outcome.get("reason")),
                           step="publish")
            self._check_publish_observation()
            proof = self._receipt_proof_with_fallback(receipt)
            if proof or receipt.fault: break
            if time.monotonic() >= deadline:
                receipt.fail("SUBMIT_RECEIPT_TIMEOUT")
                break
            self.humanizer.poll_pause()
        outcome = self._detect_publish_outcome()
        if outcome.get("result") == "published":
            self._remember_publish_outcome(outcome)
            return self._finalize_publish_outcome(plan, outcome)
        if outcome.get("result") == "failed":
            self._remember_publish_outcome(outcome)
            self._fail("\u53d1\u5e03\u5931\u8d25\uff1a" + str(outcome.get("reason")),
                       step="publish")
        if not proof:
            self._record("publish", note="\u672a\u53d6\u5f97 HTTP 200 + status_code=0 + 19 \u4f4d item_id \u5e73\u53f0\u56de\u6267\uff0c\u4e5f\u65e0\u9875\u9762\u6210\u529f\u4fe1\u53f7\uff1b\u7ed3\u679c UNKNOWN")
            return None
        # Keep the receipt path authoritative when it succeeded.
        outcome = {"result": "published", "signal": "create_v2",
                   "work_id": proof["work_id"], "receipt": proof}
        self._remember_publish_outcome(outcome)
        return self._finalize_publish_outcome(plan, outcome)

    def _finalize_publish_outcome(self, plan, outcome):
        receipt = getattr(self, "_publish_receipt", None)
        binding_failure = self._receipt_binding_failure(receipt)
        if outcome.get("result") == "published" and binding_failure:
            self._publish_outcome = {
                "result": "pending", "signal": "request_binding_failed",
                "reason": binding_failure}
            self._safe_record(
                "publish",
                note="\u672c\u6b21\u4e0a\u4f20\u4e0e\u5e73\u53f0\u56de\u6267\u7ed1\u5b9a\u4e0d\u4e00\u81f4\uff1b\u7ed3\u679c UNKNOWN\uff0c\u4e0d\u8bb0\u53d1\u5e03\u6210\u529f",
                extra={"signal": "request_binding_failed", "result": "UNKNOWN"})
            return None
        cached = getattr(self, "_finalized_publish", None)
        if cached is not None:
            return cached or None
        proof = outcome.get("receipt")
        work_id = outcome.get("work_id")
        if proof:
            work_id = proof["work_id"]
            # Account/form were checked before submit; later DOM cannot veto proof.
            self.last_publish_evidence = {
                "ok": True, "receipt": proof,
                "signal": outcome.get("signal"), "work_id": work_id}
            self._last_publish_evidence = self.last_publish_evidence
            self.confirm_publish_evidence(plan, work_id)
            if getattr(self, "upload_attempt", None) is not None:
                self.upload_attempt.final_work_id = work_id
            self._safe_record("publish",
                              note="\u672c\u6b21 create_v2 \u5e73\u53f0\u56de\u6267\u5df2\u786e\u8ba4",
                              extra=self.last_publish_evidence)
            self._finalized_publish = work_id
            return work_id
        self.last_publish_evidence = {
            "ok": True, "receipt": None,
            "signal": outcome.get("signal"),
            "work_id": work_id or None, "result": "SUCCESS"}
        self._last_publish_evidence = self.last_publish_evidence
        if work_id and getattr(self, "upload_attempt", None) is not None:
            self.upload_attempt.final_work_id = work_id
        self._safe_record(
            "publish",
            note="\u9875\u9762\u53d1\u5e03\u6210\u529f\u4fe1\u53f7\u5df2\u786e\u8ba4\uff1b" +
                 ("\u5df2\u4ece URL \u53d6\u5f97\u4f5c\u54c1 ID" if work_id else "\u672a\u53d6\u5f97\u4f5c\u54c1 ID"),
            extra={"signal": outcome.get("signal"), "work_id": work_id or None})
        self._finalized_publish = work_id or ""
        return work_id or None


def precheck(plan, ledger: Stage2Ledger, accounts, layout) -> list:
    """Three pre-publish checks (Mac publish-ops-rules section 4)."""
    reasons = []
    account_id = plan.get("account_id")
    if not any(a.account_id == account_id for a in accounts):
        reasons.append(f"unknown account {account_id}")
    if not (plan.get("compliance") or {}).get("content_hash"):
        reasons.append("plan.compliance.content_hash missing")
    key = publish_key_for(plan)
    state = ledger.state(key)
    if state in ("RESERVED", "CONSUMED"):
        reasons.append(f"idempotency key already {state}")
    return reasons


def run_publish(plan, driver, ledger: Stage2Ledger, *, mode=MODE_FILL,
                receipt=None, verify_key=None, canary_armed=False,
                post_fill_green=None, now=None, allow_simulated=False,
                approval_record=None, approval_preview=None,
                approval_refresh=None, final_check=None,
                republish_confirmed=False) -> dict:
    driver._pre_submit = False
    driver._pre_submit_failed = False
    simulated = bool(getattr(driver, "simulated", False))
    if simulated and not allow_simulated:
        raise GateRed("\u62d2\u7edd\u6a21\u62df\u9a71\u52a8\uff1b\u6d4b\u8bd5\u987b\u663e\u5f0f allow_simulated=True")
    result = {"mode": mode, "filled": False, "published": False,
              "platform_work_id": None, "work_id": None, "evidence": [],
              "env_id": plan.get("env_id"),
              "allow_submit": bool(getattr(driver, "allow_submit", False))}
    if mode == MODE_FILL:
        previous_mode = getattr(driver, "_operation_mode", None)
        had_mode = hasattr(driver, "_operation_mode")
        driver._operation_mode = MODE_FILL
        try:
            driver.fill(plan)
        finally:
            if had_mode:
                driver._operation_mode = previous_mode
            else:
                try:
                    del driver._operation_mode
                except AttributeError:
                    pass
        result.update(filled=True, evidence=list(getattr(driver, "last_evidence", []) or []),
                      topic_mode=getattr(driver, "topic_mode", None),
                      topic_modes=list(getattr(driver, "topic_modes", []) or []))
        return result
    if mode != MODE_APPROVED:
        raise GateRed("unknown publish mode")
    if not canary_armed:
        raise CanaryNotArmed("canary not armed")
    no_approval_mode = approval_record is None and approval_preview is None
    if not simulated and not no_approval_mode and (
            not callable(approval_refresh) or not approval_record or not approval_preview):
        raise GateRed("\u771f\u5b9e\u63d0\u4ea4\u5fc5\u987b\u6709\u98de\u4e66\u786e\u8ba4\u53ca\u4e34\u95e8\u8fdc\u7a0b\u5237\u65b0")
    if simulated and post_fill_green is not True:
        raise GateRed("post-fill not all green (test driver)")
    account_id = plan["account_id"]
    piece = plan["piece"]
    chash = material_fingerprint(piece, account_id=account_id)
    key = publish_key_for(plan)
    result["key"] = key

    def freeze_execution_failure(exc, *, step):
        """Freeze the channel for any execution failure, without downgrading a
        more specific hold already written by ``driver._fail()``."""
        store = getattr(driver, "_hold_store", None)
        channel = getattr(driver, "channel", None)
        channel_id = getattr(channel, "channel_id", None)
        if store is not None and channel_id:
            try:
                if store.get(channel_id):
                    return
            except Exception:
                pass
        freeze = getattr(driver, "freeze_unknown", None)
        if callable(freeze):
            try:
                freeze(str(exc) or type(exc).__name__, step=step)
            except Exception:
                # The caller's original failure must not be hidden by a second
                # freeze-time failure.  The CLI has an independent hold path.
                pass

    def authorize():
        nonlocal approval_record
        # Real calls always use the current clock; a caller cannot freeze it.
        clock = now if simulated else None
        require_receipt(receipt, plan, verify_key, now=clock,
                        operation_mode=MODE_APPROVED,
                        allow_republish=bool(republish_confirmed))
        if not no_approval_mode:
            if callable(approval_refresh):
                approval_record = approval_refresh()
            verdict = feishu_gate(approval_record, approval_preview, now=clock)
            if not simulated and (approval_record or {}).get("feishu", {}).get("dry_run") is not False:
                raise GateRed("\u5ba1\u6279\u670d\u52a1\u672a\u63d0\u4f9b\u771f\u5b9e\u98de\u4e66\u53d1\u9001\u7ed3\u679c\uff1b\u6a21\u62df\u786e\u8ba4\u4e0d\u80fd\u6388\u6743\u771f\u5b9e\u63d0\u4ea4")
            if not verdict["ok"]:
                raise GateRed("\u63d0\u4ea4\u6388\u6743\u672a\u901a\u8fc7\uff1a" + verdict["reason"])
        if callable(final_check):
            final_check()
    authorize()
    xingtu_task = xingtu_tasks.task_from_plan(plan)
    if xingtu_task:
        prepare = getattr(driver, "preflight_xingtu", None)
        if callable(prepare):
            prepare(plan)
    original_key = key
    base_identity = account_id + "|" + str(piece.get("sha256"))
    if xingtu_task.get("order_id"):
        base_identity += "|xingtu:" + str(xingtu_task["order_id"])
    elif xingtu_task.get("task_name"):
        base_identity += "|xingtu:" + str(xingtu_task["task_name"])
    replay_identity = base_identity
    reserve = ledger.reserve
    reserve_kwargs = {}
    if republish_confirmed:
        claim = (receipt or {}).get("republish") or {}
        nonce = str(claim.get("nonce") or "")
        if not nonce:
            raise GateRed("explicit republish requires a signed republish claim")
        # Only the confirmed CONSUMED chain is bypassed.  Receipt signature,
        # account, channel freeze, licence, canary and final checks still run
        # in this same path.  RESERVED / SUBMIT_INTENT / UNKNOWN never bypass.
        key = key + "|republish:" + nonce
        replay_identity = base_identity + "|republish:" + nonce
        result["key"] = key
        reserve = ledger.reserve_republish
        reserve_kwargs = {"original_key": original_key,
                          "base_identity": base_identity}
    state = reserve(key, account_id=account_id, schedule_at=plan["schedule_at"],
                    content_hash=chash, receipt_id=receipt["receipt_id"],
                    approval_id=(approval_record or {}).get("approval_id"),
                    replay_identity=replay_identity, **reserve_kwargs)
    if state != "NEW":
        if state == "NOT_CONSUMED":
            raise GateRed("explicit republish is only allowed after a CONSUMED publish")
        if state == "CONSUMED":
            raise IdempotencyConsumed(
                "idempotency: already CONSUMED; refusing to republish")
        raise GateRed(f"idempotency: already {state}; refusing to republish")
    # A pre-submit failure (Xingtu preflight, or any fill-stage exception before
    # the publish click) recycles its v2 RESERVED placeholder.  SUBMIT_INTENT /
    # UNKNOWN are never auto-recycled and require read-only reconciliation.
    driver._pre_submit = True
    driver._pre_submit_failed = False
    try:
        driver.fill(plan)
    except xingtu_tasks.XingtuError as exc:
        driver._pre_submit_failed = True
        # Every Xingtu stop is a pre-submit, pending-confirmation stop: the
        # task could not be listed, was not attached to the upload page, was
        # already submitted, or the duplicate check was unavailable.  None of
        # these is an uncertain publish attempt, so recycle the reservation and
        # leave the channel unfrozen.  The caller surfaces the specific reason.
        xingtu_recycled = False
        try:
            xingtu_recycled = ledger.recycle_zombie(
                key, reason="xingtu preflight: " + type(exc).__name__)
        except Exception:
            xingtu_recycled = False
        if not xingtu_recycled:
            driver._pre_submit_failed = False
            driver._pre_submit = False
            freeze_execution_failure(exc, step="xingtu_pre_submit_recycle_failed")
        raise
    except BaseException as exc:
        if getattr(driver, "_click_failed", False):
            ledger.mark_unknown(key, "click failed twice during fill")
            freeze_execution_failure(exc, step="fill_unknown")
        else:
            # Fill stopped before the publish button was pressed, so nothing was
            # submitted.  Recycle the v2 reservation instead of leaving it
            # RESERVED (which used to force manual ledger surgery before the
            # same material+task could be retried) and leave the channel
            # unfrozen: a pre-submit failure is not an uncertain publish.
            driver._pre_submit_failed = True
            recycled = False
            try:
                recycled = ledger.recycle_zombie(
                    key, reason="fill pre-submit: " + type(exc).__name__)
            except Exception:
                recycled = False
            if not recycled:
                driver._pre_submit_failed = False
                driver._pre_submit = False
                freeze_execution_failure(exc, step="fill_pre_submit_recycle_failed")
            else:
                # The reservation is gone, but an account-level stop (captcha,
                # SMS, risk control, lost login) must still freeze the channel:
                # otherwise every queued task walks into the same wall.
                try:
                    pre_kind = classify_fault({"reason": str(exc), "step": "fill"})
                except Exception:
                    pre_kind = None
                if pre_kind in PRE_SUBMIT_FREEZE_KINDS:
                    driver._pre_submit = False
                    held = False
                    store = getattr(driver, "_hold_store", None)
                    channel_id = getattr(getattr(driver, "channel", None), "channel_id", None)
                    if store is not None and channel_id:
                        try:
                            held = store.get(channel_id) is not None
                        except Exception:
                            held = False
                    apply_hold = getattr(driver, "_apply_hold", None)
                    if not held and callable(apply_hold):
                        try:
                            apply_hold(str(exc), "fill", {"code": pre_kind},
                                       kind=pre_kind)
                        except Exception:
                            freeze_execution_failure(exc, step="fill_account_hold")
                    elif not held:
                        freeze_execution_failure(exc, step="fill_account_hold")
        raise
    finally:
        driver._pre_submit = False
    result["filled"] = True
    result["topic_mode"] = getattr(driver, "topic_mode", None)
    result["topic_modes"] = list(getattr(driver, "topic_modes", []) or [])
    if not simulated and not getattr(driver, "allow_submit", False):
        ledger.recycle_zombie(key, reason="explicit no-submit mode")
        raise NoSubmitExit("--allow-submit \u672a\u5f00\u542f\uff1b\u8868\u5355\u5b8c\u6210\uff0c\u672a\u70b9\u51fb\u53d1\u5e03")
    try:
        if not simulated and not driver.verify_form(plan):
            raise GateRed("\u6700\u7ec8 DOM \u56de\u8bfb\u5931\u8d25")
    except BaseException as exc:
        freeze_execution_failure(exc, step="pre_submit_unknown")
        raise
    intent_written = False
    def before_submit():
        nonlocal intent_written
        authorize()
        if not simulated:
            driver.verify_form(plan)
            require_receipt(receipt, plan, verify_key,
                            operation_mode=MODE_APPROVED,
                            allow_republish=bool(republish_confirmed))
        if not intent_written:
            ledger.mark_submit_intent(key, attempt_id=getattr(getattr(driver, "upload_attempt", None), "attempt_id", None))
            intent_written = True
    driver.before_submit = before_submit
    driver.bind_submission = lambda creation, video: ledger.bind_submission(key, creation, video)
    try:
        if simulated:
            before_submit()
        driver.click_publish(plan)
        if not intent_written:
            raise GateRed("driver did not execute submit authorization callback")
        work_id = driver.read_back_work_id(plan)
        evidence = getattr(driver, "last_publish_evidence", None)
        receipt_proof = evidence.get("receipt") if isinstance(evidence, dict) else None
        # UI success has no receipt.  A receipt-shaped object with no usable
        # work ID is not page evidence; it must stay UNKNOWN.
        page_published = bool(isinstance(evidence, dict) and
                              evidence.get("ok") is True and not receipt_proof)
        if not work_id and not page_published:
            raise GateRed("publish result UNKNOWN: no uniquely bound platform work id")
        if work_id and not receipt_proof and not page_published:
            raise GateRed("publish result UNKNOWN: work id without this attempt's receipt")
        if work_id and receipt_proof and not simulated:
            driver.confirm_publish_evidence(plan, str(work_id))
        if xingtu_task:
            record_used = getattr(driver, "record_xingtu_publish", None)
            if callable(record_used):
                record_used(plan, {"platform_work_id": str(work_id or ""),
                                   "signal": (evidence or {}).get("signal")})
        if work_id and receipt_proof:
            ledger.consume(key, str(work_id),
                           before_consume=(lambda: driver.confirm_publish_evidence(plan, str(work_id))) if not simulated else None,
                           account_id=account_id,
                           env_id=plan.get("env_id"), content_hash=chash,
                           publish_evidence=evidence)
        elif page_published:
            # The page confirmed success but there is no consumable receipt.
            # Keep the row in SUBMIT_INTENT so this exact key cannot replay,
            # while recording that the outcome is known so another scheduled
            # publish on the same account is not blocked forever.
            ledger.mark_publish_confirmed(
                key, signal=(evidence or {}).get("signal"),
                work_id=str(work_id) if work_id else None,
                attempt_id=getattr(getattr(driver, "upload_attempt", None),
                                   "attempt_id", None))
    except BaseException as exc:
        ledger_failure = None
        if intent_written or getattr(driver, "_click_failed", False):
            # If the write fails, durable SUBMIT_INTENT still prevents replay.
            try:
                ledger.mark_unknown(
                    key, "submit/readback interrupted; reconcile before any retry")
            except BaseException as ledger_exc:
                ledger_failure = ledger_exc
        freeze_execution_failure(
            exc, step="publish_unknown" if intent_written else "pre_submit_unknown")
        if ledger_failure is not None:
            raise ledger_failure from exc
        raise
    finally:
        driver.before_submit = None
        driver.bind_submission = None
        finish = getattr(driver, "finish_publish_observation", None)
        if callable(finish): finish()
    result.update(published=True, platform_work_id=str(work_id or ""),
                  work_id=str(work_id or ""), submit_attempts=1,
                  republish=bool(republish_confirmed),
                  evidence=list(getattr(driver, "last_evidence", []) or []))
    return result


def run_approved_publish(plan, driver, ledger: Stage2Ledger, *,
                         approval_record, approval_preview, **kwargs) -> dict:
    """Compatibility wrapper: Feishu confirmation is checked before run_publish.

    A pending / timeout / rejected answer, or a confirmation bound to a
    different preview, never reaches ``driver.click_publish``.  The ledger in
    ``run_publish`` remains the second line of defence against a confirmed
    replay publishing twice.
    """
    verdict = feishu_gate(approval_record, approval_preview)
    if not verdict["ok"]:
        raise GateRed("\u98de\u4e66\u53d1\u5e03\u786e\u8ba4\u672a\u901a\u8fc7\uff1a" + verdict["reason"])
    return run_publish(plan, driver, ledger, mode=MODE_APPROVED,
                       approval_record=approval_record,
                       approval_preview=approval_preview, **kwargs)
