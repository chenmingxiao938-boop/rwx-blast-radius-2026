"""Controlled pinned-SQLGlot dispatch observation; no sample answers."""

import json

from sqlglot import parse_one, transpile
from sqlglot.dialects.dialect import Dialect


def main() -> None:
    rows = []
    for name in ("singlestore", "materialize", "bigquery"):
        dialect = Dialect.get_or_raise(name)
        parser = dialect.parser()
        generator = dialect.generator()
        parsed = parse_one("SELECT 1", read=name)
        written = transpile("SELECT 1", read=name, write=name)
        rows.append({
            "dialect": name,
            "dialect_class": type(dialect).__module__ + "." + type(dialect).__name__,
            "parser_class": type(parser).__module__ + "." + type(parser).__name__,
            "generator_class": type(generator).__module__ + "." + type(generator).__name__,
            "parsed_class": type(parsed).__name__,
            "transpile_result": written,
        })
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
