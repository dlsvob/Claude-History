FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install claude-archive from pre-built wheel + remaining app dependencies
COPY vendor/ vendor/
RUN uv pip install --system vendor/*.whl "flask>=3.1.3" "gunicorn>=22.0" "python-docx>=1.2.0" "google-cloud-storage>=2.18.0" && \
    rm -rf vendor/

# Copy app source
COPY app.py parser.py bridge.py main.py session_manager.py providers.py ./
COPY templates/ templates/
COPY static/ static/

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "app:app"]
