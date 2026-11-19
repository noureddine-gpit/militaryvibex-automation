from __future__ import annotations

import csv
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_DIR = Path(__file__).resolve().parent
# Keep the downloader portable: its bundled tools live beside this script.
PROJECT_DIR = APP_DIR
YTDLP_DIR = PROJECT_DIR / ".yt-dlp"
FFMPEG_EXE = (
    PROJECT_DIR
    / ".ffmpeg"
    / "imageio_ffmpeg"
    / "binaries"
    / "ffmpeg-win-x86_64-v7.1.exe"
)
NODE_EXE = (
    Path.home()
    / ".cache"
    / "codex-runtimes"
    / "codex-primary-runtime"
    / "dependencies"
    / "node"
    / "bin"
    / "node.exe"
)
STATE_PATH = APP_DIR / "progress.json"
DEFAULT_SHEET = (
    "https://docs.google.com/spreadsheets/d/"
    "17cY-hf1V2RD10XSjMj4d_Yq9MMEcn-UX8rgDbEfdeqc/"
    "edit?gid=1421960173#gid=1421960173"
)


def safe_filename(value: str, fallback: str = "video") -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:90].rstrip() or fallback)


def sheet_csv_url(value: str) -> str:
    if "export?format=csv" in value:
        return value
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    if not match:
        raise ValueError("Google Sheet link is not valid.")
    gid_match = re.search(r"(?:[?#&]gid=)(\d+)", value)
    gid = gid_match.group(1) if gid_match else "0"
    return (
        f"https://docs.google.com/spreadsheets/d/{match.group(1)}"
        f"/export?format=csv&gid={gid}"
    )


