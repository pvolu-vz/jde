# Artifact Specifications — Veza OAA Integration

This file contains the full specification for all five artifacts generated in Step 2.

> **Note on data samples:** If `./integrations/<system_slug>/samples/` contains files, use them to infer field names, entity attributes, and permission values throughout all artifacts below. If the directory is empty or absent, create `./integrations/<system_slug>/samples/SAMPLES.md` explaining what files to place there (e.g., a 5-row CSV export, a single JSON API response, or a `DESCRIBE TABLE` output).

---

## A. Main Python Script — `<system_name>.py`

```python
#!/usr/bin/env python3
"""
<System Name> to Veza OAA Integration Script
Collects identity and permission data from <System Name> and pushes to Veza.
"""
import argparse
import logging
import os
import sys
from dotenv import load_dotenv
from oaaclient.client import OAAClient, OAAClientError
from oaaclient.templates import CustomApplication, OAAPermission
```

**Required CLI arguments** (always include all of these):
- `--env-file` (default: `.env`)
- `--veza-url` (also reads `VEZA_URL` env var)
- `--veza-api-key` (also reads `VEZA_API_KEY` env var)
- `--provider-name` (default: system name)
- `--datasource-name` (default: system instance identifier)
- `--dry-run` (skip Veza push)
- `--log-level` (DEBUG/INFO/WARNING/ERROR, default: INFO)
- All source-specific args (API URL, CSV path, DB connection, etc.)

**Credential precedence** — CLI arg → env var → .env file:
```python
def load_config(args):
    if args.env_file and os.path.exists(args.env_file):
        load_dotenv(args.env_file)
    return {
        "veza_url": args.veza_url or os.getenv("VEZA_URL"),
        "veza_api_key": args.veza_api_key or os.getenv("VEZA_API_KEY"),
        # source-specific credentials follow the same pattern
    }
```

**Data collection** — adapter pattern based on data source type:
- REST API: `requests` with retry logic and proper error handling
- CSV/XLSX: `csv` module or `openpyxl`/`pandas`
- Database: `sqlalchemy` or appropriate driver; always use parameterized queries
- Data lake: `boto3` / `azure-storage-blob` / `google-cloud-storage`

**OAA payload assembly**:
```python
def build_oaa_payload(data, args):
    app = CustomApplication(name=args.datasource_name, application_type=args.provider_name)
    app.add_custom_permission("read",  [OAAPermission.DataRead])
    app.add_custom_permission("write", [OAAPermission.DataRead, OAAPermission.DataWrite])
    app.add_custom_permission("admin", [OAAPermission.DataRead, OAAPermission.DataWrite,
                                        OAAPermission.MetadataRead, OAAPermission.MetadataWrite])
    # Add local users, groups, resources, permissions based on actual data model
    return app
```

**Push to Veza** — match the NetApp reference pattern exactly:
```python
def push_to_veza(veza_url, veza_api_key, provider_name, datasource_name, app, dry_run=False):
    if dry_run:
        logging.info("[DRY RUN] Payload built successfully — skipping push")
        return
    veza_con = OAAClient(url=veza_url, token=veza_api_key)
    try:
        response = veza_con.push_application(
            provider_name=provider_name,
            data_source_name=datasource_name,
            application_object=app,
        )
        if response.get("warnings"):
            for w in response["warnings"]:
                logging.warning("Veza warning: %s", w)
        logging.info("Successfully pushed to Veza")
    except OAAClientError as e:
        logging.error("Veza push failed: %s — %s (HTTP %s)", e.error, e.message, e.status_code)
        if hasattr(e, "details"):
            for d in e.details:
                logging.error("  Detail: %s", d)
        sys.exit(1)
```

Include `if __name__ == "__main__":` with structured logging setup.

---

## B. Bash Installer — `install_<system_name>.sh`

Mirror the NetApp `install_ontap.sh` pattern exactly:

```bash
#!/usr/bin/env bash
# install_<system_name>.sh — One-command installer for <System Name>-Veza OAA integration
set -euo pipefail
```

The installer must:
1. Detect Linux distro — support RHEL/CentOS/Fedora (`dnf`/`yum`) and Ubuntu/Debian (`apt`). Also read `OS_ID` from `/etc/os-release` (`ID=` field) so Amazon Linux (`amzn`) can be handled specially.
2. Install packages **one at a time with a pre-check**, never in a single bulk `dnf install` call — a conflict on one package (e.g. `curl`) will fail the entire command and block `git` from installing:
   ```bash
   _install_pkg() {
       local pkg="$1"
       case "${PKG_MGR}" in
           dnf|yum) "${PKG_MGR}" install -y "${pkg}" >/dev/null ;;
           apt-get) apt-get install -y "${pkg}" >/dev/null ;;
       esac
   }
   command -v git     &>/dev/null || _install_pkg git
   command -v python3 &>/dev/null || _install_pkg python3
   python3 -m pip --version &>/dev/null || _install_pkg python3-pip
   ```
3. **`curl` on Amazon Linux** — `curl-minimal` is pre-installed and conflicts with full `curl`. Skip the curl install if `OS_ID=amzn` and curl is already present:
   ```bash
   if ! command -v curl &>/dev/null; then
       [[ "${OS_ID}" == "amzn" ]] \
           && warn "Skipping curl on Amazon Linux (curl-minimal conflict)" \
           || _install_pkg curl
   fi
   ```
