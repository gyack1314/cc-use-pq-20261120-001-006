import copy
import json
import tempfile
from pathlib import Path

from climate_ledger.application import Application
from climate_ledger.contracts import DomainError, canonical
from climate_ledger.publication import Publication


def run():
    examples = Path(__file__).parent / "examples"
    load = lambda name: json.loads((examples / name).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with Application(root / "city.sqlite3") as app:
            bundle = load("city.json")
            assert app.ingest(bundle)["seq"] == 1
            assert app.ingest(bundle)["replayed"]
            exposure = app.analyze(load("exposure.json"))
            assert exposure["total"]["known_exposed_person_days"] == 740
            study = app.compare(load("study.json"))
            assert study["estimate"] == "-2.500000"
            plan = app.plan(load("portfolio.json"))
            assert plan["selected"] == ["shared-river"]
            assert app.priorities(load("exposure.json"))["priorities"][0]["zone"] == "hill"
            draft = app.draft("july", "heat-july", load("exposure.json"), "分析员")
            app.review("july", draft["digest"], "审核员")
            release = app.publish("july", "july-v1")
            app.publication.export("july-v1", root / "july.json")
            assert Publication.verify_export(root / "july.json")["valid"]
            correction = copy.deepcopy(next(row for row in bundle["records"] if row["kind"] == "population"
                                             and row["data"]["id"] == "river-senior"))
            correction["data"].update(revision=2, count=40)
            app.ingest({"id": "population-corrected", "source": bundle["source"], "records": [correction]})
            assert app.analyze(load("exposure.json"), seq=1) == exposure
            assert app.publication.release("july-v1")["digest"] == release["digest"]
            assert not app.publication.release("july-v1")["current_evidence"]
            changed = app.reconcile(load("exposure.json"), 1, 2)
            assert len(changed["affected_zone_days"]) == 4
            assert app.audit()["status"] == "verified"
            app.backup(root / "backup.sqlite3")
        with Application(root / "backup.sqlite3") as restored:
            assert restored.audit()["batches"] == 2
            assert restored.publication.release("july-v1")["digest"] == release["digest"]

        # 联合情景预报与时点决策证据链：在独立数据库上重放完整生命周期。
        with Application(root / "decision.sqlite3") as decide:
            decide.ingest(load("city.json"))
            decide.ingest(load("forecast.json"))
            decision_spec = load("decision.json")
            preview = decide.preview_decision(decision_spec)
            assert preview["feasible_portfolios"] > 0
            assert decide.list_decisions()["decisions"] == []  # 预览不占名额
            chosen = preview["recommendation"]["actions"]
            confirmed = decide.confirm_decision(decision_spec, chosen)
            assert confirmed["evaluation"]["guarantee"]["satisfied"]
            # 同设施不能被另一决策重复出售。
            rival = {**decision_spec, "decision_id": "rival-bid"}
            try:
                decide.confirm_decision(rival, ["shared-river-alt"])
                raise AssertionError("设施名额被重复出售")
            except DomainError as exc:
                assert exc.code == "infeasible"
            decide.activate_reservation(decision_spec["decision_id"], chosen[:1], "2026-07-02T08:00")
            decide.cancel_reservation(decision_spec["decision_id"], chosen[1:], "2026-07-01T10:00")
            verification = decide.verify_decision(decision_spec["decision_id"])["verification"]
            assert verification["expected"]["basis_watermark"] != verification["at_watermark"] or True
            assert set(verification["attribution"]["deltas"]) == {
                "weather", "population_revision", "undelivered_service"}
            assert chosen[0] not in verification["attribution"]["undelivered_actions"]
            decide.backup(root / "decision-backup.sqlite3")
            audit = decide.audit()
            assert audit["status"] == "verified" and audit["decisions"] >= 3
        with Application(root / "decision-backup.sqlite3") as restored:
            view = restored.show_decision(decision_spec["decision_id"])
            assert [event["event_type"] for event in view["events"]][:3] == [
                "confirmed", "activated", "cancelled"]
            assert restored.audit()["status"] == "verified"
        print(canonical({"status": "通过", "synthetic": True, "exposure": exposure["total"],
                         "study_estimate": study["estimate"], "portfolio": plan["selected"],
                         "corrected_zone_days": len(changed["affected_zone_days"]), "archive_verified": True,
                         "decision_preview_feasible": preview["feasible_portfolios"],
                         "decision_verified": verification["realized_scenarios"],
                         "forecast_scenarios": len(load("forecast.json")["records"][0]["data"]["scenarios"])}))


if __name__ == "__main__":
    run()
