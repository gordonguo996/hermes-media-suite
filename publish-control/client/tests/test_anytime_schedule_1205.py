"""1.2.205: publish at any time — no windows, 3-minute floor."""
import json
from datetime import datetime, timedelta, timezone

from agent.winport import panel, schedule, settings

TZ = timezone(timedelta(hours=8))


def parse_slot(slot):
    if isinstance(slot, dict):
        slot = slot.get("scheduled_at") or slot.get("schedule_at")
    return schedule.parse_time(slot).astimezone(TZ)


def test_fresh_install_defaults_are_anytime(tmp_path):
    cfg = settings.load_settings(tmp_path / "missing.json")
    sch = cfg["schedule"]
    assert sch["min_lead_minutes"] == 3
    assert sch["avoid_minute_marks"] == []
    assert sch["windows"] == [["00:00", "23:59"]]
    assert [e["id"] for e in sch["window_entries"]] == ["allday"]


def test_legacy_settings_are_migrated(tmp_path):
    legacy = {"schedule": {
        "min_lead_minutes": 130,
        "avoid_minute_marks": [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55],
        "windows": [["07:00", "09:30"], ["11:00", "13:30"],
                    ["17:00", "19:30"], ["20:00", "22:30"]],
        "window_entries": [
            {"id": "morning", "start": "07:00", "end": "09:30", "enabled": True},
            {"id": "noon", "start": "11:00", "end": "13:30", "enabled": True},
            {"id": "evening", "start": "17:00", "end": "19:30", "enabled": True},
            {"id": "night", "start": "20:00", "end": "22:30", "enabled": True},
        ]}}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    sch = settings.load_settings(path)["schedule"]
    assert sch["min_lead_minutes"] == 3
    assert sch["windows"] == [["00:00", "23:59"]]
    assert [e["id"] for e in sch["window_entries"]] == ["allday"]


def test_validate_window_only_enforces_three_minutes():
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    assert schedule.validate_window(now + timedelta(minutes=2), now)[0] is False
    assert schedule.validate_window(now + timedelta(minutes=4), now)[0] is True
    late_night = now.replace(hour=23, minute=30, second=0, microsecond=0)
    assert schedule.validate_window(
        late_night + timedelta(minutes=5), late_night)[0] is True


def test_panel_has_no_window_editor():
    html_text = panel._cadence_section({
        "settings": {"min_lead_minutes": 3, "gap_min": 1, "gap_max": 3,
                     "windows": [["00:00", "23:59"]],
                     "window_entries": [{"id": "allday", "start": "00:00",
                                         "end": "23:59", "enabled": True}]},
        "tasks": [], "unplaced": [], "today_published": {}})
    assert "cad-windows" not in html_text
    assert "cadence-window-start" not in html_text
    assert "saveCadence" in html_text
    assert "window_entries" not in html_text.split("<script>")[-1].split("saveCadence")[0]


def test_legacy_settings_without_lead_or_custom_entries_are_migrated(tmp_path):
    for payload in (
        {"schedule": {"window_entries": [
            {"id": "custom-a", "start": "09:00", "end": "10:00", "enabled": True}]}},
        {"schedule": {"min_lead_minutes": 30}},
        {"schedule": {"min_lead_minutes": 0, "windows": [["08:00", "09:00"]]}},
    ):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        sch = settings.load_settings(path)["schedule"]
        assert sch["min_lead_minutes"] == 3
        assert sch["windows"] == [["00:00", "23:59"]]
        assert [e["id"] for e in sch["window_entries"]] == ["allday"]


def test_next_slot_keeps_full_lead_even_with_seconds():
    now = datetime(2026, 11, 9, 17, 0, 30, tzinfo=TZ)
    slot = schedule.next_slot(now)
    assert slot.second == 0 and slot.microsecond == 0
    assert slot >= now + timedelta(minutes=3)
    assert schedule.validate_window(slot, now)[0] is True

    now_clean = datetime(2026, 11, 9, 17, 0, 0, tzinfo=TZ)
    slot2 = schedule.next_slot(now_clean)
    assert schedule.validate_window(slot2, now_clean)[0] is True