4. **`python3-venv` on Amazon Linux 2023 / RHEL 9+** — `venv` is built into `python3`; `python3-venv` is not a separate package and `dnf` will error. Check first, and use `python3-virtualenv` as the fallback package name for dnf/yum:
   ```bash
   if ! python3 -m venv --help &>/dev/null; then
       case "${PKG_MGR}" in
           dnf|yum) _install_pkg python3-virtualenv ;;
           apt-get) _install_pkg python3-venv ;;
       esac
   fi
   ```
5. Check Python ≥ 3.8 and exit with clear message if not met.
6. **Clone with `GIT_TERMINAL_PROMPT=0`** — sparse-checkout flags (`--filter=blob:none --sparse`) trigger credential prompts even on public repos. Use a simple shallow clone into a temp dir, copy integration files, then clean up:
   ```bash
   tmp_dir=$(mktemp -d)
   GIT_TERMINAL_PROMPT=0 git clone --branch "${BRANCH}" --depth 1 --single-branch \
       "${REPO_URL}" "${tmp_dir}" || die "git clone failed"
   cp -f "${tmp_dir}/${INTEGRATION_SUBDIR}"/*.py  "${SCRIPTS_DIR}/"
   cp -f "${tmp_dir}/${INTEGRATION_SUBDIR}/requirements.txt" "${SCRIPTS_DIR}/"
   rm -rf "${tmp_dir}"
   ```
7. Create this directory layout:
   ```
   /opt/VEZA/<system-slug>-veza/
   ├── scripts/
   │   ├── <system_name>.py
   │   ├── requirements.txt
   │   ├── .env               (generated by installer, chmod 600)
   │   └── venv/
   └── logs/
   ```
8. Create a Python venv and install `requirements.txt`.
9. **Interactive prompts must use `/dev/tty`** — when the script runs via `curl | bash`, stdin is the pipe, not the terminal. All `read` calls must redirect from `/dev/tty`:
   ```bash
   IFS= read -r -p "Veza URL: " value </dev/tty
   IFS= read -r -s -p "API key: " secret </dev/tty; echo >/dev/tty
   ```
10. Support non-interactive/CI mode via env vars:
    ```bash
    VEZA_URL=... VEZA_API_KEY=... SOURCE_API_URL=... SOURCE_API_KEY=... \
    bash install_<system_name>.sh --non-interactive
    ```
11. Generate `.env` with `chmod 600` and explanatory comments.
12. Accept flags: `--non-interactive`, `--overwrite-env`, `--install-dir <path>`, `--repo-url <url>`, `--branch <name>`
13. Print final summary: install path, next steps, example run command.

---

## C. Requirements File — `requirements.txt`

Base dependencies always included:
```
oaaclient>=3.0.0
python-dotenv>=1.0.0
requests>=2.31.0
urllib3>=2.0.0
```

Add only what the data source requires:
- CSV/XLSX: `openpyxl>=3.1.0`
- PostgreSQL: `psycopg2-binary`
- Oracle: `cx_Oracle`
- MSSQL: `pymssql`
- MySQL: `pymysql`
- S3: `boto3>=1.34.0`
- ADLS: `azure-storage-blob>=12.19.0`
- GCS: `google-cloud-storage>=2.14.0`

---

## D. Environment Template — `.env.example`

```bash
# <System Name> Source Configuration
SOURCE_API_URL=https://your-source-system.example.com
SOURCE_API_KEY=your_api_key_here
# SOURCE_CSV_PATH=/path/to/data.csv
# SOURCE_DB_URL=postgresql://user:pass@host/db

# Veza Configuration
VEZA_URL=your-company.veza.com
VEZA_API_KEY=your_veza_api_key_here

# OAA Provider Settings (optional overrides)
# PROVIDER_NAME=<System Name>
# DATASOURCE_NAME=<System Name> Instance
```

---

## E. README — `README.md`

Write comprehensive documentation covering all of the following sections:

1. **Overview** — what the script does, which Veza entities it creates, data flow
2. **How It Works** — numbered steps matching the actual code flow
3. **Prerequisites** — OS, Python version, network access, source system API needs, Veza requirements
4. **Quick Start** — one-command installer using `curl -fsSL ... | bash`
5. **Manual Installation** — RHEL and Ubuntu instructions, venv setup, .env config
6. **Usage** — full CLI arguments table (Argument | Required | Values | Default | Description), with examples
7. **Deployment on Linux** — service account creation, file permissions, SELinux (RHEL), cron setup, log rotation:
   - Create dedicated service account: `sudo useradd -r -s /bin/bash -m -d /opt/<slug>-veza <slug>-veza`
   - `chmod 600` on `.env`, `chmod 700` on scripts dir
   - SELinux check (`getenforce`) and `restorecon` guidance for RHEL
   - Wrapper script for cron + `/etc/cron.d/` example
   - Log rotation config
8. **Multiple Instances** (if applicable) — separate .env files, `--env-file` flag, cron staggering
9. **Security Considerations** — credential rotation, file permissions, SELinux/AppArmor
10. **Troubleshooting** — auth failures, connectivity issues, missing modules, Veza push warnings
11. **Changelog** — v1.0 initial release
