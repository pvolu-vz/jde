#!/usr/bin/env bash
# install_jde.sh — One-command installer for JD Edwards EnterpriseOne → Veza OAA integration
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/VEZA/jde-veza"
REPO_URL="${REPO_URL:-https://github.com/pvolu-vz/jde.git}"
BRANCH="${BRANCH:-main}"
NON_INTERACTIVE=false
OVERWRITE_ENV=false
INTEGRATION_SUBDIR="integrations/jde"

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --non-interactive) NON_INTERACTIVE=true ;;
        --overwrite-env)   OVERWRITE_ENV=true ;;
        --install-dir)     INSTALL_DIR="$2"; shift ;;
        --repo-url)        REPO_URL="$2";    shift ;;
        --branch)          BRANCH="$2";      shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

SCRIPTS_DIR="${INSTALL_DIR}/scripts"
LOGS_DIR="${INSTALL_DIR}/logs"
VENV_DIR="${SCRIPTS_DIR}/venv"

# ── Helper functions ──────────────────────────────────────────────────────────
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
warn() { echo "[$(date '+%H:%M:%S')] WARNING: $*" >&2; }
die()  { echo "[$(date '+%H:%M:%S')] ERROR: $*" >&2; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || die "This installer must be run as root (use sudo)"
}

detect_os() {
    if   command -v dnf    &>/dev/null; then PKG_MGR="dnf"
    elif command -v yum    &>/dev/null; then PKG_MGR="yum"
    elif command -v apt-get &>/dev/null; then PKG_MGR="apt-get"
    else die "Unsupported Linux distribution — install git, python3, python3-pip manually"
    fi
    OS_ID="unknown"
    [[ -f /etc/os-release ]] && OS_ID="$(grep '^ID=' /etc/os-release | cut -d= -f2 | tr -d '"')"
    log "Package manager: ${PKG_MGR}  OS: ${OS_ID}"
}

_install_pkg() {
    local pkg="$1"
    case "${PKG_MGR}" in
        dnf|yum) "${PKG_MGR}" install -y "${pkg}" >/dev/null ;;
        apt-get) apt-get install -y "${pkg}" >/dev/null ;;
    esac
}

install_system_deps() {
    log "Installing system dependencies …"

    [[ "${PKG_MGR}" == "apt-get" ]] && apt-get update -q

    # git — required for repo clone
    command -v git &>/dev/null || _install_pkg git

    # curl — skip on Amazon Linux to avoid curl-minimal conflict; wget is available there
    if ! command -v curl &>/dev/null; then
        if [[ "${OS_ID}" == "amzn" ]]; then
            warn "Skipping curl install on Amazon Linux (curl-minimal conflict); wget will be used instead"
        else
            _install_pkg curl
        fi
    fi

    # python3
    command -v python3 &>/dev/null || _install_pkg python3

    # pip
    python3 -m pip --version &>/dev/null || _install_pkg python3-pip

    # venv — built-in on Amazon Linux 2023 / RHEL 9+; fall back to python3-virtualenv
    if ! python3 -m venv --help &>/dev/null; then
        warn "python3 venv not built-in; installing venv package …"
        case "${PKG_MGR}" in
            dnf|yum) _install_pkg python3-virtualenv ;;
            apt-get) _install_pkg python3-venv ;;
        esac
    fi

    # ODBC dev headers for pyodbc
    case "${PKG_MGR}" in
        dnf|yum) _install_pkg unixODBC-devel ;;
        apt-get) _install_pkg unixodbc-dev ;;
    esac

    log "System dependencies ready ✓"
}

check_python_version() {
    local PY
    PY=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    local MAJOR MINOR
    MAJOR=$(echo "${PY}" | cut -d. -f1)
    MINOR=$(echo "${PY}" | cut -d. -f2)
    if [[ "${MAJOR}" -lt 3 || ( "${MAJOR}" -eq 3 && "${MINOR}" -lt 8 ) ]]; then
        die "Python 3.8 or later is required (found ${PY})"
    fi
    log "Python version: ${PY} ✓"
}

setup_directories() {
    log "Creating directory layout at ${INSTALL_DIR} …"
    mkdir -p "${SCRIPTS_DIR}" "${LOGS_DIR}"
    chmod 755 "/opt/VEZA" "${INSTALL_DIR}" "${SCRIPTS_DIR}" "${LOGS_DIR}"
}

