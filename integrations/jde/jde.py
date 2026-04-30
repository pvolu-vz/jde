#!/usr/bin/env python3
"""
JD Edwards EnterpriseOne to Veza OAA Integration Script
Collects identity and permission data from JDE via MS SQL Server and pushes to Veza.

Entity model: Local Users → Local Roles → Program Resources (with Add/Change/Delete/View/Run)
Data sources: F0092L, F95921, F00950, F0101, F01151, F98OWSEC, F00926, F9312
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
        RTRIM(u.ULUSER)   AS user_id,
        RTRIM(u.ULLUSER)  AS email,
        RTRIM(a.ABALPH)   AS full_name,
        a.ABAN8           AS address_book_number,
        RTRIM(a.ABTAX)    AS employee_id,
        RTRIM(a.ABAT1)    AS ab_type
    FROM {schema}.F0092L u
    LEFT JOIN {schema}.F0101 a ON RTRIM(u.ULUSER) COLLATE DATABASE_DEFAULT = RTRIM(a.ABALKY) COLLATE DATABASE_DEFAULT
    WHERE LTRIM(RTRIM(u.ULUSER)) NOT LIKE '#%'
      AND LTRIM(RTRIM(u.ULUSER)) NOT LIKE 'TRAIN%'
      AND LTRIM(RTRIM(u.ULUSER)) NOT LIKE 'JDETST_%'
      AND u.ULUSER NOT IN ('_LDAPDEFLT', '!JDE')
"""

_SQL_ROLES = """
    SELECT DISTINCT
        RTRIM(r.RLFRROLE) AS role_id
    FROM {schema}.F95921 r
    WHERE r.RLFRROLE IS NOT NULL
      AND RTRIM(r.RLFRROLE) != ''
"""

_SQL_USER_ROLES = """
    SELECT
        RTRIM(r.RLTOROLE) AS user_id,
        RTRIM(r.RLFRROLE) AS role_id,
        r.RLEFFDATE       AS role_effective_date,
        r.RLEXPIRDATE     AS role_expiry_date
    FROM {schema}.F95921 r
    WHERE r.RLFRROLE IS NOT NULL AND r.RLTOROLE IS NOT NULL
      AND RTRIM(r.RLFRROLE) != '' AND RTRIM(r.RLTOROLE) != ''
"""

_SQL_PROGRAMS = """
    SELECT DISTINCT
        RTRIM(s.FSOBNM)  AS program_id,
        ''               AS description,
        ''               AS object_type,
        RTRIM(s.FSSY)    AS product_code
    FROM {schema}.F00950 s
    WHERE s.FSOBNM IS NOT NULL
      AND RTRIM(s.FSOBNM) != ''
"""

_SQL_USER_SECURITY = """
    SELECT
        RTRIM(s.SCUSER)    AS user_id,
        RTRIM(s.SCSECTPE)  AS security_type,
        RTRIM(s.SCUGRP)    AS user_group,
        s.SCATTEMPTS       AS login_attempts,
        s.SCSECLST         AS last_security_date
    FROM {schema}.F98OWSEC s
    WHERE s.SCUSER IS NOT NULL
      AND RTRIM(s.SCUSER) != ''
"""


_SQL_USER_STATUS = """
    SELECT
        RTRIM(u.AUUSER)        AS user_id,
        RTRIM(u.AUACTINACT)    AS status_flag,
        u.AUUPMJ               AS last_update_julian
    FROM {schema}.F00926 u
    WHERE u.AUUSER IS NOT NULL
      AND RTRIM(u.AUUSER) != ''
"""

_SQL_EMAIL = """
    SELECT
        RTRIM(e.EAUSER) AS user_id,
        RTRIM(e.EAEMAL) AS email
    FROM {schema}.F01151 e
    WHERE e.EAUSER IS NOT NULL
      AND RTRIM(e.EAUSER) != ''
      AND RTRIM(COALESCE(e.EAEMAL, '')) != ''
"""