def test_catchup_slots_never_start_below_lead():
    now = datetime(2026, 11, 9, 17, 0, 59, tzinfo=TZ)
    cfg = settings.load_settings("/nonexistent/settings.json")["schedule"]
    slots = schedule.pick_slots(3, now=now, settings={"schedule": cfg})
    assert slots
    for slot in slots:
        assert slot >= now + timedelta(minutes=3)


def test_multi_pick_respects_lead_with_seconds():
    now = datetime(2026, 11, 9, 17, 0, 59, tzinfo=TZ)
    cfg = settings.load_settings("/nonexistent/settings.json")["schedule"]
    items = [{"id": f"t{i}", "account_id": "acc1", "title": f"t{i}",
              "file_name": f"{i}.mp4"} for i in range(4)]
    res = schedule.pick_slots(items, now=now, settings={"schedule": cfg}, seed=139)
    placed = res.get("assigned") or res.get("slots") or []
    times = [parse_slot(s) for s in placed]
    assert times, res
    for dt in times:
        assert dt >= now + timedelta(minutes=3), (dt, now)


def test_migration_ignores_disabled_or_bad_all_day_entry(tmp_path):
    payload = {"schedule": {"min_lead_minutes": 3, "avoid_minute_marks": [],
                            "windows": [["00:00", "23:59"]],
                            "window_entries": [{"id": "allday", "start": "00:00",
                                                "end": "23:59", "enabled": False}]}}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    sch = settings.load_settings(path)["schedule"]
    assert sch["window_entries"][0]["enabled"] is True
    assert schedule.window_contains(datetime(2026, 11, 9, 3, 0, tzinfo=TZ),
                                    settings={"schedule": sch}) is True


def test_migration_survives_junk_lead_and_disabled_entry(tmp_path):
    cases = [
        {"schedule": {"min_lead_minutes": None}},
        {"schedule": {"min_lead_minutes": "3.0"}},
        {"schedule": {"min_lead_minutes": "abc"}},
        {"schedule": {"min_lead_minutes": []}},
        {"schedule": {"min_lead_minutes": True}},
        {"schedule": {"min_lead_minutes": 3, "avoid_minute_marks": [],
                      "windows": [["00:00", "23:59"]],
                      "window_entries": [{"id": "allday", "start": "00:00",
                                          "end": "23:59", "enabled": 0}]}},
        {"schedule": {"window_entries": ["allday"]}},
    ]
    for payload in cases:
        path = tmp_path / "settings.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        sch = settings.load_settings(path)["schedule"]
        assert type(sch["min_lead_minutes"]) is int
        assert sch["min_lead_minutes"] == 3
        assert sch["windows"] == [["00:00", "23:59"]]
        assert sch["window_entries"][0]["enabled"] is True
        # scheduling must still work (empty windows would raise here)
        assert schedule.next_slot(settings={"schedule": sch}) is not None


def test_explicit_window_start_cannot_beat_the_lead():
    now = datetime(2026, 11, 9, 17, 0, 59, tzinfo=TZ)
    # one-minute window starting at now has no legal slot at all
    assert schedule.pick_slots(1, now=now, window_start=now,
                               window_end=now + timedelta(minutes=1),
                               seed=139) == []
    wide = schedule.pick_slots(2, now=now, window_start=now,
                               window_end=now + timedelta(hours=3), seed=139)
    assert wide
    for slot in wide:
        assert slot >= now + timedelta(minutes=3)


def test_non_finite_lead_values_do_not_break_migration(tmp_path):
    raw = ['1e309', 'Infinity', 1e309, float('inf'), float('-inf')]
    path = tmp_path / "settings.json"
    for value in raw:
        path.write_text(json.dumps({"schedule": {"min_lead_minutes": value}}),
                        encoding="utf-8")
        sch = settings.load_settings(path)["schedule"]
        assert type(sch["min_lead_minutes"]) is int
        assert sch["min_lead_minutes"] == 3
        assert schedule.next_slot(settings={"schedule": sch}) is not None
