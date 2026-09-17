#!/bin/bash
# Move a standalone stack started before #53 onto the pinned compose project, keeping
# HAF's data.
#
# Before #53 docker/docker-compose.yml pinned no project name, so a stack ran under the
# compose file's directory, `docker`, unless it was started with -p or
# COMPOSE_PROJECT_NAME. The file now pins `haf_fyp`, so the plain quick-start commands no
# longer reach the old containers and would create a second stack on a new, empty HAF
# volume. This script finds the old stack from THIS checkout's HAF container, stops it
# with HAF's two minutes, and starts it again from the new file on the same data.
#
# Without --apply it changes nothing: it prints what it found and what it would do, and
# refuses with a reason whenever it cannot tell where HAF's data is.
#
# OPTIONS:                                                      # --- help-start ---
#   --apply            Do it. Without this flag the script only inspects and reports.
#   --old=PROJECT      The project the old stack ran under, when the script cannot tell:
#                      no HAF container from before #53 that has run is left in this
#                      checkout (for example after a `down`). PROJECT_haf_datadir must
#                      hold the data.
#   --env-file=PATH    The env file the stack runs with (default: docker/.env.local)
#   --accept-killed-haf
#                      Go on although the old stack's HAF exited 137, killed rather than
#                      shut down. Read its log first: its next start runs crash recovery.
#   --help,-h,-?       Show this and exit                       # --- help-end ---
#
# It handles a stack run from docker/docker-compose.yml alone, with HAF's data in a named
# volume, on Compose v2.18.1 or newer (older releases cannot parse the volume names the
# #53 file uses). It never removes a volume, never runs `down -v` or --remove-orphans, and
# stops only containers labelled with this checkout's docker/ directory. It exits 1 when
# it refuses, having changed nothing, and 3 when anything fails after it changed the host.

set -Eeuo pipefail
SCRIPT="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname "${SCRIPT}")" && pwd)"
ROOT="$(CDPATH='' cd -- "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${ROOT}/docker/docker-compose.yml"
# Compose labels each container with the absolute directory of its first -f file, as the
# path it was started through, so `cd` here must not resolve symlinks either (no -P).
WORKDIR="${ROOT}/docker"
DATADIR=/home/hived/datadir
NEW_PROJECT=haf_fyp
SERVICES="haf-fyp-sync haf-fyp-ranker haf-fyp-api haf-fyp-postgrest haf-fyp-postgrest-rewriter haf-fyp-redis"
# Seconds to let the new stack settle before the last check, so a service that exits a
# moment after `up` is not reported as running. The tests set it to 0.
SETTLE=${HAF_FYP_UPGRADE_SETTLE_SECONDS:-5}

ENV_FILE="${ROOT}/docker/.env.local"
OLD=""
APPLY=0
ACCEPT_KILLED=0
CHANGED=0          # 1 once the host has been changed: every later failure exits 3
KILLED=0           # 1 when the HAF that was moved had been killed, not shut down

print_help() {
    # See scripts/install_app.sh for why the markers carry a character class.
    sed -n '/--- help-sta[r]t ---/,/--- help-en[d] ---/p' "${SCRIPT}" | sed 's/#\( \|$\)//; s/ *# --- help-[a-z]* ---//'
}

for arg in "$@"; do
    case "${arg}" in
        --apply) APPLY=1 ;;
        --old=*) OLD="${arg#--old=}" ;;
        --env-file=*) ENV_FILE="${arg#--env-file=}" ;;
        --accept-killed-haf) ACCEPT_KILLED=1 ;;
        --help|-h|-\?) print_help; exit 0 ;;
        *) echo "upgrade: unknown option: ${arg}" >&2; print_help >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
