import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


REQUEST_SCHEMA = "stickerdb.input_replay_satellite.request.v1"
RESPONSE_SCHEMA = "stickerdb.input_replay_satellite.response.v1"
REPORT_KEYS = {
    "ts",
    "recording",
    "completion-status",
    "stage",
    "abort-index",
    "labels-added",
    "error",
}
JOB_SPECS = {
    "layout_sticker_to_lds": {
        "recording-key": "recording.layout",
        "inputs": ["sticker_image_path"],
        "outputs": ["lds_file_path"],
        "checklists": [
            "leonardo-design-studio",
            "launch-folder",
            "leonardo-save-as",
            "standard",
        ],
    },
    "print_lds_file": {
        "recording-key": "recording.print",
        "inputs": ["lds_file_path"],
        "outputs": [],
        "checklists": [
            "leonardo-design-studio",
            "launch-folder",
            "printer-ready",
            "standard",
        ],
    },
    "infranview_print_x2": {
        "recording-key": "recording.infranview-print-x2",
        "inputs": ["image_path"],
        "outputs": [],
        "checklists": [
            "infran-view",
            "launch-folder",
            "printer-ready",
            "infranview-test-print",
            "standard",
        ],
    },
    "infranview_print_x1": {
        "recording-key": "recording.infranview-print-x1",
        "inputs": ["image_path"],
        "outputs": [],
        "checklists": [
            "infran-view",
            "launch-folder",
            "printer-ready",
            "infranview-test-print",
            "standard",
        ],
    },
}
PENDING_STATES = {"pending"}


def scan_inbox(inbox, runs, now=None):
    """Read local request copies and return normalized queue entries."""
    inbox.mkdir(parents=True, exist_ok=True)
    runs.mkdir(parents=True, exist_ok=True)
    entries = []

    for path in sorted(inbox.glob("*.json"), key=lambda item: (item.stat().st_mtime, item.name)):
        entries.append(load_queue_entry(path, runs, now))

    return entries


def clear_non_pending_entries(entries):
    """Delete local queue entries that are not still pending."""
    results = []
    for entry in entries:
        if is_pending_entry(entry):
            results.append({"job-id": entry["job-id"], "state": entry["state"], "fate": "kept"})
            continue
        delete_queue_entry(entry)
        results.append({"job-id": entry["job-id"], "state": entry["state"], "fate": "deleted"})
    return results


def delete_queue_entry(entry):
    """Delete the satellite-owned inbox copy and terminal local record for one job."""
    source_path = entry.get("source-path")
    if source_path is not None and source_path.exists():
        source_path.unlink()

    record_path = entry.get("record-path")
    if record_path is not None and record_path.exists():
        record_dir = record_path.parent
        if record_dir.exists():
            shutil.rmtree(record_dir)


def is_pending_entry(entry):
    return entry.get("state") in PENDING_STATES


def load_queue_entry(path, runs, now=None):
    try:
        data = read_json(path)
        request = normalize_request(data, now)
    except Exception as exc:
        return {
            "source-path": path,
            "state": "invalid",
            "job-id": path.stem,
            "job": "",
            "created-at": "",
            "expires-at": "",
            "message": str(exc),
            "request": None,
        }

    record_path = get_record_path(runs, request["job_id"])
    state = "pending"
    message = ""
    if request["expired"]:
        state = "expired"
        message = "Request has passed expires_at."
    elif record_path.exists():
        record = read_json(record_path)
        state = record.get("state", "recorded")
        message = record.get("message", "")

    return {
        "source-path": path,
        "record-path": record_path,
        "state": state,
        "job-id": request["job_id"],
        "job": request["job"],
        "created-at": request["created_at"],
        "expires-at": request["expires_at"],
        "message": message,
        "request": request,
    }


