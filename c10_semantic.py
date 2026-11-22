"""Positive SQLGlot impact signals from patch text and pinned test inputs."""

from __future__ import annotations

import ast
from collections import Counter
from functools import lru_cache
from io import StringIO
from pathlib import PurePosixPath
import re
import textwrap
import tokenize


_WORDS = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SQL_KEYWORDS = frozenset("""
    alter array between by case cast collate connect create cross cte delete distinct
    drop except exists filter from full group having ilike index inner insert intersect
    join lateral left like limit merge natural null offset order outer over partition
    pivot qualify recursive replace returning right select semi set table tablesample
    try_cast union unnest unpivot update values where window with
""".split())
_SQL_COMPOUND = frozenset("""
    cross_join distinct_on group_by inner_join left_join natural_join not_in
    order_by partition_by right_join union_all with_recursive
""".split())
_STOP = _SQL_KEYWORDS | frozenset("""
    and args as assert async await c col return f false import in is none not
    or pass raise result self sql t test true try value values class def del elif
    else except for global if lambda nonlocal while yield kwargs cls with
""".split())
_FUNCTION = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
_SQL_MARKER = re.compile(
    r"\b(?:select|from|where|create|drop|insert|delete|update|alter|values|merge|"
    r"join|having|qualify|window|partition|union|exists|between)\b|::|->", re.I
)
_SQL_QUOTED = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)
_DIALECT = re.compile(r"[a-z][a-z_0-9]{1,30}\Z")
_LOADER_NAMES = {"load_sql_fixtures", "load_sql_fixture_pairs"}


def _mark(target: Counter[str], key: str, weight: int) -> None:
    if target[key] < weight:
        target[key] = weight


def _merge(target: Counter[str], features: Counter[str]) -> None:
    for key, weight in features.items():
        _mark(target, key, weight)


def _identifier(target: Counter[str], name: str, weight: int) -> None:
    parts = _CAMEL_BOUNDARY.sub("_", name).lower().strip("_").split("_")
    if len(parts) > 1:
        _mark(target, "name:" + "_".join(parts), weight)
    for part in parts:
        if len(part) >= 3 and part not in _STOP:
            _mark(target, "name:" + part, 1)


def _expression(target: Counter[str], name: str, weight: int) -> None:
    normalized = name.lower().replace("_", "")
    if normalized:
        _mark(target, "expr:" + normalized, weight)


def _expression_ref(target: Counter[str], name: str) -> None:
    _expression(target, name, 3)
    word = name.lower()
    if word in _SQL_KEYWORDS:
        _mark(target, "sql:" + word, 1)


def sql_features(text: str) -> Counter[str]:
    """Index SQL clause pairs, function/operator names and fixture metadata."""
    features: Counter[str] = Counter()
    for match in re.finditer(r"(?im)^\s*#\s*dialect\s*:\s*([a-z][a-z_0-9]+)", text):
        _mark(features, "dialect:" + match[1].lower(), 3)
    sql = _SQL_QUOTED.sub(" ", text[:65536]).lower()
    words = _WORDS.findall(sql)
    for word in words:
        if word in _SQL_KEYWORDS:
            _mark(features, "sql:" + word, 1)
    for left, right in zip(words, words[1:]):
        compound = left + "_" + right
        if compound in _SQL_COMPOUND:
            _mark(features, "sql:" + compound, 3)
            _expression(features, compound, 2)
    for match in _FUNCTION.finditer(sql):
        if match[1] not in _STOP or match[1] in {"cast", "try_cast", "unnest", "pivot"}:
            _expression(features, match[1], 3)
    for op, name in (("::", "cast"), ("->", "arrow"), ("||", "concat"), ("=>", "named_argument")):
        if op in sql:
            _expression(features, name, 2)
    return features


def _literal_sql(target: Counter[str], text: str) -> None:
    if _SQL_MARKER.search(text) or _FUNCTION.match(text.lstrip()):
        _merge(target, sql_features(text))


def _patch_python(target: Counter[str], source: str) -> None:
    tokens: list[tokenize.TokenInfo] = []
    try:
        for token in tokenize.generate_tokens(StringIO(textwrap.dedent(source)).readline):
            tokens.append(token)
    except (tokenize.TokenError, IndentationError):
        # Hunk fragments may end mid-expression. Keep the valid prefix.
        pass
    for i, tok in enumerate(tokens):
        if tok.type == tokenize.NAME:
            _identifier(target, tok.string, 2)
            if i >= 2 and tokens[i - 1].string == "." and tokens[i - 2].string in ("exp", "TokenType"):
                _expression_ref(target, tok.string)
        elif tok.type == tokenize.STRING:
            try:
                value = ast.literal_eval(tok.string)
            except (ValueError, SyntaxError):
                continue
            if isinstance(value, str):
                _literal_sql(target, value)
        elif tok.type == getattr(tokenize, "FSTRING_MIDDLE", -1):
            _literal_sql(target, tok.string)


