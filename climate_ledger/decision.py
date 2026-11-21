from copy import deepcopy
from decimal import Decimal

from .adaptation import Adaptation
from .contracts import (DomainError, canonical, days, decimal_text, distinct_strings,
                        identifier, integer, moment, number, overlaps, query_spec, ratio,
                        require, shape)
from .forecast import Forecasts
from .residents import Residents


def fee_terms(record):
    shape(record, ("action", "reserve_fee_cents", "activation_fee_cents",
                   "cancel_deadline", "late_cancel_fee_cents"))
    moment(record["cancel_deadline"], "cancel_deadline")
    return {"action": identifier(record["action"], "action"),
            "reserve_fee_cents": integer(record["reserve_fee_cents"], "reserve_fee_cents", 0, 100_000_000_000),
            "activation_fee_cents": integer(record["activation_fee_cents"], "activation_fee_cents", 0, 100_000_000_000),
            "cancel_deadline": record["cancel_deadline"],
            "late_cancel_fee_cents": integer(record["late_cancel_fee_cents"], "late_cancel_fee_cents", 0, 100_000_000_000)}


def decision_spec(value):
    shape(value, ("decision_id", "decision_at", "forecast", "query", "budget_cents",
                  "severe_scenarios", "guarantees", "proposals"))
    identifier(value["decision_id"], "decision_id")
    moment(value["decision_at"], "decision_at")
    identifier(value["forecast"], "forecast")
    query = query_spec(value["query"])
    budget = integer(value["budget_cents"], "budget_cents", 0, 100_000_000_000)
    severe = distinct_strings(value["severe_scenarios"], "severe_scenarios", nonempty=False)
    require(isinstance(value["guarantees"], list) and value["guarantees"], "guarantee", "至少需要一条保障要求")
    guarantees = []
    for item in value["guarantees"]:
        shape(item, ("zone", "group", "minimum_ratio"))
        ratio = number(item["minimum_ratio"], "minimum_ratio", "0", "1")
        guarantees.append({"zone": identifier(item["zone"], "zone"),
                           "group": identifier(item["group"], "group"), "minimum_ratio": str(ratio)})
    require(isinstance(value["proposals"], list) and 1 <= len(value["proposals"]) <= 16,
            "proposals", "候选行动需要在1至16个之间")
    proposals = []
    seen = set()
    for item in value["proposals"]:
        terms = fee_terms(item)
        require(terms["action"] not in seen, "proposals", "候选行动不能重复")
        seen.add(terms["action"])
        proposals.append(terms)
    return {"decision_id": value["decision_id"], "decision_at": value["decision_at"],
            "forecast": value["forecast"], "query": query, "budget_cents": budget,
            "severe_scenarios": severe, "guarantees": guarantees,
            "proposals": sorted(proposals, key=lambda item: item["action"])}


