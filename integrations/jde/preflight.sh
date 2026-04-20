#!/bin/bash

#####################################################################
# JD Edwards EnterpriseOne Pre-Flight Validation Script
# Purpose: Validate all prerequisites before deploying jde.py
# Date: 2026-04-20
#####################################################################

set -o pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'
BOLD='\033[1m'

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
REQUIREMENTS_FILE="${SCRIPT_DIR}/requirements.txt"
JDE_SCRIPT="${SCRIPT_DIR}/jde.py"
LOG_FILE="${SCRIPT_DIR}/preflight_$(date +%Y%m%d_%H%M%S).log"

# Test counters
TESTS_PASSED=0
TESTS_FAILED=0
TESTS_WARNING=0

#####################################################################
# Utility Functions
#####################################################################

print_header() {
    echo -e "\n${BOLD}${BLUE}=== $1 ===${NC}\n"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
    ((TESTS_PASSED++))
}

print_fail() {
    echo -e "${RED}✗${NC} $1"
    ((TESTS_FAILED++))
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
    ((TESTS_WARNING++))
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

log_output() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

#####################################################################
# Validation Functions
#####################################################################

validate_system_requirements() {
    print_header "System Requirements Validation"

    # Check Python version
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
        PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
        PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

        if [[ "$PYTHON_MAJOR" -ge 3 ]] && [[ "$PYTHON_MINOR" -ge 9 ]]; then
            print_success "Python version $PYTHON_VERSION (>= 3.9 required)"
        else
            print_fail "Python version $PYTHON_VERSION is too old (>= 3.9 required)"
        fi
    else
        print_fail "Python 3 not found. Please install Python 3.9 or higher"
        return 1
    fi

    # Check pip
    if command -v pip3 &> /dev/null; then
        PIP_VERSION=$(pip3 --version 2>&1 | awk '{print $2}')
        print_success "pip3 version $PIP_VERSION installed"
    else
        print_fail "pip3 not found. Please install pip3"
        echo -e "  ${YELLOW}Install with: python3 -m ensurepip --upgrade${NC}"
    fi

    # Check virtual environment
    if [[ -n "$VIRTUAL_ENV" ]]; then
        print_success "Running in virtual environment: $VIRTUAL_ENV"
    else
        print_warning "Not running in virtual environment (recommended but not required)"
        echo -e "  ${YELLOW}Create one with: python3 -m venv venv && source venv/bin/activate${NC}"
    fi

    # OS detection
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if [ -f /etc/os-release ]; then
            OS_NAME=$(grep "^NAME=" /etc/os-release | cut -d'"' -f2)
            OS_VERSION=$(grep "^VERSION=" /etc/os-release | cut -d'"' -f2)
            print_info "Operating System: $OS_NAME $OS_VERSION"
        else
            print_info "Operating System: Linux"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        print_info "Operating System: macOS"
    else
        print_warning "Operating System: $OSTYPE (RHEL 8+ recommended for production)"
    fi

    # Check curl
    if command -v curl &> /dev/null; then
        print_success "curl installed (required for API tests)"
    else
        print_fail "curl not found. Please install curl"
    fi

    # Check jq (optional)
    if command -v jq &> /dev/null; then
        print_success "jq installed (optional, for enhanced JSON parsing)"
    else
        print_warning "jq not found (optional). Will use Python for JSON parsing"
    fi

    # Check ODBC driver for pyodbc (MS SQL Server)
    if odbcinst -q -d 2>/dev/null | grep -qi "ODBC Driver.*SQL Server\|FreeTDS"; then
        DRIVER=$(odbcinst -q -d 2>/dev/null | grep -i "ODBC Driver.*SQL Server\|FreeTDS" | head -1 | tr -d '[]')
        print_success "ODBC driver found: $DRIVER"
    else
        print_warning "No ODBC driver for SQL Server detected"
        echo -e "  ${YELLOW}Install Microsoft ODBC Driver 17+ for SQL Server or FreeTDS${NC}"
        echo -e "  ${YELLOW}See: https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server${NC}"
    fi
}

validate_dependencies() {
    print_header "Python Dependencies Validation"

    # Check requirements.txt
    if [ ! -f "$REQUIREMENTS_FILE" ]; then
        print_fail "requirements.txt not found at $REQUIREMENTS_FILE"
        return 1
    fi
    print_success "requirements.txt found"

    # Prefer venv Python; fall back to system python3
    VENV_PYTHON="${SCRIPT_DIR}/venv/bin/python"
    if [[ -x "$VENV_PYTHON" ]]; then
        PYTHON_CMD="$VENV_PYTHON"
        print_info "Using venv Python: $VENV_PYTHON"
    else
        PYTHON_CMD="python3"
        print_warning "Venv not found at ${SCRIPT_DIR}/venv — using system python3 (packages may be missing)"
    fi

    echo -e "\n${BOLD}Checking installed packages:${NC}"

    # requests
    if "$PYTHON_CMD" -c "import requests" 2>/dev/null; then
        VER=$("$PYTHON_CMD" -c "import requests; print(requests.__version__)" 2>/dev/null)
        print_success "requests==$VER installed"
    else
        print_fail "requests not installed"
    fi

    # python-dotenv
    if "$PYTHON_CMD" -c "import dotenv" 2>/dev/null; then
        VER=$("$PYTHON_CMD" -c "import dotenv; print(dotenv.__version__)" 2>/dev/null)
        print_success "python-dotenv==$VER installed"
    else
        print_fail "python-dotenv not installed"
    fi

    # oaaclient
    if "$PYTHON_CMD" -c "import oaaclient" 2>/dev/null; then
        VER=$("$PYTHON_CMD" -c "import importlib.metadata; print(importlib.metadata.version('oaaclient'))" 2>/dev/null)
        print_success "oaaclient==${VER:-installed}"
    else
        print_fail "oaaclient not installed"
    fi

    # pyodbc
    if "$PYTHON_CMD" -c "import pyodbc" 2>/dev/null; then
        VER=$("$PYTHON_CMD" -c "import pyodbc; print(pyodbc.version)" 2>/dev/null)
        print_success "pyodbc==$VER installed"
    else
        print_fail "pyodbc not installed"
    fi

    # urllib3
    if "$PYTHON_CMD" -c "import urllib3" 2>/dev/null; then
        VER=$("$PYTHON_CMD" -c "import urllib3; print(urllib3.__version__)" 2>/dev/null)
        print_success "urllib3==$VER installed"
    else
        print_fail "urllib3 not installed"
    fi

    if [[ $TESTS_FAILED -gt 0 ]]; then
        echo -e "\n${YELLOW}Install dependencies with: ${SCRIPT_DIR}/venv/bin/pip install -r $REQUIREMENTS_FILE${NC}"
    fi
}

validate_configuration() {
    print_header "Configuration File Validation"

    if [ ! -f "$ENV_FILE" ]; then
        print_fail ".env file not found at $ENV_FILE"
        echo -e "  ${YELLOW}Create one using: Option 10 from main menu${NC}"
        return 1
    fi
    print_success ".env file exists"

    # Check file permissions
    PERMS=$(stat -f "%OLp" "$ENV_FILE" 2>/dev/null || stat -c "%a" "$ENV_FILE" 2>/dev/null)
    if [[ "$PERMS" == "600" ]]; then
        print_success ".env file permissions are secure (600)"
    else
        print_warning ".env file permissions are $PERMS (should be 600 for security)"
        echo -e "  ${YELLOW}Fix with: chmod 600 $ENV_FILE${NC}"
    fi

    echo -e "\n${BOLD}Validating environment variables:${NC}"

    source "$ENV_FILE"

    echo -e "\n${BOLD}JDE SQL Server Configuration:${NC}"
    check_env_var "JDE_DB_SERVER"   "$JDE_DB_SERVER"
    check_env_var "JDE_DB_PORT"     "$JDE_DB_PORT"
    check_env_var "JDE_DB_NAME"     "$JDE_DB_NAME"
    check_env_var "JDE_DB_USER"     "$JDE_DB_USER"
    check_env_var "JDE_DB_PASSWORD" "$JDE_DB_PASSWORD"
    check_env_var "JDE_DB_SCHEMA"   "$JDE_DB_SCHEMA" "optional"

    echo -e "\n${BOLD}Veza Configuration:${NC}"
    check_env_var "VEZA_URL"     "$VEZA_URL"
    check_env_var "VEZA_API_KEY" "$VEZA_API_KEY"
}

check_env_var() {
    local var_name=$1
    local var_value=$2
    local optional=$3

    if [[ -z "$var_value" ]]; then
        if [[ "$optional" == "optional" ]]; then
            print_info "$var_name not set (optional)"
        else
            print_fail "$var_name is not set"
        fi
    elif [[ "$var_value" =~ ^your_.* ]]; then
        print_warning "$var_name contains placeholder value"
    else
        if [[ "$var_name" =~ PASSWORD|KEY|TOKEN|SECRET ]]; then
            print_success "$var_name set (${var_value:0:8}...)"
        else
            print_success "$var_name set"
        fi
    fi
}

validate_network_connectivity() {
    print_header "Network Connectivity Tests"

    source "$ENV_FILE" 2>/dev/null || true

    echo -e "${BOLD}Testing connectivity to required endpoints:${NC}\n"

    # Test JDE SQL Server (TCP port)
    if [[ -n "$JDE_DB_SERVER" && ! "$JDE_DB_SERVER" =~ your-.* ]]; then
        DB_PORT="${JDE_DB_PORT:-1433}"
        test_tcp_connectivity "JDE SQL Server" "$JDE_DB_SERVER" "$DB_PORT"
    else
        print_warning "JDE_DB_SERVER not configured, skipping SQL Server connectivity test"
    fi

    # Test Veza (HTTPS)
    if [[ -n "$VEZA_URL" && ! "$VEZA_URL" =~ your-.* ]]; then
        VEZA_HOST=$(echo "$VEZA_URL" | sed -E 's|https?://||' | cut -d'/' -f1)
        test_https_connectivity "Veza Instance" "$VEZA_HOST" 443
    else
        print_warning "VEZA_URL not configured, skipping Veza connectivity test"
    fi
}

test_tcp_connectivity() {
    local name=$1
    local host=$2
    local port=$3

    if command -v nc &> /dev/null; then
        if nc -zw 5 "$host" "$port" 2>/dev/null; then
            print_success "$name ($host:$port) - TCP port reachable"
        else
            print_fail "$name ($host:$port) - TCP port unreachable"
        fi
    elif command -v curl &> /dev/null; then
        if curl -s --connect-timeout 5 "telnet://${host}:${port}" 2>&1 | grep -qv "Failed"; then
            print_success "$name ($host:$port) - TCP port reachable"
        else
            # curl doesn't support telnet cleanly everywhere; try a no-op
            if timeout 5 bash -c "echo >/dev/tcp/${host}/${port}" 2>/dev/null; then
                print_success "$name ($host:$port) - TCP port reachable"
            else
                print_fail "$name ($host:$port) - TCP port unreachable"
            fi
        fi
    else
        if timeout 5 bash -c "echo >/dev/tcp/${host}/${port}" 2>/dev/null; then
            print_success "$name ($host:$port) - TCP port reachable"
        else
            print_fail "$name ($host:$port) - TCP port unreachable (install nc for better diagnostics)"
        fi
    fi
}

test_https_connectivity() {
    local name=$1
    local host=$2
    local port=$3

    if command -v curl &> /dev/null; then
        RESPONSE=$(curl -s -o /dev/null -w "%{http_code}|%{time_total}" -m 10 "https://${host}" 2>/dev/null)
        HTTP_CODE=$(echo "$RESPONSE" | cut -d'|' -f1)
        TIME=$(echo "$RESPONSE" | cut -d'|' -f2)

        if [[ -n "$HTTP_CODE" && "$HTTP_CODE" != "000" ]]; then
            TIME_MS=$(echo "$TIME * 1000" | bc 2>/dev/null || echo "$TIME")
            print_success "$name ($host:$port) - HTTP $HTTP_CODE - ${TIME_MS}ms"
        else
            print_fail "$name ($host:$port) - HTTPS connection failed"
        fi
    else
        if command -v nc &> /dev/null; then
            if nc -zw 5 "$host" "$port" 2>/dev/null; then
                print_success "$name ($host:$port) - Port is open"
            else
                print_fail "$name ($host:$port) - Port is closed or unreachable"
            fi
        else
            print_warning "$name ($host:$port) - Cannot test (curl and nc not available)"
        fi
    fi
}

validate_api_authentication() {
    print_header "API Authentication Tests"

    source "$ENV_FILE" 2>/dev/null || true

    # Test Veza API key
    echo -e "${BOLD}Testing Veza API Authentication:${NC}"
    if [[ -n "$VEZA_URL" && -n "$VEZA_API_KEY" && ! "$VEZA_URL" =~ your-.* && ! "$VEZA_API_KEY" =~ your_.* ]]; then
        VEZA_BASE_URL="https://${VEZA_URL}"
        echo -e "${BLUE}[DEBUG] Request: GET ${VEZA_BASE_URL}/api/v1/providers${NC}"
        echo -e "${BLUE}[DEBUG] Authorization: Bearer ${VEZA_API_KEY:0:8}...${NC}"

        RESPONSE_BODY=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X GET "${VEZA_BASE_URL}/api/v1/providers" \
            -H "Authorization: Bearer $VEZA_API_KEY" \
            -H "Content-Type: application/json" \
            2>/dev/null)

        HTTP_CODE=$(echo "$RESPONSE_BODY" | grep "HTTP_CODE:" | cut -d: -f2)
        BODY=$(echo "$RESPONSE_BODY" | sed '/HTTP_CODE:/d')

        if [[ "$HTTP_CODE" == "200" ]]; then
            print_success "Veza API authentication successful (HTTP 200)"
        elif [[ "$HTTP_CODE" == "401" ]]; then
            print_fail "Veza API authentication failed (HTTP 401 - Invalid API key)"
            echo -e "${RED}[DEBUG] Response body:${NC}"
            echo "$BODY" | python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))" 2>/dev/null || echo "$BODY"
        elif [[ "$HTTP_CODE" == "403" ]]; then
            print_fail "Veza API authentication failed (HTTP 403 - Access forbidden)"
            echo -e "${RED}[DEBUG] Response body:${NC}"
            echo "$BODY" | python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))" 2>/dev/null || echo "$BODY"
        else
            print_warning "Veza API response: HTTP $HTTP_CODE"
            echo -e "${YELLOW}[DEBUG] Response body:${NC}"
            echo "$BODY" | python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))" 2>/dev/null || echo "$BODY"
        fi
    else
        print_warning "Veza credentials not configured, skipping Veza authentication test"
    fi

    # Test JDE SQL Server connectivity via pyodbc
    echo -e "\n${BOLD}Testing JDE SQL Server Authentication:${NC}"
    if [[ -n "$JDE_DB_SERVER" && -n "$JDE_DB_USER" && -n "$JDE_DB_PASSWORD" && ! "$JDE_DB_SERVER" =~ your-.* ]]; then
        DB_PORT="${JDE_DB_PORT:-1433}"
        DB_NAME="${JDE_DB_NAME:-master}"

        VENV_PYTHON="${SCRIPT_DIR}/venv/bin/python"
        PYTHON_CMD=$([[ -x "$VENV_PYTHON" ]] && echo "$VENV_PYTHON" || echo "python3")

        echo -e "${BLUE}[DEBUG] Server: ${JDE_DB_SERVER}:${DB_PORT}, DB: ${DB_NAME}, User: ${JDE_DB_USER}${NC}"

        DB_RESULT=$("$PYTHON_CMD" - <<PYEOF 2>&1
import sys
try:
    import pyodbc
    # Try Microsoft ODBC Driver first, then FreeTDS
    drivers = [d for d in pyodbc.drivers() if 'SQL Server' in d or 'FreeTDS' in d]
    if not drivers:
        print("NO_DRIVER")
        sys.exit(1)
    driver = sorted(drivers)[-1]
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER=${JDE_DB_SERVER},{DB_PORT};"
        f"DATABASE=${DB_NAME};"
        f"UID=${JDE_DB_USER};"
        f"PWD=${JDE_DB_PASSWORD};"
        "TrustServerCertificate=yes;"
    )
    conn = pyodbc.connect(conn_str, timeout=10)
    cur = conn.cursor()
    cur.execute("SELECT @@VERSION")
    row = cur.fetchone()
    conn.close()
    print("OK:" + str(row[0]).split('\n')[0].strip())
