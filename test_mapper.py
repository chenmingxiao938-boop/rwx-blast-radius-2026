"""Candidate code-to-test mapping for the pinned sqlglot project.

Candidates are evidence, not permission to reduce tests. Dependency analysis
may add candidates, but unresolved impact still requires the full suite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from patch_parser import FileChange, Patch


@dataclass(frozen=True)
class Candidate:
    test_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FileImpact:
    """One changed file, including both paths of a rename or copy.

    A dependency graph can union its test IDs with candidate_ids, then must
    resolve every unresolved_reasons entry before considering test reduction.
    """

    kind: str
    old_path: str | None
    new_path: str | None
    candidate_ids: tuple[str, ...]
    categories: tuple[str, ...]
    reasons: tuple[str, ...]
    unresolved_reasons: tuple[str, ...]


@dataclass(frozen=True)
class MappingResult:
    candidates: tuple[Candidate, ...]
    categories: tuple[str, ...]
    confidence: str
    fallback_required: bool
    fallback_reasons: tuple[str, ...]
    file_impacts: tuple[FileImpact, ...] = ()

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(candidate.test_id for candidate in self.candidates)


def _canonical_test_path(path: str) -> bool:
    """Check raw components; never turn a malformed path into a valid one."""
    if not path or "\\" in path or path.startswith("/") or ":" in path:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    parts = path.split("/")
    return (
        len(parts) >= 2
        and parts[0] == "tests"
        and all(part not in ("", ".", "..") for part in parts)
        and parts[-1].endswith(".py")
    )


def _test_index(metadata: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    """Validate the official ordered, gapless test manifest."""
    if not isinstance(metadata, Mapping):
        raise TypeError("test metadata must be a mapping")
    tests = metadata.get("tests")
    if not isinstance(tests, list):
        raise ValueError("test metadata must contain a tests list")
    by_file: dict[str, set[str]] = {}
    ids: set[str] = set()
    for expected_number, entry in enumerate(tests):
        if not isinstance(entry, Mapping):
            raise ValueError("test entry must be an object")
        test_id, path, number = entry.get("id"), entry.get("file"), entry.get("number")
        if not isinstance(test_id, str) or not test_id or not isinstance(path, str):
            raise ValueError("test entry needs nonempty id and file")
        if not _canonical_test_path(path):
            raise ValueError("test file path is not canonical")
        if not isinstance(number, int) or isinstance(number, bool) or number != expected_number:
            raise ValueError("test numbers must be ordered from zero without gaps")
        if not test_id.startswith(path + "::"):
            raise ValueError("test id and file disagree")
        if test_id in ids:
            raise ValueError("duplicate test id")
        ids.add(test_id)
        by_file.setdefault(path, set()).add(test_id)
    return {path: tuple(sorted(file_ids)) for path, file_ids in by_file.items()}


def _source_target(path: str) -> str | None:
    """Return structural candidate links; these do not prove completeness."""
    if not path.startswith("sqlglot/") or not path.endswith(".py"):
        return None
    parts = path.split("/")
    stem = parts[-1][:-3]
    if stem == "__init__":
        return None
    if len(parts) == 2:
        return f"tests/test_{stem}.py"
    if len(parts) == 3 and parts[1] in ("dialects", "parsers", "generators"):
        if stem in ("base", "dialect"):
            return None
        return f"tests/dialects/test_{stem}.py"
    if len(parts) == 3 and parts[1] == "optimizer":
        return "tests/test_optimizer.py"
    if len(parts) == 3 and parts[1] == "executor":
        return "tests/test_executor.py"
    return None


def _risk(path: str) -> tuple[str, ...]:
    if path.startswith("tests/fixtures/") or path in (
        "pyproject.toml", "pytest.ini", "setup.cfg", "setup.py", "tox.ini",
        "conftest.py", "requirements.lock",
    ) or path.endswith("/conftest.py") or path.startswith(".github/"):
        return ("configuration_or_fixture",)
    if path in ("tests/helpers.py", "tests/leakcheck.py", "tests/gen_fixtures.py",
                "tests/dialects/test_dialect.py") or path.endswith("/__init__.py") and path.startswith("tests/"):
        return ("shared_utility",)
    if path.startswith("tests/") and path.endswith(".py"):
        return ("direct_test_file",)
    if path in ("sqlglot/__init__.py", "sqlglot/dialects/dialect.py",
                "sqlglot/parser.py", "sqlglot/generator.py", "sqlglot/expressions/__init__.py"):
        return ("public_api", "shared_utility")
    if path.startswith("sqlglot/") and path.endswith("/__init__.py"):
        return ("import_or_dependency", "shared_utility")
    if path.startswith("sqlglot/") and path.endswith(".py"):
        if path in ("sqlglot/helper.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py") or (
            path.startswith(("sqlglot/expressions/", "sqlglot/parsers/base.py",
                             "sqlglot/generators/base.py"))
        ):
            return ("shared_utility",)
        return ("local_source_module",)
    return ("unknown_or_unsupported",)


def _import_status(change: FileChange) -> str:
    """Detect visible imports; a hunk alone cannot rule out multiline imports."""
    if not any(path and path.endswith(".py") for path in (change.old_path, change.new_path)):
        return "not_applicable"
    for hunk in change.hunks:
        for line in hunk.body:
            if line and line[0] in " +-" and (
                line[1:].lstrip().startswith("import ")
                or (line[1:].lstrip().startswith("from ") and " import" in line[1:])
            ):
                return "detected"
    return "unresolved"


def map_tests(patch: Patch, metadata: Mapping[str, object]) -> MappingResult:
    """Return deterministic candidates and per-file evidence, always full-suite.

    Dependency-derived IDs may be unioned with candidate_ids. Neither that
    union nor confidence removes the fallback requirement: a separate analysis
    must prove every changed file and indirect dependency resolved.
    """
    if not isinstance(patch, Patch):
        raise TypeError("patch must be a Patch")
    by_file = _test_index(metadata)
    candidates: dict[str, set[str]] = {}
    categories: set[str] = set()
    fallback: set[str] = {"Dependency and coverage completeness has not been established"}
    impacts: list[FileImpact] = []
    structural = direct = unknown = False

    if not patch.files:
        fallback.add("Patch has no verified file changes")
        unknown = True
    for reason in patch.unknown_reasons:
        fallback.add(f"Patch parser: {reason}")
        unknown = True

    changes = sorted(
        patch.files,
        key=lambda file: (file.old_path or "", file.new_path or "", file.kind, file.unknown_reason or "",
                          tuple((h.old_start, h.new_start, h.body) for h in file.hunks)),
    )
    for change in changes:
        file_ids: set[str] = set()
        file_categories: set[str] = set()
        file_reasons: set[str] = set()
        unresolved: set[str] = {"Indirect dependency coverage has not been established"}
        if change.kind == "unknown" or change.unknown_reason:
            file_categories.add("unknown_or_unsupported")
            unresolved.add(f"Uncertain file change: {change.unknown_reason or change.kind}")
            unknown = True
        paths = sorted({path for path in (change.old_path, change.new_path) if path})
        if not paths:
            unresolved.add("File change has no reliable path")
            unknown = True

        import_status = _import_status(change)
        if import_status == "detected":
            file_categories.add("import_or_dependency")
            file_reasons.add("Import statement appears in changed hunk")
            unresolved.add("Import dependents have not been resolved")
        elif import_status == "unresolved":
            file_categories.add("dependency_uncertain")
            unresolved.add("Import impact cannot be ruled out from diff hunks alone")

        for path in paths:
            risks = _risk(path)
            file_categories.update(risks)
            if "unknown_or_unsupported" in risks:
                unresolved.add(f"Unsupported changed path: {path}")
                unknown = True
                continue
            if "configuration_or_fixture" in risks:
                unresolved.add(f"Configuration or fixture change: {path}")
            if "shared_utility" in risks or "public_api" in risks or "import_or_dependency" in risks:
                unresolved.add(f"Shared or public dependency change: {path}")
            target = path if path.startswith("tests/") and path.endswith(".py") else _source_target(path)
            if target is None:
                if risks in (("local_source_module",), ("direct_test_file",)):
                    unresolved.add(f"No structural test link for {path}")
                    unknown = True
                continue
            ids = by_file.get(target, ())
            if not ids:
                unresolved.add(f"No registered tests for {path} at {target}")
                unknown = True
                continue
            is_direct = path == target
            direct |= is_direct
            structural |= not is_direct
            reason = (f"Changed registered test file: {path}" if is_direct else
                      f"Structural source link: {path} -> {target}")
            file_reasons.add(reason)
            file_ids.update(ids)
            for test_id in ids:
                candidates.setdefault(test_id, set()).add(reason)

        categories.update(file_categories)
        fallback.update(unresolved)
        impacts.append(FileImpact(
            change.kind, change.old_path, change.new_path, tuple(sorted(file_ids)),
            tuple(sorted(file_categories)), tuple(sorted(file_reasons)),
            tuple(sorted(unresolved)),
        ))

    confidence = "unknown" if unknown else "structural" if structural else "direct" if direct else "unknown"
    return MappingResult(
        tuple(Candidate(test_id, tuple(sorted(reasons))) for test_id, reasons in sorted(candidates.items())),
        tuple(sorted(categories)),
        confidence,
        True,
        tuple(sorted(fallback)),
        tuple(impacts),
    )