clone_or_update_repo() {
    local tmp_dir
    tmp_dir=$(mktemp -d)

    log "Cloning repository …"
    GIT_TERMINAL_PROMPT=0 git clone --branch "${BRANCH}" --depth 1 --single-branch \
        "${REPO_URL}" "${tmp_dir}" \
        || die "git clone failed — verify REPO_URL (${REPO_URL}) and network connectivity"

    [[ -d "${tmp_dir}/${INTEGRATION_SUBDIR}" ]] \
        || die "Integration directory not found in repo: ${INTEGRATION_SUBDIR}"

    log "Staging integration files to ${SCRIPTS_DIR}/ …"
    cp -f "${tmp_dir}/${INTEGRATION_SUBDIR}"/*.py  "${SCRIPTS_DIR}/" 2>/dev/null || true
    [[ -f "${tmp_dir}/${INTEGRATION_SUBDIR}/requirements.txt" ]] \
        && cp -f "${tmp_dir}/${INTEGRATION_SUBDIR}/requirements.txt" "${SCRIPTS_DIR}/"

    rm -rf "${tmp_dir}"
    log "Repository cloned ✓"
}

setup_venv() {
    log "Setting up Python virtual environment …"
    if [[ ! -d "${VENV_DIR}" ]]; then
        python3 -m venv "${VENV_DIR}"
    fi
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet -r "${SCRIPTS_DIR}/requirements.txt"
    log "Dependencies installed ✓"
}

prompt_or_env() {
    local var_name="$1"
    local prompt_text="$2"
    local is_secret="${3:-false}"
    local current_val="${!var_name:-}"

    if [[ -n "${current_val}" ]]; then
        echo "${current_val}"
        return
    fi

    if [[ "${NON_INTERACTIVE}" == "true" ]]; then
        die "Non-interactive mode: ${var_name} must be set as an environment variable"
    fi

    if [[ "${is_secret}" == "true" ]]; then
        IFS= read -r -s -p "${prompt_text}: " value </dev/tty
        echo "" >/dev/tty
    else
        IFS= read -r -p "${prompt_text}: " value </dev/tty
    fi
    echo "${value}"
}

generate_env_file() {
    local env_path="${SCRIPTS_DIR}/.env"

    if [[ -f "${env_path}" && "${OVERWRITE_ENV}" == "false" ]]; then
        warn ".env already exists — skipping generation (use --overwrite-env to replace)"
        return
    fi

    log "Gathering credentials …"
    local veza_url veza_api_key db_server db_port db_name db_user db_password db_schema

    veza_url=$(prompt_or_env    "VEZA_URL"       "Veza instance URL (e.g. acme.veza.com)")
    veza_api_key=$(prompt_or_env "VEZA_API_KEY"  "Veza API key" "true")
    db_server=$(prompt_or_env   "JDE_DB_SERVER"  "JDE SQL Server host")
    db_port=$(prompt_or_env     "JDE_DB_PORT"    "SQL Server port" && echo "${JDE_DB_PORT:-1433}")
    db_name=$(prompt_or_env     "JDE_DB_NAME"    "JDE database name")
    db_user=$(prompt_or_env     "JDE_DB_USER"    "DB username")
    db_password=$(prompt_or_env "JDE_DB_PASSWORD" "DB password" "true")
    db_schema=$(prompt_or_env   "JDE_DB_SCHEMA"  "DB schema (default: dbo)" && echo "${JDE_DB_SCHEMA:-dbo}")

    cat > "${env_path}" <<EOF
# JDE MS SQL Server Connection
JDE_DB_SERVER=${db_server}
JDE_DB_PORT=${db_port:-1433}
JDE_DB_NAME=${db_name}
JDE_DB_USER=${db_user}
JDE_DB_PASSWORD=${db_password}
JDE_DB_SCHEMA=${db_schema:-dbo}

# Veza Configuration
VEZA_URL=${veza_url}
VEZA_API_KEY=${veza_api_key}

# OAA Provider Settings (optional overrides)
# PROVIDER_NAME=JD Edwards
# DATASOURCE_NAME=JDE EnterpriseOne
EOF

    log ".env created at ${env_path} ✓"
    warn "Remember to restrict .env permissions before production use: chmod 600 ${env_path}"
}

print_summary() {
    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo "  JDE → Veza OAA Integration — Installation Complete"
    echo "════════════════════════════════════════════════════════════"
    echo "  Install path:  ${SCRIPTS_DIR}/"
    echo "  Logs:          ${LOGS_DIR}/"
    echo ""
    echo "  Next steps:"
    echo "  1. Review and update credentials:"
    echo "     ${SCRIPTS_DIR}/.env"
    echo ""
    echo "  2. Test with a dry-run:"
    echo "     cd ${SCRIPTS_DIR}"
    echo "     ./venv/bin/python3 jde.py --dry-run --save-json --log-level DEBUG"
    echo ""
    echo "  3. Schedule via cron (example — daily at 02:00):"
    echo "     0 2 * * * jde-veza ${SCRIPTS_DIR}/venv/bin/python3 \\"
    echo "       ${SCRIPTS_DIR}/jde.py \\"
    echo "       --env-file ${SCRIPTS_DIR}/.env \\"
    echo "       --log-level INFO >> ${LOGS_DIR}/cron.log 2>&1"
    echo "════════════════════════════════════════════════════════════"
}

# ── Main ──────────────────────────────────────────────────────────────────────
require_root
detect_os
install_system_deps
check_python_version
setup_directories
clone_or_update_repo
setup_venv
generate_env_file
print_summary
