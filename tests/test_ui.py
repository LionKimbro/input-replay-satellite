from pathlib import Path
import queue

import pytest

from input_replay_satellite import ui


def make_config():
    return {
        "execpath.staging-folder": Path("C:/Users/Robert/Launch"),
        "execpath.leonardo-save-folder": Path("D:/tmp"),
        "leonardo.output-filename": "Untitled.LDS",
    }


def make_entry(job):
    return {"job": job}


def flatten_items(checklist):
    return [item for section in checklist for item in section["items"]]


def test_layout_checklist_contains_complete_layout_requirements():
    checklist = ui.build_preflight_checklist([make_entry("layout_sticker_to_lds")], make_config())
    checklist_ids = [section["id"] for section in checklist]
    items = flatten_items(checklist)

    assert checklist_ids == [
        "leonardo-design-studio",
        "launch-folder",
        "leonardo-save-as",
        "standard",
    ]
    assert "Leonardo Design Studio is open on the left display." in items
    assert "Leonardo Design Studio contains only one, fresh, blank tab." in items
    assert "Launch folder is open on the left half of the right display." in items
    assert "Launch folder is open to C:\\Users\\Robert\\Launch." in items
    assert '"Save As" in Leonardo Design Studio saves to D:\\tmp\\Untitled.LDS.' in items
    assert 'Ensure that the Launch folder\'s "View | Show > Navigation Pane" is OFF.' in items
    assert 'Ensure that the Launch folder\'s "View | Extra large icons" is ON.' in items
    assert "You will not touch the mouse or keyboard during playback." in items


def test_print_checklist_contains_complete_print_requirements():
    checklist = ui.build_preflight_checklist([make_entry("print_lds_file")], make_config())
    checklist_ids = [section["id"] for section in checklist]
    items = flatten_items(checklist)

    assert checklist_ids == [
        "leonardo-design-studio",
        "launch-folder",
        "printer-ready",
        "standard",
    ]
    assert "The printer is on." in items
    assert "The printer is loaded with the right size of paper for your task." in items
    assert 'Ensure that the Launch folder\'s "View | Show > Navigation Pane" is OFF.' in items
    assert 'Ensure that the Launch folder\'s "View | Extra large icons" is ON.' in items
    assert not any("Save As" in item for item in items)


def test_infranview_print_jobs_share_one_complete_checklist_set():
    x1_checklist = ui.build_preflight_checklist([make_entry("infranview_print_x1")], make_config())
    x2_checklist = ui.build_preflight_checklist([make_entry("infranview_print_x2")], make_config())
    checklist_ids = [section["id"] for section in x1_checklist]
    items = flatten_items(x1_checklist)

    assert x1_checklist == x2_checklist
    assert checklist_ids == [
        "infran-view",
        "launch-folder",
        "printer-ready",
        "infranview-test-print",
        "standard",
    ]
    assert "InfranView is open on the left display." in items
    assert "The printer is loaded with the right size of paper for your task." in items
    assert any("completed one print with InfranView" in item for item in items)
    assert not any("Leonardo" in item for item in items)
    assert not any("Save As" in item for item in items)


def test_mixed_checklist_is_deduplicated_and_uses_dictionary_order():
    checklist = ui.build_preflight_checklist(
        [
            make_entry("layout_sticker_to_lds"),
            make_entry("print_lds_file"),
            make_entry("infranview_print_x1"),
            make_entry("infranview_print_x2"),
            make_entry("layout_sticker_to_lds"),
        ],
        make_config(),
    )
    checklist_ids = [section["id"] for section in checklist]

    assert checklist_ids == [
        "infran-view",
        "leonardo-design-studio",
        "launch-folder",
        "printer-ready",
        "leonardo-save-as",
        "infranview-test-print",
        "standard",
    ]


def test_checklist_incompatibilities_are_derived_from_the_registry():
    checklist = ui.build_preflight_checklist(
        [make_entry("layout_sticker_to_lds"), make_entry("infranview_print_x1")],
        make_config(),
    )

    assert ui.find_checklist_incompatibilities(checklist) == [
        ("infran-view", "leonardo-design-studio"),
        ("leonardo-design-studio", "infran-view"),
    ]


def test_checklist_incompatibilities_do_not_depend_on_specific_checklist_names(monkeypatch):
    monkeypatch.setattr(
        ui,
        "PREFLIGHT_CHECKLISTS",
        {
            "alpha": {"incompatible-with": ["beta"]},
            "beta": {"incompatible-with": ["alpha"]},
        },
    )

    assert ui.find_checklist_incompatibilities([{"id": "alpha"}, {"id": "beta"}]) == [
        ("alpha", "beta"),
        ("beta", "alpha"),
    ]


def test_cancel_queue_cancels_all_pending_entries_after_confirmation(monkeypatch):
    pending = {"job-id": "pending", "state": "pending"}
    terminal = {"job-id": "finished", "state": "complete"}
    cancelled = []
    statuses = []
    ui.g["running"] = False
    ui.g["entries"] = [pending, terminal]
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *_args: True)
    monkeypatch.setattr(ui.core, "cancel_pending_entries", lambda entries: cancelled.extend(entries))
    monkeypatch.setattr(ui, "refresh_queue", lambda: None)
    monkeypatch.setattr(ui, "set_status", statuses.append)

    ui.cancel_queue()

    assert cancelled == [pending]
    assert statuses == ["Cancelled 1 pending job(s)."]


