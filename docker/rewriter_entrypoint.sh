#!/bin/sh
# Entrypoint for the haf_fyp PostgREST URL rewriter (openresty).
# Substitutes runtime placeholders into the nginx config template, then starts openresty.
#
#   REWRITE_LOG     - set to "on" to enable nginx rewrite logging (default: off)
#   POSTGREST_HOST  - hostname of the PostgREST service to proxy to (default: haf-fyp-postgrest)
#   POSTGREST_PORT  - port of the PostgREST service (default: 3000)

if [ "$REWRITE_LOG" = "on" ]; then
    REWRITE_LOG="rewrite_log on;"
else
    REWRITE_LOG="# rewrite_log off;"
fi

# Defaults to the service name a haf_api_node deployment uses. The old default,
# "postgrest", resolves to nothing there, so an operator who swapped the
# bind-mounted config for this image without also setting POSTGREST_HOST would get
# no rewriter at all: the template's upstream is a literal with no `resolver`, so
# nginx resolves it while parsing the config and openresty exits with
# "[emerg] host not found in upstream".
#
# "Matching haf_stats" is NOT the reason for this name: haf_stats' entrypoint defaults
# to its own docker/docker-compose.yml service name (haf-stats-api), which its node
# module must override. Here the default also names the service in THIS repo's
# docker-compose.yml -- since #48 renamed that service from plain "postgrest" to
# haf-fyp-postgrest -- so one default is right for both a node and the reference
# stack. docker-compose.yml still sets POSTGREST_HOST explicitly, agreeing with this
# line, so that changing it cannot silently move that stack's upstream.
POSTGREST_HOST="${POSTGREST_HOST:-haf-fyp-postgrest}"
POSTGREST_PORT="${POSTGREST_PORT:-3000}"

sed -e "s|\${REWRITE_LOG}|$REWRITE_LOG|g" \
    -e "s|\${POSTGREST_HOST}|$POSTGREST_HOST|g" \
    -e "s|\${POSTGREST_PORT}|$POSTGREST_PORT|g" \
    /usr/local/openresty/nginx/conf/nginx.conf.template \
    > /usr/local/openresty/nginx/conf/nginx.conf

exec /usr/local/openresty/bin/openresty -g 'daemon off;'
