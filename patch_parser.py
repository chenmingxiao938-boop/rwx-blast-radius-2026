"""Parse Git unified diffs without making test-selection decisions.

A caller must treat ``Patch.unknown`` as requiring conservative selection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ChangeKind = Literal["added", "deleted", "modified", "renamed", "copied", "unknown"]
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_INDEX = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+(?: [0-7]{6})?$")
_PERCENT = re.compile(r"^(?:dis)?similarity index (\d+)%$")
_MODE = re.compile(r"^(?:new file mode|deleted file mode|old mode|new mode) [0-7]{6}$")
_NO_NEWLINE = r"\ No newline at end of file"
_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, "\\": 92, '"': 34}


class _Unsupported(ValueError):
    pass


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    context: str
    old_changed_lines: tuple[int, ...]
    new_changed_lines: tuple[int, ...]
    body: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileChange:
    kind: ChangeKind
    old_path: str | None
    new_path: str | None
    hunks: tuple[Hunk, ...]
    unknown_reason: str | None = None


@dataclass(frozen=True)
class Patch:
    files: tuple[FileChange, ...]
    unknown_reasons: tuple[str, ...] = ()

    @property
    def unknown(self) -> bool:
        return bool(self.unknown_reasons)

    @property
    def changed_paths(self) -> frozenset[str]:
        return frozenset(
            path for file in self.files for path in (file.old_path, file.new_path) if path is not None
        )


def _token(text: str, start: int) -> tuple[str, int]:
    """Read one Git path token, including Git's C-quoted UTF-8 escapes."""
    if start >= len(text):
        raise _Unsupported("missing path")
    if text[start] != '"':
        end = start
        while end < len(text) and not text[end].isspace():
            end += 1
        if end == start:
            raise _Unsupported("empty path")
        return text[start:end], end

    data = bytearray()
    position = start + 1
    while position < len(text):
        char = text[position]
        position += 1
        if char == '"':
            try:
                return data.decode("utf-8"), position
            except UnicodeDecodeError as error:
                raise _Unsupported("invalid quoted UTF-8 path") from error
        if char != "\\":
            try:
                data.extend(char.encode("utf-8"))
            except UnicodeEncodeError as error:
                raise _Unsupported("invalid Unicode in quoted path") from error
            continue
        if position >= len(text):
            raise _Unsupported("unfinished path escape")
        escape = text[position]
        position += 1
        if escape in _ESCAPES:
            data.append(_ESCAPES[escape])
        elif escape in "01234567" and position + 1 < len(text) and all(
            digit in "01234567" for digit in text[position : position + 2]
        ):
            octet = int(escape + text[position : position + 2], 8)
            if octet > 255:
                raise _Unsupported("out-of-range octal path escape")
            data.append(octet)
            position += 2
        else:
            raise _Unsupported("unsupported path escape")
    raise _Unsupported("unclosed quoted path")


def _repo_path(path: str) -> str:
    if not path or path.startswith("/") or "\\" in path or any(
        part in ("", ".", "..") for part in path.split("/")
    ) or any(ord(char) < 32 for char in path):
        raise _Unsupported("unsafe or ambiguous repository path")
    try:
        path.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _Unsupported("invalid Unicode in path") from error
    return path


def _diff_paths(header: str) -> tuple[str, str]:
    if not header.startswith("diff --git "):
        raise _Unsupported("unsupported diff header")
    text = header[len("diff --git ") :]
    first, position = _token(text, 0)
    if position >= len(text) or text[position] != " ":
        raise _Unsupported("missing second diff path")
    second, position = _token(text, position + 1)
    if position != len(text) or not first.startswith("a/") or not second.startswith("b/"):
        raise _Unsupported("ambiguous diff paths")
    return _repo_path(first[2:]), _repo_path(second[2:])


def _field_path(value: str, prefix: str | None = None) -> str | None:
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        path, position = _token(value, 0)
        if position != len(value):
            raise _Unsupported("extra text after path")
    else:
        path = value
    if prefix is not None:
        if not path.startswith(prefix):
            raise _Unsupported("wrong path prefix")
        path = path[len(prefix) :]
    return _repo_path(path)


def _decimal(text: str, field: str) -> int:
    try:
        return int(text)
    except ValueError as error:
        raise _Unsupported(f"invalid {field}") from error


def _first_line(start: int, count: int) -> int:
    return start if count else start + 1