refuse() { printf 'upgrade: REFUSED: %s\n' "$*" >&2; exit 1; }            # nothing changed yet
fail() { printf 'upgrade: FAILED after changing the host: %s\n' "$*" >&2; exit 3; }
# Any command that fails once the host has been changed -- a `docker stop` of a container
# someone else removed, a `docker rm` refused by the daemon -- must report where that left
# the host, not exit with the code that means "refused, nothing changed".
on_error() {
    local code=$?
    [[ ${CHANGED} -eq 1 ]] && fail "a command exited ${code} (its output is above). ${AFTER_FAILURE}"
    exit "${code}"
}
on_signal() {
    if [[ ${CHANGED} -eq 1 ]]; then
        fail "interrupted. A \`docker stop\` already handed to Docker runs on to its two-minute limit whatever happens to this script. ${AFTER_FAILURE}"
    fi
    refuse "interrupted before anything was changed."
}
AFTER_FAILURE="Run the script again: it reports what is left and what to do."
trap on_error ERR
trap on_signal INT TERM HUP

act() {  # act DESCRIPTION -- COMMAND...
    local description="$1"; shift 2
    if [[ ${APPLY} -eq 1 ]]; then
        say "  - ${description}"
        CHANGED=1
        "$@"
    else
        say "  - would ${description}"
    fi
}
compose() { docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ${PROJECT_ARGS[@]+"${PROJECT_ARGS[@]}"} "$@"; }
clear_project() { compose down || fail "docker compose down failed. ${AFTER_FAILURE}"; }
# shellcheck disable=SC2086
start_stack() { compose up -d --build ${SERVICES} || fail "docker compose up failed. ${AFTER_FAILURE}"; }
set_project() {  # the project the stack will run under, and how every command names it
    PROJECT=$1
    PROJECT_ARGS=()
    PROJECT_FLAG=""
    if [[ ${PROJECT} != "${NEW_PROJECT}" ]]; then
        PROJECT_ARGS=(-p "${PROJECT}")
        PROJECT_FLAG=" -p ${PROJECT}"
    fi
}
check_description() { printf 'check that the new HAF mounts %s and that haf and every service run' "${VOLUME}"; }
# The stack is only up if it is still up a moment later: `up -d` returns before a service
# that exits on a bad configuration has done so.
check_stack() {
    local haf_id mounted running service missing pass
    say "  - $(check_description)"
    for pass in first second; do
        [[ ${pass} == second ]] && sleep "${SETTLE}"
        haf_id=$(compose ps -q haf)      # running containers only
        [[ -n ${haf_id} ]] || fail "HAF is not running under project ${PROJECT}. ${AFTER_FAILURE}"
        mounted=$(docker inspect -f '{{range .Mounts}}{{.Destination}}={{.Type}}:{{.Name}}|{{end}}' "${haf_id}")
        grep -qxF -- "${DATADIR}=volume:${VOLUME}" < <(tr '|' '\n' <<<"${mounted}") \
            || fail "the new HAF does not mount volume ${VOLUME} at ${DATADIR}: ${mounted}. Stop that stack before anything writes to the wrong volume (\`docker compose --env-file '${ENV_FILE}' -f '${COMPOSE_FILE}'${PROJECT_FLAG} down\`); ${VOLUME} still holds the data."
        running=$(compose ps --status running --services)
        missing=""
        for service in haf ${SERVICES}; do
            grep -qxF -- "${service}" <<<"${running}" || missing+=" ${service}"
        done
        [[ -n ${missing} ]] && fail "these services are not running under project ${PROJECT}:${missing}. HAF is running on ${VOLUME}. ${AFTER_FAILURE}"
    done
    say "Done: project ${PROJECT} runs HAF on ${VOLUME}."
    say "From now on pass --env-file '${ENV_FILE}'${PROJECT_FLAG} to every compose command for this stack, with no COMPOSE_PROJECT_NAME in the shell: before Compose v2.27.1 one there renames this stack's volumes."
    if [[ ${KILLED} -eq 1 ]]; then
        say "HAF had been killed (exit 137), so this start runs crash recovery: watch its log before relying on the stack."
    fi
    say "Check /health on the API port: its last_processed_block should continue from where the old stack was."
}

# The script names the project on every compose call; a value left in the shell would
# otherwise outrank the file's `name:`.
unset COMPOSE_PROJECT_NAME
# A volume name set in the shell outranks every env file, so the stack would move again
# the first time a later compose command ran without it.
for var in HAF_FYP_HAF_DATADIR_VOLUME HAF_FYP_REDIS_DATA_VOLUME; do
    if [[ -n ${!var+set} ]]; then
        refuse "${var} is set in this shell. The stack's volume names belong in the env file, which every later compose command reads: unset it and run again."
    fi
