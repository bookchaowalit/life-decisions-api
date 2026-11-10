FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

# Dependencies come from uv.lock (--frozen), so every build installs the versions CI tested.
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY migrations ./migrations
COPY db ./db
RUN uv sync --frozen --no-dev --no-editable --no-cache --python /usr/local/bin/python3 \
    && useradd --system --uid 10001 --no-create-home decisions
# Image hardening: take Debian security fixes newer than the base tag, then drop the base
# image's pip (the app runs from its installed environment; pip's vendored urllib3/msgpack/
# setuptools copies are what the Trivy image scan flags).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/python3 -m pip uninstall -y --disable-pip-version-check pip
USER decisions
# Secrets (database URLs, public key, publisher token) arrive as environment variables at runtime only.
# One image for both services: decisions-api (this CMD) and decisions-publisher (start command
# decisions-publish, set in .railway/railway.ts).
CMD ["decisions-api"]
