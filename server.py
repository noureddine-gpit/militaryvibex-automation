"""MilitaryVibeX Downloader v2.0 — Web Interface.

Replaces the old tkinter GUI with a browser-based dashboard served on
localhost.  Run this file and your default browser opens automatically.
"""
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
import urllib.request
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

# ── paths ────────────────────────────────────────────────────────────
APP_DIR = Path(__file__).resolve().parent
YTDLP_DIR = APP_DIR / ".yt-dlp"
FFMPEG_EXE = (
    APP_DIR / ".ffmpeg" / "imageio_ffmpeg" / "binaries"
    / "ffmpeg-win-x86_64-v7.1.exe"
)
NODE_EXE = (
    Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime"
    / "dependencies" / "node" / "bin" / "node.exe"
)
STATE_PATH = APP_DIR / "progress.json"
DEFAULT_SHEET = (
    "https://docs.google.com/spreadsheets/d/"
    "17cY-hf1V2RD10XSjMj4d_Yq9MMEcn-UX8rgDbEfdeqc/"
    "edit?gid=1421960173#gid=1421960173"
)
DEFAULT_FOLDER = str(Path.home() / "Downloads" / "MilitaryVibeX")
PORT = 8745

# ── helpers ──────────────────────────────────────────────────────────

def safe_filename(value: str, fallback: str = "video") -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:90].rstrip() or fallback


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
    req = urllib.request.Request(
        sheet_csv_url(sheet_url),
        headers={"User-Agent": "MilitaryVibeX/2.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8-sig")

    matrix = list(csv.reader(text.splitlines()))
    header_idx = next(
        (i for i, row in enumerate(matrix)
         if row and row[0].strip().lower() == "youtube link"),
        None,
    )
    if header_idx is None:
        raise ValueError("Could not find the 'YouTube link' header.")

    result: list[dict[str, str]] = []
    for src_i, row in enumerate(matrix[header_idx + 1:], start=1):
        link = row[0].strip() if row else ""
        if not link.startswith(("https://", "http://")):
            continue
        title = row[1].strip() if len(row) > 1 else f"video-{src_i:03d}"
        result.append({"link": link, "title": title, "index": str(src_i)})
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
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def check_dependencies() -> dict[str, bool]:
    return {
        "yt-dlp": YTDLP_DIR.exists(),
        "ffmpeg": FFMPEG_EXE.exists(),
        "node": NODE_EXE.exists(),
    }


# ── download engine ─────────────────────────────────────────────────

class DownloadEngine:
    """Manages the download queue and emits real-time events via SSE."""

    def __init__(self) -> None:
        self.status = "idle"
        self.progress = 0.0
        self.current_video = ""
        self.position = 0
        self.total = 0
        self.worker: threading.Thread | None = None
        self.pause_event = threading.Event()
        self.current_process: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._clients_lock = threading.Lock()

    # ── SSE pub/sub ──────────────────────────────────────────────────

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=512)
        with self._clients_lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._clients_lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def _emit(self, event: str, data: dict) -> None:
        with self._clients_lock:
            dead: list[queue.Queue] = []
            for q in self._clients:
                try:
                    q.put_nowait({"event": event, "data": data})
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._clients.remove(q)

    def _log(self, msg: str) -> None:
        self._emit("log", {"message": msg, "time": time.strftime("%H:%M:%S")})

    def _push_status(self) -> None:
        self._emit("status", self.get_status())

    # ── public API ───────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "status": self.status,
            "progress": self.progress,
            "currentVideo": self.current_video,
            "position": self.position,
            "total": self.total,
            "completedCount": len(load_state()["completed"]),
            "deps": check_dependencies(),
        }

    def start(
        self,
        sheet_url: str,
        folder: str,
        batch_size: int,
        quality: int,
    ) -> dict:
        if self.worker and self.worker.is_alive():
            return {"ok": False, "error": "Already running."}
        deps = check_dependencies()
        missing = [k for k, v in deps.items() if not v]
        if missing:
            return {"ok": False, "error": f"Missing: {', '.join(missing)}"}

        self.pause_event.clear()
        self.status = "loading"
        self.progress = 0
        self._push_status()

        self.worker = threading.Thread(
            target=self._run_batch,
            args=(sheet_url, Path(folder).expanduser(), batch_size, quality),
            daemon=True,
        )
        self.worker.start()
        return {"ok": True}

    def pause(self) -> dict:
        self.pause_event.set()
        self.status = "pausing"
        self._push_status()
        with self._proc_lock:
            p = self.current_process
        if p and p.poll() is None:
            p.terminate()
        return {"ok": True}

    # ── internals ────────────────────────────────────────────────────

    def _download_one(
        self, item: dict, out_dir: Path, quality: int
    ) -> tuple[bool, str]:
        idx = int(item["index"])
        base = f"{idx:03d} - {safe_filename(item['title'])}"
        tpl = str(out_dir / f"{base}.%(ext)s")
        fmt = (
            f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={quality}]+ba/b[height<={quality}]"
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = str(YTDLP_DIR)
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "--no-playlist", "--continue", "--newline",
            "--retries", "5", "--fragment-retries", "5",
            "--js-runtimes", f"node:{NODE_EXE}",
            "--ffmpeg-location", str(FFMPEG_EXE),
            "--format", fmt,
            "--merge-output-format", "mp4",
            "--output", tpl,
            item["link"],
        ]
        cflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=cflags,
        )
        with self._proc_lock:
            self.current_process = proc

        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line:
                self._log(line)
            m = re.search(r"\[download\]\s+([0-9.]+)%", line)
            if m:
                self.progress = float(m.group(1))
                self._push_status()

        rc = proc.wait()
        with self._proc_lock:
            self.current_process = None
        final = out_dir / f"{base}.mp4"
        return rc == 0 and final.exists(), str(final)

    def _run_batch(
        self, sheet_url: str, out_dir: Path, batch_size: int, quality: int
    ) -> None:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            self._log("Reading links from Google Sheet…")
            rows = fetch_sheet_rows(sheet_url)
            state = load_state()
            pending = [
                r for r in rows if r["link"] not in state["completed"]
            ][:batch_size]

            if not pending:
                self._log("✓ Nothing left to download — all done!")
                self.status = "complete"
                self._push_status()
                return

            self.total = len(pending)
            self.status = "downloading"
            self._log(f"Found {len(rows)} total, {len(pending)} pending.")
            self._push_status()

            for pos, item in enumerate(pending, 1):
                if self.pause_event.is_set():
                    break
                self.position = pos
                self.current_video = item["title"][:60]
                self.progress = 0
                self._push_status()
                self._log(f"\n━━ [{pos}/{len(pending)}] {item['title']}")

                ok, path = self._download_one(item, out_dir, quality)

                if self.pause_event.is_set():
                    self._log("⏸ Paused — click Start to resume.")
                    break
                if ok:
                    state["completed"][item["link"]] = {
                        "title": item["title"],
                        "path": path,
                        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    save_state(state)
                    self.progress = 100
                    self._push_status()
                    self._log(f"✓ Saved → {path}")
                else:
                    self._log("✗ Failed — will retry next run.")

            self.status = "paused" if self.pause_event.is_set() else "complete"
            if self.status == "complete":
                self._log("\n✓ Batch complete!")
        except Exception as exc:
            self._log(f"ERROR: {exc}")
            self.status = "error"
        finally:
            self.current_video = ""
            self.position = 0
            self.total = 0
            self.progress = 0
            self._push_status()


# ── HTTP server ──────────────────────────────────────────────────────

engine = DownloadEngine()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kw):
        super().__init__(*args, directory=str(APP_DIR / "web"), **kw)

    def log_message(self, *_a):
        pass  # silence default access-log noise

    # ── routing ──────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.path = "/index.html"
            return super().do_GET()
        if path == "/api/status":
            return self._json(engine.get_status())
        if path == "/api/events":
            return self._sse()
        if path == "/api/defaults":
            return self._json({
                "sheetUrl": DEFAULT_SHEET,
                "folder": DEFAULT_FOLDER,
            })
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/start":
            d = json.loads(body) if body else {}
            return self._json(engine.start(
                sheet_url=d.get("sheetUrl", DEFAULT_SHEET),
                folder=d.get("folder", DEFAULT_FOLDER),
                batch_size=max(1, min(500, int(d.get("batchSize", 10)))),
                quality=int(d.get("quality", 1080)),
            ))
        if path == "/api/pause":
            return self._json(engine.pause())
        if path == "/api/browse":
            return self._browse()
        if path == "/api/open-folder":
            d = json.loads(body) if body else {}
            folder = Path(d.get("folder", DEFAULT_FOLDER))
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(folder)
            return self._json({"ok": True})
        self.send_error(404)

    # ── helpers ──────────────────────────────────────────────────────

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def _json(self, data: dict, code: int = 200) -> None:
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = engine.subscribe()
        try:
            while True:
                try:
                    item = q.get(timeout=25)
                    self.wfile.write(
                        f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
                        .encode()
                    )
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            engine.unsubscribe(q)

    def _browse(self) -> None:
        """Spawn a small tkinter dialog in a subprocess to pick a folder."""
        script = (
            "import tkinter as tk\n"
            "from tkinter import filedialog\n"
            "root = tk.Tk()\n"
            "root.withdraw()\n"
            "root.attributes('-topmost', True)\n"
            "path = filedialog.askdirectory(title='Choose download folder')\n"
            "print(path or '')\n"
            "root.destroy()\n"
        )
        try:
            r = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._json({"ok": True, "path": r.stdout.strip()})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ── main ─────────────────────────────────────────────────────────────

def main() -> None:
    # Ensure web/ directory exists
    web_dir = APP_DIR / "web"
    if not (web_dir / "index.html").exists():
        print("ERROR: web/index.html not found. Place the web UI files in", web_dir)
        input("Press Enter to exit…")
        return

    port = PORT
    for _ in range(20):
        try:
            srv = ThreadedServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
    else:
        print("Could not find an available port.")
        input("Press Enter to exit…")
        return

    url = f"http://localhost:{port}"
    print(f"MilitaryVibeX Downloader v2.0")
    print(f"Running at  {url}")
    print(f"Press Ctrl+C to stop.\n")

    webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