def test_cancel_job_cancels_selected_pending_entry_without_confirmation(monkeypatch):
    pending = {"job-id": "pending", "state": "pending"}
    cancelled = []
    statuses = []
    ui.g["running"] = False
    monkeypatch.setattr(ui.core, "cancel_pending_entries", lambda entries: cancelled.extend(entries))
    monkeypatch.setattr(ui, "refresh_queue", lambda: None)
    monkeypatch.setattr(ui, "set_status", statuses.append)

    ui.cancel_job(pending)

    assert cancelled == [pending]
    assert statuses == ["Cancelled pending job: pending"]


def test_double_click_cancels_the_selected_job(monkeypatch):
    selected = {"job-id": "pending", "state": "pending"}
    cancelled = []
    monkeypatch.setattr(ui, "get_selected_entry", lambda: selected)
    monkeypatch.setattr(ui, "cancel_job", cancelled.append)

    ui.handle_tree_double_click(None)

    assert cancelled == [selected]


@pytest.mark.parametrize(
    ("job", "input_key"),
    [
        ("layout_sticker_to_lds", "sticker_image_path"),
        ("infranview_print_x1", "image_path"),
        ("infranview_print_x2", "image_path"),
    ],
)
def test_get_preview_image_path_returns_existing_image_for_image_jobs(tmp_path, job, input_key):
    image_path = tmp_path / "sticker.png"
    image_path.write_bytes(b"image")
    request = {"job": job, "input": {input_key: image_path}}

    assert ui.get_preview_image_path(request) == image_path


def test_get_preview_image_path_ignores_non_image_jobs(tmp_path):
    image_path = tmp_path / "sheet.lds"
    image_path.write_bytes(b"lds")
    request = {"job": "print_lds_file", "input": {"lds_file_path": image_path}}

    assert ui.get_preview_image_path(request) is None


def test_parse_minutes_budget_blank_means_no_limit():
    assert ui.parse_minutes_budget("") is None
    assert ui.parse_minutes_budget("   ") is None


def test_parse_minutes_budget_positive_number():
    budget = ui.parse_minutes_budget("45")

    assert budget["started-at"] is None
    assert budget["seconds"] == 2700
    assert budget["expired"] is False


def test_parse_minutes_budget_rejects_invalid_values():
    for value in ["0", "-1", "nope"]:
        with pytest.raises(ValueError, match="Enter a positive number of minutes, or leave blank."):
            ui.parse_minutes_budget(value)


def test_format_duration_uses_hh_mm_ss_and_never_negative():
    assert ui.format_duration(2533) == "00:42:13"
    assert ui.format_duration(3661) == "01:01:01"
    assert ui.format_duration(-1) == "00:00:00"


def test_stop_queue_if_time_expired_only_between_jobs(monkeypatch):
    ui.g["events"] = queue.Queue()
    ui.g["queue-budget"] = {"started-at": 1.0, "seconds": 1.0, "expired": False}
    monkeypatch.setattr(ui, "has_time_budget_expired", lambda: True)

    assert ui.stop_queue_if_time_expired(0, 2) is True
    assert ui.g["queue-budget"]["expired"] is True
    assert ui.g["events"].get_nowait()["type"] == "queue-time-expired"


def test_stop_queue_if_time_expired_does_not_stop_after_last_job(monkeypatch):
    ui.g["events"] = queue.Queue()
    ui.g["queue-budget"] = {"started-at": 1.0, "seconds": 1.0, "expired": False}
    monkeypatch.setattr(ui, "has_time_budget_expired", lambda: True)

    assert ui.stop_queue_if_time_expired(1, 2) is False
    assert ui.g["events"].empty()


def test_playback_countdown_runs_callback_after_three_ticks(monkeypatch):
    scheduled = []
    statuses = []

    class FakeRoot:
        def after(self, delay, callback):
            scheduled.append((delay, callback))

    ui.g["root"] = FakeRoot()
    monkeypatch.setattr(ui, "set_status", statuses.append)
    ready = []

    ui.start_playback_countdown(lambda: ready.append(True))

    while scheduled:
        delay, callback = scheduled.pop(0)
        assert delay == 1000
        callback()

    assert ready == [True]
    assert [message.split("...")[0] for message in statuses] == [
        "Starting in 3",
        "Starting in 2",
        "Starting in 1",
    ]


def test_retry_decision_waits_for_playback_countdown(monkeypatch):
    ui.g["decisions"] = queue.Queue()
    callbacks = []
    monkeypatch.setattr(ui, "start_playback_countdown", lambda callback: callbacks.append(callback))

    ui.submit_abnormal_decision({}, "retry")

    assert ui.g["decisions"].empty()
    assert len(callbacks) == 1
    callbacks[0]()
    assert ui.g["decisions"].get_nowait() == "retry"


def test_non_retry_decisions_are_sent_without_a_countdown(monkeypatch):
    ui.g["decisions"] = queue.Queue()
    monkeypatch.setattr(
        ui,
        "start_playback_countdown",
        lambda _callback: pytest.fail("countdown should not start here"),
    )

    ui.submit_abnormal_decision({}, "fail-job")
    ui.submit_abnormal_decision({}, "fail-queue")

    assert ui.g["decisions"].get_nowait() == "fail-job"
    assert ui.g["decisions"].get_nowait() == "fail-queue"


def test_abnormal_job_completion_counts_down_before_next_job(monkeypatch):
    ui.g["events"] = queue.Queue()

    class FakeEvent:
        def wait(self):
            pass

        def set(self):
            pass

    monkeypatch.setattr(ui.threading, "Event", FakeEvent)

    ui.pause_after_job_if_needed(
        {"job-id": "test"},
        {"message": "failed"},
        0,
        2,
        countdown_before_next=True,
    )

    event = ui.g["events"].get_nowait()
    assert event["countdown-before-next"] is True
    assert event["acknowledged"] is not None