done
docker version >/dev/null 2>&1 \
    || refuse "\`docker version\` failed, so nothing here can be read: run this on the host that runs the stack, with a Docker daemon it can reach."

# --- what is there ------------------------------------------------------------------------

# One line per HAF container of this checkout, in any project (not `compose run` ones):
#   ID|PROJECT|STATUS|STOP_TIMEOUT|DATADIR_MOUNT
# STOP_TIMEOUT is 120 for a container created from the #53 file (stop_grace_period: 2m)
# and <nil> for one created before it. DATADIR_MOUNT is volume:NAME, bind: for a bind
# mount, or - when nothing is mounted at the data directory. The fields are separated by a
# character no value can hold: an empty one must not shift the others.
haf_ids() {  # [PROJECT]
    docker ps -a --no-trunc --filter label=com.docker.compose.service=haf \
        --filter label=com.docker.compose.oneoff=False \
        --filter "label=com.docker.compose.project.working_dir=${WORKDIR}" \
        ${1:+--filter "label=com.docker.compose.project=$1"} --format '{{.ID}}'
}
haf_rows() {
    local ids id mounts mount m fields
    ids=$(haf_ids) || return 1
    for id in ${ids}; do
        mounts=$(docker inspect -f '{{range .Mounts}}{{.Destination}}={{.Type}}:{{.Name}}|{{end}}' "${id}") || return 1
        mount=-
        while IFS= read -r -d '|' m; do
            [[ ${m%%=*} == "${DATADIR}" ]] && mount=${m#*=}
        done <<<"${mounts}"
        fields=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}|{{.State.Status}}|{{.Config.StopTimeout}}' "${id}") || return 1
        printf '%s|%s|%s\n' "${id}" "${fields}" "${mount}"
    done
}
# FIELD of the rows given on stdin, one per line ($1 is the 1-based field number).
field() { awk -F'|' -v n="$1" 'NF {print $n}'; }

# ID|PROJECT|NAME|WORKING_DIR for every container, in any state, using a volume. The labels
# are empty for a container compose did not create, hence a separator that is not blank.
volume_users() {
    docker ps -a --no-trunc --filter "volume=$1" \
        --format '{{.ID}}|{{.Label "com.docker.compose.project"}}|{{.Names}}|{{.Label "com.docker.compose.project.working_dir"}}'
}

# Containers of PROJECT started from this checkout.
mine() {
    docker ps -a --no-trunc --filter "label=com.docker.compose.project=$1" \
        --filter "label=com.docker.compose.project.working_dir=${WORKDIR}" "${@:2}"
}

# HAF data volumes on this host, by name or by compose key, labelled or not, that nothing
# uses; except the names given. A volume counts as used when a container is running on it,
# or when a HAF container that has run mounts it. One of the rest may hold a stack's data
# after its containers were removed; others may belong to other checkouts or other apps.
unclaimed_volumes() {
    local by_name by_key volume skip users
    by_name=$(docker volume ls --format '{{.Name}}') || return 1
    by_key=$(docker volume ls --filter label=com.docker.compose.volume=haf_datadir --format '{{.Name}}') || return 1
    while IFS= read -r volume; do
        [[ -z ${volume} ]] && continue
        for skip in "$@"; do [[ ${volume} == "${skip}" ]] && continue 2; done
        users=$(docker ps --no-trunc --filter "volume=${volume}" --format '{{.Names}}') || return 1
        [[ -n ${users} ]] && continue                        # something runs on it now
        users=$(docker ps -a --no-trunc --filter "volume=${volume}" \
                    --filter label=com.docker.compose.service=haf --format '{{.State}}') || return 1
        # A HAF that has run on it claims it; a `created` one never started.
        [[ -n ${users} ]] && grep -qvx created <<<"${users}" && continue
        printf '%s (%s)\n' "${volume}" \
            "$(docker volume inspect -f 'project {{index .Labels "com.docker.compose.project"}}, created {{.CreatedAt}}' "${volume}")"
    done < <({ grep '_haf_datadir$' <<<"${by_name}" || true; printf '%s\n' "${by_key}"; } | sort -u)
}