except pyodbc.Error as e:
    print("ERROR:" + str(e))
except Exception as e:
    print("EXCEPTION:" + str(e))
PYEOF
)

        if [[ "$DB_RESULT" == OK:* ]]; then
            VERSION="${DB_RESULT#OK:}"
            print_success "SQL Server connection successful"
            print_info "Server version: $VERSION"
        elif [[ "$DB_RESULT" == "NO_DRIVER" ]]; then
            print_fail "No ODBC driver for SQL Server found — install Microsoft ODBC Driver 17+"
        elif [[ "$DB_RESULT" == ERROR:* || "$DB_RESULT" == EXCEPTION:* ]]; then
            print_fail "SQL Server connection failed"
            echo -e "${RED}[DEBUG] ${DB_RESULT#*:}${NC}"
        else
            print_warning "Unexpected SQL Server test output: $DB_RESULT"
        fi
    else
        print_info "JDE DB credentials not configured, skipping SQL Server authentication test"
    fi
}

validate_api_endpoints() {
    print_header "API Endpoint Accessibility Tests"

    source "$ENV_FILE" 2>/dev/null || true

    echo -e "${BOLD}Testing authenticated Veza endpoint access:${NC}\n"

    if [[ -n "$VEZA_URL" && -n "$VEZA_API_KEY" && ! "$VEZA_URL" =~ your-.* ]]; then
        VEZA_BASE_URL="https://${VEZA_URL}"
        test_veza_endpoint "Veza Query API" "${VEZA_BASE_URL}/api/v1/assessments/query_spec:nodes" "$VEZA_API_KEY"
    else
        print_warning "Veza credentials not configured, skipping endpoint test"
    fi
}

