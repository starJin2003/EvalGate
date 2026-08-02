#!/usr/bin/env bash
#
# retry-apply.sh - win the Ampere A1 capacity lottery.
#
# Loops `terraform apply`, cycling through every availability domain the region
# reports, and backs off far enough between attempts to stay clear of the OCI
# LaunchInstance rate limit. Exits 0 the moment an instance launches.
#
# Every attempt is logged with a UTC timestamp to logs/retry-<date>.log and to
# stdout. Run it under nohup, tmux, or screen; it is designed to be left alone
# for hours or days.
#
#   ./retry-apply.sh                       # run until it succeeds
#   ./retry-apply.sh --max-attempts 50     # give up after 50 attempts
#   ./retry-apply.sh --dry-run             # exercise the loop with no OCI calls
#
# Downsizing to 2 OCPU is a BUILD_PLAN.md P2 contingency and a decision for a
# human, not something this loop does on its own after a few failures. The
# guard below refuses to run below 4 OCPU without --allow-downsize.

set -uo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOG_DIR="${SCRIPT_DIR}/logs"

# --- Tunables. Environment overrides all of them. ----------------------------

# Seconds between attempts on consecutive ADs inside one cycle.
ATTEMPT_DELAY="${ATTEMPT_DELAY:-180}"

# Extra seconds after a full pass over every AD, on top of ATTEMPT_DELAY.
CYCLE_DELAY="${CYCLE_DELAY:-900}"

# First backoff after OCI reports throttling. Doubles per consecutive throttle.
THROTTLE_DELAY_BASE="${THROTTLE_DELAY_BASE:-1800}"

# Ceiling on any single sleep.
MAX_DELAY="${MAX_DELAY:-7200}"

# 0 means retry forever.
MAX_ATTEMPTS="${MAX_ATTEMPTS:-0}"

DRY_RUN=0
ALLOW_DOWNSIZE=0

# --- Argument parsing --------------------------------------------------------

usage() {
  sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,2\} \{0,1\}//'
  cat <<'EOF'

Options:
  --max-attempts N     Stop after N attempts. 0 = unlimited (default).
  --attempt-delay S    Seconds between ADs within a cycle. Default 180.
  --cycle-delay S      Extra seconds after a full AD pass. Default 900.
  --allow-downsize     Permit ocpus < 4. Requires a DECISIONS.md entry.
  --dry-run            Print what would run without calling terraform or OCI.
  -h, --help           This text.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-attempts)  MAX_ATTEMPTS="$2"; shift 2 ;;
    --attempt-delay) ATTEMPT_DELAY="$2"; shift 2 ;;
    --cycle-delay)   CYCLE_DELAY="$2"; shift 2 ;;
    --allow-downsize) ALLOW_DOWNSIZE=1; shift ;;
    --dry-run)       DRY_RUN=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

# --- Logging -----------------------------------------------------------------

mkdir -p "${LOG_DIR}"
readonly LOG_FILE="${LOG_DIR}/retry-$(date -u +%Y%m%d).log"

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

log() {
  printf '%s  %s\n' "$(now_utc)" "$*" | tee -a "${LOG_FILE}"
}

# BSD date on macOS and GNU date on Linux disagree on relative-time flags, and
# this script has to read correctly in both places.
wake_time() {
  local secs="$1"
  date -u -v "+${secs}S" +%H:%M:%SZ 2>/dev/null \
    || date -u -d "+${secs} seconds" +%H:%M:%SZ 2>/dev/null \
    || echo "unknown"
}

human() {
  local s="$1"
  if (( s >= 3600 )); then printf '%dh%02dm' $(( s / 3600 )) $(( (s % 3600) / 60 ))
  elif (( s >= 60 )); then printf '%dm%02ds' $(( s / 60 )) $(( s % 60 ))
  else printf '%ds' "$s"; fi
}

START_EPOCH="$(date -u +%s)"
ATTEMPT=0

on_exit() {
  local code=$?
  local elapsed=$(( $(date -u +%s) - START_EPOCH ))
  if (( code != 0 )); then
    log "STOP  attempts=${ATTEMPT}  total_elapsed=$(human "${elapsed}")  exit=${code}"
  fi
}
trap on_exit EXIT

