"""泵组滚动寻优、缺电耗降级、顺序切泵与控制台口径。"""

from __future__ import annotations

import calendar

from heatnet.config import default_config
from heatnet.console.api import Api
from heatnet.pump.curve import PumpCurve
from heatnet.pump.group import PumpGroup
from heatnet.pump.hydraulics import (
    ROLLING_PERIOD_S,
    NetworkCondition,
    PowerReading,
    PumpCharacteristics,
    optimize_pump_schedule,
)
from heatnet.pump.pump import Pump


def _characteristics() -> list[PumpCharacteristics]:
    return [
        PumpCharacteristics("a", 2400.0, 28.0, 44.0, 320.0),
        PumpCharacteristics("b", 2400.0, 28.0, 42.0, 285.0),
    ]


def _readings(now: float, a: float = 260.0, b: float = 230.0) -> dict[str, PowerReading]:
    return {
        "a": PowerReading("a", a, now),
        "b": PowerReading("b", b, now),
    }


def test_peak_period_prefers_flow_margin_and_offpeak_prefers_power():
    peak_ts = calendar.timegm((2026, 1, 2, 8, 0, 0, 0, 0, 0))
    off_ts = calendar.timegm((2026, 1, 2, 23, 0, 0, 0, 0, 0))
    network = NetworkCondition(1200.0, 0.000002, 20.0)
    pumps = _characteristics()

    peak = optimize_pump_schedule(pumps, network, _readings(peak_ts), now=peak_ts)
    off = optimize_pump_schedule(pumps, network, _readings(off_ts), now=off_ts)

    assert peak.policy == "flow_first"
    assert peak.mode == "variable_speed"
    assert any(item.speed_pct > off.speed_pct for item in peak.candidates)
    assert off.policy == "energy_first"
    assert off.selected_pump == "b"
    assert off.power_kw < off.fixed_speed.power_kw
    assert off.saved_energy_kwh > 0.0
    assert off.optimization == "periodic_rolling"
    assert off.period_s == ROLLING_PERIOD_S


def test_missing_power_is_partial_failure_and_falls_back_to_fixed_speed():
    now = 1_000.0
    schedule = optimize_pump_schedule(
        _characteristics(), NetworkCondition(1200.0, 0.000002), {}, now=now
    )

    assert schedule.ok is True
    assert schedule.degraded is True
    assert schedule.mode == "fixed_speed"
    assert schedule.speed_pct == 100.0
    assert schedule.saved_energy_kwh == 0.0
    assert any(
        item.startswith(("power_reading_missing", "power_reading_stale"))
        for item in schedule.partial_failures
    )
    assert schedule.reasons == ("fallback_fixed_speed:power_unavailable",)


def test_stale_power_is_treated_like_missing_power():
    now = 1_000.0
    schedule = optimize_pump_schedule(
        _characteristics(),
        NetworkCondition(1200.0, 0.000002),
        _readings(now - 121.0),
        now=now,
    )

    assert schedule.mode == "fixed_speed"
    assert schedule.degraded is True
    assert any(item.startswith("power_reading_stale") for item in schedule.partial_failures)


def test_rotation_switches_only_after_fixed_dwell_and_balances_runtime():
    now = 10_000.0
    pumps = [
        PumpCharacteristics("a", 2400.0, 28.0, 42.0, 300.0),
        PumpCharacteristics("b", 2400.0, 28.0, 42.0, 300.0),
    ]
    network = NetworkCondition(1200.0, 0.000002)
    early = optimize_pump_schedule(
        pumps,
        network,
        _readings(now, 153.6, 153.6),
        now=now,
        runtime_s={"a": 100.0, "b": 0.0},
        current_pump="a",
        current_speed_pct=80.0,
        last_switch_ts=0.0,
    )
    due = optimize_pump_schedule(
        pumps,
        network,
        _readings(now + 8 * 3600, 153.6, 153.6),
        now=now + 8 * 3600,
        runtime_s={"a": 8 * 3600.0, "b": 0.0},
        current_pump="a",
        current_speed_pct=80.0,
        last_switch_ts=0.0,
    )

    assert early.selected_pump == "a"
    assert early.switch_deferred is True
    assert due.selected_pump == "b"
    assert due.switched is True
    assert due.rotation is True


