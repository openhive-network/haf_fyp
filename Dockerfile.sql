FROM registry.gitlab.syncad.com/hive/common-ci-configuration/psql:14-2

USER root

WORKDIR /app

COPY db/             /app/db/
COPY endpoints/      /app/endpoints/
COPY --chmod=755 scripts/ /app/scripts/

ENTRYPOINT ["/bin/bash"]
