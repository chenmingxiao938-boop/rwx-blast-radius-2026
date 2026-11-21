"""Research-only provenance for existing module-level dependency candidates.

No edge is removed and no test is authorized for exclusion. The evidence-only
subset is diagnostic: Python imports and module initializers can have effects
even when a symbol is not referenced in the importing file.
"""

from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path
from typing import Mapping, Sequence

from dependency_map import DependencyMap, Uncertainty
from test_mapper import _test_index


@dataclass(frozen=True)
class EdgeEvidence:
    importer: str
    dependency: str
    kind: str
    lines: tuple[int, ...]
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class ModulePrecision:
    path: str
    current_candidate_numbers: tuple[int, ...]
    combined_candidate_numbers: tuple[int, ...]
    direct_candidate_numbers: tuple[int, ...]
    transitive_candidate_numbers: tuple[int, ...]
    evidence_candidate_numbers: tuple[int, ...]
    conservative_candidate_numbers: tuple[int, ...]
    direct_importers: tuple[str, ...]
    transitive_importers_count: int
    shared_base_importers: tuple[str, ...]
    first_hop_edges: tuple[EdgeEvidence, ...]
    example_chains: tuple[tuple[str, ...], ...]
    global_uncertainties: tuple[Uncertainty, ...]
    path_uncertainties: tuple[Uncertainty, ...]
    changed_module_uncertainties: tuple[Uncertainty, ...]
    dynamic_unresolved: tuple[str, ...]
    fallback_required: bool = True


_EVIDENCE_KINDS = frozenset(("symbol_reference", "class_inheritance", "dispatch_assignment",
                             "dynamic_literal", "lazy_export_import"))


def _module(path: str) -> str:
    name = path.removesuffix(".py").replace("/", ".")
    return name.removesuffix(".__init__")


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _reverse_paths(graph: DependencyMap, target: str) -> dict[str, str | None]:
    parents: dict[str, str | None] = {target: None}
    queue = deque((target,))
    while queue:
        child = queue.popleft()
        for importer in graph._importers.get(child, ()):
            if importer not in parents:
                parents[importer] = child
                queue.append(importer)
    return parents


class _SourceEvidence:
    def __init__(self, root: Path):
        self.root = root
        self.cache: dict[str, ast.Module | None] = {}

    def tree(self, path: str) -> ast.Module | None:
        if path not in self.cache:
            try:
                self.cache[path] = ast.parse((self.root / path).read_text(encoding="utf-8"), filename=path)
            except (OSError, UnicodeError, SyntaxError):
                self.cache[path] = None
        return self.cache[path]

    def edge(self, importer: str, dependency: str) -> EdgeEvidence:
        tree = self.tree(importer)
        if tree is None:
            return EdgeEvidence(importer, dependency, "unknown_source", (), ())
        target = _module(dependency)
        package = _module(importer) if importer.endswith("/__init__.py") else _module(importer).rpartition(".")[0]
        lines: set[int] = set()
        symbols: set[str] = set()
        aliases: set[str] = set()
        sqlglot_heads: set[str] = set()
        for imported in ast.walk(tree):
            if isinstance(imported, ast.Import):
                sqlglot_heads.update(alias.asname or "sqlglot" for alias in imported.names
                                     if alias.name == "sqlglot" or alias.name.startswith("sqlglot."))
            elif isinstance(imported, ast.ImportFrom) and imported.module and imported.module.startswith("sqlglot"):
                sqlglot_heads.update(alias.asname or alias.name for alias in imported.names)
        dynamic = False
        dialect_literal = False
        lazy_export = False
        lazy_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == target or alias.name.startswith(target + "."):
                        lines.add(node.lineno)
                        aliases.add(alias.asname or alias.name.split(".", 1)[0])
                        symbols.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    try:
                        module = resolve_name("." * node.level + module, package)
                    except ImportError:
                        continue
                if module == target:
                    lines.add(node.lineno)
                    for alias in node.names:
                        aliases.add(alias.asname or alias.name)
                        symbols.add(alias.name)
                elif target.startswith(module + "."):
                    child = target[len(module) + 1:]
                    for alias in node.names:
                        if alias.name == child or (module == "sqlglot.dialects" and
                                                    alias.name.lower() == child.lower()):
                            lines.add(node.lineno)
                            aliases.add(alias.asname or alias.name)
                            symbols.add(alias.name)
                            lazy_export |= module == "sqlglot.dialects" and alias.name.lower() == child.lower()
                            if module == "sqlglot.dialects" and alias.name.lower() == child.lower():
                                lazy_lines.add(node.lineno)
            elif isinstance(node, ast.Call) and _dotted(node.func) in ("importlib.import_module", "__import__"):
                if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == target:
                    lines.add(node.lineno)
                    dynamic = True
            elif isinstance(node, ast.Call) and target.startswith("sqlglot.dialects."):
                function = _dotted(node.func)
                if function and function.split(".", 1)[0] in sqlglot_heads:
                    values = [argument.value for argument in node.args[1:3]
                              if function.rsplit(".", 1)[-1] in ("parse_one", "parse", "transpile", "tokenize")
                              and isinstance(argument, ast.Constant) and isinstance(argument.value, str)]
                    values.extend(keyword.value.value for keyword in node.keywords
                                  if keyword.arg in ("read", "write", "dialect")
                                  and isinstance(keyword.value, ast.Constant)
                                  and isinstance(keyword.value.value, str))
                    if any(value.split(",", 1)[0].lower() == target.rsplit(".", 1)[-1] for value in values):
                        lines.add(node.lineno)
                        dialect_literal = True
        if not lines:
            kind = "implicit_package" if _module(importer).startswith(target + ".") else "unexplained_graph_edge"
            return EdgeEvidence(importer, dependency, kind, (), ())
        if dynamic:
            return EdgeEvidence(importer, dependency, "dynamic_literal", tuple(sorted(lines)), tuple(sorted(symbols)))
        if lazy_export:
            return EdgeEvidence(importer, dependency, "lazy_export_import", tuple(sorted(lazy_lines)), tuple(sorted(symbols)))
        if dialect_literal and not aliases:
            return EdgeEvidence(importer, dependency, "dialect_literal", tuple(sorted(lines)), ())
        inheritance = False
        assignment = False
        used = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                inheritance |= any((_dotted(base) or "").split(".", 1)[0] in aliases for base in node.bases)
            if isinstance(node, ast.Assign):
                assignment |= any(isinstance(target_node, ast.Name) and target_node.id in ("Parser", "Generator")
                                  for target_node in node.targets) and isinstance(node.value, ast.Name) and node.value.id in aliases
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in aliases:
                used = True
        kind = ("dispatch_assignment" if assignment else "class_inheritance" if inheritance else
                "symbol_reference" if used else "explicit_import_only")
        return EdgeEvidence(importer, dependency, kind, tuple(sorted(lines)), tuple(sorted(symbols)))


