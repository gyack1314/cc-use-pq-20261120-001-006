import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from climate_ledger.application import Application
from climate_ledger.contracts import DomainError


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def forecast_record(bundle):
    return next(item for item in bundle["records"] if item["kind"] == "forecast")["data"]


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(Path(self.temp.name) / "city.sqlite3")
        self.addCleanup(self.app.ledger.close)
        self.app.ingest(example("city.json"))
        self.forecast_bundle = example("forecast.json")

    def ingest_forecast(self, **changes):
        bundle = copy.deepcopy(self.forecast_bundle)
        record = forecast_record(bundle)
        record.update(changes)
        return self.app.ingest(bundle)

    def assert_error(self, code, call):
        with self.assertRaises(DomainError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def test_joint_scenarios_ingest_and_probabilities_sum_to_one(self):
        result = self.ingest_forecast()
        self.assertEqual(result["seq"], 2)
        snap = self.app.ledger.snapshot()
        forecast = snap["forecast"]["fc-heat-v1"]
        self.assertEqual([s["id"] for s in forecast["scenarios"]], ["S1-river-hot", "S2-hill-hot"])
        self.assertEqual(sum(Decimal(s["probability"]) for s in forecast["scenarios"]), Decimal("1"))

    def test_probabilities_not_summing_to_one_rejected(self):
        bundle = copy.deepcopy(self.forecast_bundle)
        forecast_record(bundle)["scenarios"][0]["probability"] = "0.7"
        self.assert_error("probability", lambda: self.app.ingest(bundle))
        self.assertEqual(self.app.ledger.snapshot()["forecast"], {})

    def test_scenario_grid_must_be_complete(self):
        bundle = copy.deepcopy(self.forecast_bundle)
        forecast_record(bundle)["scenarios"][0]["readings"].pop()
        self.assert_error("scenario_gap", lambda: self.app.ingest(bundle))

    def test_forecast_cannot_split_and_recombine_zones_into_new_scenario(self):
        # 来源只给过 S1/S2 两个联合情景，引擎只能在这两个情景上评估，不能拼出 S1 河畔 + S2 山麓。
        self.ingest_forecast()
        preview = self.app.preview_decision(example("decision.json"))
        scenarios = {s["scenario"] for p in preview["portfolios"] for s in p["scenario_results"]}
        self.assertEqual(scenarios, {"S1-river-hot", "S2-hill-hot"})

    def test_forecast_reading_out_of_metric_range_rejected(self):
        bundle = copy.deepcopy(self.forecast_bundle)
        forecast_record(bundle)["scenarios"][0]["readings"][0]["value"] = "200"
        self.assert_error("forecast_range", lambda: self.app.ingest(bundle))

    def test_forecast_does_not_fill_observation_gap(self):
        self.ingest_forecast()
        # 删掉一个真实观测；暴露分析仍须未知，预报不能补成确定区日。
        city = example("city.json")
        reading = next(copy.deepcopy(r) for r in city["records"]
                       if r["kind"] == "reading" and r["data"]["station"] == "river-s"
                       and r["data"]["day"] == "2026-07-03")
        reading["data"].update(revision=2, quality="missing", value=None)
        self.app.ingest({"id": "gap", "source": city["source"], "records": [reading]})
        exposure = self.app.analyze(example("exposure.json"))
        self.assertEqual(exposure["status"], "incomplete")
        target = next(r for r in exposure["daily"] if r["zone"] == "river" and r["day"] == "2026-07-03")
        self.assertIsNone(target["exposed"])

    def test_later_published_forecast_cannot_flow_back_into_earlier_decision(self):
        self.ingest_forecast()
        later = copy.deepcopy(self.forecast_bundle)
        later["id"] = "forecast-v2-batch"
        record = forecast_record(later)
        record.update(id="fc-heat-v2", revision=1, published_at="2026-06-27T09:00")
        self.app.ingest(later)
        spec = example("decision.json")
        # 决策时点 2026-06-26 不能引用 06-27 才发布的 v2。
        self.assert_error("forecast_timing",
                          lambda: self.app.preview_decision({**spec, "forecast": "fc-heat-v2"}))
        # 把决策时点移到发布之后即可见。
        later_spec = {**spec, "decision_at": "2026-06-27T10:00", "forecast": "fc-heat-v2"}
        self.assertEqual(self.app.preview_decision(later_spec)["forecast"], "fc-heat-v2")

    def test_realize_matches_within_subset_grid_when_forecast_is_wider(self):
        from climate_ledger.forecast import Forecasts
        self.ingest_forecast()
        forecasts = Forecasts(self.app.ledger.snapshot())
        # 只用查询窗口（预报整版的一个子集）的观测，也应能在该网格上识别，而不误报缺测。
        sub_keys = {("river", "2026-07-03"), ("hill", "2026-07-03")}
        observed = {("river", "2026-07-03"): Decimal("35"), ("hill", "2026-07-03"): Decimal("28")}
        self.assertEqual(forecasts.realize("fc-heat-v1", observed, keys=sub_keys), ["S1-river-hot"])
        # 缺一个待匹配区日仍须拒绝，不能借预报补值。
        with self.assertRaises(DomainError) as error:
            forecasts.realize("fc-heat-v1", {("river", "2026-07-03"): Decimal("35")}, keys=sub_keys)
        self.assertEqual(error.exception.code, "observation_gap")

    def test_forecast_revision_does_not_rewrite_confirmed_decision(self):
        self.ingest_forecast()
        spec = example("decision.json")
        selected = self.app.preview_decision(spec)["recommendation"]["actions"]
        confirmed = self.app.confirm_decision(spec, selected)
        bound_forecast = confirmed["evaluation"]["scenario_results"]
        # 同来源发布修订版预报。
        revised = copy.deepcopy(self.forecast_bundle)
        revised["id"] = "forecast-revision-batch"
        record = forecast_record(revised)
        record.update(revision=2, published_at="2026-06-27T09:00")
        record["scenarios"][0]["readings"][0]["value"] = "40"
        self.app.ingest(revised)
        view = self.app.show_decision(spec["decision_id"])
        first_confirm = next(e for e in view["events"] if e["event_type"] == "confirmed")
        self.assertEqual(first_confirm["payload"]["evaluation"]["scenario_results"], bound_forecast)
        self.assertEqual(first_confirm["payload"]["forecast"], "fc-heat-v1")


if __name__ == "__main__":
    unittest.main()
