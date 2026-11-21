"""Research-only, source-derived evidence for SQLGlot dialect dispatch.

Candidates are positive evidence. This analysis does not prove that omitted
tests are unaffected, so every result retains full-suite fallback.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from test_mapper import _test_index


@dataclass(frozen=True)
class DialectEntry:
    name: str
    dialect_module: str
    parser_module: str | None
    generator_module: str | None


@dataclass(frozen=True)
class DialectEvidence:
    test_id: str
    test_number: int
    dialect: str
    role: str
    source_path: str
    line: int
    mechanism: str
    implementation_modules: tuple[str, ...]


@dataclass(frozen=True)
class DialectImpact:
    path: str
    candidate_ids: tuple[str, ...]
    candidate_numbers: tuple[int, ...]
    evidence: tuple[DialectEvidence, ...]
    referenced_dialects: tuple[str, ...]
    unresolved: tuple[str, ...]


@dataclass(frozen=True)
class DialectMappingResult:
    candidate_ids: tuple[str, ...]
    candidate_numbers: tuple[int, ...]
    file_impacts: tuple[DialectImpact, ...]
    unresolved: tuple[str, ...]
    global_unresolved: tuple[str, ...]
    fallback_required: bool = True


@dataclass(frozen=True)
class _Reference:
    dialect: str
    role: str
    source_path: str
    line: int
    mechanism: str


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _names(node: ast.AST | None, env: Mapping[str, tuple[str, ...]], self_dialect: str | None) -> tuple[str, ...]:
    literal = _literal(node)
    if literal is not None:
        return (literal.split(",", 1)[0].strip().lower(),)
    if isinstance(node, ast.Name):
        return env.get(node.id, ())
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self" and node.attr == "dialect" and self_dialect):
        return (self_dialect,)
    return ()


def _name_set(node: ast.AST, env: Mapping[str, tuple[str, ...]], self_dialect: str | None) -> tuple[str, ...]:
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values = [_names(item, env, self_dialect) for item in node.elts]
        return tuple(sorted({name for group in values for name in group})) if all(values) else ()
    if isinstance(node, ast.Dict):
        values = [_names(item, env, self_dialect) for item in node.keys]
        return tuple(sorted({name for group in values for name in group})) if all(values) else ()
    return _names(node, env, self_dialect)


def _registry(root: Path) -> tuple[dict[str, DialectEntry], list[str]]:
    path = root / "sqlglot/dialects/__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "DIALECTS" for target in node.targets):
            if isinstance(node.value, (ast.List, ast.Tuple)):
                names = [value.value.lower() for value in node.value.elts if isinstance(value, ast.Constant) and isinstance(value.value, str)]
            break
    if not names:
        raise ValueError("pinned dialect registry has no static DIALECTS list")
    entries: dict[str, DialectEntry] = {}
    unresolved: list[str] = []
    for name in sorted(set(names)):
        module = f"sqlglot/dialects/{name}.py"
        try:
            tree = ast.parse((root / module).read_text(encoding="utf-8"), filename=module)
        except (OSError, UnicodeError, SyntaxError) as exc:
            unresolved.append(f"Cannot inspect registry module {module}: {type(exc).__name__}")
            continue
        imports: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("sqlglot."):
                for alias in node.names:
                    imports[alias.asname or alias.name] = node.module.replace(".", "/") + ".py"
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name.lower() == name]
        if len(classes) != 1:
            unresolved.append(f"Cannot identify dialect class in {module}")
            continue
        klass = classes[0]
        component: dict[str, str | None] = {"Parser": None, "Generator": None}
        for node in klass.body:
            if isinstance(node, ast.ClassDef) and node.name in component:
                component[node.name] = module
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in component:
                        component[target.id] = imports.get(node.value.id) if isinstance(node.value, ast.Name) else None
        if component["Parser"] is None or component["Generator"] is None:
            unresolved.append(f"Inherited or unresolved dispatch component in {module}")
        entries[name] = DialectEntry(name, module, component["Parser"], component["Generator"])
    return entries, unresolved


def _kw(call: ast.Call, key: str) -> ast.AST | None:
    return next((item.value for item in call.keywords if item.arg == key), None)


def _arg(call: ast.Call, position: int, key: str) -> ast.AST | None:
    return _kw(call, key) or (call.args[position] if len(call.args) > position else None)


def _scan_method(
    method: ast.FunctionDef | ast.AsyncFunctionDef, source_path: str,
    self_dialect: str | None, validator: bool,
) -> tuple[list[_Reference], list[str]]:
    refs: list[_Reference] = []
    unresolved: list[str] = []

    def record(node: ast.AST | None, role: str, line: int, mechanism: str,
               env: Mapping[str, tuple[str, ...]]) -> None:
        if node is None:
            return
        names = _names(node, env, self_dialect)
        if names:
            refs.extend(_Reference(name, role, source_path, line, mechanism) for name in names if name)
        else:
            unresolved.append(f"Computed dialect in {source_path}:{line} ({mechanism})")

    def dictionary(node: ast.AST | None, role: str, line: int, mechanism: str,
                   env: Mapping[str, tuple[str, ...]]) -> None:
        if node is None:
            return
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if key is not None:
                    record(key, role, line, mechanism, env)
        else:
            unresolved.append(f"Computed dialect collection in {source_path}:{line} ({mechanism})")

    def visit(node: ast.AST, env: dict[str, tuple[str, ...]]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        if isinstance(node, (ast.For, ast.AsyncFor)):
            child = env.copy()
            if isinstance(node.target, ast.Name):
                values = _name_set(node.iter, env, self_dialect)
                if values:
                    child[node.target.id] = values
                else:
                    child.pop(node.target.id, None)
            visit(node.iter, env)
            for statement in node.body:
                visit(statement, child)
            for statement in node.orelse:
                visit(statement, env)
            return
        if isinstance(node, ast.Assign):
            visit(node.value, env)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values = _name_set(node.value, env, self_dialect)
                    if values:
                        env[target.id] = values
                    else:
                        env.pop(target.id, None)
            return
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            attr = func.attr if isinstance(func, ast.Attribute) else None
            owner = func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else None
            if name in ("parse_one", "parse"):
                record(_arg(node, 1, "read") or _kw(node, "dialect"), "parser", node.lineno, name, env)
            elif name == "transpile":
                read = _arg(node, 1, "read")
                write = _arg(node, 2, "write")
                if isinstance(write, ast.Constant) and write.value is None:
                    write = None
                identity = _kw(node, "identity")
                record(read, "parser", node.lineno, "transpile read", env)
                record(write or (read if not (isinstance(identity, ast.Constant) and identity.value is False)
                                 else None), "generator", node.lineno, "transpile write/identity", env)
            elif attr == "sql":
                record(_arg(node, 0, "dialect"), "generator", node.lineno, "Expr.sql", env)
            elif owner == "Dialect" and attr in ("get_or_raise", "get", "__getitem__"):
                record(node.args[0] if node.args else None, "dialect", node.lineno, "Dialect registry", env)
            elif owner == "self" and attr == "parse_one" and validator:
                if self_dialect:
                    refs.append(_Reference(self_dialect, "parser", source_path, node.lineno, "Validator.parse_one"))
            elif owner == "self" and validator and attr in ("validate_identity", "validate_all", "validate_transpile"):
                if self_dialect:
                    refs.append(_Reference(self_dialect, "parser", source_path, node.lineno, f"Validator.{attr}"))
                    if attr in ("validate_identity", "validate_all"):
                        refs.append(_Reference(self_dialect, "generator", source_path, node.lineno, f"Validator.{attr}"))
                if attr == "validate_all":
                    dictionary(_kw(node, "read"), "parser", node.lineno, "Validator.validate_all read", env)
                    dictionary(_kw(node, "write"), "generator", node.lineno, "Validator.validate_all write", env)
                if attr == "validate_transpile":
                    record(_kw(node, "write_dialect") or (node.args[2] if len(node.args) > 2 else None),
                           "generator", node.lineno, "Validator.validate_transpile write", env)
            elif (owner == "importlib" and attr == "import_module") or name == "__import__":
                value = _literal(node.args[0]) if node.args else None
                if value and value.startswith("sqlglot.dialects."):
                    refs.append(_Reference(value.rsplit(".", 1)[-1].lower(), "dialect", source_path,
                                           node.lineno, "dynamic import literal"))
                else:
                    unresolved.append(f"Dynamic import in {source_path}:{node.lineno}")
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "Dialect":
            record(node.slice, "dialect", node.lineno, "Dialect registry index", env)
        for child in ast.iter_child_nodes(node):
            visit(child, env)

    env: dict[str, tuple[str, ...]] = {}
    for statement in method.body:
        visit(statement, env)
    return refs, unresolved


def _scan_tests(root: Path, metadata: Mapping[str, object]) -> tuple[dict[str, int], dict[str, list[_Reference]], list[str]]:
    _test_index(metadata)
    registered = {entry["id"]: entry["number"] for entry in metadata["tests"]}
    by_file: dict[str, list[str]] = {}
    for test_id in registered:
        by_file.setdefault(test_id.split("::", 1)[0], []).append(test_id)
    refs: dict[str, list[_Reference]] = {}
    unresolved: list[str] = []
    for source_path, ids in sorted(by_file.items()):
        try:
            tree = ast.parse((root / source_path).read_text(encoding="utf-8"), filename=source_path)
        except (OSError, UnicodeError, SyntaxError) as exc:
            unresolved.append(f"Cannot inspect registered test {source_path}: {type(exc).__name__}")
            continue
        for klass in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            dialect = None
            for node in klass.body:
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "dialect" for target in node.targets):
                    dialect = _literal(node.value)
                    if dialect is None and not (isinstance(node.value, ast.Constant) and node.value.value is None):
                        unresolved.append(f"Computed class dialect in {source_path}:{node.lineno}")
            validator = any(isinstance(base, ast.Name) and base.id == "Validator" for base in klass.bases)
            if not validator and dialect is not None and any(
                isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "self" and node.attr == "dialect" for node in ast.walk(klass)
            ):
                unresolved.append(f"Unverified dialect-bearing test inheritance in {source_path}:{klass.lineno}")
            for method in (node for node in klass.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")):
                prefix = f"{source_path}::{klass.name}::{method.name}"
                matches = [test_id for test_id in ids if test_id == prefix or test_id.startswith(prefix + "[")]
                if not matches:
                    continue
                found, issues = _scan_method(method, source_path, dialect, validator)
                unresolved.extend(issues)
                for test_id in matches:
                    refs.setdefault(test_id, []).extend(found)
    return registered, refs, unresolved


def map_dialect_impacts(
    changed_paths: Sequence[str], test_metadata: Mapping[str, object], source_root: str | Path,
) -> DialectMappingResult:
    """Find positive dynamic-dispatch candidates in the pinned source tree."""
    root = Path(source_root)
    registry, registry_issues = _registry(root)
    registered, references, scan_issues = _scan_tests(root, test_metadata)
    global_issues = set(registry_issues) | set(scan_issues)
    for refs in references.values():
        for ref in refs:
            if ref.dialect not in registry:
                global_issues.add(f"Unknown dialect {ref.dialect} at {ref.source_path}:{ref.line}")
    impacts: list[DialectImpact] = []
    for path in sorted(set(changed_paths)):
        affected = [(entry, role) for entry in registry.values() for role, module in (
            ("dialect", entry.dialect_module), ("parser", entry.parser_module),
            ("generator", entry.generator_module)) if module == path]
        evidence: set[DialectEvidence] = set()
        issues: set[str] = set()
        if not affected:
            issues.add(f"No verified dialect dispatch link for {path}")
        for test_id, refs in references.items():
            for ref in refs:
                if ref.dialect not in registry:
                    continue
                for entry, role in affected:
                    if ref.dialect == entry.name and (role == "dialect" or ref.role == role):
                        evidence.add(DialectEvidence(test_id, registered[test_id], entry.name, ref.role,
                            ref.source_path, ref.line, ref.mechanism,
                            tuple(module for module in (entry.dialect_module, entry.parser_module,
                                entry.generator_module) if module)))
        ordered = tuple(sorted(evidence, key=lambda item: (
            item.test_number, item.dialect, item.role, item.source_path, item.line, item.mechanism)))
        issues.add("Unmodeled dispatch and indirect consumers have not been excluded")
        impacts.append(DialectImpact(path, tuple(sorted({item.test_id for item in ordered})),
            tuple(sorted({item.test_number for item in ordered})), ordered,
            tuple(sorted({entry.name for entry, _ in affected})), tuple(sorted(issues))))
    ids = tuple(sorted({test_id for impact in impacts for test_id in impact.candidate_ids}))
    numbers = tuple(sorted({number for impact in impacts for number in impact.candidate_numbers}))
    issues = set(global_issues) | {reason for impact in impacts for reason in impact.unresolved}
    if not impacts:
        issues.add("No changed source paths")
    return DialectMappingResult(ids, numbers, tuple(impacts), tuple(sorted(issues)),
                                tuple(sorted(global_issues)))
