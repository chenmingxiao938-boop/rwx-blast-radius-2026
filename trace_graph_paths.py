"""Observe whether a shared test helper import eagerly loads dialect modules."""

import json
import sys

import sqlglot


def loaded():
    return {name: f"sqlglot.dialects.{name}" in sys.modules
            for name in ("clickhouse", "sqlite", "bigquery")}


def main():
    before = loaded()
    from tests.dialects.test_dialect import Validator  # noqa: F401
    after_helper_import = loaded()
    sqlglot.parse_one("SELECT 1", read="clickhouse")
    after_clickhouse_parse = loaded()
    print(json.dumps({"before_helper": before, "after_helper_import": after_helper_import,
                      "after_clickhouse_parse": after_clickhouse_parse}, indent=2))


if __name__ == "__main__":
    main()
