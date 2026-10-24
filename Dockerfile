# Build stage
FROM python:3.13-slim AS build
WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# Runtime stage
FROM python:3.13-slim
WORKDIR /app

# Node.js for @modelcontextprotocol/server-filesystem and tavily-mcp, both
# launched through npx.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm && \
    rm -rf /var/lib/apt/lists/*

# Keep Python dependencies accessible to the non-root runtime user.
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Fetch uses ReadabiliPy's Node-based HTML extractor. Install its JavaScript
# dependencies while building as root, rather than on the first tool call.
# Pin the extractor packages: newer jsdom releases require a newer Node runtime.
RUN python -c "import pathlib, readabilipy, subprocess; subprocess.run(['npm', 'install', '--omit=dev', '--no-audit', '--no-fund', '--save-exact', 'jsdom@26.1.0', '@mozilla/readability@0.6.0', 'minimist@1.2.8'], cwd=pathlib.Path(readabilipy.__file__).parent / 'javascript', check=True)"

COPY . .

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

# Pre-warm the two npx-launched MCP servers into npm's cache so the first
# /chat request doesn't pay an npm-registry round trip (and so a cold
# container with restricted egress at request time still has the packages
# available locally). Safe to skip silently if the build network can't
# reach the registry; npx will just fetch on first use instead.
RUN npx -y --quiet @modelcontextprotocol/server-filesystem --help > /dev/null 2>&1 || true
RUN npx -y --quiet tavily-mcp@latest --version > /dev/null 2>&1 || true

EXPOSE 8080
CMD ["python3", "main.py"]