def _after_last_line(start: int, count: int) -> int:
    return start + count if count else start + 1


def _parse_hunk(lines: list[str], index: int) -> tuple[Hunk, int]:
    match = _HUNK.fullmatch(lines[index])
    if match is None:
        raise _Unsupported("invalid hunk header")
    old_start, new_start = _decimal(match[1], "old hunk start"), _decimal(match[3], "new hunk start")
    old_count = _decimal(match[2], "old hunk count") if match[2] is not None else 1
    new_count = _decimal(match[4], "new hunk count") if match[4] is not None else 1
    if (old_count and old_start == 0) or (new_count and new_start == 0):
        raise _Unsupported("nonempty hunk range starts at zero")
    old_line, new_line = old_start, new_start
    old_changed: list[int] = []
    new_changed: list[int] = []
    body: list[str] = []
    last_was_body = False
    index += 1
    while old_line < old_start + old_count or new_line < new_start + new_count:
        if index >= len(lines):
            raise _Unsupported("truncated hunk")
        line = lines[index]
        index += 1
        if line == _NO_NEWLINE:
            if not last_was_body:
                raise _Unsupported("misplaced no-newline marker")
            last_was_body = False
            continue
        if not line:
            raise _Unsupported("unmarked hunk line")
        body.append(line)
        if line[0] == " ":
            old_line += 1
            new_line += 1
        elif line[0] == "-":
            old_changed.append(old_line)
            old_line += 1
        elif line[0] == "+":
            new_changed.append(new_line)
            new_line += 1
        else:
            raise _Unsupported("unexpected hunk line")
        last_was_body = True
        if old_line > old_start + old_count or new_line > new_start + new_count:
            raise _Unsupported("hunk line count exceeded")
    while index < len(lines) and lines[index] == _NO_NEWLINE:
        if not last_was_body:
            raise _Unsupported("misplaced no-newline marker")
        last_was_body = False
        index += 1
    return Hunk(
        old_start, old_count, new_start, new_count, match[5],
        tuple(old_changed), tuple(new_changed), tuple(body),
    ), index


