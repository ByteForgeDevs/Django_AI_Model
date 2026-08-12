# The image exists so a CI system without Python can still run the audit. See
# docs/container.md.
#
# It is built from this checkout rather than installed from PyPI, so the image
# published at a tag contains that tag's code. Installing a release by number
# would let the image and the tag drift, and the report carries `__version__`,
# so the drift would be published in the findings themselves.

FROM python:3.13-slim AS build

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir --disable-pip-version-check build \
    && python -m build --wheel --outdir /wheels

FROM python:3.13-slim

# djaudit reads the project it audits and, unless asked for the live tier,
# never executes it. Running as a non-root user is what keeps that true of the
# container as well: a mounted source tree stays readable and unwritable.
RUN useradd --create-home --uid 1000 djaudit

COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir --disable-pip-version-check /wheels/*.whl \
    && rm -rf /wheels

USER djaudit
WORKDIR /src

# `djaudit` rather than `djaudit run`, so every subcommand is reachable:
# `docker run ... rules`, `... explain`, `... triage`. The default argument
# audits what is mounted, which is what a bare `docker run` should do.
ENTRYPOINT ["djaudit"]
CMD ["run", "/src"]

LABEL org.opencontainers.image.title="djaudit" \
      org.opencontainers.image.description="Django-aware static analysis: security, DRF authorization, ORM performance, migration safety and database portability." \
      org.opencontainers.image.source="https://github.com/ByteForgeDevs/Django_AI_Model" \
      org.opencontainers.image.licenses="MIT"
