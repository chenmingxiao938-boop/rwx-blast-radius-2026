import difflib
import unittest

from c12_noop import _blob_sha1, assess_patch
from patch_parser import parse_patch


PATH = "sqlglot/demo.py"


def diff(old, new, path=PATH, metadata=None):
    if metadata is None:
        metadata = f"index {_blob_sha1(old)[:10]}..{_blob_sha1(new)[:10]} 100644\n"
    return f"diff --git a/{path} b/{path}\n" + metadata + "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}",
    ))


class NoopTests(unittest.TestCase):
    def assess(self, old, new, *, path=PATH, test_sources=None, graph=None, metadata=None):
        text = diff(old, new, path, metadata)
        return assess_patch(parse_patch(text), {path: old}, test_sources or {}, graph or {}, patch_text=text)

    def test_same_line_comment_has_equal_executable_and_metadata(self):
        result = self.assess("# hello\ndef f(x):\n    return x + 1\n",
                             "# goodbye\ndef f(x):\n    return x + 1\n")
        self.assertEqual(result["classification"], "executable_equivalent")
        self.assertTrue(result["zero_candidate"])
        row = result["files"][0]
        for key in ("replay_verified", "blob_verified", "ast_equal", "bytecode_equal",
                    "source_metadata_equal", "tokens_equal", "token_positions_equal", "imports_equal"):
            self.assertTrue(row[key], key)
        self.assertTrue(result["assumptions"])

    def test_inserted_comment_is_only_likely_equivalent(self):
        result = self.assess("def f():\n    return 1\n", "def f():\n    # note\n    return 1\n")
        self.assertEqual(result["classification"], "likely_equivalent")
        self.assertTrue(result["zero_candidate"])
        self.assertFalse(result["files"][0]["source_metadata_equal"])
        self.assertTrue(result["files"][0]["bytecode_equal"])
        self.assertIn("line shifts", result["assumptions"][-1])

    def test_nested_constant_and_import_behavior_change(self):
        old = "def f():\n    import abc\n    return lambda: 1\n"
        new = "def f():\n    import ast\n    return lambda: 2\n"
        result = self.assess(old, new)
        self.assertEqual(result["classification"], "changed_behavior")
        self.assertFalse(result["zero_candidate"])
        self.assertFalse(result["files"][0]["imports_equal"])
        nested = self.assess(old, old.replace("lambda: 1", "lambda: 2"))
        self.assertFalse(nested["files"][0]["bytecode_equal"])

    def test_source_readers_and_line_observers_block_zero(self):
        old = "# old name\ndef f():\n    return 1\n"
        new = "# new name\ndef f():\n    return 1\n"
        for text in ("from inspect import getsource as reader\nimport sqlglot.demo\nreader(sqlglot.demo.f)\n",
                     "import sqlglot.demo\nassert sqlglot.demo.f.__code__.co_firstlineno == 2\n",
                     "from pathlib import Path\nimport sqlglot.demo\nassert Path(sqlglot.demo.__file__).read_text()\n"):
            with self.subTest(text=text):
                result = self.assess(old, new, test_sources={"tests/test_demo.py": text})
                self.assertEqual(result["classification"], "executable_equivalent")
                self.assertFalse(result["zero_candidate"])
                self.assertIn("concrete_source_or_position_observer", result["files"][0]["blockers"])

    def test_observer_must_be_relevant_to_changed_module(self):
        old, new = "# old note\nx = 1\n", "# edited note\nx = 1\n"
        tests = {"tests/test_reader.py": "import inspect\nimport unrelated\ninspect.getsource(unrelated)\n"}
        self.assertTrue(self.assess(old, new, test_sources=tests)["zero_candidate"])
        self.assertFalse(self.assess(old, new, test_sources=tests,
                                     graph={"tests/test_reader.py": [PATH]})["zero_candidate"])

    def test_aliased_stack_api_can_observe_comment_line_shift(self):
        for imports, call in (("import inspect as helper", "helper.stack()"),
                              ("from inspect import stack as frames", "frames()")):
            with self.subTest(call=call):
                old = f"{imports}\n\ndef observe():\n    return {call}[0][2]\n"
                new = old.replace("def observe", "# Ordinary comment\ndef observe")
                actual = []
                for source in (old, new):
                    scope = {}
                    exec(compile(source, PATH, "exec"), scope)
                    actual.append(scope["observe"]())
                self.assertNotEqual(*actual)
                diagnosis = self.assess(old, new)
                self.assertFalse(diagnosis["zero_candidate"])
                self.assertTrue(any(mark.endswith(":inspect.stack")
                                    for mark in diagnosis["files"][0]["observed_source_consumers"]))

    def test_directives_modes_and_source_encoding_block_zero(self):
        old = "# ordinary note\nx = 1\n"
        for new in ("# noqa: E266\nx = 1\n", "# -*- coding: latin-1 -*-\nx = 1\n",
                    "# type: ignore[attr]\nx = 1\n"):
            self.assertFalse(self.assess(old, new)["zero_candidate"])
        result = self.assess(old, old.replace("ordinary", "another"),
                             metadata="old mode 100644\nnew mode 100755\n")
        self.assertEqual(result["classification"], "unknown")
        self.assertIn("file_mode_or_patch_metadata_changed", result["files"][0]["blockers"])

    def test_exact_replay_and_blob_check_reject_wrong_base(self):
        old, new = "# ordinary note\nx = 1\n", "# another note\nx = 1\n"
        text = diff(old, new)
        result = assess_patch(parse_patch(text), {PATH: old.replace("x = 1", "x = 2")}, {}, {}, patch_text=text)
        self.assertFalse(result["files"][0]["replay_verified"])
        tampered = self.assess(old, new, metadata="index ffffffff..aaaaaaaa 100644\n")
        self.assertFalse(tampered["files"][0]["blob_verified"])
        self.assertFalse(tampered["zero_candidate"])

    def test_ast_equality_without_raw_metadata_cannot_skip(self):
        old, new = "# text\nx = 1\n", "# changed\nx = 1\n"
        text = diff(old, new)
        self.assertFalse(assess_patch(parse_patch(text), {PATH: old}, {}, {})["zero_candidate"])
        self.assertFalse(self.assess("x = (1)\n", "x = 1\n")["zero_candidate"])

    def test_mixed_fixture_input_or_missing_file_cannot_skip(self):
        before, after = "# old note\nx = 1\n", "# new note\nx = 1\n"
        fixture = "tests/fixtures/demo.sql"
        text = diff(before, after) + diff("SELECT 1\n", "SELECT 2\n", fixture)
        parsed = parse_patch(text)
        known = assess_patch(parsed, {PATH: before, fixture: "SELECT 1\n"}, {}, {}, patch_text=text)
        self.assertEqual(known["classification"], "changed_behavior")
        self.assertFalse(known["zero_candidate"])
        missing = assess_patch(parsed, {PATH: before}, {}, {}, patch_text=text)
        self.assertEqual(missing["classification"], "unknown")
        self.assertFalse(missing["zero_candidate"])


if __name__ == "__main__":
    unittest.main()