test_veza_endpoint() {
    local name=$1
    local url=$2
    local api_key=$3

    local QUERY='{"query":"nodes{InstanceId first:1}"}'

    echo -e "${BLUE}[DEBUG] Request: POST $url${NC}"
    echo -e "${BLUE}[DEBUG] Query: $QUERY${NC}"

    RESPONSE_BODY=$(curl -s -w "\nHTTP_CODE:%{http_code}" -X POST "$url" \
        -H "Authorization: Bearer $api_key" \
        -H "Content-Type: application/json" \
        -d "$QUERY" \
        2>/dev/null)

    HTTP_CODE=$(echo "$RESPONSE_BODY" | grep "HTTP_CODE:" | cut -d: -f2)
    BODY=$(echo "$RESPONSE_BODY" | sed '/HTTP_CODE:/d')

    if [[ "$HTTP_CODE" == "200" ]]; then
        print_success "$name accessible (HTTP 200)"
    elif [[ "$HTTP_CODE" == "401" ]]; then
        print_fail "$name failed (HTTP 401 - Invalid API key)"
        echo "$BODY" | python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))" 2>/dev/null || echo "$BODY"
    elif [[ "$HTTP_CODE" == "403" ]]; then
        print_fail "$name failed (HTTP 403 - Insufficient permissions)"
        echo "$BODY" | python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))" 2>/dev/null || echo "$BODY"
    else
        print_warning "$name returned HTTP $HTTP_CODE"
        echo "$BODY" | python3 -c "import sys, json; print(json.dumps(json.load(sys.stdin), indent=2))" 2>/dev/null || echo "$BODY"
    fi
}

