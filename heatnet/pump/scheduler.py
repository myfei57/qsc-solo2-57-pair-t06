"""循环泵组滚动寻优。

策略固定为**按周期滚动**，而不是整点批量：每个滚动周期用当前负荷、管网阻力和
最新电耗重新算一次。电耗读数缺失或过期属于部分失败，本周期不做节能寻优，改按
配置的定速泵和固定转速保供水。夜间节电时段在不可行时才牺牲流量；其余时段始终
先保流量，即使要选高电耗泵。
"""

from __future__ import annotations

from ..config import PumpGroupLimits
from .schedule import PumpSchedule, PumpSpec

MIN_SPEED_PCT = 20.0
MAX_SPEED_PCT = 100.0


def in_energy_priority(limits: PumpGroupLimits, hour: int) -> bool:
    """判断当前小时是否为节电优先时段（支持跨零点）。"""

    hour = int(hour) % 24
    start = limits.energy_priority_start_hour
    end = limits.energy_priority_end_hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def policy_for_hour(limits: PumpGroupLimits, hour: int) -> str:
    """按时段返回固定的冲突处理策略。"""

    return "energy_priority" if in_energy_priority(limits, hour) else "flow_priority"


def _fixed_candidate(spec: PumpSpec, resistance: float, speed_pct: float) -> tuple[float, float]:
    speed_ratio = max(0.0, min(1.0, float(speed_pct) / 100.0))
    max_flow_by_head = (spec.rated_head_kpa / resistance) ** 0.5
    flow = min(
        spec.rated_flow_t_h * speed_ratio,
        max_flow_by_head * speed_ratio**0.5,
    )
    return speed_ratio, flow


def _candidate(
    spec: PumpSpec,
    demand_t_h: float,
    resistance: float,
    policy: str,
    min_speed_pct: float = MIN_SPEED_PCT,
) -> dict:
    """计算一台泵在当前负荷下的转速、流量与预测电耗。"""

    demand = max(0.0, float(demand_t_h))
    required_ratio = (resistance * demand**2.0) / spec.rated_head_kpa
    flow_ratio = demand / spec.rated_flow_t_h
    speed_ratio = max(flow_ratio, required_ratio**0.5)
    speed_ratio = max(min_speed_pct / 100.0, speed_ratio)

    if speed_ratio <= 1.0:
        delivered = demand
        feasible = True
        speed = max(min_speed_pct, speed_ratio * 100.0)
    else:
        feasible = False
        speed = MAX_SPEED_PCT
        # 满转速受扬程包络限制的可输送流量，用于流量优先时保最大供水。
        delivered = min(spec.rated_flow_t_h, (spec.rated_head_kpa / resistance) ** 0.5)

    power = spec.rated_power_kw * (speed / 100.0) ** 3.0
    if policy == "energy_priority" and not feasible and demand > 0.0:
        speed = min_speed_pct
        delivered = min(spec.rated_flow_t_h * min_speed_pct / 100.0, demand)
        power = spec.rated_power_kw * (min_speed_pct / 100.0) ** 3.0

    return {
        "pump": spec.name,
        "feasible": feasible,
        "speed_pct": round(speed, 2),
        "delivered_flow_t_h": round(delivered, 2),
        "flow_met": feasible and delivered >= demand - 1e-9,
        "estimated_power_kw": round(power, 3),
        "runtime_s": 0.0,
    }


def _select_optimized(
    candidates: list[dict], runtimes: dict[str, float], balance_s: float, policy: str
) -> dict:
    """按固定排序选泵，保证同条件结果可复现。"""

    def runtime(item: dict) -> float:
        return float(runtimes.get(str(item["pump"]), 0.0))

    feasible = [item for item in candidates if item["feasible"]]
    # 轮换是独立于瞬时电耗的磨损约束：两台泵都能满足流量，且运行时长差超过
    # 配置阈值时，优先选累计运行更短的泵，避免一台长期占母管。
    if feasible and balance_s > 0:
        runtimes_for_feasible = [runtime(item) for item in feasible]
        if max(runtimes_for_feasible) - min(runtimes_for_feasible) >= balance_s:
            return sorted(
                feasible,
                key=lambda item: (
                    runtime(item),
                    float(item["estimated_power_kw"]),
                    str(item["pump"]),
                ),
            )[0]

    def key(item: dict) -> tuple:
        feasible_flag = bool(item["feasible"])
        if policy == "flow_priority":
            # 先可行；不可行时按可输送流量最大化；再用电耗和累计运行时间轮换。
            return (
                0 if feasible_flag else 1,
                -float(item["delivered_flow_t_h"]) if not feasible_flag else 0.0,
                float(item["estimated_power_kw"]),
                runtime(item),
                str(item["pump"]),
            )
        # 节电时段仍先选能满足流量的泵；确实都不够时才退到低电耗方案。
        return (
            0 if feasible_flag else 1,
            float(item["estimated_power_kw"]),
            runtime(item),
            str(item["pump"]),
        )

    return sorted(candidates, key=key)[0]


