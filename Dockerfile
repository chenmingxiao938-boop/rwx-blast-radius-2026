# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV V5_FIXTURE_MODE=aggressive_fixture_only \
    V5_FIXTURE_SAFETY_MODE=conservative \
    V5_COVERAGE_MODE=subprocess \
    V5_METHOD_MODE=ast_method_body \
    V5_AST_METHOD_SELECTION=disabled \
    V5_MIXED_MODE=comment_only_fixture
COPY selector.py /app/selector.py
COPY patch_parser.py method_boundary.py impact_selector.py v5_main.py /app/
COPY mixed_patch_impact.py /app/mixed_patch_impact.py
COPY coverage_index.json.gz coverage_index_b31.json.gz source_ranges.json.gz dependency-map.json.gz source_snapshot.json.gz /app/
CMD ["python", "/app/v5_main.py"]
