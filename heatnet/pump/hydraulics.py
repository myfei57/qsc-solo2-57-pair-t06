"""泵组水力寻优。

寻优口径固定为“按周期滚动”：每次只使用调用时刻的负荷需求、管网阻力和新鲜电耗，
不做整点批量预测。先用水力特性求每台泵在当前管网上的可达流量与转速，再按时段
策略排序；峰段优先保流量裕量，平谷段在满足流量的前提下取低电耗。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

PEAK_START_HOUR = 6
PEAK_END_HOUR = 22
FLOW_PRIORITY_MARGIN = 1.03
POWER_SWITCH_TOLERANCE = 0.02
ROTATION_DWELL_S = 8.0 * 3600.0
ROLLING_PERIOD_S = 60.0
POWER_FRESH_S = 120.0


@dataclass(frozen=True)
class PumpCharacteristics:
    """一台泵的额定水力与电耗特性。"""

    name: str
    rated_flow_t_h: float
    rated_head_m: float
    shutoff_head_m: float
    rated_power_kw: float
    min_speed_pct: float = 20.0
    max_speed_pct: float = 100.0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("泵名不能为空")
        if self.rated_flow_t_h <= 0:
            raise ValueError("额定流量必须为正")
        if self.rated_head_m <= 0 or self.shutoff_head_m <= 0:
            raise ValueError("扬程必须为正")
        if self.rated_head_m >= self.shutoff_head_m:
            raise ValueError("关断扬程必须高于额定扬程")
        if self.rated_power_kw <= 0:
            raise ValueError("额定功率必须为正")
        if not 0.0 <= self.min_speed_pct < self.max_speed_pct <= 100.0:
            raise ValueError("泵转速边界非法")

    def head_at(self, flow_t_h: float, speed_pct: float) -> float:
        """按二次泵曲线计算给定流量、转速下的扬程。"""

        speed = max(0.0, speed_pct) / 100.0
        q_ratio = float(flow_t_h) / self.rated_flow_t_h
        return speed**2 * (
            self.shutoff_head_m - (self.shutoff_head_m - self.rated_head_m) * q_ratio**2
        )

    def speed_for_head(self, flow_t_h: float, required_head_m: float) -> float:
        """反算提供指定流量与扬程所需转速百分比。"""

        numerator = float(required_head_m) + (
            self.shutoff_head_m - self.rated_head_m
        ) * (float(flow_t_h) / self.rated_flow_t_h) ** 2
        if numerator <= 0:
            return 0.0
        return 100.0 * math.sqrt(numerator / self.shutoff_head_m)

    def power_for_speed(self, speed_pct: float, calibration: float = 1.0) -> float:
        """按相似定律估算轴功率，现场系数只修正模型偏差。"""

        speed = max(0.0, speed_pct) / 100.0
        return max(0.0, self.rated_power_kw * speed**3 * calibration)


@dataclass(frozen=True)
class NetworkCondition:
    """当前管网阻力点：H = static_head_m + resistance * Q²。"""

    required_flow_t_h: float
    resistance: float
    static_head_m: float = 20.0

    def __post_init__(self) -> None:
        if self.required_flow_t_h < 0:
            raise ValueError("需求流量不能为负")
        if self.resistance < 0:
            raise ValueError("管网阻力不能为负")
        if self.static_head_m < 0:
            raise ValueError("静扬程不能为负")

    def head_for(self, flow_t_h: float) -> float:
        return self.static_head_m + self.resistance * float(flow_t_h) ** 2


@dataclass(frozen=True)
class PowerReading:
    """一次电耗采样。"""

    pump: str
    power_kw: float
    ts: float

    def __post_init__(self) -> None:
        if self.power_kw < 0:
            raise ValueError("电耗不能为负")


@dataclass(frozen=True)
class PumpCandidate:
    """单台泵的本轮可行解。"""

    pump: str
    speed_pct: float
    delivered_flow_t_h: float
    required_flow_t_h: float
    head_m: float
    power_kw: float
    feasible: bool
    shortage_t_h: float
    calibration: float

    def as_dict(self) -> dict:
        return {
            "pump": self.pump,
            "speed_pct": round(self.speed_pct, 2),
            "delivered_flow_t_h": round(self.delivered_flow_t_h, 2),
            "required_flow_t_h": round(self.required_flow_t_h, 2),
            "head_m": round(self.head_m, 3),
            "power_kw": round(self.power_kw, 3),
            "feasible": self.feasible,
            "shortage_t_h": round(self.shortage_t_h, 2),
            "calibration": round(self.calibration, 4),
        }


@dataclass(frozen=True)
class FixedSpeedComparison:
    """原定速方式与本轮寻优结果的对比。"""

    pump: str
    speed_pct: float
    delivered_flow_t_h: float
    power_kw: float
    energy_kwh: float
    meets_flow: bool
    shortage_t_h: float

    def as_dict(self) -> dict:
        return {
            "pump": self.pump,
            "speed_pct": round(self.speed_pct, 2),
            "delivered_flow_t_h": round(self.delivered_flow_t_h, 2),
            "power_kw": round(self.power_kw, 3),
            "energy_kwh": round(self.energy_kwh, 4),
            "meets_flow": self.meets_flow,
            "shortage_t_h": round(self.shortage_t_h, 2),
        }


@dataclass(frozen=True)
class PumpSchedule:
    """一轮泵组滚动寻优的唯一结果口径。"""

    selected_pump: str
    requested_pump: str
    speed_pct: float
    delivered_flow_t_h: float
    required_flow_t_h: float
    power_kw: float
    energy_kwh: float
    policy: str
    mode: str
    optimization: str
    period_s: float
    candidates: list[PumpCandidate] = field(default_factory=list)
    fixed_speed: FixedSpeedComparison | None = None
    saved_energy_kwh: float = 0.0
    saving_pct: float = 0.0
    ok: bool = True
    degraded: bool = False
    switched: bool = False
    switch_deferred: bool = False
    rotation: bool = False
    partial_failures: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "selected_pump": self.selected_pump,
            "requested_pump": self.requested_pump,
            "speed_pct": round(self.speed_pct, 2),
            "delivered_flow_t_h": round(self.delivered_flow_t_h, 2),
            "required_flow_t_h": round(self.required_flow_t_h, 2),
            "power_kw": round(self.power_kw, 3),
            "energy_kwh": round(self.energy_kwh, 4),
            "policy": self.policy,
            "mode": self.mode,
            "optimization": self.optimization,
            "period_s": self.period_s,
            "candidates": [item.as_dict() for item in self.candidates],
            "fixed_speed": None if self.fixed_speed is None else self.fixed_speed.as_dict(),
            "saved_energy_kwh": round(self.saved_energy_kwh, 4),
            "saving_pct": round(self.saving_pct, 2),
            "ok": self.ok,
            "degraded": self.degraded,
            "switched": self.switched,
            "switch_deferred": self.switch_deferred,
            "rotation": self.rotation,
            "partial_failures": list(self.partial_failures),
            "reasons": list(self.reasons),
        }


FLOW_SORT_TOLERANCE_T_H = 0.01
POWER_SORT_TOLERANCE_KW = 0.01


def _flow_bucket(flow_t_h: float) -> int:
    return int(round(float(flow_t_h) / FLOW_SORT_TOLERANCE_T_H))


def _power_bucket(power_kw: float) -> int:
    return int(round(float(power_kw) / POWER_SORT_TOLERANCE_KW))


def policy_for_timestamp(ts: float) -> str:
    """按UTC小时返回峰段/平谷段策略。边界定死：06:00-22:00保流量。"""

    hour = time.gmtime(float(ts)).tm_hour
    return "flow_first" if PEAK_START_HOUR <= hour < PEAK_END_HOUR else "energy_first"


def _intersection_flow(pump: PumpCharacteristics, network: NetworkCondition, speed_pct: float) -> float:
    """求泵曲线与管网曲线交点。"""

    speed_ratio = max(0.0, speed_pct) / 100.0
    head_drop = pump.shutoff_head_m - pump.rated_head_m
    denominator = network.resistance + head_drop / pump.rated_flow_t_h**2
    numerator = speed_ratio**2 * pump.shutoff_head_m - network.static_head_m
    if numerator <= 0 or denominator <= 0:
        return 0.0
    return math.sqrt(numerator / denominator)


def _candidate(
    pump: PumpCharacteristics,
    network: NetworkCondition,
    target_flow_t_h: float,
    calibration: float,
) -> PumpCandidate:
    if target_flow_t_h <= 0.0:
        speed = pump.min_speed_pct
        delivered = _intersection_flow(pump, network, speed)
        return PumpCandidate(
            pump=pump.name,
            speed_pct=speed,
            delivered_flow_t_h=delivered,
            required_flow_t_h=network.required_flow_t_h,
            head_m=network.head_for(delivered),
            power_kw=pump.power_for_speed(speed, calibration),
            feasible=True,
            shortage_t_h=0.0,
            calibration=calibration,
        )

    needed_head = network.head_for(target_flow_t_h)
    needed_speed = pump.speed_for_head(target_flow_t_h, needed_head)
    speed = max(pump.min_speed_pct, min(pump.max_speed_pct, needed_speed))
    delivered = _intersection_flow(pump, network, speed)
    speed_limited = needed_speed > pump.max_speed_pct + 1e-9
    feasible = not speed_limited and delivered + 1e-9 >= target_flow_t_h
    shortage = max(0.0, network.required_flow_t_h - delivered)
    return PumpCandidate(
        pump=pump.name,
        speed_pct=speed,
        delivered_flow_t_h=delivered,
        required_flow_t_h=network.required_flow_t_h,
        head_m=network.head_for(delivered),
        power_kw=pump.power_for_speed(speed, calibration),
        feasible=feasible,
        shortage_t_h=shortage,
        calibration=calibration,
    )


def _calibrations(
    pumps: list[PumpCharacteristics],
    readings: dict[str, PowerReading],
    current_pump: str | None,
    current_speed_pct: float,
) -> tuple[dict[str, float], tuple[str, ...]]:
    failures: tuple[str, ...] = ()
    factors = {pump.name: 1.0 for pump in pumps}
    for pump in pumps:
        reading = readings.get(pump.name)
        if reading is None:
            failures += (f"power_reading_missing:{pump.name}",)
            continue
        if reading.power_kw <= 0.0:
            failures += (f"power_reading_invalid:{pump.name}",)
    if current_pump and current_pump in factors:
        reading = readings.get(current_pump)
        if reading and reading.power_kw > 0.0 and current_speed_pct > 0.0:
            profile = next(item for item in pumps if item.name == current_pump)
            modeled = profile.power_for_speed(current_speed_pct, 1.0)
            if modeled > 0.0:
                factors[current_pump] = reading.power_kw / modeled
    return factors, failures


def _fixed_comparison(
    pump: PumpCharacteristics,
    network: NetworkCondition,
    calibration: float,
    period_s: float,
) -> FixedSpeedComparison:
    flow = _intersection_flow(pump, network, pump.max_speed_pct)
    power = pump.power_for_speed(pump.max_speed_pct, calibration)
    shortage = max(0.0, network.required_flow_t_h - flow)
    return FixedSpeedComparison(
        pump=pump.name,
        speed_pct=pump.max_speed_pct,
        delivered_flow_t_h=flow,
        power_kw=power,
        energy_kwh=power * period_s / 3600.0,
        meets_flow=shortage <= 1e-9,
        shortage_t_h=shortage,
    )


def _fallback_calibration(
    pump: PumpCharacteristics,
    readings: dict[str, PowerReading],
    now: float,
    power_fresh_s: float,
) -> float:
    reading = readings.get(pump.name)
    if reading and reading.power_kw > 0.0 and now - reading.ts <= power_fresh_s:
        return reading.power_kw / pump.power_for_speed(pump.max_speed_pct, 1.0)
    return 1.0


def _fallback(
    pumps: list[PumpCharacteristics],
    network: NetworkCondition,
    readings: dict[str, PowerReading],
    current_pump: str | None,
    runtime_s: dict[str, float],
    now: float,
    period_s: float,
    reasons: tuple[str, ...],
    power_fresh_s: float,
) -> PumpSchedule:
    candidates = [
        _candidate(pump, network, network.required_flow_t_h * FLOW_PRIORITY_MARGIN, 1.0)
        for pump in pumps
    ]
    feasible = [item for item in candidates if item.feasible]
    if current_pump and any(item.pump == current_pump for item in feasible):
        name = current_pump
    elif feasible:
        name = sorted(
            feasible,
            key=lambda item: (-_flow_bucket(item.delivered_flow_t_h), runtime_s.get(item.pump, 0.0)),
        )[0].pump
    else:
        name = sorted(
            candidates,
            key=lambda item: (-_flow_bucket(item.delivered_flow_t_h), runtime_s.get(item.pump, 0.0)),
        )[0].pump
    pump = next(item for item in pumps if item.name == name)
    calibration = _fallback_calibration(pump, readings, now, power_fresh_s)
    flow = _intersection_flow(pump, network, pump.max_speed_pct)
    power = pump.power_for_speed(pump.max_speed_pct, calibration)
    comparison = _fixed_comparison(pump, network, calibration, period_s)
    return PumpSchedule(
        selected_pump=name,
        requested_pump=name,
        speed_pct=pump.max_speed_pct,
        delivered_flow_t_h=flow,
        required_flow_t_h=network.required_flow_t_h,
        power_kw=power,
        energy_kwh=power * period_s / 3600.0,
        policy=policy_for_timestamp(now),
        mode="fixed_speed",
        optimization="periodic_rolling",
        period_s=period_s,
        fixed_speed=comparison,
        saved_energy_kwh=0.0,
        saving_pct=0.0,
        degraded=True,
        partial_failures=reasons,
        reasons=("fallback_fixed_speed:power_unavailable",),
    )


def optimize_pump_schedule(
    pumps: list[PumpCharacteristics],
    network: NetworkCondition,
    power_readings: dict[str, PowerReading],
    *,
    now: float,
    runtime_s: dict[str, float] | None = None,
    current_pump: str | None = None,
    current_speed_pct: float = 0.0,
    last_switch_ts: float = 0.0,
    period_s: float = ROLLING_PERIOD_S,
    power_fresh_s: float = POWER_FRESH_S,
) -> PumpSchedule:
    """计算本轮泵组调度；电耗缺失或过期时整轮退回定速。"""

    if not pumps:
        raise ValueError("至少需要一台泵")
    if period_s <= 0:
        raise ValueError("滚动周期必须为正")
    runtime = dict(runtime_s or {})
    stale = tuple(
        f"power_reading_stale:{pump.name}"
        for pump in pumps
        if pump.name not in power_readings or now - power_readings[pump.name].ts > power_fresh_s
    )
    if stale:
        return _fallback(
            pumps, network, power_readings, current_pump, runtime, now, period_s, stale, power_fresh_s
        )

    factors, failures = _calibrations(pumps, power_readings, current_pump, current_speed_pct)
    if failures:
        return _fallback(
            pumps, network, power_readings, current_pump, runtime, now, period_s, failures, power_fresh_s
        )

    policy = policy_for_timestamp(now)
    target_flow = (
        network.required_flow_t_h * FLOW_PRIORITY_MARGIN
        if policy == "flow_first"
        else network.required_flow_t_h
    )
    candidates = [_candidate(pump, network, target_flow, factors[pump.name]) for pump in pumps]
    feasible = [item for item in candidates if item.feasible]
    if feasible:
        if policy == "flow_first":
            requested = sorted(
                feasible,
                key=lambda item: (
                    -_flow_bucket(item.delivered_flow_t_h),
                    _power_bucket(item.power_kw),
                    runtime.get(item.pump, 0.0),
                ),
            )[0]
        else:
            requested = sorted(
                feasible,
                key=lambda item: (
                    _power_bucket(item.power_kw),
                    runtime.get(item.pump, 0.0),
                    -_flow_bucket(item.delivered_flow_t_h),
                ),
            )[0]
    else:
        # 所有泵都无法满足需求时，任何时段都先抢最大可达流量。
        requested = sorted(
            candidates,
            key=lambda item: (
                -_flow_bucket(item.delivered_flow_t_h),
                _power_bucket(item.power_kw),
                runtime.get(item.pump, 0.0),
            ),
        )[0]

    selected = requested
    switch_deferred = False
    rotation = False
    reasons: list[str] = []
    if current_pump and current_pump != requested.pump:
        current = next((item for item in candidates if item.pump == current_pump), None)
        dwell_ok = now - last_switch_ts >= ROTATION_DWELL_S
        if current is None or not current.feasible:
            reasons.append("switch:flow_capability")
        elif not dwell_ok:
            selected = current
            switch_deferred = True
            reasons.append("switch_deferred:minimum_dwell")
        else:
            close_power = requested.power_kw >= current.power_kw * (1.0 - POWER_SWITCH_TOLERANCE)
            current_runtime = runtime.get(current.pump, 0.0)
            alternate_runtime = runtime.get(requested.pump, 0.0)
            if close_power and current_runtime < alternate_runtime + ROTATION_DWELL_S:
                selected = current
                switch_deferred = True
                reasons.append("switch_deferred:rotation_dwell")
            elif close_power and current_runtime >= ROTATION_DWELL_S:
                rotation = True
                reasons.append("rotation:balanced_runtime")
            elif close_power:
                reasons.append("rotation_hold:minimum_runtime")
            else:
                reasons.append("switch:lower_power")

    baseline_pump = current_pump or selected.pump
    baseline_profile = next(item for item in pumps if item.name == baseline_pump)
    fixed = _fixed_comparison(baseline_profile, network, factors[baseline_pump], period_s)
    saved = max(0.0, fixed.energy_kwh - selected.power_kw * period_s / 3600.0)
    saving = 100.0 * saved / fixed.energy_kwh if fixed.energy_kwh > 0 else 0.0
    if not selected.feasible:
        reasons.append("flow_shortage:all_pumps_limited")

    return PumpSchedule(
        selected_pump=selected.pump,
        requested_pump=requested.pump,
        speed_pct=selected.speed_pct,
        delivered_flow_t_h=selected.delivered_flow_t_h,
        required_flow_t_h=network.required_flow_t_h,
        power_kw=selected.power_kw,
        energy_kwh=selected.power_kw * period_s / 3600.0,
        policy=policy,
        mode="variable_speed",
        optimization="periodic_rolling",
        period_s=period_s,
        candidates=candidates,
        fixed_speed=fixed,
        saved_energy_kwh=saved,
        saving_pct=saving,
        switched=bool(current_pump and selected.pump != current_pump),
        switch_deferred=switch_deferred,
        rotation=rotation,
        reasons=tuple(reasons),
    )
