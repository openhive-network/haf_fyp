#!/bin/bash
# Regenerates the embedded OpenAPI JSON in endpoint_schema.sql and rewrites
# rewrite_rules.conf from the /** openapi:paths ... */ annotations.
#
# Run this after adding or changing endpoint annotations, then commit the result.
#
# Requires: pip install deepmerge jsonpointer pyyaml

set -euo pipefail
SCRIPTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENDPOINTS_DIR="${SCRIPTDIR}/../endpoints"
OUTPUT_DIR="${SCRIPTDIR}/openapi_out"

# PINNED, not "develop": the generator is fetched at run time, so a moving ref means
# the same annotations can produce different artifacts on different days with nothing
# to signal it.
#
# This default MUST match COMMON_CI_REF in .gitlab-ci.yml's openapi-drift job, which
# pins the same SHA. It only applies to a developer running the script by hand, and the
# whole point is that they regenerate with the generator CI validates against -- an
# earlier revision of this line defaulted to "develop", which let local and CI drift
# apart silently, which is the divergence this comment exists to prevent.
COMMON_CI_REF="${COMMON_CI_REF:-96889a650b3f45244cecc4bb3f250af439a71900}"
COMMON_CI_URL="https://gitlab.syncad.com/hive/common-ci-configuration/-/raw/${COMMON_CI_REF}"
PROCESS_OPENAPI="${SCRIPTDIR}/process_openapi.py"
OFFLINE="${COMMON_CI_OFFLINE:-0}"

# Always re-fetch. Skipping when the file already exists (it is gitignored, so it
# survives every checkout) meant a stale cached helper from whatever ref the developer
# first ran silently produced different artifacts than a colleague's -- the same class
# of drift the pin above prevents, one level down. It also made COMMON_CI_REF a no-op
# on any machine that had already run this script once.
if [[ "${OFFLINE}" == "1" ]]; then
    if [[ ! -f "${PROCESS_OPENAPI}" ]]; then
        echo "ERROR: COMMON_CI_OFFLINE=1 but ${PROCESS_OPENAPI} has never been downloaded." >&2
        echo "       Run once with network access first." >&2
        exit 1
    fi
    echo "Using cached process_openapi.py (COMMON_CI_OFFLINE=1) -- ref not verified."
else
    echo "Fetching process_openapi.py from common-ci-configuration (ref: ${COMMON_CI_REF})..."
    curl -fsSL "${COMMON_CI_URL}/haf-app-tools/python/process_openapi.py" -o "${PROCESS_OPENAPI}"
fi

pip install deepmerge jsonpointer pyyaml -q

echo "Running process_openapi.py..."
# IMPORTANT: pass a RELATIVE input path and run from the repo root. The generator
# writes its output to "${OUTPUT_DIR}/<input_path>"; since Python's
# `Path(out) / Path("/abs/...")` discards the left side and returns the absolute
# path, passing an absolute input would make it open (and truncate) the source
# file itself. A relative path keeps the output inside OUTPUT_DIR. The generator
# also writes rewrite_rules.conf into the CWD, hence the repo-root cd.
# PYTHONUTF8=1 forces UTF-8 for all file I/O regardless of the host locale. Without it,
# process_openapi.py reads the UTF-8 source under the platform default (e.g. cp1252 on
# Windows) and mangles non-ASCII in the regenerated JSON blob — em-dashes become mojibake
# (â€”) in unrelated endpoint descriptions. No effect where UTF-8 is already
# the default (Linux/CI).
( cd "${SCRIPTDIR}/.." && PYTHONUTF8=1 python3 "${PROCESS_OPENAPI}" "${OUTPUT_DIR}" "endpoints/endpoint_schema.sql" )

# Copy generated endpoint_schema.sql back (contains the embedded JSON blob), then
# strip trailing whitespace the generator leaves on "RETURNS <type> " lines so the
# file passes `git diff --check`.
cp "${OUTPUT_DIR}/endpoints/endpoint_schema.sql" "${ENDPOINTS_DIR}/endpoint_schema.sql"
sed -i 's/[[:space:]]*$//' "${ENDPOINTS_DIR}/endpoint_schema.sql"

# Build endpoints/rewrite_rules.conf from the generated rewrite_rules.conf:
#  - the generator emits "# comment" then "rewrite ...", with the catch-all first;
#    reverse the BLOCK order so the catch-all/default ends up LAST (first match
#    wins in nginx) while keeping each comment above its rewrite;
#  - anchor specific patterns with a trailing "$" (the generator omits it, so
#    "^/health" would also match "/healthXYZ"); "^/$" and "^/(.*)$" already end in $;
#  - prepend a header.
{
    echo "# nginx rewrite rules — auto-generated from OpenAPI annotations in endpoint_schema.sql."
    echo "# Run scripts/openapi_rewrite.sh to regenerate after changing endpoint paths."
    echo "# Specific rules must precede the default catch-all (first match wins in nginx)."
    awk 'BEGIN { RS=""; FS="\n" } { block[NR]=$0 }
         END { for (i=NR; i>=1; i--) if (block[i] != "") { print ""; print block[i] } }' \
        "${SCRIPTDIR}/../rewrite_rules.conf"
} | sed -E 's#^(rewrite \^[^ ]*[^$]) #\1$ #' > "${ENDPOINTS_DIR}/rewrite_rules.conf"

# Clean up temp files
rm -f "${SCRIPTDIR}/../rewrite_rules.conf"
rm -rf "${OUTPUT_DIR}"

echo "Done. Review and commit endpoints/endpoint_schema.sql and endpoints/rewrite_rules.conf."
