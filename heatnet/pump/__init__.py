"""循环泵。

二次网靠循环泵建立流量。泵的启停与混水阀有硬顺序：先让阀门到位再启泵，停运时
先停泵再关阀。泵本身只负责转速与流量，加上 :mod:`~heatnet.pump.start` 里的
顺序看守和 :mod:`~heatnet.secondary.cycle` 里的收尾保证，组成了不会被异常
短路掉的启停路径。
"""

from __future__ import annotations

from .curve import PumpCurve
from .group import PumpGroup
from .hydraulics import (
    NetworkCondition,
    PowerReading,
    PumpCharacteristics,
    PumpSchedule,
    optimize_pump_schedule,
    policy_for_timestamp,
)
from .pump import Pump
from .start import start_pump_after_valve, stop_pump_before_valve
from .state import PumpSnapshot

__all__ = [
    "NetworkCondition",
    "PowerReading",
    "Pump",
    "PumpCharacteristics",
    "PumpCurve",
    "PumpGroup",
    "PumpSchedule",
    "PumpSnapshot",
    "optimize_pump_schedule",
    "policy_for_timestamp",
    "start_pump_after_valve",
    "stop_pump_before_valve",
]
