"""Local V5 runner. Pass --mixed-comments to opt into the experiment."""

import argparse
from functools import partial
import os
from pathlib import Path
import sys
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path, help="directory containing tests.json and commits/")
    parser.add_argument("--mixed-comments", action="store_true")
    args = parser.parse_args()
    if sys.platform == "win32":
        # The selector needs resource only for an optional stderr RSS metric.
        sys.modules["resource"] = SimpleNamespace(
            RUSAGE_SELF=0, getrusage=lambda _: SimpleNamespace(ru_maxrss=-1)
        )

    import v5_main
    from mixed_patch_impact import analyze_mixed_patch

    v5_main.APP = Path(__file__).resolve().parent
    v5_main.DATA = args.data.resolve()
    # Reproduce the configuration of the checked-in Dockerfile by default.
    os.environ.setdefault("V5_FIXTURE_MODE", "aggressive_fixture_only")
    os.environ.setdefault("V5_COVERAGE_MODE", "subprocess")
    os.environ.setdefault("V5_METHOD_MODE", "ast_method_body")
    if args.mixed_comments:
        v5_main.analyze_patch = partial(analyze_mixed_patch, baseline_analyze=v5_main.analyze_patch)
    v5_main.main()


if __name__ == "__main__":
    main()
