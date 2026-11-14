#!/usr/bin/env python3
"""Windows desktop launcher for the self-contained Story Tools build."""

from __future__ import annotations

import os
import secrets
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, simpledialog


APP_HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "StoryTools"
CONFIG_FILE = APP_HOME / ".env"
URL = "http://127.0.0.1:8765"


def read_config() -> dict[str, str]:
    values: dict[str, str] = {}
    if CONFIG_FILE.exists():
        for raw in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def write_config(values: dict[str, str]) -> None:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        "\n".join(
            [
                f"PEXELS_API_KEY={values.get('PEXELS_API_KEY', '')}",
                f"SECRET_KEY={values.get('SECRET_KEY') or secrets.token_urlsafe(48)}",
                "WHISPER_MODEL=small",
                r"FONT_FILE=C:\Windows\Fonts\arial.ttf",
                "",
            ]
        ),
        encoding="utf-8",
    )


class StoryToolsDesktop:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Story Tools Studio")
        self.root.geometry("560x330")
        self.root.resizable(False, False)
        self.root.configure(bg="#080b16")
        self.server = None
        self.server_thread: threading.Thread | None = None
        self._build_ui()
        self._ensure_first_run_config()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(150, self.start_server)

    def _build_ui(self) -> None:
        tk.Label(
            self.root,
            text="STORY TOOLS",
            fg="#9f7aea",
            bg="#080b16",
            font=("Segoe UI Semibold", 11),
        ).pack(pady=(34, 4))
        tk.Label(
            self.root,
            text="Your story studio is starting",
            fg="#f7f8ff",
            bg="#080b16",
            font=("Segoe UI Semibold", 23),
        ).pack()
        self.status = tk.Label(
            self.root,
            text="Preparing the video engine…",
            fg="#a7b0c9",
            bg="#080b16",
            font=("Segoe UI", 11),
        )
        self.status.pack(pady=(12, 25))
        row = tk.Frame(self.root, bg="#080b16")
        row.pack()
        self.open_button = tk.Button(
            row,
            text="Open Story Tools",
            command=self.open_browser,
            state="disabled",
            bg="#7c3aed",
            activebackground="#8b5cf6",
            fg="white",
            activeforeground="white",
            relief="flat",
            padx=24,
            pady=11,
            font=("Segoe UI Semibold", 10),
            cursor="hand2",
        )
        self.open_button.pack(side="left", padx=7)
        tk.Button(
            row,
            text="Pexels settings",
            command=self.edit_settings,
            bg="#181d31",
            activebackground="#242b45",
            fg="#e7e9f4",
            activeforeground="white",
            relief="flat",
            padx=20,
            pady=11,
            font=("Segoe UI", 10),
            cursor="hand2",
        ).pack(side="left", padx=7)
        tk.Label(
            self.root,
            text="Keep this window open while creating videos.",
            fg="#68728e",
            bg="#080b16",
            font=("Segoe UI", 9),
        ).pack(side="bottom", pady=25)

    def _ensure_first_run_config(self) -> None:
        values = read_config()
        if not CONFIG_FILE.exists():
            key = simpledialog.askstring(
                "Free stock-video access",
                "Paste a Pexels API key for automatic stock-video search.\n"
                "You can leave this empty and add it later in Pexels settings.",
                parent=self.root,
            )
            values["PEXELS_API_KEY"] = (key or "").strip()
            write_config(values)

    def edit_settings(self) -> None:
        values = read_config()
        key = simpledialog.askstring(
            "Pexels settings",
            "Pexels API key:",
            initialvalue=values.get("PEXELS_API_KEY", ""),
            parent=self.root,
        )
        if key is None:
            return
        values["PEXELS_API_KEY"] = key.strip()
        write_config(values)
        messagebox.showinfo(
            "Settings saved",
            "Restart Story Tools to apply the new Pexels key.",
            parent=self.root,
        )

    def start_server(self) -> None:
        self.status.configure(text="Loading narration and video tools…")

        def run() -> None:
            try:
                import uvicorn
                from story_tools import app

                config = uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=8765,
                    log_level="warning",
                    log_config=None,
                    access_log=False,
                    loop="asyncio",
                    http="h11",
                )
                self.server = uvicorn.Server(config)
                self.root.after(0, self._ready)
                self.server.run()
            except Exception as exc:
                APP_HOME.mkdir(parents=True, exist_ok=True)
                (APP_HOME / "startup_error.log").write_text(
                    traceback.format_exc(), encoding="utf-8"
                )
                self.root.after(0, lambda: self._failed(str(exc)))

        self.server_thread = threading.Thread(target=run, daemon=True)
        self.server_thread.start()

    def _ready(self) -> None:
        self.status.configure(text="Ready — Arabic, Darija and English studio is running")
        self.open_button.configure(state="normal")
        self.root.after(450, self.open_browser)

    def _failed(self, detail: str) -> None:
        self.status.configure(text="Story Tools could not start")
        messagebox.showerror("Startup error", detail, parent=self.root)

    @staticmethod
    def open_browser() -> None:
        webbrowser.open(URL)

    def close(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    StoryToolsDesktop().run()
