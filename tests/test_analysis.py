"""Counterexamples that would make naive mean/row-bootstrap gates unsafe."""
from copy import deepcopy
import math
import unittest

from evalanchor import contracts
from evalanchor.analysis import compare_runs, default_policy, _summarize


def runs(n=24, repeats=1, left=0, right=1, tags=None, independent=False, group_ids=None):
    cases = [{"id": f"case-{i}", "group_id": group_ids[i] if group_ids else f"group-{i}",
              "tags": tags or [], "messages": [{"role": "user", "content": f"Question {i}"}],
              "scorer": "exact", "expected": "correct"} for i in range(n)]
    sampling = {"kind": "declared_random_sample" if independent else "synthetic_contract_suite", "independent_groups": independent}
    suite = {"schema_version": 1, "id": "analysis-fixture", "cases": cases, "sampling": sampling}
    outputs = []
    for label, score in (("baseline", left), ("candidate", right)):
        records = []
        for i, case in enumerate(cases):
            for repeat in range(repeats):
                value = score[i] if isinstance(score, list) else score
                records.append({"case_id": case["id"], "group_id": case["group_id"], "tags": case["tags"],
                                "repeat": repeat, "input_hash": contracts.case_hash(case), "output": "correct" if value == 1 else "incorrect",
                                "status": "ok", "error": None, "score": value, "reason": "fixture",
                                "latency_ms": 10 + i, "cost_usd": None, "usage": {}})
        outputs.append({"schema_version": 1, "id": label, "created_at": "2026-10-29T00:00:00Z",
                        "status": "complete", "suite": {"id": suite["id"], "fingerprint": contracts.suite_hash(suite), "case_count": n},
                        "protocol": contracts.protocol(repeats), "target": {"id": label, "provider": "fixture"}, "suite_definition": deepcopy(suite),
                        "records": records, "provenance": {"sampling": deepcopy(sampling)}})
    return outputs


