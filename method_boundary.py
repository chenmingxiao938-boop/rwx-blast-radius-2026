"""Strict old/new AST check for one existing class method body."""

import ast
import copy

from patch_parser import FileChange


def _replay(source: str, change: FileChange) -> str | None:
    old = source.splitlines()
    result = []
    cursor = 0
    for hunk in change.hunks:
        start = hunk.old_start - (1 if hunk.old_count else 0)
        if start < cursor or start > len(old):
            return None
        result.extend(old[cursor:start])
        new_start = hunk.new_start - (1 if hunk.new_count else 0)
        if len(result) != new_start:
            return None
        consumed = produced = 0
        for line in hunk.body:
            marker, content = line[:1], line[1:]
            if marker in (" ", "-"):
                if start + consumed >= len(old) or old[start + consumed] != content:
                    return None
                consumed += 1
            if marker in (" ", "+"):
                result.append(content)
                produced += 1
            if marker not in (" ", "-", "+"):
                return None
        if consumed != hunk.old_count or produced != hunk.new_count:
            return None
        cursor = start + consumed
    result.extend(old[cursor:])
    return "\n".join(result) + ("\n" if source.endswith("\n") else "")


def _methods(tree: ast.Module) -> dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] | None:
    found = {}
    classes = set()
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        if cls.name in classes:
            return None
        classes.add(cls.name)
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = (cls.name, node.name)
                if key in found:
                    return None
                found[key] = node
    return found


def _outside_body(tree: ast.Module, key: tuple[str, str]) -> str:
    clone = copy.copy(tree)
    clone.body = list(tree.body)
    for class_index, cls in enumerate(tree.body):
        if isinstance(cls, ast.ClassDef) and cls.name == key[0]:
            masked_class = copy.copy(cls)
            masked_class.body = list(cls.body)
            clone.body[class_index] = masked_class
            for method_index, node in enumerate(cls.body):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == key[1]:
                    masked_method = copy.copy(node)
                    masked_method.body = [ast.Pass()]
                    masked_class.body[method_index] = masked_method
    return ast.dump(clone, include_attributes=False)


def _touches_structure(method, changed: set[int]) -> bool:
    for node in ast.walk(method):
        if node is method or not isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef,
                                                   ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(node.lineno <= line <= node.end_lineno for line in changed):
            return True
    return False


def _empty_side_anchors(hunk, side: str) -> tuple[int, ...]:
    old_line, new_line = hunk.old_start, hunk.new_start
    anchors = []
    for line in hunk.body:
        if line.startswith("+"):
            if side == "old":
                anchors.append(old_line - 1)
            new_line += 1
        elif line.startswith("-"):
            if side == "new":
                anchors.append(new_line - 1)
            old_line += 1
        else:
            old_line += 1
            new_line += 1
    return tuple(anchors)


def method_body_symbol(change: FileChange, source: str, patch_text: str) -> str | None:
    """Return Class.method only when exact patch replay proves body confinement."""
    if (change.unknown_reason or change.kind != "modified" or not change.hunks
            or change.old_path != change.new_path
            or "\\ No newline at end of file" in patch_text
            or any(line.startswith(("old mode ", "new mode "))
                   for line in patch_text.splitlines())):
        return None
    updated = _replay(source, change)
    if updated is None:
        return None
    try:
        old_tree = ast.parse(source)
        new_tree = ast.parse(updated)
    except (SyntaxError, ValueError):
        return None
    old_methods, new_methods = _methods(old_tree), _methods(new_tree)
    if old_methods is None or new_methods is None:
        return None
    old_changed = {line for hunk in change.hunks for line in hunk.old_changed_lines}
    new_changed = {line for hunk in change.hunks for line in hunk.new_changed_lines}
    old_lines, new_lines = source.splitlines(), updated.splitlines()
    candidates = []
    for key in old_methods.keys() & new_methods.keys():
        old_method, new_method = old_methods[key], new_methods[key]
        # A one-line header is required so its colon is unambiguous.
        if (not old_lines[old_method.lineno - 1].rstrip().endswith(":")
                or not new_lines[new_method.lineno - 1].rstrip().endswith(":")):
            continue
        if not all(old_method.lineno < line <= old_method.end_lineno for line in old_changed):
            continue
        if not all(new_method.lineno < line <= new_method.end_lineno for line in new_changed):
            continue
        if any(not hunk.old_changed_lines and not all(
               old_method.lineno <= anchor < old_method.end_lineno
               for anchor in _empty_side_anchors(hunk, "old"))
               for hunk in change.hunks):
            continue
        if any(not hunk.new_changed_lines and not all(
               new_method.lineno <= anchor < new_method.end_lineno
               for anchor in _empty_side_anchors(hunk, "new"))
               for hunk in change.hunks):
            continue
        if (_touches_structure(old_method, old_changed)
                or _touches_structure(new_method, new_changed)):
            continue
        if _outside_body(old_tree, key) != _outside_body(new_tree, key):
            continue
        candidates.append(".".join(key))
    return candidates[0] if len(candidates) == 1 else None


def requires_structural_fallback(change: FileChange, source: str) -> bool:
    """Reject edits to imports, definitions, module/class bodies or invalid ASTs."""
    updated = _replay(source, change)
    if updated is None:
        return True
    try:
        versions = ((ast.parse(source), {line for h in change.hunks for line in h.old_changed_lines}),
                    (ast.parse(updated), {line for h in change.hunks for line in h.new_changed_lines}))
    except (SyntaxError, ValueError):
        return True
    for tree, changed in versions:
        functions = [node for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                    node.lineno <= line <= node.end_lineno for line in changed):
                return True
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                header_start = min((decorator.lineno for decorator in node.decorator_list),
                                   default=node.lineno)
                body_start = node.body[0].lineno if node.body else node.end_lineno + 1
                if any(header_start <= line < body_start for line in changed):
                    return True
        if any(not any(node.body and node.body[0].lineno <= line <= node.end_lineno
                       for node in functions) for line in changed):
            return True
    return False