def patch_features(parsed_patch) -> Counter[str]:
    """Extract signals from both sides of the diff, independent of patch IDs."""
    features: Counter[str] = Counter()
    for change in parsed_patch.files:
        paths = {path for path in (change.old_path, change.new_path) if path}
        for path in paths:
            file = PurePosixPath(path)
            if path.startswith("tests/fixtures/") and file.suffix == ".sql":
                _mark(features, "fixture:" + path, 6)
            if file.parent.name in {"dialects", "generators", "parsers", "tokenizers"}:
                dialect = file.stem.removeprefix("test_")
                if dialect not in {"dialect", "generator", "parser", "tokenizer", "__init__"}:
                    _mark(features, "dialect:" + dialect, 4)
            _identifier(features, file.stem, 1)
        if all(path.endswith(".sql") for path in paths) and paths:
            for hunk in change.hunks:
                for prefix in ("+", "-"):
                    _merge(features, sql_features("\n".join(
                        line[1:] for line in hunk.body if line.startswith(prefix)
                    )))
        elif any(path.endswith(".py") for path in paths):
            for hunk in change.hunks:
                _patch_python(features, hunk.context)
                for prefix in ("+", "-"):
                    _patch_python(features, "\n".join(
                        line[1:] for line in hunk.body if line.startswith(prefix)
                    ))
    return features


def _fixture(target: Counter[str], arg: str | None) -> None:
    if not arg:
        return
    path = PurePosixPath(arg)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".sql":
        return
    _mark(target, "fixture:tests/fixtures/" + path.as_posix(), 5)


def _string_expr(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for item in node.values:
            if isinstance(item, ast.FormattedValue):
                if item.conversion != -1 or item.format_spec is not None:
                    return None
                item = item.value
            part = _string_expr(item, bindings)
            if part is None:
                return None
            parts.append(part)
        return "".join(parts)
    return None


@lru_cache(maxsize=96)
def _source(source_text: str):
    tree = ast.parse(source_text)
    entries = {}
    loaders = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "tests.helpers":
            loaders.update(alias.asname or alias.name for alias in node.names
                           if alias.name in _LOADER_NAMES)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entries[(node.name,)] = (node, None)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    entries[(node.name, child.name)] = (child, node)
    return entries, loaders


def _named_call(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _owner_signals(target: Counter[str], owner: ast.ClassDef | None) -> ast.AST | None:
    wrapper = None
    if owner is None:
        return wrapper
    for node in owner.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "dialect"
                                               for t in node.targets):
            dialect = _string_expr(node.value, {})
            if dialect and _DIALECT.fullmatch(dialect):
                _mark(target, "dialect:" + dialect, 4)
        elif isinstance(node, ast.FunctionDef) and node.name == "check_file":
            wrapper = node
    return wrapper


def _callee_dialects(target: Counter[str], call: ast.Call) -> None:
    for keyword in call.keywords:
        if keyword.arg not in {"dialect", "read", "write", "read_dialect", "write_dialect"}:
            continue
        keys = keyword.value.keys if isinstance(keyword.value, ast.Dict) else [keyword.value]
        for node in keys:
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and _DIALECT.fullmatch(node.value):
                _mark(target, "dialect:" + node.value, 3)


def test_features(nodeid: str, source_text: str) -> Counter[str]:
    """Index SQL literals, expression refs, and dialect/fixture use for a node."""
    features: Counter[str] = Counter()
    path, *scope = nodeid.split("::")
    if not scope:
        return features
    scope[-1] = scope[-1].split("[", 1)[0]
    _identifier(features, scope[-1].removeprefix("test_"), 1)
    if "/dialects/" in path and PurePosixPath(path).stem != "test_dialect":
        _mark(features, "dialect:" + PurePosixPath(path).stem.removeprefix("test_"), 2)
    try:
        entries, loaders = _source(source_text)
    except SyntaxError:
        return features
    entry, owner = entries.get(tuple(scope), (None, None))
    if entry is None:
        return features
    wrapper = _owner_signals(features, owner)
    for node in ast.walk(entry):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            _literal_sql(features, node.value)
        elif isinstance(node, ast.Name):
            _identifier(features, node.id, 2)
        elif isinstance(node, ast.Attribute):
            _identifier(features, node.attr, 2)
            if isinstance(node.value, ast.Name) and node.value.id in {"exp", "TokenType"}:
                _expression_ref(features, node.attr)
        elif isinstance(node, ast.Call):
            _callee_dialects(features, node)
            if not node.args:
                continue
            name = _named_call(node)
            string_arg = _string_expr(node.args[0], {})
            if name in loaders:
                _fixture(features, string_arg)
            elif name == "check_file" and string_arg is not None and wrapper is not None:
                args = wrapper.args.posonlyargs + wrapper.args.args
                if len(args) >= 2:
                    for call in ast.walk(wrapper):
                        if isinstance(call, ast.Call) and _named_call(call) in loaders and call.args:
                            _fixture(features, _string_expr(call.args[0], {args[1].arg: string_arg}))
    return features
