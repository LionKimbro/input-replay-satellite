import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from input_replay_satellite import core


def make_request(tmp_path, job="layout_sticker_to_lds"):
    now = datetime.now().astimezone()
    job_dir = tmp_path / "job"
    job_dir.mkdir(parents=True)
    input_path = job_dir / ("sticker.png" if job == "layout_sticker_to_lds" else "sheet.lds")
    input_path.write_bytes(b"input")
    response_path = job_dir / "response.json"
    request = {
        "schema": core.REQUEST_SCHEMA,
        "job_id": "job-test-001",
        "job": job,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "input": {},
        "response_path": str(response_path),
    }
    if job == "layout_sticker_to_lds":
        request["input"]["sticker_image_path"] = str(input_path)
        request["output"] = {"lds_file_path": str(job_dir / "generated.lds")}
    else:
        request["input"]["lds_file_path"] = str(input_path)
    return request


def make_report(recording, status="normal", stage="complete", error=None):
    return {
        "ts": str(int(datetime.now().timestamp())),
        "recording": recording,
        "completion-status": status,
        "stage": stage,
        "abort-index": 9 if status == "user-aborted" else None,
        "labels-added": 0,
        "error": error,
    }


def make_config(tmp_path):
    inputlog_root = tmp_path / "inputlog"
    inputlog_root.mkdir(parents=True)
    staging = tmp_path / "launch"
    staging.mkdir()
    save_folder = tmp_path / "save"
    save_folder.mkdir()
    return {
        "execpath.inputlog-root": inputlog_root,
        "execpath.staging-folder": staging,
        "execpath.leonardo-save-folder": save_folder,
        "leonardo.output-filename": "Untitled.LDS",
        "inputlog.command": "inputlog",
        "recording.layout": "layout",
        "recording.print": "print-sticker",
    }


def fake_inputlog(monkeypatch, report, returncode=0):
    def run(command, cwd, capture_output, text, check):
        report_path = Path(command[command.index("--report") + 1])
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return SimpleNamespace(returncode=returncode, stdout="Playback complete.\n", stderr="")

    monkeypatch.setattr(core.subprocess, "run", run)


def test_normalize_layout_request(tmp_path):
    data = make_request(tmp_path)
    data["title"] = "Building sticker sheet for Gengar"
    request = core.normalize_request(data)

    assert request["job_id"] == "job-test-001"
    assert request["title"] == "Building sticker sheet for Gengar"
    assert request["input"]["sticker_image_path"].is_absolute()
    assert request["output"]["lds_file_path"].is_absolute()
    assert request["expired"] is False


def test_normalize_rejects_relative_response_path(tmp_path):
    data = make_request(tmp_path)
    data["response_path"] = "response.json"

    with pytest.raises(ValueError, match="response_path must be an absolute path"):
        core.normalize_request(data)


def test_scan_inbox_marks_invalid_expired_and_recorded(tmp_path):
    inbox = tmp_path / "inbox"
    runs = tmp_path / "runs"
    inbox.mkdir()

    valid = make_request(tmp_path / "valid")
    (inbox / "valid.json").write_text(json.dumps(valid), encoding="utf-8")
    (inbox / "invalid.json").write_text("[]", encoding="utf-8")

    expired = make_request(tmp_path / "expired")
    now = datetime.now().astimezone()
    expired["created_at"] = (now - timedelta(hours=2)).isoformat()
    expired["expires_at"] = (now - timedelta(hours=1)).isoformat()
    expired["job_id"] = "job-expired"
    (inbox / "expired.json").write_text(json.dumps(expired), encoding="utf-8")

    record_path = core.get_record_path(runs, valid["job_id"])
    core.write_json_atomic(record_path, {"state": "done", "message": "already ran"})
    entries = core.scan_inbox(inbox, runs)
    states = {entry["source-path"].name: entry["state"] for entry in entries}

    assert states == {
        "expired.json": "expired",
        "invalid.json": "invalid",
        "valid.json": "done",
    }


def test_clear_non_pending_entries_keeps_pending_and_deletes_terminal_items(tmp_path):
    inbox = tmp_path / "inbox"
    runs = tmp_path / "runs"
    inbox.mkdir()

    pending = make_request(tmp_path / "pending")
    pending["job_id"] = "job-pending"
    (inbox / "pending.json").write_text(json.dumps(pending), encoding="utf-8")

    done = make_request(tmp_path / "done")
    done["job_id"] = "job-done"
    (inbox / "done.json").write_text(json.dumps(done), encoding="utf-8")
    done_record_path = core.get_record_path(runs, done["job_id"])
    core.write_json_atomic(done_record_path, {"state": "done", "message": "already ran"})

    entries = core.scan_inbox(inbox, runs)
    results = core.clear_non_pending_entries(entries)

    assert sorted((item["state"], item["fate"]) for item in results) == [
        ("done", "deleted"),
        ("pending", "kept"),
    ]
    assert (inbox / "pending.json").exists()
    assert not (inbox / "done.json").exists()
    assert not done_record_path.exists()


def test_execute_layout_requires_requested_output(tmp_path, monkeypatch):
    request = core.normalize_request(make_request(tmp_path))
    config = make_config(tmp_path)
    fake_inputlog(monkeypatch, make_report("layout"))

    outcome = core.execute_job(request, config, tmp_path / "run")

    assert outcome["normal"] is False
    assert outcome["kind"] == "job-adapter-error"


