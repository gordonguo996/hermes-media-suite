"""1.2.216: >50MB uploads go through CDP instead of the playwright driver.

Client evidence (2026-11-13, cust-1y-20261109-1357): a 172MB video failed with
``Locator.set_input_files: Cannot transfer files larger than 50Mb to a browser
not co-located with the server``. The browser runs on the same machine, so the
path can be handed to it directly with DOM.setFileInputFiles.
"""
import os

from agent.winport import publish_exec


class FakeHandle:
    def __init__(self):
        self.attrs = {}
        self.calls = []

    def evaluate(self, script, arg=None):
        self.calls.append((script, arg))
        if "setAttribute" in script:
            self.attrs["data-hl-upload"] = arg
        elif "removeAttribute" in script:
            self.attrs.pop("data-hl-upload", None)
        return None


class FakeLocator:
    def __init__(self, handle):
        self.handle = handle

    def element_handle(self):
        return self.handle


class FakeSession:
    def __init__(self, tree):
        self.tree = tree
        self.sent = []
        self.detached = False

    def send(self, method, params=None):
        self.sent.append((method, params or {}))
        if method == "DOM.getDocument":
            return {"root": self.tree}
        return {}

    def detach(self):
        self.detached = True


class FakeContext:
    def __init__(self, session):
        self.session = session
        self.targets = []

    def new_cdp_session(self, target):
        self.targets.append(target)
        return self.session


class FakePage:
    def __init__(self, frames, context):
        self.frames = list(frames)
        self.context = context


def make_driver(tree, frames=None, pick=None):
    session = FakeSession(tree)
    context = FakeContext(session)
    page = FakePage(frames or ["frame0"], context)
    handle = FakeHandle()
    driver = object.__new__(publish_exec.PlaywrightDriver)
    driver._page = page
    driver._upload_input_pick = pick or {"frame": 0, "index": 0}
    return driver, session, context, handle


def _tree_with_marker(marker, node_id=42):
    return {"nodeId": 1, "children": [
        {"nodeId": 2, "attributes": ["type", "file"]},
        {"nodeId": 3, "shadowRoots": [
            {"nodeId": node_id, "attributes": ["data-hl-upload", marker]}]},
    ]}


def test_large_upload_uses_cdp_marker_and_local_path(monkeypatch):
    marker = "hl123"
    monkeypatch.setattr(publish_exec.time, "time", lambda: 0.123)
    driver, session, context, handle = make_driver(_tree_with_marker(marker),
                                                   frames=["f0", "f1"])
    media = "C:/tmp/big.mp4"
    driver._set_input_files_cdp(FakeLocator(handle), media)

    assert context.targets == ["f0"]
    methods = [m for m, _ in session.sent]
    assert "DOM.getDocument" in methods and "DOM.setFileInputFiles" in methods
    sent = dict(session.sent)["DOM.setFileInputFiles"]
    assert sent["nodeId"] == 42
    assert sent["files"] == [os.path.abspath(media)]
    assert session.detached is True
    # marker cleaned up, not left on the page
    assert "data-hl-upload" not in handle.attrs


def test_large_upload_missing_marker_raises():
    driver, session, _, handle = make_driver({"nodeId": 1, "children": []})
    try:
        driver._set_input_files_cdp(FakeLocator(handle), "C:/tmp/x.mp4")
    except RuntimeError as exc:
        assert "cdp_input_missing" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")
    assert session.detached is True


def test_threshold_stays_below_playwright_limit():
    assert publish_exec.LARGE_UPLOAD_CDP_BYTES < 50 * 1000 * 1000
