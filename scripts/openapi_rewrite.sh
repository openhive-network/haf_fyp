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
python3 "${PROCESS_OPENAPI}" "${OUTPUT_DIR}" "${ENDPOINTS_DIR}/endpoint_schema.sql"

# Copy generated endpoint_schema.sql back (contains the embedded JSON blob)
cp "${OUTPUT_DIR}/endpoints/endpoint_schema.sql" "${ENDPOINTS_DIR}/endpoint_schema.sql"

# Reverse the generated rewrite_rules.conf so specific rules come first
# (nginx processes rewrites top-to-bottom; the generator puts the default last,
#  so we reverse to ensure specific paths are matched before the catch-all)
awk '
BEGIN { RS=""; FS="\n" }
{
    comment=""; rewrite=""
    for (i=1; i<=NF; i++) {
        if ($i ~ /^#/) comment=$i
        else if ($i ~ /^rewrite/) rewrite=$i
    }
    if (NR>1) print ""
    print comment
    print rewrite
}
' rewrite_rules.conf | tac > "${ENDPOINTS_DIR}/rewrite_rules.conf"

# Clean up temp files
rm -f rewrite_rules.conf
rm -rf "${OUTPUT_DIR}"

echo "Done. Review and commit endpoints/endpoint_schema.sql and endpoints/rewrite_rules.conf."