def test_execute_layout_accepts_normal_report_and_existing_output(tmp_path, monkeypatch):
    request = core.normalize_request(make_request(tmp_path))
    request["output"]["lds_file_path"].write_bytes(b"lds")
    config = make_config(tmp_path)
    fake_inputlog(monkeypatch, make_report("layout"))

    outcome = core.execute_inputlog(request, config, tmp_path / "run")

    assert outcome["normal"] is True
    assert outcome["status"] == "done"


def test_execute_user_abort_is_not_success(tmp_path, monkeypatch):
    request = core.normalize_request(make_request(tmp_path, "print_lds_file"))
    config = make_config(tmp_path)
    fake_inputlog(monkeypatch, make_report("print-sticker", "user-aborted"))

    outcome = core.execute_inputlog(request, config, tmp_path / "run")

    assert outcome["normal"] is False
    assert outcome["status"] == "interrupted"
    assert outcome["report"]["abort-index"] == 9


def test_running_report_after_exit_is_failure(tmp_path, monkeypatch):
    request = core.normalize_request(make_request(tmp_path, "print_lds_file"))
    config = make_config(tmp_path)
    fake_inputlog(monkeypatch, make_report("print-sticker", "running", "playback"))

    outcome = core.execute_inputlog(request, config, tmp_path / "run")

    assert outcome["normal"] is False
    assert outcome["kind"] == "inputlog-report-error"


def test_nonzero_exit_cannot_be_success_even_with_normal_report(tmp_path, monkeypatch):
    request = core.normalize_request(make_request(tmp_path, "print_lds_file"))
    config = make_config(tmp_path)
    fake_inputlog(monkeypatch, make_report("print-sticker"), returncode=3)

    outcome = core.execute_inputlog(request, config, tmp_path / "run")

    assert outcome["normal"] is False
    assert outcome["kind"] == "inputlog-exit-error"


def test_complete_entry_writes_response_and_terminal_record(tmp_path):
    data = make_request(tmp_path)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(data), encoding="utf-8")
    runs = tmp_path / "runs"
    entry = core.load_queue_entry(request_path, runs)
    outcome = {
        "normal": False,
        "status": "failed",
        "kind": "test-failure",
        "message": "Nope.",
        "started-at": None,
        "finished-at": core.now_string(),
        "error": {"type": "TestFailure", "message": "Nope."},
        "report": None,
        "returncode": 1,
        "stdout": "",
        "stderr": "",
    }
    response = core.make_response(entry["request"], outcome)

    core.complete_entry(entry, response, outcome)

    written_response = core.read_json(entry["request"]["response_path"])
    record = core.read_json(entry["record-path"])
    assert written_response["status"] == "failed"
    assert record["state"] == "failed"


def test_preflight_refuses_nonblank_launch_folder(tmp_path):
    entry = {
        "job-id": "job-test",
        "job": "print_lds_file",
        "request": core.normalize_request(make_request(tmp_path / "request", "print_lds_file")),
    }
    config = make_config(tmp_path / "config")
    (config["execpath.staging-folder"] / "unexpected.txt").write_text("no", encoding="utf-8")

    problems = core.validate_queue_preflight([entry], config)

    assert any("doesn't look safe" in problem for problem in problems)
    assert (config["execpath.staging-folder"] / "unexpected.txt").exists()


def test_execute_print_stages_input_and_clears_folder(tmp_path, monkeypatch):
    request = core.normalize_request(make_request(tmp_path / "request", "print_lds_file"))
    config = make_config(tmp_path / "config")
    seen = {}

    def run(command, cwd, capture_output, text, check):
        staged = list(config["execpath.staging-folder"].iterdir())
        seen["staged"] = [path.name for path in staged]
        report_path = Path(command[command.index("--report") + 1])
        report_path.write_text(json.dumps(make_report("print-sticker")), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(core.subprocess, "run", run)
    outcome = core.execute_job(request, config, tmp_path / "run")

    assert outcome["normal"] is True
    assert seen["staged"] == ["sheet.lds"]
    assert list(config["execpath.staging-folder"].iterdir()) == []


def test_execute_layout_collects_untitled_lds_and_clears_folder(tmp_path, monkeypatch):
    request = core.normalize_request(make_request(tmp_path / "request"))
    config = make_config(tmp_path / "config")

    def run(command, cwd, capture_output, text, check):
        generated = core.get_leonardo_output_path(config)
        generated.write_bytes(b"generated lds")
        report_path = Path(command[command.index("--report") + 1])
        report_path.write_text(json.dumps(make_report("layout")), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(core.subprocess, "run", run)
    outcome = core.execute_job(request, config, tmp_path / "run")

    assert outcome["normal"] is True
    assert request["output"]["lds_file_path"].read_bytes() == b"generated lds"
    assert not core.get_leonardo_output_path(config).exists()
    assert list(config["execpath.staging-folder"].iterdir()) == []


def test_interrupted_layout_removes_attempt_owned_untitled_lds(tmp_path, monkeypatch):
    request = core.normalize_request(make_request(tmp_path / "request"))
    config = make_config(tmp_path / "config")

    def run(command, cwd, capture_output, text, check):
        generated = core.get_leonardo_output_path(config)
        generated.write_bytes(b"partial")
        report_path = Path(command[command.index("--report") + 1])
        report_path.write_text(
            json.dumps(make_report("layout", "user-aborted")),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(core.subprocess, "run", run)
    outcome = core.execute_job(request, config, tmp_path / "run")

    assert outcome["status"] == "interrupted"
    assert not core.get_leonardo_output_path(config).exists()
    assert list(config["execpath.staging-folder"].iterdir()) == []
