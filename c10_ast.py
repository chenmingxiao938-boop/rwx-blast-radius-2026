"""Positive AST/coverage evidence for C10-A; no dependency completeness claim."""

import ast
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath

from method_boundary import _replay

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFINITIONS = (*FUNCTIONS, ast.ClassDef)


@lru_cache(maxsize=256)
def parse(source):
    return ast.parse(source, type_comments=True)


def definitions(tree, prefix=""):
    for node in tree.body:
        if isinstance(node, DEFINITIONS):
            name = prefix + node.name
            yield name, node
            if isinstance(node, ast.ClassDef):
                yield from definitions(node, name + ".")


def _limits(node):
    return min((d.lineno for d in node.decorator_list), default=node.lineno), node.end_lineno


def _body_owner(defs, line):
    return any(isinstance(node, FUNCTIONS) and node.body[0].lineno <= line <= node.end_lineno
               for _, node in defs)


def _statement_lines(tree, line):
    """Use the narrowest old AST statement, without sibling branch bodies."""
    statements = [node for node in ast.walk(tree) if isinstance(node, ast.stmt)
                  and node.lineno <= line <= node.end_lineno]
    if not statements:
        return (line,)
    node = min(statements, key=lambda item: (item.end_lineno - item.lineno, -item.col_offset))
    if isinstance(getattr(node, "body", None), list):
        return (line,)
    return range(node.lineno, node.end_lineno + 1)


@dataclass
class Region:
    symbols: set[str]
    old_lines: set[int]
    branch_lines: set[int]
    structure_risk: bool
    text_only: bool


def analyze_region(change, source):
    patched = _replay(source, change)
    if patched is None:
        raise ValueError(f"hunk does not match pinned source: {change.old_path}")
    before, after = parse(source), parse(patched)
    before_defs, after_defs = list(definitions(before)), list(definitions(after))
    old_lines, new_lines, branches = set(), set(), set()
    before_names = {name for name, _ in before_defs}
    symbols = set()
    for hunk in change.hunks:
        actual_old = set(hunk.old_changed_lines)
        old = actual_old or _insertion_anchors(hunk)
        new = set(hunk.new_changed_lines)
        old_lines.update(old)
        new_lines.update(new)
        for line in old:
            branches.update(_statement_lines(before, line))
        for lines, defs in ((old, before_defs), (new, after_defs)):
            for line in lines:
                owners = [(name, _limits(node)) for name, node in defs
                          if _limits(node)[0] <= line <= _limits(node)[1]]
                if owners:
                    name, _ = min(owners, key=lambda row: row[1][1] - row[1][0])
                    if name in before_names:
                        symbols.add(name)
    text_only = ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False)
    all_old_changes = {line for hunk in change.hunks for line in hunk.old_changed_lines}
    structure = (not text_only and (not old_lines or not new_lines and not all_old_changes
                 or any(not _body_owner(before_defs, line) for line in all_old_changes)
                 or any(not _body_owner(after_defs, line) for line in new_lines)))
    return Region(symbols, old_lines, branches, structure, text_only)


def _insertion_anchors(hunk):
    cursor = hunk.old_start if hunk.old_count else hunk.old_start + 1
    result = set()
    for line in hunk.body:
        if line.startswith("+"):
            result.update((cursor - 1, cursor))
        elif line.startswith(("-", " ")):
            cursor += 1
    return {line for line in result if line > 0}


def _module(path):
    parts = PurePosixPath(path).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(tree, file):
    result = {}
    package = _module(file).split(".")
    if not file.endswith("/__init__.py"):
        package.pop()
    for item in tree.body:
        if isinstance(item, ast.ImportFrom):
            parent = (package[:len(package) - item.level + 1] if item.level else [])
            if item.module:
                parent = [*parent, *item.module.split(".")]
            for alias in item.names:
                if alias.name != "*":
                    result[alias.asname or alias.name] = ".".join([*parent, alias.name])
        elif isinstance(item, ast.Import):
            for alias in item.names:
                root = alias.name.split(".")[0]
                result[alias.asname or root] = alias.name if alias.asname else root
    return result


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (base := _dotted(node.value)):
        return base + "." + node.attr
    return None


def call_index(sources):
    """Index locally resolvable calls, including imported function aliases."""
    modules = {_module(path): path for path in sources}
    reverse = defaultdict(set)
    for path, source in sorted(sources.items()):
        tree = parse(source)
        imports = _imports(tree, path)
        defs = dict(definitions(tree))
        for owner, node in defs.items():
            if not isinstance(node, FUNCTIONS):
                continue
            for call in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if not isinstance(call, ast.Call) or not (name := _dotted(call.func)):
                    continue
                first, _, tail = name.partition(".")
                if first in ("self", "cls") and tail and "." in owner:
                    target = (path, owner.rsplit(".", 1)[0] + "." + tail)
                    if target[1] not in defs:
                        continue
                elif name in defs:
                    target = (path, name)
                elif first in imports:
                    parts = (imports[first] + ("." + tail if tail else "")).split(".")
                    target = None
                    for cut in range(len(parts) - 1, 0, -1):
                        if module_path := modules.get(".".join(parts[:cut])):
                            target = (module_path, ".".join(parts[cut:]))
                            break
                    if target is None:
                        continue
                else:
                    continue
                reverse[target].add((path, owner))
    return reverse


def caller_distances(index, targets, depth=2):
    distances = {}
    frontier = set(targets)
    seen = set(frontier)
    for distance in range(1, depth + 1):
        frontier = {caller for key in frontier for caller in index.get(key, ())} - seen
        distances.update(dict.fromkeys(frontier, distance))
        seen.update(frontier)
    return distances
