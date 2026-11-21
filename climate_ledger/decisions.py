import json

from .contracts import (canonical, fingerprint, identifier, moment, require)
from .decision import DecisionEngine, decision_spec


class DecisionStore:
    """决策事件溯源。已确认组合不可改写，只能追加取消/调整/核验事件。"""

    def __init__(self, ledger):
        self.ledger = ledger
        self.db = ledger.connection

    def append_event(self, decision_id, event_type, payload):
        require(self.db.in_transaction, "transaction", "决策变更需要写事务")
        last = self.db.execute(
            "SELECT digest FROM decision_events WHERE decision_id=? ORDER BY seq DESC LIMIT 1",
            (decision_id,)).fetchone()
        prev_digest = last["digest"] if last else None
        record = {"decision_id": decision_id, "event_type": event_type,
                  "ledger_seq": self.ledger.watermark(), "prev_digest": prev_digest, "payload": payload}
        digest = fingerprint(record)
        self.db.execute("INSERT INTO decision_events(decision_id,event_type,ledger_seq,payload,digest,prev_digest)"
                        " VALUES(?,?,?,?,?,?)",
                        (decision_id, event_type, record["ledger_seq"], canonical(payload), digest, prev_digest))
        return {"seq": self.db.execute("SELECT MAX(seq) s FROM decision_events").fetchone()["s"],
                "digest": digest}

    def events(self, decision_id):
        rows = self.db.execute("SELECT * FROM decision_events WHERE decision_id=? ORDER BY seq",
                               (decision_id,)).fetchall()
        require(rows, "decision", "决策不存在", decision=decision_id)
        result = []
        previous = None
        for row in rows:
            payload = json.loads(row["payload"])
            require(row["prev_digest"] == previous, "decision_chain", "决策事件链断裂")
            record = {"decision_id": row["decision_id"], "event_type": row["event_type"],
                      "ledger_seq": row["ledger_seq"], "prev_digest": row["prev_digest"], "payload": payload}
            require(fingerprint(record) == row["digest"], "decision_chain", "决策事件摘要不一致")
            previous = row["digest"]
            result.append({**record, "event_seq": row["seq"], "digest": row["digest"]})
        return result

    def list_decisions(self):
        rows = self.db.execute(
            "SELECT decision_id, MIN(seq) first_seq FROM decision_events"
            " GROUP BY decision_id ORDER BY first_seq").fetchall()
        return [row["decision_id"] for row in rows]

    @staticmethod
    def replay(events):
        """把事件流折叠为当前承诺状态与费用台账。"""
        state = {"created": None, "active_reservations": {}, "cancelled": {}, "activated": set(),
                 "fee_ledger": {"reservation_paid": 0, "reservation_refunded": 0,
                                "late_cancel_paid": 0, "activation_paid": 0},
                 "verifications": [], "history": []}
        for event in events:
            kind, payload = event["event_type"], event["payload"]
            state["history"].append({"event_seq": event["event_seq"], "event_type": kind,
                                     "at": payload.get("at"), "digest": event["digest"]})
            if kind == "confirmed":
                # 调整会在取消旧预留后追加新的确认；任一时刻有效预留由取消事件维护。
                state["created"] = payload
                for action in payload["selected"]:
                    require(action not in state["active_reservations"], "decision",
                            "新确认不能重复预留仍有效的行动", action=action)
                    state["active_reservations"][action] = {"fee_cents": payload["reserve_fee"][action],
                                                             "confirmed_at": payload["at"]}
                state["fee_ledger"]["reservation_paid"] += sum(payload["reserve_fee"].values())
            elif kind == "cancelled":
                for action in payload["actions"]:
                    require(action in state["active_reservations"], "decision", "取消的行动不在有效预留中",
                            action=action)
                    paid = state["active_reservations"].pop(action)["fee_cents"]
                    state["cancelled"][action] = {"at": payload["at"], "fee_cents": paid,
                                                  "refund_cents": payload["refund_fee"].get(action, 0),
                                                  "late_fee_cents": payload["late_fee"].get(action, 0)}
                state["fee_ledger"]["reservation_refunded"] += sum(payload["refund_fee"].values())
                state["fee_ledger"]["late_cancel_paid"] += sum(payload["late_fee"].values())
            elif kind == "activated":
                for action in payload["actions"]:
                    require(action in state["active_reservations"], "decision", "启用的行动尚未预留", action=action)
                    require(action not in state["activated"], "decision", "行动不能重复启用", action=action)
                    state["activated"].add(action)
                state["fee_ledger"]["activation_paid"] += sum(payload["activation_fee"].values())
            elif kind == "verified":
                state["verifications"].append(payload)
            else:
                require(False, "decision", "未知决策事件类型")
        return state

    def state(self, decision_id):
        return self.replay(self.events(decision_id))

    def occupied_actions(self, *, exclude_decision=None):
        """跨全部决策仍在占用设施名额的行动（已取消或结束的预留不再占用）。"""
        occupied = set()
        for decision_id in self.list_decisions():
            if decision_id == exclude_decision:
                continue
            state = self.state(decision_id)
            occupied.update(state["active_reservations"])
        return occupied

    def spent_cents(self, decision_id):
        state = self.state(decision_id)
        ledger = state["fee_ledger"]
        return (ledger["reservation_paid"] - ledger["reservation_refunded"]
                + ledger["late_cancel_paid"] + ledger["activation_paid"])

    @staticmethod
    def serializable(state):
        return {**state, "activated": sorted(state["activated"])}