validate_deployment_structure() {
    print_header "Deployment Structure Validation"

    if [ -f "$JDE_SCRIPT" ]; then
        print_success "jde.py exists at $JDE_SCRIPT"

        if [ -r "$JDE_SCRIPT" ]; then
            print_success "jde.py is readable"
        else
            print_fail "jde.py exists but is not readable"
        fi

        if head -n 1 "$JDE_SCRIPT" | grep -q "^#!"; then
            if [ -x "$JDE_SCRIPT" ]; then
                print_success "jde.py is executable"
            else
                print_warning "jde.py has shebang but is not executable"
                echo -e "  ${YELLOW}Fix with: chmod +x $JDE_SCRIPT${NC}"
            fi
        fi
    else
        print_fail "jde.py not found at $JDE_SCRIPT"
        return 1
    fi

    echo -e "\n${BOLD}Current deployment location:${NC}"
    print_info "Script directory: $SCRIPT_DIR"

    if [[ "$SCRIPT_DIR" =~ /opt/jde-veza/scripts ]]; then
        print_success "Deployed in recommended production location"
    else
        print_info "Not in recommended production location (/opt/jde-veza/scripts)"
    fi

    if [ -d "${SCRIPT_DIR}/logs" ]; then
        if [ -w "${SCRIPT_DIR}/logs" ]; then
            print_success "logs/ directory exists and is writable"
        else
            print_warning "logs/ directory exists but is not writable"
        fi
    else
        print_info "logs/ directory does not exist (will be created on first run)"
    fi

    CURRENT_USER=$(whoami)
    if [[ "$CURRENT_USER" == "jde-veza" ]]; then
        print_success "Running as dedicated service account (jde-veza)"
    else
        print_info "Running as user: $CURRENT_USER"
    fi
}

