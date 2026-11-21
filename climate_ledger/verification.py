from decimal import Decimal

from .adaptation import Adaptation
from .contracts import days, decimal_text, require
from .forecast import Forecasts
from .observatory.readings import Readings
from .residents import Residents


class Verification:
    """事后核验：只用有效观测核对实际危险与已兑现服务，并把差异归因。"""

    def __init__(self, ledger, store):
        self.ledger = ledger
        self.store = store

    def observed_zone_day(self, readings, zone, metric, when, minimum_coverage):
        weather = readings.zone_day(zone, metric, when, minimum_coverage)
        if not weather["usable"]:
            return None
        return Decimal(weather["value"])

    def burden_under_weather(self, snapshot, query, weather_source, *,
                             active_override=None, population_override=None):
        """按给定天气来源与行动集合，重算查询窗口的加权负担与保护。

        weather_source(zone, when) -> Decimal 区日值；返回 None 表示该区日未知。
        active_override：决策启用的行动编号集合；快照中本就生效的常态行动始终保留。
        population_override：预留的人口快照（用于人口修订归因）。
        """
        projected = snapshot
        if active_override is not None:
            from copy import deepcopy
            projected = deepcopy(snapshot)
            baseline_active = {row["id"] for row in projected["action"].values()
                               if row["status"] == "active"}
            keep = baseline_active | set(active_override)
            for row in projected["action"].values():
                row["status"] = "active" if row["id"] in keep else "planned"
            Adaptation(projected).validate()
        residents = Residents(projected) if population_override is None else population_override
        adaptation = Adaptation(projected)
        from .observatory.catalog import Catalog
        catalog = Catalog(projected)
        total_burden = Decimal(0)
        protected = 0
        hazard_population = 0
        unknown = []
        for zone in query["zones"]:
            for when in days(query["start"], query["end"]):
                value = weather_source(zone, when)
                if value is None:
                    unknown.append({"zone": zone, "day": when})
                    continue
                population = residents.day(zone, when)
                require(population["complete"], "population_gap", "人口分母不完整，不能形成核验估计",
                        zone=zone, day=when)
                hazardous = catalog.hazardous(query["metric"], value)
                severity = catalog.severity(query["metric"], value)
                allocation = adaptation.allocate(zone, query["metric"], when, hazardous=hazardous)
                burden = residents.burden(zone, when, severity, allocation["protected"])
                total_burden += Decimal(burden["weighted_burden"])
                if hazardous:
                    hazard_population += population["population"]
                    protected += sum(allocation["protected"].values())
        return {"weighted_burden": decimal_text(total_burden),
                "protected_person_days": protected,
                "hazard_population_days": hazard_population, "unknown_zone_days": unknown}

    def run(self, decision_id, *, minimum_coverage=None):
        with self.ledger.transaction():
            state = self.store.state(decision_id)
            created = state["created"]
            spec = created["spec"]
            query = spec["query"]
            coverage = minimum_coverage or query["minimum_coverage"]
            snapshot = self.ledger.snapshot()
            readings = Readings(snapshot)
            residents = Residents(snapshot)
            forecasts = Forecasts(snapshot)

            observed = {}
            gaps = []
            for zone in query["zones"]:
                for when in days(query["start"], query["end"]):
                    value = self.observed_zone_day(readings, zone, query["metric"], when, coverage)
                    if value is None:
                        gaps.append({"zone": zone, "day": when, "reason": "observation_gap"})
                    else:
                        observed[(zone, when)] = value

            activated = set(state["activated"])
            reserved_not_activated = sorted(set(state["active_reservations"]) - activated)
            cancelled = set(state["cancelled"])

            # 1) 实际负担：只承认已启用且在危险日真正提供服务的行动。
            actual = self.burden_under_weather(
                snapshot, query, lambda zone, when: observed.get((zone, when)),
                active_override=activated)
            actual["reserved_not_activated"] = reserved_not_activated
            actual["cancelled_actions"] = sorted(cancelled)

            # 2) 兑现情景识别：完整观测才能匹配；缺测不借预报下结论。
            realized = None
            if not gaps:
                query_keys = {(zone, when) for zone in query["zones"]
                              for when in days(query["start"], query["end"])}
                realized = forecasts.realize(spec["forecast"], observed, keys=query_keys)

            # 3) 当时预计负担：用决策水位快照、原确认组合与原预报概率重算（可复查）。
            decision_snapshot = self.ledger.snapshot(created["watermark"])
            engine_evaluation = created["evaluation"]
            expected_burden = sum(
                (Decimal(row["weighted_burden"]) * Decimal(row["probability"])
                 for row in engine_evaluation["scenario_results"]), Decimal(0))

            # 4) 反事实分解（仅在观测完整时给出确定值，否则只给已知部分并标未知）。
            decomposition = self.attribute(decision_id, snapshot, decision_snapshot, state, query,
                                           observed, created, coverage, gaps)

            forecast_performance = self.forecast_performance(spec, forecasts, observed, gaps)

            payload = {"decision_id": decision_id, "at_watermark": self.ledger.watermark(),
                       "forecast": spec["forecast"], "realized_scenarios": realized,
                       "observation_gaps": gaps,
                       "expected": {"unprotected_weighted_burden": decimal_text(expected_burden),
                                    "basis_watermark": created["watermark"],
                                    "published_at": created["published_at"]},
                       "committed": {"reservation_paid": state["fee_ledger"]["reservation_paid"],
                                     "reservation_refunded": state["fee_ledger"]["reservation_refunded"],
                                     "late_cancel_paid": state["fee_ledger"]["late_cancel_paid"],
                                     "activation_paid": state["fee_ledger"]["activation_paid"],
                                     "spent_cents": self.store.spent_cents(decision_id),
                                     "active_reservations": sorted(state["active_reservations"]),
                                     "activated": sorted(activated)},
                       "actual": actual,
                       "attribution": decomposition,
                       "forecast_performance": forecast_performance,
                       "research_discipline": "预测驱动的选择不是随机分组，不能用事后气象条件重选看似更优的候选来评价原决定",
                       "interpretation": "实际保护仅计已启用并在危险日兑现的服务；缺测不借预报补确定结果"}
            event = self.store.append_event(decision_id, "verified", payload)
            return {"decision_id": decision_id, "event": event, "verification": payload}

    def attribute(self, decision_id, snapshot, decision_snapshot, state, query, observed, created,
                  coverage, gaps):
        """差异三来源：气象条件偏差、人口修订、未兑现服务。观测不完整时分解标注未知。"""
        if gaps:
            return {"status": "unknown", "reason": "observation_gap", "unknown_zone_days": gaps,
                    "note": "存在缺测区日，不产生确定性归因，避免把未知当作零风险"}
        # 原决定组合只取首个确认事件；后续调整的组合不回写为最初选择。
        first_confirm = next(event for event in self.store.events(decision_id)
                             if event["event_type"] == "confirmed")
        original_selected = set(first_confirm["payload"]["selected"])
        weather = lambda zone, when: observed.get((zone, when))
        # A：原组合 + 原人口 + 实际天气（隔离气象偏差）
        weather_only = self.burden_under_weather(
            decision_snapshot, query, weather, active_override=original_selected)
        # B：原组合 + 当前人口 + 实际天气（再隔离人口修订）
        population_change = self.burden_under_weather(
            snapshot, query, weather, active_override=original_selected)
        # C：实际启用集合 + 当前人口 + 实际天气（再隔离未兑现服务），即 actual
        service_change = self.burden_under_weather(
            snapshot, query, weather, active_override=set(state["activated"]))
        return {"status": "qualified",
                "weather_deviation_burden": weather_only["weighted_burden"],
                "after_population_revision_burden": population_change["weighted_burden"],
                "after_service_delivery_burden": service_change["weighted_burden"],
                "deltas": {
                    "weather": decimal_text(Decimal(weather_only["weighted_burden"])),
                    "population_revision": decimal_text(
                        Decimal(population_change["weighted_burden"]) - Decimal(weather_only["weighted_burden"])),
                    "undelivered_service": decimal_text(
                        Decimal(service_change["weighted_burden"]) - Decimal(population_change["weighted_burden"]))},
                "population_revised": self.population_revised(decision_snapshot, snapshot, query),
                "undelivered_actions": sorted(original_selected - set(state["activated"]))}

    @staticmethod
    def population_revised(before, after, query):
        changes = []
        for identity, old in before["population"].items():
            new = after["population"].get(identity)
            if new != old:
                changes.append({"population": identity,
                                "before_revision": old["revision"],
                                "after_revision": new["revision"] if new else None,
                                "before_count": old["count"],
                                "after_count": new["count"] if new else None})
        for identity, new in after["population"].items():
            if identity not in before["population"]:
                changes.append({"population": identity, "before_revision": None,
                                "after_revision": new["revision"], "before_count": None,
                                "after_count": new["count"]})
        return sorted(changes, key=lambda item: item["population"])

    def forecast_performance(self, spec, forecasts, observed, gaps):
        """预报表现：逐情景逐区日误差与概率校准；缺测区日不参与，也不补值。"""
        forecast = forecasts.version(spec["forecast"])
        metric = spec["query"]["metric"]
        from .observatory.catalog import Catalog
        catalog = Catalog(self.ledger.snapshot())
        scenario_errors = []
        for scenario in forecast["scenarios"]:
            grid = forecasts.grid(forecast["id"], scenario["id"])
            absolute_error = Decimal(0)
            compared = 0
            hazard_hits = 0
            for key, actual_value in observed.items():
                predicted = grid[key]
                absolute_error += abs(predicted - actual_value)
                compared += 1
                if catalog.hazardous(metric, predicted) == catalog.hazardous(metric, actual_value):
                    hazard_hits += 1
            scenario_errors.append({
                "scenario": scenario["id"], "probability": scenario["probability"],
                "compared_zone_days": compared, "missing_zone_days": len(gaps),
                "mean_absolute_error": decimal_text(absolute_error / compared) if compared else None,
                "hazard_direction_accuracy": decimal_text(Decimal(hazard_hits) / compared) if compared else None})
        query_keys = {(zone, when) for zone in spec["query"]["zones"]
                      for when in days(spec["query"]["start"], spec["query"]["end"])}
        realized = forecasts.realize(forecast["id"], observed, keys=query_keys) if not gaps else None
        return {"realized_scenarios": realized,
                "exact_match": bool(realized),
                "scenario_errors": scenario_errors,
                "note": "误差仅基于有效观测；实际观测缺失时不借预报计算确定结果"}