class DecisionService:
    def __init__(self, ledger):
        self.ledger = ledger
        self.store = DecisionStore(ledger)

    def preview(self, spec):
        with self.ledger.transaction():
            normalized = decision_spec(spec)
            snapshot = self.ledger.snapshot()
            engine = DecisionEngine(snapshot)
            occupied = self.store.occupied_actions(exclude_decision=normalized["decision_id"])
            result = engine.preview(normalized, occupied=occupied)
            result["watermark"] = self.ledger.watermark()
            return result

    def confirm(self, spec, selected):
        with self.ledger.transaction(write=True):
            normalized = decision_spec(spec)
            existing = self.store.db.execute(
                "SELECT 1 FROM decision_events WHERE decision_id=? AND event_type='confirmed' LIMIT 1",
                (normalized["decision_id"],)).fetchone()
            require(existing is None, "decision", "决策已确认，不能覆盖原决定")
            snapshot = self.ledger.snapshot()
            engine = DecisionEngine(snapshot)
            occupied = self.store.occupied_actions(exclude_decision=normalized["decision_id"])
            selected = sorted(identifier(item, "action") for item in selected)
            known = {item["action"] for item in normalized["proposals"]}
            require(set(selected).issubset(known), "action", "确认组合含未知候选行动")
            reserve_fee = {item["action"]: item["reserve_fee_cents"] for item in normalized["proposals"]}
            evaluation = engine.evaluate(normalized, selected, reserve_fee=reserve_fee, occupied=occupied)
            require(evaluation["feasible"], "infeasible", "确认组合不满足保障或资源约束",
                    guarantee_gap=evaluation["infeasible_reasons"] + evaluation["guarantee"]["violations"])
            terms = {item["action"]: item for item in normalized["proposals"]}
            payload = {"at": normalized["decision_at"], "forecast": normalized["forecast"],
                       "published_at": engine.forecasts.version(normalized["forecast"])["published_at"],
                       "spec": normalized, "selected": selected,
                       "reserve_fee": {action: terms[action]["reserve_fee_cents"] for action in selected},
                       "budget_cents": normalized["budget_cents"],
                       "evaluation": evaluation, "watermark": self.ledger.watermark()}
            event = self.store.append_event(normalized["decision_id"], "confirmed", payload)
            return {"decision_id": normalized["decision_id"], "event": event,
                    "state": self.store.serializable(self.store.state(normalized["decision_id"])), "evaluation": evaluation}

    def cancel(self, decision_id, actions, at):
        with self.ledger.transaction(write=True):
            moment(at, "at")
            state = self.store.state(decision_id)
            created = state["created"]
            terms = {item["action"]: item for item in created["spec"]["proposals"]}
            actions = sorted(identifier(item, "action") for item in actions)
            require(actions, "decision", "取消行动列表不能为空")
            refund_fee, late_fee = {}, {}
            for action in actions:
                require(action not in state["activated"], "decision", "已启用行动不能按预留取消", action=action)
                deadline = terms[action]["cancel_deadline"]
                if at <= deadline:
                    refund_fee[action] = state["active_reservations"][action]["fee_cents"]
                    late_fee[action] = 0
                else:
                    refund_fee[action] = 0
                    late_fee[action] = terms[action]["late_cancel_fee_cents"]
            new_spend = self.store.spent_cents(decision_id) + sum(late_fee.values()) - sum(refund_fee.values())
            require(new_spend <= created["budget_cents"], "over_budget", "逾期取消费用超出决策预算")
            payload = {"at": at, "actions": actions, "refund_fee": refund_fee, "late_fee": late_fee,
                       "watermark": self.ledger.watermark()}
            event = self.store.append_event(decision_id, "cancelled", payload)
            return {"decision_id": decision_id, "event": event, "state": self.store.serializable(self.store.state(decision_id))}

    def activate(self, decision_id, actions, at):
        with self.ledger.transaction(write=True):
            moment(at, "at")
            state = self.store.state(decision_id)
            created = state["created"]
            terms = {item["action"]: item for item in created["spec"]["proposals"]}
            actions = sorted(identifier(item, "action") for item in actions)
            require(actions, "decision", "启用行动列表不能为空")
            activation_fee = {}
            for action in actions:
                require(action in state["active_reservations"], "decision", "启用的行动尚未预留", action=action)
                activation_fee[action] = terms[action]["activation_fee_cents"]
            new_spend = self.store.spent_cents(decision_id) + sum(activation_fee.values())
            require(new_spend <= created["budget_cents"], "over_budget", "启用费用超出决策预算")
            payload = {"at": at, "actions": actions, "activation_fee": activation_fee,
                       "watermark": self.ledger.watermark()}
            event = self.store.append_event(decision_id, "activated", payload)
            return {"decision_id": decision_id, "event": event, "state": self.store.serializable(self.store.state(decision_id))}

    def adjust(self, decision_id, new_spec, selected, at):
        """显式确认的调整：先取消原有效预留（保留原决定与成本），再确认新组合。"""
        with self.ledger.transaction(write=True):
            moment(at, "at")
            old_state = self.store.state(decision_id)
            require(old_state["created"] is not None, "decision", "只能调整已确认决策")
            old_actions = sorted(old_state["active_reservations"])
            old_terms = {item["action"]: item for item in old_state["created"]["spec"]["proposals"]}
            refund_fee, late_fee = {}, {}
            for action in old_actions:
                if at <= old_terms[action]["cancel_deadline"]:
                    refund_fee[action] = old_state["active_reservations"][action]["fee_cents"]
                else:
                    late_fee[action] = old_terms[action]["late_cancel_fee_cents"]
            spent = self.store.spent_cents(decision_id)
            if old_actions:
                projected_spend = spent + sum(late_fee.values()) - sum(refund_fee.values())
                require(projected_spend <= old_state["created"]["budget_cents"], "over_budget",
                        "调整产生的逾期取消费用超出决策预算", spent_cents=projected_spend)
                self.store.append_event(decision_id, "cancelled",
                                        {"at": at, "actions": old_actions, "refund_fee": refund_fee,
                                         "late_fee": late_fee, "watermark": self.ledger.watermark()})
            normalized = decision_spec(new_spec)
            require(normalized["decision_id"] == decision_id, "decision", "调整不能改变决策编号")
            require(normalized["decision_at"] >= at, "decision", "调整时刻不能早于取消时刻")
            snapshot = self.ledger.snapshot()
            engine = DecisionEngine(snapshot)
            occupied = self.store.occupied_actions(exclude_decision=decision_id)
            selected = sorted(identifier(item, "action") for item in selected)
            reserve_fee = {item["action"]: item["reserve_fee_cents"] for item in normalized["proposals"]}
            evaluation = engine.evaluate(normalized, selected, reserve_fee=reserve_fee, occupied=occupied)
            require(evaluation["feasible"], "infeasible", "调整后组合不满足保障或资源约束",
                    guarantee_gap=evaluation["infeasible_reasons"] + evaluation["guarantee"]["violations"])
            spent = self.store.spent_cents(decision_id)
            new_reserve = sum(reserve_fee[a] for a in selected)
            require(spent + new_reserve <= normalized["budget_cents"], "over_budget",
                    "调整后的预留费用超出决策预算", spent_cents=spent)
            terms = {item["action"]: item for item in normalized["proposals"]}
            payload = {"at": at, "forecast": normalized["forecast"],
                       "published_at": engine.forecasts.version(normalized["forecast"])["published_at"],
                       "spec": normalized, "selected": selected,
                       "reserve_fee": {action: terms[action]["reserve_fee_cents"] for action in selected},
                       "budget_cents": normalized["budget_cents"],
                       "evaluation": evaluation, "watermark": self.ledger.watermark()}
            self.store.append_event(decision_id, "confirmed", payload)
            return {"decision_id": decision_id, "state": self.store.serializable(self.store.state(decision_id)),
                    "evaluation": evaluation}

    def decision(self, decision_id):
        with self.ledger.transaction():
            events = self.store.events(decision_id)
            state = self.store.replay(events)
            return {"decision_id": decision_id, "events": events,
                    "active_reservations": sorted(state["active_reservations"]),
                    "activated": sorted(state["activated"]),
                    "cancelled": state["cancelled"], "fee_ledger": state["fee_ledger"],
                    "spent_cents": self.store.spent_cents(decision_id),
                    "verifications": state["verifications"], "timeline": state["history"]}

