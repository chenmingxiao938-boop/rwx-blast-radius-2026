"""Research-only, source-backed SQL fixture impact candidates.

The pinned test source is the evidence. This module never authorizes test
reduction: observed calls are not a completeness proof.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class FixtureEvidence:
    fixture_path: str
    test_id: str
    test_number: int
    source_path: str
    line: int
    method: str


@dataclass(frozen=True)
class FixtureImpact:
    path: str
    candidate_ids: tuple[str, ...]
    candidate_numbers: tuple[int, ...]
    evidence: tuple[FixtureEvidence, ...]
    unresolved: tuple[str, ...]
    method: str


@dataclass(frozen=True)
class FixtureMappingResult:
    candidate_ids: tuple[str, ...]
    candidate_numbers: tuple[int, ...]
    file_impacts: tuple[FixtureImpact, ...]
    unresolved: tuple[str, ...]
    fallback_required: bool = True


def _path_ok(path: str) -> bool:
    return (
        bool(path) and path.startswith("tests/fixtures/")
        and "\\" not in path and ":" not in path
        and all(part not in ("", ".", "..") for part in path.split("/"))
    )


def _literal(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _registered_tests(metadata: Mapping[str, object]) -> dict[str, int]:
    entries = metadata.get("tests") if isinstance(metadata, Mapping) else None
    if not isinstance(entries, list):
        raise ValueError("test metadata needs a tests list")
    result: dict[str, int] = {}
    for number, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError("test entry must be an object")
        test_id, path, actual = entry.get("id"), entry.get("file"), entry.get("number")
        if (not isinstance(test_id, str) or not isinstance(path, str)
                or not test_id.startswith(path + "::") or not path.startswith("tests/")
                or any(part in ("", ".", "..") for part in path.split("/"))
                or "\\" in path or ":" in path
                or not isinstance(actual, int) or isinstance(actual, bool) or actual != number
                or test_id in result):
            raise ValueError("invalid test manifest")
        result[test_id] = number
    return result


def _call_fixture(call: ast.Call, source_path: str) -> tuple[str | None, str] | None:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else None
    if name in ("load_sql_fixtures", "load_sql_fixture_pairs"):
        argument = _literal(call.args[0]) if call.args else None
        return (f"tests/fixtures/{argument}" if argument is not None else None, name)
    if (source_path == "tests/test_optimizer.py" and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name) and func.value.id == "self"
            and func.attr == "check_file"):
        argument = _literal(call.args[0]) if call.args else None
        return (f"tests/fixtures/optimizer/{argument}.sql" if argument is not None else None,
                "TestOptimizer.check_file")
    return None


def _scan_source(root: Path, registered: Mapping[str, int]) -> tuple[dict[str, list[FixtureEvidence]], list[str]]:
    index: dict[str, list[FixtureEvidence]] = {}
    unresolved: list[str] = []
    test_paths = sorted({test_id.split("::", 1)[0] for test_id in registered})
    for source_path in test_paths:
        source = root / source_path
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=source_path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            unresolved.append(f"Cannot inspect registered test source {source_path}: {type(exc).__name__}")
            continue
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            methods = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for method in methods:
                is_setup = method.name in ("setUp", "setUpClass")
                if not method.name.startswith("test_") and not is_setup:
                    continue
                prefix = f"{source_path}::{cls.name}::"
                matches = ([test_id for test_id in registered if test_id.startswith(prefix)] if is_setup else
                           [prefix + method.name] if prefix + method.name in registered else [])
                for call in (node for node in ast.walk(method) if isinstance(node, ast.Call)):
                    found = _call_fixture(call, source_path)
                    if found is None:
                        continue
                    fixture_path, via = found
                    if fixture_path is None or not _path_ok(fixture_path):
                        unresolved.append(f"Dynamic or invalid fixture path at {source_path}:{call.lineno}")
                        continue
                    for test_id in matches:
                        index.setdefault(fixture_path, []).append(FixtureEvidence(
                            fixture_path, test_id, registered[test_id], source_path, call.lineno, via,
                        ))
    return index, unresolved


def map_fixture_impacts(
    changed_paths: Sequence[str], test_metadata: Mapping[str, object], source_root: str | Path,
) -> FixtureMappingResult:
    """Map changed SQL fixtures against registered test calls in pinned source.

    source_root is the pinned sqlglot checkout. Unmapped files remain unresolved.
    Every result retains full-suite fallback until completeness is independently proven.
    """
    registered = _registered_tests(test_metadata)
    root = Path(source_root)
    index, scan_uncertainty = _scan_source(root, registered)
    impacts: list[FixtureImpact] = []
    for path in sorted(set(changed_paths)):
        problems = set(scan_uncertainty)
        evidence = tuple(sorted(set(index.get(path, ())), key=lambda item: (
            item.test_number, item.source_path, item.line, item.method)))
        if not _path_ok(path) or not path.endswith(".sql"):
            problems.add(f"Unsupported fixture path or type: {path}")
        elif not (root / path).is_file():
            problems.add(f"Fixture absent from pinned source: {path}")
        if not evidence:
            problems.add(f"No verified registered reader for {path}")
        problems.add("Other direct or indirect file reads have not been excluded")
        impacts.append(FixtureImpact(
            path, tuple(sorted({item.test_id for item in evidence})),
            tuple(sorted({item.test_number for item in evidence})), evidence,
            tuple(sorted(problems)), "static_ast_call_site" if evidence else "unresolved",
        ))
    all_ids = tuple(sorted({test_id for impact in impacts for test_id in impact.candidate_ids}))
    all_numbers = tuple(sorted({number for impact in impacts for number in impact.candidate_numbers}))
    uncertainties = {reason for impact in impacts for reason in impact.unresolved}
    if not impacts:
        uncertainties.add("No changed fixture paths")
    return FixtureMappingResult(all_ids, all_numbers, tuple(impacts), tuple(sorted(uncertainties)))
