# V5 mixed Python and SQL fixture experiment

Base commit: `e331c55ac2b9743a663b04ab56a2fb7f11465112`.
The existing Docker entry point, selector, coverage indexes, build files and
scorer are unchanged. The local runner defaults to the Dockerfile configuration:
aggressive fixture-only selection, the subprocess coverage index and AST
method-body analysis. `--mixed-comments` opts into the new experiment.

## Method

Run the normal V5 selector first. For a mixed patch that would fall back,
require a complete parse and simple modifications to existing files. For
every changed Python file, replay the unified diff exactly against the pinned
source snapshot. Require every edited line on both sides to be a standalone
ordinary comment token and require identical old and new ASTs with type
comments enabled. Reject encoding hints, shebangs, type comments, pragmas,
file mode changes, missing sources, mismatched context and duplicate paths.

Only after these checks, pass the verified SQL blocks to the existing V5
fixture-only selector. It resolves observed readers against official IDs,
deduplicates their union, and retains fallback for missing or ambiguous
readers, collection-time reads and unsupported changes. The original V5
decision applies if any gate fails. A substantive Python edit retains full
fallback rather than reducing based on observed coverage alone. No sample
commit identifier or failure label is used to select tests.

## Local validation

Python 3.13.9 on Windows, 30 official samples and the checked-in coverage
index. The CPU median uses seven separate, interleaved runs per mode and
includes index loading. Each mode produced byte-identical output across
the seven runs. The runner's default output also matched the unmodified V5
entry point. The official scorer validated both outputs.

| Metric | Baseline | Experimental |
| --- | ---: | ---: |
| Simulated seconds | 520.233 | 450.989 |
| Missed commits | 0 | 0 |
| Full-suite fallbacks | 10 | 8 |
| Tests selected across the batch | 16,964 | 13,726 |
| Selector CPU median | 859.375 ms | 921.875 ms |
| Docker build and runtime | Unavailable | Unavailable |

Two mixed patches with ordinary Python comment additions each went from
1,620 selected tests to one observed fixture reader. The third mixed patch
included a Python behavior change and retained its original full-suite
fallback. Simulated savings were 69.244 seconds (13.31%). Median CPU increased
by 62.5 ms. The timings are local Windows measurements rather than scorer
container timings. Official leaderboard values were not reproduced here.

```text
python -B run_mixed_v5.py PATH_TO_KIT/sample
python -B run_mixed_v5.py PATH_TO_KIT/sample --mixed-comments
python -B PATH_TO_KIT/score.py PATH_TO_KIT/sample PATH_TO_KIT/sample/answers.json CAPTURED_JSONL
python -B -m unittest discover -q
python -m compileall -q mixed_patch_impact.py run_mixed_v5.py test_mixed_patch_impact.py
```

The full suite passed 84 tests, including seven new test methods. Full-suite
discovery on Windows used pytest and coverage in a separate virtual environment
and a compatibility stub for the Unix `resource` module. This stub only
replaces optional RSS reporting. Docker and GNU Make were unavailable, so
there was no container build or runtime validation.

## Review recommendation

Keep the experiment opt-in. The narrow gate keeps behavior-changing Python
patches on V5's existing path and avoids making a coverage-based claim that
they are unaffected. AST equality still cannot rule out effects from source
inspection, source line numbers, comment-aware tools or shared fixture
readers that baseline tracing did not observe. Local zero misses therefore
support review and further validation, with baseline submission behavior
preserved until broader evidence is available.

## C2 Docker integration validation

The official entry point now accepts `V5_MIXED_MODE=baseline` or
`V5_MIXED_MODE=comment_only_fixture`. The Docker image defaults to the
experimental mode; setting the variable to `baseline` restores the original V5
selector path. The image copies `mixed_patch_impact.py` and retains the same
Python entry point and runtime limits.

Clean `linux/amd64` build with networking disabled passed. Under the official
offline runtime limits, both modes emitted 30 JSONL records accepted by the
official local scorer:

| Mode | Simulated | Misses | Full-suite fallbacks | Selector CPU |
| --- | ---: | ---: | ---: | ---: |
| Baseline | 520.233 s | 0 | 10 | 808.123 ms |
| Comment-only fixture | 450.989 s | 0 | 8 | 897.689 ms |

Only two sample patch decisions changed. `986c` changes
`sqlglot/dialects/singlestore.py`, `sqlglot/typing/snowflake.py` and
`tests/fixtures/optimizer/unnest_subqueries.sql`; its selection shrank from
1,620 tests to one and schedule cost from 36,817 ms to 2,329 ms. `bvw2`
changes `sqlglot/dialects/materialize.py` and
`tests/fixtures/optimizer/annotate_types.sql`; its selection also shrank from
1,620 tests to one and schedule cost from 36,817 ms to 2,061 ms. No other
sample worker selection changed. The image default produced byte-identical
JSONL to the explicit experimental switch.

The 72 directly relevant V5 unit tests passed. No coverage map was rebuilt.
The experiment still depends on the observed fixture-reader map being
complete. AST equality also cannot rule out tests or tools that inspect source
comments or line positions. Zero local misses do not establish hidden-patch
recall.
