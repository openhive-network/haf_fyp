#!/bin/sh
# Entrypoint for the haf_fyp PostgREST URL rewriter (openresty).
# Substitutes runtime placeholders into the nginx config template, then starts openresty.
#
#   REWRITE_LOG     - set to "on" to enable nginx rewrite logging (default: off)
#   POSTGREST_HOST  - hostname of the PostgREST service to proxy to (default: postgrest)
#   POSTGREST_PORT  - port of the PostgREST service (default: 3000)

if [ "$REWRITE_LOG" = "on" ]; then
    REWRITE_LOG="rewrite_log on;"
else
    REWRITE_LOG="# rewrite_log off;"
fi

POSTGREST_HOST="${POSTGREST_HOST:-postgrest}"
POSTGREST_PORT="${POSTGREST_PORT:-3000}"

sed -e "s|\${REWRITE_LOG}|$REWRITE_LOG|g" \
    -e "s|\${POSTGREST_HOST}|$POSTGREST_HOST|g" \
    -e "s|\${POSTGREST_PORT}|$POSTGREST_PORT|g" \
    /usr/local/openresty/nginx/conf/nginx.conf.template \
    > /usr/local/openresty/nginx/conf/nginx.conf

exec /usr/local/openresty/bin/openresty -g 'daemon off;'