def normalize_request(data, now=None):
    """Castle gate for requester-owned job JSON."""
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")

    required = ["schema", "job_id", "job", "created_at", "expires_at", "input", "response_path"]
    for key in required:
        if key not in data:
            raise ValueError(f"request is missing {key}")

    if data["schema"] != REQUEST_SCHEMA:
        raise ValueError(f"unsupported request schema: {data['schema']!r}")

    job_id = require_nonempty_string(data["job_id"], "job_id")
    job = require_nonempty_string(data["job"], "job")
    if job not in JOB_SPECS:
        raise ValueError(f"unknown job: {job}")

    created_at = parse_timestamp(data["created_at"], "created_at")
    expires_at = parse_timestamp(data["expires_at"], "expires_at")
    if expires_at <= created_at:
        raise ValueError("expires_at must be later than created_at")

    input_data = require_object(data["input"], "input")
    output_data = data.get("output", {})
    output_data = require_object(output_data, "output")
    spec = JOB_SPECS[job]

    normalized_input = {}
    for key in spec["inputs"]:
        if key not in input_data:
            raise ValueError(f"{job} input is missing {key}")
        normalized_input[key] = require_absolute_path(input_data[key], f"input.{key}")

    normalized_output = {}
    for key in spec["outputs"]:
        if key not in output_data:
            raise ValueError(f"{job} output is missing {key}")
        normalized_output[key] = require_absolute_path(output_data[key], f"output.{key}")

    response_path = require_absolute_path(data["response_path"], "response_path")
    current = now or datetime.now().astimezone()
    title = data.get("title", "")
    if title is not None and not isinstance(title, str):
        raise ValueError("title must be a string")

    return {
        "schema": REQUEST_SCHEMA,
        "job_id": job_id,
        "job": job,
        "title": (title or "").strip(),
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "input": normalized_input,
        "output": normalized_output,
        "response_path": response_path,
        "expired": current >= expires_at,
        "source": data,
    }


