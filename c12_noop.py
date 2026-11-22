"""Source-only Strategy A. Executable equality does not prove equal observations."""

from __future__ import annotations

import ast
import hashlib
import io
import marshal
import re
import tokenize
from functools import lru_cache
from types import CodeType

from method_boundary import _replay
from patch_parser import FileChange, Patch, parse_patch


_INDEX = re.compile(r"index ([0-9a-f]{7,40})\.\.([0-9a-f]{7,40})(?: (100644|100755))?")
_MODE_HEADERS = ("old mode ", "new mode ", "new file mode ", "deleted file mode ")
_UNREPRESENTED_LINES = frozenset("\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_DIRECTIVE = re.compile(
    r"^#(?:!|\s*(?:type\s*:|noqa\b|pragma\b|pylint\b|pyright\b|mypy\b|pyre[-:]|"
    r"fmt\b|ruff\b|isort\b|nosec\b|coverage\b|doctest\b|region\b|endregion\b|"
    r"flake8\b|cython\b|distutils\b|pydocstyle\b|language\s*[:=]))", re.I,
)
_CODING = re.compile(r"\bcoding\s*[:=]", re.I)
_SOURCE_OBSERVERS = frozenset({
    "getsource", "getsourcelines", "getsourcefile", "get_source", "findsource",
    "get_source_segment", "linecache", "__code__", "co_firstlineno", "co_linetable",
    "co_lines", "co_positions", "co_filename", "co_lnotab", "f_lineno", "tb_lineno",
    "getframeinfo", "currentframe", "_getframe", "extract_stack", "format_stack",
    "settrace", "setprofile", "gettrace", "getprofile",
})
_FILE_READERS = frozenset({"open", "read_text", "read_bytes", "get_data"})
_LOCATION_APIS = {
    "inspect": {"stack", "trace", "getouterframes", "getinnerframes"},
    "traceback": {"extract_tb", "format_tb", "format_exc", "format_exception", "print_tb", "print_exc",
                  "print_exception", "walk_stack", "walk_tb", "StackSummary", "FrameSummary", "TracebackException"},
}
_IGNORED_TOKENS = {tokenize.ENCODING, tokenize.COMMENT, tokenize.NL, tokenize.ENDMARKER}


def _headers(patch_text: str):
    """Find metadata not represented by FileChange, preserving section order."""
    groups: list[list[str]] = []
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            groups.append([])
        elif groups and line.startswith(("index ", *_MODE_HEADERS)):
            groups[-1].append(line)
    return groups


def _blob_sha1(text: str) -> str:
    payload = text.encode("utf-8")
    return hashlib.sha1(b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()


def _code_views(code: CodeType):
    constants = []
    descendants = []
    for value in code.co_consts:
        if isinstance(value, CodeType):
            executable, position_table = _code_views(value)
            constants.append(("code", executable))
            descendants.append(position_table)
        else:
            constants.append(("literal", marshal.dumps(value, 4)))
    executable = (
        code.co_code, code.co_exceptiontable, code.co_argcount, code.co_posonlyargcount,
        code.co_kwonlyargcount, code.co_nlocals, code.co_stacksize, code.co_flags,
        code.co_names, code.co_varnames, code.co_cellvars, code.co_freevars,
        code.co_name, code.co_qualname, tuple(constants),
    )
    location = (code.co_filename, code.co_firstlineno, code.co_linetable, tuple(descendants))
    return executable, location


def _token_views(text: str):
    raw = text.encode("utf-8")
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    meaningful = [token for token in tokenize.tokenize(io.BytesIO(raw).readline)
                  if token.type not in _IGNORED_TOKENS]
    return (encoding, tuple((token.type, token.string) for token in meaningful),
            tuple((token.start, token.end) for token in meaningful))


def _import_view(tree):
    return tuple(ast.dump(node, include_attributes=False) for node in ast.walk(tree)
                 if isinstance(node, (ast.Import, ast.ImportFrom)))


def _plain_comment_edit(change: FileChange) -> tuple[bool, bool]:
    edits = [line[1:].strip() for hunk in change.hunks for line in hunk.body
             if line.startswith(("+", "-")) and line[1:].strip()]
    directive = any(_DIRECTIVE.match(line) or _CODING.search(line) for line in edits
                    if line.startswith("#"))
    plain = bool(edits) and all(line.startswith("# ") for line in edits) and not directive
    return plain, directive


@lru_cache(maxsize=128)
def _observers(source: str):
    """Positive evidence only. A missing marker does not prove an observer absent."""
    try:
        tree = ast.parse(source, type_comments=True)
    except (SyntaxError, ValueError, RecursionError):
        return (), ()
    observers = set()
    io_readers = set()
    modules, aliases = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _LOCATION_APIS:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in _LOCATION_APIS:
            for alias in node.names:
                if alias.name in _LOCATION_APIS[node.module]:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Attribute):
            word = node.attr
            if isinstance(node.value, ast.Name) and (module := modules.get(node.value.id)):
                if word in _LOCATION_APIS[module]:
                    observers.add((line, f"{module}.{word}"))
        elif isinstance(node, ast.Name):
            word = node.id
            if word in aliases:
                observers.add((line, aliases[word]))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            word = node.value
        elif isinstance(node, ast.alias):
            word = node.name
        else:
            continue
        if word in _SOURCE_OBSERVERS:
            observers.add((line, word))
        if word in _FILE_READERS:
            io_readers.add((line, word))
    return tuple(sorted(observers)), tuple(sorted(io_readers))


