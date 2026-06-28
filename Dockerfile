# Base-image registry prefix. Empty default = public Docker Hub; a private
# deploy mirror passes --build-arg BASE=registry.example/library/ .
ARG BASE=
# Per-item CMAF packager. Slim image — no CUDA, no faster-whisper, no
# chromaprint. Just ffmpeg + shaka-packager + a small Python worker that
# claims items from katalog and runs the package.
#
# This used to live as a thread inside katalog-analyzer's main loop. The
# split lets a packager OOM or codec crash take down only this pod, lets
# us scale packager replicas independently (CPU-bound), and shaves the
# image from ~3 GB (CUDA base) to ~250 MB.
FROM ${BASE}python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg / ffprobe: source-side transmux + per-stream probe + trickplay
# sprite generation. curl + ca-certificates: fetch the shaka-packager
# binary at build time and validate TLS to katalog at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      ca-certificates \
      curl \
    && rm -rf /var/lib/apt/lists/*

# shaka-packager: Google's CMAF/HLS/DASH packager. Pinned to the same
# version used historically by the analyzer pod so package layout
# remains identical across the cutover.
ARG SHAKA_PACKAGER_VERSION=v3.4.2
RUN curl -fsSL -o /usr/local/bin/packager \
      "https://github.com/shaka-project/shaka-packager/releases/download/${SHAKA_PACKAGER_VERSION}/packager-linux-x64" \
    && chmod +x /usr/local/bin/packager \
    && /usr/local/bin/packager --version

WORKDIR /app

# Runtime deps first (cached separately from source).
RUN pip install httpx==0.28.1 structlog==25.4.0 fastapi==0.118.0 "uvicorn[standard]==0.32.0"

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps -e .

# OpenShift runs containers under a random non-root uid that's in GID 0.
# Make everything group-writable so the runtime can own it.
RUN chown -R 0:0 /app && chmod -R g=u /app

EXPOSE 8080
CMD ["python", "-m", "packager.main"]
