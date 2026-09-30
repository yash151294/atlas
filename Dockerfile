# The app as a container: the same wheel and models as the laptop, nothing downloaded at start.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1 UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" \
    HOME=/app FASTEMBED_CACHE_PATH=/app/.cache/fastembed ATLAS_CONF=/app/conf PYTHONUNBUFFERED=1 ATLAS_REQUIRE_AUTH=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# the rules-engine model, the reranker and the DuckDB extensions are baked in, so the container needs no egress to become ready
RUN uv pip install --python /opt/venv https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl \
 && python -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL fts;')" \
 && python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; TextCrossEncoder('Xenova/ms-marco-MiniLM-L-6-v2')"
COPY atlas atlas
COPY app app
COPY conf conf
COPY data/labels data/labels
COPY data/world.json data/world.json
# --inexact: an exact sync would remove the spaCy model installed above. The runtime user owns nothing under /app: code and models stay root-owned, read-only; every write goes to OneLake, DuckDB spills to /tmp.
RUN uv sync --frozen --no-dev --inexact && useradd -r -u 10001 atlas && chmod -R a+rX /app/.cache /app/.duckdb
USER atlas
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