run_all_checks() {
    print_header "Running Complete Pre-Flight Validation"

    echo -e "${BOLD}Starting comprehensive validation at $(date)${NC}\n"

    TESTS_PASSED=0
    TESTS_FAILED=0
    TESTS_WARNING=0

    validate_system_requirements
    echo ""
    validate_dependencies
    echo ""
    validate_configuration
    echo ""
    validate_network_connectivity
    echo ""
    validate_api_authentication
    echo ""
    validate_api_endpoints
    echo ""
    validate_deployment_structure

    print_summary
}

print_summary() {
    print_header "Validation Summary"

    echo -e "${GREEN}Passed:${NC}   $TESTS_PASSED"
    echo -e "${RED}Failed:${NC}   $TESTS_FAILED"
    echo -e "${YELLOW}Warnings:${NC} $TESTS_WARNING"
    echo ""

    if [[ $TESTS_FAILED -eq 0 ]]; then
        echo -e "${GREEN}${BOLD}✓ All critical checks passed! JDE deployment is ready.${NC}"
        echo -e "\nTo run jde.py:"
        echo -e "  ${BLUE}cd $SCRIPT_DIR${NC}"
        echo -e "  ${BLUE}./venv/bin/python3 jde.py --dry-run --save-json${NC}"
        return 0
    else
        echo -e "${RED}${BOLD}✗ Some checks failed. Please address the issues above before deployment.${NC}"
        return 1
    fi
}

