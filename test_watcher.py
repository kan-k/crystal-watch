"""Offline tests with a fake API - run: python -m pytest -q  (or python test_watcher.py)."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("REQUEST_GAP_SEC", "0")
os.environ.setdefault("SWEEP_GAP_SEC", "0")
os.environ.setdefault("DAYS_AHEAD", "2")

import watcher as W  # noqa: E402
from crystal_api import parse_slot  # noqa: E402

COURTS = [("1", "North-1", "LOC001", "Crystal Sports"), ("18", "G North-1", "LOC002", "Crystal Sports G")]


def make_rows(booked: set[tuple[str, str]] | None = None, all_free=False):
    rows = []
    for sid, sname, lid, lname in COURTS:
        for h in range(6, 24):
            t = f"{h:02d}:00"
            status = "0" if (all_free or (booked is not None and (sid, t) not in booked)) else "1"
            rows.append({"stadiumtimeId": int(sid) * 100 + h, "stadiumId": int(sid), "timeId": h,
                         "timeStart": f"{t}:00", "timeEnd": f"{(h + 1) % 24:02d}:00:00", "stadiumName": sname,
                         "timeName": t, "stadiumtimePrice": "500.0000", "reservestatus": status,
                         "locName": lname, "locId": lid})
    return rows


ALL_BOOKED = {(sid, f"{h:02d}:00") for sid, *_ in COURTS for h in range(6, 24)}


class FakeAPI:
    def __init__(self):
        self.days: dict[str, list[dict]] = {}
        self.calls = 0

    def get_day(self, date, stadium_id=None, loc_id=None):
        self.calls += 1
        return [parse_slot(date, r) for r in self.days.get(date, [])]


class Capture:
    name = "capture"

    def __init__(self):
        self.msgs = []

    def send(self, text):
        self.msgs.append(text)
        return True


def dates(n):
    now = W.now_tz()
    return [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def build(tmp: Path):
    cfg = W.Config()
    cfg.state_file = tmp / "state.json"
    cfg.request_gap = 0
    cfg.sweep_gap = 0
    cfg.days_ahead = 2
    api, cap = FakeAPI(), Capture()
    return cfg, api, cap


def test_cancellation_triggers_single_alert():
    with tempfile.TemporaryDirectory() as d:
        cfg, api, cap = build(Path(d))
        d0, d1, d2 = dates(3)
        for dd in (d0, d1, d2):
            api.days[dd] = make_rows(ALL_BOOKED)
        w = W.Watcher(cfg, api, [cap])
        w.sweep()
        assert len(cap.msgs) == 1 and "No free slots" in cap.msgs[0], cap.msgs

        # someone cancels G North-1 19:00 on day+1
        api.days[d1] = make_rows(ALL_BOOKED - {("18", "19:00")})
        w.sweep()
        assert len(cap.msgs) == 2, cap.msgs
        msg = cap.msgs[1]
        assert "AVAILABLE" in msg and "Crystal Sports G — G North-1: 19:00-20:00" in msg and W.fmt_date(d1) in msg
        assert "booking.php" in msg

        # still free next sweep -> no repeat alert
        w.sweep()
        assert len(cap.msgs) == 2

        # re-booked, then cancelled again -> alert again
        api.days[d1] = make_rows(ALL_BOOKED)
        w.sweep()
        api.days[d1] = make_rows(ALL_BOOKED - {("18", "19:00")})
        w.sweep()
        assert len(cap.msgs) == 3
        assert api.calls == 15


def test_new_day_opens_is_compressed_into_ranges():
    with tempfile.TemporaryDirectory() as d:
        cfg, api, cap = build(Path(d))
        d0, d1, d2 = dates(3)
        api.days[d0] = make_rows(ALL_BOOKED)
        api.days[d1] = make_rows(ALL_BOOKED)
        w = W.Watcher(cfg, api, [cap])
        w.sweep()                       # d2 returns nothing yet (not open)
        api.days[d2] = make_rows(all_free=True)
        w.sweep()
        assert len(cap.msgs) == 2
        msg = cap.msgs[1]
        assert "North-1: 06:00-00:00" in msg, msg           # 18 hours compressed to one range
        assert msg.count("•") == 2                          # one line per court, not 36 lines


def test_startup_summary_lists_currently_free_and_state_survives_restart():
    with tempfile.TemporaryDirectory() as d:
        cfg, api, cap = build(Path(d))
        d0, d1, d2 = dates(3)
        api.days[d0] = make_rows(ALL_BOOKED)
        api.days[d1] = make_rows(ALL_BOOKED - {("1", "23:00"), ("1", "22:00")})
        api.days[d2] = make_rows(ALL_BOOKED)
        w = W.Watcher(cfg, api, [cap])
        w.sweep()
        assert len(cap.msgs) == 1 and "Watcher started" in cap.msgs[0]
        assert "North-1: 22:00-00:00" in cap.msgs[0]
        assert json.loads(cfg.state_file.read_text())["slots"]

        # restart with the same state file: nothing new -> silent
        cap2 = Capture()
        w2 = W.Watcher(cfg, api, [cap2])
        w2.sweep()
        assert cap2.msgs == []


def test_past_slots_today_are_ignored():
    with tempfile.TemporaryDirectory() as d:
        cfg, api, cap = build(Path(d))
        d0, d1, d2 = dates(3)
        now = W.now_tz()
        for dd in (d0, d1, d2):
            api.days[dd] = make_rows(ALL_BOOKED)
        w = W.Watcher(cfg, api, [cap])
        w.sweep()
        past_hour = f"{max(6, now.hour - 1):02d}:00"
        api.days[d0] = make_rows(ALL_BOOKED - {("1", past_hour)})
        w.sweep()
        if now.hour > 6:
            assert len(cap.msgs) == 1, cap.msgs     # a slot that already started is not an alert


def test_filters():
    with tempfile.TemporaryDirectory() as d:
        cfg, api, cap = build(Path(d))
        cfg.venues = {"loc002"}
        cfg.time_from, cfg.time_to = "18:00", "22:00"
        d0, d1, d2 = dates(3)
        for dd in (d0, d1, d2):
            api.days[dd] = make_rows(ALL_BOOKED)
        w = W.Watcher(cfg, api, [cap])
        w.sweep()
        api.days[d2] = make_rows(ALL_BOOKED - {("1", "19:00"), ("18", "17:00"), ("18", "20:00")})
        w.sweep()
        assert len(cap.msgs) == 2
        assert "G North-1: 20:00-21:00" in cap.msgs[1] and "North-1: 19:00" not in cap.msgs[1] and "17:00" not in cap.msgs[1]


def test_compress_hours():
    assert W.compress_hours(["06:00", "07:00", "08:00", "14:00", "23:00"]) == "06:00-09:00, 14:00-15:00, 23:00-00:00"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main(["-q", __file__]))
