"""Databricks CLI JSON -> snapshot items (provider kind status: id, label, role, text, plus addon fields).
Pure functions; nothing here runs a command."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

FAILED_RESULTS = frozenset({"FAILED", "TIMEDOUT", "INTERNAL_ERROR", "UPSTREAM_FAILED"})
FAILED_TYPES = frozenset({"INTERNAL_ERROR", "CLIENT_ERROR", "CLOUD_FAILURE"})
RUNNING_STATES = frozenset({"PENDING", "QUEUED", "RUNNING", "BLOCKED", "TERMINATING", "WAITING"})
ACTIVE_UPDATES = frozenset({"QUEUED", "CREATED", "WAITING_FOR_RESOURCES", "INITIALIZING", "RESETTING",
                            "SETTING_UP_TABLES", "RUNNING", "STOPPING"})
DEDICATED = frozenset({"SINGLE_USER", "DATA_SECURITY_MODE_DEDICATED"})
STUCK_AFTER = timedelta(hours=2)
RUN_ROLE = {"failed": "err", "running": "info", "succeeded": "ok", "other": "neu"}
_DBR = re.compile(r"(\d+\.\d+)")
_LOCK = re.compile(r'name = "databricks-connect"\s*\nversion = "([^"]+)"')
_REQ = re.compile(r"databricks-connect\s*(?:\[[^\]]*\])?\s*(?:==|~=|>=)\s*([0-9][0-9.]*)")


def _ms(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat() if value else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _iso(value) -> str | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else None


def _https(value) -> str | None:
    return value if isinstance(value, str) and value.startswith("https://") and not any(c.isspace() for c in value) else None


def _name(value, fallback: str) -> str:
    return (" ".join(str(value or "").split()) or fallback)[:200]


def _with_url(item: dict, url) -> dict:
    url = _https(url)
    if url:
        item["url"] = url
    return item


def run_state(run: dict) -> str:
    state = run.get("state") or {}
    status = run.get("status") or {}
    details = status.get("termination_details") or {}
    life = str(status.get("state") or state.get("life_cycle_state") or "").upper()
    result = str(state.get("result_state") or "").upper()
    if life in RUNNING_STATES:
        return "running"
    if result in FAILED_RESULTS or str(details.get("type") or "").upper() in FAILED_TYPES or life == "INTERNAL_ERROR":
        return "failed"
    if result == "SUCCESS" or str(details.get("code") or "").upper() == "SUCCESS":
        return "succeeded"
    return "other"


def run_item(env: str, run: dict, host: str | None) -> dict:
    """The run's link is built from the pinned host and the ids, like pipeline links; the CLI's run_page_url
    is not used."""
    state = run_state(run)
    run_id = str(run.get("run_id") or "")
    job_id = str(run.get("job_id") or "")
    name = _name(run.get("run_name"), f"run {run_id}")
    item = {"id": f"run:{run_id}", "type": "run", "env": env, "label": name, "name": name, "role": RUN_ROLE[state],
            "text": state, "state": state, "run_id": run_id, "job_id": job_id,
            "owners": [run["creator_user_name"]] if run.get("creator_user_name") else [],
            "started_at": _ms(run.get("start_time")), "ended_at": _ms(run.get("end_time")),
            "message": str((run.get("state") or {}).get("state_message") or "")[:300]}
    ids = run_id.isdigit() and job_id.isdigit()
    return _with_url(item, f"{host}/#job/{job_id}/run/{run_id}" if host and ids else None)


def pipeline_item(env: str, pipeline: dict, host: str | None, now: datetime) -> dict:
    updates = pipeline.get("latest_updates") or []
    last = updates[0] if updates and isinstance(updates[0], dict) else {}
    ustate = str(last.get("state") or "").upper()
    at = _iso(last.get("creation_time"))
    if ustate == "FAILED":
        role, text = "err", "last update failed"
    elif ustate in ACTIVE_UPDATES:
        started = datetime.fromisoformat(at) if at else None
        if started and now - started > STUCK_AFTER:
            role, text = "warn", f"running for {int((now - started).total_seconds() // 3600)} h"
        else:
            role, text = "info", "running"
    elif ustate == "COMPLETED":
        role, text = "ok", "last update completed"
    elif ustate == "CANCELED":
        role, text = "neu", "last update canceled"
    else:
        role, text = "neu", str(pipeline.get("state") or "idle").lower()
    pid = str(pipeline.get("pipeline_id") or "")
    name = _name(pipeline.get("name"), pid or "pipeline")
    owners = [o for o in (pipeline.get("creator_user_name"), pipeline.get("run_as_user_name")) if o]
    item = {"id": f"pipeline:{pid}", "type": "pipeline", "env": env, "label": name, "name": name, "role": role,
            "text": text, "pipeline_id": pid, "owners": owners, "last_update_at": at}
    return _with_url(item, f"{host}/pipelines/{pid}" if host and pid else None)


def warehouse_item(env: str, warehouse: dict) -> dict:
    wid = str(warehouse.get("id") or "")
    state = str(warehouse.get("state") or "unknown").upper()
    name = _name(warehouse.get("name"), wid or "warehouse")
    path = (warehouse.get("odbc_params") or {}).get("path") or (f"/sql/1.0/warehouses/{wid}" if wid else "")
    return {"id": f"warehouse:{wid}", "type": "warehouse", "env": env, "label": name, "name": name,
            "role": "ok" if state == "RUNNING" else "neu", "text": state.lower(), "access": "can_use",
            "reason": "listed by warehouses list", "http_path": str(path)[:200], "size": warehouse.get("cluster_size"),
            "auto_stop": warehouse.get("auto_stop_mins"),
            "owners": [warehouse["creator_name"]] if warehouse.get("creator_name") else []}


def cluster_access(cluster: dict, me: str | None) -> tuple[str, str]:
    """Three-state access (spec v2 §15). Dedicated clusters of others are no access; a shared cluster someone
    else created is unknown, because the addon does not read permissions."""
    def mine(value) -> bool:
        return bool(me) and str(value or "").lower() == me.lower()

    mode = str(cluster.get("data_security_mode") or "").upper()
    if mode in DEDICATED:
        return ("can_use", "dedicated to you") if mine(cluster.get("single_user_name")) else ("no_access", "dedicated to another user")
    if mine(cluster.get("creator_user_name")):
        return "can_use", "you created it"
    return "unknown", "shared cluster; its permissions are not read"


def dbr(spark_version) -> str | None:
    m = _DBR.match(str(spark_version or ""))
    return m.group(1) if m else None


def cluster_item(env: str, cluster: dict, me: str | None) -> dict:
    cid = str(cluster.get("cluster_id") or "")
    state = str(cluster.get("state") or "unknown").upper()
    access, reason = cluster_access(cluster, me)
    name = _name(cluster.get("cluster_name"), cid or "cluster")
    return {"id": f"cluster:{cid}", "type": "cluster", "env": env, "label": name, "name": name,
            "role": "ok" if state == "RUNNING" else "neu", "text": state.lower(), "access": access, "reason": reason,
            "dbr": dbr(cluster.get("spark_version")), "auto_stop": cluster.get("autotermination_minutes"),
            "cluster_id": cid, "owners": [cluster["creator_user_name"]] if cluster.get("creator_user_name") else []}


def env_item(env, me: str | None, connect_version: str | None) -> dict:
    return {"id": f"env:{env.name}", "type": "env", "env": env.name, "label": env.name, "role": "ok",
            "text": f"signed in as {me or 'unknown'}", "profile": env.profile, "host": env.host, "me": me,
            "connect_version": connect_version}


def problem_item(section: str, message: str) -> dict:
    return {"id": f"problem:{section}", "type": "problem", "label": section, "role": "err", "text": str(message)[:300]}


def local_connect_version(folders) -> str | None:
    """The databricks-connect version the local project pins (uv.lock, pyproject.toml or requirements.txt)."""
    for folder in folders:
        for name, pattern in (("uv.lock", _LOCK), ("pyproject.toml", _REQ), ("requirements.txt", _REQ)):
            try:
                text = (Path(folder) / name).read_text(encoding="utf-8")[:2_000_000]
            except (OSError, UnicodeDecodeError):
                continue
            m = pattern.search(text)
            if m:
                return m.group(1)
    return None
