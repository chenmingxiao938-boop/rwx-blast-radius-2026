"""Cost-aware preservation of positive dependency evidence for C11-B only."""

from collections import Counter
import re

from c11a_select import STRONG


_DIRECTIVE = re.compile(
    r"^#(?:!|\s*(?:coding\s*[:=]|type\s*:|noqa\b|pragma\b|pylint\b|pyright\b|mypy\b|"
    r"fmt\b|ruff\b|isort\b|nosec\b|coverage\b|doctest\b|region\b|endregion\b))", re.I,
)
_CODING = re.compile(r"\bcoding\s*[:=]", re.I)


def ordinary_comment_change(patch, risks):
    """Require verified AST equality and edits consisting of plain comments."""
    paths = {change.new_path for change in patch.files}
    if (patch.unknown or not paths or len(paths) != len(patch.files)
            or any(not path or not path.startswith("sqlglot/") or not path.endswith(".py")
                   or f"same_ast_text_changed:{path}" not in risks for path in paths)):
        return False
    comments = []
    for change in patch.files:
        if change.kind != "modified":
            return False
        for hunk in change.hunks:
            for line in hunk.body:
                if line.startswith(("+", "-")) and (content := line[1:].strip()):
                    if not content.startswith("# ") or _DIRECTIVE.match(content) or _CODING.search(content):
                        return False
                    comments.append(content)
    return bool(comments)


def prune(selected, mandatory, ranking, scoped, tests, risks, patch, schedule_cost):
    """Remove a duplicate only if all scoped evidence and module diversity survive."""
    comments = ordinary_comment_change(patch, risks)
    if not comments and ranking.signals.structural:
        return selected, []
    protected = set(mandatory)
    if not comments:
        protected.update(n for n in selected if ranking.evidence.get(n, set()) & STRONG)

    remaining = set(selected)
    if not protected <= remaining:
        raise ValueError("missing mandatory or strong evidence test")
    tokens = {n: set(scoped.get(n, ())) | {("test_module", tests[n]["file"])} for n in remaining}
    # A line/branch pair at the same position is one observation.
    for n in remaining:
        tokens[n] = {("observed_line", *token[1:]) if token[0] in {"line", "branch"} else token
                     for token in tokens[n]}
    witnesses = Counter(token for per_test in tokens.values() for token in per_test)
    cost = schedule_cost(remaining)
    floor = min(4, len(remaining))
    removed = []
    for number in sorted(remaining - protected, key=lambda n: (-tests[n]["seconds"], n)):
        if len(remaining) <= floor or any(witnesses[token] == 1 for token in tokens[number]):
            continue
        trial = remaining - {number}
        trial_cost = schedule_cost(trial)
        if trial_cost > cost:
            continue
        removed.append(number)
        remaining = trial
        cost = trial_cost
        witnesses.subtract(tokens[number])
    return tuple(sorted(remaining)), removed
