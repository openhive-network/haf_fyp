FROM registry.gitlab.syncad.com/hive/common-ci-configuration/psql:14-2

WORKDIR /app

COPY db/             /app/db/
COPY endpoints/      /app/endpoints/
COPY --chmod=755 scripts/ /app/scripts/

# Commit this image was built from. scripts/install_app.sh (step 15) writes it into
# haf_fyp_app.app_version so GET /version can name the running commit (#20). Empty by
# default, in which case the installer records the placeholder rather than a fake version.
#
# BAKED AT BUILD TIME ON PURPOSE. This image carries db/ and endpoints/ and scripts/, so the
# hash has to describe the code inside the image, not whatever the host happened to export
# when the container started. That is also why docker-compose passes it as a build arg and
# not as an `environment:` entry -- a runtime override would let a stale image be labelled
# with a newer commit, the undetectable-wrong-hash failure step 15 exists to avoid.
# The practical consequence, documented in README.md: changing GIT_HASH requires a REBUILD
# ('docker compose up --build'). A plain 'up' or 'run' reuses the existing image and its
# already-baked value.
#
# Deliberately an ARG/ENV pair rather than a RUN that generates db/version.sql the way
# haf_stats does. No RUN means nothing has to write into /app, which COPY created as root
# while this base image runs as haf_admin -- the exact wall haf_stats hit, and which it
# cleared only by switching the image to USER root and then having to silence DL3002 and
# DL3066 in its hadolint job. It also keeps the SQL in a reviewed file instead of a shell
# string interpolated at build time.
ARG GIT_HASH=""
ENV GIT_HASH=${GIT_HASH}

ENTRYPOINT ["/bin/bash"]
