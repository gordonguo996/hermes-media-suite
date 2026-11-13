"""1.2.212: every requested topic must really land on the publish page.

Field failure (2026-11-11 17:11, env_1): four topics were configured
(#艾绒肚挤贴 #温和草本 #健康生活好物 #草本); the page ended up with a single chip
(#艾绒肚脐贴) and the run died with "话题回读不一致".  Two separate defects:

  1. only the first tag was committed-verified, the rest were typed blind;
  2. Douyin rewrites a tag to the canonical spelling (挤 -> 脐), and an exact
     string compare called that a mismatch.

The fix verifies each tag after entry, retries once, records what is still
missing, and accepts a one-character correction as the same topic.
"""
import sys
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLIENT))
sys.path.insert(0, str(CLIENT / "agent"))

import winport.publish_exec as pe  # noqa: E402  (needs the agent path above)


def test_exact_tokens_match():
    assert pe._topic_close_enough("#温和草本", "#温和草本")


def test_one_character_platform_correction_is_the_same_topic():
    assert pe._topic_close_enough("#艾绒肚挤贴", "#艾绒肚脐贴")


def test_missing_or_extra_character_is_close():
    assert pe._topic_close_enough("#草本", "#草本植物") is False
    assert pe._topic_close_enough("#艾绒肚脐贴", "#艾绒肚脐贴") is True


def test_unrelated_tokens_do_not_match():
    assert pe._topic_close_enough("#温和草本", "#健康生活好物") is False


def test_two_differences_do_not_match():
    assert pe._topic_close_enough("#温暖草本", "#温和草原") is False


class _Humanizer:
    def step_pause(self, **kwargs):
        return None

    def poll_pause(self):
        return None


class _Driver:
    """PlaywrightDriver without Playwright: only the topic plumbing."""

    _add_topics = pe.PlaywrightDriver._add_topics
    _topic_present = pe.PlaywrightDriver._topic_present
    _topic_chip_texts = pe.PlaywrightDriver._topic_chip_texts
    _topic_name = staticmethod(pe._topic_name)
    _new_chip = pe.PlaywrightDriver._new_chip
    _plain_editor_text = pe.PlaywrightDriver._plain_editor_text

    def __init__(self, failing_first_round):
        self.humanizer = _Humanizer()
        self.topic_modes = []
        self.topic_mode = "none"
        self.topic_missing = []
        self.chips = []
        self.typed = []
        self.failing_first_round = set(failing_first_round)
        self.round = 0
        self.progress = []

    def _unique_locator(self, selectors, timeout=0, visible=True):
        return ("EDITOR", None)

    def _focus_editor_end(self, editor):
        return True

    def _ensure_editor_separator(self, editor, force=False):
        return None

    def _editor_text(self, editor):
        return ""

    def _topic_conversion_timeout(self):
        return 0.0

    def _select_topic_or_fallback(self, editor, topic):
        self.typed.append(topic)
        if self.round == 1 and topic in self.failing_first_round:
            return ""
        self.chips.append(pe._topic_token(topic))
        return pe._topic_token(topic)

    def _wait_topic_present(self, editor, topic, timeout=None):
        token = pe._topic_token(topic)
        return "chip" if token in self.chips else ""

    def _progress(self, name, **kwargs):
        self.progress.append((name, kwargs))


def test_every_topic_is_verified_and_missing_one_is_retried():
    driver = _Driver(failing_first_round={"#健康生活好物"})
    driver.fail_once = {"#健康生活好物"}

    def select(editor, topic):
        driver.typed.append(topic)
        if topic in driver.fail_once:
            driver.fail_once.discard(topic)
            return ""
        driver.chips.append(pe._topic_token(topic))
        return pe._topic_token(topic)

    driver._select_topic_or_fallback = select
    driver._add_topics(["#艾绒肚挤贴", "#温和草本", "#健康生活好物", "#草本"])
    assert sorted(driver.chips) == sorted(
        ["#艾绒肚挤贴", "#温和草本", "#健康生活好物", "#草本"])
    assert driver.typed.count("#健康生活好物") == 2       # first try + retry
    assert driver.topic_missing == []