def fetch_sheet_rows(sheet_url: str) -> list[dict[str, str]]:
    request = urllib.request.Request(
        sheet_csv_url(sheet_url),
        headers={"User-Agent": "MilitaryVibeX-Automation/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8-sig")

    matrix = list(csv.reader(text.splitlines()))
    header_index = next(
        (
            index
            for index, row in enumerate(matrix)
            if row and row[0].strip().lower() == "youtube link"
        ),
        None,
    )
    if header_index is None:
        raise ValueError("Could not find the 'YouTube link' header in the Sheet.")

    result: list[dict[str, str]] = []
    for source_index, row in enumerate(matrix[header_index + 1 :], start=1):
        link = row[0].strip() if row else ""
        if not link.startswith(("https://", "http://")):
            continue
        title = row[1].strip() if len(row) > 1 else f"video-{source_index:03d}"
        result.append({"link": link, "title": title, "index": str(source_index)})
    return result


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"completed": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        data.setdefault("completed", {})
        return data
    except (OSError, json.JSONDecodeError):
        return {"completed": {}}


def save_state(state: dict) -> None:
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp_path.replace(STATE_PATH)


class DownloaderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MilitaryVibeX Downloader")
        self.root.geometry("880x650")
        self.root.minsize(760, 560)

        self.sheet_var = tk.StringVar(value=DEFAULT_SHEET)
        self.folder_var = tk.StringVar(
            value=str(Path.home() / "Downloads" / "MilitaryVibeX")
        )
        self.batch_var = tk.IntVar(value=10)
        self.quality_var = tk.StringVar(value="1080")
        self.rights_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0)

        self.worker: threading.Thread | None = None
        self.pause_requested = threading.Event()
        self.current_process: subprocess.Popen[str] | None = None
        self.process_lock = threading.Lock()
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_ui()
        self.root.after(100, self._drain_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Sub.TLabel", font=("Segoe UI", 10), foreground="#475569")
        style.configure("Accent.TButton", font=("Segoe UI", 11, "bold"))

        main = ttk.Frame(self.root, padding=22)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="MilitaryVibeX Downloader", style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            main,
            text="Download authorized videos from the Google Sheet, in order, without duplicates.",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(2, 18))

        form = ttk.Frame(main)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Google Sheet").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=self.sheet_var).grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=6
        )

        ttk.Label(form, text="Download folder").grid(
            row=1, column=0, sticky="w", pady=6
        )
        ttk.Entry(form, textvariable=self.folder_var).grid(
            row=1, column=1, sticky="ew", padx=(12, 8), pady=6
        )
        ttk.Button(form, text="Browse…", command=self._choose_folder).grid(
            row=1, column=2, sticky="e", pady=6
        )

        options = ttk.Frame(main)
        options.pack(fill="x", pady=(12, 4))
        ttk.Label(options, text="Videos this run").pack(side="left")
        ttk.Spinbox(
            options, from_=1, to=212, width=7, textvariable=self.batch_var
        ).pack(side="left", padx=(8, 24))
        ttk.Label(options, text="Max quality").pack(side="left")
        ttk.Combobox(
            options,
            state="readonly",
            width=9,
            values=("1080", "720", "480"),
            textvariable=self.quality_var,
        ).pack(side="left", padx=(8, 0))

        ttk.Checkbutton(
            main,
            text="I confirm that I own these videos or have permission to download and reuse them.",
            variable=self.rights_var,
        ).pack(anchor="w", pady=(10, 14))

        controls = ttk.Frame(main)
        controls.pack(fill="x")
        self.start_button = ttk.Button(
            controls, text="Start", style="Accent.TButton", command=self._start
        )
        self.start_button.pack(side="left")
        self.pause_button = ttk.Button(
            controls, text="Pause", command=self._pause, state="disabled"
        )
        self.pause_button.pack(side="left", padx=8)
        ttk.Button(controls, text="Open folder", command=self._open_folder).pack(
            side="left"
        )
        ttk.Label(controls, textvariable=self.status_var).pack(side="right")

        ttk.Progressbar(
            main, variable=self.progress_var, maximum=100, mode="determinate"
        ).pack(fill="x", pady=(16, 10))

        log_frame = ttk.LabelFrame(main, text="Activity", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_frame,
            height=16,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            background="#0f172a",
            foreground="#e2e8f0",
            insertbackground="#ffffff",
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _ui(self, callback, *args) -> None:
        self.root.after(0, callback, *args)

    def _log(self, message: str) -> None:
        self.log_queue.put(message.rstrip())

    def _drain_log_queue(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(100, self._drain_log_queue)

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.folder_var.get())
        if chosen:
            self.folder_var.set(chosen)

    def _open_folder(self) -> None:
        folder = Path(self.folder_var.get()).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def _dependencies_ok(self) -> bool:
        missing = [
            str(path)
            for path in (YTDLP_DIR, FFMPEG_EXE, NODE_EXE)
            if not path.exists()
        ]
        if missing:
            messagebox.showerror(
                "Missing dependency",
                "The downloader is missing a required component:\n\n"
                + "\n".join(missing),
            )
            return False
        return True

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.rights_var.get():
            messagebox.showwarning(
                "Permission required",
                "Confirm that you own the videos or have permission to reuse them.",
            )
            return
        if not self._dependencies_ok():
            return
        try:
            batch_size = max(1, min(212, int(self.batch_var.get())))
        except (TypeError, ValueError):
            messagebox.showerror("Invalid number", "Choose a valid batch size.")
            return

        self.pause_requested.clear()
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal")
        self.progress_var.set(0)
        self.status_var.set("Loading Google Sheet…")
        self.worker = threading.Thread(
            target=self._run_batch,
            args=(
                self.sheet_var.get().strip(),
                Path(self.folder_var.get()).expanduser(),
                batch_size,
                int(self.quality_var.get()),
            ),
            daemon=True,
        )
        self.worker.start()

    def _pause(self) -> None:
        self.pause_requested.set()
        self.status_var.set("Pausing…")
        with self.process_lock:
            process = self.current_process
        if process and process.poll() is None:
            process.terminate()

    def _download_one(
        self, item: dict[str, str], output_folder: Path, quality: int
    ) -> tuple[bool, str]:
        index = int(item["index"])
        base_name = f"{index:03d} - {safe_filename(item['title'])}"
        output_template = str(output_folder / f"{base_name}.%(ext)s")
        format_selector = (
            f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={quality}]+ba/b[height<={quality}]"
        )

        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(YTDLP_DIR)
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--continue",
            "--newline",
            "--retries",
            "5",
            "--fragment-retries",
            "5",
            "--js-runtimes",
            f"node:{NODE_EXE}",
            "--ffmpeg-location",
            str(FFMPEG_EXE),
            "--format",
            format_selector,
            "--merge-output-format",
            "mp4",
            "--output",
            output_template,
            item["link"],
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            creationflags=creation_flags,
        )
        with self.process_lock:
            self.current_process = process

        assert process.stdout is not None
        for line in process.stdout:
            compact = line.strip()
            if compact:
                self._log(compact)
            percent_match = re.search(r"\[download\]\s+([0-9.]+)%", compact)
            if percent_match:
                self._ui(self.progress_var.set, float(percent_match.group(1)))

        return_code = process.wait()
        with self.process_lock:
            self.current_process = None
        final_path = output_folder / f"{base_name}.mp4"
        return return_code == 0 and final_path.exists(), str(final_path)

    def _run_batch(
        self, sheet_url: str, output_folder: Path, batch_size: int, quality: int
    ) -> None:
        try:
            output_folder.mkdir(parents=True, exist_ok=True)
            self._log("Reading links from Google Sheet…")
            rows = fetch_sheet_rows(sheet_url)
            state = load_state()
            pending = [
                item for item in rows if item["link"] not in state["completed"]
            ][:batch_size]
            if not pending:
                self._log("Everything in the Sheet is already downloaded.")
                self._ui(self.status_var.set, "Nothing left to download")
                return

            self._log(
                f"Found {len(rows)} videos. Starting {len(pending)} pending video(s)."
            )
            for position, item in enumerate(pending, start=1):
                if self.pause_requested.is_set():
                    break
                self._ui(
                    self.status_var.set,
                    f"Video {position}/{len(pending)} — {item['title'][:48]}",
                )
                self._ui(self.progress_var.set, 0)
                self._log(f"\n[{position}/{len(pending)}] {item['title']}")
                ok, final_path = self._download_one(item, output_folder, quality)
                if self.pause_requested.is_set():
                    self._log("Paused. Run Start again to resume the partial download.")
                    break
                if ok:
                    state["completed"][item["link"]] = {
                        "title": item["title"],
                        "path": final_path,
                        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    save_state(state)
                    self._log(f"Saved: {final_path}")
                    self._ui(self.progress_var.set, 100)
                else:
                    self._log("Download failed. It will be retried next time.")

            if self.pause_requested.is_set():
                self._ui(self.status_var.set, "Paused")
            else:
                self._ui(self.status_var.set, "Batch complete")
                self._log("\nBatch complete.")
        except Exception as error:
            self._log(f"ERROR: {error}")
            self._ui(self.status_var.set, "Error")
            self._ui(messagebox.showerror, "Downloader error", str(error))
        finally:
            self._ui(self.start_button.configure, {"state": "normal"})
            self._ui(self.pause_button.configure, {"state": "disabled"})

    def _on_close(self) -> None:
        self.pause_requested.set()
        with self.process_lock:
            process = self.current_process
        if process and process.poll() is None:
            process.terminate()
        self.root.destroy()


def self_check() -> int:
    checks = {
        "yt-dlp": YTDLP_DIR.exists(),
        "ffmpeg": FFMPEG_EXE.exists(),
        "node": NODE_EXE.exists(),
        "project": PROJECT_DIR.exists(),
    }
    print(json.dumps(checks, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        raise SystemExit(self_check())
    window = tk.Tk()
    DownloaderApp(window)
    window.mainloop()
