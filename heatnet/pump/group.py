"""两台循环泵的互斥切换与运行状态。"""

from __future__ import annotations

from ..config import PumpLimits
from .pump import Pump
from .curve import PumpCurve
from .schedule import PumpSchedule, PumpSpec

MAX_STOP_STEPS = 6000


class PumpGroup:
    """同一母管上的循环泵组，任何时刻只允许一台泵运行。"""

    def __init__(self, specs: list[PumpSpec], limits: PumpLimits) -> None:
        if not specs:
            raise ValueError("泵组至少需要一台泵")
        self._specs = {spec.name: spec for spec in specs}
        self._pumps = {
            spec.name: Pump(
                f"secondary.circulating.{spec.name}",
                limits,
                PumpCurve(
                    limits.min_speed_pct,
                    limits.max_speed_pct,
                    spec.rated_flow_t_h * 0.25,
                    spec.rated_flow_t_h,
                ),
            )
            for spec in specs
        }
        self._runtimes = {spec.name: 0.0 for spec in specs}
        self._active: str | None = None
        self._in_service = False
        self._last_schedule: PumpSchedule | None = None
        self._power_readings: dict[str, float] = {}
        self._power_ts: float | None = None

    def names(self) -> list[str]:
        """按登记顺序返回泵名。"""

        return list(self._specs)

    def specs(self) -> list[PumpSpec]:
        """返回泵额定特性。"""

        return list(self._specs.values())

    def get(self, name: str) -> Pump:
        """读取一台泵。"""

        return self._pumps[name]

    def active_name(self) -> str | None:
        """当前接在母管上的泵；无泵运行时返回 ``None``。"""

        return self._active

    def active_pump(self) -> Pump | None:
        """返回当前运行泵对象。"""

        return None if self._active is None else self._pumps[self._active]

    def in_service(self) -> bool:
        """泵组是否处于自动调度服务中。"""

        return self._in_service

    def runtime_s(self, name: str) -> float:
        """返回累计运行秒数。"""

        return self._runtimes[name]

    def runtimes(self) -> dict[str, float]:
        """返回累计运行秒数副本。"""

        return dict(self._runtimes)

    def last_schedule(self) -> PumpSchedule | None:
        """返回最近一次实际使用的计划。"""

        return self._last_schedule

    def power_readings(self) -> dict[str, float]:
        """返回最新电耗读数副本。"""

        return dict(self._power_readings)

    def power_reading_ts(self) -> float | None:
        """返回最新电耗读数时间。"""

        return self._power_ts

    def set_power_readings(self, readings: dict[str, float], ts: float) -> None:
        """登记一组各泵电耗读数。"""

        clean: dict[str, float] = {}
        for name in self.names():
            value = readings.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"泵 {name} 电耗读数必须是数值")
            if float(value) < 0.0:
                raise ValueError(f"泵 {name} 电耗读数不得为负")
            clean[name] = float(value)
        self._power_readings = clean
        self._power_ts = float(ts)

    def restore(self, payload: dict) -> None:
        """从持久化当前态恢复运行统计和最近计划。"""

        runtimes = payload.get("runtimes", {})
        if isinstance(runtimes, dict):
            for name in self.names():
                value = runtimes.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self._runtimes[name] = max(0.0, float(value))
        self._in_service = bool(payload.get("in_service", False))
        active = payload.get("active_pump")
        self._active = str(active) if active in self._pumps else None
        raw_schedule = payload.get("last_schedule")
        if isinstance(raw_schedule, dict):
            self._last_schedule = PumpSchedule.from_dict(raw_schedule)
        readings = payload.get("power_readings", {})
        if isinstance(readings, dict):
            clean = {}
            for name in self.names():
                value = readings.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    clean[name] = float(value)
            if len(clean) == len(self._pumps):
                self._power_readings = clean
                value = payload.get("power_reading_ts")
                self._power_ts = float(value) if isinstance(value, (int, float)) else None

    def state_dict(self) -> dict:
        """返回需要持久化的泵组当前态。"""

        return {
            "runtimes": {name: round(seconds, 3) for name, seconds in self._runtimes.items()},
            "in_service": self._in_service,
            "active_pump": self._active,
            "last_schedule": (
                None if self._last_schedule is None else self._last_schedule.as_dict()
            ),
            "power_readings": dict(self._power_readings),
            "power_reading_ts": self._power_ts,
        }

    def _stop_active(self) -> None:
        if self._active is None:
            return
        pump = self._pumps[self._active]
        pump.stop()
        for _ in range(MAX_STOP_STEPS):
            pump.step(1.0)
            if pump.idle():
                break
        if not pump.idle():
            raise RuntimeError("循环泵未停稳，禁止备用泵接母管")
        self._active = None

    def apply(self, schedule: PumpSchedule, engage: bool = True) -> dict:
        """执行已经确定的计划，返回与计划一致的执行结果。"""

        selected = schedule.selected_pump
        if schedule.demand_t_h <= 0.0 or selected is None:
            self._stop_active()
            self._in_service = engage
        elif self._active == selected:
            pump = self._pumps[selected]
            if not pump.running():
                pump.start(schedule.speed_pct, schedule.target_flow_t_h)
            else:
                pump.set_speed(schedule.speed_pct, schedule.target_flow_t_h)
            self._in_service = engage
        else:
            # 切泵必须先停旧泵并确认流量归零，然后才允许新泵接母管。
            self._stop_active()
            self._pumps[selected].start(schedule.speed_pct, schedule.target_flow_t_h)
            self._active = selected
            self._in_service = engage

        if schedule.demand_t_h > 0.0 and selected is not None:
            self._active = selected
        self._last_schedule = schedule
        return {
            "active_pump": self._active,
            "selected_pump": selected,
            "sequence": list(schedule.sequence),
            "speed_pct": round(schedule.speed_pct, 2),
            "target_flow_t_h": round(schedule.target_flow_t_h, 2),
            "mode": schedule.mode,
            "partial_failure": schedule.partial_failure,
        }

    def stop_service(self) -> dict:
        """退出泵组自动服务并停稳全部泵。"""

        self._stop_active()
        self._in_service = False
        return {"in_service": False, "active_pump": None}

    def step(self, dt: float = 1.0) -> dict[str, float]:
        """推进当前泵流量，并在其运行时累计运行时长。"""

        flows = {name: 0.0 for name in self.names()}
        for name, pump in self._pumps.items():
            if name == self._active or pump.running() or pump.flow_t_h() > 0.0:
                running_before = pump.running()
                flows[name] = pump.step(dt)
                if running_before:
                    self._runtimes[name] += dt
        return flows

    def snapshot(self) -> dict:
        """返回控制台、审计和持久化共用的泵组状态口径。"""

        return {
            "in_service": self._in_service,
            "active_pump": self._active,
            "runtimes_s": {name: round(seconds, 2) for name, seconds in self._runtimes.items()},
            "power_readings_kw": dict(self._power_readings),
            "power_reading_ts": self._power_ts,
            "last_schedule": (
                None if self._last_schedule is None else self._last_schedule.as_dict()
            ),
            "pumps": {
                name: {
                    "spec": self._specs[name].as_dict(),
                    **self._pumps[name].snapshot().as_dict(),
                    "runtime_s": round(self._runtimes[name], 2),
                }
                for name in self.names()
            },
        }
