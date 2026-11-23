# Enterprise Ontology Platform image (built with Podman / Buildah).
#   podman build -t localhost/ontology-platform:0.1.0 -f Containerfile .
#
# Fully-qualified base image names avoid Podman short-name resolution prompts.
FROM docker.io/library/python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[postgres]"

# Sample dataset (local test adapter data) is baked in so demos need no bind mounts.
COPY sample_data ./sample_data

# Unprivileged user; works under rootless Podman (maps into the user namespace).
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin ontology \
    && chown -R ontology:ontology /app
USER 10001

EXPOSE 8000 8001

ENTRYPOINT ["ontology-platform"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