def execute_inputlog(request, config, run_dir):
    """Run one known InputLog recording and return a normalized outcome."""
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "inputlog-report.json"
    if report_path.exists():
        report_path.unlink()

    recording = config[JOB_SPECS[request["job"]]["recording-key"]]
    command = [
        config["inputlog.command"],
        "play",
        "--recording",
        recording,
        "--report",
        str(report_path),
    ]
    started_at = now_string()

    try:
        process = subprocess.run(
            command,
            cwd=config["execpath.inputlog-root"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as exc:
        return {
            "kind": "inputlog-launch-error",
            "normal": False,
            "status": "failed",
            "started-at": started_at,
            "finished-at": now_string(),
            "message": f"Could not launch InputLog: {exc}",
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "report": None,
            "stdout": "",
            "stderr": "",
            "returncode": None,
        }

    finished_at = now_string()
    report, report_error = read_inputlog_report(report_path, recording, started_at)
    base = {
        "started-at": started_at,
        "finished-at": finished_at,
        "report": report,
        "stdout": process.stdout,
        "stderr": process.stderr,
        "returncode": process.returncode,
    }
    if report_error:
        return {
            **base,
            "kind": "inputlog-report-error",
            "normal": False,
            "status": "failed",
            "message": report_error,
            "error": {"type": "InputLogReportError", "message": report_error},
        }

    completion = report["completion-status"]
    if completion == "normal":
        if process.returncode != 0:
            message = (
                "InputLog reported normal completion but exited with "
                f"return code {process.returncode}."
            )
            return {
                **base,
                "kind": "inputlog-exit-error",
                "normal": False,
                "status": "failed",
                "message": message,
                "error": {"type": "InputLogExitError", "message": message},
            }
        return {
            **base,
            "kind": "normal",
            "normal": True,
            "status": "done",
            "message": make_success_message(request),
            "error": None,
        }

    if completion == "user-aborted":
        message = f"InputLog playback was interrupted at event index {report['abort-index']}."
        return {
            **base,
            "kind": "user-aborted",
            "normal": False,
            "status": "interrupted",
            "message": message,
            "error": {"type": "InputLogInterrupted", "message": message},
        }

    error = report["error"]
    message = f"InputLog failed during {report['stage']}: {error['type']}: {error['message']}"
    return {
        **base,
        "kind": "inputlog-error",
        "normal": False,
        "status": "failed",
        "message": message,
        "error": error,
    }


def execute_job(request, config, run_dir):
    """Stage one request, run InputLog, collect output, and clear staging."""
    staging = config["execpath.staging-folder"]
    outcome = None
    try:
        clear_directory(staging)
        stage_request_input(request, staging)
        prepare_layout_output(request, config)
        outcome = execute_inputlog(request, config, run_dir)
        if outcome["normal"]:
            collect_job_output(request, config)
        elif request["job"] == "layout_sticker_to_lds":
            generated = get_leonardo_output_path(config)
            if generated.exists():
                generated.unlink()
        return outcome
    except Exception as exc:
        return {
            "kind": "job-adapter-error",
            "normal": False,
            "status": "failed",
            "started-at": outcome.get("started-at") if outcome else None,
            "finished-at": now_string(),
            "message": f"Input Replay Satellite could not prepare or collect the job: {exc}",
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "report": outcome.get("report") if outcome else None,
            "stdout": outcome.get("stdout", "") if outcome else "",
            "stderr": outcome.get("stderr", "") if outcome else "",
            "returncode": outcome.get("returncode") if outcome else None,
        }
    finally:
        clear_directory(staging)


def validate_queue_preflight(entries, config):
    """Automatic checks that must pass at the instant the operator says Go."""
    staging = config["execpath.staging-folder"]
    save_folder = config["execpath.leonardo-save-folder"]
    problems = []

    if not staging.is_dir():
        problems.append(f"Launch folder does not exist: {staging}")
    elif any(staging.iterdir()):
        problems.append(
            "Hey, wait, wait, this doesn't look safe. "
            f"The launch folder is not blank: {staging}"
        )

    if not save_folder.is_dir():
        problems.append(f"Leonardo save folder does not exist: {save_folder}")

    has_layout = any(entry["job"] == "layout_sticker_to_lds" for entry in entries)
    if has_layout and save_folder.is_dir():
        generated = get_leonardo_output_path(config)
        if generated.exists():
            problems.append(
                f"Leonardo's expected output file already exists: {generated}. "
                "Move or delete it before launching the queue."
            )

    for entry in entries:
        request = entry["request"]
        for key, path in request["input"].items():
            if not path.is_file():
                problems.append(f"{entry['job-id']} input.{key} does not exist: {path}")
        for key, path in request["output"].items():
            if path.exists():
                problems.append(f"{entry['job-id']} output.{key} already exists: {path}")

    return problems


def stage_request_input(request, staging):
    source = next(iter(request["input"].values()))
    if not source.is_file():
        raise FileNotFoundError(f"request input does not exist: {source}")
    destination = staging / source.name
    shutil.copy2(source, destination)
    return destination


def prepare_layout_output(request, config):
    if request["job"] != "layout_sticker_to_lds":
        return
    generated = get_leonardo_output_path(config)
    if generated.exists():
        raise FileExistsError(f"Leonardo output already exists: {generated}")
    requested = request["output"]["lds_file_path"]
    if requested.exists():
        raise FileExistsError(f"requested output already exists: {requested}")


def collect_job_output(request, config):
    if request["job"] != "layout_sticker_to_lds":
        return
    generated = get_leonardo_output_path(config)
    if not generated.is_file():
        raise FileNotFoundError(f"Leonardo did not create the expected output: {generated}")
    requested = request["output"]["lds_file_path"]
    requested.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(generated), str(requested))


def get_leonardo_output_path(config):
    return config["execpath.leonardo-save-folder"] / config["leonardo.output-filename"]


def clear_directory(path):
    """Clear a dedicated staging directory whose emptiness authorized this run."""
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def read_inputlog_report(path, expected_recording, started_at):
    if not path.is_file():
        return None, "InputLog did not produce a completion report."

    try:
        report = read_json(path)
    except Exception as exc:
        return None, f"InputLog report is unreadable: {exc}"

    if not isinstance(report, dict):
        return report, "InputLog report must be a JSON object."

    missing = sorted(REPORT_KEYS - set(report))
    if missing:
        return report, "InputLog report is missing keys: " + ", ".join(missing)

    if report["recording"] != expected_recording:
        return report, f"InputLog report recording mismatch: {report['recording']!r}"

    try:
        report_ts = int(report["ts"])
        started_ts = int(parse_timestamp(started_at, "started_at").timestamp())
    except (TypeError, ValueError):
        return report, "InputLog report ts is invalid."
    if report_ts < started_ts - 2:
        return report, "InputLog report is older than this execution attempt."

    status = report["completion-status"]
    if status == "running":
        return report, "InputLog exited while its report still said running."
    if status not in {"normal", "user-aborted", "error"}:
        return report, f"Unknown InputLog completion-status: {status!r}"
    if status == "error" and not isinstance(report["error"], dict):
        return report, "InputLog error report has no error object."
    return report, None


def make_response(request, outcome):
    observations = []
    report = outcome.get("report")
    if report is not None:
        observations.append(
            f"InputLog completion-status was {report['completion-status']} at stage {report['stage']}."
        )
        if report["labels-added"]:
            observations.append(f"InputLog added {report['labels-added']} recording label(s).")

    if outcome["normal"]:
        for key, path in request["output"].items():
            observations.append(f"The requested {key} exists at {path}.")
        if request["job"] in {
            "print_lds_file",
            "infranview_print_x1",
            "infranview_print_x2",
        }:
            observations.append("The satellite does not independently verify physical print quality.")

    error = None
    if not outcome["normal"]:
        source_error = outcome.get("error") or {}
        error = {
            "kind": outcome["kind"].replace("-", "_"),
            "message": outcome["message"],
        }
        if source_error:
            error["source-type"] = source_error.get("type", "")

    return {
        "schema": RESPONSE_SCHEMA,
        "job_id": request["job_id"],
        "job": request["job"],
        "ok": outcome["normal"],
        "status": outcome["status"],
        "message": outcome["message"],
        "started_at": outcome.get("started-at"),
        "finished_at": outcome.get("finished-at") or now_string(),
        "outputs": {key: str(path) for key, path in request["output"].items()},
        "error": error,
        "observations": observations,
    }


def make_operator_failure(request, message, status="failed", kind="operator_failed"):
    return {
        "schema": RESPONSE_SCHEMA,
        "job_id": request["job_id"],
        "job": request["job"],
        "ok": False,
        "status": status,
        "message": message,
        "started_at": None,
        "finished_at": now_string(),
        "outputs": {},
        "error": {"kind": kind, "message": message},
        "observations": [],
    }


def write_response(request, response):
    write_json_atomic(request["response_path"], response)


def write_run_record(record_path, entry, response, outcome=None):
    record = {
        "job_id": entry["job-id"],
        "job": entry["job"],
        "source_path": str(entry["source-path"]),
        "state": response["status"],
        "message": response["message"],
        "response_path": str(entry["request"]["response_path"]),
        "recorded_at": now_string(),
        "response": response,
    }
    if outcome is not None:
        record["inputlog"] = {
            "returncode": outcome.get("returncode"),
            "stdout": outcome.get("stdout", ""),
            "stderr": outcome.get("stderr", ""),
            "report": outcome.get("report"),
        }
    write_json_atomic(record_path, record)


def complete_entry(entry, response, outcome=None):
    write_response(entry["request"], response)
    write_run_record(entry["record-path"], entry, response, outcome)


def get_record_path(runs, job_id):
    digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
    return runs / digest / "record.json"


def get_attempt_dir(runs, job_id, attempt):
    return get_record_path(runs, job_id).parent / f"attempt-{attempt:03d}"


def make_success_message(request):
    if request["job"] == "layout_sticker_to_lds":
        return "InputLog completed normally and the requested LDS file exists."
    if request["job"] == "print_lds_file":
        return "InputLog completed the LDS print recording normally."
    if request["job"] == "infranview_print_x1":
        return "InputLog completed the IrfanView x1 print recording normally."
    return "InputLog completed the IrfanView x2 print recording normally."


def require_nonempty_string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def require_object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def require_absolute_path(value, name):
    value = require_nonempty_string(value, name)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path.resolve()


def parse_timestamp(value, name):
    value = require_nonempty_string(value, name)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if timestamp.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return timestamp


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            json.dump(data, temp, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def now_string():
    return datetime.now().astimezone().isoformat()
