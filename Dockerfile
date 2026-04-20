# syntax=docker/dockerfile:1.7
#
# Single unified image for the traider hub. All connector deps are
# installed once; which tool groups actually run inside is gated by
# TRAIDER_PROFILES at startup (see src/traider/server.py).

FROM condaforge/miniforge3:24.9.0-0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONDA_ENV=traider

# Python + TA-Lib C library + wrapper from conda-forge. TA-Lib needs
# the native C lib; conda-forge is the project-recommended install
# path. yfinance, lxml, beautifulsoup4, mcp, httpx, etc. are pip-only
# and installed from pyproject.toml below.
RUN conda create -n "${CONDA_ENV}" -c conda-forge -y \
        python=3.13 \
        ta-lib \
    && conda clean -afy

SHELL ["conda", "run", "--no-capture-output", "-n", "traider", "/bin/bash", "-c"]

WORKDIR /app

# Install Python deps first so code edits don't invalidate this layer.
# Stub the package tree so the hatchling editable install has a path to
# link against; the real source is copied in below.
COPY pyproject.toml /app/
RUN mkdir -p src/traider \
    && touch src/traider/__init__.py \
    && pip install --no-cache-dir -e .

COPY src /app/src

EXPOSE 8765

ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "traider", "traider"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
