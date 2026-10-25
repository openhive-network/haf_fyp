# The image AIDEV runs this project's checks in (`environment.image` in
# .aidev/project.yaml). Checks run with the caller's uid and, for the slots that
# start the test stack (.aidev/test-stack.compose.yml), on that stack's network.
# It carries what .gitlab-ci.yml's lint and test jobs install, each in its own
# environment so one job's packages cannot change another job's verdict:
#
#   python:3.11-slim (python-lint / python-tests / fastapi-tests' image) with
#     requirements.txt + pytest, pytest-cov, httpx, pyyaml (python-tests) in the
#     system site-packages, and git, curl, jq, bash
#   /opt/lint        ruff + mypy and their pinned set (python-lint; requirements.txt is
#                    deliberately NOT installed there, as in CI) and sqlfluff 3.3.0 (sql-lint)
#   /opt/openapi     openapi_rewrite.sh's deepmerge/jsonpointer/pyyaml and
#                    openapi-spec-validator 0.9.0 (openapi-drift), plus
#                    /opt/openapi/process_openapi.py: the generator at common-ci-configuration
#                    96889a65 (openapi-drift's COMMON_CI_REF), checksum-verified, so the
#                    check runs offline (COMMON_CI_OFFLINE=1)
#   /opt/tavern      tavern 3.6.3 (postgrest-tests' tavern container)
#   psql 18 (PGDG; the install lanes' verify client is postgres:18-alpine)
#   shellcheck 0.11.0, hadolint 2.12.0, the docker CLI + compose plugin (compose-validate's
#   `docker compose config`, which needs no daemon), postgrest 12.2.3 (postgrest-tests)
#   /opt/psql-image/usr/local/bin/install_with_app_lock.py, from the psql image
#   Dockerfile.sql is built FROM (common-ci-configuration/psql:14-3): in that image
#   scripts/install_app.sh takes HAF's install lock through it, and the install lanes
#   check that it does. It is not at /usr/local/bin, where the unit tests (CI's
#   python:3.11-slim has no wrapper) would find it; .aidev/compose-lanes.yaml points
#   the lanes' Dockerfile.sql services at it. psycopg2 comes from requirements.txt.
#   /opt/psql-image/usr/local/bin/haf_app_driver.py, from the same image: the HAF
#   block-processing driver scripts/process_blocks.sh execs there. Off PATH for the
#   same reason; the replay steps put that directory on PATH.
#
# Inputs: this file and requirements.txt (the only file in the build context). Every
# FROM is digest-pinned. Build and pin it with .aidev/runtime/build.sh; see
# .aidev/README.md.
FROM koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d AS shellcheck
FROM hadolint/hadolint:v2.12.0@sha256:30a8fd2e785ab6176eed53f74769e04f125afb2f74a6c52aef7d463583b6d45e AS hadolint
FROM docker:24-cli@sha256:2b62f89aaab9e4df0ff843f0d9a7bbd17340a9ba7c660ba6f40b1f00b38c2a4d AS docker
FROM postgrest/postgrest:v12.2.3@sha256:729bf65c733b73f5b52777f0e4b853f22ed73aa67a22d38269d289779b0a8401 AS postgrest
FROM registry.gitlab.syncad.com/hive/common-ci-configuration/psql:14-3@sha256:6a3794d9fdc4e0590789fc01e48539bcd73273715e3b69dd1f45d3ecde8b1cde AS psql

FROM python:3.11-slim@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377

RUN apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
        git curl jq bash ca-certificates gnupg procps \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update -qq \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=shellcheck /bin/shellcheck /usr/local/bin/shellcheck
COPY --from=hadolint /bin/hadolint /usr/local/bin/hadolint
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=postgrest /bin/postgrest /usr/local/bin/postgrest
COPY --from=psql /usr/local/bin/install_with_app_lock.py /opt/psql-image/usr/local/bin/install_with_app_lock.py
COPY --from=psql /usr/local/bin/haf_app_driver.py /opt/psql-image/usr/local/bin/haf_app_driver.py

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -q -r /tmp/requirements.txt \
        pytest==9.1.1 pytest-cov==7.0.0 httpx==0.28.1 pyyaml==6.0.3 \
    && rm /tmp/requirements.txt \
    && python -m venv /opt/lint \
    && /opt/lint/bin/pip install --no-cache-dir -q ruff==0.15.12 \
        mypy==2.4.0 ast-serialize==0.13.1 librt==0.16.0 pathspec==1.1.1 \
        typing_extensions==4.16.0 mypy_extensions==1.1.0 \
        types-redis==4.6.0.20241004 types-pyOpenSSL==24.1.0.20240722 \
        sqlfluff==3.3.0 \
    && python -m venv /opt/openapi \
    && /opt/openapi/bin/pip install --no-cache-dir -q deepmerge jsonpointer pyyaml openapi-spec-validator==0.9.0 \
    && curl -fsSL https://gitlab.syncad.com/hive/common-ci-configuration/-/raw/96889a650b3f45244cecc4bb3f250af439a71900/haf-app-tools/python/process_openapi.py \
        -o /opt/openapi/process_openapi.py \
    && echo "292e8825e36117357983f8d669ba5d9805a33127ace28869feaa29731ecb6f0a  /opt/openapi/process_openapi.py" | sha256sum -c - \
    && echo 96889a650b3f45244cecc4bb3f250af439a71900 > /opt/openapi/COMMON_CI_REF \
    && python -m venv /opt/tavern \
    && /opt/tavern/bin/pip install --no-cache-dir -q tavern==3.6.3

ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT []
CMD ["bash"]