display_current_config() {
    print_header "Current Configuration"

    if [ ! -f "$ENV_FILE" ]; then
        print_fail ".env file not found"
        return 1
    fi

    source "$ENV_FILE" 2>/dev/null

    echo -e "${BOLD}JDE SQL Server Configuration:${NC}"
    echo "  JDE_DB_SERVER:   ${JDE_DB_SERVER:-<not set>}"
    echo "  JDE_DB_PORT:     ${JDE_DB_PORT:-<not set>}"
    echo "  JDE_DB_NAME:     ${JDE_DB_NAME:-<not set>}"
    echo "  JDE_DB_USER:     ${JDE_DB_USER:-<not set>}"
    echo "  JDE_DB_PASSWORD: ${JDE_DB_PASSWORD:+<set>}${JDE_DB_PASSWORD:-<not set>}"
    echo "  JDE_DB_SCHEMA:   ${JDE_DB_SCHEMA:-<not set>}"

    echo -e "\n${BOLD}Veza Configuration:${NC}"
    echo "  VEZA_URL:     ${VEZA_URL:-<not set>}"
    echo "  VEZA_API_KEY: ${VEZA_API_KEY:+<set>}${VEZA_API_KEY:-<not set>}"
}

generate_env_template() {
    print_header "Generate .env Template"

    if [ -f "$ENV_FILE" ]; then
        echo -e "${YELLOW}Warning: .env file already exists at $ENV_FILE${NC}"
        read -p "Overwrite? (y/N): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Cancelled."
            return 0
        fi
    fi

    cat > "$ENV_FILE" << 'EOF'
# ── JDE MS SQL Server Connection ──────────────────────────────────────────────
JDE_DB_SERVER=your-sql-server-host.example.com
JDE_DB_PORT=1433
JDE_DB_NAME=JDE_PRODUCTION
JDE_DB_USER=jde_readonly_user
JDE_DB_PASSWORD=your_db_password_here
JDE_DB_SCHEMA=dbo

# ── Veza Configuration ────────────────────────────────────────────────────────
VEZA_URL=your-company.veza.com
VEZA_API_KEY=your_veza_api_key_here

# ── OAA Provider Settings (optional overrides) ────────────────────────────────
# PROVIDER_NAME=JD Edwards
# DATASOURCE_NAME=JDE EnterpriseOne
EOF

    chmod 600 "$ENV_FILE"
    print_success "Template .env file created at $ENV_FILE"
    print_success "File permissions set to 600"
    echo -e "\n${YELLOW}Please edit the file and replace all placeholder values with actual credentials.${NC}"
}

