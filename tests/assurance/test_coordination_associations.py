"""The models are told which control relates to which KPI, and nothing more.

Owner instruction 2026-09-19: "예측기는 어떤 조정이랑 어떤 KPI 가 연관이 있는지만
말하게 해. 증가·상승도 말하지 말라고 해."  The predictor's per-configuration
table had the models follow its ranking instead of reasoning.
"""
import json
import re
import unittest

from assurance.coordination import (
    FunctionCatalog, control_kpi_associations, model_effect_evidence)
from assurance.coordination.agents import SYSTEM_PROMPTS, _user_prompt

A, B = "12345678", "87654321"
CATALOG = FunctionCatalog.from_record([
    {"functionId": "steer", "xapp": "ts", "actionId": "AIC_UECellSteering_1.0.0",
     "scopes": ["ue@ue1", "ue@ue2"],
     "policyFields": {"servingCell": {"values": [A, B], "unit": "nci"}},
     "axis": "servingCell@<ue>"},
    {"functionId": "ue-sched-priority", "xapp": "rc", "actionId": "103",
     "scopes": ["ue@ue2"],
     "policyFields": {"pfWeight": {"values": ["1.0", "4.0"], "baseline": "1.0"}},
     "axis": "pfWeight@<ue>"},
    {"functionId": "cell-tx-attenuation", "xapp": "rc", "actionId": "104",
     "scopes": [f"cell@{A}"],
     "policyFields": {"txAttenuationDb": {"values": ["0.0", "3.0"], "baseline": "0.0"}},
     "axis": "txAttenuationDb@<cell>"},
])
STATE = {"ues": {"ue1": {"servingCell": B}, "ue2": {"servingCell": A},
                 "ue3": {"servingCell": A}},
         "cells": {A: {"capacityMbps": 14.5}, B: {"capacityMbps": 14.5}}}

#: Words that would say how a KPI moves, how much, or whether that is good.
DIRECTION = re.compile(r"\b(increase|decrease|rise|raise|higher|lower|improv|degrad|"
                       r"gain|loss|better|worse|up|down|benefit|harm)", re.I)


def row(rows, function_id, scope):
    return next(r for r in rows if r["functionId"] == function_id and r["scope"] == scope)


class TheAssociationsFollowThePlacement(unittest.TestCase):
    def setUp(self):
        self.rows = control_kpi_associations(CATALOG, STATE)

    def test_a_ue_scoped_priority_names_the_ues_sharing_its_cell(self):
        kpis = row(self.rows, "ue-sched-priority", "ue@ue2")["associatedKpis"]
        self.assertEqual(sorted(kpis), sorted(
            f"{k}@{ue}" for ue in ("ue2", "ue3")
            for k in ("dlGoodputMbps", "deadlineSuccessRatio")))

    def test_steering_names_its_serving_cell_and_both_cells_ues(self):
        kpis = row(self.rows, "steer", "ue@ue1")["associatedKpis"]
        self.assertIn("servingCell@ue1", kpis)
        for ue in ("ue1", "ue2", "ue3"):
            self.assertIn(f"dlGoodputMbps@{ue}", kpis)

    def test_a_cell_attenuation_names_its_cell_and_the_neighbours(self):
        kpis = row(self.rows, "cell-tx-attenuation", f"cell@{A}")["associatedKpis"]
        self.assertIn("dlGoodputMbps@ue1", kpis)       # the neighbour cell's UE
        self.assertIn("deadlineSuccessRatio@ue2", kpis)


