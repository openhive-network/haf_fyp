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

COMMON_CI_REF="${COMMON_CI_REF:-develop}"
COMMON_CI_URL="https://gitlab.syncad.com/hive/common-ci-configuration/-/raw/${COMMON_CI_REF}"
PROCESS_OPENAPI="${SCRIPTDIR}/process_openapi.py"

if [[ ! -f "${PROCESS_OPENAPI}" ]]; then
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
