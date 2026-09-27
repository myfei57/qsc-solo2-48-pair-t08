"""窑体耐火砖多测点温度监测。

窑体耐火砖侵蚀到后期会局部发热，靠人工点检发现时往往已经发红。这里按
区域组织测点、持续记录温度，对每个测点做滑动窗口趋势拟合，再按区域
汇总判定：

- 测点数据不足（样本太少或时间跨度太短）但方向向上，该点只记"依据
  不足"，区域最多给待观察，不升级；
- 单个测点确认升温，或测点明显高过同区域同伴（热点），区域给待观察，
  提示去现场复核位置；
- 一片区域内至少两个且不少于一半的测点确认升温、区域平均升温速率达到
  预警值，才升级为区域预警，预警事件带上区域名与驱动测点，位置一眼
  可查。

判定只到区域这一级，不存在"整窑报警"：快照里的整窑结论只是各区域状态
的最差值。升级到预警需要连续多个判定周期确认，回落也要连续多个周期才
解除，避免一次抖动就把整窑搅得不得安宁。
"""

from __future__ import annotations

import statistics
import threading
from dataclasses import dataclass

from ..config import RefractoryLimits
from ..errors import InterlockError
from ..eventbus import Event, EventBus

#: 区域状态等级：normal < watch < alarm。
SEVERITY = {"normal": 0, "watch": 1, "alarm": 2}

#: 状态迁移对应的审计动作与严重度。
_TRANSITION_ACTION = {
    "watch": ("refractory_region_watch", "warning"),
    "alarm": ("refractory_region_alarm", "alarm"),
    "normal": ("refractory_region_clear", "notice"),
}


def _slope_c_per_min(samples: list[tuple[float, float]]) -> float | None:
    """对窗口内样本做最小二乘拟合，返回每分钟升温速率；无法拟合时返回 None。"""

    count = len(samples)
    if count < 2:
        return None
    mean_t = sum(item[0] for item in samples) / count
    mean_v = sum(item[1] for item in samples) / count
    denom = sum((item[0] - mean_t) ** 2 for item in samples)
    if denom <= 0.0:
        return None
    cov = sum((item[0] - mean_t) * (item[1] - mean_v) for item in samples)
    return cov / denom * 60.0