def _observer_risks(path: str, source: str, updated: str, sources, test_sources, dependency_graph):
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module.removesuffix(".__init__")
    package = module.rsplit(".", 1)[0]
    filename = path.rsplit("/", 1)[-1]
    risks = set()
    for observer, text in sorted((sources | test_sources).items()):
        direct = observer == path or path in dependency_graph.get(observer, ())
        literal_path = path in text or ('"' + filename + '"' in text or "'" + filename + "'" in text) and "sqlglot" in text
        literal_module = module in text
        source_path_read = "__file__" in text and (literal_module or package + ".__file__" in text or observer == path)
        if not (direct or literal_path or literal_module or source_path_read):
            continue
        versions = (source, updated) if observer == path else (text,)
        for version in versions:
            markers, readers = _observers(version)
            risks.update(f"{observer}:{line}:{marker}" for line, marker in markers)
            if literal_path or source_path_read or (direct and "__file__" in version):
                risks.update(f"{observer}:{line}:source_{reader}" for line, reader in readers)
    return sorted(risks)


def _assess_file(change: FileChange, headers, sources, test_sources, dependency_graph):
    path = change.new_path or change.old_path
    row = {"path": path, "classification": "unknown", "zero_candidate": False,
           "replay_verified": False, "blob_verified": None, "blockers": []}
    blockers = row["blockers"]
    if change.kind != "modified" or change.old_path != change.new_path or not change.hunks:
        blockers.append("unsupported_file_shape_or_metadata")
        return row
    if change.unknown_reason or any(line.startswith(_MODE_HEADERS) for line in headers):
        blockers.append("file_mode_or_patch_metadata_changed")
        return row
    if path not in sources and path not in test_sources:
        blockers.append("pinned_source_unavailable")
        return row
    old = sources[path] if path in sources else test_sources[path]
    if _UNREPRESENTED_LINES.intersection(old) or any(
        _UNREPRESENTED_LINES.intersection(line) for hunk in change.hunks for line in hunk.body
    ):
        blockers.append("unverified_line_endings")
        return row
    new = _replay(old, change)
    if new is None:
        blockers.append("exact_replay_failed")
        return row
    indexes = [line for line in headers if line.startswith("index ")]
    if indexes:
        match = _INDEX.fullmatch(indexes[0]) if len(indexes) == 1 else None
        row["blob_verified"] = bool(match and _blob_sha1(old).startswith(match[1])
                                    and _blob_sha1(new).startswith(match[2]))
        if not row["blob_verified"]:
            blockers.append("git_blob_or_mode_mismatch")
            return row
    row["replay_verified"] = True
    row["line_delta"] = new.count("\n") - old.count("\n")
    if not path.endswith(".py"):
        row["classification"] = "changed_behavior" if new != old else "unknown"
        blockers.append("non_python_input_observable")
        return row
    try:
        before, after = ast.parse(old, type_comments=True), ast.parse(new, type_comments=True)
        old_ops, old_locations = _code_views(compile(old, path, "exec", dont_inherit=True, optimize=0))
        new_ops, new_locations = _code_views(compile(new, path, "exec", dont_inherit=True, optimize=0))
        old_encoding, old_tokens, old_positions = _token_views(old)
        new_encoding, new_tokens, new_positions = _token_views(new)
    except (SyntaxError, ValueError, tokenize.TokenError, RecursionError):
        blockers.append("compile_ast_or_token_verification_failed")
        return row

    plain, directive = _plain_comment_edit(change)
    row.update({
        "ast_equal": ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False),
        "bytecode_equal": old_ops == new_ops,
        "source_metadata_equal": old_locations == new_locations,
        "tokens_equal": old_tokens == new_tokens,
        "token_positions_equal": old_positions == new_positions,
        "imports_equal": _import_view(before) == _import_view(after),
        "encoding_equal": old_encoding == new_encoding,
        "plain_comment_edit": plain,
    })
    if not row["bytecode_equal"]:
        row["classification"] = "changed_behavior"
        blockers.append("compiled_executable_changed")
        return row
    if not all(row[key] for key in ("ast_equal", "tokens_equal", "imports_equal", "encoding_equal")):
        blockers.append("executable_matches_but_ast_tokens_or_import_unproved")
        return row
    row["classification"] = (
        "executable_equivalent" if row["source_metadata_equal"] and row["token_positions_equal"]
        else "likely_equivalent"
    )
    if not plain or not path.startswith("sqlglot/"):
        blockers.append("comment_directive_changed" if directive else "not_standalone_sqlglot_comment_edit")
        return row
    row["observed_source_consumers"] = _observer_risks(path, old, new, sources, test_sources, dependency_graph)
    if row["observed_source_consumers"]:
        blockers.append("concrete_source_or_position_observer")
        return row
    row["zero_candidate"] = True
    return row


