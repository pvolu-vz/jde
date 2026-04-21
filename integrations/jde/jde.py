#!/usr/bin/env python3
"""
JD Edwards EnterpriseOne to Veza OAA Integration Script
Collects identity and permission data from JDE via MS SQL Server and pushes to Veza.

Entity model: Local Users → Local Roles → Program Resources (with Add/Change/Delete/View/Run)
Data sources: F0092, F00926, F9860, F00950, F0101, F01151
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from typing import Dict, List, Optional

from dotenv import load_dotenv
from oaaclient.client import OAAClient, OAAClientError
from oaaclient.templates import CustomApplication, OAAPermission, OAAPropertyType

log = logging.getLogger(__name__)


def _setup_logging(log_level: str = "INFO") -> None:
    """Configure file-only logging with hourly rotation to the logs/ folder."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(script_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%d%m%Y-%H%M")
    script_name = os.path.splitext(os.path.basename(__file__))[0]
    log_file = os.path.join(log_dir, f"{script_name}_{timestamp}.log")

    handler = TimedRotatingFileHandler(
        log_file,
        when="h",
        interval=1,
        backupCount=24,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper()))
    root.addHandler(handler)


# ── Permission definitions ────────────────────────────────────────────────────
# Maps JDE security action flags to OAA canonical permission types.

JDE_PERMISSIONS: Dict[str, List[OAAPermission]] = {
    "view":        [OAAPermission.DataRead],
    "add":         [OAAPermission.DataRead, OAAPermission.DataWrite],
    "change":      [OAAPermission.DataRead, OAAPermission.DataWrite],
    "delete":      [OAAPermission.DataRead, OAAPermission.DataWrite,
                    OAAPermission.DataDelete],
    "run":         [OAAPermission.DataRead],
    "full_access": [OAAPermission.DataRead, OAAPermission.DataWrite,
                    OAAPermission.DataDelete,
                    OAAPermission.MetadataRead, OAAPermission.MetadataWrite],
}

# ── SQL Queries ────────────────────────────────────────────────────────────────
# Schema placeholder {schema} is validated against an alphanumeric allowlist
# before interpolation — it is never user-supplied input at runtime.

_SQL_USERS = """
    SELECT
        RTRIM(u.GNUSER)                                        AS user_id,
        RTRIM(COALESCE(a.ABALPH, u.GNDSP, u.GNUSER))         AS display_name,
        CAST(u.GNEADD AS VARCHAR(20))                         AS address_book_number,
        RTRIM(u.GNSTTS)                                       AS status,
        RTRIM(w.WAEMAL)                                       AS email
    FROM {{schema}}.F0092 u
    LEFT JOIN {{schema}}.F0101 a  ON u.GNEADD = a.ABAN8
    LEFT JOIN (
        SELECT WAAB8, WAEMAL,
               ROW_NUMBER() OVER (PARTITION BY WAAB8 ORDER BY WAIDLN) AS rn
        FROM {{schema}}.F01151
        WHERE WAEMAL IS NOT NULL AND RTRIM(WAEMAL) != ''
    ) w ON u.GNEADD = w.WAAB8 AND w.rn = 1
    WHERE RTRIM(u.GNUSER) NOT IN ('', 'JDE')
"""

_SQL_ROLES = """
    SELECT DISTINCT
        RTRIM(r.WKROLE)  AS role_id,
        RTRIM(r.WKRTYPE) AS role_type
    FROM {{schema}}.F00926 r
    WHERE r.WKROLE IS NOT NULL
      AND RTRIM(r.WKROLE) NOT IN ('', 'EVERYONE')
"""

_SQL_USER_ROLES = """
    SELECT
        RTRIM(r.WKUSER) AS user_id,
        RTRIM(r.WKROLE) AS role_id
    FROM {{schema}}.F00926 r
    WHERE r.WKUSER IS NOT NULL AND r.WKROLE IS NOT NULL
      AND RTRIM(r.WKUSER) != '' AND RTRIM(r.WKROLE) != ''
"""