def test_topic_that_never_sticks_is_reported():
    driver = _Driver(failing_first_round=set())
    driver._select_topic_or_fallback = lambda editor, topic: ""
    driver._wait_topic_present = lambda editor, topic, timeout=None: ""
    driver._add_topics(["#温和草本"])
    assert driver.topic_missing == ["#温和草本"]
    assert any(name == "form.topics.missing" for name, _ in driver.progress)

def test_half_typed_token_is_not_accepted_as_landed():
    assert pe._topic_token_in_text("#温和", "#温和") is True
    assert pe._topic_token_in_text("#温和", "#温和草本") is False
    assert pe._topic_token_in_text("#温和", "正文 #温和 ") is True


def test_one_chip_cannot_satisfy_two_wanted_tags():
    pairs, missing, unexpected = pe._match_topics(["#草本", "#草木"], ["#草本"])
    assert len(pairs) == 1
    assert missing == ["#草木"]
    assert unexpected == []


def test_platform_correction_is_recorded_as_a_pair():
    pairs, missing, unexpected = pe._match_topics(["#艾绒肚挤贴"], ["#艾绒肚脐贴"])
    assert pairs == [("#艾绒肚挤贴", "#艾绒肚脐贴")]
    assert missing == [] and unexpected == []


def test_description_readback_accepts_corrected_tag():
    assert pe._description_text_matches(
        "一贴一个不吱声 植物太师肚脐贴 #艾绒肚脐贴 ", "一贴一个不吱声 植物太师肚脐贴", ["#艾绒肚挤贴"]) is True


def test_description_readback_still_rejects_a_different_tag():
    assert pe._description_text_matches(
        "正文 #温和草本", "正文", ["#草本"]) is False

def test_near_miss_never_steals_an_exact_partner():
    pairs, missing, unexpected = pe._match_topics(["#草本", "#草木"], ["#草木", "#花本"])
    assert missing == []
    assert dict(pairs) == {"#草本": "#花本", "#草木": "#草木"}


def test_unexpected_chips_are_chips_without_a_planned_tag():
    _pairs, _missing, unexpected = pe._match_topics(["#草本"], ["#草本", "#旅行"])
    assert unexpected == ["#旅行"]


class _Keyboard:
    def __init__(self, sink, driver=None):
        self.sink = sink
        self.driver = driver

    def press(self, key):
        self.sink.append(key)
        if key == "Space" and self.driver is not None and self.driver.convert_on_space:
            self.driver.chips.append("#温和草本")
            self.driver.raw = ""


class _Page:
    def __init__(self, sink, driver=None):
        self.keyboard = _Keyboard(sink, driver)


class _Editor:
    def __init__(self, driver, sink):
        self.driver = driver
        self.sink = sink

    def press(self, key):
        self.sink.append(key)
        if key == "Enter":
            self.driver.chips.append("#温和草本")
            self.driver.raw = ""


class _TypingDriver(_Driver):
    _select_topic_or_fallback = pe.PlaywrightDriver._select_topic_or_fallback
    _wait_topic_chip = pe.PlaywrightDriver._wait_topic_chip
    _new_chip = pe.PlaywrightDriver._new_chip

    def __init__(self):
        super().__init__(failing_first_round=set())
        self.chips = []
        self.sink = []
        self.raw = ""
        self.convert_on_space = False
        self._page = _Page(self.sink, self)
        self.humanizer = type("H", (), {"type_into": lambda self, page, text: setattr(drv, "raw", text)})()
        drv = self
        self.humanizer.type_into = lambda page, text: setattr(self, "raw", text)

    def _topic_chip_texts(self):
        return list(self.chips)

    def _editor_text(self, editor):
        return self.raw

    def _topic_conversion_timeout(self):
        return 0.0


