# syntax=docker/dockerfile:1.7
#
# Build context is this directory (mcp_servers/schwab_connector/).
# From ../docker-compose.yml: `build: { context: ./schwab_connector }`.

FROM condaforge/miniforge3:24.9.0-0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONDA_ENV=traider

# Create the traider env with Python 3.13 and the TA-Lib C library +
# Python wrapper from conda-forge. This is the install path the project
# README recommends; pip-installing TA-Lib alone would need the native
# lib on the base image.
RUN conda create -n "${CONDA_ENV}" -c conda-forge -y \
        python=3.13 \
        ta-lib \
    && conda clean -afy

SHELL ["conda", "run", "--no-capture-output", "-n", "traider", "/bin/bash", "-c"]

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -e .

EXPOSE 8765

ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "traider", "schwab-connector"]
CMD ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
