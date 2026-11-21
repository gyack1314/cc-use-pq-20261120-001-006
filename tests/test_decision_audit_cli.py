import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from climate_ledger.__main__ import main
from climate_ledger.application import Application
from climate_ledger.contracts import DomainError


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def example(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class DecisionAuditCliTests(unittest.TestCase):
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

    def build_lifecycle(self):
        self.app.confirm_decision(self.spec, ["cool-hill"])
        self.app.activate_reservation(self.decision_id, ["cool-hill"], "2026-07-02T08:00")
        self.app.verify_decision(self.decision_id)

    def test_audit_covers_decision_event_chain(self):
        self.build_lifecycle()
        audit = self.app.audit()
        self.assertEqual(audit["status"], "verified")
        self.assertGreaterEqual(audit["decisions"], 3)

    def test_backup_restore_preserves_decisions_and_verification(self):
        self.build_lifecycle()
        before = self.app.show_decision(self.decision_id)
        backup = Path(self.temp.name) / "backup.sqlite3"
        self.app.backup(backup)
        with Application(backup) as restored:
            self.assertEqual(restored.audit()["status"], "verified")
            after = restored.show_decision(self.decision_id)
            self.assertEqual([e["digest"] for e in after["events"]],
                             [e["digest"] for e in before["events"]])
            self.assertEqual(restored.verify_decision(self.decision_id)["verification"]["realized_scenarios"],
                             self.app.verify_decision(self.decision_id)["verification"]["realized_scenarios"])

    def test_cli_preview_confirm_verify_round_trip(self):
        spec_path = EXAMPLES / "decision.json"
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--database", str(self.database), "decision-preview", str(spec_path)])
        self.assertEqual(status, 0)
        preview = json.loads(output.getvalue())
        self.assertTrue(preview["recommendation"])

        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--database", str(self.database), "decision-confirm", str(spec_path),
                           "--select", "cool-hill"])
        self.assertEqual(status, 0)
        confirmed = json.loads(output.getvalue())
        self.assertEqual(sorted(confirmed["state"]["active_reservations"]), ["cool-hill"])

        output = io.StringIO()
        with redirect_stdout(output):
            main(["--database", str(self.database), "decision-activate", self.decision_id,
                  "--actions", "cool-hill", "--at", "2026-07-02T08:00"])
            main(["--database", str(self.database), "decision-verify", self.decision_id])
        lines = [line for line in output.getvalue().splitlines() if line]
        verification = json.loads(lines[-1])["verification"]
        self.assertIn("attribution", verification)

        output = io.StringIO()
        with redirect_stdout(output):
            main(["--database", str(self.database), "decision-list"])
        self.assertIn(self.decision_id, output.getvalue())

    def test_cli_domain_failure_returns_exit_code_two(self):
        from contextlib import redirect_stderr
        # 先确认一个占用 shared-plaza 的行动，再经 CLI 用另一决策确认同设施的互斥行动。
        self.app.confirm_decision(self.spec, ["cool-hill"])
        rival = {**self.spec, "decision_id": "rival-bid"}
        rival_path = Path(self.temp.name) / "rival.json"
        rival_path.write_text(json.dumps(rival), encoding="utf-8")
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            status = main(["--database", str(self.database), "decision-confirm", str(rival_path),
                           "--select", "shared-river-alt"])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(buffer.getvalue())["error"], "infeasible")


if __name__ == "__main__":
    unittest.main()
