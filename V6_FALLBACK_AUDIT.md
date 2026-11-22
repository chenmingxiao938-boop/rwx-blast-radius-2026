# V6 mixed selector CPU and fallback audit

Base commit: `9dcc13b58d5d216c71404b02740f757a768b4750` from PR #2.
The independent branch leaves the V5 selector, scheduler, scorer and Dockerfile
unchanged. Experimental mode still requires `--mixed-comments`.

## Single change

Once V5 has produced a fallback and the full patch has parsed successfully,
check the parsed old and new paths for a SQL fixture before projecting or
replaying individual file changes. If no SQL fixture is present, return the
original V5 decision immediately. The previous implementation already
required a SQL fixture for a mixed override, so the early return preserves
selection. The test cases check Python-only fallback without replay and a
quoted fixture path that still takes the mixed path.

## Official sample comparison

Windows Python 3.13.9, current subprocess coverage index, and 30 official
patches. The CPU medians are from nine separate, interleaved processes per
mode. The official scorer evaluated each output. Every output generated in
mixed mode was identical to PR #2. Output with the experiment off was
identical to the original V5 baseline.

| Metric | PR #2 | V6 |
| --- | ---: | ---: |
| Simulated seconds | 450.989 | 450.989 |
| Missed commits | 0 | 0 |
| Full-suite fallbacks | 8 | 8 |
| Total selected tests | 13,726 | 13,726 |
| Median selector CPU | 921.875 ms | 859.375 ms |
| Unit tests passed | 84 | 86 |

The measured local CPU savings were 62.5 ms per batch. The scorer's simulated
time excludes CPU, and Docker was unavailable. No official runtime gain has
been measured. The target below 400 seconds was not reached.

## Follow-up experiment

Profiling identified the full-tree deep copy in method body masking as the
largest repeated cost. The follow-up copies only the AST module, the target
class and the target method, including their immediate body lists where needed.
The same method body is replaced by a `Pass` in the copy, and the same AST
comparison runs. The original tree, decorators, base classes, signatures and
sibling methods are preserved. A new test verifies the exact masked tree and
checks that the original is unmodified.

| Metric | Replay shortcut only | Replay shortcut and AST path copying |
| --- | ---: | ---: |
| Simulated seconds | 450.989 | 450.989 |
| Missed commits | 0 | 0 |
| Full-suite fallbacks | 8 | 8 |
| Total selected tests | 13,726 | 13,726 |
| Median selector CPU | 875.000 ms | 640.625 ms |
| Unit tests passed | 86 | 87 |

Nine separate processes per mode ran interleaved. Every mixed-mode output was
byte-for-byte equal to PR #2. The default output was equal to the original V5
baseline. The 234.375 ms reduction is from the second paired Windows benchmark.
It does not change simulated test execution time.

## Cost limits and remaining risk

For any fixed selected test set and worker count, the busiest worker must
take at least the larger of the longest test and the ceiling of total test
duration divided by the number of workers. Add the worker charge and minimize
across one to eight workers. Every schedule in the sample already attains
that lower bound. Repacking the same tests can save zero simulated seconds.

Each full-suite fallback costs 36.817 seconds, with a theoretical maximum
saving of 36.817 seconds if no test were needed. That assumption is unsafe.
Even eliminating one entire fallback could not meet the target. The eight
fallbacks represent 294.536 simulated seconds.

- Two Python-only changes insert ordinary comments. Exact patch replay and
  structural AST comparison passed, but positional AST comparison failed for
  both. A synthetic experiment also showed that equivalent ASTs can expose
  different source text and line numbers.
- Three dialect edits change a class constant, a tokenizer table value or a
  parser registration. A fourth parser edit changes a lambda inside a
  class-level registration table. Class creation, inheritance, dynamic
  dispatch and lazy import paths limit the available exclusion evidence.
  A synthetic probe showed a change to an inherited setting reaching a
  consumer through a computed module import and an indirect call.
- The expression class edit changes whether an argument is required. Imports,
  subclasses and runtime consumers can be affected.
