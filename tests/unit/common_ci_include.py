"""What .gitlab-ci.yml's one `include:` brings into every job (#126).

The include is common-ci-configuration's haf_app_testing and haf_data_preparation
templates, for .find_haf_image. Their hidden jobs do nothing until a job here extends one,
and they define no visible job, `workflow:` or `stages:`. What does reach every job is the
pipeline-wide `variables:` and `default:` of those two files and of the files they include
(base, docker_image_jobs, source_change_detection). This module states them as they are at
REF, read from the templates at that commit, so that the lane tests -- which cannot fetch
an include -- can reason about it.

The include is pinned to REF for exactly that reason. The tests compare .gitlab-ci.yml's
include with INCLUDE, so moving the ref fails them until this file is re-read from the new
commit.
"""

from __future__ import annotations

REF = "e005ea4f2b4fd94f428232b568fe7c7ee24d00e4"

INCLUDE = [{
    "project": "hive/common-ci-configuration",
    "ref": REF,
    "file": ["/templates/haf_app_testing.gitlab-ci.yml", "/templates/haf_data_preparation.gitlab-ci.yml"],
}]

# Every pipeline-wide variable the included files define, by name.
VARIABLES = frozenset({
    "ALPINE_IMAGE", "ALPINE_IMAGE_TAG", "CACHE_MANAGER", "CACHE_MANAGER_REF", "CI_CHANGED",
    "CI_SCRIPT_PATTERNS", "DATA_CACHE_HAF_PREFIX", "DATA_CACHE_LOCAL_PREFIX",
    "DATA_CACHE_NFS_PREFIX", "DOCKER_BUILDER_TAG", "DOCKER_DIND_TAG", "DOCS_ONLY",
    "DOCS_PATTERNS", "EXTRACT_TEST_CACHE", "FORCE_FULL_PIPELINE", "HAF_APP_SKIP_PATTERNS",
    "HAF_APP_TEST_RUNNER_IMAGE", "HAF_APP_TEST_RUNNER_TAG", "HAF_EXTRACT_TIMEOUT",
    "HAF_SYNC_TIMEOUT", "HAF_TEST_TIMEOUT", "IMAGE_REMOVER_TAG", "NODE_IMAGE_VERSION",
    "NODE_TEST_TOOLS_IMAGE", "NODE_TEST_TOOLS_TAG", "NPM_CONFIG_REGISTRY",
    "PIP_DEFAULT_TIMEOUT", "PIP_INDEX_URL", "PIP_RETRIES", "PIP_TRUSTED_HOST",
    "PYTHON_TEST_TOOLS_CONSTRAINTS", "PYTHON_TEST_TOOLS_IMAGE", "PYTHON_TEST_TOOLS_TAG",
    "QUICK_TEST", "SOURCE_CHANGED", "SOURCE_CODE_PATTERNS", "TESTS_ONLY", "TEST_PATTERNS",
})

# base.gitlab-ci.yml's `default:`, merged with this file's own (which picks the runner tag).
# A 1-hour timeout is GitLab's own default for a job that sets none.
DEFAULT = {"interruptible": True, "timeout": "1 hour", "artifacts": {"when": "always", "expire_in": "24 hours"}}
