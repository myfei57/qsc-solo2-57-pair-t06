"""循环泵组负荷寻优、轮换、切泵与部分失败回退。"""

from __future__ import annotations

from heatnet.app import build_platform
from heatnet.config import default_config
from heatnet.pump.schedule import PumpSpec
from heatnet.pump.scheduler import build_schedule, policy_for_hour
from heatnet.console.api import Api


def _power(platform, a: float = 100.0, b: float = 140.0) -> None:
    platform.record_pump_power({"pump-a": a, "pump-b": b})


def test_low_load_chooses_smaller_pump_and_reports_fixed_speed_comparison(platform):
    _power(platform)
    report = platform.schedule_pumps(demand_t_h=1000.0, hour=12)
    schedule = report["schedule"]

    assert schedule["mode"] == "optimized"
    assert schedule["selected_pump"] == "pump-a"
    assert schedule["speed_pct"] < 100.0
    assert schedule["flow_met"] is True
    assert schedule["sequence"] == ["start:pump-a"]
    assert schedule["fixed_baseline"]["pump"] == "pump-a"
    assert schedule["fixed_baseline"]["speed_pct"] == 100.0
    assert schedule["comparison"]["power_saving_kw"] > 0.0
    assert platform.pump_group.active_name() == "pump-a"


def test_missing_power_is_partial_failure_and_falls_back_to_fixed_speed(platform):
    report = platform.schedule_pumps(demand_t_h=1000.0, hour=12)
    schedule = report["schedule"]

    assert schedule["mode"] == "fixed_speed"
    assert schedule["partial_failure"] is True
    assert schedule["power_available"] is False
    assert schedule["fallback_reason"] == "power_reading_unavailable"
    assert schedule["selected_pump"] == "pump-a"
    assert schedule["speed_pct"] == 100.0
    assert schedule["comparison"]["power_saving_kw"] == 0.0
    assert platform.pump_group.active_name() == "pump-a"


def test_stale_power_reading_also_falls_back_to_fixed_speed(platform):
    platform.record_pump_power({"pump-a": 100.0, "pump-b": 140.0}, ts=0.0)
    schedule = platform.schedule_pumps(demand_t_h=1000.0, hour=12)["schedule"]

    assert schedule["partial_failure"] is True
    assert schedule["mode"] == "fixed_speed"
    assert schedule["fallback_reason"] == "power_reading_unavailable"


def test_high_load_flow_priority_chooses_pump_that_can_meet_flow(platform):
    _power(platform)
    report = platform.schedule_pumps(demand_t_h=2500.0, hour=12)
    schedule = report["schedule"]

    assert schedule["policy"] == "flow_priority"
    assert schedule["selected_pump"] == "pump-b"
    assert schedule["flow_met"] is True
    assert schedule["sequence"] == ["start:pump-b"]


def test_night_policy_prefers_energy_when_neither_pump_can_meet_demand():
    config = default_config()
    specs = [
        PumpSpec("a", 2400.0, 100.0, 132.0),
        PumpSpec("b", 3000.0, 120.0, 300.0),
    ]
    schedule = build_schedule(
        specs,
        {},
        4000.0,
        config.pump_group.network_resistance,
        {"a": 100.0, "b": 140.0},
        0.0,
        23,
        config.pump_group,
        min_speed_pct=20.0,
    )

    assert schedule.policy == "energy_priority"
    assert schedule.flow_met is False
    assert schedule.selected_pump == "a"
    assert schedule.estimated_power_kw < schedule.fixed_power_kw


def test_switch_stops_and_confirms_old_pump_before_new_pump(platform):
    _power(platform)
    low = platform.schedule_pumps(demand_t_h=1000.0, hour=12)
    assert low["schedule"]["sequence"] == ["start:pump-a"]

    high = platform.schedule_pumps(demand_t_h=2500.0, hour=12)
    assert high["schedule"]["sequence"] == [
        "stop:pump-a",
        "confirm_idle:pump-a",
        "start:pump-b",
    ]
    assert platform.pump_group.get("pump-a").idle() is True
    assert platform.pump_group.get("pump-b").running() is True
    assert [pump.running() for pump in platform.pump_group._pumps.values()].count(True) == 1