@dataclass(frozen=True)
class PointEvaluation:
    """一个测点在某个时刻的现场判定。"""

    point: str
    region: str
    temperature_c: float
    samples: int
    span_s: float
    slope_c_per_min: float | None
    confirmed: bool
    rising: bool
    insufficient: bool
    hot: bool
    evidence: str

    def as_dict(self) -> dict:
        return {
            "point": self.point,
            "region": self.region,
            "temperature_c": round(self.temperature_c, 3),
            "samples": self.samples,
            "span_s": round(self.span_s, 3),
            "slope_c_per_min": round(self.slope_c_per_min, 4)
            if self.slope_c_per_min is not None
            else None,
            "confirmed": self.confirmed,
            "rising": self.rising,
            "insufficient": self.insufficient,
            "hot": self.hot,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class RegionEvaluation:
    """一个区域一轮判定的结果。"""

    region: str
    state: str
    candidate: str
    observed_points: int
    rising_points: tuple[str, ...]
    insufficient_points: tuple[str, ...]
    hot_points: tuple[str, ...]
    mean_slope_c_per_min: float | None
    confirm: int
    clear: int

    def as_dict(self) -> dict:
        return {
            "region": self.region,
            "state": self.state,
            "candidate": self.candidate,
            "observed_points": self.observed_points,
            "rising_points": list(self.rising_points),
            "insufficient_points": list(self.insufficient_points),
            "hot_points": list(self.hot_points),
            "mean_slope_c_per_min": round(self.mean_slope_c_per_min, 4)
            if self.mean_slope_c_per_min is not None
            else None,
            "confirm": self.confirm,
            "clear": self.clear,
        }


@dataclass(frozen=True)
class RegionTransition:
    """一次区域状态迁移。"""

    region: str
    from_state: str
    to_state: str
    rising_points: tuple[str, ...]
    hot_points: tuple[str, ...]
    mean_slope_c_per_min: float | None
    at: float

    def as_dict(self) -> dict:
        return {
            "region": self.region,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "rising_points": list(self.rising_points),
            "hot_points": list(self.hot_points),
            "mean_slope_c_per_min": round(self.mean_slope_c_per_min, 4)
            if self.mean_slope_c_per_min is not None
            else None,
            "at": round(self.at, 3),
        }


class RefractoryMonitor:
    """窑体耐火砖分区热点与升温趋势判定。"""

    def __init__(self, clock, bus: EventBus, limits: RefractoryLimits) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        self._region_of = limits.point_region_map()
        self._samples: dict[str, list[tuple[float, float]]] = {
            point: [] for point in self._region_of
        }
        self._states = {region: "normal" for region in limits.region_names()}
        self._confirm = {region: 0 for region in limits.region_names()}
        self._clear = {region: 0 for region in limits.region_names()}
        self._last_points: dict[str, PointEvaluation] = {}
        self._last_regions: dict[str, RegionEvaluation] = {}
        self._transitions: list[RegionTransition] = []
        self._alarms = 0
        self._watches = 0
        self._clears = 0
        self._lock = threading.RLock()

    def observe(
        self, point: str, temperature_c: float, at: float | None = None
    ) -> PointEvaluation:
        """记录一个测点读数并现场判定该点，返回判定结果。"""

        with self._lock:
            region = self._region_of.get(point)
            if region is None:
                raise InterlockError(f"耐火砖测点未登记: {point}", component="furnace")
            moment = self._clock.now() if at is None else float(at)
            samples = self._samples[point]
            samples.append((moment, float(temperature_c)))
            samples.sort(key=lambda item: item[0])
            cutoff = samples[-1][0] - self._limits.window_s
            self._samples[point] = [item for item in samples if item[0] >= cutoff]
            evaluation = self._evaluate_point(point, moment)
            self._last_points[point] = evaluation
            return evaluation

    def evaluate(self) -> dict:
        """按全部测点的历史读数现场判定各区域，返回整窑汇总。"""

        moment = self._clock.now()
        transitions: list[RegionTransition] = []
        with self._lock:
            for point in self._region_of:
                if self._samples[point]:
                    self._last_points[point] = self._evaluate_point(point, moment)
            evaluations: list[RegionEvaluation] = []
            for region in self._limits.region_names():
                evaluation = self._evaluate_region(region, moment, transitions)
                self._last_regions[region] = evaluation
                evaluations.append(evaluation)
            self._transitions.extend(transitions)
            summary = {
                "overall": self._overall(),
                "regions": [item.as_dict() for item in evaluations],
                "max_point_c": round(
                    max(
                        (item.temperature_c for item in self._last_points.values()),
                        default=0.0,
                    ),
                    3,
                ),
                "evaluated_at": round(moment, 3),
            }
        for transition in transitions:
            self._publish_transition(transition)
        return summary

    def counts(self) -> dict:
        with self._lock:
            return {
                "alarms": self._alarms,
                "watches": self._watches,
                "clears": self._clears,
            }

    def transitions(self, limit: int = 5) -> list[dict]:
        with self._lock:
            return [item.as_dict() for item in self._transitions[-limit:]]

    def snapshot(self) -> dict:
        with self._lock:
            counts = self.counts()
            return {
                "overall": self._overall(),
                "states": dict(sorted(self._states.items())),
                "watch_rise_c_per_min": self._limits.watch_rise_c_per_min,
                "alarm_rise_c_per_min": self._limits.alarm_rise_c_per_min,
                "alarms": counts["alarms"],
                "watches": counts["watches"],
                "clears": counts["clears"],
                "regions": [
                    self._last_regions[region].as_dict()
                    for region in self._limits.region_names()
                    if region in self._last_regions
                ],
                "points": [
                    self._last_points[point].as_dict()
                    for point in sorted(self._last_points)
                ],
                "recent": self.transitions(),
                "last_transition": self._transitions[-1].as_dict()
                if self._transitions
                else None,
            }

    def _window(self, point: str, moment: float) -> list[tuple[float, float]]:
        limit = moment - self._limits.window_s
        return [item for item in self._samples[point] if limit <= item[0] <= moment]

    def _evaluate_point(self, point: str, moment: float) -> PointEvaluation:
        region = self._region_of[point]
        window = self._window(point, moment)
        known = self._samples[point]
        temperature = window[-1][1] if window else known[-1][1]
        count = len(window)
        span = window[-1][0] - window[0][0] if count >= 2 else 0.0
        slope = _slope_c_per_min(window)
        confirmed = (
            slope is not None
            and count >= self._limits.min_trend_samples
            and span >= self._limits.min_span_s
        )
        rising = bool(confirmed and slope >= self._limits.watch_rise_c_per_min)
        insufficient = bool(
            slope is not None
            and slope >= self._limits.watch_rise_c_per_min
            and not confirmed
        )
        hot = self._is_hot(point, region, temperature)
        if rising:
            evidence = "rising"
        elif hot:
            evidence = "hot"
        elif insufficient:
            evidence = "insufficient"
        else:
            evidence = "normal"
        return PointEvaluation(
            point=point,
            region=region,
            temperature_c=temperature,
            samples=count,
            span_s=span,
            slope_c_per_min=slope,
            confirmed=confirmed,
            rising=rising,
            insufficient=insufficient,
            hot=hot,
            evidence=evidence,
        )

    def _is_hot(self, point: str, region: str, temperature_c: float) -> bool:
        """测点是否明显高过同区域同伴的中位数（静态热点）。"""

        peers = [
            self._last_points[other].temperature_c
            for other in self._limits.points_in(region)
            if other != point and other in self._last_points
        ]
        if not peers:
            return False
        return temperature_c - statistics.median(peers) >= self._limits.hot_delta_c

    def _evaluate_region(
        self, region: str, moment: float, transitions: list[RegionTransition]
    ) -> RegionEvaluation:
        points = self._limits.points_in(region)
        observed = [
            self._last_points[point] for point in points if point in self._last_points
        ]
        rising = tuple(item.point for item in observed if item.rising)
        insufficient = tuple(item.point for item in observed if item.insufficient)
        hot = tuple(item.point for item in observed if item.hot)
        slopes = [
            item.slope_c_per_min
            for item in observed
            if item.rising and item.slope_c_per_min is not None
        ]
        mean_slope = sum(slopes) / len(slopes) if slopes else None
        if (
            len(rising) >= self._limits.quorum(region)
            and mean_slope is not None
            and mean_slope >= self._limits.alarm_rise_c_per_min
        ):
            candidate = "alarm"
        elif rising or insufficient or hot:
            candidate = "watch"
        else:
            candidate = "normal"
        previous = self._states[region]
        state = self._advance(region, candidate)
        if state != previous:
            transitions.append(
                RegionTransition(
                    region=region,
                    from_state=previous,
                    to_state=state,
                    rising_points=rising,
                    hot_points=hot,
                    mean_slope_c_per_min=mean_slope,
                    at=moment,
                )
            )
        return RegionEvaluation(
            region=region,
            state=state,
            candidate=candidate,
            observed_points=len(observed),
            rising_points=rising,
            insufficient_points=insufficient,
            hot_points=hot,
            mean_slope_c_per_min=mean_slope,
            confirm=self._confirm[region],
            clear=self._clear[region],
        )

    def _advance(self, region: str, candidate: str) -> str:
        """按本轮证据推进区域状态机，返回推进后的状态。

        待观察立即生效；预警需要连续 ``confirm_cycles`` 个判定周期都拿到
        预警级证据；回落需要连续 ``clear_cycles`` 个周期证据不足才降级。
        """

        state = self._states[region]
        if candidate == state:
            self._confirm[region] = 0
            self._clear[region] = 0
            return state
        target = state
        if SEVERITY[candidate] > SEVERITY[state]:
            self._clear[region] = 0
            if candidate == "watch":
                target = "watch"
            else:
                self._confirm[region] += 1
                if self._confirm[region] >= self._limits.confirm_cycles:
                    self._confirm[region] = 0
                    target = "alarm"
                elif state == "normal":
                    target = "watch"
        else:
            self._confirm[region] = 0
            self._clear[region] += 1
            if self._clear[region] >= self._limits.clear_cycles:
                self._clear[region] = 0
                target = candidate
        if target == state:
            return state
        self._states[region] = target
        if target == "alarm":
            self._alarms += 1
        elif target == "watch":
            self._watches += 1
        else:
            self._clears += 1
        return target

    def _overall(self) -> str:
        """整窑结论：各区域状态的最差值，本身不构成报警。"""

        return max(self._states.values(), key=lambda state: SEVERITY[state])

    def _publish_transition(self, transition: RegionTransition) -> None:
        action, severity = _TRANSITION_ACTION[transition.to_state]
        self._bus.publish(
            Event.of(
                "furnace",
                "furnace",
                action,
                subject=transition.region,
                severity=severity,
                from_state=transition.from_state,
                to_state=transition.to_state,
                rising_points=list(transition.rising_points),
                hot_points=list(transition.hot_points),
                mean_slope_c_per_min=round(transition.mean_slope_c_per_min, 4)
                if transition.mean_slope_c_per_min is not None
                else None,
            )
        )
