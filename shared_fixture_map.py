"""Build fixture loader cohorts from the pinned test sources, never sample outcomes."""
import argparse
import ast
import json
import subprocess
import unittest
from pathlib import Path

PINNED = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
LOADERS = {"load_sql_fixtures", "load_sql_fixture_pairs"}


def build(root: Path, manifest: dict) -> dict:
    cohorts, initializers, unresolved = {}, {}, set()
    tests = manifest["tests"]
    for file in sorted((root / "tests").rglob("*.py")):
        relative = file.relative_to(root).as_posix()
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError):
            unresolved.add(relative)
            continue
        aliases = {alias.asname or alias.name for node in tree.body
                   if isinstance(node, ast.ImportFrom) and node.module == "tests.helpers"
                   for alias in node.names if alias.name in LOADERS}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                if isinstance(node.value, ast.Name) and node.value.id in aliases:
                    unresolved.add(relative)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("getattr", "globals", "locals", "eval", "exec", "__import__"):
                    unresolved.add(relative)
        if any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
               and call.func.id in aliases for fn in tree.body
               if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
               for call in ast.walk(fn)):
            unresolved.add(relative)
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            methods = {node.name: node for node in cls.body
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            prefix = f"{relative}::{cls.name}::"
            class_ids = {t["number"] for t in tests if t["id"].startswith(prefix)}
            if not class_ids:
                continue
            if any(not (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
                        and base.value.id == "unittest" and base.attr == "TestCase")
                   for base in cls.bases):
                unresolved.add(relative)
            edges, direct = {}, set()
            for name, method in methods.items():
                edges[name] = set()
                for node in ast.walk(method):
                    if isinstance(node, (ast.Assign, ast.NamedExpr, ast.AnnAssign)):
                        value = node.value
                        if isinstance(value, ast.Name) and value.id in aliases:
                            unresolved.add(relative)
                    if not isinstance(node, ast.Call):
                        continue
                    target = node.func
                    if isinstance(target, ast.Name) and target.id in aliases:
                        direct.add(name)
                        if name in ("setUpClass", "setUp"):
                            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                                unresolved.add(relative)
                            else:
                                path = "tests/fixtures/" + node.args[0].value
                                initializers.setdefault(path, set()).update(class_ids)
                    elif (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                          and target.value.id in ("self", "cls")):
                        if target.attr in methods:
                            edges[name].add(target.attr)
                        elif target.attr not in dir(unittest.TestCase):
                            unresolved.add(relative)
                    elif isinstance(target, ast.Name) and target.id == "getattr":
                        unresolved.add(relative)
            reached = set(direct)
            while True:
                expanded = reached | {name for name, calls in edges.items() if calls & reached}
                if expanded == reached:
                    break
                reached = expanded
            for name in reached:
                if name.startswith("test_"):
                    ids = {t["number"] for t in tests if t["id"] == prefix + name
                           or t["id"].startswith(prefix + name + "[")}
                    cohorts.setdefault(relative, set()).update(ids)
        if any(isinstance(node, ast.ImportFrom) and node.module == "tests.helpers"
                           and any(alias.name == "*" for alias in node.names) for node in tree.body):
            unresolved.add(relative)
    return {"pinned_commit": PINNED, "file_cohorts": {k: sorted(v) for k, v in sorted(cohorts.items())},
            "initializers": {k: sorted(v) for k, v in sorted(initializers.items())},
            "unresolved_files": sorted(unresolved)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    head = subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip()
    if head != PINNED:
        raise ValueError("source is not the pinned SQLGlot commit")
    result = build(args.source, json.loads(args.manifest.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()