- The remaining mixed change contains a substantive Python expression change
  and an optimizer fixture edit. The Python file coverage and known SQL
  readers produce 732 observed candidates, plus import-time coverage in those
  Python files. Observed coverage cannot prove that other tests are unaffected.
  The prior synthetic shared-fixture probe measured two affected consumers
  while the read tracer recorded only one.

Keep the selection policy unchanged. This review can land the output-preserving
CPU shortcut independently, with production timing verification still pending.

## Patch cost audit

Patch identifiers appear only in this report. No identifier, patch signature or
failure label is used to select tests. Costs exclude CPU and are sorted
descending.

| Patch | Kind | Tests | Workers | Cost (s) | Full fallback |
| --- | --- | ---: | ---: | ---: | --- |
| 4d63 | Python | 1620 | 4 | 36.817 | ast_structure_or_source_unverified; hunk_not_within_one_symbol:sqlglot/dialects/snowflake.py:130; import_time_line_changed:sqlglot/dialects/snowflake.py:130 |
| 535z | Python | 1620 | 4 | 36.817 | ast_structure_or_source_unverified; hunk_not_within_one_symbol:sqlglot/dialects/tsql.py:14 |
| 6ze8 | Python | 1620 | 4 | 36.817 | ast_structure_or_source_unverified; hunk_not_within_one_symbol:sqlglot/parsers/dremio.py:108 |
| bap8 | Python | 1620 | 4 | 36.817 | ast_structure_or_source_unverified; hunk_not_within_one_symbol:sqlglot/transforms.py:909; import_time_line_changed:sqlglot/transforms.py:909 |
| f4xa | Python | 1620 | 4 | 36.817 | ast_structure_or_source_unverified; hunk_not_within_one_symbol:sqlglot/generators/drill.py:116 |
| gbav | Python | 1620 | 4 | 36.817 | ast_structure_or_source_unverified; hunk_not_within_one_symbol:sqlglot/parsers/singlestore.py:210 |
| ktz3 | Python | 1620 | 4 | 36.817 | ast_structure_or_source_unverified; hunk_not_within_one_symbol:sqlglot/expressions/ddl.py:79; import_time_line_changed:sqlglot/expressions/ddl.py:79 |
| rszg | Mixed | 1620 | 4 | 36.817 | fixture_impact_completeness_unproved:tests/fixtures/optimizer/qualify_columns_ddl.sql; hunk_not_within_one_symbol:sqlglot/optimizer/scope.py:7; import_time_line_changed:sqlglot/optimizer/scope.py:7 |
| 2haz | Python | 213 | 2 | 25.454 | - |
| 2jrq | Python | 29 | 2 | 25.454 | - |
| 3utz | Python | 37 | 2 | 25.454 | - |
| 66ks | Python | 51 | 1 | 24.787 | - |
| ary6 | Python | 142 | 2 | 9.061 | - |
| j6uz | Python | 74 | 1 | 5.116 | - |
| 9u5k | Python | 47 | 1 | 4.439 | - |
| bj7g | Python | 44 | 1 | 4.004 | - |
| 2qn5 | Python | 47 | 1 | 3.093 | - |
| mthh | SQL fixture | 1 | 1 | 3.016 | - |
| 8y7j | Python | 44 | 1 | 2.629 | - |
| egys | SQL fixture | 1 | 1 | 2.596 | - |
| 5y72 | Python | 3 | 1 | 2.466 | - |
| 986c | Mixed | 1 | 1 | 2.329 | - |
| fryt | Python | 25 | 1 | 2.206 | - |
| nwnx | SQL fixture | 1 | 1 | 2.092 | - |
| 2vdj | SQL fixture | 1 | 1 | 2.083 | - |
| bvw2 | Mixed | 1 | 1 | 2.061 | - |
| gbhk | SQL fixture | 1 | 1 | 2.061 | - |
| 6vvp | SQL fixture | 1 | 1 | 2.037 | - |
| st8f | SQL fixture | 1 | 1 | 2.008 | - |
| f4bn | SQL fixture | 1 | 1 | 2.007 | - |