class DecisionEngine:
    """只读的时点决策推演：把联合情景投影为各区日天气，评估候选组合。"""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.forecasts = Forecasts(snapshot)
        self.residents = Residents(snapshot)

    def check_spec(self, spec):
        forecast = self.forecasts.require_visible(spec["forecast"], spec["decision_at"])
        query = spec["query"]
        require(forecast["metric"] == query["metric"], "scope", "预报指标与决策查询不一致")
        require(set(query["zones"]).issubset(forecast["zones"]), "scope", "决策查询区超出预报范围")
        require(forecast["start"] <= query["start"] and query["end"] <= forecast["end"],
                "scope", "决策查询日期超出预报适用期")
        require(set(spec["severe_scenarios"]).issubset({s["id"] for s in forecast["scenarios"]}),
                "scenario", "保障情景不在预报版本中")
        for rule in spec["guarantees"]:
            require(rule["zone"] in query["zones"], "guarantee", "保障区不在查询范围")
            require(rule["group"] in self.residents.registered_groups(rule["zone"]),
                    "guarantee", "保障人群尚未登记")
        terms = {}
        for proposal in spec["proposals"]:
            action = self.snapshot["action"].get(proposal["action"])
            require(action is not None, "action", "候选行动不存在")
            require(action["status"] == "planned", "action", "只能预留计划中的行动")
            require(action["metric"] == query["metric"] and action["zone"] in query["zones"],
                    "scope", "候选行动超出决策范围")
            require(overlaps(action, query), "scope", "候选行动生效期与决策窗口不相交")
            terms[action["id"]] = proposal
        return forecast, terms

    def population_gaps(self, spec):
        gaps = []
        for zone in spec["query"]["zones"]:
            coverage = self.residents.period_coverage(zone, spec["query"]["start"], spec["query"]["end"])
            for item in coverage["missing"]:
                gaps.append({"zone": zone, "day": item["day"], "groups": item["groups"]})
        return gaps

    def project(self, spec, selected):
        """在选定行动组合下，逐联合情景计算区日危险、保护与加权负担。"""
        forecast, terms = self.check_spec(spec)
        projected = deepcopy(self.snapshot)
        for action_id in selected:
            projected["action"][action_id]["status"] = "active"
        Adaptation(projected).validate()
        adaptation = Adaptation(projected)
        query = spec["query"]
        scenario_results = []
        for scenario in forecast["scenarios"]:
            sid = scenario["id"]
            burden_total = Decimal(0)
            protected_total = 0
            hazard_days = 0
            groups = {}
            for zone in query["zones"]:
                for when in days(query["start"], query["end"]):
                    weather = self.forecasts.weather(forecast["id"], sid, query["metric"], zone, when)
                    population = self.residents.day(zone, when)
                    require(population["complete"], "population_gap",
                            "人口分母不完整，不能把未知区日当作零风险", zone=zone, day=when)
                    allocation = adaptation.allocate(zone, query["metric"], when,
                                                      hazardous=weather["hazardous"])
                    burden = self.residents.burden(zone, when, weather["severity"], allocation["protected"])
                    burden_total += Decimal(burden["weighted_burden"])
                    if weather["hazardous"]:
                        hazard_days += 1
                        protected_total += sum(allocation["protected"].values())
                        for group in burden["groups"]:
                            target = groups.setdefault((zone, group["group"]),
                                                       {"zone": zone, "group": group["group"],
                                                        "hazard_person_days": 0, "protected_person_days": 0})
                            target["hazard_person_days"] += group["count"]
                            target["protected_person_days"] += group["protected"]
            scenario_results.append({
                "scenario": sid, "probability": scenario["probability"],
                "hazardous_zone_days": hazard_days,
                "weighted_burden": decimal_text(burden_total),
                "protected_person_days": protected_total,
                "groups": [{"zone": value["zone"], "group": value["group"],
                            "hazard_person_days": value["hazard_person_days"],
                            "protected_person_days": value["protected_person_days"],
                            "protection_ratio": ratio(value["protected_person_days"],
                                                      value["hazard_person_days"])
                            if value["hazard_person_days"] else "1.000000"}
                           for _, value in sorted(groups.items())]})
        return forecast, scenario_results

    def guarantee_violations(self, spec, scenario_results):
        violations = []
        for sid in spec["severe_scenarios"]:
            result = next(row for row in scenario_results if row["scenario"] == sid)
            groups = {(g["zone"], g["group"]): g for g in result["groups"]}
            for rule in spec["guarantees"]:
                key = (rule["zone"], rule["group"])
                actual = groups.get(key)
                # 该情景下此区此人群没有危险日，保障自动满足，不能当作缺口。
                if actual is None:
                    continue
                if Decimal(actual["protection_ratio"]) < Decimal(rule["minimum_ratio"]):
                    violations.append({"scenario": sid, "zone": rule["zone"], "group": rule["group"],
                                       "reason": "below_minimum_protection",
                                       "actual_ratio": actual["protection_ratio"],
                                       "required_ratio": rule["minimum_ratio"],
                                       "hazard_person_days": actual["hazard_person_days"],
                                       "protected_person_days": actual["protected_person_days"]})
        return violations

    @staticmethod
    def infeasible(actions, fee, reasons):
        return {"actions": actions, "reserve_fee_cents": fee,
                "expected_unprotected_burden": None, "expected_protected_person_days": None,
                "scenario_results": [], "guarantee": {"satisfied": False, "violations": []},
                "feasible": False, "infeasible_reasons": reasons}

    def evaluate(self, spec, selected, *, reserve_fee, occupied):
        action_records = {a["id"]: a for a in self.snapshot["action"].values()}
        reasons = []
        for action_id in selected:
            record = action_records[action_id]
            for holder in occupied:
                other = action_records[holder]
                if record["facility"] == other["facility"] and overlaps(record, other):
                    reasons.append({"action": action_id, "reason": "facility_resold", "holder": holder})
        fee = sum(reserve_fee[a] for a in selected)
        if fee > spec["budget_cents"]:
            reasons.append({"reason": "over_budget", "reserve_fee_cents": fee,
                            "budget_cents": spec["budget_cents"]})
        try:
            forecast, scenario_results = self.project(spec, selected)
        except DomainError as exc:
            if exc.code == "facility_conflict":
                reasons.append({"reason": "facility_conflict", "actions": sorted(selected)})
                return self.infeasible(sorted(selected), fee, reasons)
            if exc.code == "population_gap":
                reasons.append({"reason": "unknown_population", "details": exc.details})
                return self.infeasible(sorted(selected), fee, reasons)
            raise
        expected_burden = sum((Decimal(row["weighted_burden"]) * Decimal(row["probability"])
                               for row in scenario_results), Decimal(0)) if scenario_results else None
        expected_protected = (sum((Decimal(row["protected_person_days"]) * Decimal(row["probability"])
                                   for row in scenario_results), Decimal(0))
                              if scenario_results else None)
        violations = self.guarantee_violations(spec, scenario_results)
        return {"actions": sorted(selected), "reserve_fee_cents": fee,
                "expected_unprotected_burden": decimal_text(expected_burden) if expected_burden is not None else None,
                "expected_protected_person_days": decimal_text(expected_protected) if expected_protected is not None else None,
                "scenario_results": scenario_results,
                "guarantee": {"satisfied": not violations, "violations": violations},
                "feasible": not reasons and not violations, "infeasible_reasons": reasons}

    def preview(self, spec, *, occupied=()):
        forecast, terms = self.check_spec(spec)
        gaps = self.population_gaps(spec)
        reserve_fee = {item["action"]: item["reserve_fee_cents"] for item in spec["proposals"]}
        candidates = [item["action"] for item in spec["proposals"]]
        evaluations = []
        for mask in range(1 << len(candidates)):
            selected = [candidates[i] for i in range(len(candidates)) if mask & (1 << i)]
            evaluations.append(self.evaluate(spec, selected, reserve_fee=reserve_fee, occupied=occupied))
        feasible = [row for row in evaluations if row["feasible"]]
        feasible.sort(key=lambda row: (Decimal(row["expected_unprotected_burden"]),
                                       -Decimal(row["expected_protected_person_days"]),
                                       row["reserve_fee_cents"], row["actions"]))
        infeasible = [row for row in evaluations if not row["feasible"]]
        return {"status": "preview", "decision_id": spec["decision_id"], "forecast": forecast["id"],
                "published_at": forecast["published_at"], "decision_at": spec["decision_at"],
                "budget_cents": spec["budget_cents"],
                "population_gaps": gaps,
                "portfolios_evaluated": len(evaluations),
                "feasible_portfolios": len(feasible),
                "recommendation": feasible[0] if feasible else None,
                "guarantee_gap": ([] if feasible else self.describe_gaps(spec, evaluations, gaps)),
                "portfolios": sorted(evaluations, key=lambda row: row["actions"]),
                "infeasible_count": len(infeasible),
                "interpretation": "预览不占用预算或设施名额；确认时须以当时剩余资源重新判断"}

    @staticmethod
    def describe_gaps(spec, evaluations, population_gaps):
        if population_gaps:
            return [{"reason": "unknown_population", "details": population_gaps}]
        reasons = []
        for row in evaluations:
            reasons.extend(row["infeasible_reasons"])
            reasons.extend(row["guarantee"]["violations"])
        unique = []
        seen = set()
        for reason in reasons:
            key = canonical(reason)
            if key not in seen:
                seen.add(key)
                unique.append(reason)
        return unique