on_interrupt() {
  local elapsed=$(( $(date -u +%s) - START_EPOCH ))
  log "INTERRUPTED by signal  attempts=${ATTEMPT}  total_elapsed=$(human "${elapsed}")"
  log "Resume with the same command; terraform state is unchanged by a failed launch."
  exit 130
}
trap on_interrupt INT TERM

# --- Preflight ---------------------------------------------------------------

cd "${SCRIPT_DIR}" || exit 1

preflight_fail() { log "PREFLIGHT FAILED  ${1}"; exit 1; }

if (( DRY_RUN == 0 )); then
  [[ -f terraform.tfvars ]] \
    || preflight_fail "terraform.tfvars missing. Copy terraform.tfvars.example and fill it in."

  # This runs before every other preflight check on purpose. It is a policy
  # guard, not an environment check, and it must give the same answer whether
  # or not the OCI CLI happens to be installed yet.
  #
  # The instruction is explicit: do not quietly shrink the shape to make the
  # capacity error go away. A smaller instance that succeeds looks like a win
  # in the log and is actually an unlogged change to the project's capacity
  # plan, which the P4 Kafka sizing decision depends on.
  configured_ocpus="$(
    grep -E '^[[:space:]]*ocpus[[:space:]]*=' terraform.tfvars 2>/dev/null \
      | tail -1 | tr -dc '0-9'
  )"
  configured_ocpus="${configured_ocpus:-4}"
  if (( configured_ocpus < 4 && ALLOW_DOWNSIZE == 0 )); then
    log "REFUSING TO RUN  ocpus=${configured_ocpus} is below the 4 OCPU target."
    log "Downsizing is a DECISION POINT, not a retry strategy. Confirm with a human,"
    log "log it in DECISIONS.md, then re-run with --allow-downsize."
    exit 3
  fi
  if (( configured_ocpus < 4 )); then
    log "WARN  running at ocpus=${configured_ocpus} via --allow-downsize. This belongs in DECISIONS.md."
  fi

  command -v terraform >/dev/null 2>&1 \
    || preflight_fail "terraform not on PATH. brew install terraform"

  [[ -f "${HOME}/.oci/config" ]] \
    || preflight_fail "~/.oci/config missing. Run: oci setup config"

  if [[ ! -d .terraform ]]; then
    log "terraform init"
    terraform init -input=false 2>&1 | tee -a "${LOG_FILE}" \
      || preflight_fail "terraform init failed"
  fi
fi

# Number of ADs to cycle. Discovered from OCI when possible so the loop is not
# hardcoded to the three that us-chicago-1 happens to have.
AD_COUNT=3
if (( DRY_RUN == 0 )) && command -v oci >/dev/null 2>&1; then
  discovered="$(oci iam availability-domain list --query 'length(data)' --raw-output 2>/dev/null || true)"
  if [[ "${discovered}" =~ ^[0-9]+$ ]] && (( discovered > 0 )); then
    AD_COUNT="${discovered}"
  else
    log "WARN  could not read AD count from the OCI CLI, assuming ${AD_COUNT}"
  fi
fi

log "════════════════════════════════════════════════════════════════"
log "START  ad_count=${AD_COUNT}  attempt_delay=$(human "${ATTEMPT_DELAY}")  cycle_delay=$(human "${CYCLE_DELAY}")  max_attempts=${MAX_ATTEMPTS:-unlimited}"
log "       log=${LOG_FILE}"
(( DRY_RUN == 1 )) && log "       DRY RUN, no OCI calls will be made"
log "════════════════════════════════════════════════════════════════"

# --- Error classification ----------------------------------------------------
# Returns one of: capacity | throttle | fatal