def _file_section(lines: list[str]) -> FileChange:
    try:
        old_path, new_path = _diff_paths(lines[0])
    except _Unsupported as error:
        return FileChange("unknown", None, None, (), str(error))

    original_old, original_new = old_path, new_path
    old_marker = new_marker = False
    new_file = deleted_file = renamed = copied = mode_changed = False
    rename_from = rename_to = copy_from = copy_to = None
    similarity = None
    binary = False
    hunks: list[Hunk] = []
    index = 1
    try:
        while index < len(lines):
            line = lines[index]
            if line.startswith("@@ "):
                if not old_marker or not new_marker:
                    raise _Unsupported("hunk missing file markers")
                hunk, index = _parse_hunk(lines, index)
                if hunks:
                    previous = hunks[-1]
                    old_gap = _first_line(hunk.old_start, hunk.old_count) - _after_last_line(
                        previous.old_start, previous.old_count
                    )
                    new_gap = _first_line(hunk.new_start, hunk.new_count) - _after_last_line(
                        previous.new_start, previous.new_count
                    )
                    if old_gap < 0 or old_gap != new_gap:
                        raise _Unsupported("overlapping or inconsistent hunk coordinates")
                hunks.append(hunk)
                continue
            if hunks:
                raise _Unsupported("metadata after hunk")
            if line.startswith("@@@"):
                raise _Unsupported("combined diff is unsupported")
            if line.startswith("Binary files ") or line == "GIT binary patch":
                binary = True
                break
            if line.startswith("index "):
                if _INDEX.fullmatch(line) is None:
                    raise _Unsupported("invalid index marker")
                if line.endswith(" 160000"):
                    raise _Unsupported("submodule change")
            elif _MODE.fullmatch(line):
                new_file |= line.startswith("new file mode ")
                deleted_file |= line.startswith("deleted file mode ")
                mode_changed |= line.startswith(("old mode ", "new mode "))
                if "160000" in line:
                    raise _Unsupported("submodule change")
            elif line.startswith(("new file mode ", "deleted file mode ", "old mode ", "new mode ")):
                raise _Unsupported("invalid mode marker")
            elif (match := _PERCENT.fullmatch(line)) is not None:
                percent = _decimal(match[1], "similarity")
                if line.startswith("similarity"):
                    similarity = percent
                if percent > 100:
                    raise _Unsupported("invalid similarity")
            elif line.startswith(("similarity index ", "dissimilarity index ")):
                raise _Unsupported("invalid similarity marker")
            elif line.startswith("rename from "):
                rename_from = _field_path(line[len("rename from ") :])
                renamed = True
            elif line.startswith("rename to "):
                rename_to = _field_path(line[len("rename to ") :])
                renamed = True
            elif line.startswith("copy from "):
                copy_from = _field_path(line[len("copy from ") :])
                copied = True
            elif line.startswith("copy to "):
                copy_to = _field_path(line[len("copy to ") :])
                copied = True
            elif line.startswith("--- "):
                if new_marker:
                    raise _Unsupported("old-file marker after new-file marker")
                if old_marker:
                    raise _Unsupported("duplicate old-file marker")
                old_marker = True
                marked_old = _field_path(line[4:], "a/")
                if marked_old != original_old and marked_old is not None:
                    raise _Unsupported("old-file path mismatch")
                if marked_old is None:
                    old_path = None
            elif line.startswith("+++ "):
                if new_marker:
                    raise _Unsupported("duplicate new-file marker")
                new_marker = True
                marked_new = _field_path(line[4:], "b/")
                if marked_new != original_new and marked_new is not None:
                    raise _Unsupported("new-file path mismatch")
                if marked_new is None:
                    new_path = None
            else:
                raise _Unsupported("unrecognized diff marker")
            index += 1

        if renamed and copied or new_file and deleted_file or (renamed or copied) and (new_file or deleted_file):
            raise _Unsupported("conflicting change markers")
        if (renamed or copied) and (old_path is None or new_path is None):
            raise _Unsupported("rename or copy conflicts with null path")
        if new_file and old_marker and old_path is not None:
            raise _Unsupported("new-file marker conflicts with old path")
        if deleted_file and new_marker and new_path is not None:
            raise _Unsupported("deleted-file marker conflicts with new path")
        if renamed:
            if rename_from != original_old or rename_to != original_new:
                raise _Unsupported("incomplete rename markers")
            kind: ChangeKind = "renamed"
        elif copied:
            if copy_from != original_old or copy_to != original_new:
                raise _Unsupported("incomplete copy markers")
            kind = "copied"
        elif new_file or old_path is None:
            if deleted_file or new_path is None:
                raise _Unsupported("conflicting addition markers")
            kind = "added"
            old_path = None
        elif deleted_file or new_path is None:
            kind = "deleted"
            new_path = None
        elif old_path != new_path:
            raise _Unsupported("path changed without rename or copy markers")
        else:
            kind = "modified"
        if kind == "added" and any((h.old_start, h.old_count) != (0, 0) for h in hunks):
            raise _Unsupported("added file has old-side hunk lines")
        if kind == "deleted" and any((h.new_start, h.new_count) != (0, 0) for h in hunks):
            raise _Unsupported("deleted file has new-side hunk lines")
        if hunks and not any(h.old_changed_lines or h.new_changed_lines for h in hunks):
            raise _Unsupported("hunks contain no changes")
        if binary:
            return FileChange(kind, old_path, new_path, tuple(hunks), "binary diff has no reliable line ranges")
        if not hunks and not (renamed and similarity == 100) and not (copied and similarity == 100):
            reason = "metadata-only change has no line ranges" if mode_changed else "diff has no change hunks"
            return FileChange(kind, old_path, new_path, (), reason)
        return FileChange(kind, old_path, new_path, tuple(hunks))
    except _Unsupported as error:
        return FileChange("unknown", old_path, new_path, tuple(hunks), str(error))


def parse_patch(text: str) -> Patch:
    """Return parsed changes; any unsupported content sets ``unknown``."""
    if not isinstance(text, str):
        raise TypeError("patch text must be str")
    if not text.strip():
        return Patch(())
    sections: list[list[str]] = []
    preamble: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
        else:
            preamble.append(line)
    reasons = ["unsupported patch preamble"] if any(line.strip() for line in preamble) else []
    files = tuple(_file_section(section) for section in sections)
    reasons.extend(file.unknown_reason for file in files if file.unknown_reason is not None)
    if not sections and not reasons:
        reasons.append("patch has no file sections")
    return Patch(files, tuple(reasons))
