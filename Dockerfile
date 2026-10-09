# Single image for web, worker and migrate (Compose in development, Dokku in production).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# Install dependencies first (from pyproject.toml) so code changes don't bust this layer.
ARG INSTALL_DEV=false
COPY pyproject.toml ./
RUN python -c "import os, tomllib; \
p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
deps = p['dependencies'] + (p['optional-dependencies']['dev'] if os.environ['INSTALL_DEV'] == 'true' else []); \
print(chr(10).join(deps))" > /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN useradd --create-home --uid 1000 app
COPY --chown=app:app . .
USER app

EXPOSE 5000
# Default process (Dokku web); Compose overrides the command per service.
CMD ["sh", "-c", "exec uvicorn ya_voy.api.main:app --host 0.0.0.0 --port ${PORT:-5000} --proxy-headers"]
