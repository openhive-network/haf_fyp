FROM registry.gitlab.syncad.com/hive/common-ci-configuration/psql:14-2

WORKDIR /app

COPY db/             /app/db/
COPY endpoints/      /app/endpoints/
COPY --chmod=755 scripts/ /app/scripts/
# Both at /app/<name>, matching where every other HAF app image keeps them, so a module
# file or runbook that reaches for the path rather than for image metadata finds them.
# reputation_tracker.yaml, the reference #27 cites, hardcodes
# `test: ["CMD-SHELL","/app/block-processing-healthcheck.sh"]` -- put the script
# anywhere else and a compose file copied from it gets file-not-found, i.e. permanently
# unhealthy.
COPY --chmod=755 docker/docker_entrypoint.sh            /app/docker_entrypoint.sh
COPY --chmod=755 docker/block-processing-healthcheck.sh /app/block-processing-healthcheck.sh
# Beside the script, which resolves it relative to its own location. The predicate is a
# file rather than a shell-interpolated string so it can be compared whole in a test --
# a substring assertion cannot tell `a - b` from `b - a`, and that swap makes the check
# permanently healthy.
COPY docker/block-processing-healthcheck.sql            /app/block-processing-healthcheck.sql

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

# Verb dispatcher: install_app | process_blocks | uninstall_app (#27). Every other
# haf_api_node app image exposes that trio, which is what lets a module file say
# `command: [install_app]` instead of naming a script path inside someone else's
# image. This one used to be a bare /bin/bash, so haf_fyp's compose services had to
# spell out /app/scripts/install_app.sh -- workable in this repo's own compose, a
# blocker for shipping haf_fyp as a standard module.
#
# Not a breaking change: an argument that is not a verb but IS an executable path is
# exec'd as-is (see the script), so the existing `command: ["/app/scripts/install_app.sh"]`
# form keeps working and an operator mid-migration is not stranded.
ENTRYPOINT ["/app/docker_entrypoint.sh"]

# NO `HEALTHCHECK` instruction here, deliberately. An earlier revision of this MR added
# one, reasoning that a module file which forgets to wire a check should still get one.
# Two things wrong with that. Upstream does not: reputation_tracker's and
# balance_tracker's Dockerfiles ship the script and let each app's compose file attach
# it to the block-processing service, so declaring it here is a DEVIATION from the
# contract this MR exists to meet. And an image-level healthcheck applies to every
# container built from the image -- including haf-fyp-setup and haf-fyp-uninstall, which
# run once and exit, and for which "is the block loop advancing" is meaningless. This
# app has already produced one false alert from a health-check change (!41); inheriting
# a check onto two containers that cannot satisfy it invites the next one.
#
# docker/docker-compose.yml attaches it to haf-fyp-sync, which is the only service it
# describes.

# Provenance, matching Dockerfile.python and Dockerfile.rewriter. GIT_HASH above is
# consumed by install_app.sh (#20) and ends up in haf_fyp_app.app_version, which is a
# runtime lookup; these labels make the IMAGE itself traceable without a database.
ARG BUILD_TIME
ARG GIT_COMMIT_SHA
ARG GIT_CURRENT_BRANCH
LABEL org.opencontainers.image.created="$BUILD_TIME"
LABEL org.opencontainers.image.source="https://gitlab.syncad.com/hive/haf_fyp"
LABEL org.opencontainers.image.revision="$GIT_COMMIT_SHA"
LABEL org.opencontainers.image.title="HAF FYP SQL (schema, endpoints, installer)"
LABEL io.hive.image.branch="$GIT_CURRENT_BRANCH"
