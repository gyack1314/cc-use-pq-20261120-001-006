from decimal import Decimal

from .contracts import (days, decimal_text, distinct_strings, identifier, integer, moment,
                        number, period, require, shape)
from .observatory.catalog import Catalog


def normalize_forecast(record):
    shape(record, ("id", "revision", "source", "published_at", "metric", "start", "end",
                   "zones", "scenarios"))
    start, end = period(record["start"], record["end"])
    zones = distinct_strings(record["zones"], "zones")
    moment(record["published_at"], "published_at")
    require(isinstance(record["scenarios"], list) and 1 <= len(record["scenarios"]) <= 64,
            "scenario", "联合情景数量需在1至64之间")
    scenarios = []
    scenario_ids = set()
    probability_total = Decimal(0)
    for item in record["scenarios"]:
        shape(item, ("id", "probability", "readings"))
        scenario_id = identifier(item["id"], "scenario")
        require(scenario_id not in scenario_ids, "scenario", "情景编号不能重复")
        scenario_ids.add(scenario_id)
        probability = number(item["probability"], "probability", "0", "1")
        probability_total += probability
        require(isinstance(item["readings"], list), "scenario", "情景读数需要数组")
        readings = {}
        for cell in item["readings"]:
            shape(cell, ("zone", "day", "value"))
            zone = identifier(cell["zone"], "zone")
            require(zone in zones, "scenario", "情景读数行政区超出预报范围", zone=zone)
            when = cell["day"]
            require(start <= when <= end, "scenario", "情景读数日期超出预报适用期")
            key = (zone, when)
            require(key not in readings, "scenario", "同一情景区日读数重复", zone=zone, day=when)
            readings[key] = str(number(cell["value"], "forecast_value"))
        scenarios.append({"id": scenario_id, "probability": str(probability),
                          "readings": [{"zone": zone, "day": when, "value": readings[(zone, when)]}
                                       for zone, when in sorted(readings)]})
    require(probability_total == Decimal("1"), "probability", "情景概率之和必须为1",
            total=decimal_text(probability_total))
    expected = {(zone, when) for zone in zones for when in days(start, end)}
    for scenario in scenarios:
        actual = {(cell["zone"], cell["day"]) for cell in scenario["readings"]}
        missing = sorted(expected - actual)
        require(not missing, "scenario_gap", "情景区日不完整，缺测不能由预报填补",
                missing=[{"zone": zone, "day": when} for zone, when in missing])
    return {"id": identifier(record["id"]), "revision": integer(record["revision"], "revision", 1),
            "source": identifier(record["source"], "source"), "published_at": record["published_at"],
            "metric": identifier(record["metric"], "metric"), "start": start, "end": end,
            "zones": zones, "scenarios": sorted(scenarios, key=lambda item: item["id"])}


class Forecasts:
    """联合情景预报。同一情景的各区读数属于共同气象过程，不拆分重组。"""

    def __init__(self, snapshot):
        self.rows = snapshot["forecast"]
        self.catalog = Catalog(snapshot)

    def validate(self):
        for row in self.rows.values():
            metric = self.catalog.metric(row["metric"])
            require(set(row["zones"]).issubset(self.snapshot_zones()), "reference", "预报行政区不存在")
            low, high = Decimal(metric["minimum"]), Decimal(metric["maximum"])
            for scenario in row["scenarios"]:
                for cell in scenario["readings"]:
                    value = Decimal(cell["value"])
                    require(low <= value <= high, "forecast_range", "预报读数超出指标合理值域",
                            forecast=row["id"], scenario=scenario["id"])

    def snapshot_zones(self):
        return set(self.catalog.zones)

    def published_before(self, when):
        """决策时点可见版本：仅取发布时刻不晚于决策时刻的版本，未来发布不能倒流。"""
        return [row for row in self.rows.values() if row["published_at"] <= when]

    def version(self, forecast_id):
        row = self.rows.get(forecast_id)
        require(row is not None, "forecast", "预报版本不存在", forecast=forecast_id)
        return row

    def require_visible(self, forecast_id, decision_at):
        row = self.version(forecast_id)
        require(row["published_at"] <= decision_at, "forecast_timing",
                "决策时点不能使用尚未发布的预报版本", published_at=row["published_at"],
                decision_at=decision_at)
        return row

    def grid(self, forecast_id, scenario_id):
        row = self.version(forecast_id)
        scenario = next((item for item in row["scenarios"] if item["id"] == scenario_id), None)
        require(scenario is not None, "scenario", "预报不含该联合情景", scenario=scenario_id)
        return {(cell["zone"], cell["day"]): Decimal(cell["value"]) for cell in scenario["readings"]}

    def probability(self, forecast_id, scenario_id):
        row = self.version(forecast_id)
        scenario = next(item for item in row["scenarios"] if item["id"] == scenario_id)
        return Decimal(scenario["probability"])

    def weather(self, forecast_id, scenario_id, metric, zone, when):
        """以预报读数代替观测形成情景天气；预报不是观测，不写入暴露分析的事实口径。"""
        value = self.grid(forecast_id, scenario_id)[(zone, when)]
        return {"value": decimal_text(value),
                "hazardous": self.catalog.hazardous(metric, value),
                "severity": decimal_text(self.catalog.severity(metric, value))}

    def realize(self, forecast_id, observed, keys=None):
        """判断实际观测与哪个情景完全一致；任一待匹配区日缺测则无法识别，不借预报补结果。

        observed 为 {(zone, day): Decimal(value)}，仅含可用观测。
        keys 给出需要匹配的区日网格；缺省为该预报版本的完整网格。
        """
        row = self.version(forecast_id)
        full = {(zone, when) for zone in row["zones"] for when in days(row["start"], row["end"])}
        expected = full if keys is None else set(keys)
        if keys is not None:
            require(expected.issubset(full), "scenario", "匹配网格超出预报范围")
        require(set(observed) == expected, "observation_gap", "实际观测不完整，不能识别兑现情景",
                missing=[{"zone": zone, "day": when} for zone, when in sorted(expected - set(observed))])
        matches = []
        for scenario in row["scenarios"]:
            grid = self.grid(forecast_id, scenario["id"])
            if all(grid[key] == observed[key] for key in expected):
                matches.append(scenario["id"])
        return matches
