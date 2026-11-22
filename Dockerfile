# Use the scorer's pinned Linux/amd64 Python base.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS base

FROM base AS pinned_source
COPY fetch_pinned_test_sources.py /build/fetch_pinned_test_sources.py
RUN python /build/fetch_pinned_test_sources.py

FROM base
ENV PYTHONDONTWRITEBYTECODE=1
COPY --from=pinned_source /opt/sqlglot /opt/sqlglot
COPY c10_experiment.py c10_ast.py c10_semantic.py c10_budget.py c10_binary_budget.py /app/
COPY c11a_select.py c11b_evidence.py c11b_policy.py /app/
COPY c12_a_main.py c12_noop.py /app/
COPY selector.py patch_parser.py method_boundary.py impact_selector.py mixed_patch_impact.py /app/
COPY coverage_index_b31.json.gz source_ranges.json.gz dependency-map.json.gz source_snapshot.json.gz /app/
COPY shared_fixture_map.json fixture_path_shadow_map.json /app/
CMD ["python", "/app/c12_a_main.py"]