classify() {
  local out="$1"

  if grep -qiE 'out of (host )?capacity|outofcapacity|outofhostcapacity' <<<"${out}"; then
    echo capacity; return
  fi
  if grep -qiE 'toomanyrequests|429|rate limit|too many requests' <<<"${out}"; then
    echo throttle; return
  fi
  # A service limit is not a capacity shortage. Retrying it forever is pointless
  # and burns the rate limit budget that the capacity retries need.
  if grep -qiE 'limitexceeded|service limit|quota' <<<"${out}"; then
    echo fatal; return
  fi
  if grep -qiE 'notauthenticated|notauthorized|authorization failed|invalid.*private key' <<<"${out}"; then
    echo fatal; return
  fi
  echo fatal
}

# --- The loop ----------------------------------------------------------------

ad_index=0
consecutive_throttles=0

while :; do
  ATTEMPT=$(( ATTEMPT + 1 ))

  if (( MAX_ATTEMPTS > 0 && ATTEMPT > MAX_ATTEMPTS )); then
    log "GIVING UP  reached max_attempts=${MAX_ATTEMPTS} without a successful launch"
    exit 1
  fi

  attempt_start="$(date -u +%s)"
  log "ATTEMPT ${ATTEMPT}  ad_index=${ad_index}  applying..."

  if (( DRY_RUN == 1 )); then
    output="Error: 500-InternalError, Out of host capacity."
    rc=1
  else
    output="$(
      terraform apply \
        -auto-approve \
        -input=false \
        -lock-timeout=120s \
        -var "availability_domain_index=${ad_index}" 2>&1
    )"
    rc=$?
  fi

  attempt_secs=$(( $(date -u +%s) - attempt_start ))
  printf '%s\n' "${output}" >> "${LOG_FILE}"

  if (( rc == 0 )); then
    total=$(( $(date -u +%s) - START_EPOCH ))
    ad_name="$(terraform output -raw availability_domain 2>/dev/null || echo "ad_index=${ad_index}")"
    public_ip="$(terraform output -raw public_ip 2>/dev/null || echo unknown)"
    log "────────────────────────────────────────────────────────────────"
    log "SUCCESS  attempt=${ATTEMPT}  ad=${ad_name}  took=$(human "${attempt_secs}")"
    log "         total_elapsed=$(human "${total}")  public_ip=${public_ip}"
    log "         cloud-init still has a few minutes of work. Check with:"
    log "         terraform output -raw bootstrap_status_command"
    log "────────────────────────────────────────────────────────────────"
    trap - EXIT
    exit 0
  fi

  reason="$(classify "${output}")"

  case "${reason}" in
    capacity)
      consecutive_throttles=0
      ad_index=$(( (ad_index + 1) % AD_COUNT ))
      if (( ad_index == 0 )); then
        delay=$(( ATTEMPT_DELAY + CYCLE_DELAY ))
        note="full AD cycle done, long pause"
      else
        delay="${ATTEMPT_DELAY}"
        note="next AD"
      fi
      ;;
    throttle)
      # Do not advance the AD. Being throttled says nothing about capacity in
      # the AD just tried, so moving on would skip it for no reason.
      consecutive_throttles=$(( consecutive_throttles + 1 ))
      delay=$(( THROTTLE_DELAY_BASE * (1 << (consecutive_throttles - 1)) ))
      note="rate limited x${consecutive_throttles}, exponential backoff, holding ad_index"
      ;;
    fatal)
      log "RESULT  attempt=${ATTEMPT}  reason=fatal  took=$(human "${attempt_secs}")"
      log "FATAL  this is not a capacity error and retrying will not fix it."
      log "       Last 20 lines of terraform output:"
      printf '%s\n' "${output}" | tail -20 | sed 's/^/       | /' | tee -a "${LOG_FILE}"
      exit 1
      ;;
  esac

  (( delay > MAX_DELAY )) && delay="${MAX_DELAY}"

  log "RESULT  attempt=${ATTEMPT}  reason=${reason}  took=$(human "${attempt_secs}")  ${note}"
  log "SLEEP   $(human "${delay}")  resuming at $(wake_time "${delay}")  next_ad_index=${ad_index}"

  if (( DRY_RUN == 1 )); then
    log "DRY RUN  skipping the sleep"
    (( ATTEMPT >= 5 )) && { log "DRY RUN  loop exercised over 5 attempts, stopping"; trap - EXIT; exit 0; }
  else
    sleep "${delay}"
  fi
done
