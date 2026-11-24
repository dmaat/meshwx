"""Tests for meshwx.scheduler: next_run modes, quiet hours, DST days, BroadcastScheduler."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List

import pytest
from zoneinfo import ZoneInfo

from meshwx import scheduler as sched
from meshwx.scheduler import BroadcastScheduler, ScheduleSpec, in_quiet_hours, next_run

from conftest import make_config

NY = ZoneInfo("America/New_York")  # 2026-03-08 02:00 EST -> 03:00 EDT; 2026-11-01 02:00 EDT -> 01:00 EST
UTC = timezone.utc


def ny(*fields: int, fold: int = 0) -> datetime:
    return datetime(*fields, tzinfo=NY, fold=fold)


def iso(dt: datetime) -> str:
    """Local time with its UTC offset - tells the two 01:30s of a fall-back night apart."""
    return dt.isoformat()


def chain(spec: ScheduleSpec, start: datetime, count: int) -> List[str]:
    out, t = [], start
    for _ in range(count):
        t = next_run(spec, t)
        out.append(iso(t))
    return out


# ============================================================================ ScheduleSpec / quiet hours


def test_from_config_copies_broadcast_settings():
    cfg = make_config({"broadcast": {"times": ["18:00", "7:00"], "quiet_start": "22:00", "quiet_end": "6:00",
                                     "align_to_clock": False}})
    spec = ScheduleSpec.from_config(cfg.broadcast, 60)
    assert spec == ScheduleSpec(60, align_to_clock=False, times=["07:00", "18:00"],
                                quiet_start="22:00", quiet_end="06:00")
    spec.times.append("12:00")
    assert cfg.broadcast.times == ["07:00", "18:00"]  # the spec owns its list


@pytest.mark.parametrize(
    "start, end, hhmm, quiet",
    [
        ("22:00", "06:00", (21, 59), False),
        ("22:00", "06:00", (22, 0), True),  # start inclusive
        ("22:00", "06:00", (0, 0), True),
        ("22:00", "06:00", (5, 59), True),
        ("22:00", "06:00", (6, 0), False),  # end exclusive
        ("01:00", "05:00", (0, 59), False),
        ("01:00", "05:00", (1, 0), True),
        ("01:00", "05:00", (5, 0), False),
        ("06:00", "06:00", (6, 0), False),  # equal ends: no quiet hours
        ("", "", (3, 0), False),
    ],
)
def test_in_quiet_hours(start, end, hhmm, quiet):
    spec = ScheduleSpec(60, quiet_start=start, quiet_end=end)
    assert in_quiet_hours(spec, ny(2026, 6, 1, *hhmm)) is quiet


# ============================================================================ next_run: modes


def test_fixed_times_today_then_tomorrow():
    spec = ScheduleSpec(180, times=["07:00", "18:00"])  # times replace the interval
    assert iso(next_run(spec, ny(2026, 6, 1, 5, 0))) == "2026-06-01T07:00:00-04:00"
    assert iso(next_run(spec, ny(2026, 6, 1, 7, 0))) == "2026-06-01T18:00:00-04:00"  # strictly after
    assert iso(next_run(spec, ny(2026, 6, 1, 18, 0, 1))) == "2026-06-02T07:00:00-04:00"
    assert iso(next_run(ScheduleSpec(0, times=["12:00"]), ny(2026, 6, 1, 13, 0))) == "2026-06-02T12:00:00-04:00"


def test_interval_zero_means_off():
    assert next_run(ScheduleSpec(0), ny(2026, 6, 1, 12, 0)) is None
    assert next_run(ScheduleSpec(0, align_to_clock=False), ny(2026, 6, 1, 12, 0)) is None


@pytest.mark.parametrize(
    "interval, after, expected",
    [
        (180, (4, 10), "2026-06-01T06:00:00-04:00"),
        (180, (6, 0), "2026-06-01T09:00:00-04:00"),  # exactly on a slot -> the next one
        (180, (22, 30), "2026-06-02T00:00:00-04:00"),
        (45, (0, 50), "2026-06-01T01:30:00-04:00"),
        (15, (12, 14, 59), "2026-06-01T12:15:00-04:00"),
        (1440, (0, 0), "2026-06-02T00:00:00-04:00"),
    ],
)
def test_clock_aligned_interval(interval, after, expected):
    assert iso(next_run(ScheduleSpec(interval), ny(2026, 6, 1, *after))) == expected


def test_interval_not_dividing_a_day_is_anchored_even_when_aligned():
    after = ny(2026, 6, 1, 10, 7, 30)
    assert next_run(ScheduleSpec(100), after) == after + timedelta(minutes=100)


def test_anchored_interval_grid():
    spec = ScheduleSpec(60, align_to_clock=False)
    anchor = ny(2026, 6, 1, 10, 17)
    assert iso(next_run(spec, anchor, anchor=anchor)) == "2026-06-01T11:17:00-04:00"
    assert iso(next_run(spec, ny(2026, 6, 1, 12, 59), anchor=anchor)) == "2026-06-01T13:17:00-04:00"
    assert iso(next_run(spec, ny(2026, 6, 1, 13, 17), anchor=anchor)) == "2026-06-01T14:17:00-04:00"
    assert iso(next_run(spec, ny(2026, 6, 1, 8, 0), anchor=anchor)) == "2026-06-01T08:17:00-04:00"  # future anchor
    after = ny(2026, 6, 1, 10, 0, 12, 345)
    assert next_run(spec, after) == after + timedelta(hours=1)  # default anchor = after


def test_quiet_hours_skip_slots_in_every_mode():
    quiet = {"quiet_start": "22:00", "quiet_end": "06:00"}
    assert iso(next_run(ScheduleSpec(180, **quiet), ny(2026, 6, 1, 21, 0))) == "2026-06-02T06:00:00-04:00"
    times = ScheduleSpec(0, times=["05:00", "12:00", "23:00"], **quiet)
    assert iso(next_run(times, ny(2026, 6, 1, 13, 0))) == "2026-06-02T12:00:00-04:00"
    anchored = ScheduleSpec(100, quiet_start="01:00", quiet_end="05:00")  # grid 00:00 01:40 03:20 05:00
    assert iso(next_run(anchored, ny(2026, 6, 1), anchor=ny(2026, 6, 1))) == "2026-06-01T05:00:00-04:00"


def test_no_slot_outside_quiet_hours_gives_none():
    quiet = {"quiet_start": "22:00", "quiet_end": "06:00"}
    assert next_run(ScheduleSpec(1440, **quiet), ny(2026, 6, 1, 12, 0)) is None  # midnight is always quiet
    assert next_run(ScheduleSpec(0, times=["23:00"], **quiet), ny(2026, 6, 1, 12, 0)) is None
    weekly = ScheduleSpec(7 * 1440, align_to_clock=False, **quiet)
    assert next_run(weekly, ny(2026, 6, 1, 23, 0)) is None  # 23:00 every week; search stops after ~8 days


def test_result_is_in_the_timezone_of_after():
    after = datetime(2026, 6, 1, 13, 10, tzinfo=UTC)
    in_utc = next_run(ScheduleSpec(180), after)
    assert in_utc.tzinfo is UTC and in_utc == datetime(2026, 6, 1, 15, 0, tzinfo=UTC)  # UTC wall clock
    local = next_run(ScheduleSpec(180), after.astimezone(NY))
    assert local.tzinfo is NY and iso(local) == "2026-06-01T12:00:00-04:00"


def test_naive_datetimes_rejected():
    with pytest.raises(ValueError):
        next_run(ScheduleSpec(60), datetime(2026, 6, 1, 12, 0))
    with pytest.raises(ValueError):
        next_run(ScheduleSpec(100), ny(2026, 6, 1, 12, 0), anchor=datetime(2026, 6, 1, 12, 0))


# ============================================================================ next_run: DST


@pytest.mark.parametrize(
    "interval, expected",
    [
        (60, ["2026-03-08T00:00:00-05:00", "2026-03-08T01:00:00-05:00",
              "2026-03-08T03:00:00-04:00", "2026-03-08T04:00:00-04:00"]),
        (180, ["2026-03-08T00:00:00-05:00", "2026-03-08T03:00:00-04:00", "2026-03-08T06:00:00-04:00"]),
        (120, ["2026-03-08T00:00:00-05:00", "2026-03-08T03:00:00-04:00",  # 02:00 does not exist
               "2026-03-08T04:00:00-04:00"]),
    ],
)
def test_spring_forward_aligned(interval, expected):
    assert chain(ScheduleSpec(interval), ny(2026, 3, 7, 23, 30), len(expected)) == expected


@pytest.mark.parametrize(
    "interval, expected",
    [
        (60, ["2026-11-01T00:00:00-04:00", "2026-11-01T01:00:00-04:00",  # repeated 01:00 EST is skipped
              "2026-11-01T02:00:00-05:00", "2026-11-01T03:00:00-05:00"]),
        (180, ["2026-11-01T00:00:00-04:00", "2026-11-01T03:00:00-05:00", "2026-11-01T06:00:00-05:00"]),
    ],
)
def test_fall_back_aligned(interval, expected):
    assert chain(ScheduleSpec(interval), ny(2026, 10, 31, 23, 30), len(expected)) == expected


def test_fall_back_from_inside_the_repeated_hour():
    for after in (ny(2026, 11, 1, 1, 30), ny(2026, 11, 1, 1, 30, fold=1)):
        assert iso(next_run(ScheduleSpec(60), after)) == "2026-11-01T02:00:00-05:00"


def test_fixed_time_in_spring_forward_gap_moves_forward():
    assert chain(ScheduleSpec(0, times=["02:30"]), ny(2026, 3, 7, 12, 0), 2) == [
        "2026-03-08T03:30:00-04:00", "2026-03-09T02:30:00-04:00"]
    # the shifted 02:30 still runs, after the real 03:15
    assert chain(ScheduleSpec(0, times=["02:30", "03:15"]), ny(2026, 3, 8, 0, 0), 3) == [
        "2026-03-08T03:15:00-04:00", "2026-03-08T03:30:00-04:00", "2026-03-09T02:30:00-04:00"]


def test_fixed_time_in_fall_back_overlap_runs_once():
    spec = ScheduleSpec(0, times=["01:30"])
    assert chain(spec, ny(2026, 10, 31, 12, 0), 2) == ["2026-11-01T01:30:00-04:00", "2026-11-02T01:30:00-05:00"]
    assert iso(next_run(spec, ny(2026, 11, 1, 1, 15, fold=1))) == "2026-11-02T01:30:00-05:00"
    assert chain(ScheduleSpec(0, times=["02:30"]), ny(2026, 10, 31, 12, 0), 2) == [
        "2026-11-01T02:30:00-05:00", "2026-11-02T02:30:00-05:00"]


def test_anchored_interval_counts_real_time_across_dst():
    spec = ScheduleSpec(60, align_to_clock=False)
    assert chain(spec, ny(2026, 11, 1, 0, 30), 3) == [
        "2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00", "2026-11-01T02:30:00-05:00"]
    assert chain(spec, ny(2026, 3, 8, 1, 30), 2) == ["2026-03-08T03:30:00-04:00", "2026-03-08T04:30:00-04:00"]


@pytest.mark.parametrize(
    "spec",
    [
        ScheduleSpec(15), ScheduleSpec(60), ScheduleSpec(180), ScheduleSpec(90, align_to_clock=False),
        ScheduleSpec(0, times=["01:30", "02:30"]), ScheduleSpec(30, quiet_start="01:00", quiet_end="02:00"),
    ],
    ids=["15", "60", "180", "anchored90", "times", "quiet"],
)
@pytest.mark.parametrize("day", [(2026, 3, 8), (2026, 11, 1)], ids=["spring", "fall"])
def test_dst_day_results_always_strictly_later(spec, day):
    start = ny(*day).astimezone(UTC) - timedelta(hours=1)
    for minute in range(0, 6 * 60, 7):  # 23:00 the day before through the transition
        after = (start + timedelta(minutes=minute)).astimezone(NY)  # what datetime.now(NY) returns
        result = next_run(spec, after)
        assert result.tzinfo is NY
        assert timedelta(0) < result.astimezone(UTC) - after.astimezone(UTC) <= timedelta(hours=25)
        assert not in_quiet_hours(spec, result)


# ============================================================================ BroadcastScheduler


class FakeClock:
    """A UTC instant shown in America/New_York, like ``datetime.now(NY)``."""

    def __init__(self, local: datetime) -> None:
        self.utc = local.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.utc.astimezone(NY)

    def set(self, local: datetime) -> None:
        self.utc = local.astimezone(UTC)

    def advance(self, **delta: float) -> None:
        self.utc += timedelta(**delta)


@pytest.fixture
def count_next_run(monkeypatch) -> List[datetime]:
    """Records the ``after`` of every next_run() call made by the scheduler."""
    calls: List[datetime] = []
    real = sched.next_run

    def counting(spec, after, **kw):
        calls.append(after)
        return real(spec, after, **kw)

    monkeypatch.setattr(sched, "next_run", counting)
    return calls


def run_until(scheduler: BroadcastScheduler, clock: FakeClock, end: datetime, step: timedelta) -> List[str]:
    """Poll every ``step`` until ``end``; return the (local) times at which poll() fired."""
    fired = []
    while clock.utc <= end.astimezone(UTC):
        if scheduler.poll():
            fired.append(iso(clock()))
        clock.utc += step
    return fired


def make_scheduler(clock: FakeClock, spec: ScheduleSpec, **kw) -> BroadcastScheduler:
    return BroadcastScheduler(lambda: spec, now_fn=clock, **kw)


def test_waits_for_a_sane_clock():
    clock = FakeClock(datetime(1970, 1, 1, 0, 5, tzinfo=UTC))  # Pi without RTC, before NTP
    s = make_scheduler(clock, ScheduleSpec(60))
    assert s.poll() is False and s.next_run_at is None
    clock.set(ny(2026, 6, 1, 10, 20))
    assert iso(s.next_run_at) == "2026-06-01T11:00:00-04:00"


def test_fires_exactly_once_per_slot():
    clock = FakeClock(ny(2026, 6, 1, 10, 20, 1))
    s = make_scheduler(clock, ScheduleSpec(60))
    fired = run_until(s, clock, ny(2026, 6, 1, 13, 0, 30), timedelta(seconds=2))
    assert fired == ["2026-06-01T11:00:01-04:00", "2026-06-01T12:00:01-04:00", "2026-06-01T13:00:01-04:00"]
    assert s.poll() is False


def test_late_slot_fires_within_grace_and_is_skipped_beyond(caplog):
    clock = FakeClock(ny(2026, 6, 1, 10, 20))
    s = make_scheduler(clock, ScheduleSpec(60), grace_minutes=15)
    assert s.poll() is False
    clock.set(ny(2026, 6, 1, 11, 14))  # 14 min late (busy system)
    assert s.poll() is True
    clock.set(ny(2026, 6, 1, 12, 20))  # clock jumped forward past the 12:00 slot
    with caplog.at_level(logging.WARNING, logger="meshwx.scheduler"):
        assert s.poll() is False
    assert "missed broadcast slot" in caplog.text and "clock jump?" in caplog.text
    assert iso(s.next_run_at) == "2026-06-01T13:00:00-04:00"
    assert s.poll() is False


def test_anchored_mode_does_not_drift():
    clock = FakeClock(ny(2026, 6, 1, 10, 0))
    s = make_scheduler(clock, ScheduleSpec(100, align_to_clock=False))
    assert iso(s.next_run_at) == "2026-06-01T11:40:00-04:00"  # counted from start-up
    clock.set(ny(2026, 6, 1, 11, 41, 30))
    assert s.poll() is True
    assert iso(s.next_run_at) == "2026-06-01T13:20:00-04:00"  # from the slot, not from 11:41:30
    clock.set(ny(2026, 6, 1, 15, 5))  # missed 13:20 and 15:00
    assert s.poll() is False
    assert iso(s.next_run_at) == "2026-06-01T16:40:00-04:00"  # still on the original grid


def test_backward_clock_jump_recomputes():
    clock = FakeClock(ny(2026, 6, 1, 10, 20))
    s = make_scheduler(clock, ScheduleSpec(60))
    assert iso(s.next_run_at) == "2026-06-01T11:00:00-04:00"
    clock.set(ny(2026, 6, 1, 2, 10))  # NTP corrected a clock that was 8 h fast
    assert s.poll() is False
    assert iso(s.next_run_at) == "2026-06-01T03:00:00-04:00"
    clock.set(ny(2026, 6, 1, 3, 0))
    assert s.poll() is True


def test_small_backward_clock_step_keeps_the_pending_slot(caplog):
    clock = FakeClock(ny(2026, 6, 1, 10, 0))
    s = make_scheduler(clock, ScheduleSpec(100, align_to_clock=False))
    assert iso(s.next_run_at) == "2026-06-01T11:40:00-04:00"
    clock.set(ny(2026, 6, 1, 10, 30))
    assert s.poll() is False
    clock.set(ny(2026, 6, 1, 10, 27))  # NTP stepped the clock back 3 min: the slot just comes 3 min later
    with caplog.at_level(logging.WARNING, logger="meshwx.scheduler"):
        assert s.poll() is False
    assert iso(s.next_run_at) == "2026-06-01T11:40:00-04:00" and caplog.text == ""
    clock.set(ny(2026, 6, 1, 8, 0))  # 2.5 h back: rescheduled from now
    with caplog.at_level(logging.WARNING, logger="meshwx.scheduler"):
        assert s.poll() is False
    assert iso(s.next_run_at) == "2026-06-01T09:40:00-04:00" and "clock set back" in caplog.text


def test_long_wait_after_skipped_quiet_slots_is_not_a_clock_set_back(caplog, count_next_run):
    # TIME-5: every 25 h from 20:59 with quiet hours 22:00-03:00; 06-03 22:59 ... 06-08 02:59 are quiet.
    clock = FakeClock(ny(2026, 6, 1, 20, 59))
    s = make_scheduler(clock, ScheduleSpec(1500, quiet_start="22:00", quiet_end="03:00"))
    with caplog.at_level(logging.WARNING, logger="meshwx.scheduler"):
        fired = run_until(s, clock, ny(2026, 6, 10), timedelta(minutes=10))
    assert fired == ["2026-06-02T21:59:00-04:00", "2026-06-09T03:59:00-04:00"]
    assert caplog.text == "" and len(count_next_run) == 3  # start-up + one per slot


def test_no_slot_found_is_looked_for_again_every_hour(caplog, count_next_run):
    # TIME-5: every 2 days from 23:30 with quiet hours 22:00-06:00 - every slot of that cadence is quiet.
    clock = FakeClock(ny(2026, 6, 1, 23, 30))
    s = make_scheduler(clock, ScheduleSpec(2880, quiet_start="22:00", quiet_end="06:00"))
    with caplog.at_level(logging.WARNING, logger="meshwx.scheduler"):
        fired = run_until(s, clock, ny(2026, 6, 7), timedelta(minutes=1))
    # counted again from 06:30, the first hourly look outside the quiet hours
    assert fired == ["2026-06-04T06:30:00-04:00", "2026-06-06T06:30:00-04:00"]
    assert [r.getMessage() for r in caplog.records] == [
        "no scheduled broadcast slot outside quiet hours within 8 days; looking again hourly"]
    assert len(count_next_run) == 1 + 7 + 2  # start-up, hourly looks 00:30-06:30, one per slot


def test_weekly_slot_pushed_into_quiet_hours_by_dst_moves_out_of_them():
    # TIME-5: weekly from Saturday 21:29 EST; from March 8 that slot is 22:29 EDT, inside the quiet hours.
    clock = FakeClock(ny(2026, 2, 28, 21, 29))
    s = make_scheduler(clock, ScheduleSpec(10080, quiet_start="22:00", quiet_end="06:00"))
    fired = run_until(s, clock, ny(2026, 3, 31), timedelta(minutes=10))
    assert fired == ["2026-03-07T21:29:00-05:00", "2026-03-15T06:29:00-04:00", "2026-03-22T06:29:00-04:00",
                     "2026-03-29T06:29:00-04:00"]


def test_long_quiet_wait_is_not_mistaken_for_a_clock_jump(count_next_run):
    clock = FakeClock(ny(2026, 6, 1, 21, 30))
    s = make_scheduler(clock, ScheduleSpec(60, quiet_start="22:00", quiet_end="06:00"))
    fired = run_until(s, clock, ny(2026, 6, 2, 6, 0), timedelta(minutes=1))
    assert fired == ["2026-06-02T06:00:00-04:00"]
    assert len(count_next_run) == 2  # start-up + after the 06:00 slot; no recomputing all night


def test_times_mode_day_long_wait_is_plausible(count_next_run):
    clock = FakeClock(ny(2026, 6, 1, 6, 59))
    s = make_scheduler(clock, ScheduleSpec(0, times=["07:00"]))
    fired = run_until(s, clock, ny(2026, 6, 2, 7, 5), timedelta(minutes=5))
    assert fired == ["2026-06-01T07:04:00-04:00", "2026-06-02T07:04:00-04:00"]
    assert len(count_next_run) == 3  # start-up + one per slot: the ~24 h wait is not "implausible"


def test_spec_change_applies_without_reschedule():
    clock = FakeClock(ny(2026, 6, 1, 10, 20))
    spec = ScheduleSpec(180)
    s = make_scheduler(clock, spec)
    assert iso(s.next_run_at) == "2026-06-01T12:00:00-04:00"
    spec.interval_minutes = 60  # mutated in place, e.g. by a settings change
    assert iso(s.next_run_at) == "2026-06-01T11:00:00-04:00"
    spec.interval_minutes = 0
    assert s.next_run_at is None and s.poll() is False


def test_reschedule_restarts_anchored_cadence():
    clock = FakeClock(ny(2026, 6, 1, 10, 0))
    s = make_scheduler(clock, ScheduleSpec(100, align_to_clock=False))
    assert iso(s.next_run_at) == "2026-06-01T11:40:00-04:00"
    clock.set(ny(2026, 6, 1, 10, 30))
    s.reschedule()
    assert iso(s.next_run_at) == "2026-06-01T12:10:00-04:00"


def test_interval_zero_never_fires(caplog):
    clock = FakeClock(ny(2026, 6, 1, 0, 0))
    s = make_scheduler(clock, ScheduleSpec(0))
    with caplog.at_level(logging.INFO, logger="meshwx.scheduler"):
        assert run_until(s, clock, ny(2026, 6, 2, 0, 0), timedelta(minutes=10)) == []
    assert s.next_run_at is None and "off" in caplog.text


@pytest.mark.parametrize(
    "start, end, expected",
    [
        (ny(2026, 10, 31, 23, 50), ny(2026, 11, 1, 3, 10),
         ["2026-11-01T00:00:00-04:00", "2026-11-01T01:00:00-04:00",
          "2026-11-01T02:00:00-05:00", "2026-11-01T03:00:00-05:00"]),
        (ny(2026, 3, 7, 23, 50), ny(2026, 3, 8, 4, 10),
         ["2026-03-08T00:00:00-05:00", "2026-03-08T01:00:00-05:00",
          "2026-03-08T03:00:00-04:00", "2026-03-08T04:00:00-04:00"]),
    ],
    ids=["fall-back", "spring-forward"],
)
def test_dst_night_fires_once_per_slot(start, end, expected, count_next_run, caplog):
    clock = FakeClock(start)
    s = make_scheduler(clock, ScheduleSpec(60))
    with caplog.at_level(logging.WARNING, logger="meshwx.scheduler"):
        assert run_until(s, clock, end, timedelta(minutes=1)) == expected
    assert caplog.text == ""
    assert len(count_next_run) == len(expected) + 1  # no recompute loops in the repeated hour


def test_default_clock_uses_given_timezone():
    s = BroadcastScheduler(lambda: ScheduleSpec(60), tz=NY)
    nxt = s.next_run_at
    assert nxt.tzinfo is NY and nxt > datetime.now(UTC) and (nxt.minute, nxt.second) == (0, 0)


def test_default_clock_looks_up_the_host_zone_once(monkeypatch):
    lookups: List[int] = []

    def host_zone():
        lookups.append(1)
        return NY

    monkeypatch.setattr(sched.timeutil, "local_tz", host_zone)
    s = BroadcastScheduler(lambda: ScheduleSpec(60))
    for _ in range(3):
        s.poll()
    assert s.next_run_at.tzinfo is NY and len(lookups) == 1


def test_default_clock_follows_the_os_offset_when_the_host_zone_has_no_name(monkeypatch):
    stale = timezone(timedelta(hours=13, minutes=59))  # a fixed offset would miss the next DST change
    monkeypatch.setattr(sched.timeutil, "local_tz", lambda: stale)
    nxt = BroadcastScheduler(lambda: ScheduleSpec(60)).next_run_at
    assert nxt.utcoffset() == datetime.now().astimezone().utcoffset() != stale.utcoffset(None)


def test_spec_fn_is_consulted_on_every_poll():
    calls: List[int] = []

    def spec_fn() -> ScheduleSpec:
        calls.append(1)
        return ScheduleSpec(60)

    s = BroadcastScheduler(spec_fn, now_fn=FakeClock(ny(2026, 6, 1, 10, 20)))
    for _ in range(3):
        s.poll()
    assert len(calls) == 3
