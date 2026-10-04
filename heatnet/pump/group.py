"""两泵一组的顺序执行器。

寻优结果只决定“哪台泵、多少转速”，真正切泵在这里执行：同泵调速直接写目标；
需要切泵时先把运行泵停下并等流量归零，确认母管已脱离原泵后，才允许启动备用泵；
备用泵建立流量后再调到目标。任何时刻 ``running_pump`` 最多只有一个，禁止两台泵
同时抢母管。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .hydraulics import PumpSchedule
from .pump import Pump

MAX_TRANSFER_STEPS = 600
TRANSFER_SPEED_PCT = 80.0


@dataclass
class PumpGroupState:
    """泵组运行与轮换统计。"""

    running_pump: str | None = None
    requested_pump: str | None = None
    last_switch_ts: float = 0.0
    runtime_s: dict[str, float] = field(default_factory=dict)
    switches: int = 0

    def as_dict(self) -> dict:
        return {
            "running_pump": self.running_pump,
            "requested_pump": self.requested_pump,
            "last_switch_ts": round(self.last_switch_ts, 3),
            "runtime_s": {key: round(value, 3) for key, value in sorted(self.runtime_s.items())},
            "switches": self.switches,
        }


class PumpGroup:
    """同一母管上的互备循环泵组。"""

    def __init__(self, pumps: list[Pump]) -> None:
        if len(pumps) < 1:
            raise ValueError("泵组至少要有一台泵")
        names = [pump.name for pump in pumps]
        if len(names) != len(set(names)):
            raise ValueError("泵名不能重复")
        self._pumps = dict((pump.name, pump) for pump in pumps)
        self._state = PumpGroupState(runtime_s=dict.fromkeys(names, 0.0))

    @property
    def state(self) -> PumpGroupState:
        return self._state

    def names(self) -> list[str]:
        return list(self._pumps)

    def get(self, name: str) -> Pump:
        return self._pumps[name]

    def pumps(self) -> list[Pump]:
        return [self._pumps[name] for name in self.names()]

    def running_pump(self) -> str | None:
        return self._state.running_pump

    def runtime_s(self, name: str) -> float:
        return self._state.runtime_s.get(name, 0.0)

    def last_switch_ts(self) -> float:
        return self._state.last_switch_ts

    def speed_of_running(self) -> float:
        if not self._state.running_pump:
            return 0.0
        return self._pumps[self._state.running_pump].speed_pct()

    def tick(self, dt: float, accumulate: bool = True) -> float:
        """推进所有泵的流量惯性，并只给运行泵累计运行时长。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        total = 0.0
        for name, pump in self._pumps.items():
            flow = pump.step(dt)
            if accumulate and name == self._state.running_pump and pump.running():
                self._state.runtime_s[name] = self._state.runtime_s.get(name, 0.0) + dt
                total += flow
        self._enforce_single_running()
        return total

    def stop_all(self, dt: float = 1.0) -> dict:
        """停运整个泵组，先停运行泵并等其停稳。"""

        name = self._state.running_pump
        steps: list[dict] = []
        if name:
            pump = self._pumps[name]
            pump.stop()
            for _ in range(MAX_TRANSFER_STEPS):
                self.tick(dt, accumulate=False)
                if pump.idle():
                    break
            if not pump.idle():
                raise RuntimeError("运行泵未停稳")
            steps.append({"action": "stop", "pump": name, "flow_t_h": round(pump.flow_t_h(), 2)})
            self._state.running_pump = None
            self._state.requested_pump = None
        for other_name, pump in self._pumps.items():
            if other_name != name:
                pump.stop()
                for _ in range(MAX_TRANSFER_STEPS):
                    self.tick(dt, accumulate=False)
                    if pump.idle():
                        break
                if not pump.idle():
                    raise RuntimeError(f"备用泵 {other_name} 未停稳")
        self._enforce_single_running()
        return {"running_pump": None, "steps": steps}

    def apply(self, schedule: PumpSchedule, now: float, dt: float = 1.0) -> dict:
        """按滚动调度执行一次泵组动作。"""

        self._state.requested_pump = schedule.requested_pump
        name = schedule.selected_pump
        if name not in self._pumps:
            raise KeyError(name)
        steps: list[dict] = []
        switched = False

        if self._state.running_pump == name:
            pump = self._pumps[name]
            if not pump.running():
                pump.start(schedule.speed_pct)
                steps.append({"action": "start", "pump": name, "speed_pct": round(schedule.speed_pct, 2)})
            pump.set_speed(schedule.speed_pct, schedule.delivered_flow_t_h)
            self._settle(pump, dt, accumulate=False)
            steps.append(
                {
                    "action": "set_speed",
                    "pump": name,
                    "speed_pct": round(pump.speed_pct(), 2),
                    "flow_t_h": round(pump.flow_t_h(), 2),
                }
            )
        else:
            self._switch(name, schedule, now, dt, steps)
            switched = True

        self._enforce_single_running()
        return {
            "running_pump": self._state.running_pump,
            "requested_pump": schedule.requested_pump,
            "switched": switched,
            "steps": steps,
            "flow_t_h": round(self._pumps[name].flow_t_h(), 2),
        }

    def snapshot(self) -> dict:
        """返回泵组、各泵与运行累计的统一快照。"""

        return {
            **self._state.as_dict(),
            "speed_pct": round(self.speed_of_running(), 2),
            "pumps": {
                name: {
                    **pump.snapshot().as_dict(),
                    "runtime_s": round(self.runtime_s(name), 2),
                    "curve": pump.curve.describe(),
                }
                for name, pump in self._pumps.items()
            },
        }

    def _switch(self, name: str, schedule: PumpSchedule, now: float, dt: float, steps: list[dict]) -> None:
        outgoing = self._state.running_pump
        incoming = self._pumps[name]

        if outgoing:
            old = self._pumps[outgoing]
            old.stop()
            for _ in range(MAX_TRANSFER_STEPS):
                self.tick(dt, accumulate=False)
                if old.idle():
                    break
            if not old.idle():
                raise RuntimeError("原运行泵未停稳，禁止启动备用泵")
            self._state.running_pump = None
            steps.append(
                {"action": "stop_outgoing", "pump": outgoing, "flow_t_h": round(old.flow_t_h(), 2)}
            )

        incoming.start(TRANSFER_SPEED_PCT)
        self._enforce_single_running()
        self._settle(incoming, dt, accumulate=False)
        steps.append(
            {
                "action": "start_standby",
                "pump": name,
                "speed_pct": round(incoming.speed_pct(), 2),
                "flow_t_h": round(incoming.flow_t_h(), 2),
            }
        )

        incoming.set_speed(schedule.speed_pct, schedule.delivered_flow_t_h)
        self._settle(incoming, dt, accumulate=False)
        self._state.running_pump = name
        self._state.last_switch_ts = float(now)
        self._state.switches += 1
        steps.append(
            {
                "action": "accept_target",
                "pump": name,
                "speed_pct": round(incoming.speed_pct(), 2),
                "flow_t_h": round(incoming.flow_t_h(), 2),
            }
        )

    def _settle(self, pump: Pump, dt: float, accumulate: bool = True) -> None:
        for _ in range(MAX_TRANSFER_STEPS):
            self.tick(dt, accumulate=accumulate)
            if abs(pump.flow_t_h() - pump.target_flow_t_h()) <= 1e-6:
                return

    def _enforce_single_running(self) -> None:
        running = [name for name, pump in self._pumps.items() if pump.running()]
        if len(running) > 1:
            raise RuntimeError("两台循环泵同时抢母管")
