"""窑体耐火砖：多测点趋势判定、区域预警定位与待观察分级。"""

from __future__ import annotations

import copy
import os

import pytest

from glassmelt.app import CycleInputs
from glassmelt.config import (
    PlatformConfig,
    config_to_mapping,
    default_config,
    load_config,
)
from glassmelt.console.api import ConsoleAPI
from glassmelt.errors import ConfigError, InterlockError

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "glassmelt.yaml"
)


def _region(summary: dict, name: str) -> dict:
    return next(item for item in summary["regions"] if item["region"] == name)


def _feed_ramp(
    platform,
    region: str,
    *,
    start_c: float = 280.0,
    step_c: float = 3.0,
    step_s: float = 80.0,
    count: int = 4,
    points: tuple | None = None,
) -> None:
    """给区域测点补一段等速升温的点检历史（默认 2.25°C/min，持续 240s）。"""

    base = platform.clock.now()
    targets = points or platform.config.refractory.points_in(region)
    for step in range(count):
        moment = base - (count - 1 - step) * step_s
        for point in targets:
            platform.observe_refractory(point, start_c + step_c * step, at=moment)


def test_loaded_config_matches_default_refractory():
    assert load_config(CONFIG_PATH).refractory == default_config().refractory


def test_refractory_config_rejects_bad_sections():
    mapping = config_to_mapping(default_config())
    broken = copy.deepcopy(mapping)
    broken["limits"]["refractory"]["watch_rise_c_per_min"] = 9.9
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(broken)
    duplicated = copy.deepcopy(mapping)
    duplicated["limits"]["refractory"]["regions"]["crown-west"] = ["crown-e1"]
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(duplicated)
    missing = copy.deepcopy(mapping)
    del missing["limits"]["refractory"]
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(missing)


def test_unknown_point_is_rejected(platform):
    with pytest.raises(InterlockError):
        platform.observe_refractory("nowhere-1", 300.0)


def test_no_history_stays_normal(platform):
    summary = platform.evaluate_refractory()
    assert summary["overall"] == "normal"
    assert all(item["observed_points"] == 0 for item in summary["regions"])


def test_insufficient_history_only_watches(platform):
    # 点检次数太少、时间跨度太短：方向向上但依据不足，只能待观察。
    _feed_ramp(platform, "crown-east", count=2, step_s=40.0, step_c=4.0)
    summary = platform.evaluate_refractory()
    region = _region(summary, "crown-east")
    assert summary["overall"] == "watch"
    assert region["state"] == "watch"
    assert region["insufficient_points"] == ["crown-e1", "crown-e2", "crown-e3"]
    assert platform.refractory.counts()["alarms"] == 0


def test_single_point_rise_only_watches(platform):
    # 区域内只有一个测点确认升温，凑不够区域法定数，不升级预警。
    _feed_ramp(platform, "crown-east", points=("crown-e1",))
    for _ in range(platform.config.refractory.confirm_cycles + 1):
        summary = platform.evaluate_refractory()
    region = _region(summary, "crown-east")
    assert region["state"] == "watch"
    assert region["rising_points"] == ["crown-e1"]
    assert platform.refractory.counts()["alarms"] == 0


def test_region_wide_rise_escalates_to_alarm_and_points_location(platform):
    _feed_ramp(platform, "crown-east")
    first = platform.evaluate_refractory()
    region = _region(first, "crown-east")
    assert first["overall"] == "watch"  # 预警级证据先落到待观察
    assert region["candidate"] == "alarm"
    assert region["confirm"] == 1

    second = platform.evaluate_refractory()
    region = _region(second, "crown-east")
    assert second["overall"] == "alarm"
    assert region["state"] == "alarm"
    assert region["rising_points"] == ["crown-e1", "crown-e2", "crown-e3"]
    assert region["mean_slope_c_per_min"] == pytest.approx(2.25, abs=0.01)
    others = {
        item["region"]: item["state"]
        for item in second["regions"]
        if item["region"] != "crown-east"
    }
    assert set(others.values()) == {"normal"}  # 不整窑报警

    alarms = [item for item in platform.audit.alarms() if item["subject"] == "crown-east"]
    assert any(item["action"] == "refractory_region_alarm" for item in alarms)
    snapshot = platform.refractory.snapshot()
    assert snapshot["overall"] == "alarm"
    assert snapshot["alarms"] == 1
    assert snapshot["last_transition"]["to_state"] == "alarm"
    assert snapshot["last_transition"]["rising_points"] == ["crown-e1", "crown-e2", "crown-e3"]


def test_static_hot_spot_only_watches(platform):
    # 单点温度高过同伴一截但没有升温趋势：热点，待观察，不预警。
    _feed_ramp(platform, "crown-east", start_c=285.0, step_c=0.0)
    _feed_ramp(platform, "crown-east", points=("crown-e1",), start_c=320.0, step_c=0.0)
    summary = platform.evaluate_refractory()
    region = _region(summary, "crown-east")
    assert region["state"] == "watch"
    assert region["hot_points"] == ["crown-e1"]
    assert region["rising_points"] == []
    assert platform.refractory.counts()["alarms"] == 0


def test_alarm_clears_after_sustained_normal_readings(platform):
    _feed_ramp(platform, "crown-east")
    for _ in range(platform.config.refractory.confirm_cycles):
        platform.evaluate_refractory()
    assert platform.refractory.snapshot()["overall"] == "alarm"

    platform.clock.advance(400.0)  # 让升温历史滑出趋势窗口
    _feed_ramp(platform, "crown-east", start_c=286.0, step_c=0.0)
    last = {}
    for _ in range(platform.config.refractory.clear_cycles):
        last = platform.evaluate_refractory()
    region = _region(last, "crown-east")
    assert region["state"] == "normal"
    assert platform.refractory.snapshot()["overall"] == "normal"
    assert platform.refractory.counts()["clears"] == 1
    transitions = platform.refractory.transitions()
    assert transitions[-1]["from_state"] == "alarm"
    assert transitions[-1]["to_state"] == "normal"


def test_cycle_feeds_baseline_and_stays_normal(running):
    result = running.cycle(CycleInputs(load=0.8, pull_rate=1.0, gob_mass_g=186.0, dt=1.0))
    assert result["refractory"]["overall"] == "normal"
    assert all(item["observed_points"] > 0 for item in result["refractory"]["regions"])
    status = running.status()
    assert status["refractory"]["overall"] == "normal"
    assert status["refractory"]["points"]
    assert "refractory_max_c" in running.trend.series_names()
    assert "/api/refractory" in status["endpoints"]["api"]


def test_console_api_records_point_checks(platform):
    api = ConsoleAPI(platform)
    recorded = api.record_refractory({"point": "crown-e1", "temperature_c": 290.0})
    assert recorded["ok"] is True
    assert recorded["result"]["point"]["point"] == "crown-e1"
    assert recorded["result"]["refractory"]["overall"] == "normal"
    rejected = api.record_refractory({"point": "nowhere-9", "temperature_c": 1.0})
    assert rejected["ok"] is False
    snapshot = api.refractory()
    assert snapshot["overall"] == "normal"
    assert any(item["point"] == "crown-e1" for item in snapshot["points"])
