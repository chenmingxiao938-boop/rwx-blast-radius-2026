import unittest

from c10_budget import Signals
from c11a_select import Ranking
from c11b_policy import ordinary_comment_change, prune
from patch_parser import parse_patch
from selector import best_schedule


def change(before, after):
    return parse_patch("diff --git a/sqlglot/demo.py b/sqlglot/demo.py\n"
                       "--- a/sqlglot/demo.py\n+++ b/sqlglot/demo.py\n"
                       f"@@ -1 +1 @@\n-{before}\n+{after}\n")


class EvidencePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tests = {n: {"file": "tests/test_demo.py", "number": n,
                          "seconds": 10.0 if n == 0 else .1} for n in range(6)}
        self.scoped = {n: {("line", "sqlglot/demo.py", "9")} for n in self.tests}
        self.scoped[5].add(("symbol", "sqlglot/demo.py", "unique"))
        self.ranking = Ranking(list(self.tests), 0, {}, {0: {"line"}}, Signals(1, 0, 1, 1, False))

    def cost(self, numbers):
        return best_schedule([self.tests[n] for n in numbers])[1]

    def test_comment_needs_ast_equality_and_rejects_directives(self):
        self.assertFalse(ordinary_comment_change(change("# note", "# updated"), []))
        risks = ["same_ast_text_changed:sqlglot/demo.py"]
        self.assertTrue(ordinary_comment_change(change("# note", "# updated"), risks))
        self.assertFalse(ordinary_comment_change(change("# note", "# noqa: E501"), risks))
        self.assertFalse(ordinary_comment_change(change("# note", "# -*- coding: latin-1 -*-"), risks))

    def test_comment_prunes_expensive_duplicate_and_preserves_unique_token(self):
        selected, removed = prune(tuple(self.tests), {1}, self.ranking, self.scoped, self.tests,
                                  ["same_ast_text_changed:sqlglot/demo.py"], change("# note", "# edited"), self.cost)
        self.assertIn(0, removed)
        self.assertTrue({1, 5} <= set(selected))
        self.assertLess(self.cost(selected), self.cost(self.tests))

    def test_semantic_edit_preserves_strong_test(self):
        selected, _ = prune(tuple(self.tests), {1}, self.ranking, self.scoped, self.tests,
                            [], change("return 1", "return 2"), self.cost)
        self.assertTrue({0, 1, 5} <= set(selected))

    def test_last_test_module_witness_is_preserved(self):
        self.tests[0]["file"] = "tests/test_other.py"
        selected, removed = prune(tuple(self.tests), set(), self.ranking, self.scoped, self.tests,
                                  ["same_ast_text_changed:sqlglot/demo.py"], change("# note", "# edited"), self.cost)
        self.assertIn(0, selected)
        self.assertNotIn(0, removed)


if __name__ == "__main__":
    unittest.main()