_SQL_PROGRAMS = """
    SELECT
        RTRIM(o.SIOBNM)  AS program_id,
        RTRIM(o.SIDEMD)  AS description,
        RTRIM(o.SIOTP)   AS object_type,
        RTRIM(o.SISYS)   AS product_code
    FROM {{schema}}.F9860 o
    WHERE o.SIOTP IN ('APPL', 'UBE')
      AND o.SIOBNM IS NOT NULL
      AND RTRIM(o.SIOBNM) != ''
"""

_SQL_SECURITY = """
    SELECT
        RTRIM(s.WSAPID)  AS program_id,
        RTRIM(s.WSUSER)  AS user_or_role,
        RTRIM(s.WSSYST)  AS product_code,
        s.WSADD          AS allow_add,
        s.WSCHG          AS allow_change,
        s.WSDEL          AS allow_delete,
        s.WSRQR          AS allow_inquiry,
        s.WSRPT          AS allow_run,
        s.WSNOACC        AS no_access
    FROM {{schema}}.F00950 s
    WHERE s.WSAPID IS NOT NULL
      AND RTRIM(s.WSAPID) != ''
      AND RTRIM(COALESCE(s.WSUSER, '')) NOT IN ('', '*PUBLIC', 'EVERYONE')
"""


def _validate_schema(schema: str) -> str:
    """Reject schema names that contain anything other than word chars (SQL injection guard)."""
    if not re.match(r'^\w+$', schema):
        raise ValueError(f"Invalid schema name: {schema!r} — only alphanumeric and underscore characters are allowed")
    return schema


def _apply_schema(sql_template: str, schema: str) -> str:
    return sql_template.replace("{schema}", schema)


def _stage(label: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[JDE OAA] [{ts}] {label}")


# ── Configuration ─────────────────────────────────────────────────────────────

def load_config(args) -> dict:
    if args.env_file:
        if os.path.exists(args.env_file):
            load_dotenv(args.env_file)
        else:
            log.warning("env file not found: %s — credentials must be set as environment variables", args.env_file)

    return {
        "veza_url":       args.veza_url      or os.getenv("VEZA_URL"),
        "veza_api_key":   args.veza_api_key  or os.getenv("VEZA_API_KEY"),
        "mssql_server":   args.mssql_server  or os.getenv("JDE_DB_SERVER"),
        "mssql_port":     args.mssql_port    or os.getenv("JDE_DB_PORT", "1433"),
        "mssql_db":       args.mssql_db      or os.getenv("JDE_DB_NAME"),
        "mssql_user":     args.mssql_user    or os.getenv("JDE_DB_USER"),
        "mssql_password": args.mssql_password or os.getenv("JDE_DB_PASSWORD"),
        "jde_schema":     args.jde_schema    or os.getenv("JDE_DB_SCHEMA", "dbo"),
    }


# ── Data Loading — Database ────────────────────────────────────────────────────

def load_from_db(config: dict) -> dict:
    """Connect to JDE MS SQL Server and load all required identity/permission data."""
    try:
        import pyodbc  # type: ignore[import-untyped]
    except ImportError:
        log.error("pyodbc is not installed — run: pip install pyodbc")
        sys.exit(1)

    server   = config["mssql_server"]
    port     = config["mssql_port"]
    database = config["mssql_db"]
    user     = config["mssql_user"]
    password = config["mssql_password"]
    schema   = _validate_schema(config.get("jde_schema", "dbo"))

    missing = [k for k, v in {"JDE_DB_SERVER": server, "JDE_DB_NAME": database,
                               "JDE_DB_USER": user, "JDE_DB_PASSWORD": password}.items() if not v]
    if missing:
        log.error("Missing required DB configuration: %s", ", ".join(missing))
        sys.exit(1)

    conn_str = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={server},{port};"
        f"DATABASE={database};"
        f"UID={user};"
        f"PWD={password};"
        "TrustServerCertificate=yes;"
    )

    _stage("Connecting to SQL")
    log.info("Connecting to JDE database at %s/%s (schema: %s)", server, database, schema)
    try:
        conn = pyodbc.connect(conn_str, timeout=30)
    except Exception as exc:
        log.error("Database connection failed: %s", exc)
        sys.exit(1)

    queries = {
        "users":      _apply_schema(_SQL_USERS, schema),
        "roles":      _apply_schema(_SQL_ROLES, schema),
        "user_roles": _apply_schema(_SQL_USER_ROLES, schema),
        "programs":   _apply_schema(_SQL_PROGRAMS, schema),
        "security":   _apply_schema(_SQL_SECURITY, schema),
    }

    data = {}
    try:
        cursor = conn.cursor()
        _stage("Running queries")
        for key, query in queries.items():
            log.info("Fetching %s …", key)
            cursor.execute(query)
            columns = [col[0] for col in cursor.description]
            data[key] = [dict(zip(columns, row)) for row in cursor.fetchall()]
            log.info("  → %d %s records", len(data[key]), key)
    finally:
        conn.close()

    return data


