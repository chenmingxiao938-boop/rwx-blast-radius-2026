"""Positive, scoped dependency evidence for the isolated C11-B audit."""

from pathlib import PurePosixPath

from c10_ast import _statement_lines, analyze_region, caller_distances, parse
from c10_experiment import fixture_path


def _owner_symbols(experiment, path, region):
    if region.symbols:
        return region.symbols
    symbols = set()
    spans = experiment.ranges["symbols"].get(path, ())
    for line in region.old_lines:
        owners = [span for span in spans if span[1] <= line <= span[2]]
        if owners:
            symbols.add(min(owners, key=lambda span: span[2] - span[1])[0])
    return symbols


def _owner_from_test_id(test_id):
    return test_id.split("::", 1)[-1].split("[", 1)[0].replace("::", ".")


def scoped_evidence(experiment, parsed_patch, ranking, numbers) -> dict[int, set[tuple[str, ...]]]:
    """Scope C10's observations to changed paths, old lines and caller edges.

    Branch tokens identify observed lines in a changed statement, not observed
    branch outcomes. Anchor tokens are insertion proxies. An absent token
    does not establish that a test is unaffected.
    """
    selected = set(numbers)
    evidence = {number: set() for number in sorted(selected)}
    if ranking is None:
        return evidence

    wanted = set().union(*(ranking.evidence.get(number, ()) for number in selected))

    def add(ids, token):
        for number in selected.intersection(ids):
            evidence[number].add(token)

    coverage = experiment.coverage
    targets = set()
    for change in parsed_patch.files:
        path = change.new_path
        if not path:
            continue
        if fixture_path(path):
            experiment.fixture_evidence(path, lambda ids, reason: add(ids, (reason, path)), set())
            continue
        if path in experiment.by_file:
            add(experiment.by_file[path], ("direct_test", path))
            continue
        if path not in experiment.source_files:
            continue

        source = experiment.source_files[path]
        region = analyze_region(change, source)
        old_changed = {line for hunk in change.hunks for line in hunk.old_changed_lines}
        old_statement_lines = set()
        if old_changed:
            tree = parse(source)
            for line in old_changed:
                old_statement_lines.update(_statement_lines(tree, line))

        line_index = coverage["line_index"].get(path, {})
        for line in old_changed:
            add(line_index.get(str(line), ()), ("line", path, str(line)))
        for line in region.branch_lines.intersection(old_statement_lines):
            add(line_index.get(str(line), ()), ("branch", path, str(line)))
        for line in (region.old_lines | region.branch_lines) - (old_changed | old_statement_lines):
            add(line_index.get(str(line), ()), ("anchor", path, str(line)))

        symbol_index = coverage["symbol_index"].get(path, {})
        for name in _owner_symbols(experiment, path, region):
            add(symbol_index.get(name, ()), ("symbol", path, name))
            targets.add((path, name))

        if "module" in wanted:
            module_tests = set()
            for ids in line_index.values():
                module_tests.update(selected.intersection(ids))
            add(module_tests, ("module", path))
        if "import" in wanted:
            for importer, imports in experiment.deps["direct_dependencies"].items():
                if path in imports:
                    add(experiment.by_file.get(importer, ()), ("import", path, importer))
        if path.startswith(("sqlglot/parsers/", "sqlglot/generators/", "sqlglot/dialects/")):
            dialect = PurePosixPath(path).stem
            test_file = f"tests/dialects/test_{dialect}.py"
            add(experiment.by_file.get(test_file, ()), ("dialect", path, test_file))

    if "caller" in wanted:
        for change_path, change_symbol in sorted(targets):
            for caller_path, caller_name in caller_distances(experiment.calls, {(change_path, change_symbol)}):
                token = ("caller", change_path, change_symbol, caller_path, caller_name)
                ids = coverage["symbol_index"].get(caller_path, {}).get(caller_name, ())
                add(ids, token)
                for number in selected.intersection(experiment.by_file.get(caller_path, ())):
                    if _owner_from_test_id(experiment.tests[number]["id"]) == caller_name:
                        evidence[number].add(token)
    return evidence