class AnalysisCounterexamples(unittest.TestCase):
    def policy(self, **kwargs):
        return {"bootstrap_samples": 100, **kwargs}

    def test_100_repeats_do_not_create_100_times_the_sample_size(self):
        once = compare_runs(*runs(n=12), self.policy())
        repeated = compare_runs(*runs(n=12, repeats=100), self.policy())
        self.assertEqual(repeated["overall"]["n_pairs"], 1200)
        self.assertEqual(repeated["overall"]["n_groups"], 12)
        self.assertAlmostEqual(once["overall"]["formal_interval"]["radius"], repeated["overall"]["formal_interval"]["radius"])
        self.assertAlmostEqual(once["overall"]["delta"], repeated["overall"]["delta"])

    def test_zero_difference_bootstrap_is_not_proof_of_equivalence(self):
        result = compare_runs(*runs(left=1, right=1, independent=True), self.policy(assume_independent_groups=True))
        self.assertEqual(result["overall"]["bootstrap_interval"]["lower"], 0)
        self.assertEqual(result["overall"]["bootstrap_interval"]["upper"], 0)
        self.assertLess(result["overall"]["formal_interval"]["lower"], -0.05)
        self.assertEqual(result["decision"], "inconclusive")

    def test_policy_assertion_does_not_override_non_iid_provenance(self):
        result = compare_runs(*runs(left=0, right=1), self.policy(assume_independent_groups=True))
        self.assertEqual(result["overall"]["conditional_decision"], "pass")
        self.assertFalse(result["assumptions"]["inference_enabled"])
        self.assertEqual(result["decision"], "inconclusive")

    def test_both_sampling_declarations_and_policy_are_required(self):
        b, c = runs(left=0, right=1, independent=True)
        self.assertEqual(compare_runs(b, c, self.policy())["decision"], "inconclusive")
        self.assertEqual(compare_runs(b, c, self.policy(assume_independent_groups=True))["decision"], "pass")
        c["provenance"]["sampling"]["independent_groups"] = False
        with self.assertRaises(contracts.ContractError):
            compare_runs(b, c, self.policy(assume_independent_groups=True))

    def test_conditional_statistical_failure(self):
        result = compare_runs(*runs(left=1, right=0, independent=True), self.policy(assume_independent_groups=True))
        self.assertEqual(result["decision"], "fail")
        self.assertEqual(result["decision_basis"], "conditional_population_inference")

    def test_group_weights_do_not_favor_many_easy_variants(self):
        b, c = runs(n=10, left=[0] * 9 + [1], right=[1] * 9 + [0], group_ids=["many-variants"] * 9 + ["one-hard-case"])
        result = compare_runs(b, c, self.policy())
        self.assertAlmostEqual(result["overall"]["delta"], 0)
        self.assertEqual(result["overall"]["n_groups"], 2)
        self.assertAlmostEqual(sum(p["delta"] for p in result["pairs"]) / 10, 0.8)

    def test_critical_contract_failure_without_population_assumption(self):
        result = compare_runs(*runs(n=2, left=1, right=0, tags=["critical"]), self.policy())
        self.assertEqual(result["decision"], "fail")
        self.assertEqual(result["decision_basis"], "finite_suite_acceptance_contract")
        self.assertEqual(len(result["quality"]["critical_regressions"]), 2)
        self.assertEqual(result["quality"]["critical_pairs"], 2)
        self.assertEqual(result["quality"]["critical_regression_cases"], 2)

    def test_failures_remain_in_denominator(self):
        b, c = runs(n=10)
        c["records"][0].update(status="error", error="provider timeout", score=0)
        result = compare_runs(b, c, self.policy())
        self.assertEqual(result["quality"]["candidate_error_rate"], 0.1)
        self.assertEqual(len(result["pairs"]), 10)
        self.assertEqual(result["decision"], "fail")

    def test_forged_score_cannot_hide_actual_critical_regression(self):
        b, c = runs(n=2, left=1, right=0, tags=["critical"])
        c["records"][0]["score"] = 1
        with self.assertRaises(contracts.ContractError):
            compare_runs(b, c, self.policy())

    def test_continuous_score_kernel_preserves_equal_group_weighting(self):
        # Isolate aggregation mathematics from the current binary scorers.
        pairs = [{"case_id": str(i), "group_id": str(i // 2), "baseline_score": .25,
                  "candidate_score": value} for i, value in enumerate([.5, .75, .25, .5])]
        policy = default_policy()
        policy["bootstrap_samples"] = 100
        result = _summarize(pairs, policy, .05)
        self.assertAlmostEqual(result["delta"], .25)
        self.assertEqual(result["n_groups"], 2)

    def test_error_degraded_baseline_cannot_certify_pass(self):
        b, c = runs(n=24, left=0, right=1, independent=True)
        b["records"][0].update(status="error", error="timeout", score=0)
        result = compare_runs(b, c, self.policy(assume_independent_groups=True))
        self.assertEqual(result["overall"]["conditional_decision"], "pass")
        self.assertEqual(result["decision"], "inconclusive")

    def test_unknown_cost_is_not_zero(self):
        b, c = runs(n=2)
        c["records"][0]["cost_usd"] = 0.02
        result = compare_runs(b, c, self.policy())
        cost = result["resources"]["candidate"]["cost"]
        self.assertIsNone(cost["total_usd"])
        self.assertEqual(cost["known_subtotal_usd"], 0.02)
        self.assertEqual(cost["unknown_records"], 1)

    def test_missing_required_slice_blocks_pass(self):
        result = compare_runs(*runs(left=0, right=1, independent=True), self.policy(assume_independent_groups=True, required_tags=["missing"]))
        self.assertEqual(result["decision"], "inconclusive")
        self.assertEqual(result["slices"][0]["n_groups"], 0)

    def test_all_displayed_slices_in_bonferroni_family(self):
        result = compare_runs(*runs(tags=["a", "b"]), self.policy())
        self.assertEqual(result["assumptions"]["family_size"], 3)
        self.assertAlmostEqual(result["overall"]["formal_interval"]["alpha"], 0.05 / 3)
        expected = math.sqrt(2 * math.log(2 / (0.05 / 3)) / 24)
        self.assertAlmostEqual(result["overall"]["formal_interval"]["radius"], expected)

    def test_reproducible_bootstrap_and_untouched_inputs(self):
        b, c = runs(n=16, left=[i % 2 for i in range(16)], right=[(i // 2) % 2 for i in range(16)])
        before = deepcopy((b, c))
        result = compare_runs(b, c, self.policy())
        self.assertEqual(result, compare_runs(b, c, self.policy()))
        self.assertEqual((b, c), before)
        self.assertEqual(result["pairs"][0]["baseline_output"], b["records"][0]["output"])

    def test_missing_duplicate_hash_and_protocol_mismatch_rejected(self):
        for mutation in ("missing", "duplicate", "hash", "protocol", "group", "tags"):
            with self.subTest(mutation=mutation):
                b, c = runs(n=3)
                if mutation == "missing":
                    c["records"].pop()
                elif mutation == "duplicate":
                    c["records"].append(deepcopy(c["records"][0]))
                elif mutation == "hash":
                    c["records"][0]["input_hash"] = "changed-input"
                elif mutation == "protocol":
                    c["protocol"]["fingerprint"] = "changed-scorer"
                elif mutation == "group":
                    c["records"][0]["group_id"] = "changed-group"
                else:
                    c["records"][0]["tags"] = ["new-tag"]
                with self.assertRaises(contracts.ContractError):
                    compare_runs(b, c, self.policy())

    def test_invalid_policy_and_scores_rejected(self):
        for patch in ({"alpha": 0}, {"margin": float("nan")}, {"assume_independent_groups": 1},
                      {"bootstrap_samples": 0}, {"min_groups": True}, {"typo_margin": .1},
                      {"required_tags": ["a", "a"]}):
            with self.subTest(patch=patch), self.assertRaises(contracts.ContractError):
                compare_runs(*runs(n=2), self.policy(**patch))
        b, c = runs(n=2)
        c["records"][0]["score"] = float("nan")
        with self.assertRaises(contracts.ContractError):
            compare_runs(b, c, self.policy())

    def test_default_policy_is_not_shared_mutable_state(self):
        a = default_policy()
        a["required_tags"].append("x")
        self.assertEqual(default_policy()["required_tags"], [])


if __name__ == "__main__":
    unittest.main()
