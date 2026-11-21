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


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "city.sqlite3"
        self.app = Application(self.database)
        self.addCleanup(self.app.ledger.close)
        self.app.ingest(example("city.json"))
        self.app.ingest(example("forecast.json"))
        self.spec = example("decision.json")
        self.decision_id = self.spec["decision_id"]

    def assert_error(self, code, call):
        with self.assertRaises(DomainError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def reserve_only(self, actions):
        """确认但不启用，制造只预留未兑现的服务。"""
        return self.app.confirm_decision(self.spec, actions)

    def test_reservation_without_activation_is_not_actual_protection(self):
        self.reserve_only(["cool-hill"])
        verification = self.app.verify_decision(self.decision_id)["verification"]
        # cool-hill 仅预留未启用，实际保护中不能计入它；只有常态生效的 cool-river 提供服务。
        self.assertEqual(verification["committed"]["activated"], [])
        self.assertIn("cool-hill", verification["actual"]["reserved_not_activated"])
        # 未启用的山麓行动在 S2（山麓实际偏热）下没有保护，实际未保护负担为正。
        self.assertGreater(float(verification["actual"]["weighted_burden"]), 0)
        self.assertIn("cool-hill", verification["attribution"]["undelivered_actions"])
        self.assertGreater(float(verification["attribution"]["deltas"]["undelivered_service"]), 0)

    def test_activated_service_is_counted_and_attribution_qualified(self):
        self.app.confirm_decision(self.spec, ["cool-hill"])
        self.app.activate_reservation(self.decision_id, ["cool-hill"], "2026-07-02T08:00")
        verification = self.app.verify_decision(self.decision_id)["verification"]
        self.assertEqual(verification["attribution"]["status"], "qualified")
        self.assertEqual(verification["attribution"]["undelivered_actions"], [])
        self.assertEqual(verification["attribution"]["deltas"]["undelivered_service"], "0.000000")
        # 三来源分解字段齐全。
        self.assertEqual(set(verification["attribution"]["deltas"]),
                         {"weather", "population_revision", "undelivered_service"})

    def test_missing_observation_does_not_borrow_forecast_for_certain_result(self):
        self.app.confirm_decision(self.spec, ["cool-hill"])
        city = example("city.json")
        reading = next(copy.deepcopy(r) for r in city["records"]
                       if r["kind"] == "reading" and r["data"]["station"] == "hill-s"
                       and r["data"]["day"] == "2026-07-03")
        reading["data"].update(revision=2, quality="missing", value=None)
        self.app.ingest({"id": "hill-gap", "source": city["source"], "records": [reading]})
        verification = self.app.verify_decision(self.decision_id)["verification"]
        self.assertTrue(verification["observation_gaps"])
        # 观测不完整时不能识别兑现情景，也不能给确定性归因。
        self.assertIsNone(verification["realized_scenarios"])
        self.assertEqual(verification["attribution"]["status"], "unknown")
        self.assertFalse(verification["forecast_performance"]["exact_match"])
        # 预报误差只基于仍有效的观测，缺测区日不补值。
        for row in verification["forecast_performance"]["scenario_errors"]:
            self.assertEqual(row["compared_zone_days"], 3)
            self.assertEqual(row["missing_zone_days"], 1)

    def test_three_way_attribution_separates_weather_population_and_service(self):
        self.app.confirm_decision(self.spec, ["cool-hill"])
        # 启用山麓服务，再修订河畔老人人口，制造人口修订来源。
        self.app.activate_reservation(self.decision_id, ["cool-hill"], "2026-07-02T08:00")
        city = example("city.json")
        pop = next(copy.deepcopy(r) for r in city["records"]
                   if r["kind"] == "population" and r["data"]["id"] == "river-senior")
        pop["data"].update(revision=2, count=40)
        self.app.ingest({"id": "pop-revision", "source": city["source"], "records": [pop]})
        verification = self.app.verify_decision(self.decision_id)["verification"]
        attribution = verification["attribution"]
        self.assertTrue(attribution["population_revised"])
        self.assertEqual(attribution["population_revised"][0]["population"], "river-senior")
        self.assertEqual(attribution["population_revised"][0]["after_count"], 40)
        # 人口修订项非零，且与气象、服务项分开列示。
        self.assertNotEqual(attribution["deltas"]["population_revision"], "0.000000")

    def test_expected_committed_and_actual_are_distinctly_reported(self):
        self.app.confirm_decision(self.spec, ["cool-hill"])
        self.app.activate_reservation(self.decision_id, ["cool-hill"], "2026-07-02T08:00")
        verification = self.app.verify_decision(self.decision_id)["verification"]
        # 预计：决策水位与预报概率；承诺：费用台账；实际：观测负担。三者口径独立。
        self.assertIn("unprotected_weighted_burden", verification["expected"])
        self.assertEqual(verification["expected"]["basis_watermark"],
                         verification["committed"]["active_reservations"] and 2 or 2)
        self.assertEqual(verification["committed"]["spent_cents"],
                         verification["committed"]["reservation_paid"]
                         + verification["committed"]["activation_paid"])
        self.assertIn("weighted_burden", verification["actual"])

    def test_forecast_performance_reports_scenario_errors_without_reselecting(self):
        self.app.confirm_decision(self.spec, ["cool-hill"])
        verification = self.app.verify_decision(self.decision_id)["verification"]
        # 实际天气与两个联合情景都不完全相等：如实报告未命中，而不是事后挑一个最优情景。
        self.assertEqual(verification["realized_scenarios"], [])
        self.assertFalse(verification["forecast_performance"]["exact_match"])
        errors = verification["forecast_performance"]["scenario_errors"]
        self.assertEqual({row["scenario"] for row in errors}, {"S1-river-hot", "S2-hill-hot"})
        self.assertTrue(all(row["mean_absolute_error"] is not None for row in errors))

    def test_repeated_verification_appends_evidence_and_keeps_history(self):
        self.app.confirm_decision(self.spec, ["cool-hill"])
        first = self.app.verify_decision(self.decision_id)
        second = self.app.verify_decision(self.decision_id)
        view = self.app.show_decision(self.decision_id)
        verified = [e for e in view["events"] if e["event_type"] == "verified"]
        self.assertEqual(len(verified), 2)
        self.assertNotEqual(first["event"]["digest"], second["event"]["digest"])

    def test_adjusted_decision_keeps_original_portfolio_as_undelivered_basis(self):
        # 原决定确认 cool-hill 但不启用，随后调整为 cool-hill-alt 并启用新行动。
        self.app.confirm_decision(self.spec, ["cool-hill"])
        new_spec = {**self.spec, "decision_at": "2026-06-28T10:00"}
        self.app.adjust_decision(self.decision_id, new_spec, ["cool-hill-alt"], "2026-06-28T10:00")
        self.app.activate_reservation(self.decision_id, ["cool-hill-alt"], "2026-07-02T08:00")
        verification = self.app.verify_decision(self.decision_id)["verification"]
        # 未兑现服务以原决定的 cool-hill 为基准，而不是调整后已启用的 cool-hill-alt。
        self.assertEqual(verification["attribution"]["undelivered_actions"], ["cool-hill"])
        self.assertIn("cool-hill", verification["actual"]["cancelled_actions"])
        self.assertNotIn("cool-hill", verification["committed"]["activated"])
        view = self.app.show_decision(self.decision_id)
        self.assertEqual([event["event_type"] for event in view["events"]],
                         ["confirmed", "cancelled", "confirmed", "activated", "verified"])
        first_selected = view["events"][0]["payload"]["selected"]
        self.assertEqual(first_selected, ["cool-hill"])


if __name__ == "__main__":
    unittest.main()
