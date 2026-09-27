"""窑体耐火砖热点：多测点趋势判定、待观察、分级报警与整窑确认。"""

from __future__ import annotations

import pytest

from glassmelt.app import CycleInputs
from glassmelt.errors import SequenceError


def _feed_rising(platform, point: str, start_c: float, step_c: float, count: int, interval_s: float = 150.0):
    """按固定步长给测点升温，返回最后一次判定结果。"""

    verdict = None
    value = start_c
    for _ in range(count):
        platform.clock.advance(interval_s)
        verdict = platform.refractory.observe(point, value)
        value += step_c
    return verdict


def test_steady_points_stay_normal(platform):
    for _ in range(6):
        platform.clock.advance(150.0)
        verdict = platform.refractory.observe("crown-1", 300.0)
    assert verdict.state == "normal"
    kiln = platform.refractory.kiln_verdict()
    assert kiln["state"] == "normal"
    assert kiln["kiln_alarm"] is False


def test_insufficient_trend_evidence_gives_watch(platform):
    platform.clock.advance(150.0)
    platform.refractory.observe("crown-1", 330.0)
    platform.clock.advance(150.0)
    verdict = platform.refractory.observe("crown-1", 331.0)
    assert verdict.state == "watch"
    assert verdict.trend_ready is False
    assert "趋势依据不足" in verdict.reasons
    assert platform.refractory.kiln_verdict()["kiln_alarm"] is False


def test_confirmed_rise_escalates_to_warning_with_location(platform):
    verdict = _feed_rising(platform, "crown-2", 330.0, 3.0, 6)
    limits = platform.config.refractory
    assert verdict.state == "warning"
    assert verdict.rise_streak >= limits.confirm_windows
    assert "持续升温已确认" in verdict.reasons
    assert verdict.region == "crown"
    kiln = platform.refractory.kiln_verdict()
    assert kiln["regions"]["crown"] == "warning"
    assert kiln["kiln_alarm"] is False
    events = [
        event
        for event in platform.bus.history("furnace")
        if event.action == "refractory_point_state"
    ]
    assert any(
        event.payload.get("subject") == "crown-2" and event.payload.get("severity") == "warning"
        for event in events
    )


def test_single_region_alarm_stays_regional(platform):
    verdict = platform.refractory.observe("side-1", platform.config.refractory.alarm_c + 5.0)
    assert verdict.state == "alarm"
    kiln = platform.refractory.kiln_verdict()
    assert kiln["regions"]["sidewall"] == "alarm"
    assert kiln["kiln_alarm"] is False
    alarms = platform.audit.alarms()
    assert any(item["subject"] == "side-1" for item in alarms)


def test_kiln_alarm_requires_multiple_regions(platform):
    platform.refractory.observe("side-1", platform.config.refractory.alarm_c + 5.0)
    assert platform.refractory.kiln_verdict()["kiln_alarm"] is False
    platform.refractory.observe("crown-1", platform.config.refractory.alarm_c + 6.0)
    kiln = platform.refractory.kiln_verdict()
    assert kiln["kiln_alarm"] is True
    assert set(kiln["alarm_regions"]) == {"sidewall", "crown"}
    actions = [event.action for event in platform.bus.history("furnace")]
    assert "refractory_kiln_alarm" in actions


def test_abrupt_step_is_treated_as_suspect(platform):
    platform.refractory.observe("crown-1", 300.0)
    verdict = platform.refractory.observe("crown-1", 370.0)
    assert verdict.suspect is True
    assert verdict.state == "watch"
    assert any("突变" in reason for reason in verdict.reasons)
    platform.clock.advance(150.0)
    follow = platform.refractory.observe("crown-1", 371.0)
    assert follow.suspect is False
    assert follow.state == "warning"


def test_demotion_requires_clear_hold(platform):
    verdict = _feed_rising(platform, "crown-2", 330.0, 3.0, 6)
    assert verdict.state == "warning"
    platform.clock.advance(150.0)
    held = platform.refractory.observe("crown-2", 300.0)
    assert held.state == "warning"
    assert "回落未满保持时间，维持原级" in held.reasons
    platform.clock.advance(300.0)
    platform.refractory.observe("crown-2", 300.0)
    platform.clock.advance(150.0)
    settled = platform.refractory.observe("crown-2", 300.0)
    assert settled.state == "normal"


def test_deviation_from_region_peers_gives_watch(platform):
    for point in ("side-1", "side-2", "side-3"):
        platform.refractory.observe(point, 260.0)
    verdict = platform.refractory.observe("side-2", 290.0)
    assert verdict.deviation_c >= platform.config.refractory.dev_watch_c
    assert verdict.state == "watch"
    assert "高于同片区中位" in verdict.reasons


def test_unknown_point_is_rejected(platform):
    with pytest.raises(SequenceError):
        platform.refractory.observe("nowhere-1", 300.0)
    with pytest.raises(SequenceError):
        platform.observe_refractory({"nowhere-2": 300.0})


def test_cycle_and_status_carry_refractory(running):
    result = running.cycle(CycleInputs(load=0.8, pull_rate=1.0, gob_mass_g=186.0, dt=1.0))
    assert result["refractory"]["kiln_alarm"] is False
    status = running.status()
    assert status["refractory"]["kiln"]["state"] == "normal"
    assert len(status["refractory"]["points"]) == 12
    assert status["refractory"]["counts"]["evaluations"] >= 12
