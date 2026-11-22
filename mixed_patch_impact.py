"""Opt-in extension of V5 fixture selection to mixed comment-only patches."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from dataclasses import replace

from impact_selector import Decision, PINNED_COMMIT
from method_boundary import _replay
from patch_parser import FileChange, parse_patch


_BLOCK = re.compile(r"(?m)(?=^diff --git )")
_SPECIAL_HEADER = re.compile(r"(?m)^(?:old mode |new mode |\\ No newline at end of file)")
_DIRECTIVE = re.compile(r"#!|#.*\bcoding\s*[:=]|#.*fileencoding\s*[:=]|#\s*(?:type\s*:|pragma\b)", re.I)


def _is_sql_fixture(path: str) -> bool:
    return path.startswith("tests/fixtures/") and path.endswith(".sql")


def _ordinary_comments(source: str, changed: set[int]) -> bool:
    lines = source.splitlines()
    comments = {
        start[0]
        for kind, value, start, _, _ in tokenize.generate_tokens(io.StringIO(source).readline)
        if kind == tokenize.COMMENT and not lines[start[0] - 1][:start[1]].strip()
        and not _DIRECTIVE.match(value)
    }
    return changed.issubset(comments)


def is_comment_only(change: FileChange, source: str) -> bool:
    """Check exact replay, comment tokens, and old/new AST equality."""
    if (change.unknown_reason or change.kind != "modified" or not change.hunks
            or not change.old_path or change.old_path != change.new_path):
        return False
    updated = _replay(source, change)
    if updated is None:
        return False
    old_lines = {line for hunk in change.hunks for line in hunk.old_changed_lines}
    new_lines = {line for hunk in change.hunks for line in hunk.new_changed_lines}
    if not (old_lines or new_lines):
        return False
    try:
        if not _ordinary_comments(source, old_lines) or not _ordinary_comments(updated, new_lines):
            return False
        return ast.dump(ast.parse(source, type_comments=True)) == ast.dump(
            ast.parse(updated, type_comments=True)
        )
    except (SyntaxError, ValueError, tokenize.TokenError):
        return False


def analyze_mixed_patch(patch_text, manifest, coverage, ranges, dependencies, *,
                        baseline_analyze, fixture_mode="baseline", method_mode="baseline",
                        source_snapshot=None) -> Decision:
    """Run V5 unchanged, then optionally narrow a verified mixed fallback.

    Enabling this wrapper accepts the same observed-reader limitations as
    V5's fixture-only mode, plus the residual risk of source introspection.
    """
    options = dict(fixture_mode=fixture_mode, method_mode=method_mode, source_snapshot=source_snapshot)
    baseline = baseline_analyze(patch_text, manifest, coverage, ranges, dependencies, **options)
    if (not baseline.fallback_required or fixture_mode != "aggressive_fixture_only"
            or not source_snapshot or source_snapshot.get("pinned_commit") != PINNED_COMMIT
            or _SPECIAL_HEADER.search(patch_text)):
        return baseline
    patch = parse_patch(patch_text)
    if patch.unknown:
        return baseline
    if not any(_is_sql_fixture(path) for path in patch.changed_paths):
        return baseline  # Never project or replay Python-only patches in mixed mode.
    sources = source_snapshot.get("sources", {})
    fixture_blocks = []
    blocks = [block for block in _BLOCK.split(patch_text) if block]
    # Verify the projection against the V5 parse before relying on any block.
    if len(blocks) != len(patch.files):
        return baseline
    seen_paths = set()
    python_paths = []
    for block, change in zip(blocks, patch.files):
        path = change.old_path
        parsed_block = parse_patch(block)
        if (parsed_block.unknown or parsed_block.files != (change,) or not path
                or path != change.new_path or path in seen_paths):
            return baseline
        seen_paths.add(path)
        if _is_sql_fixture(path):
            fixture_blocks.append(block)
        elif (path.startswith("sqlglot/") and path.endswith(".py")
                and path in sources and is_comment_only(change, sources[path])):
            python_paths.append(path)
        else:
            return baseline
    if not (python_paths and fixture_blocks):
        return baseline
    fixture_result = baseline_analyze("".join(fixture_blocks), manifest, coverage, ranges,
                                      dependencies, **options)
    if fixture_result.fallback_required or len(fixture_result.test_numbers) >= len(baseline.test_numbers):
        return baseline
    uncertainty = set(fixture_result.uncertainty) | {
        f"comment_only_source_positions_or_introspection_may_change:{path}" for path in python_paths
    }
    return replace(fixture_result, uncertainty=tuple(sorted(uncertainty)))
