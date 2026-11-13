"""1.2.210: video upload input is picked by accept+enabled, never by index.

Client evidence (2026-11-10, douyin creator center):
  * /content/upload  -> exactly one input[type=file] (video, enabled)
  * /content/post/video -> two inputs (0 = cover image, 1 = video)
Picking by index made the client wait 30s for an element that never exists
and then fail the whole publish, so the picker must filter and fail closed.
"""
import pytest

from agent.winport import publish_exec
from agent.winport.errors import GateRed


class FakeNode:
    def __init__(self, accept, enabled=True, visible=False):
        self.accept = accept
        self.enabled = enabled
        self.visible = visible

    def get_attribute(self, name):
        return self.accept if name == "accept" else ""

    def is_enabled(self):
        return self.enabled

    def bounding_box(self):
        return {"width": 10, "height": 10} if self.visible else None


class FakeLocator:
    def __init__(self, nodes):
        self.nodes = nodes

    def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]


class FakeFrame:
    def __init__(self, nodes):
        self.nodes = nodes

    def locator(self, selector):
        assert selector == "input[type=file]"
        return FakeLocator(self.nodes)


def make_driver(frames):
    driver = object.__new__(publish_exec.PlaywrightDriver)
    driver._page = object()
    driver._progress_events = []

    def _progress(event, **fields):
        driver._progress_events.append((event, fields))

    def _fail(message, step=None):
        raise GateRed(message)

    driver._progress = _progress
    driver._fail = _fail
    return driver


@pytest.fixture(autouse=True)
def patch_frames(monkeypatch):
    holder = {}

    def _frames(page):
        seq = holder.get("seq")
        if seq is not None:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return holder["frames"]

    monkeypatch.setattr(publish_exec, "_page_frames", _frames)
    holder["frames"] = []
    return holder


VIDEO = "video/mp4,video/x-m4v,.flv,.avi"
IMAGE = "image/png,image/jpeg"


def test_clean_upload_page_uses_the_only_video_input(patch_frames):
    node = FakeNode(VIDEO)
    patch_frames["frames"] = [FakeFrame([node])]
    driver = make_driver(patch_frames["frames"])
    locator, selector, scan = driver._wait_usable_upload_input(2)
    assert locator is node
    assert selector == "input[type=file]"
    assert len(scan["candidates"]) == 1


def test_config_page_skips_cover_input_by_accept(patch_frames):
    cover = FakeNode(IMAGE)
    video = FakeNode(VIDEO)
    patch_frames["frames"] = [FakeFrame([cover, video])]
    driver = make_driver(patch_frames["frames"])
    locator, _, _ = driver._wait_usable_upload_input(2)
    assert locator is video


def test_disabled_input_is_never_returned(patch_frames):
    patch_frames["frames"] = [FakeFrame([FakeNode(VIDEO, enabled=False)])]
    driver = make_driver(patch_frames["frames"])
    with pytest.raises(GateRed):
        driver._wait_usable_upload_input(0.2)


def test_two_usable_invisible_inputs_fail_closed(patch_frames):
    patch_frames["frames"] = [FakeFrame([FakeNode(VIDEO), FakeNode(VIDEO)])]
    driver = make_driver(patch_frames["frames"])
    with pytest.raises(GateRed):
        driver._wait_usable_upload_input(0.2)
    assert any(event == "upload.input.ambiguous"
               for event, _ in driver._progress_events)


def test_two_usable_inputs_with_one_visible_is_picked(patch_frames):
    hidden = FakeNode(VIDEO)
    shown = FakeNode(VIDEO, visible=True)
    patch_frames["frames"] = [FakeFrame([hidden, shown])]
    driver = make_driver(patch_frames["frames"])
    locator, _, _ = driver._wait_usable_upload_input(2)
    assert locator is shown


def test_polls_until_the_input_appears(patch_frames):
    node = FakeNode(VIDEO)
    patch_frames["seq"] = [[], [], [FakeFrame([node])]]
    driver = make_driver(patch_frames["seq"][0])
    locator, _, _ = driver._wait_usable_upload_input(5)
    assert locator is node
