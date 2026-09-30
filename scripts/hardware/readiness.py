#!/usr/bin/env python3
"""Read-only, secret-free readiness receipt for a Cockpit LIVE session.

The module deliberately has a small injectable command boundary.  Unit tests
pass a fake ``command`` callable; production uses only read-only commands.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

READY, NOT_READY, UNKNOWN = "READY", "NOT_READY", "UNKNOWN"
_SECRET = re.compile(r"(?:\b(?:ki|opc|imsi|password|token|secret)\b\s*[=:]|bearer\s+)", re.I)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Command = Callable[[Sequence[str]], CommandResult]
TcpProbe = Callable[[str, int], bool]


def system_command(argv: Sequence[str]) -> CommandResult:
    try:
        done = subprocess.run(argv, text=True, capture_output=True, timeout=12, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(127, "", str(exc))
    return CommandResult(done.returncode, done.stdout, done.stderr)


def system_tcp_probe(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


@dataclass
class Settings:
    binding_path: str = "deployment/assurance-live-binding.1.0.0.json"
    live_artifact_root: str = ""
    witness_path: str = "/run/ai-ran/flexric-connection-witness.json"
    jsonl_max_age_s: int = 120
    ue_hosts: Mapping[str, str] = field(default_factory=dict)
    ue_prime_target: str = ""
    ext_dn_container: str = ""
    kpm_ue_attribution_lines: int = 64
    gnb1_running_conf: str = ""
    required_ues: frozenset[str] = frozenset(("ue1",))
    gnb: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @classmethod
    def from_environment(cls) -> "Settings":
        hosts = {name: os.environ.get(key, "") for name, key in
                 (("ue1", "HW_UE1_HOST"), ("ue2", "HW_UE2_HOST"))}
        return cls(
            binding_path=os.environ.get("HW_ASSURANCE_BINDING", cls.binding_path),
            live_artifact_root=(os.environ.get("HW_LIVE_ARTIFACT_ROOT")
                                or os.environ.get("LOWER_LIVE", "")),
            witness_path=os.environ.get("HW_FLEXRIC_WITNESS", cls.witness_path),
            jsonl_max_age_s=int(os.environ.get("HW_KPM_MAX_AGE_SECONDS", "120")),
            ue_hosts=hosts,
            ue_prime_target=os.environ.get("HW_UE_PRIME_TARGET", ""),
            ext_dn_container=os.environ.get("HW_EXT_DN_CONTAINER", os.environ.get("HW_EXT_DN", "")),
            kpm_ue_attribution_lines=int(os.environ.get("HW_KPM_UE_ATTRIBUTION_LINES", "64")),
            gnb1_running_conf=os.environ.get("HW_GNB1_RUNNING_CONF", ""),
            required_ues=frozenset(filter(None, os.environ.get("HW_REQUIRED_UES", "ue1").split(","))),
            gnb={
                "gnb1": {"host": os.environ.get("HW_GNB1_HOST", ""),
                         "log": os.environ.get("HW_GNB1_LOG", ""),
                         "process": os.environ.get("HW_GNB1_PROCESS", "nr-softmodem")},
                "gnb2": {"host": os.environ.get("HW_GNB2_HOST", ""),
                         "log": os.environ.get("HW_GNB2_LOG", ""),
                         "process": os.environ.get("HW_GNB2_PROCESS", "nr-softmodem"), "exact": "true"},
            },
        )


def _load_hardware_environment() -> None:
    """Apply the same non-secret environment defaults as the shell wrappers."""
    hardware = Path(__file__).resolve().parent
    example = hardware / "env.sh.example"
    env_file = hardware / "env.sh"
    source_files = [str(env_file)] if env_file.is_file() else [str(example)]
    result = subprocess.run(["bash", "-c", 'for file in "$@"; do source "$file"; done; env -0', "--", *source_files],
                            text=False, capture_output=True, check=False, env=os.environ.copy())
    if result.returncode:
        return
    for entry in result.stdout.split(b"\0"):
        if b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        try:
            name, text = key.decode("utf-8"), value.decode("utf-8")
            if not os.environ.get(name):
                os.environ[name] = text
        except UnicodeDecodeError:
            continue


def _item(state: str, reason: str, *, required: bool = True, **observed: Any) -> dict[str, Any]:
    return {"state": state, "reason": reason, "required": required,
            "observed": _secret_free(observed)}


def _secret_free(value: Any) -> Any:
    """Remove values which could turn a diagnostic into a credential receipt."""
    if isinstance(value, Mapping):
        return {str(k): _secret_free(v) for k, v in value.items()
                if str(k) == "secretFree" or not re.search(
                    r"(?:password|token|secret|private.?key|\bki\b|\bopc\b|\bimsi\b)", str(k), re.I)}
    if isinstance(value, list):
        return [_secret_free(v) for v in value]
    if isinstance(value, str) and _SECRET.search(value):
        return "[redacted]"
    return value


def _json(command: Command, argv: Sequence[str]) -> tuple[Any | None, str | None]:
    result = command(argv)
    if result.returncode:
        return None, (result.stderr or "command failed").strip().splitlines()[-1][:180]
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError:
        return None, "command returned malformed JSON"


def _container(command: Command, name: str) -> tuple[dict[str, Any] | None, str | None]:
    document, error = _json(command, ["docker", "inspect", name])
    if error:
        return None, error
    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
        return None, "docker inspect did not return one container"
    return document[0], None


def _env(container: Mapping[str, Any]) -> dict[str, str]:
    entries = container.get("Config", {}).get("Env", [])
    return {entry.split("=", 1)[0]: entry.split("=", 1)[1]
            for entry in entries if isinstance(entry, str) and "=" in entry}


def _running(container: Mapping[str, Any]) -> bool:
    return bool(container.get("State", {}).get("Running"))


def _witness_epochs(witness: Mapping[str, Any]) -> tuple[dict[str, int], str | None]:
    epochs: dict[str, int] = {}
    for connection in witness.get("connections", []):
        node = connection.get("globalE2NodeId", {}) if isinstance(connection, Mapping) else {}
        nb = node.get("nbId")
        epoch = connection.get("connectionEpoch") if isinstance(connection, Mapping) else None
        if connection.get("active") is True and isinstance(nb, int) and isinstance(epoch, int):
            epochs[f"0x{nb:08x}"] = epoch
    missing = [node for node in ("0x00000e00", "0x00000b00") if node not in epochs]
    return epochs, ("missing active " + ", ".join(missing)) if missing else None


def _host_pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # EPERM confirms that a host PID exists but is not ours.
    except OSError:
        return False
    return True


def _last_jsonl_age(path: str, now: float) -> tuple[float | None, str | None]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        if not lines:
            return None, "JSONL is empty"
        record = json.loads(lines[-1])
        timestamp = record.get("timestamp") or record.get("at") or record.get("observedAt")
        if isinstance(timestamp, str):
            age = now - datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        else:
            age = now - Path(path).stat().st_mtime
        return max(0.0, age), None
    except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        return None, f"cannot read JSONL last record: {exc}"[:180]


def _kpm_ue_attribution(path: str, limit: int) -> tuple[int | None, str | None]:
    """Count fresh KPM records that identify one or more UEs, read-only."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()[-limit:]
        count = 0
        for line in lines:
            record = json.loads(line)
            if record.get("event") == "kpm_indication" and isinstance(record.get("ues"), list) and record["ues"]:
                count += 1
        return count, None
    except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        return None, f"cannot read KPM UE attribution: {exc}"[:180]


