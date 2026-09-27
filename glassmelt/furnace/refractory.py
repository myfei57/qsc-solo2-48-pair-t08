"""窑体耐火砖热点监视。

窑体耐火砖的烧损从来不是一瞬间的事：某个测点先偏离同片区的邻点，再
持续升温，最后才发展到肉眼可见的发红——靠人工点检看到发红时已经晚了。
本模块把窑体测点按片区组织，每个测点维护一个滑动趋势窗口，用最小二乘
斜率估计升温速率，并按证据强度分级：

- ``normal``  温度与趋势都正常；
- ``watch``   待观察：温度越关注限、升温速率偏高、明显高于同片区邻点，
  或者趋势依据尚不足（窗口样本不够）——只提示位置，不升级；
- ``warning`` 预警：温度越预警限，或升温趋势被连续多个窗口确认；
- ``alarm``   报警：温度越报警限，或升温急促。

升级立即生效（安全优先）；降级必须等温度与趋势回落并保持一段时间，
避免在边界上来回抖动。片区状态取片区内测点的最高级，事件里带着片区
与测点位置；整窑报警要求多个片区同时报警，单点或单片区异常只定位
上报，不触发整窑报警。测点单步突变超出耐火砖物理升温能力时按可疑
处理：该次采样不进趋势窗口、状态最高只到待观察，等后续采样自证。
"""

from __future__ import annotations

import statistics
import threading
from collections import deque
from dataclasses import dataclass

from ..config import RefractoryLimits
from ..errors import SequenceError
from ..eventbus import Event, EventBus

NORMAL = "normal"
WATCH = "watch"
WARNING = "warning"
ALARM = "alarm"

#: 状态等级，下标越大越严重。
STATES = (NORMAL, WATCH, WARNING, ALARM)

#: 各状态对应的事件严重度。
SEVERITY = {NORMAL: "info", WATCH: "notice", WARNING: "warning", ALARM: "alarm"}


def default_layout() -> dict[str, tuple[str, ...]]:
    """默认窑体测点布局：大碹、胸墙、池壁、蓄热室四个片区。"""

    return {
        "crown": ("crown-1", "crown-2", "crown-3"),
        "breastwall": ("breast-1", "breast-2", "breast-3"),
        "sidewall": ("side-1", "side-2", "side-3"),
        "regenerator": ("regen-1", "regen-2", "regen-3"),
    }


@dataclass(frozen=True)
class PointVerdict:
    """一个测点的一次判定结果。"""

    point_id: str
    region: str
    state: str
    temperature_c: float
    slope_c_per_min: float | None
    trend_ready: bool
    rise_streak: int
    deviation_c: float
    suspect: bool
    reasons: tuple[str, ...]
    at: float

    def as_dict(self) -> dict:
        return {
            "point_id": self.point_id,
            "region": self.region,
            "state": self.state,
            "temperature_c": round(self.temperature_c, 3),
            "slope_c_per_min": round(self.slope_c_per_min, 4)
            if self.slope_c_per_min is not None
            else None,
            "trend_ready": self.trend_ready,
            "rise_streak": self.rise_streak,
            "deviation_c": round(self.deviation_c, 3),
            "suspect": self.suspect,
            "reasons": list(self.reasons),
            "at": round(self.at, 3),
        }


@dataclass(frozen=True)
class RefractoryTransition:
    """一次测点／片区／整窑的状态迁移。"""

    scope: str
    subject: str
    state: str
    previous: str
    reasons: tuple[str, ...]
    at: float

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "subject": self.subject,
            "state": self.state,
            "previous": self.previous,
            "reasons": list(self.reasons),
            "at": round(self.at, 3),
        }