# ── Data Loading — CSV (dry-run / testing) ────────────────────────────────────

def load_from_csv(data_dir: str) -> dict:
    """Load data from CSV files exported from JDE (used for dry-run testing)."""
    expected = {
        "users":      "users.csv",
        "roles":      "roles.csv",
        "user_roles": "user_roles.csv",
        "programs":   "programs.csv",
        "security":   "security.csv",
    }
    data = {}
    for key, filename in expected.items():
        filepath = os.path.join(data_dir, filename)
        if not os.path.exists(filepath):
            log.warning("Sample file not found: %s — %s will be empty", filepath, key)
            data[key] = []
            continue
        with open(filepath, newline="", encoding="utf-8") as fh:
            data[key] = [dict(row) for row in csv.DictReader(fh)]
        log.info("Loaded %d %s records from %s", len(data[key]), key, filename)
    return data


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_yes(value) -> bool:
    """Return True when a JDE security flag field is set to Y/1/TRUE."""
    return str(value).strip().upper() in ("Y", "1", "TRUE", "YES") if value is not None else False


# ── OAA Payload Builder ───────────────────────────────────────────────────────

def build_oaa_payload(data: dict, provider_name: str, datasource_name: str) -> CustomApplication:
    """Assemble a Veza OAA CustomApplication from JDE identity and permission data."""
    app = CustomApplication(name=datasource_name, application_type=provider_name)

    for perm_name, oaa_types in JDE_PERMISSIONS.items():
        app.add_custom_permission(perm_name, oaa_types)

    # Custom properties
    app.property_definitions.define_local_role_property("role_type", OAAPropertyType.STRING)
    app.property_definitions.define_local_user_property("jde_status", OAAPropertyType.STRING)
    app.property_definitions.define_local_user_property("address_book_number", OAAPropertyType.STRING)
    app.property_definitions.define_resource_property("Program", "object_type", OAAPropertyType.STRING)
    app.property_definitions.define_resource_property("Program", "product_code", OAAPropertyType.STRING)

    # ── Local Users ──────────────────────────────────────────────────────────
    user_ids: set = set()
    for row in data.get("users", []):
        uid = str(row.get("user_id", "")).strip()
        if not uid:
            continue
        email   = str(row.get("email", "")).strip()
        status  = str(row.get("status", "A")).strip().upper()
        abn     = str(row.get("address_book_number", "")).strip()

        identities = [email] if "@" in email else []
        user = app.add_local_user(name=uid, identities=identities)
        user.is_active = (status == "A")
        user.properties["jde_status"] = status
        if abn and abn not in ("0", ""):
            user.properties["address_book_number"] = abn
        user_ids.add(uid)

    log.info("Users added: %d", len(user_ids))

    # ── Local Roles ──────────────────────────────────────────────────────────
    role_ids: set = set()
    for row in data.get("roles", []):
        rid = str(row.get("role_id", "")).strip()
        if not rid:
            continue
        role = app.add_local_role(name=rid)
        rtype = str(row.get("role_type", "")).strip()
        if rtype:
            role.properties["role_type"] = rtype
        role_ids.add(rid)

    log.info("Roles added: %d", len(role_ids))

    # ── User → Role assignments ───────────────────────────────────────────────
    assignments = 0
    for row in data.get("user_roles", []):
        uid = str(row.get("user_id", "")).strip()
        rid = str(row.get("role_id", "")).strip()
        if not uid or not rid:
            continue
        if uid not in user_ids:
            log.debug("User-role skip: user %s not in users data", uid)
            continue
        if rid not in role_ids:
            # Role appeared in user_roles but not in roles table — auto-create
            app.add_local_role(name=rid)
            role_ids.add(rid)
            log.debug("Auto-created missing role: %s", rid)
        app.local_users[uid].add_role(role=rid, apply_to_application=True)
        assignments += 1

    log.info("User-role assignments: %d", assignments)

    # ── Program Resources ─────────────────────────────────────────────────────
    program_ids: set = set()
    for row in data.get("programs", []):
        pid   = str(row.get("program_id", "")).strip()
        if not pid:
            continue
        desc  = str(row.get("description", "")).strip() or pid
        otype = str(row.get("object_type", "")).strip()
        pcode = str(row.get("product_code", "")).strip()

        resource = app.add_resource(name=pid, resource_type="Program", description=desc)
        if otype:
            resource.properties["object_type"] = otype
        if pcode:
            resource.properties["product_code"] = pcode
        program_ids.add(pid)

    log.info("Program resources added: %d", len(program_ids))

    # ── Security → Permissions ────────────────────────────────────────────────
    perms_added = skipped = 0
    for row in data.get("security", []):
        pid     = str(row.get("program_id", "")).strip()
        subject = str(row.get("user_or_role", "")).strip()

        if not pid or not subject:
            continue
        if _is_yes(row.get("no_access")):
            log.debug("No-access record: %s on %s", subject, pid)
            continue
        if pid not in program_ids:
            log.debug("Security record for unknown program %s — skip", pid)
            skipped += 1
            continue

        is_user = subject in user_ids
        is_role = subject in role_ids

        if not is_user and not is_role:
            log.debug("Unknown security subject %s — skip", subject)
            skipped += 1
            continue

        allow_inquiry = _is_yes(row.get("allow_inquiry"))
        allow_add     = _is_yes(row.get("allow_add"))
        allow_change  = _is_yes(row.get("allow_change"))
        allow_delete  = _is_yes(row.get("allow_delete"))
        allow_run     = _is_yes(row.get("allow_run"))

        all_crud = allow_inquiry and allow_add and allow_change and allow_delete
        granted: List[str] = []
        if all_crud:
            granted.append("full_access")
        else:
            if allow_inquiry:
                granted.append("view")
            if allow_add:
                granted.append("add")
            if allow_change:
                granted.append("change")
            if allow_delete:
                granted.append("delete")
        if allow_run:
            granted.append("run")

        if not granted:
            continue

        resource = app.resources.get(pid)
        if resource is None:
            continue

        for perm in granted:
            if is_role:
                # oaaclient 1.1.x: role permissions are application-scoped
                app.local_roles[subject].add_permissions([perm])
            else:
                app.local_users[subject].add_permission(perm, resources=[resource])
        perms_added += 1

    log.info("Security records processed: %d  |  skipped: %d", perms_added, skipped)
    log.info(
        "Payload summary — Users: %d  Roles: %d  Programs: %d  SecurityRecords: %d",
        len(data.get("users", [])),
        len(data.get("roles", [])),
        len(data.get("programs", [])),
        len(data.get("security", [])),
    )
    return app