def _host_path(container: Mapping[str, Any], container_path: str) -> str | None:
    for mount in container.get("Mounts", []):
        destination, source = mount.get("Destination"), mount.get("Source")
        if isinstance(destination, str) and isinstance(source, str) and container_path.startswith(destination.rstrip("/") + "/"):
            return source.rstrip("/") + container_path[len(destination):]
        if destination == container_path:
            return source
    return None


def _read_json(path: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)[:180]
    return (payload, None) if isinstance(payload, dict) else (None, "JSON root is not an object")


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _binding_epochs(binding: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for node, epoch in binding.kpm_expected_epochs.items():
        match = re.search(r"nb=(\d+)", node)
        if match:
            result[f"0x{int(match.group(1)):08x}"] = epoch
    return result


def _rfc3339_z(value: Any) -> str | None:
    """Docker's StartedAt as an unambiguous --since timestamp."""
    if not isinstance(value, str) or not value:
        return None
    if value.endswith("Z"):
        return value
    if value.endswith("+00:00"):
        return value[:-6] + "Z"
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _host_command(host: str, argv: Sequence[str]) -> list[str]:
    """Preserve argument boundaries when an SSH remote shell receives a probe."""
    if host in ("", "localhost", "127.0.0.1"):
        return list(argv)
    return ["ssh", "-o", "BatchMode=yes", host, shlex.join(argv)]


def _sqlite_timestamp(value: Any) -> float | None:
    """Accept the producer's ISO-8601 or Unix-seconds command timestamps."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


def _sqlite_summary(path: str, now: float) -> tuple[dict[str, Any] | None, str | None]:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        if "policies" not in tables:
            return None, "schema has no policies table"
        columns = {row[1] for row in connection.execute("pragma table_info(policies)")}
        required = {"policy_id", "policy_json", "status_json"}
        if not required <= columns:
            return None, "policies schema lacks " + ", ".join(sorted(required - columns))
        occupants: dict[str, list[dict[str, Any]]] = {}
        for row in connection.execute("select policy_id, policy_json, status_json from policies"):
            try:
                policy, status = json.loads(row[1]), json.loads(row[2]) if row[2] else {}
                ue = str(policy["scope"]["ueId"]["guAmfUeNgapId"]["amfUeNgapId"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            aic = status.get("aicStatus", {}) if isinstance(status, dict) else {}
            occupants.setdefault(ue, []).append({"policyId": row[0], "episodeState": aic.get("episodeState"),
                                                  "enforceStatus": status.get("enforceStatus") if isinstance(status, dict) else None})
        stale_commands: list[dict[str, Any]] = []
        command_error = None
        if "commands" not in tables:
            command_error = "schema has no commands table"
        else:
            command_columns = {row[1] for row in connection.execute("pragma table_info(commands)")}
            required_command_columns = {"state", "created_at", "updated_at"}
            if not required_command_columns <= command_columns:
                command_error = "commands schema lacks " + ", ".join(sorted(required_command_columns - command_columns))
            else:
                query = ("select sequence, state, created_at, updated_at from commands "
                         "where state in ('RECOVERY', 'SENT')")
                for row in connection.execute(query):
                    timestamp = _sqlite_timestamp(row[3]) or _sqlite_timestamp(row[2])
                    if timestamp is not None and now - timestamp > 60:
                        stale_commands.append({"sequence": row[0], "state": row[1],
                                               "ageSeconds": round(now - timestamp, 1)})
        return {"tables": sorted(tables), "scopeOccupants": occupants,
                "staleWorkerCommands": stale_commands, "workerCommandError": command_error}, None
    except (sqlite3.Error, OSError) as exc:
        return None, f"sqlite read-only query failed: {exc}"[:180]
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass


def overall_state(items: Mapping[str, Mapping[str, Any]]) -> str:
    """READY is an all-required-observations result, never a default."""
    required = [item for item in items.values() if item.get("required", True)]
    return READY if required and all(item.get("state") == READY for item in required) else NOT_READY


def collect_readiness(*, command: Command = system_command, tcp_probe: TcpProbe = system_tcp_probe,
                      binding_loader: Callable[[str], Any] | None = None,
                      settings: Settings | None = None, now: float | None = None) -> dict[str, Any]:
    """Collect a receipt.  No item is READY unless it has an observation."""
    settings, now = settings or Settings.from_environment(), time.time() if now is None else now
    items: dict[str, dict[str, Any]] = {}
    ric, ric_error = _container(command, "oran-aic-nearrt-ric")
    witness: dict[str, Any] | None = None
    epochs: dict[str, int] = {}
    if ric_error:
        items["ric_e2"] = _item(UNKNOWN, f"cannot inspect RIC: {ric_error}")
    elif not _running(ric):
        items["ric_e2"] = _item(NOT_READY, "near-RT RIC container is not running")
    else:
        result = command(["docker", "exec", "oran-aic-nearrt-ric", "cat", settings.witness_path])
        try:
            witness = json.loads(result.stdout) if result.returncode == 0 else None
        except json.JSONDecodeError:
            witness = None
        if not isinstance(witness, dict):
            items["ric_e2"] = _item(NOT_READY, "cannot observe connection witness")
        else:
            epochs, missing = _witness_epochs(witness)
            alive = _host_pid_alive(witness.get("producerPid"))
            listener = command(["ss", "-S", "-a", "-n"])
            listener_lines = (listener.stdout + listener.stderr).splitlines()
            listens = any("LISTEN" in line and "36421" in line for line in listener_lines)
            associations = sum("ESTAB" in line and "36421" in line for line in listener_lines)
            if missing:
                items["ric_e2"] = _item(NOT_READY, missing, epochs=epochs)
            elif not listens or associations < len(epochs):
                items["ric_e2"] = _item(NOT_READY, "E2 SCTP 36421 LISTEN and active associations are not observed",
                                        epochs=epochs, establishedAssociations=associations)
            elif not alive:
                items["ric_e2"] = _item(NOT_READY, "witness producerPid is not alive on host", epochs=epochs)
            else:
                items["ric_e2"] = _item(READY, "RIC, active E2 connections, and witness producer observed", epochs=epochs)

    producer, producer_error = _container(command, "oran-aic-a1p-producer")
    producer_started_at: str | None = None
    producer_log = ""
    producer_log_observed = False
    tcp_ok = tcp_probe("192.168.50.1", 9444)
    if producer_error:
        items["a1p_r1"] = _item(UNKNOWN, f"cannot inspect A1-P producer: {producer_error}", r1TcpReachable=tcp_ok)
    elif not _running(producer):
        items["a1p_r1"] = _item(NOT_READY, "A1-P producer container is not running", r1TcpReachable=tcp_ok)
    else:
        producer_started_at = _rfc3339_z(producer.get("State", {}).get("StartedAt"))
        logs = command(["docker", "logs", "--since", producer_started_at, "oran-aic-a1p-producer"]) if producer_started_at else CommandResult(1)
        producer_log_observed = logs.returncode == 0
        log_lines = [line.strip().lower() for line in (logs.stdout + "\n" + logs.stderr).splitlines() if line.strip()]
        producer_log = "\n".join(log_lines)
        if not tcp_ok:
            items["a1p_r1"] = _item(NOT_READY, "R1 endpoint TCP 192.168.50.1:9444 is unreachable")
        elif not producer_started_at:
            items["a1p_r1"] = _item(NOT_READY, "producer StartedAt is unavailable for scoped log check")
        elif "refusing startup" in producer_log:
            items["a1p_r1"] = _item(NOT_READY, "producer log reports refusing startup")
        elif "xapp mode is live flexric" not in producer_log:
            items["a1p_r1"] = _item(NOT_READY, "producer post-start log has no live FlexRIC confirmation")
        else:
            items["a1p_r1"] = _item(READY, "producer live FlexRIC confirmation and R1 TCP observed")

    live, live_error = _read_json(str(Path(settings.live_artifact_root) / "live-flexric.json")) if settings.live_artifact_root else (None, "HW_LIVE_ARTIFACT_ROOT is unset")
    expected_sha = ((live or {}).get("e2CapabilityInventory") or {}).get("sha256")
    jsonl_path = ((live or {}).get("telemetry") or {}).get("jsonlPath")
    gate, gate_error = _container(command, "oran-aic-kpm-gate")
    age, age_error = _last_jsonl_age(jsonl_path, now) if isinstance(jsonl_path, str) else (None, "KPM JSONL path unavailable")
    gate_env = _env(gate) if gate else {}
    gate_epochs = [int(x) for x in gate_env.get("KPM_GATE_CONNECTION_EPOCHS", "").split(",") if x.isdigit()]
    witness_epoch_values = [epochs[k] for k in ("0x00000e00", "0x00000b00") if k in epochs]
    stream_fresh = age is not None and age <= settings.jsonl_max_age_s
    if live_error or gate_error:
        items["kpm_stream"] = _item(UNKNOWN, live_error or f"cannot inspect KPM gate: {gate_error}")
    elif gate_epochs != witness_epoch_values:
        items["kpm_stream"] = _item(NOT_READY, "KPM_GATE_CONNECTION_EPOCHS differs from witness", jsonlLastRecordAgeSeconds=age)
    elif gate_env.get("KPM_GATE_INVENTORY_SHA256") != expected_sha:
        items["kpm_stream"] = _item(NOT_READY, "KPM_GATE_INVENTORY_SHA256 differs from live inventory", jsonlLastRecordAgeSeconds=age)
    elif not (_running(gate) or stream_fresh):
        items["kpm_stream"] = _item(NOT_READY, "KPM gate is not running and JSONL is not fresh", jsonlLastRecordAgeSeconds=age, jsonlError=age_error)
    else:
        items["kpm_stream"] = _item(READY, "KPM epoch and inventory pins observed", jsonlLastRecordAgeSeconds=age,
                                    gateRunning=_running(gate), jsonlFresh=stream_fresh)
    attribution, attribution_error = (_kpm_ue_attribution(jsonl_path, settings.kpm_ue_attribution_lines)
                                      if isinstance(jsonl_path, str) else (None, "KPM JSONL path unavailable"))
    if attribution is None:
        items["kpm_ue_attribution"] = _item(NOT_READY, attribution_error or "KPM UE attribution unavailable")
    elif attribution == 0:
        items["kpm_ue_attribution"] = _item(NOT_READY, "no fresh KPM UE attribution", examinedLines=settings.kpm_ue_attribution_lines,
                                             attributedIndications=0)
    else:
        items["kpm_ue_attribution"] = _item(READY, "fresh KPM UE attribution observed", examinedLines=settings.kpm_ue_attribution_lines,
                                             attributedIndications=attribution)

    try:
        if binding_loader is None:
            from assurance.contracts.live_binding import load_assurance_live_binding
            binding_loader = load_assurance_live_binding
        binding = binding_loader(settings.binding_path)
        binding_epochs = _binding_epochs(binding)
        if binding_epochs != epochs:
            items["deployment_binding"] = _item(NOT_READY, "re-pin needed: binding expected epochs differ from witness",
                                                bindingEpochs=binding_epochs, witnessEpochs=epochs)
        else:
            items["deployment_binding"] = _item(READY, "binding source digests verified and epochs match witness", epochs=epochs)
    except Exception as exc:  # a receipt must report malformed deployment state, not crash
        items["deployment_binding"] = _item(NOT_READY, f"binding digest/load check failed: {exc}"[:220])

    db_path = None
    if producer:
        cmd = producer.get("Config", {}).get("Cmd", [])
        if isinstance(cmd, list) and "--db" in cmd:
            db_path = _host_path(producer, str(cmd[cmd.index("--db") + 1]))
    summary, db_error = (_sqlite_summary(db_path, now) if db_path
                         else (None, "producer database path cannot be resolved from inspect"))
    items["producer_sqlite"] = (_item(READY, "producer SQLite readable; policies schema and scope occupants observed", **summary)
                                if summary else _item(NOT_READY, db_error or "producer SQLite unavailable"))
    worker_remedy = "restart the producer (docker restart oran-aic-a1p-producer) and re-run readiness"
    if producer_error:
        items["a1p_worker"] = _item(NOT_READY, f"cannot inspect A1-P producer: {producer_error}; {worker_remedy}")
    elif not _running(producer):
        items["a1p_worker"] = _item(NOT_READY, f"A1-P producer container is not running; {worker_remedy}")
    elif not producer_started_at:
        items["a1p_worker"] = _item(NOT_READY, f"producer StartedAt is unavailable for scoped worker log check; {worker_remedy}")
    elif not producer_log_observed:
        items["a1p_worker"] = _item(NOT_READY, f"cannot read producer log after StartedAt; {worker_remedy}")
    elif "xapp worker stopped" in producer_log:
        items["a1p_worker"] = _item(NOT_READY, f"producer log reports xApp worker stopped; {worker_remedy}")
    elif not summary:
        items["a1p_worker"] = _item(NOT_READY, f"producer worker command probe failed: {db_error}; {worker_remedy}")
    elif summary.get("workerCommandError"):
        items["a1p_worker"] = _item(NOT_READY, f"{summary['workerCommandError']}; {worker_remedy}")
    elif summary.get("staleWorkerCommands"):
        states = ", ".join(sorted({str(row["state"]) for row in summary["staleWorkerCommands"]}))
        items["a1p_worker"] = _item(NOT_READY, f"producer commands has stale {states} row older than 60 seconds; {worker_remedy}",
                                     staleCommands=summary["staleWorkerCommands"])
    else:
        items["a1p_worker"] = _item(READY, "producer worker log and RECOVERY/SENT command queue are healthy")

    try:
        integration = Path(settings.live_artifact_root).parent / "deployment" / "integration-values.json"
        values, error = _read_json(str(integration))
        capability = ((live or {}).get("capabilityManifest") or {}).get("sha256")
        actual = (values or {}).get("values", {}).get("backend.capabilityManifestSha256")
        if error:
            items["capability_digest"] = _item(NOT_READY, f"cannot read integration values: {error}")
        elif actual != capability:
            items["capability_digest"] = _item(NOT_READY, "integration-values capability digest differs from live capability manifest")
        else:
            items["capability_digest"] = _item(READY, "integration-values and capability digest match", sha256=actual)
    except Exception as exc:
        items["capability_digest"] = _item(NOT_READY, f"capability digest check failed: {exc}"[:180])

    ue_ips: dict[str, str] = {}
    for name, host in settings.ue_hosts.items():
        if not host:
            items[f"{name}_liveness"] = _item(UNKNOWN, f"{name} host is not configured", required=name in settings.required_ues)
            continue
        addr = command(_host_command(host, ["ip", "-4", "-o", "addr", "show", "up", "dev", "oaitun_ue1"]))
        proc = command(_host_command(host, ["pgrep", "-x", "nr-uesoftmodem"]))
        prime = command(_host_command(host, ["ping", "-I", "oaitun_ue1", "-c", "1", "-W", "2", settings.ue_prime_target])) if settings.ue_prime_target else CommandResult(1)
        if addr.returncode or not addr.stdout.strip():
            items[f"{name}_liveness"] = _item(NOT_READY, "oaitun_ue1 has no UP IPv4 observation", required=name in settings.required_ues)
        elif proc.returncode:
            items[f"{name}_liveness"] = _item(NOT_READY, "nrue process is not observed", required=name in settings.required_ues)
        elif prime.returncode:
            items[f"{name}_liveness"] = _item(NOT_READY, "UL-prime ping did not succeed", required=name in settings.required_ues)
        else:
            ue_ips[name] = addr.stdout.strip().split()[3].split("/", 1)[0]
            items[f"{name}_liveness"] = _item(READY, "UP tun IPv4, nrue process, and UL-prime ping observed", required=name in settings.required_ues)

    required_ue_ips = {name: ue_ips.get(name) for name in settings.required_ues}
    if not settings.ext_dn_container:
        items["ue_dl_liveness"] = _item(UNKNOWN, "external-DN container is not configured", required=False)
    elif not all(required_ue_ips.values()):
        items["ue_dl_liveness"] = _item(NOT_READY, "cannot test DL liveness without every required UE tun IP")
    else:
        failures = []
        for name, ip in required_ue_ips.items():
            result = command(["docker", "exec", settings.ext_dn_container, "ping", "-c", "1", "-W", "2", str(ip)])
            if result.returncode:
                failures.append(name)
        if failures:
            items["ue_dl_liveness"] = _item(NOT_READY, "external-DN ping failed for " + ", ".join(sorted(failures)))
        else:
            items["ue_dl_liveness"] = _item(READY, "external-DN DL ping observed for required UEs",
                                              ueCount=len(required_ue_ips))

    for name, spec in settings.gnb.items():
        host, log_path, process = spec.get("host", ""), spec.get("log", ""), spec.get("process", "nr-softmodem")
        if not log_path:
            items[f"{name}_softmodem"] = _item(UNKNOWN, f"{name} log path is not configured")
            continue
        pid_args = ["pgrep", "-x", process] if spec.get("exact") == "true" else ["pgrep", "-o", "-f", process]
        pid = command(_host_command(host, pid_args))
        resolved_log = log_path
        if any(token in log_path for token in "*?["):
            # This is an operator-owned pathname glob from env.sh, intentionally
            # expanded by the target shell to select the newest running log.
            latest = command(_host_command(host, ["sh", "-lc", f"ls -1t {log_path} 2>/dev/null | head -n 1"]))
            resolved_log = latest.stdout.strip().splitlines()[0] if latest.returncode == 0 and latest.stdout.strip() else ""
        rf = command(_host_command(host, ["grep", "-F", "RU 0 RF started", resolved_log])) if resolved_log else CommandResult(1)
        log = command(_host_command(host, ["tail", "-n", "200", resolved_log])) if resolved_log else CommandResult(1)
        if pid.returncode:
            items[f"{name}_softmodem"] = _item(NOT_READY, "softmodem PID is not observed")
        elif log.returncode:
            items[f"{name}_softmodem"] = _item(NOT_READY, "cannot read softmodem log")
        elif rf.returncode:
            items[f"{name}_softmodem"] = _item(NOT_READY, "softmodem log lacks RU 0 RF started")
        elif "ERROR_CODE_TIMEOUT" in log.stdout:
            items[f"{name}_softmodem"] = _item(NOT_READY, "recent softmodem log has ERROR_CODE_TIMEOUT")
        else:
            started = command(_host_command(host, ["ps", "-o", "lstart=", "-p", pid.stdout.strip().splitlines()[0]]))
            items[f"{name}_softmodem"] = _item(READY, "PID, start time, RF start, and timeout-free recent log observed",
                                                pid=pid.stdout.strip().splitlines()[0], startTime=started.stdout.strip())

    if not settings.gnb1_running_conf:
        items["radio_profile"] = _item(UNKNOWN, "running gNB conf path is not configured; USRP warm-up observation is unavailable",
                                       required=False, expectedPrb=24, warmupSeconds=None)
    else:
        try:
            conf = Path(settings.gnb1_running_conf).read_text(encoding="utf-8")
            match = re.search(r"(?:N_RB_DL|n_rb_dl|dl_carrierBandwidth)\s*=\s*(\d+)", conf)
            prb = int(match.group(1)) if match else None
            if prb != 24:
                items["radio_profile"] = _item(NOT_READY, "running conf PRB is not the required 24", required=True,
                                               observedPrb=prb, expectedPrb=24, warmupSeconds=None)
            else:
                items["radio_profile"] = _item(READY, "running conf reports required 24 PRB; warm-up is separately unknown",
                                               observedPrb=prb, expectedPrb=24, warmupSeconds=None)
                items["usrp_warmup"] = _item(UNKNOWN, "USRP power-on timestamp is not observable", required=False,
                                               warmupSeconds=None)
        except OSError as exc:
            items["radio_profile"] = _item(NOT_READY, f"cannot read running gNB conf: {exc}"[:180], required=True,
                                           expectedPrb=24, warmupSeconds=None)
    state = overall_state(items)
    completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return _secret_free({"schemaVersion": "oran-aic-hardware-readiness/1.0.0",
                         "generatedAt": completed_at,
                         "timestamps": {"completedAt": completed_at},
                         "readiness": {"state": state, "items": items},
                         "provenance": {"secretFree": True, "collection": "read-only probes"}})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="receipt JSON path")
    args = parser.parse_args(argv)
    _load_hardware_environment()
    receipt = collect_readiness()
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{receipt['readiness']['state']}: {destination}")
    return 0 if receipt["readiness"]["state"] == READY else 1


if __name__ == "__main__":
    raise SystemExit(main())
