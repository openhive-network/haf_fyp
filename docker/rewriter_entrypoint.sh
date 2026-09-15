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
# bind-mounted config for this image without also setting POSTGREST_HOST would
# get 502 on first boot.
#
# The asymmetry this comment used to describe is GONE: the reference stack named its
# service plain "postgrest", so this default was right for a deployment and wrong for
# that stack. #48 took the second of the two options offered here and renamed the
# service to haf-fyp-postgrest, so the default is now correct in BOTH. docker-compose.yml
# still sets POSTGREST_HOST explicitly -- agreeing with this default rather than
# overriding it -- so that changing this line cannot silently move that stack's upstream.
POSTGREST_HOST="${POSTGREST_HOST:-haf-fyp-postgrest}"
POSTGREST_PORT="${POSTGREST_PORT:-3000}"

sed -e "s|\${REWRITE_LOG}|$REWRITE_LOG|g" \
    -e "s|\${POSTGREST_HOST}|$POSTGREST_HOST|g" \
    -e "s|\${POSTGREST_PORT}|$POSTGREST_PORT|g" \
    /usr/local/openresty/nginx/conf/nginx.conf.template \
    > /usr/local/openresty/nginx/conf/nginx.conf

exec /usr/local/openresty/bin/openresty -g 'daemon off;'