def test_runtime_difference_drives_rotation_on_otherwise_equal_pumps():
    config = default_config()
    specs = [
        PumpSpec("a", 2400.0, 100.0, 132.0),
        PumpSpec("b", 2400.0, 100.0, 132.0),
    ]
    selected = build_schedule(
        specs,
        {"a": config.pump_group.rotation_balance_s + 1.0, "b": 0.0},
        1000.0,
        config.pump_group.network_resistance,
        {"a": 50.0, "b": 50.0},
        0.0,
        12,
        config.pump_group,
        min_speed_pct=20.0,
    )
    assert selected.selected_pump == "b"


def test_runtime_gap_rotates_even_when_small_pump_is_more_efficient():
    config = default_config()
    specs = [
        PumpSpec("a", 2400.0, 100.0, 132.0),
        PumpSpec("b", 3000.0, 120.0, 300.0),
    ]
    selected = build_schedule(
        specs,
        {"a": config.pump_group.rotation_balance_s + 1.0, "b": 0.0},
        1000.0,
        config.pump_group.network_resistance,
        {"a": 50.0, "b": 50.0},
        0.0,
        12,
        config.pump_group,
        min_speed_pct=20.0,
    )
    assert selected.selected_pump == "b"


def test_schedule_is_the_single_source_for_console_status_and_audit(platform):
    _power(platform)
    applied = platform.schedule_pumps(demand_t_h=1000.0, hour=12)["schedule"]
    status = platform.status()["pump_group"]["last_schedule"]

    assert status == applied
    audit = platform.audit.entries(limit=1)[0]
    assert audit["action"] == "pump_group_scheduled"
    assert audit["payload"]["schedule"] == applied


def test_pump_group_runtime_and_schedule_survive_restart(platform, clock):
    _power(platform)
    platform.schedule_pumps(demand_t_h=1000.0, hour=12)
    platform.cycle(dt=120.0)

    restarted = build_platform(
        default_config(), clock=clock, storage_root=platform.store.root
    )
    assert restarted.pump_group.runtime_s("pump-a") == 120.0
    assert restarted.status()["pump_group"]["last_schedule"]["selected_pump"] == "pump-a"


def test_strategy_is_fixed_to_rolling_not_hourly_batch(platform):
    report = platform.pump_group_report()
    assert report["strategy"] == "rolling"
    assert report["rolling_period_s"] == 300.0
    assert report["batch_mode"] == "disabled"
    assert policy_for_hour(default_config().pump_group, 23) == "energy_priority"
    assert policy_for_hour(default_config().pump_group, 12) == "flow_priority"


def test_rolling_cycle_updates_after_period_but_not_before(platform):
    _power(platform)
    platform.start_pump_group(demand_t_h=1000.0, hour=12)
    first_generated = platform.pump_group.last_schedule().generated_at

    platform.cycle(dt=299.0)
    assert platform.pump_group.last_schedule().generated_at == first_generated

    platform.cycle(dt=1.0)
    assert platform.pump_group.last_schedule().generated_at > first_generated


def test_pump_group_console_api(platform):
    api = Api(platform)
    status, power = api.handle(
        "POST",
        "/api/pump-group/power",
        {},
        {"readings_kw": {"pump-a": 100.0, "pump-b": 140.0}},
    )
    assert status == 200
    assert power["readings_kw"]["pump-a"] == 100.0

    status, scheduled = api.handle(
        "POST", "/api/pump-group/schedule", {}, {"demand_t_h": 1000.0, "hour": 12}
    )
    assert status == 200
    assert scheduled["schedule"]["selected_pump"] == "pump-a"

    status, report = api.handle("GET", "/api/pump-group", {}, {})
    assert status == 200
    assert report["last_schedule"]["selected_pump"] == "pump-a"
