import unittest
from unittest.mock import patch

from c12_a_main import _should_select_zero
from patch_parser import parse_patch
from test_c12_noop import PATH, diff


class FastPathTests(unittest.TestCase):
    old = "# Old remark\nx = 1\n"
    comment = "# New remark\nx = 1\n"

    def decide(self, raw, mandatory=(), reasons=(), test_sources=None):
        return _should_select_zero(parse_patch(raw), raw, mandatory, reasons, {PATH: self.old},
                                   test_sources or {}, {})

    def test_mandatory_and_fallback_skip_assessment(self):
        raw = diff(self.old, self.comment)
        with patch("c12_a_main.assess_patch") as assess:
            self.assertFalse(self.decide(raw, mandatory={1}))
            self.assertFalse(self.decide(raw, reasons=("unsupported_path",)))
            assess.assert_not_called()

    def test_unqualified_changes_skip_assessment(self):
        raw_comment = diff(self.old, self.comment)
        with patch("c12_a_main.assess_patch") as assess:
            for raw in (diff(self.old, self.old.replace("1", "2")),
                        diff(self.old, self.comment, "tests/test_demo.py"),
                        diff(self.old, self.comment, "tests/fixtures/demo.sql"),
                        raw_comment + diff(self.old, self.comment, "tests/fixtures/demo.sql"),
                        raw_comment + raw_comment, diff(self.old, "# noqa: E501\nx = 1\n"), "broken diff"):
                with self.subTest(raw=raw):
                    self.assertFalse(self.decide(raw))
            assess.assert_not_called()

    def test_ordinary_comments_still_require_full_assessment(self):
        sources = {"tests/test_observe.py": "import sqlglot.demo\nimport inspect\ninspect.getsource(sqlglot.demo)\n"}
        self.assertTrue(self.decide(diff(self.old, self.comment)))
        self.assertFalse(self.decide(diff(self.old, self.comment), test_sources=sources))
        self.assertFalse(self.decide(diff(self.old, self.comment, metadata="index ffffffff..aaaaaaaa 100644\n")))
        self.assertFalse(self.decide(diff(self.old, self.comment, metadata="old mode 100644\nnew mode 100755\n")))


if __name__ == "__main__":
    unittest.main()