class NoModelIsGivenAPrediction(unittest.TestCase):
    """2026-09-20 (owner): associations are Control's to make, not the code's,
    and nobody estimates values.  The code-built table stays a helper only."""

    def test_the_evidence_carries_only_measurements(self):
        evidence = model_effect_evidence(CATALOG, STATE)
        self.assertEqual({"observations"}, set(evidence))

    def test_the_prompt_drops_the_predictor_and_the_code_built_associations(self):
        payload = {
            "input.effect_evidence": {
                "predictions": [{"configuration": {"pfWeight@ue2": "4.0"},
                                 "kpis": {"dlGoodputMbps@ue2": 6.1}}],
                "predictorDescription": {"cellCapacityMbps": 14.5, "snrDb": 20},
                "uncertaintyNote": "relative uncertainty",
                "controlKpiAssociations": control_kpi_associations(CATALOG, STATE),
                "note": "each function/scope is listed with the KPIs"},
            "input.network_state": STATE,
            "input.control_candidates": [
                {"controlId": "C1", "evidenceRefs": ["predictor", "prediction:x"]}]}
        prompt = _user_prompt(payload, {})
        for gone in ("predictions", "predictorDescription", "uncertaintyNote",
                     "capacityMbps", '"predictor"', "14.5", "controlKpiAssociations"):
            self.assertNotIn(gone, prompt)

    def test_no_system_prompt_names_a_predictor(self):
        for role, text in SYSTEM_PROMPTS.items():
            with self.subTest(role=role):
                self.assertNotRegex(text, re.compile(r"predictor|prediction", re.I))


class ControlStatesRelationsNotValues(unittest.TestCase):
    """2026-09-20 (owner): "control 이 연관만 만들게 하고 값 추정은 하지 말게 하라"."""

    def test_the_control_schema_asks_for_related_kpis_and_no_values(self):
        from assurance.coordination.agents import _CONTROL_SCHEMA, _MONOLITH_FORM_SCHEMA
        for schema in (_CONTROL_SCHEMA, _MONOLITH_FORM_SCHEMA):
            row = schema["candidates"][0]
            self.assertIn("relatedKpis", row)
            for gone in ("predicted", "uncertainty", "predictedTarget"):
                self.assertNotIn(gone, row)

    def test_values_a_model_sends_anyway_are_dropped_not_refused(self):
        from assurance.coordination.agents import _controls_from_answer
        answer = {"candidates": [{
            "controlId": "CA",
            "functions": [{"functionId": "ue-sched-priority", "scope": "ue@ue2",
                           "policy": {"pfWeight": "4.0"}}],
            "relatedKpis": ["dlGoodputMbps@ue2", "dlGoodputMbps@ue3"],
            "predicted": {"I1.r1": 5.2}, "uncertainty": {"I1.r1": 0.3},
            "predictedTarget": "T0"}]}
        candidate = _controls_from_answer(answer, None, "m").candidates[0]
        self.assertEqual(("dlGoodputMbps@ue2", "dlGoodputMbps@ue3"), candidate.related_kpis)
        self.assertEqual({}, candidate.predicted)
        self.assertEqual("", candidate.predicted_target)
        record = candidate.to_record()
        self.assertNotIn("predicted", record)
        self.assertEqual(["dlGoodputMbps@ue2", "dlGoodputMbps@ue3"], record["relatedKpis"])

    def test_a_selector_sees_functions_and_related_kpis_only(self):
        from assurance.coordination import ControlCandidate, ControlCandidates, TrajectoryInputs
        c0 = ControlCandidate(control_id="C0", configuration={"pfWeight@ue2": "1.0"})
        c1 = ControlCandidate(
            control_id="C1", configuration={"pfWeight@ue2": "4.0"},
            functions=({"functionId": "ue-sched-priority", "scope": "ue@ue2",
                        "policy": {"pfWeight": "4.0"}},),
            related_kpis=("dlGoodputMbps@ue2",), predicted={"I1.r1": 5.2},
            predicted_target="T0", applicability=("ue2 attached",), rationale="x")
        block = TrajectoryInputs(control_candidates=ControlCandidates(
            candidates=(c0, c1), action_space={"pfWeight@ue2": ("1.0", "4.0")})).payload()[
            "input.control_candidates"]
        self.assertEqual({"controlId", "functions", "relatedKpis", "rationale"}, set(block[1]))
        self.assertNotIn("predicted", json.dumps(block))


if __name__ == "__main__":
    unittest.main()