# ── Veza Push ─────────────────────────────────────────────────────────────────

def push_to_veza(
    veza_url: str,
    veza_api_key: str,
    provider_name: str,
    datasource_name: str,
    app: CustomApplication,
    dry_run: bool = False,
    save_json: bool = False,
) -> None:
    if save_json:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(script_dir, f"{datasource_name.replace(' ', '_')}_payload.json")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(app.get_payload(), fh, indent=2, default=str)
        log.info("Payload saved to %s", json_path)
        print(f"[JDE OAA] Payload saved → {json_path}")

    if dry_run:
        log.info("[DRY RUN] Payload built successfully — push to Veza skipped")
        _stage("Result: SUCCESS (dry-run complete)")
        return

    if not veza_url or not veza_api_key:
        log.error("VEZA_URL and VEZA_API_KEY are required for a live push")
        _stage("Result: FAILURE — missing VEZA_URL or VEZA_API_KEY")
        sys.exit(1)

    _stage("Pushing to Veza")
    veza_con = OAAClient(url=veza_url, token=veza_api_key)
    try:
        log.info("Pushing payload to Veza at %s", veza_url)
        response = veza_con.push_application(
            provider_name=provider_name,
            data_source_name=datasource_name,
            application_object=app,
            create_provider=True,
        )
        if response.get("warnings"):
            for w in response["warnings"]:
                log.warning("Veza warning: %s", w)
        log.info("Successfully pushed to Veza")
        _stage("Result: SUCCESS — payload pushed to Veza")
    except OAAClientError as exc:
        log.error("Veza push failed: %s — %s (HTTP %s)", exc.error, exc.message, exc.status_code)
        if hasattr(exc, "details"):
            for detail in exc.details:
                log.error("  Detail: %s", detail)
        _stage(f"Result: FAILURE — {exc.error}")
        sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="JD Edwards EnterpriseOne → Veza OAA Integration",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = parser.add_argument_group("Data source")
    src.add_argument("--data-dir",
        help="Directory containing CSV sample files — skips live DB connection when provided")
    src.add_argument("--env-file", default=".env",
        help="Path to .env credentials file")

    db = parser.add_argument_group("MS SQL Server connection (or set JDE_DB_* env vars)")
    db.add_argument("--mssql-server",   help="Hostname or IP of the SQL Server instance")
    db.add_argument("--mssql-port",     default="1433", help="TCP port")
    db.add_argument("--mssql-db",       help="JDE database name (e.g. JDE_PRODUCTION)")
    db.add_argument("--mssql-user",     help="SQL login username")
    db.add_argument("--mssql-password", help="SQL login password")
    db.add_argument("--jde-schema",     default="dbo", help="JDE table schema name")

    vz = parser.add_argument_group("Veza")
    vz.add_argument("--veza-url",     help="Veza instance URL (overrides VEZA_URL)")
    vz.add_argument("--veza-api-key", help="Veza API key (overrides VEZA_API_KEY)")
    vz.add_argument("--provider-name",    default="Oracle JDE",
        help="Provider name displayed in Veza")
    vz.add_argument("--datasource-name",  default="Oracle JDE EnterpriseOne",
        help="Datasource name displayed in Veza")

    run = parser.add_argument_group("Execution")
    run.add_argument("--dry-run",   action="store_true",
        help="Build the OAA payload without pushing to Veza")
    run.add_argument("--save-json", action="store_true",
        help="Save the OAA payload to a JSON file for inspection")
    run.add_argument("--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity")

    return parser.parse_args()