def test_pump_group_never_runs_two_pumps_and_switches_in_order():
    config = default_config()
    pumps = [
        Pump("a", config.pump, PumpCurve(20.0, 100.0, 600.0, 2400.0)),
        Pump("b", config.pump, PumpCurve(20.0, 100.0, 600.0, 2400.0)),
    ]
    group = PumpGroup(pumps)
    pumps[0].start(100.0)
    group.state.running_pump = "a"
    first = optimize_pump_schedule(
        _characteristics(),
        NetworkCondition(1200.0, 0.000002),
        _readings(1000.0, 400.0, 230.0),
        now=1000.0,
        current_pump="a",
        current_speed_pct=100.0,
        last_switch_ts=-8 * 3600.0,
    )
    report = group.apply(first, 1000.0)

    assert first.selected_pump == "b"
    assert report["switched"] is True
    assert [step["action"] for step in report["steps"]][:2] == ["stop_outgoing", "start_standby"]
    assert group.running_pump() == "b"
    assert pumps[0].idle() is True
    assert pumps[1].running() is True
    assert len([pump for pump in pumps if pump.running()]) == 1


def test_platform_schedule_is_shown_and_audited_with_same_payload(platform):
    power = platform.report_pump_power(
        {"secondary.circulating-a": 260.0, "secondary.circulating-b": 230.0}
    )
    assert power["ok"] is True
    report = platform.schedule_pump_group(1200.0, 0.000002)

    assert report["optimization"] == "periodic_rolling"
    assert report["selected_pump"] == "secondary.circulating-b"
    assert platform.status()["pump_group"]["latest_schedule"] == report
    entries = platform.audit.entries(limit=20)
    audit_report = next(
        item["payload"] for item in entries if item["action"] == "pump_group_scheduled"
    )
    assert audit_report["selected_pump"] == report["selected_pump"]
    assert audit_report["candidates"] == report["candidates"]


def test_platform_selects_pump_from_current_resistance(platform):
    platform.report_pump_power(
        {"secondary.circulating-a": 320.0, "secondary.circulating-b": 285.0}
    )
    low = platform.schedule_pump_group(1200.0, 0.000002)
    high = platform.schedule_pump_group(2200.0, 0.000004)

    assert low["selected_pump"] == "secondary.circulating-b"
    assert high["selected_pump"] == "secondary.circulating-a"
    assert high["reasons"][0] == "switch:flow_capability"
    assert "flow_shortage:all_pumps_limited" in high["reasons"]


def test_platform_missing_power_degrades_and_console_exposes_group(platform):
    api = Api(platform)
    status, report = api.handle(
        "POST", "/api/pump-group", {}, {"required_flow_t_h": 1200.0, "resistance": 0.000002}
    )
    assert status == 200
    assert report["mode"] == "fixed_speed"
    assert report["degraded"] is True
    status, shown = api.handle("GET", "/api/pump-group", {}, {})
    assert status == 200
    assert shown["optimization"] == "periodic_rolling"
    assert shown["latest_schedule"]["partial_failures"] == report["partial_failures"]


def test_power_endpoint_requires_both_pumps(platform):
    try:
        platform.report_pump_power({"secondary.circulating-a": 260.0})
    except ValueError as error:
        assert "missing" in str(error)
    else:  # pragma: no cover
        raise AssertionError("missing pump reading must be rejected")


def test_platform_rolls_schedule_forward_each_fixed_period(platform):
    platform.report_pump_power(
        {"secondary.circulating-a": 260.0, "secondary.circulating-b": 230.0}
    )
    first = platform.schedule_pump_group(1200.0, 0.000002)
    platform.clock.advance(ROLLING_PERIOD_S - 2)
    platform.cycle(1.0)
    assert platform.pump_group_report()["latest_schedule"]["source"] == "console"

    platform.clock.advance(2.0)
    platform.cycle(1.0)
    second = platform.pump_group_report()["latest_schedule"]
    assert second["source"] == "rolling-cycle"