def _sequence(
    active_pump: str | None, selected_pump: str | None, demand_t_h: float
) -> tuple[str, ...]:
    """生成切泵顺序；任何时刻只允许一台泵接母管。"""

    if demand_t_h <= 0.0:
        if active_pump:
            return (f"stop:{active_pump}",)
        return ()
    if not selected_pump:
        return ()
    if not active_pump:
        return (f"start:{selected_pump}",)
    if active_pump == selected_pump:
        return (f"adjust:{selected_pump}",)
    return (f"stop:{active_pump}", f"confirm_idle:{active_pump}", f"start:{selected_pump}")


def build_schedule(
    specs: list[PumpSpec],
    runtimes: dict[str, float],
    demand_t_h: float,
    resistance: float | None,
    power_readings: dict[str, float] | None,
    now: float,
    hour: int,
    limits: PumpGroupLimits,
    *,
    active_pump: str | None = None,
    readings_ts: float | None = None,
    min_speed_pct: float = MIN_SPEED_PCT,
) -> PumpSchedule:
    """计算一个滚动周期的泵组计划。

    电耗读数本身不直接改写模型，而是作为优化可用性的现场健康条件：任一台泵没有
    读数或读数过期，都按部分失败退回定速，避免用不可核对的电耗声称节能。
    """

    if not specs:
        raise ValueError("泵组至少需要一台泵")
    network_k = float(limits.network_resistance if resistance is None else resistance)
    if network_k <= 0.0:
        raise ValueError("管网阻力系数必须为正")
    demand = max(0.0, float(demand_t_h))
    required_head = network_k * demand**2.0
    policy = policy_for_hour(limits, hour)
    fixed_spec = specs[0]
    fixed_ratio, fixed_flow = _fixed_candidate(
        fixed_spec, network_k, limits.fixed_speed_pct
    )
    fixed_power = fixed_spec.rated_power_kw * fixed_ratio**3.0
    fixed_met = demand > 0.0 and fixed_flow >= demand - 1e-9
    if demand <= 0.0:
        fixed_flow = 0.0
        fixed_power = 0.0
        fixed_met = True

    power_ok = demand <= 0.0
    if demand > 0.0:
        readings = power_readings or {}
        age = None if readings_ts is None else float(now) - float(readings_ts)
        power_ok = all(
            isinstance(readings.get(spec.name), (int, float))
            and not isinstance(readings.get(spec.name), bool)
            and float(readings[spec.name]) >= 0.0
            for spec in specs
        ) and (age is None or 0.0 <= age <= limits.power_max_age_s)

    if not power_ok:
        selected = fixed_spec.name if demand > 0.0 else None
        speed = limits.fixed_speed_pct if demand > 0.0 else 0.0
        target_flow = fixed_flow
        candidates: list[dict] = []
        reason = "power_reading_unavailable"
        mode = "fixed_speed"
        estimated_power = fixed_power
        delivered = fixed_flow
        flow_met = fixed_met
        sequence = _sequence(active_pump, selected, demand)
    else:
        candidates = [_candidate(spec, demand, network_k, policy, min_speed_pct) for spec in specs]
        for item in candidates:
            item["runtime_s"] = round(float(runtimes.get(str(item["pump"]), 0.0)), 2)
        chosen = _select_optimized(candidates, runtimes, limits.rotation_balance_s, policy)
        selected = str(chosen["pump"]) if demand > 0.0 else None
        speed = float(chosen["speed_pct"]) if demand > 0.0 else 0.0
        target_flow = float(chosen["delivered_flow_t_h"]) if demand > 0.0 else 0.0
        delivered = target_flow
        flow_met = bool(chosen["flow_met"]) if demand > 0.0 else True
        estimated_power = float(chosen["estimated_power_kw"]) if demand > 0.0 else 0.0
        reason = ""
        mode = "optimized"
        sequence = _sequence(active_pump, selected, demand)

    saving = fixed_power - estimated_power if power_ok and demand > 0.0 else 0.0
    saving_pct = (saving / fixed_power * 100.0) if fixed_power > 0.0 and power_ok else 0.0
    return PumpSchedule(
        generated_at=float(now),
        demand_t_h=demand,
        resistance=network_k,
        required_head_kpa=required_head,
        policy=policy,
        mode=mode,
        selected_pump=selected,
        speed_pct=speed,
        target_flow_t_h=target_flow,
        delivered_flow_t_h=delivered,
        flow_met=flow_met,
        estimated_power_kw=estimated_power,
        fixed_pump=fixed_spec.name,
        fixed_speed_pct=limits.fixed_speed_pct if demand > 0.0 else 0.0,
        fixed_flow_t_h=fixed_flow,
        fixed_flow_met=fixed_met,
        fixed_power_kw=fixed_power,
        power_saving_kw=saving,
        power_saving_pct=saving_pct,
        rolling_period_s=limits.rolling_period_s,
        power_available=power_ok,
        partial_failure=not power_ok and demand > 0.0,
        fallback_reason=reason,
        sequence=sequence,
        candidates=tuple(candidates),
    )