def assess_patch(patch: Patch, sources: dict[str, str], test_sources: dict[str, str],
                 dependency_graph: dict[str, list[str]], *, patch_text: str | None = None) -> dict:
    """Classify a whole patch; the inferred zero decision is opt-in and not a proof.

    The raw diff is necessary because the baseline parser does not retain file
    modes on content hunks. The caller controls whether to use the zero decision.
    """
    if patch_text is None or patch.unknown or not patch.files or parse_patch(patch_text) != patch:
        return {"classification": "unknown", "zero_candidate": False, "files": [],
                "assumptions": [], "reason": "unverified_patch_shape"}
    metadata = _headers(patch_text)
    if len(metadata) != len(patch.files) or len(patch.changed_paths) != len(patch.files):
        return {"classification": "unknown", "zero_candidate": False, "files": [],
                "assumptions": [], "reason": "ambiguous_file_sections"}

    files = [_assess_file(change, headers, sources, test_sources, dependency_graph)
             for change, headers in zip(patch.files, metadata)]
    kinds = {row["classification"] for row in files}
    classification = next(kind for kind in ("changed_behavior", "unknown", "likely_equivalent",
                                             "executable_equivalent") if kind in kinds)
    zero = all(row["zero_candidate"] for row in files)
    assumptions = []
    if zero:
        assumptions = [
            "same pinned CPython compiler and import configuration",
            "unobserved source readers or runtime hooks do not inspect changed comments or source bytes",
            "no unrecognized tooling directive in the edited ordinary comment text",
        ]
        if classification == "likely_equivalent":
            assumptions.append("unobserved traceback, logging, tracing or position checks are insensitive to line shifts")
    return {"classification": classification, "zero_candidate": zero, "files": files,
            "assumptions": assumptions}
