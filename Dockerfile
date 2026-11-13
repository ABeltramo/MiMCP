FROM ghcr.io/astral-sh/uv:python3.12-bookworm

WORKDIR /app

# install deps first so source edits don't bust the layer
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

ENV MIMCP_HOST=0.0.0.0 \
    MIMCP_PORT=8000

EXPOSE 8000

CMD ["uv", "run", "mimcp"]
