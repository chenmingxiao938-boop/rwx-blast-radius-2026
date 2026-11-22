"""Experimental positive-evidence impact selector.

This is deliberately not a safety proof. Every returned reduction carries an
uncertainty explaining the unobserved paths. Unknown patch structure and
structural edits retain the full suite.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath

from method_boundary import method_body_symbol, requires_structural_fallback
from patch_parser import FileChange, Hunk, parse_patch

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
FIXTURE_MODES = ("baseline", "aggressive_fixture_only")
FIXTURE_SAFETY_MODES = ("current", "conservative", "targeted")
METHOD_MODES = ("baseline", "ast_method_body")


@dataclass(frozen=True)
class Decision:
    test_numbers: tuple[int, ...]
    reasons: dict[int, tuple[str, ...]]
    uncertainty: tuple[str, ...]
    fallback_required: bool
    fallback_reasons: tuple[str, ...]


@dataclass(frozen=True)
class FixtureReaderResolution:
    numbers: tuple[int, ...]
    resolved: tuple[tuple[str, int], ...]
    unresolved: tuple[str, ...]
    ambiguous: tuple[str, ...]
    collection_seen: bool


def resolve_fixture_readers(readers, manifest: dict) -> FixtureReaderResolution:
    """Resolve observed pytest node IDs using exact official manifest IDs."""
    tests = manifest["tests"]
    by_id = {}
    numbers = set()
    for test in tests:
        if not isinstance(test, dict) or not {"id", "number", "file"} <= test.keys():
            raise ValueError("invalid official test manifest")
        nodeid, number, path = test["id"], test["number"], test["file"]
        if (not isinstance(path, str) or not path.startswith("tests/")
                or "\\" in path or any(part in ("", ".", "..") for part in path.split("/"))
                or not isinstance(nodeid, str) or not nodeid.startswith(path + "::")
                or not nodeid[len(path) + 2:]
                or not isinstance(number, int) or isinstance(number, bool)
                or number < 0 or nodeid in by_id or number in numbers):
            raise ValueError("invalid official test manifest")
        by_id[nodeid] = number
        numbers.add(number)
    if sorted(numbers) != list(range(len(tests))):
        raise ValueError("invalid official test numbering")

    resolved = []
    unresolved = []
    ambiguous = []
    collection_seen = False
    for reader in sorted(set(readers), key=str):
        if not isinstance(reader, str) or not reader:
            unresolved.append(str(reader))
            continue
        if reader == "collection":
            collection_seen = True
            continue
        nodeid = reader.removeprefix("test:")
        if nodeid in by_id:
            resolved.append((reader, by_id[nodeid]))
            continue
        # A short method or parametrized base can refer to multiple official IDs.
        matches = [name for name in by_id if name.endswith("::" + nodeid)
                   or name.startswith(nodeid + "[")]
        (ambiguous if len(matches) > 1 else unresolved).append(reader)
    return FixtureReaderResolution(
        tuple(sorted({number for _, number in resolved})),
        tuple(resolved), tuple(unresolved), tuple(ambiguous), collection_seen)


def _anchors(hunk: Hunk) -> tuple[int, ...]:
    if hunk.old_changed_lines:
        return hunk.old_changed_lines
    old_line = hunk.old_start
    anchors = set()
    for line in hunk.body:
        if line.startswith("+"):
            anchors.update((old_line - 1, old_line))
        elif line.startswith(("-", " ")):
            old_line += 1
    return tuple(sorted(line for line in anchors if line > 0))


def _symbol_at(spans: list, line: int) -> str | None:
    matches = [span for span in spans if span[1] <= line <= span[2]]
    if not matches:
        return None
    name, start, end, definition, kind = min(matches, key=lambda s: (s[2] - s[1], s[1]))
    if line <= definition or kind == "class":
        return None  # decorator/signature/class definition can change import behavior
    return name


def _structural_change(change: FileChange) -> bool:
    for hunk in change.hunks:
        for line in hunk.body:
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                text = line[1:].lstrip()
                if text.startswith(("import ", "from ", "class ", "def ", "async def ", "@")):
                    return True
    return False


def _fixture_evidence_issues(readers, manifest: dict, *, inspect_observations: bool) -> tuple[str, ...]:
    """Return path-local evidence defects; global coverage warnings have no fixture provenance."""
    if not isinstance(readers, dict) or not readers:
        return ("reader_map_missing",)
    resolution = resolve_fixture_readers(readers, manifest)
    issues = set()
    if not resolution.numbers:
        issues.add("no_resolved_readers")
    if resolution.unresolved:
        issues.add("unresolved_reader_ids")
    if resolution.ambiguous:
        issues.add("ambiguous_reader_ids")
    if resolution.collection_seen:
        issues.add("collection_read")
    if inspect_observations:
        for reader, observations in sorted(readers.items(), key=lambda item: str(item[0])):
            if reader == "collection":
                continue
            if not isinstance(observations, list) or not observations:
                issues.add(f"reader_evidence_missing:{reader}")
                continue
            if any(
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or not all(isinstance(part, str) and part for part in item)
                for item in observations
            ):
                issues.add(f"reader_evidence_malformed:{reader}")
    return tuple(sorted(issues))


def _eligible_fixture_only(patch, patch_text: str, manifest: dict, coverage: dict,
                           fixture_safety_mode: str) -> bool:
    if (patch.unknown or not patch.files
            or any(line.startswith(("old mode ", "new mode "))
                   for line in patch_text.splitlines())):
        return False
    for change in patch.files:
        path = change.new_path
        if (change.unknown_reason or change.kind != "modified" or not change.hunks
                or path is None or path != change.old_path
                or not path.startswith("tests/fixtures/") or not path.endswith(".sql")):
            return False
        readers = coverage["fixture_reads"].get(path)
        if _fixture_evidence_issues(
            readers, manifest, inspect_observations=fixture_safety_mode == "targeted"
        ):
            return False
    return True


def analyze_patch(patch_text: str, manifest: dict, coverage: dict, ranges: dict,
                  dependencies: dict, *, fixture_mode: str = "baseline",
                  method_mode: str = "baseline", source_snapshot: dict | None = None,
                  fixture_safety_mode: str = "current") -> Decision:
    if fixture_mode not in FIXTURE_MODES:
        raise ValueError(f"unknown fixture mode: {fixture_mode}")
    if fixture_safety_mode not in FIXTURE_SAFETY_MODES:
        raise ValueError(f"unknown fixture safety mode: {fixture_safety_mode}")
    if method_mode not in METHOD_MODES:
        raise ValueError(f"unknown method mode: {method_mode}")
    if method_mode == "ast_method_body" and (source_snapshot is None or
            source_snapshot.get("pinned_commit") != PINNED_COMMIT):
        raise ValueError("method mode requires the pinned source snapshot")
    if any(data.get("pinned_commit", data.get("commit")) != PINNED_COMMIT
           for data in (coverage, ranges, dependencies)):
        raise ValueError("map is not from the official pinned SQLGlot commit")
    tests = manifest["tests"]
    all_ids = tuple(t["number"] for t in tests)
    # Validate the manifest before using it for any observed fixture reads.
    resolve_fixture_readers((), manifest)
    by_file = {}
    for test in tests:
        by_file.setdefault(test["file"], set()).add(test["number"])
    if len(all_ids) != len(set(all_ids)) or sorted(all_ids) != list(range(len(all_ids))):
        raise ValueError("invalid official test numbering")
    if coverage["manifest_count"] != len(all_ids) or coverage["missing_ids"] or coverage["extra_ids"] or coverage["pytest_exit_code"]:
        raise ValueError("coverage map did not complete the official test inventory")
    patch = parse_patch(patch_text)
    method_override = None
    method_risk = False
    if method_mode == "ast_method_body" and not patch.unknown and len(patch.files) == 1:
        only = patch.files[0]
        if only.old_path and only.old_path.startswith("sqlglot/"):
            source = source_snapshot["sources"].get(only.old_path)
            if source is not None:
                method_override = method_body_symbol(only, source, patch_text)
                if method_override is None:
                    method_risk = requires_structural_fallback(only, source)
            else:
                method_risk = True
            if ("\\ No newline at end of file" in patch_text or
                    any(line.startswith(("old mode ", "new mode "))
                        for line in patch_text.splitlines())):
                method_risk = True
    aggressive_fixture = (
        fixture_mode == "aggressive_fixture_only"
        and fixture_safety_mode != "conservative"
        and _eligible_fixture_only(
            patch, patch_text, manifest, coverage, fixture_safety_mode
        )
    )
    candidates: set[int] = set()
    evidence: dict[int, set[str]] = {}
    uncertainties = {"observed_coverage_does_not_prove_absent_dependencies",
                     "subprocess_and_native_code_are_not_fully_attributed"}
    fallback = set(patch.unknown_reasons)
    if method_risk:
        fallback.add("ast_structure_or_source_unverified")
    if not patch.files:
        fallback.add("patch_has_no_reliable_file_changes")

    def add(numbers, reason):
        for number in numbers:
            candidates.add(number)
            evidence.setdefault(number, set()).add(reason)

    graph = dependencies["direct_dependencies"]
    for change in patch.files:
        path = change.new_path or change.old_path
        if change.unknown_reason or path is None:
            fallback.add(f"unknown_change:{path}")
            continue
        if change.kind not in ("modified",):
            fallback.add(f"nonlocal_file_change:{path}:{change.kind}")
            continue
        if path.startswith("tests/fixtures/"):
            if not path.endswith(".sql"):
                fallback.add(f"unsupported_fixture:{path}")
                continue
            readers = coverage["fixture_reads"].get(path, {})
            if not isinstance(readers, dict):
                readers = {}
            if fixture_safety_mode == "conservative":
                fallback.add(f"conservative_fixture_handling:{path}")
            elif fixture_safety_mode == "targeted":
                for issue in _fixture_evidence_issues(
                    readers, manifest, inspect_observations=True
                ):
                    fallback.add(f"targeted_fixture_incompleteness:{path}:{issue}")
            resolution = resolve_fixture_readers(readers, manifest)
            if not readers or not resolution.numbers:
                fallback.add(f"fixture_reader_unresolved:{path}")
            if resolution.collection_seen:
                fallback.add(f"fixture_collection_read:{path}")
            for reader in resolution.unresolved:
                fallback.add(f"fixture_reader_unresolved:{path}:{reader}")
            for reader in resolution.ambiguous:
                fallback.add(f"fixture_reader_ambiguous:{path}:{reader}")
            add(resolution.numbers, f"observed_fixture_read:{path}")
            if aggressive_fixture:
                uncertainties.add(f"aggressive_fixture_only_observed_readers_may_be_incomplete:{path}")
            else:
                fallback.add(f"fixture_impact_completeness_unproved:{path}")
            uncertainties.add(f"unobserved_fixture_reads_possible:{path}")
            continue
        if path.startswith("tests/") and path.endswith(".py"):
            if path not in by_file:
                fallback.add(f"test_file_not_registered:{path}")
                continue
            add(by_file[path], f"changed_test_file:{path}")
            importers = [source for source, imports in graph.items() if path in imports]
            for source in importers:
                add(by_file.get(source, ()), f"direct_static_import:{source}->{path}")
            if importers:
                uncertainties.add(f"transitive_test_imports_possible:{path}")
            continue
        if not path.startswith("sqlglot/") or not path.endswith(".py"):
            fallback.add(f"unsupported_path:{path}")
            continue
        if not change.hunks or _structural_change(change):
            fallback.add(f"module_structure_or_import_changed:{path}")
            continue
        spans = ranges["symbols"].get(path)
        if not spans:
            fallback.add(f"no_source_symbols:{path}")
            continue
        symbols = set()
        for hunk in change.hunks:
            anchors = _anchors(hunk)
            if method_override is not None:
                symbols.add(method_override)
            else:
                resolved = {_symbol_at(spans, line) for line in anchors}
                if not anchors or None in resolved or len(resolved) != 1:
                    fallback.add(f"hunk_not_within_one_symbol:{path}:{hunk.old_start}")
                else:
                    symbols.update(resolved)
            if any(line in coverage["collection_lines"].get(path, ()) for line in anchors):
                fallback.add(f"import_time_line_changed:{path}:{hunk.old_start}")
        for symbol in sorted(symbols):
            numbers = coverage["symbol_index"].get(path, {}).get(symbol, ())
            if not numbers:
                fallback.add(f"symbol_has_no_observed_tests:{path}:{symbol}")
            add(numbers, f"observed_symbol:{path}:{symbol}")
        if method_override is not None:
            uncertainties.add(f"ast_method_body_coverage_not_complete:{path}:{method_override}")
        for importer, imports in graph.items():
            if path in imports and importer in by_file:
                add(by_file[importer], f"direct_static_import:{importer}->{path}")
        if path.startswith(("sqlglot/parsers/", "sqlglot/generators/")):
            dialect = PurePosixPath(path).stem
            dialect_test = f"tests/dialects/test_{dialect}.py"
            add(by_file.get(dialect_test, ()), f"dialect_test_module:{dialect_test}")
            uncertainties.add(f"dynamic_dialect_routes_may_be_unobserved:{path}")
    if fallback:
        add(all_ids, "full_suite_fallback")
    if not candidates:
        add(all_ids, "empty_candidate_fallback")
        fallback.add("empty_candidate_set")
    return Decision(tuple(sorted(candidates)),
                    {number: tuple(sorted(reasons)) for number, reasons in sorted(evidence.items())},
                    tuple(sorted(uncertainties)), bool(fallback), tuple(sorted(fallback)))