# The last value KEY is set to in the env file, as compose reads KEY=v and KEY: v, with
# `export` and quotes. Values with a `#` or a space are cut short: none of the four keys
# read here (a project name, two volume names, a host name) can hold either.
env_value() {
    sed -nE "s/^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*[=:][[:space:]]*[\"']?([^\"'#[:space:]]*).*/\2/p" \
        "${ENV_FILE}" | tail -n 1 | tr -d '\r'
}

# PROJECT [VOLUME_OVERRIDE]: prints "NAME HAF_VOLUME" as compose resolves them. It returns
# non-zero rather than refusing, because its callers read it through a command substitution,
# where an exit would end only the subshell and the script would carry on with empty values.
compose_config() {
    local config args=()
    [[ $1 != "${NEW_PROJECT}" ]] && args=(-p "$1")
    config=$(if [[ -n ${2:-} ]]; then
                 HAF_FYP_HAF_DATADIR_VOLUME=$2 docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ${args[@]+"${args[@]}"} config
             else
                 docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ${args[@]+"${args[@]}"} config
             fi) || return 1
    printf '%s %s\n' "$(sed -n 's/^name: *//p' <<<"${config}" | head -n 1)" \
        "$(awk '/^volumes:/ {v = 1; next} v && /^  haf_datadir:/ {h = 1; next} h && /^    name:/ {print $2; exit}' <<<"${config}")"
}
config_failed() {  # $1 = project
    refuse "docker compose config failed for ${ENV_FILE} under project $1 (its output is above). The #53 compose file needs Compose v2.18.1 or newer for its volume names; this is $(docker compose version --short 2>/dev/null || echo 'unknown'). Fix what it reports, or upgrade Compose, and run again."
}

say "Checkout: ${ROOT}"
[[ -f ${ENV_FILE} ]] || refuse "${ENV_FILE} does not exist: the stack's settings (HAF_VERSION, keys) live there. Create it first."
ENV_FILE="$(CDPATH='' cd -- "$(dirname "${ENV_FILE}")" && pwd)/$(basename "${ENV_FILE}")"
[[ -n $(env_value HAF_VERSION) ]] || refuse "${ENV_FILE} does not set HAF_VERSION."
if [[ ${ENV_FILE} == "${ROOT}/docker/.env" ]] \
        || git -C "${ROOT}" ls-files --error-unmatch -- "${ENV_FILE}" >/dev/null 2>&1; then
    refuse "${ENV_FILE} is tracked by git. The stack's volume setting must live in an untracked file such as docker/.env.local, where a checkout or a pull cannot drop it."
fi
# One run at a time: two would stop the same stack and fight over the env file.
if command -v flock >/dev/null 2>&1; then
    exec 9<"${ENV_FILE}"
    flock -n 9 || refuse "another run of this script holds ${ENV_FILE}: wait for it to finish."
fi

rows=$(haf_rows) || refuse "reading this checkout's HAF containers failed (\`docker ps\`/\`docker inspect\`; their output is above)."
say "HAF containers from this checkout (id project status stop-timeout datadir):"
say "${rows:-  (none)}"
created=$(awk -F'|' '$3 == "created" {print $1}' <<<"${rows}")
# From the #53 file: StopTimeout 120, and not under `docker`, which that file pins away
# from (a grace period can also come from an extra -f file).
old_rows=$(awk -F'|' 'NF > 1 && $3 != "created" && ($4 != "120" || $2 == "docker")' <<<"${rows}")
new_rows=$(awk -F'|' 'NF > 1 && $3 != "created" && $4 == "120" && $2 != "docker"' <<<"${rows}")
old_count=$(grep -c . <<<"${old_rows}" || true)

# --- which project is `old` -------------------------------------------------------------------

if [[ ${old_count} -gt 1 ]]; then
    refuse "this checkout has ${old_count} HAF containers from before #53 that have run (projects: $(field 2 <<<"${old_rows}" | paste -sd ' ' -)), and the script moves one stack. Decide which holds this stack's data, remove the other's containers (\`docker stop -t 120 <ids>\`, then \`docker rm <ids>\`; its volumes stay), and run again."
