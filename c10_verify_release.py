"""Offline acceptance check for the D40 diagnostic release."""

import argparse
import json
import math
from pathlib import Path

from score import evaluate_run, run_total_microseconds


def _parse_output(stdout):
    records = [json.loads(line) for line in stdout.decode("utf-8").split("\n") if line]
    return {record["commit"]: record["workers"] for record in records}


def verify(sample: Path, output: Path, reference: Path, cpu_seconds: float | None = None) -> dict:
    tests = json.loads((sample / "tests.json").read_text(encoding="utf-8"))["tests"]
    answers = json.loads((sample / "answers.json").read_text(encoding="utf-8"))["commits"]
    commits = {patch.stem for patch in (sample / "commits").glob("*.patch")}
    if len(commits) != 30 or commits != set(answers):
        raise ValueError("expected 30 public patches with matching answer labels")

    stdout = output.read_bytes()
    reference_stdout = reference.read_bytes()
    for name, content in (("container output", stdout), ("reference", reference_stdout)):
        evaluation = evaluate_run(content, tests, answers)
        if not evaluation["ok"]:
            raise ValueError(f"official scorer rejected {name}: {evaluation}")
        if evaluation["simulated_milliseconds"] != 163_416 or evaluation["missed_commits"]:
            raise ValueError(f"unexpected D40 result for {name}: {evaluation}")

    actual = _parse_output(stdout)
    expected = _parse_output(reference_stdout)
    differences = sorted(commit for commit in commits if actual[commit] != expected[commit])
    if differences:
        raise ValueError(f"D40 worker assignments differ for: {', '.join(differences)}")

    selected = {commit: {number for worker in workers for number in worker}
                for commit, workers in actual.items()}
    failures = {commit: set(entry["failing"]) for commit, entry in answers.items()}
    failed_count = sum(len(numbers) for numbers in failures.values())
    recalled = sum(len(numbers & selected[commit]) for commit, numbers in failures.items())
    if recalled != failed_count or failed_count != 73:
        raise ValueError(f"unexpected failing-test recall: {recalled}/{failed_count}")

    total_seconds = None
    if cpu_seconds is not None:
        if not math.isfinite(cpu_seconds) or cpu_seconds < 0:
            raise ValueError("CPU seconds must be a finite, nonnegative measurement")
        total_seconds = run_total_microseconds(163_416, round(cpu_seconds * 1_000_000)) / 1_000_000
    return {"records": len(actual), "selected_test_occurrences": sum(map(len, selected.values())),
            "workers_match_reference": True, "simulated_seconds": 163.416, "misses": 0,
            "recalled_failures": recalled, "labelled_failures": failed_count,
            "cpu_seconds": cpu_seconds, "total_seconds": total_seconds}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--cpu-seconds", type=float)
    args = parser.parse_args()
    try:
        result = verify(args.sample, args.output, args.reference, args.cpu_seconds)
    except (OSError, ValueError) as error:
        parser.exit(1, f"D40 verification failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
