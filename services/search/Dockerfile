FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never ARTORIAS_SEARCH_DB=/data/search.sqlite
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable && useradd --uid 10001 --create-home search && mkdir /data && chown search /data
USER search
VOLUME /data
EXPOSE 8766
CMD ["/app/.venv/bin/artorias-search", "--host", "0.0.0.0", "--port", "8766"]
