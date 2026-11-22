import json
import unittest

from c10_ast import call_index, caller_distances
from c10_budget import Signals
import c10_experiment as c10
from c11a_select import MODES, Ranking, capture_rank, choose
from score import evaluate_run
from selector import best_schedule


def make_ranking(count, required=0, evidence=None, scores=None, structural=False):
    return Ranking(list(range(count)), required, scores or dict.fromkeys(range(count), 10.0),
                   evidence or {}, Signals(0, 0, 1, 1, structural))


class C11SelectionTests(unittest.TestCase):
    def test_observer_records_rank_without_leaving_the_c10_hook_installed(self):
        original = c10.choose_budget

        class Experiment:
            def select(self, *_):
                order = [2, 0, 1]
                budget = c10.choose_budget("breadth", order, 1, {2: 9, 0: 8, 1: 2},
                                           {2: {"line"}}, Signals(1, 0, 1, 1, False), lambda _: 0)
                return order[:budget], (), []

        observed, selected, fallback, _ = capture_rank(Experiment(), "", None)
        self.assertEqual(observed.order, [2, 0, 1])
        self.assertEqual((selected, fallback), ([2, 0, 1], ()))
        self.assertIs(c10.choose_budget, original)

    def test_computed_dynamic_import_is_not_in_static_call_graph(self):
        graph = call_index({
            "sqlglot/demo.py": "def target():\n    return 1\n",
            "tests/test_demo.py": "import importlib\nfrom sqlglot.demo import target\n"
                                  "def test_static():\n    assert target()\n"
                                  "def test_dynamic():\n    path = '.'.join(('sqlglot', 'demo'))\n"
                                  "    assert importlib.import_module(path).target()\n",
        })
        callers = caller_distances(graph, {("sqlglot/demo.py", "target")})
        self.assertEqual(callers, {("tests/test_demo.py", "test_static"): 1})

    def test_all_policies_keep_mandatory_fixture_and_direct_tests(self):
        ranking = make_ranking(12, required=11)
        for mode in MODES:
            with self.subTest(mode=mode):
                chosen, _ = choose(mode, ranking, {}, lambda _: 0)
                self.assertTrue(set(range(11)).issubset(chosen))

    def test_score_cliff_preserves_tied_group(self):
        ranking = make_ranking(20, scores={n: 10 if n < 6 else 1 for n in range(20)})
        self.assertEqual(choose("P-cliff", ranking, {}, lambda _: 0)[0], list(range(6)))

    def test_substitution_protects_same_family_strong_and_mandatory(self):
        ranking = make_ranking(42, required=1,
                               evidence={1: {"line"}, 2: {"module"},
                                         40: {"module", "line"}, 41: {"module", "caller"}})
        tests = {n: {"id": f"tests/test_demo.py::Demo::test_{n}", "number": n, "seconds": .01}
                 for n in ranking.order}
        tests[0]["seconds"], tests[1]["seconds"], tests[2]["seconds"] = 5, 6, 15
        tests[40]["id"] = "tests/test_other.py::Other::test_40"

        def cost(numbers):
            return best_schedule([tests[n] for n in numbers])[1]

        chosen, swaps = choose("P-substitute", ranking, tests, cost)
        self.assertTrue({0, 1, 41}.issubset(chosen))
        self.assertNotIn(2, chosen)
        self.assertNotIn(40, chosen)
        self.assertEqual(len(swaps), 1)
        self.assertGreater(swaps[0]["schedule_saving_ms"], 0)

    def test_only_failing_test_at_rank_41_can_still_be_missed(self):
        ranking = make_ranking(45)
        tests = {n: {"id": f"tests/test_demo.py::Demo::test_{n}", "number": n, "seconds": .01}
                 for n in ranking.order}
        for mode in MODES:
            with self.subTest(mode=mode):
                chosen, _ = choose(mode, ranking, tests, lambda _: 0)
                output = json.dumps({"commit": "synthetic", "workers": [chosen]}).encode()
                result = evaluate_run(output, list(tests.values()), {"synthetic": {"failing": [40]}})
                self.assertTrue(result["ok"])
                self.assertEqual(result["missed_commits"], ["synthetic"])


if __name__ == "__main__":
    unittest.main()
