import unittest

from v5_main import needs_source_snapshot, resolve_method_mode


class ASTAblationTests(unittest.TestCase):
    def test_default_mode_preserves_c2_ast_selection(self):
        self.assertEqual(resolve_method_mode("ast_method_body", "enabled"),
                         "ast_method_body")

    def test_disabled_mode_changes_only_method_selection(self):
        self.assertEqual(resolve_method_mode("ast_method_body", "disabled"), "baseline")
        self.assertEqual(resolve_method_mode("baseline", "disabled"), "baseline")

    def test_mixed_analysis_still_loads_snapshot_when_ast_is_disabled(self):
        method_mode = resolve_method_mode("ast_method_body", "disabled")
        self.assertEqual(method_mode, "baseline")
        self.assertTrue(needs_source_snapshot(method_mode, "comment_only_fixture"))
        self.assertFalse(needs_source_snapshot(method_mode, "baseline"))

    def test_unknown_ast_ablation_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_method_mode("ast_method_body", "typo")


if __name__ == "__main__":
    unittest.main()