fi
row_project=$(field 2 <<<"${old_rows}")
if [[ -n ${OLD} && -n ${row_project} && ${OLD} != "${row_project}" ]]; then
    refuse "--old=${OLD}, but this checkout's HAF from before #53 runs under ${row_project}."
fi
OLD=${OLD:-${row_project}}
hint=""
override=$(env_value HAF_FYP_HAF_DATADIR_VOLUME)
if [[ -z ${OLD} && -z ${new_rows} && ${override} == *_haf_datadir ]]; then
    # An earlier run wrote that line for this stack, so it names the data even though no
    # container is left -- after an --apply that failed before the `up`, for instance.
    OLD=${override%_haf_datadir}
    say "No HAF container of this checkout is left, but ${ENV_FILE} sets HAF_FYP_HAF_DATADIR_VOLUME=${override}, so the old project is ${OLD}."
fi

new_projects=$(field 2 <<<"${new_rows}" | sort -u | paste -sd ' ' -)

# The stack is already on the #53 file when compose would keep each HAF where it is and
# every service runs. With --apply, services that are not running are started.
report_moved() {  # [PROJECT]
    local projects=${1:-${new_projects}} project mount resolved config_project config_volume
    local running missing service
    for project in ${projects}; do
        mount=$(awk -F'|' -v p="${project}" '$2 == p {print $5; exit}' <<<"${new_rows}")
        resolved=$(compose_config "${project}") || config_failed "${project}"
        read -r config_project config_volume <<<"${resolved}"
        if [[ ${mount} != "volume:${config_volume}" || ${config_project} != "${project}" ]]; then
            refuse "project ${project}'s HAF mounts ${mount}, but with ${ENV_FILE} compose resolves project '${config_project}' and volume '${config_volume}': its next compose command would move it. Set HAF_FYP_HAF_DATADIR_VOLUME in that file to the volume that holds the data, and run again."
        fi
        running=$(mine "${project}" --filter status=running --format '{{.Label "com.docker.compose.service"}}')
        missing=""
        for service in haf ${SERVICES}; do
            grep -qxF -- "${service}" <<<"${running}" || missing+=" ${service}"
        done
        [[ -z ${missing} ]] && continue
        set_project "${project}"
        VOLUME=${mount#volume:}
        if [[ ${APPLY} -eq 0 ]]; then
            say "Plan (run again with --apply to do it):"
            say "  - would start project ${project}'s services that are not running:${missing}"
            say "  - would $(check_description)"
            exit 0
        fi
        say "Applying:"
        AFTER_FAILURE="HAF's data is in ${VOLUME}. Fix the cause shown above (a port, a network or a volume name already taken) and run the script again."
        act "start project ${project}'s services that are not running:${missing}" -- start_stack
        check_stack
        exit 0
    done
    say "Nothing to do: HAF and every service already run from the #53 compose file (project ${projects}), on the volume compose resolves."
    say "If a compose command from the new file recreated a HAF from before #53, compose gave it ten seconds to stop: look for crash recovery near the start of its log."
    exit 0
}

if [[ -z ${OLD} ]]; then
    if [[ -z ${new_rows} ]]; then
        # Name the volumes that could hold the data, and the --old each one asks for: this
        # is where an --apply that failed after removing the old containers lands.
        candidates=$(unclaimed_volumes) \
            || refuse "reading this host's volumes failed (\`docker volume ls\`; its output is above)."
        while IFS= read -r line; do
            [[ -z ${line} ]] && continue
            hint+=" ${line}, for --old=${line%%_haf_datadir *};"
        done <<<"${candidates}"
        refuse "this checkout (${WORKDIR}) has no HAF container that has run. If the stack was started through another path -- a symlink to this directory, or a symlink to the compose file -- run the script through that path; if its containers were removed, run again with --old=PROJECT for the PROJECT_haf_datadir that holds its data.${hint:+ HAF data volumes that nothing uses, and the option each one needs:${hint}}"
    fi
    # No HAF data volume may be left that could be this stack's instead.
    new_volumes=()
    while IFS= read -r line; do new_volumes+=("${line#volume:}"); done < <(field 5 <<<"${new_rows}")
    unclaimed=$(unclaimed_volumes ${new_volumes[@]+"${new_volumes[@]}"}) \
        || refuse "reading this host's volumes failed (\`docker volume ls\`; its output is above)."
    if [[ -n ${unclaimed} ]]; then
        refuse "this checkout's HAF already runs from the #53 compose file (project ${new_projects}), and HAF data volumes that nothing uses exist on this host: $(paste -sd ';' - <<<"${unclaimed}"). If one of them holds this stack's data (a quick start after the pull creates a new, empty volume), run again with --old=PROJECT for PROJECT_haf_datadir. If none does, this stack needs nothing. They may belong to other checkouts or other apps: remove one only if you know which stack created it and that nothing needs it."
    fi
    report_moved
fi
if [[ -z ${old_rows} ]] && awk -F'|' -v v="volume:${OLD}_haf_datadir" '$5 == v {found = 1} END {exit !found}' <<<"${new_rows}"; then
    report_moved "$(awk -F'|' -v v="volume:${OLD}_haf_datadir" '$5 == v {print $2}' <<<"${new_rows}" | sort -u | paste -sd ' ' -)"
fi

if [[ ${OLD} == docker ]]; then set_project "${NEW_PROJECT}"; else set_project "${OLD}"; fi

# --- where the data is, and whose ------------------------------------------------------------

old_id=$(field 1 <<<"${old_rows}")
if [[ -n ${old_id} ]]; then
    mount=$(field 5 <<<"${old_rows}")
    case "${mount}" in
        volume:?*) VOLUME=${mount#volume:} ;;
        bind:*) refuse "the old HAF's data directory is a bind mount (\`docker inspect ${old_id}\`). The script moves stacks whose data is in a named volume: move this one by hand, as the README describes." ;;
        *) refuse "the old HAF container mounts nothing at ${DATADIR}." ;;
    esac
    # docker-compose v1 records the -f argument as it was typed, so a relative spelling of
    # this checkout's own file must not read as another file.
    config_files=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "${old_id}")
    if [[ -n ${config_files} && ${config_files} != "${COMPOSE_FILE}" \
            && ${config_files} != */ && "${WORKDIR}/${config_files##*/}" != "${COMPOSE_FILE}" ]]; then
        refuse "the old stack was started from ${config_files}, not ${COMPOSE_FILE} alone. The script starts the new stack from that file only, so what the other files set would be lost: move this stack by hand, as the README describes."
    fi
    old_code=$(docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "${old_id}")
    if [[ ${old_code} != running* && ${old_code##* } == 137 && ${ACCEPT_KILLED} -eq 0 ]]; then
        refuse "the old HAF already exited 137 before this run: it was killed rather than shut down (by a compose stop's ten seconds, or the OOM killer). Read \`docker logs ${old_id}\`, since HAF's next start runs crash recovery, then run again with --accept-killed-haf."
    fi
else
    VOLUME="${OLD}_haf_datadir"
    docker volume inspect "${VOLUME}" >/dev/null 2>&1 \
        || refuse "volume ${VOLUME} does not exist."
    volume_project=$(docker volume inspect -f '{{index .Labels "com.docker.compose.project"}}' "${VOLUME}")
    if [[ -n ${volume_project} && ${volume_project} != "${OLD}" ]]; then
        refuse "volume ${VOLUME} was created for project '${volume_project}', not ${OLD}."
    fi
fi
say "Old project: ${OLD}; HAF data volume: ${VOLUME}; new project: ${PROJECT}"

old_ids=$(mine "${OLD}" -q)
while IFS='|' read -r id project name workdir; do
    [[ -z ${id} ]] && continue
    grep -qxF -- "${id}" <<<"${created}" && continue     # removed below; it never ran
    if [[ ${workdir} != "${WORKDIR}" ]]; then
        refuse "volume ${VOLUME} is also used by container ${name} (project '${project}', started from '${workdir}'), which is not this checkout's. Stop and remove it where it runs, or pick the checkout that should keep the data, and run again."
    fi
    if [[ ${project} != "${OLD}" ]]; then
        refuse "volume ${VOLUME} is also used by ${name} of project ${project} from this checkout, so two HAF containers share its data: remove whichever is stale and run again."
    fi
done <<<"$(volume_users "${VOLUME}")"
if [[ -z ${old_ids} ]]; then
    say "Note: this checkout has no container of project ${OLD}, so ${VOLUME} was created by whatever started that project. Check the plan below names the data you mean."
fi

# `down` and `up` act on every container of the project, whichever checkout started it.
while IFS='|' read -r name workdir; do
    [[ -z ${name} || ${workdir} == "${WORKDIR}" ]] && continue
    refuse "${name} of project ${PROJECT} was started from '${workdir}': another checkout's stack, which the compose commands below would take over. Remove its containers from that checkout (a HAF from before #53 with \`docker stop -t 120\` first), and run again."
done <<<"$(docker ps -a --no-trunc --filter "label=com.docker.compose.project=${PROJECT}" \
               --format '{{.Names}}|{{.Label "com.docker.compose.project.working_dir"}}')"

# Anything else running on the old project's network reaches this HAF as `haf` (the
# collision #53 fixes) and would lose it. Compose containers and hand-attached ones alike.
if [[ ${OLD} != "${PROJECT}" ]]; then
    while IFS='|' read -r name workdir; do
        [[ -z ${name} || ${workdir} == "${WORKDIR}" ]] && continue
        refuse "${name} runs on the network ${OLD}_default${workdir:+, started from '${workdir}'}, where it can reach HAF as \`haf\`: stopping this HAF would cut it off. Stop it first, or move it to its own network, and run again."
    done <<<"$(docker ps --no-trunc --filter "network=${OLD}_default" \
                   --format '{{.Names}}|{{.Label "com.docker.compose.project.working_dir"}}')"
fi

# This checkout's other stacks: one created from the #53 file on another volume (a quick
# start after the pull), or one whose HAF never ran (a start that failed on a port). They
# are stopped first, HAF given its two minutes, and removed; their volumes stay.
stale_projects=$(awk -F'|' -v old="${OLD}" 'NF > 1 && $2 != old && ($3 == "created" || ($4 == "120" && $2 != "docker")) {print $2}' <<<"${rows}" | sort -u)

# --- the env file ----------------------------------------------------------------------------

cpn=$(env_value COMPOSE_PROJECT_NAME)
if [[ -n ${cpn} && ( ${OLD} == docker || ${cpn} != "${OLD}" ) ]]; then
    refuse "${ENV_FILE} sets COMPOSE_PROJECT_NAME=${cpn}. Delete that line: this script, and the README's commands, name the project themselves."
fi
if [[ $(env_value POSTGREST_HOST) == postgrest ]]; then
    refuse "${ENV_FILE} sets POSTGREST_HOST=postgrest, a service renamed in #48. Delete that line."
fi
add_override=0
if [[ ${VOLUME} != "${PROJECT}_haf_datadir" && -z ${override} ]]; then
    add_override=1
    [[ -w ${ENV_FILE} ]] || refuse "${ENV_FILE} is not writable, and the script must add HAF_FYP_HAF_DATADIR_VOLUME=${VOLUME} to it."
elif [[ -n ${override} && ${override} != "${VOLUME}" ]]; then
    refuse "${ENV_FILE} sets HAF_FYP_HAF_DATADIR_VOLUME=${override}, but the old HAF's data is in ${VOLUME}."
fi

resolved=$(compose_config "${PROJECT}" "$([[ ${add_override} -eq 1 ]] && printf '%s' "${VOLUME}")") \
    || config_failed "${PROJECT}"
read -r config_project config_volume <<<"${resolved}"
if [[ ${config_project} != "${PROJECT}" || ${config_volume} != "${VOLUME}" ]]; then
    refuse "compose resolves project '${config_project}' and HAF volume '${config_volume}', expected ${PROJECT} and ${VOLUME}."
fi

# --- the plan ----------------------------------------------------------------------------------

if [[ ${APPLY} -eq 1 ]]; then say "Applying:"; else say "Plan (run again with --apply to do it):"; fi
unclaimed=$(unclaimed_volumes "${VOLUME}") \
    || refuse "reading this host's volumes failed (\`docker volume ls\`; its output is above)."
if [[ -n ${unclaimed} ]]; then
    say "  Left in place, as every volume is: HAF data volumes that nothing uses:"
    while IFS= read -r line; do say "    ${line}"; done <<<"${unclaimed}"
    say "  If one of them, rather than ${VOLUME}, holds this stack's data, do not apply: run again with --old=PROJECT."
fi

stop_project() {  # PROJECT old|stale: stop and remove PROJECT's containers from this checkout
    local project=$1 kind=$2 ids haf apps id code
    ids=$(mine "${project}" -q)
    if [[ -n ${ids} ]]; then
        haf=$(haf_ids "${project}")
        # The applications first, as compose does, so none is cut off by its database.
        apps=$(grep -vxF -f <(printf '%s\n' "${haf}") <<<"${ids}" || true)
        # shellcheck disable=SC2086
        [[ -n ${apps} ]] && docker stop -t 120 ${apps} >/dev/null
        for id in ${haf}; do
            docker stop -t 120 "${id}" >/dev/null
            code=$(docker inspect -f '{{.State.ExitCode}}' "${id}")
            if [[ ${code} == 137 && ${kind} == stale ]]; then
                say "    HAF of project ${project} exited 137, killed rather than shut down. Its data is not the volume this script moves, and its volume is left as it is."
            elif [[ ${code} == 137 && ${ACCEPT_KILLED} -eq 0 ]]; then
                fail "HAF of project ${project} exited 137: it was killed rather than shut down, at this stop's two-minute limit or by the OOM killer. The old stack is stopped, and its containers are kept: read \`docker logs ${id}\`, since HAF's next start runs crash recovery, then run again with --accept-killed-haf."
            elif [[ ${code} == 137 ]]; then
                KILLED=1
            fi
        done
        # shellcheck disable=SC2086
        docker rm ${ids} >/dev/null
    fi
    # Also on a later run, when an earlier one was interrupted between the two.
    if [[ -z $(docker ps -aq --filter "network=${project}_default") ]]; then
        docker network rm "${project}_default" >/dev/null 2>&1 || true
    fi
}

for stale in ${stale_projects}; do
    act "stop this checkout's containers of project ${stale} (a quick start after the pull, or a start whose HAF never ran) the same way as the old stack's below, then remove them and their network if nothing else is attached" -- \
        stop_project "${stale}" stale
done
if [[ -n ${old_ids} ]]; then
    act "stop the old stack's containers (project ${OLD}, this checkout): the applications, then HAF, each with two minutes; stop here if HAF exited 137; otherwise remove them, and the project network if nothing else is attached" -- \
        stop_project "${OLD}" old
fi
if [[ -n ${old_code:-} && ${old_code##* } == 137 ]]; then
    KILLED=1
fi
add_override_line() {
    printf '\n# The HAF data of the stack from before #53 (scripts/upgrade_standalone_stack.sh).\nHAF_FYP_HAF_DATADIR_VOLUME=%s\n' \
        "${VOLUME}" >> "${ENV_FILE}"
}
if [[ ${add_override} -eq 1 ]]; then
    act "add HAF_FYP_HAF_DATADIR_VOLUME=${VOLUME} to ${ENV_FILE}" -- add_override_line
fi
AFTER_FAILURE="HAF's data is still in ${VOLUME}. Fix the cause shown above (a port, a network or a volume name already taken) and run the script again, with --old=${OLD} if it asks for it."
act "clear anything a failed attempt left under project ${PROJECT}: docker compose ... down (volumes kept)" -- \
    clear_project
if [[ -z ${GIT_HASH:-} ]]; then
    GIT_HASH=$(git -C "${ROOT}" rev-parse --short=8 HEAD 2>/dev/null || true)
fi
export GIT_HASH
act "build and start the stack under project ${PROJECT}: docker compose ... up -d --build ${SERVICES}" -- \
    start_stack

if [[ ${APPLY} -eq 0 ]]; then
    say "  - would $(check_description)"
    exit 0
fi
check_stack
