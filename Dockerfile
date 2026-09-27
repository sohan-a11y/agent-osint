FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 AGENT_OSINT_HOME=/data
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir ".[c2pa]" && useradd --create-home app && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]

# Choose the server at run time, e.g.:
#   docker run -i --rm -v osint-data:/data agent-osint agent-osint-depcheck
#   docker run --rm -p 8000:8000 agent-osint agent-osint-sitegraph serve --transport streamable-http
CMD ["agent-osint-depcheck"]