_SQL_LAST_ACCESS = """
    SELECT
        RTRIM(s.SHUSER)  AS user_id,
        MAX(s.SHUPMJ)    AS last_access_julian
    FROM {schema}.F9312 s
    WHERE s.SHEVTYP = '01'
      AND s.SHUSER IS NOT NULL
      AND RTRIM(s.SHUSER) != ''
    GROUP BY RTRIM(s.SHUSER)
"""

_SQL_SECURITY = """
    SELECT
        RTRIM(s.FSOBNM)  AS program_id,
        RTRIM(s.FSUSER)  AS user_or_role,
        RTRIM(s.FSSY)    AS product_code,
        s.FSA            AS allow_add,
        s.FSCHNG         AS allow_change,
        s.FSDLT          AS allow_delete,
        s.FSIOK          AS allow_inquiry,
        s.FSRUN          AS allow_run
    FROM {schema}.F00950 s
    WHERE s.FSOBNM IS NOT NULL
      AND RTRIM(s.FSOBNM) != ''
      AND RTRIM(COALESCE(s.FSUSER, '')) NOT IN ('', '*PUBLIC', 'EVERYONE')
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
        "users":         _apply_schema(_SQL_USERS, schema),
        "roles":         _apply_schema(_SQL_ROLES, schema),
        "user_roles":    _apply_schema(_SQL_USER_ROLES, schema),
        "programs":      _apply_schema(_SQL_PROGRAMS, schema),
        "user_security": _apply_schema(_SQL_USER_SECURITY, schema),
        "security":      _apply_schema(_SQL_SECURITY, schema),
        "user_status":   _apply_schema(_SQL_USER_STATUS, schema),
        "emails":        _apply_schema(_SQL_EMAIL, schema),
        "last_access":   _apply_schema(_SQL_LAST_ACCESS, schema),
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
    """Load data from JDE CSV exports (used for dry-run testing).

    Reads files by their JDE table names and derives the data keys
    the payload builder expects.
    """
    data: dict = {k: [] for k in [
        "users", "roles", "user_roles", "programs", "security",
        "user_status", "emails", "last_access",
    ]}

    f0092l = os.path.join(data_dir, "F0092L.csv")
    if os.path.exists(f0092l):
        with open(f0092l, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                uid   = row.get("ULUSER", "").strip()
                email = row.get("ULLUSER", "").strip()
                if uid:
                    data["users"].append({"user_id": uid, "email": email, "display_name": email})
        log.info("Loaded %d users from F0092L.csv", len(data["users"]))
    else:
        log.warning("F0092L.csv not found in %s — users will be empty", data_dir)

    f95921 = os.path.join(data_dir, "F95921.csv")
    if os.path.exists(f95921):
        role_ids_seen: set = set()
        with open(f95921, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                from_role = row.get("RLFRROLE", "").strip()
                to_user   = row.get("RLTOROLE", "").strip()
                if from_role and from_role not in role_ids_seen:
                    data["roles"].append({"role_id": from_role})
                    role_ids_seen.add(from_role)
                if from_role and to_user:
                    data["user_roles"].append({
                        "user_id":              to_user,
                        "role_id":              from_role,
                        "role_effective_date":  row.get("RLEFFDATE", ""),
                        "role_expiry_date":     row.get("RLEXPIRDATE", ""),
                    })
        log.info("Loaded %d roles and %d user-role assignments from F95921.csv",
                 len(data["roles"]), len(data["user_roles"]))
    else:
        log.warning("F95921.csv not found in %s — roles will be empty", data_dir)

    f00950 = os.path.join(data_dir, "F00950.csv")
    if os.path.exists(f00950):
        program_ids_seen: set = set()
        with open(f00950, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pid          = row.get("FSOBNM", "").strip()
                user_or_role = row.get("FSUSER", "").strip()
                if pid and pid not in program_ids_seen:
                    data["programs"].append({
                        "program_id":   pid,
                        "description":  "",
                        "object_type":  "",
                        "product_code": row.get("FSSY", "").strip(),
                    })
                    program_ids_seen.add(pid)
                if pid and user_or_role:
                    data["security"].append({
                        "program_id":   pid,
                        "user_or_role": user_or_role,
                        "product_code": row.get("FSSY", "").strip(),
                        "allow_add":    row.get("FSA", ""),
                        "allow_change": row.get("FSCHNG", ""),
                        "allow_delete": row.get("FSDLT", ""),
                        "allow_inquiry":row.get("FSIOK", ""),
                        "allow_run":    row.get("FSRUN", ""),
                    })
        log.info("Loaded %d programs and %d security records from F00950.csv",
                 len(data["programs"]), len(data["security"]))
    else:
        log.warning("F00950.csv not found in %s — programs and security will be empty", data_dir)

    f00926 = os.path.join(data_dir, "F00926.csv")
    if os.path.exists(f00926):
        with open(f00926, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                uid = row.get("AUUSER", "").strip()
                if uid:
                    data["user_status"].append({
                        "user_id":            uid,
                        "status_flag":        row.get("AUACTINACT", "").strip(),
                        "last_update_julian": row.get("AUUPMJ", ""),
                    })
        log.info("Loaded %d user-status records from F00926.csv", len(data["user_status"]))
    else:
        log.warning("F00926.csv not found in %s — user status will default to active", data_dir)

    f01151 = os.path.join(data_dir, "F01151.csv")
    if os.path.exists(f01151):
        with open(f01151, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                uid   = row.get("EAUSER", "").strip()
                email = row.get("EAEMAL", "").strip()
                if uid and "@" in email:
                    data["emails"].append({"user_id": uid, "email": email})
        log.info("Loaded %d email records from F01151.csv", len(data["emails"]))
    else:
        log.warning("F01151.csv not found in %s — emails will come from F0092L only", data_dir)

    f9312 = os.path.join(data_dir, "F9312.csv")
    if os.path.exists(f9312):
        last_access_map: dict = {}
        with open(f9312, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                uid      = row.get("SHUSER", "").strip()
                evt_type = row.get("SHEVTYP", "").strip()
                julian   = row.get("SHUPMJ", "")
                if uid and evt_type == "01":
                    try:
                        val = int(julian)
                    except (TypeError, ValueError):
                        val = 0
                    if val > last_access_map.get(uid, 0):
                        last_access_map[uid] = val
        data["last_access"] = [
            {"user_id": uid, "last_access_julian": julian}
            for uid, julian in last_access_map.items()
        ]
        log.info("Loaded %d last-access records from F9312.csv", len(data["last_access"]))
    else:
        log.warning("F9312.csv not found in %s — last_access_date will not be set", data_dir)

    return data


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_yes(value) -> bool:
    """Return True when a JDE security flag field is set to Y/1/TRUE."""
    return str(value).strip().upper() in ("Y", "1", "TRUE", "YES") if value is not None else False


def _jde_julian_to_date(jde_int) -> Optional[str]:
    """Convert a JDE Julian date integer (CYYDDD) to an ISO-8601 date string.

    JDE Julian format: C = century offset (0→19xx, 1→20xx), YY = 2-digit year,
    DDD = day of year.  A value of 0 means 'no date'.
    """
    from datetime import date, timedelta
    try:
        val = int(jde_int)
    except (TypeError, ValueError):
        return None
    if val == 0:
        return None
    s = str(val).zfill(6)
    year = (int(s[0]) + 19) * 100 + int(s[1:3])
    day_of_year = int(s[3:6])
    try:
        return (date(year, 1, 1) + timedelta(days=day_of_year - 1)).isoformat()
    except (ValueError, OverflowError):
        return None


def _role_category(role_id: str) -> str:
    """Classify a JDE role into a human-readable category based on its prefix."""
    r = role_id.upper()
    if r.startswith("RT9"):
        return "PPD"
    if r.startswith("RT") or r.startswith("AD"):
        return "RT/AD"
    if r.startswith("BU"):
        return "Business Unit"
    if r.startswith("PR"):
        return "Printer"
    return "Other"


# ── OAA Payload Builder ───────────────────────────────────────────────────────

def build_oaa_payload(data: dict, provider_name: str, datasource_name: str) -> CustomApplication:
    """Assemble a Veza OAA CustomApplication from JDE identity and permission data."""
    app = CustomApplication(name=datasource_name, application_type=provider_name)

    for perm_name, oaa_types in JDE_PERMISSIONS.items():
        app.add_custom_permission(perm_name, oaa_types)

    # Custom properties — users
    app.property_definitions.define_local_user_property("jde_status",          OAAPropertyType.STRING)
    app.property_definitions.define_local_user_property("address_book_number",  OAAPropertyType.STRING)
    app.property_definitions.define_local_user_property("employee_id",          OAAPropertyType.STRING)
    app.property_definitions.define_local_user_property("ab_type",              OAAPropertyType.STRING)
    app.property_definitions.define_local_user_property("disabled_date",        OAAPropertyType.STRING)
    app.property_definitions.define_local_user_property("last_access_date",     OAAPropertyType.STRING)
    # Custom properties — roles
    app.property_definitions.define_local_role_property("role_type",            OAAPropertyType.STRING)
    app.property_definitions.define_local_role_property("role_category",        OAAPropertyType.STRING)
    app.property_definitions.define_local_role_property("effective_date",       OAAPropertyType.STRING)
    app.property_definitions.define_local_role_property("expiry_date",          OAAPropertyType.STRING)
    # Custom properties — resources
    app.property_definitions.define_resource_property("Program", "object_type", OAAPropertyType.STRING)
    app.property_definitions.define_resource_property("Program", "product_code", OAAPropertyType.STRING)

    # Build lookup dicts for enrichment data
    status_by_user: dict = {
        str(r.get("user_id", "")).strip().upper(): r
        for r in data.get("user_status", [])
    }
    email_by_user: dict = {
        str(r.get("user_id", "")).strip().upper(): str(r.get("email", "")).strip()
        for r in data.get("emails", [])
        if "@" in str(r.get("email", ""))
    }
    last_access_by_user: dict = {
        str(r.get("user_id", "")).strip().upper(): r.get("last_access_julian")
        for r in data.get("last_access", [])
    }

    # ── Local Users ──────────────────────────────────────────────────────────
    user_ids: set = set()
    for row in data.get("users", []):
        uid = str(row.get("user_id", "")).strip().upper()
        if not uid or uid in user_ids:
            continue

        # Email: prefer F01151 verified address over F0092L ULLUSER
        email_f01151 = email_by_user.get(uid, "")
        email_f0092l = str(row.get("email", "")).strip()
        email = email_f01151 if email_f01151 else email_f0092l

        # Status from F00926
        status_row  = status_by_user.get(uid, {})
        status_flag = str(status_row.get("status_flag", "")).strip().upper()
        is_inactive = (status_flag == "I")

        abn = str(row.get("address_book_number", "")).strip()

        identities = [email] if "@" in email else []
        user = app.add_local_user(name=uid, identities=identities)
        user.is_active = not is_inactive
        user.set_property("jde_status", "Inactive" if is_inactive else "Active")

        if is_inactive:
            disabled_date = _jde_julian_to_date(status_row.get("last_update_julian"))
            if disabled_date:
                user.set_property("disabled_date", disabled_date)

        if abn and abn not in ("0", ""):
            user.set_property("address_book_number", abn)

        emp_id = str(row.get("employee_id", "")).strip()
        if emp_id:
            user.set_property("employee_id", emp_id)

        ab_type = str(row.get("ab_type", "")).strip()
        if ab_type:
            user.set_property("ab_type", ab_type)

        last_access_julian = last_access_by_user.get(uid)
        if last_access_julian:
            last_access_date = _jde_julian_to_date(last_access_julian)
            if last_access_date:
                user.set_property("last_access_date", last_access_date)

        user_ids.add(uid)

    log.info("Users added: %d", len(user_ids))

    # ── Local Roles ──────────────────────────────────────────────────────────
    role_ids: set = set()
    for row in data.get("roles", []):
        rid = str(row.get("role_id", "")).strip().upper()
        if not rid or rid in role_ids:
            continue
        role = app.add_local_role(name=rid)
        rtype = str(row.get("role_type", "")).strip()
        if rtype:
            role.set_property("role_type", rtype)
        role.set_property("role_category", _role_category(rid))
        role_ids.add(rid)

    log.info("Roles added: %d", len(role_ids))

    # ── Program Resources ─────────────────────────────────────────────────────
    program_ids: set = set()
    for row in data.get("programs", []):
        pid   = str(row.get("program_id", "")).strip().upper()
        if not pid or pid in program_ids:
            continue
        desc  = str(row.get("description", "")).strip() or pid
        otype = str(row.get("object_type", "")).strip()
        pcode = str(row.get("product_code", "")).strip()

        resource = app.add_resource(name=pid, resource_type="Program", description=desc)
        if otype:
            resource.set_property("object_type", otype)
        if pcode:
            resource.set_property("product_code", pcode)
        program_ids.add(pid)

    log.info("Program resources added: %d", len(program_ids))

    # ── Security → Permissions ────────────────────────────────────────────────
    # role_resources: role_id -> {pid: resource} — used to scope role assignments
    # role_perms:     role_id -> set of perm strings — applied via add_permissions()
    role_resources: dict = {}
    role_perms: dict = {}
    perms_added = skipped = 0

    for row in data.get("security", []):
        pid     = str(row.get("program_id", "")).strip().upper()
        subject = str(row.get("user_or_role", "")).strip().upper()

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

        if is_role:
            role_resources.setdefault(subject, {})[pid] = resource
            role_perms.setdefault(subject, set()).update(granted)
        else:
            for perm in granted:
                app.local_users[subject].add_permission(perm, resources=[resource])
        perms_added += 1

    # Apply accumulated role-level permissions (defines what each role can do)
    for rid, perms in role_perms.items():
        app.local_roles[rid].add_permissions(list(perms))

    log.info("Security records processed: %d  |  skipped: %d", perms_added, skipped)

    # ── User → Role assignments ───────────────────────────────────────────────
    # Scope each role assignment to the programs that role grants access to,
    # so Veza renders: User → Role → Program → Application.
    # Role effective/expiry dates are stored on the role object (last-write wins
    # for shared roles — dates are typically consistent across assignments).
    assignments = 0
    for row in data.get("user_roles", []):
        uid = str(row.get("user_id", "")).strip().upper()
        rid = str(row.get("role_id", "")).strip().upper()
        if not uid or not rid:
            continue
        if uid not in user_ids:
            log.debug("User-role skip: user %s not in users data", uid)
            continue
        if rid not in role_ids:
            # Role appeared in user_roles but not in roles table — auto-create
            role_obj = app.add_local_role(name=rid)
            role_obj.set_property("role_category", _role_category(rid))
            role_ids.add(rid)
            log.debug("Auto-created missing role: %s", rid)

        # Propagate effective/expiry dates onto the role object
        role_obj = app.local_roles[rid]
        eff_date = _jde_julian_to_date(row.get("role_effective_date"))
        exp_date = _jde_julian_to_date(row.get("role_expiry_date"))
        if eff_date:
            role_obj.set_property("effective_date", eff_date)
        if exp_date:
            role_obj.set_property("expiry_date", exp_date)

        resources_for_role = list(role_resources.get(rid, {}).values())
        if resources_for_role:
            app.local_users[uid].add_role(role=rid, resources=resources_for_role)
        else:
            app.local_users[uid].add_role(role=rid, apply_to_application=True)
        assignments += 1

    log.info("User-role assignments: %d", assignments)
    log.info(
        "Payload summary — Users: %d  Roles: %d  Programs: %d  SecurityRecords: %d",
        len(user_ids),
        len(role_ids),
        len(program_ids),
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