def test_enter_is_used_when_space_does_not_convert():
    driver = _TypingDriver()
    editor = _Editor(driver, driver.sink)
    mode = driver._select_topic_or_fallback(editor, "#温和草本")
    assert mode == "#温和草本"
    assert driver.sink == ["Space", "Enter"]


def test_space_alone_converts_without_pressing_enter():
    driver = _TypingDriver()
    driver.convert_on_space = True            # Space converts it
    editor = _Editor(driver, driver.sink)
    mode = driver._select_topic_or_fallback(editor, "#温和草本")
    assert mode == "#温和草本"
    assert driver.sink == ["Space"]

def test_old_near_chip_does_not_count_as_this_entry():
    driver = _TypingDriver()
    driver.chips.append("#温和草")            # older, near-miss chip
    editor = _Editor(driver, driver.sink)
    mode = driver._select_topic_or_fallback(editor, "#温和草本")
    assert mode == "#温和草本"
    assert driver.sink == ["Space", "Enter"]   # still had to convert


def test_claim_uses_the_real_corrected_chip():
    # planned typo and its corrected twin: the platform unifies both, so the
    # second tag must NOT be treated as already present.
    driver = _Driver(failing_first_round=set())
    driver.chips = []
    orig_select = None

    def select(editor, topic):
        token = pe._topic_token(topic)
        if token == "#艾绒肚挤贴":
            if "#艾绒肚脐贴" not in driver.chips:
                driver.chips.append("#艾绒肚脐贴")
                return "#艾绒肚脐贴"          # platform correction
            return ""
        driver.chips.append(token)
        return token

    driver._select_topic_or_fallback = select
    driver._topic_chip_texts = lambda: list(driver.chips)
    driver._add_topics(["#艾绒肚挤贴", "#艾绒肚脐贴"])
    assert driver.chips.count("#艾绒肚脐贴") == 2


def test_maximum_matching_finds_a_complete_pairing():
    pairs, missing, unexpected = pe._match_topics(["#草本", "#草木"], ["#草水", "#花本"])
    assert missing == [] and unexpected == []
    assert len(pairs) == 2

def test_new_chip_returns_the_added_one_not_an_old_lookalike():
    driver = _TypingDriver()
    driver.chips.extend(["#草本", "#草木"])
    assert driver._new_chip(["#草木"], "#草本") == "#草本"


def test_chip_label_is_not_read_back_as_plain_text():
    driver = _TypingDriver()
    driver.chips.append("#艾绒肚脐贴")
    driver.raw = "#艾绒肚脐贴"
    assert "#艾绒肚脐贴" not in driver._plain_editor_text(None)


def test_complete_pairing_beats_exact_locking():
    # Only one complete pairing exists here: #草本 must give up its exact
    # partner so #草木 (one character from #草本) can take it.
    pairs, missing, unexpected = pe._match_topics(["#草本", "#草木"], ["#草本", "#花本"])
    assert missing == [] and unexpected == []
    assert dict(pairs) == {"#草本": "#花本", "#草木": "#草本"}


def test_exact_spelling_is_preferred_when_both_pairings_work():
    pairs, missing, unexpected = pe._match_topics(["#草本", "#草木"], ["#草药", "#草本"])
    assert missing == [] and unexpected == []
    assert dict(pairs)["#草本"] == "#草本"

def test_plain_text_copy_survives_when_a_chip_has_the_same_label():
    driver = _TypingDriver()
    driver.chips.append("#艾绒肚脐贴")
    driver.raw = "#艾绒肚脐贴 #艾绒肚脐贴"      # one chip + one plain tag
    plain = driver._plain_editor_text(None)
    assert plain.count("#艾绒肚脐贴") == 1


def test_swap_pass_terminates_on_duplicate_tags():
    pairs, missing, unexpected = pe._match_topics(["#草本", "#草本"], ["#草本", "#草木"])
    assert len(pairs) == 2
    assert missing == [] and unexpected == []