def analyze_graph_precision(
    changed_paths: Sequence[str], metadata: Mapping[str, object], graph: DependencyMap,
    source_root: str | Path, *, dynamic_candidates: Mapping[str, Sequence[int]] | None = None,
    dynamic_unresolved: Sequence[str] = (),
) -> tuple[ModulePrecision, ...]:
    """Describe import-chain provenance; retain every original candidate.

    dynamic_candidates are positive IDs from an independent source-derived map.
    The evidence subset is informational and never controls fallback.
    """
    _test_index(metadata)
    tests_by_path: dict[str, set[int]] = {}
    for entry in metadata["tests"]:
        tests_by_path.setdefault(entry["file"], set()).add(entry["number"])
    source = _SourceEvidence(Path(source_root))
    output: list[ModulePrecision] = []
    for path in sorted(set(changed_paths)):
        if path not in graph.direct_dependencies:
            raise ValueError(f"changed path absent from graph: {path}")
        parents = _reverse_paths(graph, path)
        # The shortest graph path is evidence of reachability, not execution.
        direct = set()
        transitive = set()
        chains = []
        for test_path, numbers in sorted(tests_by_path.items()):
            if test_path not in parents:
                continue
            chain = [test_path]
            while parents[chain[-1]] is not None:
                chain.append(parents[chain[-1]])
            chains.append(tuple(chain))
            (direct if len(chain) == 2 else transitive).update(numbers)
        # A second reverse walk follows only symbol/registry evidence. It is a
        # diagnostic lower bound, never a proposed test execution set.
        verified = {path}
        queue = deque((path,))
        while queue:
            dependency = queue.popleft()
            for importer in graph._importers.get(dependency, ()):
                if importer in verified:
                    continue
                evidence = source.edge(importer, dependency)
                if evidence.kind in _EVIDENCE_KINDS:
                    verified.add(importer)
                    queue.append(importer)
        module_candidates = direct | transitive
        evidence_numbers = {number for test_path, numbers in tests_by_path.items()
                            if test_path in verified for number in numbers}
        dynamic = set((dynamic_candidates or {}).get(path, ()))
        valid_numbers = {entry["number"] for entry in metadata["tests"]}
        if not dynamic <= valid_numbers:
            raise ValueError("dynamic candidate outside official manifest")
        evidence_numbers.update(dynamic)
        combined = module_candidates | dynamic
        direct_importers = tuple(sorted(graph._importers.get(path, ())))
        first_hop = tuple(source.edge(importer, path) for importer in direct_importers)
        path_issues = tuple(issue for issue in graph.uncertainties if issue.path in parents)
        changed_issues = tuple(issue for issue in graph.uncertainties if issue.path == path)
        output.append(ModulePrecision(
            path, tuple(sorted(module_candidates)), tuple(sorted(combined)),
            tuple(sorted(direct)), tuple(sorted(transitive)),
            tuple(sorted(evidence_numbers)), tuple(sorted(combined - evidence_numbers)),
            direct_importers, len(parents) - len(direct_importers) - 1,
            tuple(edge.importer for edge in first_hop if edge.kind == "class_inheritance"),
            first_hop, tuple(chains[:5]), graph.uncertainties, path_issues,
            changed_issues, tuple(sorted(set(dynamic_unresolved))), True,
        ))
    return tuple(output)
