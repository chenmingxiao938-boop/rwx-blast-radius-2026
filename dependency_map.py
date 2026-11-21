"""Static Python dependency evidence for Blast Radius test selection.

Build once from a source snapshot, then pass repository-relative changed paths
and the official tests.json `tests` entries to DependencyMap.analyze(). This
module never runs or imports the analyzed project.
"""

from __future__ import annotations

import ast
import tokenize
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import NamedTuple


class Uncertainty(NamedTuple):
    path: str
    line: int
    detail: str


class SelectionReason(NamedTuple):
    kind: str
    chain: tuple[str, ...]


@dataclass(frozen=True)
class ImpactResult:
    candidate_test_ids: tuple[int, ...]
    reasons: Mapping[int, tuple[SelectionReason, ...]]
    uncertainties: tuple[Uncertainty, ...]
    full_suite_fallback: bool


def _path(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/"))
    if not normalized.parts or normalized.is_absolute() or ".." in normalized.parts or ":" in normalized.parts[0]:
        raise ValueError(f"expected a repository-relative path: {path!r}")
    return normalized.as_posix()


def _name(path: str) -> str:
    parts = PurePosixPath(path).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _parents(name: str) -> Iterable[str]:
    while "." in name:
        name = name.rpartition(".")[0]
        yield name


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _Imports:
    """Resolve one module's imports against the indexed snapshot."""

    _BY_NAME = {"importlib.import_module", "builtins.__import__", "__import__", "runpy.run_module"}
    _BY_PATH = {
        "importlib.util.spec_from_file_location", "importlib.machinery.SourceFileLoader", "runpy.run_path"
    }

    def __init__(self, path, modules, known_names, roots):
        self.path = path
        self.modules = modules
        self.known_names = known_names
        self.roots = roots
        module = _name(path)
        self.package = module if PurePosixPath(path).name == "__init__.py" else module.rpartition(".")[0]
        self.edges = set()
        self.issues = set()

    def problem(self, line, message):
        self.issues.add(Uncertainty(self.path, line, message))

    def add(self, name, line):
        if name in self.known_names:
            self.edges.update(self.modules.get(name, ()))
        elif name.partition(".")[0] in self.roots:
            self.problem(line, f"unresolved local import {name}")

    def relative(self, name, package, line):
        try:
            return resolve_name(name, package)
        except ImportError:
            self.problem(line, f"cannot resolve relative import {name}")
            return None

    def scan(self, tree):
        nodes = tuple(ast.walk(tree))
        aliases = defaultdict(set)
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.add(alias.name, node.lineno)
                    root = alias.name.partition(".")[0]
                    aliases[alias.asname or root].add(alias.name if alias.asname else root)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = self.relative("." * node.level + module, self.package, node.lineno)
                if module:
                    self.add(module, node.lineno)
                    for alias in node.names:
                        name = f"{module}.{alias.name}"
                        namespace_member = (
                            module in self.known_names and module not in self.modules and alias.name != "*"
                        )
                        if name in self.known_names or namespace_member:
                            self.add(name, node.lineno)
                        aliases[alias.asname or alias.name].add(name)

        for node in nodes:
            if isinstance(node, ast.Call):
                function = _dotted(node.func)
                head, separator, tail = function.partition(".")
                for base in aliases.get(head, (head,)):
                    self.dynamic(node, base + separator + tail)

        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = (node.target,)
            else:
                continue
            if any(isinstance(target, ast.Name) and target.id == "pytest_plugins" for target in targets):
                entries = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else (node.value,)
                if entries and self.path != "conftest.py":
                    self.problem(node.lineno, "pytest plugin registration may affect other test files")
                for entry in entries:
                    plugin = _literal(entry)
                    if plugin is None:
                        self.problem(node.lineno, "nonliteral pytest plugin")
                    elif plugin in self.known_names:
                        self.add(plugin, node.lineno)
                    else:
                        self.problem(node.lineno, f"unindexed pytest plugin {plugin}")

    def dynamic(self, node, function):
        if function in self._BY_PATH:
            self.problem(node.lineno, f"path-based loader {function}")
        if function not in self._BY_NAME:
            return
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        target = _literal(node.args[0] if node.args else keywords.get("name", keywords.get("mod_name")))
        if not target or None in keywords:
            self.problem(node.lineno, f"nonliteral target for {function}")
            return
        if function in {"__import__", "builtins.__import__"}:
            level = node.args[4] if len(node.args) > 4 else keywords.get("level", ast.Constant(0))
            if not isinstance(level, ast.Constant) or level.value != 0:
                self.problem(node.lineno, f"unsupported relative level for {function}")
                return
            self.add(target, node.lineno)
            fromlist = node.args[3] if len(node.args) > 3 else keywords.get("fromlist")
            if isinstance(fromlist, (ast.List, ast.Tuple)):
                for entry in fromlist.elts:
                    member = _literal(entry)
                    if member is None and target.partition(".")[0] in self.roots:
                        self.problem(node.lineno, f"nonliteral fromlist for {function}")
                    if member is not None and f"{target}.{member}" in self.known_names:
                        self.add(f"{target}.{member}", node.lineno)
            elif fromlist is not None and target.partition(".")[0] in self.roots:
                self.problem(node.lineno, f"nonliteral fromlist for {function}")
            return
        if target.startswith("."):
            package_node = node.args[1] if len(node.args) > 1 else keywords.get("package")
            package = self.package if _dotted(package_node) == "__package__" else _literal(package_node)
            target = self.relative(target, package, node.lineno)
        if target:
            self.add(target, node.lineno)


@dataclass(frozen=True)
class DependencyMap:
    """Read-only import edges, their reverse index, and static-analysis diagnostics."""

    direct_dependencies: Mapping[str, tuple[str, ...]]
    _importers: Mapping[str, tuple[str, ...]]
    uncertainties: tuple[Uncertainty, ...]

    def transitive_dependencies(self, path: str) -> tuple[str, ...]:
        """Return every reachable dependency of a known path, excluding itself."""
        path = _path(path)
        if path not in self.direct_dependencies:
            raise KeyError(path)
        return tuple(sorted(set(_walk(self.direct_dependencies, (path,))) - {path}))

    def analyze(self, changed_paths: Iterable[str], tests: Iterable[Mapping]) -> ImpactResult:
        """Return positive candidates with shortest evidence paths and fallback flags.

        Pass the full official inventory. A true fallback means the caller must
        use all inventory IDs. A false flag only says this scanner found no
        specific blocker. It does not establish that other tests are unaffected.
        """
        tests_by_path = defaultdict(set)
        seen = set()
        issues = set()
        for test in tests:
            number = test["number"]
            if type(number) is not int or number is None or number in seen:
                raise ValueError(f"expected a unique nonnegative test number, got {number!r}")
            seen.add(number)
            try:
                path = _path(test["file"])
            except (AttributeError, TypeError, ValueError):
                issues.add(Uncertainty(repr(test["file"]), 0, "invalid test metadata path"))
                continue
            tests_by_path[path].add(number)
            if path not in self.direct_dependencies:
                issues.add(Uncertainty(path, 0, "test file missing from the dependency map"))

        changed = set()
        for path in changed_paths:
            try:
                changed.add(_path(path))
            except (AttributeError, TypeError, ValueError):
                issues.add(Uncertainty(repr(path), 0, "invalid changed path"))
        if not changed and not issues:
            issues.add(Uncertainty("", 0, "no changed paths supplied"))
        reachable = _walk(self.direct_dependencies, sorted(tests_by_path))
        issues.update(issue for issue in self.uncertainties if issue.path in reachable or issue.path in changed)
        reasons = defaultdict(list)
        for path in sorted(changed):
            if path not in self.direct_dependencies:
                issues.add(Uncertainty(path, 0, "changed path missing from the dependency map"))
                continue
            parents = _walk(self._importers, (path,))
            matched = False
            for test_path, numbers in tests_by_path.items():
                if test_path not in parents:
                    continue
                matched = True
                chain = [test_path]
                while parents[chain[-1]] is not None:
                    chain.append(parents[chain[-1]])
                kind = {1: "same-file", 2: "direct"}.get(len(chain), "indirect")
                reason = SelectionReason(kind, tuple(chain))
                for number in numbers:
                    reasons[number].append(reason)
            if not matched:
                issues.add(Uncertainty(path, 0, "no test dependency found for changed path"))

        ids = tuple(sorted(reasons))
        return ImpactResult(ids, MappingProxyType({n: tuple(reasons[n]) for n in ids}), tuple(sorted(issues)), bool(issues))


def _walk(edges, starts):
    parents = dict.fromkeys(starts)
    queue = deque(parents)
    while queue:
        path = queue.popleft()
        for neighbor in edges.get(path, ()):
            if neighbor not in parents:
                parents[neighbor] = path
                queue.append(neighbor)
    return parents


def build_dependency_map(sources: Mapping[str, str | None]) -> DependencyMap:
    """Index repository-relative Python files. Use None for an unreadable file."""
    normalized = {_path(path): source for path, source in sources.items()}
    if len(normalized) != len(sources) or any(not path.endswith(".py") for path in normalized):
        raise ValueError("source paths must be distinct Python files")
    modules = defaultdict(set)
    for path in normalized:
        modules[_name(path)].add(path)
 known_names = set(modules)
    for module in modules:
 known_names.update(_parents(module))
 roots = { ame.partition(".")[0] for ame in modules}
 edges = {}
 reverse = defaultdict(set)
 issues = set()
    for path, source in sorted( ormalized.items()):
 imports = _Imports(path, modules, known_names, roots)
        for parent in _parents(_name(path)):
 imports.edges.update(modules.get(parent, ()))
 parts = PurePosixPath(path).parts
        if parts[0] == "tests" or parts[-1].startswith("test_") or parts[-1].endswith("_test.py"):
            for end in range(len(parts)):
 fixture = "/".join((*parts[:end], "conftest.py"))
                if fixture in ormalized:
 imports.edges.add(fixture)
        if source is None:
 imports.problem(0, "unreadable Python source")
        else:
            try:
 imports.scan(ast.parse(source, filename=path))
            except SyntaxError as error:
 imports.problem(error.lineno or 0, "could not parse Python source")
 imports.edges.discard(path)
 edges[path] = tuple(sorted(imports.edges))
        for dependency in imports.edges:
 reverse[dependency].add(path)
 issues.update(imports.issues)

    return DependencyMap(
        MappingProxyType(edges),
        MappingProxyType({p: tuple(sorted(reverse[p])) for p in sorted(reverse)}),
        tuple(sorted(issues)),
    )


def scan_dependency_map(root: Path | str, packages: Iterable[str] = ("sqlglot", "tests")) -> DependencyMap:
    """Read Python sources in the named trees and an optional root conftest.py."""
 root = Path(root)
 sources = {}
 paths = [path for package in packages for path in (root / _path(package)).rglob("*.py")]
    if (root / "conftest.py").is_file():
 paths.append(root / "conftest.py")
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
 relative = path.relative_to(root).as_posix()
        try:
            with tokenize.open(path) as source:
 sources[relative] = source.read()
        except (OSError, SyntaxError, UnicodeError):
 sources[relative] = None
    return build_dependency_map(sources)
