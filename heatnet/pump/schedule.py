"""泵组寻优计划。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PumpSpec:
    """一台泵的额定特性。"""

    name: str
    rated_flow_t_h: float
    rated_head_kpa: float
    rated_power_kw: float

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "rated_flow_t_h": round(self.rated_flow_t_h, 2),
            "rated_head_kpa": round(self.rated_head_kpa, 2),
            "rated_power_kw": round(self.rated_power_kw, 2),
        }


@dataclass(frozen=True)
class PumpSchedule:
    """一次滚动寻优得出、供执行与审计复用的唯一计划。"""

    generated_at: float
    demand_t_h: float
    resistance: float
    required_head_kpa: float
    policy: str
    mode: str
    selected_pump: str | None
    speed_pct: float
    target_flow_t_h: float
    delivered_flow_t_h: float
    flow_met: bool
    estimated_power_kw: float
    fixed_pump: str
    fixed_speed_pct: float
    fixed_flow_t_h: float
    fixed_flow_met: bool
    fixed_power_kw: float
    power_saving_kw: float
    power_saving_pct: float
    rolling_period_s: float
    power_available: bool
    partial_failure: bool
    fallback_reason: str = ""
    sequence: tuple[str, ...] = field(default_factory=tuple)
    candidates: tuple[dict, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "generated_at": round(self.generated_at, 3),
            "demand_t_h": round(self.demand_t_h, 2),
            "resistance": self.resistance,
            "required_head_kpa": round(self.required_head_kpa, 2),
            "policy": self.policy,
            "mode": self.mode,
            "selected_pump": self.selected_pump,
            "speed_pct": round(self.speed_pct, 2),
            "target_flow_t_h": round(self.target_flow_t_h, 2),
            "delivered_flow_t_h": round(self.delivered_flow_t_h, 2),
            "flow_met": self.flow_met,
            "estimated_power_kw": round(self.estimated_power_kw, 3),
            "fixed_baseline": {
                "pump": self.fixed_pump,
                "speed_pct": round(self.fixed_speed_pct, 2),
                "delivered_flow_t_h": round(self.fixed_flow_t_h, 2),
                "flow_met": self.fixed_flow_met,
                "estimated_power_kw": round(self.fixed_power_kw, 3),
            },
            "comparison": {
                "power_saving_kw": round(self.power_saving_kw, 3),
                "power_saving_pct": round(self.power_saving_pct, 2),
            },
            "fixed_speed_comparison": {
                "fixed_pump": self.fixed_pump,
                "fixed_speed_pct": round(self.fixed_speed_pct, 2),
                "fixed_flow_t_h": round(self.fixed_flow_t_h, 2),
                "fixed_flow_met": self.fixed_flow_met,
                "fixed_power_kw": round(self.fixed_power_kw, 3),
                "optimized_pump": self.selected_pump,
                "optimized_speed_pct": round(self.speed_pct, 2),
                "optimized_flow_t_h": round(self.delivered_flow_t_h, 2),
                "optimized_power_kw": round(self.estimated_power_kw, 3),
                "power_saving_kw": round(self.power_saving_kw, 3),
                "power_saving_pct": round(self.power_saving_pct, 2),
            },
            "rolling_period_s": round(self.rolling_period_s, 2),
            "power_available": self.power_available,
            "partial_failure": self.partial_failure,
            "fallback_reason": self.fallback_reason,
            "sequence": list(self.sequence),
            "candidates": list(self.candidates),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "PumpSchedule":
        """从持久化文档还原计划。"""

        fixed = dict(raw.get("fixed_baseline", {}))
        comparison = dict(raw.get("comparison", {}))
        return cls(
            generated_at=float(raw.get("generated_at", 0.0)),
            demand_t_h=float(raw.get("demand_t_h", 0.0)),
            resistance=float(raw.get("resistance", 0.0)),
            required_head_kpa=float(raw.get("required_head_kpa", 0.0)),
            policy=str(raw.get("policy", "flow_priority")),
            mode=str(raw.get("mode", "fixed_speed")),
            selected_pump=raw.get("selected_pump"),
            speed_pct=float(raw.get("speed_pct", 0.0)),
            target_flow_t_h=float(raw.get("target_flow_t_h", 0.0)),
            delivered_flow_t_h=float(raw.get("delivered_flow_t_h", 0.0)),
            flow_met=bool(raw.get("flow_met", False)),
            estimated_power_kw=float(raw.get("estimated_power_kw", 0.0)),
            fixed_pump=str(fixed.get("pump", "")),
            fixed_speed_pct=float(fixed.get("speed_pct", 0.0)),
            fixed_flow_t_h=float(fixed.get("delivered_flow_t_h", 0.0)),
            fixed_flow_met=bool(fixed.get("flow_met", False)),
            fixed_power_kw=float(fixed.get("estimated_power_kw", 0.0)),
            power_saving_kw=float(comparison.get("power_saving_kw", 0.0)),
            power_saving_pct=float(comparison.get("power_saving_pct", 0.0)),
            rolling_period_s=float(raw.get("rolling_period_s", 0.0)),
            power_available=bool(raw.get("power_available", False)),
            partial_failure=bool(raw.get("partial_failure", False)),
            fallback_reason=str(raw.get("fallback_reason", "")),
            sequence=tuple(str(item) for item in raw.get("sequence", ())),
            candidates=tuple(dict(item) for item in raw.get("candidates", ())),
        )