install_dependencies() {
    print_header "Install Python Dependencies"

    if [ ! -f "$REQUIREMENTS_FILE" ]; then
        print_fail "requirements.txt not found at $REQUIREMENTS_FILE"
        return 1
    fi

    VENV_DIR="${SCRIPT_DIR}/venv"
    if [[ ! -d "$VENV_DIR" ]]; then
        echo -e "${BOLD}Creating virtual environment at ${VENV_DIR}...${NC}"
        python3 -m venv "$VENV_DIR"
    fi

    echo -e "${BOLD}Installing dependencies from requirements.txt...${NC}\n"

    if "${VENV_DIR}/bin/pip" install -r "$REQUIREMENTS_FILE"; then
        print_success "All dependencies installed successfully"
        print_info "Activate with: source ${VENV_DIR}/bin/activate"
    else
        print_fail "Failed to install some dependencies"
        return 1
    fi
}

#####################################################################
# Main Menu
#####################################################################

show_menu() {
    echo -e "\n${BOLD}${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}${BLUE}║     JD Edwards EnterpriseOne Pre-Flight Validation         ║${NC}"
    echo -e "${BOLD}${BLUE}╚════════════════════════════════════════════════════════════╝${NC}\n"

    echo -e "${BOLD}Validation Checks:${NC}"
    echo "  1) System Requirements (Python, pip, ODBC driver, OS)"
    echo "  2) Python Dependencies (packages)"
    echo "  3) Configuration File (.env validation)"
    echo "  4) Network Connectivity (SQL Server port, Veza HTTPS)"
    echo "  5) API Authentication (SQL Server + Veza API key)"
    echo "  6) API Endpoint Accessibility (Veza)"
    echo "  7) Deployment Structure"
    echo ""
    echo -e "${BOLD}Comprehensive Tests:${NC}"
    echo "  8) Run ALL Checks (recommended)"
    echo ""
    echo -e "${BOLD}Utilities:${NC}"
    echo "  9) Display Current Configuration"
    echo "  10) Generate Template .env File"
    echo "  11) Install Python Dependencies"
    echo ""
    echo "  0) Exit"
    echo ""
}

main() {
    if [[ "$1" == "--all" ]]; then
        run_all_checks
        exit $?
    fi

    while true; do
        show_menu
        read -p "Select option: " choice

        case $choice in
            1) validate_system_requirements ;;
            2) validate_dependencies ;;
            3) validate_configuration ;;
            4) validate_network_connectivity ;;
            5) validate_api_authentication ;;
            6) validate_api_endpoints ;;
            7) validate_deployment_structure ;;
            8) run_all_checks ;;
            9) display_current_config ;;
            10) generate_env_template ;;
            11) install_dependencies ;;
            0)
                echo -e "\n${BLUE}Exiting. Logs saved to: $LOG_FILE${NC}"
                exit 0
                ;;
            *)
                echo -e "${RED}Invalid option. Please try again.${NC}"
                ;;
        esac

        echo -e "\n${BOLD}Press Enter to continue...${NC}"
        read
    done
}

main "$@"
