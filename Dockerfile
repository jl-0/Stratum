# One image, two entrypoints (08 section 2). Built by `make image`, never by Terraform, and
# pinned into a deployment by digest (ADR-0003).
#
# The environment comes from pixi.lock, the same file a laptop resolves from, so the image cannot
# drift from the environment the tests ran in. That is why this is not the AWS Lambda Python base
# image with pip wheels: GDAL comes from conda-forge (ADR-0001), and a second resolution is a
# second set of versions to be surprised by.
#
# The target is arm64. Lambda runs it on Graviton, it is native on an Apple-silicon laptop, so
# `make image` builds without emulation, and `linux-aarch64` is one of the lock's platforms.
FROM ghcr.io/prefix-dev/pixi:0.78.0-jammy

# libcurl and the CA bundle are for the Lambda Runtime Interface Client and for Earthdata over
# HTTPS; everything else the pipeline needs comes from the pixi environment.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates libcurl4 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/stratum

# The manifest and the lock first, so a source-only change does not re-solve the environment.
COPY pyproject.toml pixi.lock README.md ./
# Both distributions and the vendored KD-tree library are path dependencies installed editable
# (ADR-0001 section 3: a non-editable build of SpectralUtil drops its subpackages), so their
# sources have to be in the image at the path the lock names.
COPY src/ ./src/
COPY plugins/ ./plugins/
COPY vendor/SpectralUtil/ ./vendor/SpectralUtil/

RUN pixi install --locked --environment default

# Out-of-repo plugin distributions. A project that is not a directory in this repository drops its
# built wheel into plugins/wheels/ and it is installed beside the framework - no edit to any file
# here, which is the point (ADR-0003).
#
# --no-deps, so a dropped wheel can never move a version the lock decided. Its own third-party
# dependencies must therefore already be in the environment; a plugin that needs a new one belongs
# in plugins/ as a path dependency of this workspace, where pixi will solve for it.
RUN set -eu; \
    if [ -n "$(find plugins/wheels -name '*.whl' 2>/dev/null)" ]; then \
      echo "installing plugin wheels:"; ls -1 plugins/wheels/*.whl; \
      pixi run -e default python -m pip install --no-cache-dir --no-deps plugins/wheels/*.whl; \
    else \
      echo "no wheels in plugins/wheels/; the image carries the in-repo plugins only"; \
    fi

# The Lambda Runtime Interface Client: pip-only, Linux-only, and deliberately not in pixi.lock,
# which has to solve for macOS too. Pinned, and installed with no dependency resolution of its
# own so it cannot move anything the lock decided.
ARG AWSLAMBDARIC_VERSION=4.0.3
RUN pixi run -e default python -m pip install --no-cache-dir --no-deps \
        "awslambdaric==${AWSLAMBDARIC_VERSION}" simplejson snapshot-restore-py

# Activation, resolved once at build time rather than on every cold start. It is what puts GDAL's
# and PROJ's data directories on the environment, which a bare PATH would not.
RUN pixi shell-hook --environment default --shell bash > /opt/stratum/activate.sh

COPY docker/entrypoint.sh /usr/local/bin/stratum-entrypoint
RUN chmod +x /usr/local/bin/stratum-entrypoint

# /tmp is the only writable filesystem on Lambda, and it is where the storage mirror and the
# staged granules go. 10 GB of it, against blocks of about 1 GB.
ENV STRATUM_SCRATCH=/tmp \
    STRATUM_ASSET_CACHE=/tmp/assets \
    HOME=/tmp \
    PYTHONUNBUFFERED=1 \
    GDAL_CACHEMAX=512 \
    # a granule read is one big sequential read, not a series of small ones
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif,.TIF,.nc,.NC

ENTRYPOINT ["/usr/local/bin/stratum-entrypoint"]
CMD ["stratum.executors.awslambda.handler"]
