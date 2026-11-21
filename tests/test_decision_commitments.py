import copy
import json
import tempfile
import unittest
from pathlib import Path

from climate_ledger.application import Application
from climate_ledger.contracts import DomainError


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class CommitmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name) / "city.sqlite3")
        self.addCleanup(self.app.ledger.close)
        self.app.ingest(example("city.json"))
        self.app.ingest(example("forecast.json"))
        self.spec = example("decision.json")

    def assert_error(self, code, call):
        with self.assertRaises(DomainError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def confirm(self, selected=None):
        if selected is None:
            selected = self.app.preview_decision(self.spec)["recommendation"]["actions"]
        return self.app.confirm_decision(self.spec, selected)

    def test_preview_does_not_consume_budget_or_facility(self):
        before = self.app.list_decisions()
        preview_a = self.app.preview_decision(self.spec)
        preview_b = self.app.preview_decision({**self.spec, "decision_id": "parallel-preview"})
        self.assertEqual(before["decisions"], [])
        # 两个预览都能独立给出推荐，互不占用名额。
        self.assertTrue(preview_a["recommendation"])
        self.assertTrue(preview_b["recommendation"])
        self.assertEqual(self.app.list_decisions()["decisions"], [])

    def test_two_confirms_cannot_spend_same_facility_capacity(self):
        # 第一决策预留 shared-plaza 上的山麓行动（单行动即可满足保障）。
        first = self.confirm(["cool-hill"])
        self.assertIn("cool-hill", first["state"]["active_reservations"])
        second_spec = {**self.spec, "decision_id": "second-bid"}
        # 另一决策想预留同设施 shared-plaza 的河畔行动，必须被设施占用拦截。
        self.assert_error("infeasible",
                          lambda: self.app.confirm_decision(second_spec, ["shared-river-alt"]))
        # 不同设施（hill-gym、river-park）的组合仍可确认并满足两区保障。
        second = self.app.confirm_decision(second_spec, ["cool-hill-alt", "shared-river"])
        self.assertEqual(set(second["state"]["active_reservations"]),
                         {"cool-hill-alt", "shared-river"})

    def test_confirm_rechecks_remaining_budget_at_confirmation_time(self):
        # 把预算压到低于推荐预留费，确认必须失败而非沿用预览结论。
        tight = {**self.spec, "budget_cents": 1000}
        self.assert_error("infeasible", lambda: self.app.confirm_decision(
            tight, self.app.preview_decision(self.spec)["recommendation"]["actions"]))

    def test_confirmed_decision_cannot_be_overwritten(self):
        self.confirm(["cool-hill-alt"])
        self.assert_error("decision", lambda: self.confirm(["cool-hill-alt"]))

    def test_early_cancel_refunds_reserve_and_late_cancel_keeps_penalty(self):
        self.confirm(["cool-hill", "cool-hill-alt"])
        early = self.app.cancel_reservation(self.spec["decision_id"], ["cool-hill-alt"], "2026-07-01T10:00")
        self.assertEqual(early["state"]["fee_ledger"]["reservation_refunded"], 2500)
        self.assertNotIn("cool-hill-alt", early["state"]["active_reservations"])
        late = self.app.cancel_reservation(self.spec["decision_id"], ["cool-hill"], "2026-07-02T10:00")
        self.assertEqual(late["state"]["fee_ledger"]["late_cancel_paid"], 3000)
        self.assertEqual(late["state"]["fee_ledger"]["reservation_refunded"], 2500)
        # 已发生费用不会被抹掉。
        self.assertEqual(late["state"]["cancelled"]["cool-hill"]["fee_cents"], 3000)

    def test_activated_action_cannot_be_cancelled_as_reservation(self):
        self.confirm(["cool-hill"])
        self.app.activate_reservation(self.spec["decision_id"], ["cool-hill"], "2026-07-02T08:00")
        self.assert_error("decision", lambda: self.app.cancel_reservation(
            self.spec["decision_id"], ["cool-hill"], "2026-07-02T09:00"))

    def test_adjustment_preserves_original_decision_and_cancel_cost(self):
        self.confirm(["cool-hill"])
        new_spec = {**self.spec, "decision_at": "2026-06-28T10:00"}
        adjusted = self.app.adjust_decision(self.spec["decision_id"], new_spec,
                                            ["cool-hill-alt"], "2026-06-28T10:00")
        view = self.app.show_decision(self.spec["decision_id"])
        kinds = [event["event_type"] for event in view["events"]]
        # 原确认事件仍在，先取消旧预留，再追加新确认；历史不被替换。
        self.assertEqual(kinds, ["confirmed", "cancelled", "confirmed"])
        self.assertIn("cool-hill", adjusted["state"]["cancelled"])
        self.assertIn("cool-hill-alt", adjusted["state"]["active_reservations"])
        original = next(e for e in view["events"] if e["event_type"] == "confirmed")
        self.assertEqual(original["payload"]["selected"], ["cool-hill"])
    def test_adjustment_after_deadline_records_late_fee_and_rechecks_budget(self):
        self.confirm(["cool-hill"])
        new_spec = {**self.spec, "decision_at": "2026-07-02T10:00"}
        adjusted = self.app.adjust_decision(self.spec["decision_id"], new_spec,
                                            ["cool-hill-alt"], "2026-07-02T10:00")
        self.assertEqual(adjusted["state"]["fee_ledger"]["late_cancel_paid"], 3000)

    def test_no_feasible_portfolio_reports_guarantee_gap_not_zero_risk(self):
        # 预算为0时任何含行动的组合都因费用不可行，空组合又无法满足严重情景保障；
        # 系统必须显式报告保障缺口，而不是把空组合当作零风险最优解。
        impossible = {**self.spec, "budget_cents": 0}
        preview = self.app.preview_decision(impossible)
        self.assertEqual(preview["feasible_portfolios"], 0)
        self.assertIsNone(preview["recommendation"])
        gap_reasons = {g.get("reason") for g in preview["guarantee_gap"]}
        self.assertIn("below_minimum_protection", gap_reasons)
        self.assertIn("over_budget", gap_reasons)
        # 空组合的保障违规要指明情景、区、人群与实际/要求比例。
        empty = next(p for p in preview["portfolios"] if not p["actions"])
        self.assertFalse(empty["feasible"])
        violation = empty["guarantee"]["violations"][0]
        self.assertEqual(set(violation), {"scenario", "zone", "group", "reason", "actual_ratio",
                                          "required_ratio", "hazard_person_days",
                                          "protected_person_days"})

    def test_recommendation_ranks_feasible_portfolios_by_expected_burden(self):
        preview = self.app.preview_decision(self.spec)
        feasible = sorted((p for p in preview["portfolios"] if p["feasible"]),
                          key=lambda p: float(p["expected_unprotected_burden"]))
        self.assertEqual(preview["recommendation"]["actions"], feasible[0]["actions"])
        burdens = [float(p["expected_unprotected_burden"]) for p in feasible]
        self.assertEqual(burdens, sorted(burdens))
        reco = preview["recommendation"]
        for row in reco["scenario_results"]:
            self.assertIn("probability", row)

    def test_adjustment_budget_guard_blocks_unaffordable_late_fees(self):
        self.confirm(["cool-hill"])
        # 预算只够一次新预留，无法再吸收逾期取消罚金；截止后调整应在预算守卫处失败且不留半条事件。
        tight = {**self.spec, "budget_cents": 5500, "decision_at": "2026-07-02T10:00"}
        before = len(self.app.show_decision(self.spec["decision_id"])["events"])
        self.assert_error("over_budget", lambda: self.app.adjust_decision(
            self.spec["decision_id"], tight, ["cool-hill-alt"], "2026-07-02T10:00"))
        self.assertEqual(len(self.app.show_decision(self.spec["decision_id"])["events"]), before)

    def test_unknown_population_is_reported_not_treated_as_zero(self):
        city = example("city.json")
        pop = next(copy.deepcopy(r) for r in city["records"]
                   if r["kind"] == "population" and r["data"]["id"] == "river-senior")
        pop["data"].update(revision=2, end="2026-07-02")
        self.app.ingest({"id": "pop-gap", "source": city["source"], "records": [pop]})
        preview = self.app.preview_decision(self.spec)
        self.assertTrue(preview["population_gaps"])
        self.assertEqual(preview["feasible_portfolios"], 0)
        self.assertTrue(any(g["reason"] == "unknown_population" for g in preview["guarantee_gap"]))
        # 人口不完整时任何组合都不能被确认，不能把缺测人群当作零风险后通过。
        self.assert_error("infeasible",
                          lambda: self.app.confirm_decision(self.spec, ["cool-hill-alt"]))


if __name__ == "__main__":
    unittest.main()