def main():
    args = parse_args()
    _setup_logging(args.log_level)

    print("=" * 60)
    print("  JD Edwards EnterpriseOne → Veza OAA Integration")
    print(f"  Provider:    {args.provider_name}")
    print(f"  Datasource:  {args.datasource_name}")
    print(f"  Mode:        {'DRY RUN' if args.dry_run else 'LIVE PUSH'}")
    print("=" * 60)

    _stage("Started")
    config = load_config(args)

    if args.data_dir:
        if not os.path.isdir(args.data_dir):
            log.error("--data-dir %s does not exist", args.data_dir)
            sys.exit(1)
        log.info("Loading data from CSV files in %s", args.data_dir)
        _stage("Loading CSV data")
        data = load_from_csv(args.data_dir)
    else:
        log.info("Loading data from JDE MS SQL Server database")
        data = load_from_db(config)

    _stage("Building OAA payload")
    app = build_oaa_payload(data, args.provider_name, args.datasource_name)

    push_to_veza(
        veza_url=config["veza_url"],
        veza_api_key=config["veza_api_key"],
        provider_name=args.provider_name,
        datasource_name=args.datasource_name,
        app=app,
        dry_run=args.dry_run,
        save_json=args.save_json,
    )


if __name__ == "__main__":
    main()
