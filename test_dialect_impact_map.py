"""Focused evidence and uncertainty tests for dynamic dialect mapping."""

import tempfile
import unittest
from pathlib import Path

from dialect_impact_map import map_dialect_impacts


class DialectMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write("sqlglot/dialects/__init__.py", "DIALECTS = ['BigQuery', 'Materialize', 'SingleStore']\n")
        for name, klass in (("bigquery", "BigQuery"), ("materialize", "Materialize"),
                            ("singlestore", "SingleStore")):
            self.write(f"sqlglot/dialects/{name}.py", f"""from sqlglot.parsers.{name} import {klass}Parser
from sqlglot.generators.{name} import {klass}Generator
class {klass}(Dialect):
    Parser = {klass}Parser
    Generator = {klass}Generator
""")
        self.write("tests/dialects/test_singlestore.py", """import importlib
class TestSingleStore(Validator):
    dialect = 'singlestore'
    def test_inherited(self):
        self.validate_identity('SELECT 1')
    def test_self_attribute(self):
        parse_one('SELECT 1', dialect=self.dialect)
    def test_literal(self):
        parse_one('SELECT 1', read='singlestore')
    def test_write(self):
        transpile('SELECT 1', read='bigquery', write='singlestore')
    def test_computed(self):
        parse_one('SELECT 1', read=prefix + suffix)
    def test_dynamic_import(self):
        importlib.import_module('sqlglot.dialects.singlestore')
    def test_unknown_import(self):
        importlib.import_module(module_name)
""")
        self.write("tests/dialects/test_dialect.py", """class TestDialect(Validator):
    def test_multiple(self):
        for dialect in ('bigquery', 'materialize'):
            parse_one('SELECT 1', read=dialect)
    def test_registry(self):
        Dialect.get_or_raise('bigquery')
    def test_generator(self):
        parse_one('SELECT 1').sql(dialect='materialize')
    def test_unknown_name(self):
        parse_one('SELECT 1', read='not_a_dialect')
    def test_transpile_without_identity(self):
        transpile('SELECT 1', read='singlestore', identity=False)
""")
        self.write("tests/dialects/test_computed.py", """class TestComputed(Validator):
    dialect = configured_name
    def test_dynamic(self):
        self.validate_identity('SELECT 1')
""")
        names = [
            ("tests/dialects/test_singlestore.py", "TestSingleStore", name)
            for name in ("test_inherited", "test_self_attribute", "test_literal", "test_write",
                         "test_computed", "test_dynamic_import", "test_unknown_import")
        ] + [
            ("tests/dialects/test_dialect.py", "TestDialect", name)
            for name in ("test_multiple", "test_registry", "test_generator", "test_unknown_name",
                         "test_transpile_without_identity")
        ] + [("tests/dialects/test_computed.py", "TestComputed", "test_dynamic")]
        self.metadata = {"tests": [
            {"number": i, "id": f"{path}::{klass}::{method}", "file": path, "seconds": 0.001}
            for i, (path, klass, method) in enumerate(names)
        ]}

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def map(self, *paths):
        return map_dialect_impacts(paths, self.metadata, self.root)

    def test_literal_parser_name(self):
        result = self.map("sqlglot/parsers/singlestore.py")
        self.assertIn(2, result.candidate_numbers)

    def test_self_dialect_inherited_helper(self):
        result = self.map("sqlglot/parsers/singlestore.py")
        self.assertIn(0, result.candidate_numbers)
        self.assertIn(1, result.candidate_numbers)

    def test_parser_and_generator_dispatch_are_distinct(self):
        parser = self.map("sqlglot/parsers/singlestore.py")
        generator = self.map("sqlglot/generators/singlestore.py")
        self.assertNotIn(3, parser.candidate_numbers)
        self.assertIn(3, generator.candidate_numbers)

    def test_transpile_identity_false_uses_only_read_parser(self):
        parser = self.map("sqlglot/parsers/singlestore.py")
        generator = self.map("sqlglot/generators/singlestore.py")
        self.assertIn(11, parser.candidate_numbers)
        self.assertNotIn(11, generator.candidate_numbers)

    def test_registry_lookup_is_dialect_module_evidence(self):
        dialect = self.map("sqlglot/dialects/bigquery.py")
        parser = self.map("sqlglot/parsers/bigquery.py")
        self.assertIn(8, dialect.candidate_numbers)
        self.assertNotIn(8, parser.candidate_numbers)

    def test_multiple_dialects_from_loop(self):
        bigquery = self.map("sqlglot/parsers/bigquery.py")
        materialize = self.map("sqlglot/parsers/materialize.py")
        self.assertIn(7, bigquery.candidate_numbers)
        self.assertIn(7, materialize.candidate_numbers)

    def test_unknown_name_is_unresolved(self):
        result = self.map("sqlglot/parsers/bigquery.py")
        self.assertTrue(any("Unknown dialect not_a_dialect" in item for item in result.unresolved))
        self.assertTrue(any("Unknown dialect not_a_dialect" in item for item in result.global_unresolved))
        self.assertFalse(any("Unknown dialect not_a_dialect" in item
                             for item in result.file_impacts[0].unresolved))

    def test_computed_name_is_unresolved(self):
        result = self.map("sqlglot/parsers/singlestore.py")
        self.assertTrue(any("Computed dialect" in item for item in result.unresolved))

    def test_computed_class_dialect_is_unresolved(self):
        result = self.map("sqlglot/parsers/singlestore.py")
        self.assertTrue(any("Computed class dialect" in item for item in result.unresolved))

    def test_dynamic_import_literal_and_computed(self):
        result = self.map("sqlglot/dialects/singlestore.py")
        self.assertIn(5, result.candidate_numbers)
        self.assertTrue(any("Dynamic import" in item for item in result.unresolved))

    def test_shared_implementation_path(self):
        for name, klass in (("bigquery", "BigQuery"), ("materialize", "Materialize")):
            self.write(f"sqlglot/dialects/{name}.py", f"""from sqlglot.parsers.common import CommonParser
from sqlglot.generators.{name} import {klass}Generator
class {klass}(Dialect):
    Parser = CommonParser
    Generator = {klass}Generator
""")
        result = self.map("sqlglot/parsers/common.py")
        self.assertEqual(result.file_impacts[0].referenced_dialects, ("bigquery", "materialize"))
        self.assertIn(7, result.candidate_numbers)

    def test_deterministic_order_and_union(self):
        paths = ("sqlglot/parsers/singlestore.py", "sqlglot/dialects/materialize.py")
        self.assertEqual(self.map(*paths), self.map(*reversed(paths)))
        result = self.map(*paths)
        self.assertEqual(len(result.candidate_numbers), len(set(result.candidate_numbers)))

    def test_missing_metadata_is_rejected(self):
        with self.assertRaises(ValueError):
            map_dialect_impacts(["sqlglot/parsers/bigquery.py"], {}, self.root)

    def test_fallback_is_always_required(self):
        self.assertTrue(self.map("sqlglot/parsers/bigquery.py").fallback_required)
        self.assertTrue(self.map().fallback_required)

    def test_unknown_changed_path_is_unresolved(self):
        result = self.map("sqlglot/parsers/unregistered.py")
        self.assertEqual(result.candidate_numbers, ())
        self.assertTrue(any("No verified" in item for item in result.unresolved))


if __name__ == "__main__":
    unittest.main()
