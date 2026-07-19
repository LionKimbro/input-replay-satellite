import os
import queue
import threading
import tkinter as tk
import time
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk

from input_replay_satellite import core


g = {
    "root": None,
    "widgets": {},
    "entries": [],
    "events": queue.Queue(),
    "decisions": queue.Queue(),
    "running": False,
    "config": None,
    "path-vars": {},
    "preview-image": None,
    "preview-source-image": None,
    "context-entry": None,
    "queue-budget": None,
    "timer-after-id": None,
}


def run(config):
    g["config"] = config
    root = tk.Tk()
    g["root"] = root
    root.title("Input Replay Satellite")
    root.geometry("1050x660")
    create_widgets(root)
    refresh_queue()
    root.protocol("WM_DELETE_WINDOW", handle_close)
    root.after(100, poll_worker_events)
    root.after(config["poll.ms"], periodic_refresh)
    root.mainloop()


def create_widgets(root):
    g["root"] = root
    frame = ttk.Frame(root, padding=12)
    frame.grid(row=0, column=0, sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(2, weight=1)
    frame.rowconfigure(4, weight=1)

    title = ttk.Label(frame, text="Input Replay Satellite", font=("TkDefaultFont", 16, "bold"))
    title.grid(row=0, column=0, sticky="w", pady=(0, 10))

    path_frame = ttk.LabelFrame(frame, text="Execution locations", padding=8)
    path_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
    path_frame.columnconfigure(1, weight=1)
    add_path_row(
        path_frame,
        0,
        "Launch folder",
        "execpath.staging-folder",
        "Must be completely blank when Go is pressed. The satellite owns its contents during the queue run.",
    )
    add_path_row(
        path_frame,
        1,
        "Leonardo save folder",
        "execpath.leonardo-save-folder",
        "Leonardo must save the generated Untitled.LDS file here.",
    )

    columns = ("state", "title", "job-id", "expires")
    tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
    tree.heading("state", text="State")
    tree.heading("title", text="Title")
    tree.heading("job-id", text="Job ID")
    tree.heading("expires", text="Expires")
    tree.column("state", width=90, stretch=False)
    tree.column("title", width=320)
    tree.column("job-id", width=240)
    tree.column("expires", width=230)
    tree.grid(row=2, column=0, sticky="nsew")
    tree.bind("<<TreeviewSelect>>", handle_selection)
    tree.bind("<Double-1>", handle_tree_double_click)
    tree.bind("<Button-3>", handle_tree_right_click)

    controls = ttk.Frame(frame)
    controls.grid(row=3, column=0, sticky="ew", pady=10)
    ttk.Label(controls, text="Run for").pack(side="left")
    minutes_var = tk.StringVar(value="")
    minutes_entry = ttk.Entry(controls, textvariable=minutes_var, width=6)
    minutes_entry.pack(side="left", padx=(6, 4))
    ttk.Label(controls, text="minutes").pack(side="left", padx=(0, 8))
    start_button = ttk.Button(controls, text="Start Pending Queue", command=start_queue)
    start_button.pack(side="left")
    checklist_button = ttk.Button(controls, text="Preflight Checklist", command=show_preflight_checklist)
    checklist_button.pack(side="left", padx=(8, 0))
    clear_button = ttk.Button(controls, text="Clear Queue", command=clear_queue)
    clear_button.pack(side="left", padx=(8, 0))
    timer_var = tk.StringVar(value="No time limit")
    ttk.Label(controls, textvariable=timer_var).pack(side="right", padx=(12, 0))
    pending_var = tk.StringVar(value="")
    ttk.Label(controls, textvariable=pending_var).pack(side="right")

    preview = ttk.PanedWindow(frame, orient="horizontal")
    preview.grid(row=4, column=0, sticky="nsew")
    preview_left = ttk.Frame(preview)
    preview_right = ttk.Frame(preview)
    preview.add(preview_left, weight=2)
    preview.add(preview_right, weight=1)

    preview_tree = ttk.Treeview(preview_left, columns=("value",), show="tree headings", height=10)
    preview_tree.heading("#0", text="Field")
    preview_tree.heading("value", text="Value")
    preview_tree.column("#0", width=220, stretch=False)
    preview_tree.column("value", width=520)
    preview_tree.pack(fill="both", expand=True)

    image_canvas = tk.Canvas(preview_right, background="#f2f2f2", highlightthickness=1, highlightbackground="#cccccc")
    image_canvas.pack(fill="both", expand=True)
    image_canvas.bind("<Configure>", lambda _event: redraw_preview_image())

    status_var = tk.StringVar(value="Ready.")
    ttk.Label(frame, textvariable=status_var, anchor="w").grid(row=5, column=0, sticky="ew", pady=(8, 0))

    g["widgets"] = {
        "tree": tree,
        "minutes-entry": minutes_entry,
        "minutes-var": minutes_var,
        "start": start_button,
        "checklist": checklist_button,
        "clear": clear_button,
        "timer-var": timer_var,
        "pending-var": pending_var,
        "preview-tree": preview_tree,
        "image-canvas": image_canvas,
        "status-var": status_var,
    }
    create_context_menu(root)


def add_path_row(parent, row, label, key, help_text):
    variable = tk.StringVar(value=str(g["config"][key]))
    g["path-vars"][key] = variable
    ttk.Label(parent, text=label).grid(row=row * 2, column=0, sticky="w", padx=(0, 8), pady=3)
    entry = ttk.Entry(parent, textvariable=variable)
    entry.grid(row=row * 2, column=1, sticky="ew", pady=3)
    ttk.Button(parent, text="Browse", command=lambda: browse_path(variable)).grid(
        row=row * 2, column=2, padx=(8, 0), pady=3
    )
    ttk.Button(parent, text="Open", command=lambda: open_path(variable)).grid(
        row=row * 2, column=3, padx=(6, 0), pady=3
    )
    ttk.Label(parent, text=help_text, foreground="#555555").grid(
        row=row * 2 + 1, column=1, columnspan=3, sticky="w", pady=(0, 4)
    )


def refresh_queue():
    entries = core.scan_inbox(
        g["config"]["projpath.inbox"],
        g["config"]["projpath.runs"],
    )
    g["entries"] = entries
    tree = g["widgets"]["tree"]
    selected = get_selected_entry()
    selected_source = str(selected["source-path"]) if selected else None
    tree.delete(*tree.get_children())

    for index, entry in enumerate(entries):
        iid = str(index)
        tree.insert(
            "",
            "end",
            iid=iid,
            values=(entry["state"], get_entry_title(entry), entry["job-id"], entry["expires-at"]),
        )
        if str(entry["source-path"]) == selected_source:
            tree.selection_set(iid)

    pending = sum(entry["state"] == "pending" for entry in entries)
    invalid = sum(entry["state"] == "invalid" for entry in entries)
    expired = sum(entry["state"] == "expired" for entry in entries)
    g["widgets"]["pending-var"].set(f"{pending} pending · {invalid} invalid · {expired} expired")
    set_status(f"Queue refreshed. {pending} job(s) ready.")


def handle_selection(_event=None):
    entry = get_selected_entry()
    if entry is None:
        show_preview(None)
        return
    show_preview(entry)


def get_entry_title(entry):
    request = entry.get("request")
    if request is None:
        return entry.get("message") or entry.get("job") or "(invalid request)"
    return request.get("title") or request.get("job") or entry.get("job")


def start_queue():
    if g["running"]:
        return
    pending = [entry for entry in g["entries"] if entry["state"] == "pending"]
    if not pending:
        messagebox.showinfo("Input Replay Satellite", "There are no pending jobs.")
        return
    try:
        budget = parse_minutes_budget(g["widgets"]["minutes-var"].get())
    except ValueError as exc:
        messagebox.showerror("Input Replay Satellite", str(exc))
        return
    sync_runtime_paths()
    problems = core.validate_queue_preflight(pending, g["config"])
    if problems:
        messagebox.showerror(
            "Input Replay Satellite — unsafe to launch",
            "\n\n".join(problems),
        )
        return
    if not show_preflight_checklist_dialog(pending, "launch"):
        return

    g["running"] = True
    g["queue-budget"] = budget
    set_controls_enabled(False)
    start_playback_countdown(lambda: begin_queue_worker(pending))


def start_playback_countdown(on_ready, count=3):
    if count > 0:
        set_status(
            f"Starting in {count}... Hands off the mouse and keyboard. "
            "Let any click or keystroke settle."
        )
        g["root"].after(1000, lambda: start_playback_countdown(on_ready, count - 1))
        return

    on_ready()


def begin_queue_worker(entries):
    set_status(f"Running {len(entries)} job(s). Do not touch mouse or keyboard during playback.")
    start_timer_display()
    worker = threading.Thread(target=run_queue, args=(entries,), daemon=True)
    worker.start()


def parse_minutes_budget(text):
    value = text.strip()
    if not value:
        return None
    try:
        minutes = float(value)
    except ValueError as exc:
        raise ValueError("Enter a positive number of minutes, or leave blank.") from exc
    if minutes <= 0:
        raise ValueError("Enter a positive number of minutes, or leave blank.")
    return {
        "started-at": None,
        "seconds": minutes * 60.0,
        "expired": False,
    }


def activate_queue_budget():
    budget = g.get("queue-budget")
    if budget is not None and budget["started-at"] is None:
        budget["started-at"] = time.monotonic()


def has_time_budget_expired():
    budget = g.get("queue-budget")
    if budget is None or budget["started-at"] is None:
        return False
    return time.monotonic() - budget["started-at"] >= budget["seconds"]


def mark_time_budget_expired():
    budget = g.get("queue-budget")
    if budget is not None:
        budget["expired"] = True


def format_duration(seconds):
    remaining = max(0, int(seconds))
    hours = remaining // 3600
    minutes = (remaining % 3600) // 60
    secs = remaining % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def get_timer_text():
    budget = g.get("queue-budget")
    if budget is None:
        return "No time limit"
    if budget.get("expired"):
        return "Time expired — finishing current task"
    if budget["started-at"] is None:
        return format_duration(budget["seconds"]) + " remaining"
    remaining = budget["seconds"] - (time.monotonic() - budget["started-at"])
    return format_duration(remaining) + " remaining"


def start_timer_display():
    activate_queue_budget()
    update_timer_display()


def update_timer_display():
    if not g["running"]:
        set_timer_text("No time limit")
        g["timer-after-id"] = None
        return
    if has_time_budget_expired():
        mark_time_budget_expired()
    set_timer_text(get_timer_text())
    g["timer-after-id"] = g["root"].after(250, update_timer_display)


def set_timer_text(text):
    g["widgets"]["timer-var"].set(text)


def run_queue(entries):
    try:
        _run_queue(entries)
    except Exception as exc:
        g["events"].put(
            {
                "type": "queue-error",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )


def _run_queue(entries):
    for queue_index, entry in enumerate(entries):
        if has_time_budget_expired():
            mark_time_budget_expired()
            g["events"].put({"type": "queue-time-expired"})
            return
        attempt = 1
        while True:
            g["events"].put(
                {
                    "type": "job-started",
                    "entry": entry,
                    "queue-index": queue_index,
                    "queue-count": len(entries),
                    "attempt": attempt,
                }
            )
            run_dir = core.get_attempt_dir(
                g["config"]["projpath.runs"],
                entry["job-id"],
                attempt,
            )
            outcome = core.execute_job(entry["request"], g["config"], run_dir)
            if outcome["normal"]:
                response = core.make_response(entry["request"], outcome)
                core.complete_entry(entry, response, outcome)
                pause_after_job_if_needed(entry, response, queue_index, len(entries))
                if stop_queue_if_time_expired(queue_index, len(entries)):
                    return
                break

            g["events"].put(
                {
                    "type": "decision-required",
                    "entry": entry,
                    "outcome": outcome,
                    "attempt": attempt,
                }
            )
            decision = g["decisions"].get()
            if decision == "retry":
                attempt += 1
                continue

            response = core.make_response(entry["request"], outcome)
            if decision == "fail-job":
                core.complete_entry(entry, response, outcome)
                pause_after_job_if_needed(
                    entry,
                    response,
                    queue_index,
                    len(entries),
                    countdown_before_next=True,
                )
                if stop_queue_if_time_expired(queue_index, len(entries)):
                    return
                break

            core.complete_entry(entry, response, outcome)
            for remaining in entries[queue_index + 1 :]:
                failure = core.make_operator_failure(
                    remaining["request"],
                    "The operator failed the entire queue after an earlier job stopped.",
                    kind="queue_failed",
                )
                core.complete_entry(remaining, failure)
            g["events"].put({"type": "queue-failed"})
            return

    g["events"].put({"type": "queue-finished"})


def stop_queue_if_time_expired(queue_index, queue_count):
    if queue_index >= queue_count - 1:
        return False
    if not has_time_budget_expired():
        return False
    mark_time_budget_expired()
    g["events"].put({"type": "queue-time-expired"})
    return True


def pause_after_job_if_needed(
    entry,
    response,
    queue_index,
    queue_count,
    countdown_before_next=False,
):
    acknowledged = threading.Event()
    g["events"].put(
        {
            "type": "job-finished",
            "entry": entry,
            "response": response,
            "queue-index": queue_index,
            "queue-count": queue_count,
            "acknowledged": acknowledged,
            "countdown-before-next": countdown_before_next,
        }
    )
    if queue_index < queue_count - 1:
        acknowledged.wait()


def poll_worker_events():
    while True:
        try:
            event = g["events"].get_nowait()
        except queue.Empty:
            break
        handle_worker_event(event)
    g["root"].after(100, poll_worker_events)


def handle_worker_event(event):
    event_type = event["type"]
    if event_type == "job-started":
        set_status(
            f"Running {event['entry']['job-id']} "
            f"({event['queue-index'] + 1}/{event['queue-count']}), attempt {event['attempt']}."
        )
        return
    if event_type == "job-finished":
        handle_job_finished_event(event)
        return
    if event_type == "decision-required":
        choose_after_abnormal_result(event)
        return
    if event_type == "queue-failed":
        finish_run("Queue failed by operator decision.")
        return
    if event_type == "queue-error":
        messagebox.showerror("Input Replay Satellite", event["message"])
        finish_run("Queue stopped because the satellite encountered an error.")
        return
    if event_type == "queue-time-expired":
        finish_run("Stopped — time budget expired", "Stopped — time budget expired")
        return
    if event_type == "queue-finished":
        finish_run("Queue complete.")


def handle_job_finished_event(event):
    acknowledged = event.get("acknowledged")
    try:
        update_finished_entry(event["entry"])
        title = get_entry_title(event["entry"])
        set_status(
            f"Finished {event['queue-index'] + 1}/{event['queue-count']}: "
            f"{title} — {event['response']['message']}"
        )
    finally:
        if acknowledged is not None:
            if event["queue-index"] < event["queue-count"] - 1:
                if event.get("countdown-before-next"):
                    start_playback_countdown(acknowledged.set)
                else:
                    g["root"].after(1500, acknowledged.set)
            else:
                acknowledged.set()


def update_finished_entry(entry):
    source_path = entry["source-path"]
    for index, current in enumerate(g["entries"]):
        if current["source-path"] != source_path:
            continue
        updated = core.load_queue_entry(source_path, g["config"]["projpath.runs"])
        g["entries"][index] = updated
        iid = str(index)
        tree = g["widgets"]["tree"]
        if tree.exists(iid):
            tree.item(
                iid,
                values=(updated["state"], get_entry_title(updated), updated["job-id"], updated["expires-at"]),
            )
        if get_selected_entry() is not None and get_selected_entry()["source-path"] == source_path:
            show_preview(updated)
        return


def choose_after_abnormal_result(event):
    outcome = event["outcome"]
    answer = messagebox.askyesnocancel(
        "Execution stopped",
        f"{event['entry']['job-id']} stopped:\n\n{outcome['message']}\n\n"
        "Yes: retry this job\n"
        "No: fail this job and continue\n"
        "Cancel: fail the entire queue",
    )
    if answer is True:
        submit_abnormal_decision(event, "retry")
    elif answer is False:
        submit_abnormal_decision(event, "fail-job")
    else:
        submit_abnormal_decision(event, "fail-queue")


def submit_abnormal_decision(event, decision):
    if decision == "retry":
        start_playback_countdown(lambda: g["decisions"].put(decision))
        return
    g["decisions"].put(decision)


def finish_run(message, timer_text="No time limit"):
    g["running"] = False
    g["queue-budget"] = None
    timer_after_id = g.get("timer-after-id")
    if timer_after_id is not None:
        try:
            g["root"].after_cancel(timer_after_id)
        except tk.TclError:
            pass
    g["timer-after-id"] = None
    set_controls_enabled(True)
    refresh_queue()
    set_status(message)
    set_timer_text(timer_text)


def periodic_refresh():
    if not g["running"]:
        refresh_queue()
    g["root"].after(g["config"]["poll.ms"], periodic_refresh)


def handle_close():
    if g["running"]:
        messagebox.showwarning(
            "Input Replay Satellite",
            "The queue is running. Resolve or finish the active InputLog job before closing the satellite.",
        )
        return
    g["root"].destroy()


def set_controls_enabled(enabled):
    state = "normal" if enabled else "disabled"
    g["widgets"]["minutes-entry"].configure(state=state)
    g["widgets"]["start"].configure(state=state)
    g["widgets"]["checklist"].configure(state=state)
    g["widgets"]["clear"].configure(state=state)


def set_status(message):
    g["widgets"]["status-var"].set(message)


def show_preview(entry):
    preview = g["widgets"]["preview-tree"]
    preview.delete(*preview.get_children())
    g["preview-source-image"] = None
    g["preview-image"] = None
    clear_image_canvas("No image preview")

    if entry is None:
        return

    add_preview_row("state", entry["state"])
    add_preview_row("title", get_entry_title(entry))
    add_preview_row("job", entry["job"])
    add_preview_row("job_id", entry["job-id"])
    add_preview_row("source_path", entry["source-path"])
    add_preview_row("expires_at", entry["expires-at"])
    if entry.get("message"):
        add_preview_row("message", entry["message"])

    request = entry.get("request")
    if request is None:
        return

    add_preview_row("created_at", request["created_at"])
    add_preview_row("response_path", request["response_path"])
    input_parent = add_preview_section("input")
    for key, path in request["input"].items():
        add_preview_row(key, path, input_parent)
    output_parent = add_preview_section("output")
    for key, path in request["output"].items():
        add_preview_row(key, path, output_parent)

    image_path = get_preview_image_path(request)
    if image_path is not None:
        load_preview_image(image_path)


def add_preview_section(label):
    return g["widgets"]["preview-tree"].insert("", "end", text=label, values=("",), open=True)


def add_preview_row(field, value, parent=""):
    g["widgets"]["preview-tree"].insert(parent, "end", text=str(field), values=(str(value),))


def get_preview_image_path(request):
    if request["job"] != "layout_sticker_to_lds":
        return None
    path = request["input"].get("sticker_image_path")
    if path is None or not path.is_file():
        return None
    return path


def load_preview_image(path):
    try:
        with Image.open(path) as image:
            g["preview-source-image"] = image.copy()
    except Exception as exc:
        clear_image_canvas(f"Could not preview image:\n{exc}")
        return
    redraw_preview_image()


def redraw_preview_image():
    canvas = g["widgets"].get("image-canvas")
    if canvas is None:
        return
    image = g.get("preview-source-image")
    if image is None:
        return
    width = max(canvas.winfo_width(), 1)
    height = max(canvas.winfo_height(), 1)
    shown = ImageOps.contain(image, (width - 12, height - 12))
    g["preview-image"] = ImageTk.PhotoImage(shown)
    canvas.delete("all")
    x = width // 2
    y = height // 2
    canvas.create_image(x, y, image=g["preview-image"], anchor="center")


def clear_image_canvas(message=""):
    canvas = g["widgets"].get("image-canvas")
    if canvas is None:
        return
    canvas.delete("all")
    if message:
        canvas.create_text(12, 12, text=message, anchor="nw", fill="#666666", width=260)


def create_context_menu(root):
    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="View Job", command=lambda: view_job(g["context-entry"]))
    menu.add_command(label="Delete Job", command=lambda: delete_job(g["context-entry"]))
    menu.add_command(label="Reload Job", command=lambda: reload_job(g["context-entry"]))
    g["widgets"]["context-menu"] = menu


def handle_tree_double_click(_event):
    view_job(get_selected_entry())


def handle_tree_right_click(event):
    tree = g["widgets"]["tree"]
    iid = tree.identify_row(event.y)
    if not iid:
        return
    tree.selection_set(iid)
    entry = g["entries"][int(iid)]
    g["context-entry"] = entry
    g["widgets"]["context-menu"].tk_popup(event.x_root, event.y_root)


def get_selected_entry():
    selection = g["widgets"]["tree"].selection()
    if not selection:
        return None
    index = int(selection[0])
    if index < 0 or index >= len(g["entries"]):
        return None
    return g["entries"][index]


def view_job(entry):
    if entry is None:
        return
    path = entry["source-path"]
    if not path.exists():
        messagebox.showerror("Input Replay Satellite", f"Job file no longer exists:\n{path}")
        return
    os.startfile(path)


def delete_job(entry):
    if entry is None:
        return
    if g["running"]:
        messagebox.showwarning("Input Replay Satellite", "The queue is running. Do not delete queue items right now.")
        return
    if not messagebox.askyesno(
        "Delete job",
        f"Delete this local queue item?\n\n{entry['job-id']}\n{entry['source-path']}",
    ):
        return
    core.delete_queue_entry(entry)
    refresh_queue()
    set_status(f"Deleted local queue item: {entry['job-id']}")


def reload_job(entry):
    if entry is None:
        return
    if g["running"]:
        messagebox.showwarning("Input Replay Satellite", "The queue is running. Do not reload queue items right now.")
        return
    if not entry["source-path"].exists():
        refresh_queue()
        set_status(f"Job file is gone: {entry['source-path']}")
        return
    updated = core.load_queue_entry(entry["source-path"], g["config"]["projpath.runs"])
    index = g["entries"].index(entry)
    g["entries"][index] = updated
    tree = g["widgets"]["tree"]
    iid = str(index)
    tree.item(iid, values=(updated["state"], get_entry_title(updated), updated["job-id"], updated["expires-at"]))
    tree.selection_set(iid)
    show_preview(updated)
    set_status(f"Reloaded local queue item: {updated['job-id']}")


def clear_queue():
    if g["running"]:
        return
    results = plan_clear_queue(g["entries"])
    deleted = [item for item in results if item["fate"] == "deleted"]
    if not deleted:
        messagebox.showinfo("Clear Queue", "No non-pending local queue items were found.")
        return
    summary = summarize_clear_results(results)
    if not messagebox.askyesno("Clear Queue", summary + "\n\nProceed?"):
        return
    results = core.clear_non_pending_entries(g["entries"])
    deleted_count = sum(item["fate"] == "deleted" for item in results)
    kept_count = sum(item["fate"] == "kept" for item in results)
    refresh_queue()
    set_status(f"Clear Queue deleted {deleted_count} local item(s), kept {kept_count} pending item(s).")


def plan_clear_queue(entries):
    results = []
    for entry in entries:
        fate = "kept" if core.is_pending_entry(entry) else "deleted"
        results.append({"job-id": entry["job-id"], "state": entry["state"], "fate": fate})
    return results


def summarize_clear_results(results):
    counts = {}
    for item in results:
        key = (item["state"], item["fate"])
        counts[key] = counts.get(key, 0) + 1
    lines = ["Clear Queue keeps pending jobs and deletes all other local queue items.", ""]
    for (state, fate), count in sorted(counts.items()):
        lines.append(f"{state}: {fate} ({count})")
    return "\n".join(lines)


def sync_runtime_paths():
    for key, variable in g["path-vars"].items():
        raw = variable.get().strip()
        if not raw:
            continue
        g["config"][key] = Path(raw).expanduser().resolve()


def browse_path(variable):
    selected = filedialog.askdirectory(initialdir=variable.get().strip() or None)
    if selected:
        variable.set(selected)


def open_path(variable):
    path = Path(variable.get().strip()).expanduser()
    if not path.is_dir():
        messagebox.showerror("Input Replay Satellite", f"Folder does not exist:\n{path}")
        return
    os.startfile(path)


def show_preflight_checklist():
    sync_runtime_paths()
    pending = [entry for entry in g["entries"] if entry["state"] == "pending"]
    if not pending:
        messagebox.showinfo("Input Replay Satellite — preflight checklist", "There are no pending jobs.")
        return
    problems = core.validate_queue_preflight(pending, g["config"])
    if problems:
        messagebox.showerror(
            "Input Replay Satellite — unsafe to launch",
            "\n\n".join(problems),
        )
        return
    show_preflight_checklist_dialog(pending, "review")


def show_preflight_checklist_dialog(entries, mode):
    checklist = build_preflight_checklist(entries, g["config"])
    dialog = tk.Toplevel(g["root"])
    dialog.title("Input Replay Satellite — preflight checklist")
    dialog.transient(g["root"])
    dialog.grab_set()
    dialog.geometry("720x520")
    dialog.columnconfigure(0, weight=1)
    dialog.rowconfigure(1, weight=1)
    state = {"accepted": False}

    intro_text = (
        f"Review {len(entries)} pending job(s). Check every applicable item before launch."
        if mode == "launch"
        else f"Checklist for {len(entries)} pending job(s)."
    )
    intro = ttk.Label(dialog, text=intro_text, padding=(12, 12, 12, 4), wraplength=680)
    intro.grid(row=0, column=0, sticky="ew")

    canvas = tk.Canvas(dialog, highlightthickness=0)
    scrollbar = ttk.Scrollbar(dialog, orient="vertical", command=canvas.yview)
    content = ttk.Frame(canvas, padding=(12, 8, 12, 8))
    content.columnconfigure(0, weight=1)
    window_id = canvas.create_window((0, 0), window=content, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.grid(row=1, column=0, sticky="nsew")
    scrollbar.grid(row=1, column=1, sticky="ns")

    def sync_scroll_region(event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(window_id, width=canvas.winfo_width())

    content.bind("<Configure>", sync_scroll_region)
    canvas.bind("<Configure>", sync_scroll_region)

    variables = []
    row = 0
    for section in checklist:
        label = ttk.Label(content, text=section["title"], font=("TkDefaultFont", 10, "bold"))
        label.grid(row=row, column=0, sticky="w", pady=(8, 4))
        row += 1
        for item in section["items"]:
            variable = tk.BooleanVar(value=False)
            variables.append(variable)
            check = ttk.Checkbutton(content, text=item, variable=variable)
            check.grid(row=row, column=0, sticky="w", pady=2)
            row += 1

    warning = ttk.Label(
        content,
        text=(
            "When you press Launch, the blank launch folder becomes satellite-owned "
            "for the duration of this queue run. Do not touch the mouse or keyboard during playback."
        ),
        foreground="#8a4b00",
        wraplength=660,
    )
    warning.grid(row=row, column=0, sticky="ew", pady=(14, 4))

    button_row = ttk.Frame(dialog, padding=12)
    button_row.grid(row=2, column=0, columnspan=2, sticky="e")
    primary_text = "Launch Queue" if mode == "launch" else "Done"
    primary = ttk.Button(button_row, text=primary_text)
    damn_the_law = ttk.Button(button_row, text="Damn the Law!") if mode == "launch" else None
    cancel = ttk.Button(button_row, text="Cancel", command=dialog.destroy)
    primary.pack(side="left")
    if damn_the_law is not None:
        damn_the_law.pack(side="left", padx=(8, 0))
    cancel.pack(side="left", padx=(8, 0))

    def all_checked():
        return all(variable.get() for variable in variables)

    def update_primary_state(*_args):
        if mode == "review":
            primary.configure(state="normal")
            return
        primary.configure(state="normal" if all_checked() else "disabled")

    def accept():
        if mode == "launch" and not all_checked():
            return
        state["accepted"] = True
        dialog.destroy()

    def accept_without_checklist():
        state["accepted"] = True
        dialog.destroy()

    for variable in variables:
        variable.trace_add("write", update_primary_state)
    primary.configure(command=accept)
    if damn_the_law is not None:
        damn_the_law.configure(command=accept_without_checklist)
    update_primary_state()

    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.wait_window()
    return state["accepted"]


def build_preflight_checklist(entries, config):
    jobs = {entry["job"] for entry in entries}
    launch_folder = config["execpath.staging-folder"]
    leonardo_output = core.get_leonardo_output_path(config)
    sections = []

    if "layout_sticker_to_lds" in jobs:
        sections.append(
            {
                "title": "layout_sticker_to_lds",
                "items": [
                    "Leonardo Design Studio is open on the left display",
                    "Launch folder is open on the left side of the right display",
                    f"Launch folder is open to {launch_folder}",
                    f'"Save As" in Leonardo Design Studio saves to {leonardo_output}',
                ],
            }
        )

    if "print_lds_file" in jobs:
        sections.append(
            {
                "title": "print_lds_file",
                "items": [
                    "Leonardo Design Studio is open on the left display",
                    "Launch folder is open on the left side of the right display",
                    f"Launch folder is open to {launch_folder}",
                    "The printer is on.",
                    "The printer is loaded with paper.",
                ],
            }
        )

    sections.append(
        {
            "title": "always",
            "items": [
                "Ensure that the Launch folder's \"View | Show > Navigation Pane\" is OFF",
                "Ensure that the Launch folder's \"View | Extra large icons\" is ON",
                "You will not touch the mouse or keyboard during playback.",
            ],
        }
    )

    return sections