class RefractoryMonitor:
    """按测点趋势与片区聚合判定窑体耐火砖热点。"""

    def __init__(
        self,
        clock,
        bus: EventBus,
        limits: RefractoryLimits,
        layout: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._clock = clock
        self._bus = bus
        self._limits = limits
        regions = layout or default_layout()
        self._regions = {region: tuple(points) for region, points in regions.items()}
        self._point_region = {
            point: region for region, points in self._regions.items() for point in points
        }
        self._samples: dict[str, deque] = {point: deque() for point in self._point_region}
        self._last_value: dict[str, float] = {}
        self._state = {point: NORMAL for point in self._point_region}
        self._rise_streak = {point: 0 for point in self._point_region}
        self._clear_since: dict[str, float | None] = {point: None for point in self._point_region}
        self._verdicts: dict[str, PointVerdict] = {}
        self._region_state = {region: NORMAL for region in self._regions}
        self._region_lead: dict[str, str | None] = {region: None for region in self._regions}
        self._kiln_alarm = False
        self._evaluations = 0
        self._escalations = {WATCH: 0, WARNING: 0, ALARM: 0}
        self._kiln_alarms = 0
        self._transitions: list[RefractoryTransition] = []
        self._lock = threading.RLock()

    def point_regions(self) -> dict[str, str]:
        """测点到片区的归属表。"""

        return dict(self._point_region)

    def observe(self, point_id: str, temperature_c: float) -> PointVerdict:
        """录入一个测点的温度并现场判定，返回该测点的判定结果。"""

        if point_id not in self._point_region:
            raise SequenceError(f"未知的窑体测点: {point_id}", component="furnace")
        moment = self._clock.now()
        value = float(temperature_c)
        with self._lock:
            region = self._point_region[point_id]
            previous_value = self._last_value.get(point_id)
            suspect = (
                previous_value is not None
                and abs(value - previous_value) > self._limits.sensor_max_step_c
            )
            self._last_value[point_id] = value
            samples = self._samples[point_id]
            if not suspect:
                samples.append((moment, value))
                cutoff = moment - self._limits.window_s
                while samples and samples[0][0] < cutoff:
                    samples.popleft()
            slope, trend_ready = self._slope(samples)
            if (
                not suspect
                and trend_ready
                and slope is not None
                and slope >= self._limits.rise_confirm_c_per_min
            ):
                streak = self._rise_streak[point_id] + 1
            else:
                streak = 0
            self._rise_streak[point_id] = streak
            deviation = self._deviation(region, value)
            target, reasons = self._target(value, slope, streak, deviation, suspect, trend_ready)
            state = self._apply(point_id, target, value, slope, deviation, suspect, moment)
            if state != target:
                reasons = reasons + ("回落未满保持时间，维持原级",)
            verdict = PointVerdict(
                point_id=point_id,
                region=region,
                state=state,
                temperature_c=value,
                slope_c_per_min=slope,
                trend_ready=trend_ready,
                rise_streak=streak,
                deviation_c=deviation,
                suspect=suspect,
                reasons=reasons,
                at=moment,
            )
            self._verdicts[point_id] = verdict
            self._evaluations += 1
            previous = self._state[point_id]
            changed = state != previous
            if changed:
                self._state[point_id] = state
                if STATES.index(state) > STATES.index(previous):
                    self._escalations[state] += 1
                self._record("point", point_id, state, previous, verdict.reasons, moment)
            region_change = self._refresh_region(region, moment)
            kiln_change = self._refresh_kiln(moment)
        if changed:
            self._publish_point(verdict, previous)
        if region_change is not None:
            self._publish_region(region, region_change)
        if kiln_change is not None:
            self._publish_kiln(kiln_change)
        return verdict

    def observe_many(self, readings: dict[str, float]) -> dict:
        """录入一批测点温度，返回整窑判定汇总。"""

        unknown = sorted(point for point in readings if point not in self._point_region)
        if unknown:
            raise SequenceError(f"未知的窑体测点: {', '.join(unknown)}", component="furnace")
        for point_id, value in readings.items():
            self.observe(point_id, value)
        return self.kiln_verdict()

    def kiln_verdict(self) -> dict:
        """整窑层面的判定汇总：最高片区状态、整窑报警与报警片区。"""

        with self._lock:
            return {
                "state": self._max_region_state(),
                "kiln_alarm": self._kiln_alarm,
                "alarm_regions": sorted(
                    region for region, state in self._region_state.items() if state == ALARM
                ),
                "regions": dict(self._region_state),
            }

    def counts(self) -> dict:
        with self._lock:
            return {
                "evaluations": self._evaluations,
                "watch": self._escalations[WATCH],
                "warning": self._escalations[WARNING],
                "alarm": self._escalations[ALARM],
                "kiln_alarms": self._kiln_alarms,
            }

    def transitions(self, limit: int = 10) -> list[dict]:
        with self._lock:
            return [item.as_dict() for item in self._transitions[-limit:]]

    def snapshot(self) -> dict:
        counts = self.counts()
        with self._lock:
            points = {}
            for point_id, region in self._point_region.items():
                verdict = self._verdicts.get(point_id)
                points[point_id] = (
                    verdict.as_dict()
                    if verdict is not None
                    else {"region": region, "state": NORMAL, "observed": False}
                )
            regions = {
                region: {
                    "state": self._region_state[region],
                    "lead_point": self._region_lead[region],
                }
                for region in self._regions
            }
        return {
            "kiln": self.kiln_verdict(),
            "regions": regions,
            "points": points,
            "watch_c": self._limits.watch_c,
            "warn_c": self._limits.warn_c,
            "alarm_c": self._limits.alarm_c,
            "counts": counts,
            "recent": self.transitions(5),
        }

    def _slope(self, samples: deque) -> tuple[float | None, bool]:
        """窗口内的最小二乘升温速率（℃/分）；样本不足时返回依据不足。"""

        if len(samples) < self._limits.min_samples:
            return None, False
        span = samples[-1][0] - samples[0][0]
        if span < self._limits.window_s * 0.5:
            return None, False
        t0 = samples[0][0]
        xs = [ts - t0 for ts, _ in samples]
        ys = [value for _, value in samples]
        count = len(xs)
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xx = sum(x * x for x in xs)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        denom = count * sum_xx - sum_x * sum_x
        if denom <= 0:
            return None, False
        slope_per_s = (count * sum_xy - sum_x * sum_y) / denom
        return slope_per_s * 60.0, True

    def _deviation(self, region: str, value: float) -> float:
        """测点温度高出同片区中位数多少；片区数据不足时按零计。"""

        peers = [
            self._last_value[point]
            for point in self._regions[region]
            if point in self._last_value
        ]
        if len(peers) < 2:
            return 0.0
        return value - statistics.median(peers)

    def _target(
        self,
        value: float,
        slope: float | None,
        streak: int,
        deviation: float,
        suspect: bool,
        trend_ready: bool,
    ) -> tuple[str, tuple[str, ...]]:
        """按当前证据计算目标状态与判定理由。"""

        limits = self._limits
        if suspect:
            return WATCH, ("测点单步突变，按可疑处理待复核",)
        over_watch = value >= limits.watch_c
        over_warn = value >= limits.warn_c
        over_alarm = value >= limits.alarm_c
        rising = slope is not None and slope >= limits.rise_watch_c_per_min
        confirmed = streak >= limits.confirm_windows
        surging = slope is not None and slope >= limits.rise_alarm_c_per_min
        deviating = deviation >= limits.dev_watch_c
        reasons: list[str] = []
        if over_alarm:
            reasons.append("温度越报警限")
        if surging and over_watch:
            reasons.append("升温急促")
        if reasons:
            return ALARM, tuple(reasons)
        if over_warn:
            reasons.append("温度越预警限")
        if confirmed and (over_watch or deviating):
            reasons.append("持续升温已确认")
        if reasons:
            return WARNING, tuple(reasons)
        if over_watch:
            reasons.append("温度越关注限")
        if rising:
            reasons.append("升温速率偏高")
        if deviating:
            reasons.append("高于同片区中位")
        if reasons:
            if not trend_ready:
                reasons.append("趋势依据不足")
            return WATCH, tuple(reasons)
        return NORMAL, ()

    def _apply(
        self,
        point_id: str,
        target: str,
        value: float,
        slope: float | None,
        deviation: float,
        suspect: bool,
        moment: float,
    ) -> str:
        """把目标状态落成实际状态：升级立即生效，降级须回落并保持。"""

        current = self._state[point_id]
        if STATES.index(target) >= STATES.index(current):
            self._clear_since[point_id] = None
            return target
        clear = (
            not suspect
            and value <= self._limits.watch_c - self._limits.clear_hysteresis_c
            and (slope is None or slope < self._limits.rise_watch_c_per_min)
            and deviation < self._limits.dev_watch_c
        )
        if not clear:
            self._clear_since[point_id] = None
            return current
        since = self._clear_since[point_id]
        if since is None:
            self._clear_since[point_id] = moment
            return current
        if moment - since < self._limits.clear_hold_s:
            return current
        self._clear_since[point_id] = None
        return target

    def _refresh_region(self, region: str, moment: float) -> dict | None:
        """重算片区状态（取片内测点最高级），状态变化时返回迁移内容。"""

        points = self._regions[region]
        lead = max(
            points,
            key=lambda point: (
                STATES.index(self._state[point]),
                self._last_value.get(point, float("-inf")),
            ),
        )
        self._region_lead[region] = lead
        state = self._state[lead]
        previous = self._region_state[region]
        if state == previous:
            return None
        self._region_state[region] = state
        verdict = self._verdicts.get(lead)
        reasons = verdict.reasons if verdict is not None else ()
        self._record("region", region, state, previous, reasons, moment)
        return {"state": state, "previous": previous, "lead_point": lead, "reasons": reasons}

    def _refresh_kiln(self, moment: float) -> dict | None:
        """重算整窑报警：多个片区同时报警才置位，否则只定位到片区。"""

        alarm_regions = sorted(
            region for region, state in self._region_state.items() if state == ALARM
        )
        alarm = len(alarm_regions) >= self._limits.kiln_alarm_regions
        if alarm == self._kiln_alarm:
            return None
        self._kiln_alarm = alarm
        if alarm:
            self._kiln_alarms += 1
        reasons = (
            tuple(f"片区 {name} 报警" for name in alarm_regions)
            if alarm
            else ("报警片区不足，整窑报警复位",)
        )
        self._record("kiln", "kiln", ALARM if alarm else NORMAL, NORMAL if alarm else ALARM, reasons, moment)
        return {"alarm": alarm, "regions": alarm_regions}

    def _max_region_state(self) -> str:
        return max(self._region_state.values(), key=STATES.index, default=NORMAL)

    def _record(
        self,
        scope: str,
        subject: str,
        state: str,
        previous: str,
        reasons: tuple[str, ...],
        moment: float,
    ) -> None:
        self._transitions.append(
            RefractoryTransition(
                scope=scope,
                subject=subject,
                state=state,
                previous=previous,
                reasons=tuple(reasons),
                at=moment,
            )
        )

    def _publish_point(self, verdict: PointVerdict, previous: str) -> None:
        self._bus.publish(
            Event.of(
                "furnace",
                "furnace",
                "refractory_point_state",
                subject=verdict.point_id,
                severity=SEVERITY[verdict.state],
                region=verdict.region,
                state=verdict.state,
                previous=previous,
                temperature_c=round(verdict.temperature_c, 3),
                slope_c_per_min=round(verdict.slope_c_per_min, 4)
                if verdict.slope_c_per_min is not None
                else None,
                reasons=list(verdict.reasons),
            )
        )

    def _publish_region(self, region: str, change: dict) -> None:
        self._bus.publish(
            Event.of(
                "furnace",
                "furnace",
                "refractory_region_state",
                subject=region,
                severity=SEVERITY[change["state"]],
                state=change["state"],
                previous=change["previous"],
                lead_point=change["lead_point"],
                reasons=list(change["reasons"]),
            )
        )

    def _publish_kiln(self, change: dict) -> None:
        self._bus.publish(
            Event.of(
                "furnace",
                "furnace",
                "refractory_kiln_alarm" if change["alarm"] else "refractory_kiln_clear",
                subject="kiln",
                severity="alarm" if change["alarm"] else "notice",
                regions=list(change["regions"]),
            )
        )
