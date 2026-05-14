FROM registry.gitlab.syncad.com/hive/common-ci-configuration/psql:14-2 AS base

WORKDIR /app

COPY db/         /app/db/
COPY endpoints/  /app/endpoints/
COPY scripts/    /app/scripts/

RUN chmod +x /app/scripts/*.sh

ENTRYPOINT ["/bin/bash"]
