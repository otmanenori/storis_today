#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
STORY TOOLS – FINAL VERSION (ALL FIXES + FAVICON)
Single‑file local story‑to‑short‑video application.
Python 3.12.x – Run: python story_tools.py – Open: http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, Response
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware

# ---- Translation & Transliteration ----
try:
    from deep_translator import GoogleTranslator
    _translator = GoogleTranslator(source='auto', target='en')
    _translate_available = True
except ImportError:
    _translate_available = False

try:
    from transliterate import translit
    _translit_available = True
except ImportError:
    _translit_available = False

# ---- Text-to-Speech ----
# Prefer edge-tts for high quality, fallback to gTTS
try:
    import edge_tts
    _tts_available = True
    _tts_engine = "edge"
except ImportError:
    try:
        from gtts import gTTS
        _tts_available = True
        _tts_engine = "gtts"
    except ImportError:
        _tts_available = False
        _tts_engine = None

# ============================================================
# PATHS / CONFIG
# ============================================================

IS_FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_HOME = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "StoryTools"
    if IS_FROZEN
    else Path(__file__).resolve().parent
)
BASE_DIR = RESOURCE_DIR
DATA_DIR = APP_HOME / "data"
PROJECTS_DIR = DATA_DIR / "projects"
FFMPEG_DIR = RESOURCE_DIR / "ffmpeg" / "bin"
FFMPEG_EXE = FFMPEG_DIR / "ffmpeg.exe"
FFPROBE_EXE = FFMPEG_DIR / "ffprobe.exe"
DB_PATH = DATA_DIR / "story_tools.db"
TTS_CONFIG_FILE = APP_HOME / "tts_providers.json"
ENV_CONFIG_FILE = APP_HOME / ".env"

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# Add ffmpeg/bin to PATH so whisper and ffmpeg can be found
if FFMPEG_DIR.exists():
    os.environ["PATH"] = str(FFMPEG_DIR) + os.pathsep + os.environ.get("PATH", "")

load_dotenv(APP_HOME / ".env")

SECRET_KEY = os.getenv("SECRET_KEY", "")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
DEFAULT_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
FONT_FILE_ENV = os.getenv("FONT_FILE", r"C:\Windows\Fonts\arial.ttf")

MAX_AUDIO_BYTES = 250 * 1024 * 1024
MAX_SRT_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 500 * 1024 * 1024

ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".webm"}
ALLOWED_SRT_EXT = {".srt"}

# Use sha256_crypt – built‑in, no external dependency
pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")
executor = ThreadPoolExecutor(max_workers=2)
progress_lock = threading.Lock()
progress: dict[str, dict[str, Any]] = {}
whisper_models: dict[str, Any] = {}
whisper_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("story-tools")

app = FastAPI(title="Story Tools", version="1.0.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY or "temporary-local-secret-change-me",
    session_cookie="story_tools_session",
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 24 * 7,
)

# ---- Favicon (to avoid 404) ----
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)  # No content, no log noise


# ============================================================
# STARTUP CHECKS
# ============================================================

def python_version_ok() -> bool:
    return sys.version_info[:2] == (3, 12)


def startup_checks() -> None:
    logger.info("=" * 40)
    logger.info("       STORY TOOLS (FINAL)")
    logger.info("=" * 40)
    logger.info("Python: %s", sys.version.split()[0])
    logger.info("FFmpeg: %s", "OK" if FFMPEG_EXE.exists() else "MISSING")
    logger.info("FFprobe: %s", "OK" if FFPROBE_EXE.exists() else "MISSING")
    logger.info("Pexels: %s", "Configured" if PEXELS_API_KEY else "Not configured")
    logger.info("TTS: %s", _tts_engine if _tts_available else "Not installed (install edge-tts or gTTS)")
    logger.info("Server: http://127.0.0.1:8000")
    logger.info("=" * 40)

    if not python_version_ok():
        logger.warning("Story Tools targets Python 3.12.x. Current: %s", sys.version.split()[0])
    if FFMPEG_EXE.exists():
        logger.info("FFmpeg detected")
    if FFPROBE_EXE.exists():
        logger.info("FFprobe detected")
    if not SECRET_KEY:
        logger.warning("SECRET_KEY is not configured. Set it in .env before production use.")


# ============================================================
# DATABASE
# ============================================================

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mode TEXT NOT NULL,
                output_format TEXT NOT NULL DEFAULT '9:16',
                audio_name TEXT,
                srt_name TEXT,
                status TEXT NOT NULL DEFAULT 'Draft',
                progress INTEGER NOT NULL DEFAULT 0,
                current_task TEXT DEFAULT '',
                final_output TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_message TEXT DEFAULT '',
                burn_subtitles INTEGER NOT NULL DEFAULT 0,
                language TEXT NOT NULL DEFAULT 'auto',
                script TEXT,
                tts_voice TEXT DEFAULT 'female',
                tts_style TEXT DEFAULT 'story',
                tts_provider TEXT DEFAULT 'edge',
                music_name TEXT,
                music_volume REAL NOT NULL DEFAULT 0.12
            );
            """
        )
        # Migrations
        migrations = {
            "burn_subtitles": "INTEGER NOT NULL DEFAULT 0",
            "language": "TEXT NOT NULL DEFAULT 'auto'",
            "script": "TEXT",
            "tts_voice": "TEXT DEFAULT 'female'",
            "tts_style": "TEXT DEFAULT 'story'",
            "tts_provider": "TEXT DEFAULT 'edge'",
            "music_name": "TEXT",
            "music_volume": "REAL NOT NULL DEFAULT 0.12",
        }
        for col, definition in migrations.items():
            try:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        # Ensure defaults
        conn.execute("UPDATE projects SET burn_subtitles = 0 WHERE burn_subtitles IS NULL")
        conn.execute("UPDATE projects SET tts_voice = 'female' WHERE tts_voice IS NULL")
        conn.execute("UPDATE projects SET tts_style = 'story' WHERE tts_style IS NULL")
        conn.execute("UPDATE projects SET tts_provider = 'edge' WHERE tts_provider IS NULL")
        conn.execute("UPDATE projects SET music_volume = 0.12 WHERE music_volume IS NULL")


def admin_exists() -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def create_admin(username: str, password: str) -> None:
    username = username.strip().lower()
    if len(username) < 3:
        raise ValueError("Username/email is too short.")
    if len(password) < 8:
        raise ValueError("Password must contain at least 8 characters.")
    password_hash = pwd_context.hash(password)
    now = utc_now()
    with db() as conn:
        conn.execute(
            "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
            (username, password_hash, now),
        )


def verify_admin(username: str, password: str) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE username=?",
            (username.strip().lower(),),
        ).fetchone()
    if not row:
        return False
    try:
        return pwd_context.verify(password, row["password_hash"])
    except Exception:
        return False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_row(project_id: str) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()


def project_json(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def read_tts_config() -> dict[str, Any]:
    try:
        value = json.loads(TTS_CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("providers", []), list):
            return value
    except (OSError, json.JSONDecodeError):
        pass
    return {"providers": [], "default_provider": "edge"}


def save_tts_config(value: dict[str, Any]) -> None:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    temporary = TTS_CONFIG_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(TTS_CONFIG_FILE)


def tts_provider_by_id(provider_id: str) -> Optional[dict[str, Any]]:
    return next(
        (p for p in read_tts_config().get("providers", []) if p.get("id") == provider_id),
        None,
    )


def available_tts_providers() -> list[dict[str, str]]:
    providers: list[dict[str, str]] = []
    if _tts_engine == "edge":
        providers.append({"id": "edge", "name": "Microsoft Edge Neural (free)"})
    if _tts_available:
        providers.append({"id": "gtts", "name": "Google Translate (free)"})
    providers.extend(
        {"id": p["id"], "name": p["name"]}
        for p in read_tts_config().get("providers", [])
        if p.get("id") and p.get("name") and p.get("base_url") and p.get("model")
    )
    return providers


def normalize_blur_regions(regions: Any) -> list[dict[str, float]]:
    if regions is None:
        return []
    if not isinstance(regions, list) or len(regions) > 12:
        raise ValueError("A scene can contain up to 12 blur areas.")
    normalized = []
    for region in regions:
        if not isinstance(region, dict):
            raise ValueError("Invalid blur area.")
        try:
            x, y = float(region["x"]), float(region["y"])
            width = float(region.get("width", region.get("w")))
            height = float(region.get("height", region.get("h")))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Each blur area needs x, y, width and height.") from exc
        if not all(map(lambda n: 0 <= n <= 1, (x, y))) or not all(
            map(lambda n: 0 < n <= 1, (width, height))
        ) or x + width > 1.000001 or y + height > 1.000001:
            raise ValueError("Blur areas must fit inside the video frame.")
        normalized.append({"x": x, "y": y, "width": width, "height": height})
    return normalized


def public_tts_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    config = config or read_tts_config()
    return {
        "providers": [
            {k: p.get(k, "") for k in ("id", "name", "base_url", "model", "default_voice", "response_format")}
            | {"has_api_key": bool(p.get("api_key"))}
            for p in config.get("providers", [])
        ],
        "default_provider": config.get("default_provider", "edge"),
        "available": available_tts_providers(),
        "pexels_api_key_set": bool(PEXELS_API_KEY),
    }


def save_pexels_api_key(value: str) -> None:
    APP_HOME.mkdir(parents=True, exist_ok=True)
    lines = ENV_CONFIG_FILE.read_text(encoding="utf-8").splitlines() if ENV_CONFIG_FILE.exists() else []
    output = []
    found = False
    for line in lines:
        if line.strip().startswith("PEXELS_API_KEY="):
            if value:
                output.append(f"PEXELS_API_KEY={value}")
            found = True
        else:
            output.append(line)
    if value and not found:
        output.append(f"PEXELS_API_KEY={value}")
    ENV_CONFIG_FILE.write_text("\n".join(output) + "\n", encoding="utf-8")
    global PEXELS_API_KEY
    PEXELS_API_KEY = value


def build_blur_filter(fmt: str, blur: bool, blur_regions: Any = None) -> tuple[str, str]:
    """Return an FFmpeg filter graph and its output label for normalized blur areas."""
    width, height = output_size(fmt)
    regions = normalize_blur_regions(blur_regions)
    base = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps=30,"
        "eq=contrast=1.035:saturation=1.055:brightness=0.005,unsharp=5:5:0.25"
    )
    if blur:
        return base + ",boxblur=14:2", "0:v"
    if not regions:
        return base, "0:v"
    graph = [f"[0:v]{base},split=2[main0][blur0src]"]
    previous = "main0"
    for index, region in enumerate(regions):
        x = int(region["x"] * width) // 2 * 2
        y = int(region["y"] * height) // 2 * 2
        w = max(2, int(region["width"] * width) // 2 * 2)
        h = max(2, int(region["height"] * height) // 2 * 2)
        w = min(w, width - x) // 2 * 2
        h = min(h, height - y) // 2 * 2
        graph.append(f"[blur{index}src]crop={w}:{h}:{x}:{y},boxblur=18:3[region{index}]")
        if index + 1 < len(regions):
            graph.append(f"[{previous}][region{index}]overlay={x}:{y},split=2[main{index+1}][blur{index+1}src]")
            previous = f"main{index+1}"
        else:
            graph.append(f"[{previous}][region{index}]overlay={x}:{y}[blurred]")
    return ";".join(graph), "blurred"


def update_project(project_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utc_now()
    keys = list(fields)
    values = [fields[k] for k in keys]
    sql = "UPDATE projects SET " + ", ".join(f"{k}=?" for k in keys) + " WHERE id=?"
    with db() as conn:
        conn.execute(sql, (*values, project_id))


def require_login(request: Request) -> None:
    if not request.session.get("user"):
        raise HTTPException(status_code=401, detail="Authentication required.")


# ============================================================
# FILE / PROJECT HELPERS
# ============================================================

def safe_name(name: str, fallback: str = "file") -> str:
    name = Path(name or "").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("._")
    ext = re.sub(r"[^A-Za-z0-9.]", "", Path(name).suffix.lower())
    return (stem or fallback)[:100] + ext


def project_dir(project_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12,40}", project_id):
        raise HTTPException(status_code=400, detail="Invalid project id.")
    p = PROJECTS_DIR / project_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def project_videos_dir(project_id: str) -> Path:
    p = project_dir(project_id) / "videos"
    p.mkdir(exist_ok=True)
    return p


def validate_upload_size(file: UploadFile, max_bytes: int) -> None:
    if file.filename and len(file.filename) > 180:
        raise HTTPException(status_code=400, detail="Filename is too long.")


async def save_upload(file: UploadFile, destination: Path, max_bytes: int) -> None:
    total = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail="File is too large.")
                out.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def set_progress(project_id: str, percent: int, task: str, status: Optional[str] = None) -> None:
    percent = max(0, min(100, int(percent)))
    with progress_lock:
        progress[project_id] = {"percent": percent, "task": task, "status": status}
    fields = {"progress": percent, "current_task": task}
    if status:
        fields["status"] = status
    update_project(project_id, **fields)


def get_progress(project_id: str) -> dict[str, Any]:
    row = project_row(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found.")
    with progress_lock:
        live = progress.get(project_id)
    if live:
        return live
    return {
        "percent": row["progress"],
        "task": row["current_task"] or "",
        "status": row["status"],
    }


# ============================================================
# SRT
# ============================================================

TIME_RE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{3})"
)


def time_to_seconds(value: str) -> float:
    m = TIME_RE.fullmatch(value.strip())
    if not m:
        raise ValueError(f"Invalid SRT time: {value}")
    return (
        int(m.group("h")) * 3600
        + int(m.group("m")) * 60
        + int(m.group("s"))
        + int(m.group("ms")) / 1000
    )


def seconds_to_srt(value: float) -> str:
    value = max(0.0, float(value))
    ms_total = int(round(value * 1000))
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt_text(text: str) -> list[dict[str, Any]]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = re.split(r"\n\s*\n", text.strip())
    scenes = []
    for block in blocks:
        lines = block.split("\n")
        if len(lines) < 2:
            continue
        idx = None
        try:
            idx = int(lines[0].strip())
            timing_line = lines[1].strip()
            text_lines = lines[2:]
        except ValueError:
            timing_line = lines[0].strip()
            text_lines = lines[1:]
        match = re.match(
            r"(.+?)\s*-->\s*(.+?)(?:\s+.*)?$",
            timing_line,
        )
        if not match:
            continue
        try:
            start = time_to_seconds(match.group(1))
            end = time_to_seconds(match.group(2))
        except ValueError:
            continue
        if end < start:
            continue
        scene = {
            "index": idx or (len(scenes) + 1),
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(max(0, end - start), 3),
            "text": "\n".join(text_lines).strip(),
            "query": "",
            "selected_video": None,
            "videos": [],
            "blur": False,
            "blur_regions": [],
        }
        scenes.append(scene)
    for i, scene in enumerate(scenes, 1):
        scene["index"] = i
    return scenes


def scenes_to_srt(scenes: list[dict[str, Any]]) -> str:
    chunks = []
    for i, scene in enumerate(scenes, 1):
        start = float(scene["start"])
        end = float(scene["end"])
        text = str(scene.get("text", "")).strip()
        if end < start:
            raise ValueError(f"Subtitle {i}: end time is before start time.")
        chunks.append(
            f"{i}\n{seconds_to_srt(start)} --> {seconds_to_srt(end)}\n{text}"
        )
    return "\n\n".join(chunks) + ("\n" if chunks else "")


def merge_short_scenes(scenes: list[dict[str, Any]], language: str) -> list[dict[str, Any]]:
    """Avoid frantic one-second stock clips while keeping speech timing intact."""
    merged: list[dict[str, Any]] = []
    for scene in scenes:
        if merged:
            previous = merged[-1]
            combined_duration = float(scene["end"]) - float(previous["start"])
            combined_words = len((previous["text"] + " " + scene["text"]).split())
            if float(previous["duration"]) < 3.0 and combined_duration <= 8.0 and combined_words <= 20:
                previous["end"] = scene["end"]
                previous["duration"] = round(combined_duration, 3)
                previous["text"] = (previous["text"] + " " + scene["text"]).strip()
                previous["query"] = make_query(previous["text"], language)
                continue
        merged.append(dict(scene))
    if len(merged) > 1 and float(merged[-1]["duration"]) < 2.0:
        last = merged.pop()
        previous = merged[-1]
        previous["end"] = last["end"]
        previous["duration"] = round(float(previous["end"]) - float(previous["start"]), 3)
        previous["text"] = (previous["text"] + " " + last["text"]).strip()
        previous["query"] = make_query(previous["text"], language)
    for index, scene in enumerate(merged, 1):
        scene["index"] = index
    return merged


# ============================================================
# TRANSLATION HELPERS
# ============================================================

def translate_to_english(text: str) -> str:
    if not _translate_available:
        return text
    try:
        if re.search(r'[A-Za-z]{3,}', text) and not re.search(r'[\u0600-\u06FF]', text):
            return text
        result = _translator.translate(text)
        result = (result or "").strip()
        # Some unofficial translation endpoints return an HTML/error message
        # with HTTP 200. Never turn that message into a stock-video query.
        lowered = result.lower()
        if not result or any(marker in lowered for marker in ("error 500", "server error", "too many requests", "service unavailable")):
            return text
        return result
    except Exception as e:
        logger.error("Translation failed: %s", e)
        return text


def transliterate_arabic(text: str) -> str:
    if not _translit_available:
        return text
    try:
        return translit(text, 'ar', reversed=True)
    except Exception:
        # The third-party package is installed but commonly ships without an
        # Arabic language pack. This is an optional fallback, not an app error.
        return text


# ============================================================
# VISUAL KEYWORD GENERATOR (with translation/transliteration)
# ============================================================

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "as", "at", "by", "from", "that", "this",
    "these", "those", "it", "its", "be", "has", "have", "had", "where",
    "when", "while", "into", "over", "under", "their", "they", "them",
    "you", "your", "we", "our", "can", "will", "a", "an",
    "did", "ever", "entire", "pretty", "something", "anything", "very",
    "just", "one", "once", "upon", "time", "really", "every",
    "من", "في", "و", "أو", "على", "إلى", "عن", "مع", "هذا", "هذه", "ذلك",
    "تلك", "هو", "هي", "هم", "كما", "حيث", "عندما", "ثم", "قد", "أن",
}

# Frequent Modern Standard Arabic and Moroccan Darija concepts. These are used
# before the online translator so visual searches still work when a sentence
# contains dialect words that generic translation services often misunderstand.
ARABIC_VISUAL_TERMS = {
    "رجل": "man", "امرأة": "woman", "طفل": "child", "بنت": "girl", "ولد": "boy",
    "عائلة": "family", "مدينة": "city", "قرية": "village", "منزل": "house",
    "بيت": "house", "مدرسة": "school", "مستشفى": "hospital", "طريق": "road",
    "سيارة": "car", "قطار": "train", "طائرة": "airplane", "بحر": "ocean",
    "شاطئ": "beach", "جبل": "mountain", "غابة": "forest", "صحراء": "desert",
    "ليل": "night", "نهار": "day", "مطر": "rain", "ثلج": "snow",
    "حزين": "sad person", "سعيد": "happy person", "خائف": "frightened person",
    "خدمة": "work", "خدام": "worker", "فلوس": "money", "زنقة": "street",
    "طوموبيل": "car", "دار": "home", "دوار": "rural village", "بوليس": "police",
    "حوت": "fish", "مرا": "woman", "راجل": "man", "درية": "young girl",
    "حيات": "life change", "تخيل": "thoughtful person", "حلم": "dreaming person",
    "وحيد": "lonely person", "خوف": "frightened person", "خاف": "frightened person",
    "بكى": "crying person", "بكاء": "crying person", "فرح": "happy person",
    "أمل": "hopeful person", "امل": "hopeful person", "وقع": "dramatic event",
    "هاتف": "smartphone", "تلفون": "smartphone", "باب": "door",
    "مكتب": "office", "مطعم": "restaurant", "سوق": "market",
}


def make_query(text: str, language: str = "auto") -> str:
    has_arabic = bool(re.search(r'[\u0600-\u06FF]', text))

    if has_arabic:
        local_terms = []
        for arabic, english in ARABIC_VISUAL_TERMS.items():
            if arabic in text and english not in local_terms:
                local_terms.append(english)
        if _translate_available:
            translated = translate_to_english(text)
            if translated != text:
                text_for_keywords = translated
            elif _translit_available:
                text_for_keywords = transliterate_arabic(text)
            else:
                text_for_keywords = text
        elif _translit_available:
            text_for_keywords = transliterate_arabic(text)
        else:
            text_for_keywords = text
        if local_terms:
            text_for_keywords = " ".join(local_terms) + " " + text_for_keywords
    else:
        text_for_keywords = text

    words = re.findall(r"[A-Za-z0-9'-]+", text_for_keywords.lower())
    useful = [w for w in words if len(w) > 2 and w not in STOPWORDS]
    seen = set()
    picked = []
    for word in useful:
        if word not in seen:
            seen.add(word)
            picked.append(word)
        if len(picked) >= 5:
            break
    query = " ".join(picked)
    if not query:
        if has_arabic:
            query = "arabic scene"
        else:
            query = "nature landscape cinematic"
    return (query + " cinematic realistic").strip()[:120]


# ============================================================
# TTS (Text-to-Speech) – high quality with edge-tts (async)
# ============================================================

LANGUAGE_MAP = {
    "auto": None,
    "Arabic": "ar",
    "English": "en",
    "French": "fr",
    "Spanish": "es",
}

# Verified free Microsoft neural voices. Moroccan is the default; nearby Arabic
# voices are useful because the most natural speaker varies with the script.
EDGE_VOICES = {
    "en": {"female": "en-US-JennyNeural", "male": "en-US-ChristopherNeural"},
    "ar": {"female": "ar-MA-MounaNeural", "male": "ar-MA-JamalNeural"},
    "fr": {"female": "fr-FR-DeniseNeural", "male": "fr-FR-HenriNeural"},
    "es": {"female": "es-ES-ElviraNeural", "male": "es-ES-AlvaroNeural"},
}
ARABIC_VOICE_IDS = {
    "ma_female": "ar-MA-MounaNeural", "ma_male": "ar-MA-JamalNeural",
    "dz_female": "ar-DZ-AminaNeural", "dz_male": "ar-DZ-IsmaelNeural",
    "tn_female": "ar-TN-ReemNeural", "tn_male": "ar-TN-HediNeural",
    "eg_female": "ar-EG-SalmaNeural", "eg_male": "ar-EG-ShakirNeural",
    "sa_female": "ar-SA-ZariyahNeural", "sa_male": "ar-SA-HamedNeural",
}
ENGLISH_VOICE_IDS = {
    "en_us_female": "en-US-AriaNeural", "en_us_male": "en-US-GuyNeural",
    "en_uk_female": "en-GB-SoniaNeural", "en_uk_male": "en-GB-RyanNeural",
}
VOICE_IDS = {**ARABIC_VOICE_IDS, **ENGLISH_VOICE_IDS}
VOICE_STYLES = {
    "story": ("-8%", "-2Hz"),
    "cinematic": ("-13%", "-5Hz"),
    "energetic": ("+4%", "+1Hz"),
    "calm": ("-16%", "-1Hz"),
}
DARJA_LATIN_WORDS = {
    "ana": "أنا", "nta": "نتا", "nti": "نتي", "hna": "حنا", "ntoma": "نتوما",
    "wach": "واش", "3lach": "علاش", "fin": "فين", "kifach": "كيفاش",
    "chno": "شنو", "ach": "آش", "daba": "دابا", "bzaf": "بزاف",
    "mzyan": "مزيان", "khayb": "خايب", "baghi": "باغي", "bghit": "بغيت",
    "ghadi": "غادي", "dyal": "ديال", "dial": "ديال", "hit": "حيت",
    "walakin": "ولكن", "safi": "صافي", "kayn": "كاين", "makaynch": "ما كاينش",
    "flous": "فلوس", "dar": "دار", "zen9a": "زنقة", "tomobil": "طوموبيل",
}
EDGE_FALLBACK_VOICE = "en-US-JennyNeural"

def prepare_text_for_speech(script: str, language_code: str) -> str:
    """Improve pauses and common Latin Darija without changing saved captions."""
    text = unicodedata.normalize("NFKC", script).replace("…", "...")
    if language_code == "ar":
        def replace_darja(match: re.Match) -> str:
            token = match.group(0)
            return DARJA_LATIN_WORDS.get(token.lower(), token)
        text = re.sub(r"(?<!\w)[A-Za-z0-9]+(?!\w)", replace_darja, text)
        text = re.sub(r"\s*[,،]\s*", "، ", text)
        text = re.sub(r"\s*[;؛]\s*", "؛ ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if text and text[-1] not in ".!?؟":
        text += "."
    return text


async def generate_audio_with_edge(script: str, output_path: Path, language_code: str,
                                   voice_choice: str = "female", style: str = "story") -> None:
    """Generate natural neural narration with selectable region and delivery."""
    voices = EDGE_VOICES.get(language_code, EDGE_VOICES["en"])
    if voice_choice in VOICE_IDS:
        voice = VOICE_IDS[voice_choice]
    else:
        gender = "male" if voice_choice == "male" or voice_choice.endswith("_male") else "female"
        voice = voices.get(gender, EDGE_FALLBACK_VOICE)
    rate, pitch = VOICE_STYLES.get(style, VOICE_STYLES["story"])
    speech_text = prepare_text_for_speech(script, language_code)
    communicate = edge_tts.Communicate(speech_text, voice, rate=rate, pitch=pitch, volume="+0%")
    boundaries = []
    with open(output_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "SentenceBoundary":
                boundaries.append({
                    "start": round(float(chunk["offset"]) / 10_000_000, 4),
                    "end": round(float(chunk["offset"] + chunk["duration"]) / 10_000_000, 4),
                    "text": chunk.get("text", ""),
                })
    write_json(output_path.parent / "tts_boundaries.json", boundaries)


async def generate_audio_with_compatible_provider(
    script: str, output_path: Path, provider: dict[str, Any], voice: str
) -> None:
    """Call providers implementing the common OpenAI audio/speech request format."""
    base_url = str(provider.get("base_url", "")).rstrip("/")
    if not base_url.endswith("/audio/speech"):
        base_url += "/audio/speech"
    headers = {"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"}
    payload = {
        "model": provider["model"],
        "input": script,
        "voice": voice or provider.get("default_voice", "alloy"),
        "response_format": provider.get("response_format", "mp3"),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0)) as client:
        response = await client.post(base_url, headers=headers, json=payload)
    if response.is_error:
        detail = response.text[:500] if response.text else f"HTTP {response.status_code}"
        raise RuntimeError(f"TTS provider rejected the request: {detail}")
    if not response.content:
        raise RuntimeError("TTS provider returned an empty audio file.")
    output_path.write_bytes(response.content)


async def generate_audio_from_preview(
    script: str, output_path: Path, language_code: str, voice_choice: str, style: str,
    provider_id: str,
) -> None:
    if provider_id == "edge" and _tts_engine == "edge":
        await generate_audio_with_edge(script, output_path, language_code, voice_choice, style)
        return
    if provider_id == "gtts" and _tts_available:
        from gtts import gTTS
        await asyncio.to_thread(lambda: gTTS(
            text=prepare_text_for_speech(script, language_code), lang=language_code, slow=False
        ).save(str(output_path)))
        return
    raise RuntimeError("The selected speech provider is not available.")

async def generate_audio_from_script(project_id: str, script: str, language: str,
                                     voice_choice: str = "female", style: str = "story",
                                     provider_id: str = "edge", custom_voice: str = "") -> bool:
    """
    Generate high-quality TTS audio using edge-tts (if available),
    otherwise fallback to gTTS (run in thread).
    """
    pdir = project_dir(project_id)
    audio_path = pdir / "audio.mp3"
    lang_code = LANGUAGE_MAP.get(language, "en")
    if lang_code is None:
        lang_code = "ar" if re.search(r"[\u0600-\u06FF]", script) else "en"
    fallback_text = prepare_text_for_speech(script, lang_code)

    if provider_id not in {"edge", "gtts"}:
        provider = tts_provider_by_id(provider_id)
        if not provider or not provider.get("api_key"):
            raise RuntimeError("Choose a configured text-to-speech provider with a valid API key.")
        voice = custom_voice.strip() or provider.get("default_voice", "alloy")
        await generate_audio_with_compatible_provider(fallback_text, audio_path, provider, voice)
        logger.info("Configured TTS provider generated audio for project %s", project_id)
        return True
    if provider_id == "edge" and _tts_engine == "edge":
        try:
            await generate_audio_with_edge(script, audio_path, lang_code, voice_choice, style)
            logger.info("Edge-TTS audio generated for project %s", project_id)
            return True
        except Exception as e:
            logger.error("Edge-TTS failed: %s, falling back to gTTS", e)
            try:
                from gtts import gTTS
                def make_gtts():
                    tts = gTTS(text=fallback_text, lang=lang_code, slow=False)
                    tts.save(str(audio_path))
                await asyncio.to_thread(make_gtts)
                logger.info("gTTS fallback audio generated for project %s", project_id)
                return True
            except Exception as e2:
                logger.error("gTTS fallback also failed: %s", e2)
                raise RuntimeError(f"TTS failed: {e2}")
    elif provider_id == "gtts" and _tts_available:
        try:
            from gtts import gTTS
            def make_gtts():
                tts = gTTS(text=fallback_text, lang=lang_code, slow=False)
                tts.save(str(audio_path))
            await asyncio.to_thread(make_gtts)
            logger.info("gTTS audio generated for project %s", project_id)
            return True
        except Exception as e:
            logger.error("gTTS generation failed: %s", e)
            raise RuntimeError(f"TTS failed: {e}")
    else:
        raise RuntimeError("No TTS engine available. Install edge-tts or gTTS.")


def split_script_for_scenes(script: str, max_words: int = 13) -> list[str]:
    """Keep the user's exact Arabic/text while creating short visual scenes."""
    clean = re.sub(r"[ \t]+", " ", script.replace("\r", "\n")).strip()
    # Commas stay inside a scene; splitting on every Arabic comma creates tiny,
    # visually meaningless clips such as "once upon a time".
    sentences = [x.strip() for x in re.split(r"(?<=[.!?؟؛])\s+|\n+", clean) if x.strip()]
    chunks: list[str] = []
    for sentence in sentences:
        words = sentence.split()
        if len(words) <= max_words:
            if words:
                chunks.append(" ".join(words))
            continue
        # Balance long sentences so the last scene never contains one stray word.
        part_count = (len(words) + max_words - 1) // max_words
        base, extra = divmod(len(words), part_count)
        cursor = 0
        for part in range(part_count):
            size = base + (1 if part < extra else 0)
            chunks.append(" ".join(words[cursor:cursor + size]))
            cursor += size
    return chunks or ([clean] if clean else [])


def create_script_subtitles(project_id: str, script: str, language: str) -> list[dict[str, Any]]:
    """Create SRT directly from the script and the generated narration length."""
    pdir = project_dir(project_id)
    audio_duration = media_duration(pdir / "audio.mp3")
    clean = re.sub(r"[ \t]+", " ", script.replace("\r", "\n")).strip()
    sentences = [x.strip() for x in re.split(r"(?<=[.!?؟؛])\s+|\n+", clean) if x.strip()]
    chunks = split_script_for_scenes(script)
    if not chunks or audio_duration <= 0:
        raise RuntimeError("Could not create timed subtitles from the script.")
    boundaries = read_json(pdir / "tts_boundaries.json", [])
    use_boundaries = len(boundaries) == len(sentences) and bool(boundaries)
    timeline: list[tuple[str, float, float]] = []
    if use_boundaries:
        for sentence_index, sentence in enumerate(sentences):
            sentence_chunks = split_script_for_scenes(sentence)
            span_start = 0.0 if sentence_index == 0 else float(boundaries[sentence_index]["start"])
            span_end = audio_duration if sentence_index == len(sentences) - 1 else float(boundaries[sentence_index + 1]["start"])
            weights = [max(1, len(x.split())) for x in sentence_chunks]
            cursor = span_start
            for part_index, (part, weight) in enumerate(zip(sentence_chunks, weights)):
                end = span_end if part_index == len(sentence_chunks) - 1 else cursor + (span_end - span_start) * weight / sum(weights)
                timeline.append((part, cursor, end))
                cursor = end
    else:
        weights = [max(1, len(re.findall(r"\S+", chunk))) for chunk in chunks]
        cursor = 0.0
        for index, (chunk, weight) in enumerate(zip(chunks, weights), 1):
            end = audio_duration if index == len(chunks) else cursor + audio_duration * weight / sum(weights)
            timeline.append((chunk, cursor, end))
            cursor = end
    scenes: list[dict[str, Any]] = []
    for index, (chunk, start, end) in enumerate(timeline, 1):
        scenes.append({
            "index": index,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "text": chunk,
            "query": make_query(chunk, language),
            "selected_video": None,
            "videos": [],
            "blur": False,
            "blur_regions": [],
        })
    (pdir / "subtitles.srt").write_text(scenes_to_srt(scenes), encoding="utf-8-sig")
    write_json(pdir / "scenes.json", scenes)
    return scenes


# ============================================================
# FFMPEG
# ============================================================

def ffmpeg_available() -> bool:
    return FFMPEG_EXE.exists() and FFPROBE_EXE.exists()


def ensure_ffmpeg() -> None:
    if not ffmpeg_available():
        raise RuntimeError(
            "FFmpeg is missing. Please place ffmpeg.exe and ffprobe.exe inside ffmpeg/bin/."
        )


def run_cmd(args: list[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    logger.info("FFmpeg command started")
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(BASE_DIR),
    )
    if result.returncode != 0:
        logger.error("Command failed: %s", result.stderr[-5000:])
        raise RuntimeError("FFmpeg processing failed. Check the project logs.")
    logger.info("FFmpeg command completed")
    return result


def ffprobe_json(path: Path) -> dict[str, Any]:
    ensure_ffmpeg()
    result = subprocess.run(
        [
            str(FFPROBE_EXE),
            "-v", "error",
            "-show_format",
            "-show_streams",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError("Unable to inspect the video with ffprobe.")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Invalid ffprobe response.")


def media_duration(path: Path) -> float:
    info = ffprobe_json(path)
    duration = info.get("format", {}).get("duration")
    if duration is None:
        for stream in info.get("streams", []):
            if stream.get("duration"):
                duration = stream["duration"]
                break
    if duration is None:
        raise RuntimeError("Could not determine media duration.")
    return float(duration)


def output_size(fmt: str) -> tuple[int, int]:
    return {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1": (1080, 1080),
    }.get(fmt, (1080, 1920))


def subtitle_filter_path(path: Path) -> str:
    p = str(path.resolve()).replace("\\", "/")
    p = p.replace(":", r"\:")
    p = p.replace("'", r"\'")
    return p


def find_font() -> str:
    candidates = [
        Path(FONT_FILE_ENV),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\tahoma.ttf"),
        Path(r"C:\Windows\Fonts\calibri.ttf"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return ""


def process_scene_video(src: Path, out: Path, duration: float, fmt: str, blur: bool = False,
                        blur_regions: Any = None) -> None:
    """
    Create a video clip of exactly the given duration by:
      - trimming if the source is longer
      - looping if the source is shorter
    This ensures each scene matches its subtitle duration perfectly.
    """
    vf, output_label = build_blur_filter(fmt, blur, blur_regions)
    source_duration = media_duration(src)
    offset = random.uniform(0, max(0.0, source_duration - duration - 0.25)) if source_duration > duration + 0.5 else 0.0
    run_cmd(
        [
            str(FFMPEG_EXE),
            "-y",
            "-stream_loop", "-1",          # loop source indefinitely
            "-i", str(src),
            "-ss", f"{offset:.3f}",
            "-t", f"{max(duration, 0.1):.3f}",  # cut to exact subtitle duration
            "-vf" if output_label == "0:v" else "-filter_complex", vf,
            *([] if output_label == "0:v" else ["-map", f"[{output_label}]"]),
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "21",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out),
        ],
        timeout=600,
    )


def concat_videos(clips: list[Path], output: Path) -> None:
    list_file = output.parent / "_concat.txt"
    lines = []
    for clip in clips:
        safe = str(clip.resolve()).replace("\\", "/").replace("'", r"'\''")
        lines.append(f"file '{safe}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        run_cmd(
            [
                str(FFMPEG_EXE),
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                str(output),
            ],
            timeout=1200,
        )
    finally:
        list_file.unlink(missing_ok=True)


def extend_video_to_duration(video: Path, target_duration: float, output: Path) -> None:
    """
    Ensure the video has exactly the target duration by looping (if shorter)
    or trimming (if longer) while preserving codec and quality.
    """
    run_cmd(
        [
            str(FFMPEG_EXE),
            "-y",
            "-stream_loop", "-1",
            "-i", str(video),
            "-t", f"{max(target_duration, 0.1):.3f}",
            "-c", "copy",
            str(output),
        ],
        timeout=600,
    )


def add_audio_to_video(video: Path, audio: Path, output: Path) -> None:
    # Video and audio should now have the same duration; -shortest is safe
    run_cmd(
        [
            str(FFMPEG_EXE),
            "-y",
            "-i", str(video),
            "-i", str(audio),
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "copy",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=9",
            "-c:a", "aac",
            "-ar", "48000",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(output),
        ],
        timeout=600,
    )


def mix_narration_and_music(narration: Path, music: Path, output: Path, volume: float) -> None:
    """Loop quiet background music and stop it exactly with the narration."""
    volume = max(0.0, min(0.5, float(volume)))
    run_cmd(
        [
            str(FFMPEG_EXE), "-y", "-i", str(narration),
            "-stream_loop", "-1", "-i", str(music),
            "-filter_complex",
            f"[0:a]loudnorm=I=-16:TP=-1.5:LRA=9[voice];"
            f"[1:a]volume={volume:.3f}[music];"
            "[music][voice]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=450[ducked];"
            "[voice][ducked]amix=inputs=2:duration=first:normalize=0[a]",
            "-map", "[a]", "-c:a", "libmp3lame", "-ar", "48000", "-b:a", "192k", str(output),
        ],
        timeout=600,
    )


def burn_subtitles_and_audio(
    video: Path,
    audio: Path,
    srt: Path,
    output: Path,
    fmt: str,
) -> None:
    sub_path = subtitle_filter_path(srt)
    # Movie-style captions: small, low, clean, and without a background box.
    force_style = (
        "FontName=Arial,FontSize=10,MarginV=38,Alignment=2,"
        "PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,"
        "BorderStyle=1,BackColour=&H00000000&,Shadow=1,Outline=1.2"
    )
    vf = f"subtitles='{sub_path}':charenc=UTF-8:force_style='{force_style}'"
    run_cmd(
        [
            str(FFMPEG_EXE),
            "-y",
            "-i", str(video),
            "-i", str(audio),
            "-vf", vf,
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=9",
            "-c:a", "aac",
            "-ar", "48000",
            "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            str(output),
        ],
        timeout=1800,
    )


def create_social_cover(video: Path, output: Path, fmt: str) -> None:
    """Export a sharp cover frame for Shorts, Reels, and TikTok."""
    duration = media_duration(video)
    w, h = output_size(fmt)
    position = min(max(duration * 0.28, 0.1), max(duration - 0.1, 0.1))
    run_cmd([
        str(FFMPEG_EXE), "-y", "-ss", f"{position:.3f}",
        "-i", str(video), "-frames:v", "1",
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},unsharp=5:5:0.35",
        "-q:v", "2", "-update", "1", str(output),
    ], timeout=180)


# ============================================================
# WHISPER
# ============================================================

WHISPER_MODELS = {"tiny", "base", "small", "medium", "large-v3"}
LANGUAGE_NAMES = {v: k for k, v in LANGUAGE_MAP.items() if k != "auto"}


def get_whisper_model(model_name: str):
    if model_name not in WHISPER_MODELS:
        raise ValueError("Unsupported Whisper model.")
    with whisper_lock:
        if model_name not in whisper_models:
            try:
                import whisper
            except ImportError:
                raise RuntimeError(
                    "openai-whisper is not installed. Run: pip install openai-whisper"
                )
            logger.info("Loading Whisper model: %s", model_name)
            whisper_models[model_name] = whisper.load_model(model_name)
        return whisper_models[model_name]


def transcribe_project(project_id: str, model_name: str, language: str) -> None:
    try:
        row = project_row(project_id)
        if not row:
            return
        pdir = project_dir(project_id)
        audio = pdir / "audio.mp3"
        if not audio.exists():
            raise RuntimeError("Audio file is missing.")
        
        # Get audio duration to later extend the last subtitle
        audio_duration = media_duration(audio)
        
        update_project(project_id, status="Transcribing", error_message="", language=language)
        set_progress(project_id, 5, "Analyzing audio...", "Transcribing")
        set_progress(project_id, 8, "Loading Whisper model...", "Transcribing")
        model = get_whisper_model(model_name)
        set_progress(project_id, 12, "Model loaded, preparing transcription...", "Transcribing")
        lang_code = LANGUAGE_MAP.get(language, None)
        set_progress(project_id, 15, "Transcribing...", "Transcribing")

        result = model.transcribe(
            str(audio),
            language=lang_code,
            task="transcribe",
            verbose=False,
            word_timestamps=True,
            fp16=False,
        )

        output = []
        total_duration = result["segments"][-1]["end"] if result["segments"] else 0
        for seg in result["segments"]:
            start = seg["start"]
            end = seg["end"]
            text = seg["text"].strip()
            if not text:
                continue
            query = make_query(text, language)
            output.append({
                "index": len(output) + 1,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "text": text,
                "query": query,
                "selected_video": None,
                "videos": [],
                "blur": False,
                "blur_regions": [],
            })
            if total_duration > 0:
                pct = 15 + int(min(65, max(0, end / total_duration * 65)))
                set_progress(project_id, pct, f"Transcribing... {int(end)}s", "Transcribing")

        if not output:
            raise RuntimeError("Whisper did not detect usable speech in the audio.")

        # --- FIX: Extend the last subtitle to match the full audio duration ---
        last = output[-1]
        if last["end"] < audio_duration - 0.1:  # allow small tolerance
            logger.info("Extending last subtitle from %.2f to %.2f to match audio duration",
                        last["end"], audio_duration)
            last["end"] = round(audio_duration, 3)
            last["duration"] = round(last["end"] - last["start"], 3)

        output = merge_short_scenes(output, language)

        srt = scenes_to_srt(output)
        (pdir / "subtitles.srt").write_text(srt, encoding="utf-8")
        write_json(pdir / "scenes.json", output)
        update_project(project_id, srt_name="subtitles.srt", status="Ready", progress=85, current_task="Subtitles ready")
        set_progress(project_id, 85, "Subtitles ready", "Ready")
        logger.info("Transcription completed")
    except Exception as exc:
        logger.exception("Transcription failed")
        update_project(project_id, status="Error", error_message=f"{type(exc).__name__}: {exc}")
        set_progress(project_id, 0, str(exc), "Error")


# ============================================================
# PEXELS
# ============================================================

async def pexels_search(query: str, per_page: int = 10, page: int = 1, orientation: str = "portrait") -> list[dict[str, Any]]:
    if not PEXELS_API_KEY:
        raise RuntimeError("Pexels API key is missing.")
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": per_page, "page": page, "orientation": orientation}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params=params,
            )
        if response.status_code == 401:
            raise RuntimeError("Pexels API key is invalid.")
        if response.status_code == 403:
            raise RuntimeError("Pexels API access was forbidden.")
        if response.status_code == 429:
            raise RuntimeError("Pexels rate limit reached. Please try again later.")
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException:
        raise RuntimeError("Unable to connect to Pexels: request timed out.")
    except httpx.RequestError:
        raise RuntimeError("Unable to connect to Pexels.")
    except json.JSONDecodeError:
        raise RuntimeError("Pexels returned an invalid response.")

    results = []
    for video in payload.get("videos", []):
        files = video.get("video_files", [])
        mp4s = [
            f for f in files
            if f.get("file_type") == "video/mp4" and f.get("link")
        ]
        if not mp4s:
            continue
        mp4s.sort(key=lambda x: (
            0 if (x.get("height") or 0) >= (x.get("width") or 0) else 1,
            abs((x.get("width") or 0) - 1080),
            abs((x.get("height") or 0) - 1920),
        ))
        f = mp4s[0]
        download_urls = list(dict.fromkeys(x.get("link") for x in mp4s if x.get("link")))
        results.append({
            "id": video.get("id"),
            "duration": video.get("duration"),
            "preview": video.get("image"),
            "mp4_url": f.get("link"),
            "download_urls": download_urls,
            "pexels_url": video.get("url"),
            "width": f.get("width"),
            "height": f.get("height"),
        })
    return results


# ============================================================
# VIDEO DOWNLOAD / RENDER (FIXED: retry + no SSL verify)
# ============================================================

async def download_file(url: str, destination: Path, retries: int = 4) -> None:
    """Download atomically with long streaming timeouts and clean retries."""
    partial = destination.with_suffix(destination.suffix + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries):
        total = 0
        partial.unlink(missing_ok=True)
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=60.0),
                verify=False,  # Disable SSL verification for compatibility
                follow_redirects=True,
                headers={"User-Agent": "StoryTools/2.0", "Accept": "video/mp4,*/*"},
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    expected = int(response.headers.get("content-length", 0) or 0)
                    if expected > MAX_VIDEO_BYTES:
                        raise RuntimeError("Stock video is larger than the 500 MB safety limit.")
                    with partial.open("wb") as out:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_VIDEO_BYTES:
                                raise RuntimeError("Downloaded stock video is too large.")
                            out.write(chunk)
            if total < 1024:
                raise RuntimeError("The stock-video server returned an empty file.")
            partial.replace(destination)
            return
        except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError) as e:
            partial.unlink(missing_ok=True)
            logger.warning("Download attempt %s/%s failed (%s): %s", attempt + 1, retries, type(e).__name__, e)
            if attempt == retries - 1:
                raise RuntimeError(f"Unable to download this video after {retries} attempts ({type(e).__name__}).")
            await asyncio.sleep(min(8, 2 * (attempt + 1)))


async def download_scene_with_fallback(scene: dict[str, Any], destination: Path,
                                       orientation: str) -> dict[str, Any]:
    """Try alternate resolutions and clips before failing the whole project."""
    selected = scene.get("selected_video") or {}
    candidates = [selected]
    candidates.extend(v for v in (scene.get("videos") or []) if v.get("id") != selected.get("id"))
    attempted: set[str] = set()

    async def try_candidates(items: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        for video in items:
            urls = video.get("download_urls") or [video.get("mp4_url")]
            for url in urls:
                if not url or url in attempted:
                    continue
                attempted.add(url)
                try:
                    await download_file(url, destination, retries=2)
                    if media_duration(destination) > 0.1:
                        return video
                except Exception as exc:
                    destination.unlink(missing_ok=True)
                    logger.warning("Trying another source for scene %s after %s", scene.get("index"), exc)
        return None

    winner = await try_candidates(candidates)
    if winner:
        return winner
    fresh = await pexels_search(scene.get("query") or "cinematic story", per_page=8, page=1, orientation=orientation)
    winner = await try_candidates(fresh)
    if winner:
        scene["videos"] = fresh
        return winner
    raise RuntimeError(f"No downloadable Pexels footage was available for scene {scene.get('index')}.")


def render_project(project_id: str) -> None:
    """
    Renders the final video by:
      1. For each scene, downloading the selected Pexels video.
      2. Using process_scene_video() to create a clip that exactly matches
         the subtitle duration (looping or trimming as needed).
      3. Concatenating all clips.
      4. Extending the combined video to the audio duration (loop if shorter).
      5. Adding the narration audio and optionally burning subtitles.
    """
    try:
        ensure_ffmpeg()
        row = project_row(project_id)
        if not row:
            return
        row_dict = dict(row)
        pdir = project_dir(project_id)
        audio = pdir / "audio.mp3"
        srt = pdir / "subtitles.srt"
        scenes = read_json(pdir / "scenes.json", [])
        if not audio.exists():
            raise RuntimeError("Audio file is missing.")
        if not srt.exists():
            raise RuntimeError("Subtitles file is missing.")
        if not scenes:
            scenes = parse_srt_text(srt.read_text(encoding="utf-8"))
        if not scenes:
            raise RuntimeError("No scenes were found.")
        missing = [s["index"] for s in scenes if not s.get("selected_video")]
        if missing:
            raise RuntimeError(f"Select a video for scene {missing[0]} before rendering.")

        update_project(project_id, status="Processing", error_message="")
        clips_dir = pdir / "_clips"
        clips_dir.mkdir(exist_ok=True)
        selected_dir = project_videos_dir(project_id)
        clips: list[Path] = []

        total = len(scenes)
        orientation = "landscape" if row_dict["output_format"] == "16:9" else "portrait"
        for pos, scene in enumerate(scenes, 1):
            scene_start_pct = 4 + int((pos - 1) / total * 85)
            scene_done_pct = 4 + int(pos / total * 85)
            set_progress(
                project_id,
                scene_start_pct,
                f"Downloading video for scene {pos} of {total}...",
                "Processing",
            )
            target_duration = float(scene["duration"])
            blur_signature = json.dumps(
                {"blur": bool(scene.get("blur", False)), "regions": scene.get("blur_regions", []), "video_id": (scene.get("selected_video") or {}).get("id")},
                sort_keys=True, separators=(",", ":"),
            )
            signature = hashlib.sha256(blur_signature.encode("utf-8")).hexdigest()[:10]
            clip = clips_dir / f"clip_{pos:04d}_{signature}.mp4"
            if clip.exists():
                try:
                    if abs(media_duration(clip) - target_duration) <= 0.08:
                        clips.append(clip)
                        set_progress(project_id, scene_done_pct, f"Reusing scene {pos} of {total}...", "Processing")
                        continue
                except Exception:
                    pass
                clip.unlink(missing_ok=True)
            raw = selected_dir / f"scene_{pos}.mp4"
            raw_valid = False
            if raw.exists():
                try:
                    raw_valid = media_duration(raw) > 0.1
                except Exception:
                    raw.unlink(missing_ok=True)
            if not raw_valid:
                winner = asyncio.run(download_scene_with_fallback(scene, raw, orientation))
                scene["selected_video"] = winner
                write_json(pdir / "scenes.json", scenes)
            # Create a clip that exactly matches the subtitle duration:
            process_scene_video(
                raw,
                clip,
                target_duration,
                row_dict["output_format"],
                bool(scene.get("blur", False)),
                scene.get("blur_regions", []),
            )
            clips.append(clip)
            set_progress(
                project_id,
                scene_done_pct,
                f"Processing scene {pos} of {total}...",
                "Processing",
            )

        set_progress(project_id, 91, "Combining scenes...", "Processing")
        combined = pdir / "_combined.mp4"
        concat_videos(clips, combined)

        # Get durations
        combined_duration = media_duration(combined)
        audio_duration = media_duration(audio)
        logger.info("Combined video: %.2f s, Audio: %.2f s", combined_duration, audio_duration)

        # Extend combined video to exactly match audio duration
        set_progress(project_id, 93, "Adjusting video length to audio...", "Processing")
        extended_video = pdir / "_extended.mp4"
        if abs(combined_duration - audio_duration) > 0.05:
            logger.info("Extending video from %.2f to %.2f to match audio", combined_duration, audio_duration)
            extend_video_to_duration(combined, audio_duration, extended_video)
        else:
            # Already close enough, just copy
            shutil.copy(combined, extended_video)

        set_progress(project_id, 94, "Mixing narration and music...", "Processing")
        final_audio = audio
        music = pdir / "background_music.mp3"
        mixed_audio = pdir / "_mixed_audio.mp3"
        if music.exists():
            mix_narration_and_music(audio, music, mixed_audio, row_dict.get("music_volume", 0.12))
            final_audio = mixed_audio
        burn = row_dict.get("burn_subtitles", 0)
        final = pdir / "final.mp4"
        if burn:
            set_progress(project_id, 95, "Burning subtitles...", "Processing")
            burn_subtitles_and_audio(
                extended_video,
                final_audio,
                srt,
                final,
                row_dict["output_format"],
            )
        else:
            set_progress(project_id, 95, "Adding audio to video...", "Processing")
            add_audio_to_video(extended_video, final_audio, final)

        if not final.exists() or final.stat().st_size < 1000:
            raise RuntimeError("Final video was not created correctly.")

        # Cleanup
        for clip in clips:
            clip.unlink(missing_ok=True)
        shutil.rmtree(clips_dir, ignore_errors=True)
        combined.unlink(missing_ok=True)
        extended_video.unlink(missing_ok=True)
        mixed_audio.unlink(missing_ok=True)

        update_project(
            project_id,
            status="Completed",
            progress=100,
            current_task="Completed!",
            final_output="final.mp4",
            error_message="",
        )
        set_progress(project_id, 100, "Completed!", "Completed")
        logger.info("Final video created")
    except Exception as exc:
        logger.exception("Video rendering failed")
        update_project(project_id, status="Error", error_message=f"{type(exc).__name__}: {exc}")
        set_progress(project_id, 0, str(exc), "Error")


# ============================================================
# AUTH / PAGE ROUTES
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not admin_exists():
        return RedirectResponse("/setup", status_code=303)
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    if admin_exists():
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(render_page("setup", "Create administrator"))


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    if not admin_exists():
        return RedirectResponse("/setup", status_code=303)
    return HTMLResponse(render_page("login", "Login"))


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    require_login(request)
    return HTMLResponse(render_page("dashboard", "Dashboard"))


@app.get("/project/{project_id}", response_class=HTMLResponse)
async def project_page(request: Request, project_id: str):
    require_login(request)
    row = project_row(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return HTMLResponse(render_page("project", f"Project – {row['name']}", project_id=project_id))


# ============================================================
# AUTH API
# ============================================================

@app.post("/api/setup")
async def setup_api(username: str = Form(...), password: str = Form(...)):
    if admin_exists():
        raise HTTPException(status_code=409, detail="Administrator already exists.")
    try:
        create_admin(username, password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("Administrator created")
    return {"ok": True}


@app.post("/api/auth/login")
async def login_api(request: Request, username: str = Form(...), password: str = Form(...)):
    if not verify_admin(username, password):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    request.session["user"] = username.strip().lower()
    return {"ok": True}


@app.post("/api/auth/logout")
async def logout_api(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {
        "authenticated": bool(request.session.get("user")),
        "setup_required": not admin_exists(),
    }


@app.get("/api/settings/tts")
async def get_tts_settings(request: Request):
    require_login(request)
    return public_tts_config()


@app.put("/api/settings/tts")
async def put_tts_settings(request: Request):
    require_login(request)
    body = await request.json()
    incoming = body.get("providers", [])
    if not isinstance(incoming, list) or len(incoming) > 10:
        raise HTTPException(status_code=400, detail="Add up to 10 custom speech providers.")
    old = {p.get("id"): p for p in read_tts_config().get("providers", [])}
    providers = []
    used_ids = set()
    for item in incoming:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Invalid provider settings.")
        name = str(item.get("name", "")).strip()[:60]
        base_url = str(item.get("base_url", "")).strip().rstrip("/")
        model = str(item.get("model", "")).strip()[:120]
        voice = str(item.get("default_voice", "alloy")).strip()[:120]
        response_format = str(item.get("response_format", "mp3")).strip().lower()
        provider_id = str(item.get("id") or secrets.token_hex(8)).strip()
        api_key = str(item.get("api_key", "")).strip() or str(old.get(provider_id, {}).get("api_key", ""))
        parsed = urlparse(base_url)
        local_http = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            not name or not model or not api_key or not parsed.hostname
            or parsed.scheme not in ({"https", "http"} if local_http else {"https"})
            or response_format not in {"mp3", "wav"}
            or not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", provider_id)
            or provider_id in {"edge", "gtts"}
            or provider_id in used_ids
        ):
            raise HTTPException(status_code=400, detail="Check each provider's name, secure URL, model, voice and API key.")
        used_ids.add(provider_id)
        providers.append({
            "id": provider_id, "name": name, "base_url": base_url, "model": model,
            "default_voice": voice or "alloy", "response_format": response_format, "api_key": api_key,
        })
    default_provider = str(body.get("default_provider", "edge"))
    supported = {p["id"] for p in providers} | {
        p["id"] for p in available_tts_providers() if p["id"] in {"edge", "gtts"}
    }
    if default_provider not in supported:
        raise HTTPException(status_code=400, detail="Choose an available default speech provider.")
    save_tts_config({"providers": providers, "default_provider": default_provider})
    if "pexels_api_key" in body and str(body["pexels_api_key"]).strip():
        save_pexels_api_key(str(body["pexels_api_key"]).strip())
    elif body.get("clear_pexels_api_key"):
        save_pexels_api_key("")
    return public_tts_config()


# ============================================================
# PROJECT API
# ============================================================

@app.post("/api/voice-preview")
async def voice_preview(
    request: Request,
    background_tasks: BackgroundTasks,
    text: str = Form(...),
    voice: str = Form("ma_female"),
    style: str = Form("story"),
    provider_id: str = Form("edge"),
    custom_voice: str = Form(""),
):
    require_login(request)
    text = text.strip()[:240]
    if not text:
        raise HTTPException(status_code=400, detail="Write a short script first.")
    if style not in VOICE_STYLES:
        raise HTTPException(status_code=400, detail="Invalid voice preview settings.")
    if provider_id in {"edge", "gtts"} and voice not in {"male", "female", "auto_female", "auto_male", *VOICE_IDS.keys()}:
        raise HTTPException(status_code=400, detail="Invalid voice preview settings.")
    preview_dir = DATA_DIR / "previews" / secrets.token_hex(8)
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_file = preview_dir / "preview.mp3"
    try:
        preview_language = "ar" if re.search(r"[\u0600-\u06FF]", text) else "en"
        if provider_id in {"edge", "gtts"}:
            await generate_audio_from_preview(text, preview_file, preview_language, voice, style, provider_id)
        else:
            provider = tts_provider_by_id(provider_id)
            if not provider or not provider.get("api_key"):
                raise ValueError("Select a saved speech provider first.")
            await generate_audio_with_compatible_provider(
                prepare_text_for_speech(text, preview_language), preview_file, provider,
                custom_voice.strip() or provider.get("default_voice", "alloy"),
            )
    except Exception as exc:
        shutil.rmtree(preview_dir, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"Voice preview failed: {exc}")
    background_tasks.add_task(shutil.rmtree, preview_dir, True)
    preview_provider = tts_provider_by_id(provider_id) if provider_id not in {"edge", "gtts"} else None
    preview_format = (preview_provider or {}).get("response_format", "mp3")
    return FileResponse(preview_file, media_type="audio/wav" if preview_format == "wav" else "audio/mpeg", background=background_tasks)

@app.post("/api/projects")
async def create_project(
    request: Request,
    name: str = Form(...),
    mode: str = Form(...),
    output_format: str = Form("9:16"),
    language: str = Form("auto"),
    script: Optional[str] = Form(None),
    tts_voice: str = Form("female"),
    tts_style: str = Form("story"),
    tts_provider: str = Form("edge"),
    tts_custom_voice: str = Form(""),
):
    require_login(request)
    name = name.strip()
    if not name or len(name) > 120:
        raise HTTPException(status_code=400, detail="Project name must contain 1-120 characters.")
    if mode not in {"automatic", "existing", "tts"}:
        raise HTTPException(status_code=400, detail="Invalid input mode.")
    if output_format not in {"9:16", "16:9", "1:1"}:
        raise HTTPException(status_code=400, detail="Invalid output format.")
    if language not in LANGUAGE_MAP:
        raise HTTPException(status_code=400, detail="Invalid language.")
    if tts_provider in {"edge", "gtts"}:
        allowed_voices = {"male", "female", "auto_female", "auto_male", *VOICE_IDS.keys()}
        if tts_voice not in allowed_voices:
            raise HTTPException(status_code=400, detail="Invalid narration voice.")
        if tts_provider not in {provider["id"] for provider in available_tts_providers()}:
            raise HTTPException(status_code=400, detail="The selected speech provider is not installed.")
        project_voice = tts_voice
    else:
        custom_provider = tts_provider_by_id(tts_provider)
        if not custom_provider or not custom_provider.get("api_key"):
            raise HTTPException(status_code=400, detail="Configure the selected speech provider in Settings first.")
        project_voice = (tts_custom_voice.strip() or custom_provider.get("default_voice", "alloy"))[:120]
    if tts_style not in VOICE_STYLES:
        raise HTTPException(status_code=400, detail="Invalid narration style.")
    if mode == "tts" and not (script or "").strip():
        raise HTTPException(status_code=400, detail="Script is required for TTS mode.")
    
    project_id = secrets.token_hex(8)
    pdir = project_dir(project_id)
    now = utc_now()
    
    with db() as conn:
        conn.execute(
            """
            INSERT INTO projects
            (id,name,mode,output_format,status,progress,current_task,created_at,updated_at,language,script,tts_voice,tts_style,tts_provider,burn_subtitles)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (project_id, name, mode, output_format, "Draft", 0, "", now, now, language, script, project_voice, tts_style, tts_provider, 1),
        )
    logger.info("Project created: %s", project_id)
    
    # Script mode: voice first, then create SRT directly from the original text.
    # This preserves Arabic spelling and ties every video scene to narration time.
    if mode == "tts":
        try:
            update_project(project_id, status="Processing", progress=2, current_task="Creating natural narration")
            await generate_audio_from_script(
                project_id, script.strip(), language, project_voice, tts_style, tts_provider,
                project_voice if tts_provider not in {"edge", "gtts"} else "",
            )
            create_script_subtitles(project_id, script.strip(), language)
            update_project(
                project_id, audio_name="audio.mp3", srt_name="subtitles.srt",
                status="Ready", progress=35, current_task="Narration and timed scenes ready",
            )
        except Exception as e:
            update_project(project_id, status="Error", error_message=f"TTS failed: {e}")
            raise HTTPException(status_code=500, detail=f"TTS generation failed: {e}")
    
    return {"ok": True, "project": project_json(project_row(project_id))}


@app.get("/api/projects")
async def list_projects(request: Request):
    require_login(request)
    with db() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
    return {"projects": [project_json(r) for r in rows]}


@app.get("/api/projects/{project_id}")
async def get_project(request: Request, project_id: str):
    require_login(request)
    row = project_row(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found.")
    pdir = project_dir(project_id)
    scenes = read_json(pdir / "scenes.json", [])
    return {"project": project_json(row), "scenes": scenes}


@app.delete("/api/projects/{project_id}")
async def delete_project(request: Request, project_id: str):
    require_login(request)
    if not project_row(project_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    shutil.rmtree(project_dir(project_id), ignore_errors=True)
    with db() as conn:
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    with progress_lock:
        progress.pop(project_id, None)
    return {"ok": True}


@app.post("/api/projects/{project_id}/upload")
async def upload_project_files(
    request: Request,
    project_id: str,
    audio: UploadFile = File(...),
    srt: Optional[UploadFile] = File(None),
):
    require_login(request)
    row = project_row(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found.")
    validate_upload_size(audio, MAX_AUDIO_BYTES)
    audio_ext = Path(audio.filename or "").suffix.lower()
    if audio_ext not in ALLOWED_AUDIO_EXT:
        raise HTTPException(status_code=400, detail="Unsupported audio format.")
    pdir = project_dir(project_id)
    await save_upload(audio, pdir / "audio.mp3", MAX_AUDIO_BYTES)
    update_project(project_id, audio_name=safe_name(audio.filename or "audio.mp3"))

    if row["mode"] == "existing":
        if not srt:
            raise HTTPException(status_code=400, detail="An SRT file is required.")
        validate_upload_size(srt, MAX_SRT_BYTES)
        if Path(srt.filename or "").suffix.lower() not in ALLOWED_SRT_EXT:
            raise HTTPException(status_code=400, detail="Only .srt files are allowed.")
        await save_upload(srt, pdir / "subtitles.srt", MAX_SRT_BYTES)
        text = (pdir / "subtitles.srt").read_text(encoding="utf-8-sig")
        scenes = parse_srt_text(text)
        if not scenes:
            raise HTTPException(status_code=400, detail="The SRT file contains no valid subtitles.")
        lang = row["language"] or "auto"
        for scene in scenes:
            scene["query"] = make_query(scene["text"], lang)
        write_json(pdir / "scenes.json", scenes)
        update_project(
            project_id,
            srt_name=safe_name(srt.filename or "subtitles.srt"),
            status="Ready",
            progress=30,
            current_task="Subtitles ready",
        )
    else:  # automatic mode
        update_project(project_id, status="Draft", progress=10, current_task="Audio uploaded")
    logger.info("Audio uploaded")
    return {"ok": True, "project": project_json(project_row(project_id))}


@app.post("/api/projects/{project_id}/transcribe")
async def transcribe_api(
    request: Request,
    project_id: str,
    model: str = Form("small"),
    language: str = Form("auto"),
):
    require_login(request)
    row = project_row(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found.")
    if model not in WHISPER_MODELS:
        raise HTTPException(status_code=400, detail="Invalid Whisper model.")
    if language not in LANGUAGE_MAP:
        raise HTTPException(status_code=400, detail="Invalid language.")
    if not (project_dir(project_id) / "audio.mp3").exists():
        raise HTTPException(status_code=400, detail="Upload an audio file first.")
    set_progress(project_id, 2, "Starting transcription...", "Transcribing")
    executor.submit(transcribe_project, project_id, model, language)
    return {"ok": True, "message": "Transcription started."}


@app.get("/api/projects/{project_id}/subtitles")
async def get_subtitles(request: Request, project_id: str):
    require_login(request)
    path = project_dir(project_id) / "subtitles.srt"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Subtitles not found.")
    return {"srt": path.read_text(encoding="utf-8-sig"), "scenes": parse_srt_text(path.read_text(encoding="utf-8-sig"))}


@app.put("/api/projects/{project_id}/subtitles")
async def update_subtitles(request: Request, project_id: str):
    require_login(request)
    body = await request.json()
    scenes = body.get("scenes")
    if not isinstance(scenes, list):
        raise HTTPException(status_code=400, detail="Invalid subtitle data.")
    normalized = []
    row = project_row(project_id)
    lang = row["language"] if row else "auto"
    previous_scenes = read_json(project_dir(project_id) / "scenes.json", [])
    try:
        for i, item in enumerate(scenes, 1):
            start = float(item["start"])
            end = float(item["end"])
            text = str(item.get("text", "")).strip()
            if start < 0 or end < start or not text:
                raise ValueError(f"Subtitle {i} is invalid.")
            previous = previous_scenes[i - 1] if i <= len(previous_scenes) else {}
            normalized.append({
                "index": i,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "text": text,
                "query": make_query(text, lang),
                "selected_video": item.get("selected_video", previous.get("selected_video")),
                "videos": item.get("videos", previous.get("videos", [])),
                "blur": bool(item.get("blur", previous.get("blur", False))),
                "blur_regions": normalize_blur_regions(item.get("blur_regions", previous.get("blur_regions", []))),
            })
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    pdir = project_dir(project_id)
    (pdir / "subtitles.srt").write_text(scenes_to_srt(normalized), encoding="utf-8")
    write_json(pdir / "scenes.json", normalized)
    update_project(project_id, status="Ready", progress=35, current_task="Subtitles saved")
    return {"ok": True, "scenes": normalized}


@app.get("/api/projects/{project_id}/scenes")
async def get_scenes(request: Request, project_id: str):
    require_login(request)
    scenes = read_json(project_dir(project_id) / "scenes.json", [])
    return {"scenes": scenes}


@app.post("/api/projects/{project_id}/search-videos")
async def search_videos(request: Request, project_id: str):
    require_login(request)
    pdir = project_dir(project_id)
    scenes = read_json(pdir / "scenes.json", [])
    if not scenes:
        raise HTTPException(status_code=400, detail="Generate or upload subtitles first.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    requested_index = int(body.get("scene_index", 0) or 0)
    requested_query = str(body.get("query", "")).strip()[:120]
    if requested_index and not (1 <= requested_index <= len(scenes)):
        raise HTTPException(status_code=400, detail="Invalid scene index.")
    update_project(project_id, status="Searching", progress=40, current_task="Finding matching footage...")
    logger.info("Pexels search started")
    try:
        targets = [(requested_index, scenes[requested_index - 1])] if requested_index else list(enumerate(scenes, 1))
        row = project_row(project_id)
        orientation = "landscape" if row and row["output_format"] == "16:9" else "portrait"
        for position, (i, scene) in enumerate(targets, 1):
            query = requested_query if requested_index else (scene.get("query") or make_query(scene["text"], "auto"))
            scene["query"] = query
            # A different result page makes repeated searches useful instead of
            # showing the same clips forever.
            scene["videos"] = await pexels_search(query, page=random.randint(1, 5), orientation=orientation)
            set_progress(
                project_id,
                40 + int(position / len(targets) * 30),
                f"Finding footage for scene {i}...",
                "Searching",
            )
        write_json(pdir / "scenes.json", scenes)
        update_project(project_id, status="Ready", progress=70, current_task="Videos ready")
        logger.info("Pexels results received")
        return {"ok": True, "scenes": scenes}
    except Exception as exc:
        update_project(project_id, status="Error", error_message=str(exc))
        set_progress(project_id, 0, str(exc), "Error")
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/projects/{project_id}/select-video")
async def select_video(request: Request, project_id: str):
    require_login(request)
    body = await request.json()
    scene_index = int(body.get("scene_index", 0))
    video = body.get("video")
    if not isinstance(video, dict) or not video.get("mp4_url"):
        raise HTTPException(status_code=400, detail="Invalid video selection.")
    pdir = project_dir(project_id)
    scenes = read_json(pdir / "scenes.json", [])
    if not (1 <= scene_index <= len(scenes)):
        raise HTTPException(status_code=400, detail="Invalid scene index.")
    old_video = scenes[scene_index - 1].get("selected_video") or {}
    if old_video.get("id") != video.get("id"):
        (project_videos_dir(project_id) / f"scene_{scene_index}.mp4").unlink(missing_ok=True)
    scenes[scene_index - 1]["selected_video"] = video
    write_json(pdir / "scenes.json", scenes)
    return {"ok": True, "scene": scenes[scene_index - 1]}


@app.post("/api/projects/{project_id}/auto-select")
async def auto_select_videos(request: Request, project_id: str):
    require_login(request)
    pdir = project_dir(project_id)
    scenes = read_json(pdir / "scenes.json", [])
    selected = 0
    for scene in scenes:
        choices = scene.get("videos") or []
        if not choices:
            continue
        current_id = (scene.get("selected_video") or {}).get("id")
        alternatives = [video for video in choices if video.get("id") != current_id] or choices
        scene["selected_video"] = random.choice(alternatives)
        if (scene.get("selected_video") or {}).get("id") != current_id:
            (project_videos_dir(project_id) / f"scene_{scene.get('index', selected + 1)}.mp4").unlink(missing_ok=True)
        selected += 1
    write_json(pdir / "scenes.json", scenes)
    return {"ok": True, "selected": selected, "scenes": scenes}


@app.post("/api/projects/{project_id}/scene-settings")
async def scene_settings(request: Request, project_id: str):
    require_login(request)
    body = await request.json()
    scene_index = int(body.get("scene_index", 0))
    scenes = read_json(project_dir(project_id) / "scenes.json", [])
    if not (1 <= scene_index <= len(scenes)):
        raise HTTPException(status_code=400, detail="Invalid scene index.")
    try:
        if "blur" in body:
            scenes[scene_index - 1]["blur"] = bool(body["blur"])
        if "blur_regions" in body:
            scenes[scene_index - 1]["blur_regions"] = normalize_blur_regions(body["blur_regions"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    write_json(project_dir(project_id) / "scenes.json", scenes)
    return {"ok": True}


@app.post("/api/projects/{project_id}/create-video")
async def create_video(
    request: Request,
    project_id: str,
    burn_subtitles: bool = Form(False),
    music: Optional[UploadFile] = File(None),
    music_volume: float = Form(0.12),
):
    require_login(request)
    row = project_row(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found.")
    music_volume = max(0.0, min(0.5, float(music_volume)))
    update_project(project_id, burn_subtitles=1 if burn_subtitles else 0, music_volume=music_volume)
    pdir = project_dir(project_id)
    if music and music.filename:
        validate_upload_size(music, MAX_AUDIO_BYTES)
        if Path(music.filename).suffix.lower() not in ALLOWED_AUDIO_EXT:
            raise HTTPException(status_code=400, detail="Unsupported background music format.")
        await save_upload(music, pdir / "background_music.mp3", MAX_AUDIO_BYTES)
        update_project(project_id, music_name=safe_name(music.filename))
    scenes = read_json(pdir / "scenes.json", [])
    missing = [s["index"] for s in scenes if not s.get("selected_video")]
    if missing:
        raise HTTPException(status_code=400, detail=f"Select a video for scene {missing[0]} first.")
    executor.submit(render_project, project_id)
    return {"ok": True, "message": "Video processing started."}


@app.get("/api/projects/{project_id}/progress")
async def project_progress(request: Request, project_id: str):
    require_login(request)
    return get_progress(project_id)


@app.get("/api/projects/{project_id}/download")
async def download_video(request: Request, project_id: str):
    require_login(request)
    row = project_row(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found.")
    final = project_dir(project_id) / "final.mp4"
    if not final.exists():
        raise HTTPException(status_code=404, detail="Final video is not ready.")
    return FileResponse(
        final,
        media_type="video/mp4",
        filename=f"{safe_name(row['name'], 'story')}.mp4",
    )


@app.get("/api/projects/{project_id}/download-srt")
async def download_srt(request: Request, project_id: str):
    require_login(request)
    row = project_row(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found.")
    subtitle = project_dir(project_id) / "subtitles.srt"
    if not subtitle.exists():
        raise HTTPException(status_code=404, detail="Subtitles are not ready.")
    return FileResponse(subtitle, media_type="application/x-subrip", filename=f"{safe_name(row['name'], 'story')}.srt")


@app.get("/api/projects/{project_id}/download-cover")
async def download_cover(request: Request, project_id: str):
    require_login(request)
    row = project_row(project_id)
    if not row:
        raise HTTPException(status_code=404, detail="Project not found.")
    pdir = project_dir(project_id)
    final = pdir / "final.mp4"
    cover = pdir / "cover.jpg"
    if not final.exists():
        raise HTTPException(status_code=404, detail="Final video is not ready.")
    if not cover.exists() or cover.stat().st_mtime < final.stat().st_mtime:
        create_social_cover(final, cover, row["output_format"])
    return FileResponse(cover, media_type="image/jpeg", filename=f"{safe_name(row['name'], 'story')}-cover.jpg")


# ============================================================
# EMBEDDED FRONTEND – improved design + dynamic updates
# ============================================================

CSS = r"""
:root {
  --bg: #0a0e1a;
  --panel: #111827;
  --panel2: #1a253a;
  --border: #2d3b54;
  --text: #f0f4ff;
  --muted: #8899bb;
  --primary: #6d7cff;
  --primary2: #4e5ce6;
  --success: #34d399;
  --danger: #ff6b7a;
  --warning: #fbbf24;
  --shadow: 0 20px 60px rgba(0,0,0,0.6);
}
* { box-sizing: border-box; margin: 0; }
body {
  background: radial-gradient(ellipse at top right, #18244a 0%, #0a0e1a 80%);
  color: var(--text);
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif;
  min-height: 100vh;
  padding: 20px;
}
.app {
  max-width: 1400px;
  margin: 0 auto;
}
.card {
  background: rgba(17, 24, 39, 0.85);
  backdrop-filter: blur(16px);
  border: 1px solid var(--border);
  border-radius: 24px;
  padding: 24px;
  box-shadow: var(--shadow);
  transition: transform 0.15s ease, border-color 0.2s;
}
.card:hover { border-color: #4a5a7a; }
.hero {
  background: linear-gradient(135deg, rgba(109, 124, 255, 0.08), rgba(78, 92, 230, 0.04));
  border-color: rgba(109, 124, 255, 0.25);
}
h1 { font-size: 2rem; font-weight: 700; letter-spacing: -0.02em; margin-bottom: 4px; }
h2 { font-size: 1.4rem; font-weight: 600; margin-bottom: 16px; }
h3 { font-weight: 600; }
.muted { color: var(--muted); }
.small { font-size: 0.85rem; }
.flex { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.gap-2 { gap: 8px; }
.mb-2 { margin-bottom: 12px; }
.mt-2 { margin-top: 12px; }
.grid { display: grid; gap: 20px; }
.grid-2 { grid-template-columns: repeat(2,1fr); }
.grid-3 { grid-template-columns: repeat(3,1fr); }
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 10px 18px;
  border: none;
  border-radius: 12px;
  font-weight: 600;
  font-size: 0.9rem;
  background: #25324f;
  color: #fff;
  transition: all 0.15s;
  cursor: pointer;
  text-decoration: none;
}
.btn:hover { filter: brightness(1.15); transform: translateY(-1px); }
.btn-primary { background: linear-gradient(135deg, var(--primary), var(--primary2)); }
.btn-success { background: #0d6d4f; }
.btn-danger { background: #6d2633; }
.btn-ghost { background: transparent; border: 1px solid var(--border); }
.btn-ghost:hover { background: rgba(255,255,255,0.05); }
.btn-block { width: 100%; justify-content: center; }
input, select, textarea {
  width: 100%;
  padding: 12px 14px;
  background: #0f1729;
  border: 1px solid var(--border);
  border-radius: 12px;
  color: var(--text);
  font-size: 0.95rem;
  outline: none;
  transition: border-color 0.2s;
}
input:focus, select:focus, textarea:focus { border-color: var(--primary); }
textarea { resize: vertical; min-height: 80px; }
label { font-weight: 500; display: block; margin-bottom: 6px; }
.radio-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 0;
}
.radio {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: #0f1729;
  border: 1px solid var(--border);
  border-radius: 12px;
  cursor: pointer;
  transition: all 0.15s;
}
.radio:hover { background: #1a2744; }
.radio input[type="radio"] { width: auto; margin: 0; }
.project-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.project-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  background: #0f1729;
  border-radius: 16px;
  border: 1px solid var(--border);
  transition: all 0.15s;
  flex-wrap: wrap;
}
.project-item:hover { border-color: #4a5a7a; background: #141f35; }
.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 700;
  background: #1e2a44;
  color: #aabbdd;
}
.status.Completed { background: #0d3d2e; color: #34d399; }
.status.Error { background: #3d1e24; color: #ff6b7a; }
.status.Processing, .status.Transcribing, .status.Searching { background: #3d3114; color: #fbbf24; }
.progress-bar {
  width: 100%;
  height: 8px;
  background: #0a1120;
  border-radius: 999px;
  overflow: hidden;
  border: 1px solid var(--border);
  margin: 8px 0;
}
.progress-bar > div {
  height: 100%;
  background: linear-gradient(90deg, var(--primary), #9b6dff);
  transition: width 0.3s;
}
.filter-bar {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 20px;
}
.filter-bar input, .filter-bar select { flex: 1 1 180px; }
.steps {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 4px 0 8px;
}
.step {
  flex: 0 0 auto;
  padding: 10px 14px;
  background: #0f1729;
  border: 1px solid var(--border);
  border-radius: 12px;
  font-size: 0.8rem;
  color: var(--muted);
  white-space: nowrap;
}
.step.done { color: var(--success); border-color: var(--success); }
.step.active { color: var(--text); border-color: var(--primary); background: #1a2744; }
.video-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px,1fr)); gap: 12px; }
.video-card {
  background: #0f1729;
  border: 1px solid var(--border);
  border-radius: 14px;
  overflow: hidden;
  cursor: pointer;
  transition: all 0.15s;
}
.video-card:hover { border-color: var(--primary); }
.video-card video { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; background: #000; }
.video-card .body { padding: 10px; }
.scene {
  background: #0f1729;
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 16px;
  margin-bottom: 12px;
}
.scene-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 8px; }
.badge { font-size: 0.75rem; padding: 2px 10px; border-radius: 999px; background: #1e2a44; }
.toast {
  position: fixed;
  bottom: 30px;
  right: 30px;
  padding: 14px 24px;
  border-radius: 14px;
  background: #1a2744;
  border: 1px solid var(--border);
  color: #fff;
  max-width: 400px;
  box-shadow: var(--shadow);
  z-index: 999;
  transition: all 0.3s;
}
.toast.error { background: #3d1e24; border-color: #6d2633; }
.toast.ok { background: #0d3d2e; border-color: #0d6d4f; }
.operation-overlay {
  position:fixed; inset:0; z-index:2000; display:grid; place-items:center;
  padding:22px; background:rgba(3,7,16,.76); backdrop-filter:blur(14px);
  opacity:0; visibility:hidden; transition:opacity .22s ease,visibility .22s ease;
}
.operation-overlay.show { opacity:1; visibility:visible; }
.operation-card {
  width:min(520px,100%); position:relative; overflow:hidden; padding:30px;
  border:1px solid rgba(255,255,255,.12); border-radius:24px;
  background:linear-gradient(150deg,rgba(22,31,51,.98),rgba(9,15,28,.99));
  box-shadow:0 32px 100px rgba(0,0,0,.65);
}
.operation-card::before {
  content:''; position:absolute; width:210px; height:210px; right:-90px; top:-120px;
  border-radius:50%; background:linear-gradient(135deg,#00d6aa,#7869ef);
  filter:blur(18px); opacity:.24;
}
.operation-top { display:flex; align-items:center; gap:16px; position:relative; }
.operation-spinner {
  width:50px; height:50px; flex:0 0 50px; border-radius:50%;
  border:3px solid rgba(255,255,255,.1); border-top-color:#67e8c5;
  border-right-color:#7869ef; animation:operationSpin .9s linear infinite;
}
.operation-title { font-size:1.28rem; font-weight:750; letter-spacing:-.015em; }
.operation-detail { color:#a9b5ce; margin-top:5px; line-height:1.45; }
.operation-track { height:8px; margin:24px 0 18px; background:#080e19; border:1px solid rgba(255,255,255,.08); border-radius:999px; overflow:hidden; }
.operation-fill { height:100%; width:12%; border-radius:inherit; background:linear-gradient(90deg,#00c997,#7869ef,#a98bff); transition:width .5s ease; position:relative; }
.operation-fill.indeterminate { width:38%; animation:operationTravel 1.6s ease-in-out infinite; }
.operation-meta { display:flex; justify-content:space-between; gap:12px; color:#7f8ba5; font-size:.8rem; }
.operation-steps { display:grid; gap:9px; margin-top:20px; }
.operation-step { display:flex; align-items:center; gap:10px; color:#68758f; font-size:.86rem; transition:color .2s; }
.operation-step::before { content:''; width:8px; height:8px; flex:0 0 8px; border-radius:50%; background:#35415a; }
.operation-step.active { color:#f3f6ff; }
.operation-step.active::before { background:#67e8c5; box-shadow:0 0 0 5px rgba(103,232,197,.1); }
.operation-step.done { color:#8fa0bd; }
.operation-step.done::before { background:#7869ef; }
.render-status-grid { display:grid; grid-template-columns:145px 1fr; gap:24px; align-items:center; margin-top:22px; }
.render-ring { --progress:0; width:138px; aspect-ratio:1; display:grid; place-items:center; border-radius:50%; background:conic-gradient(#67e8c5 calc(var(--progress)*1%),#7869ef calc(var(--progress)*1%),#172033 0); position:relative; }
.render-ring::after { content:''; position:absolute; inset:10px; border-radius:50%; background:#0d1525; }
.render-ring strong { position:relative; z-index:1; font-size:1.65rem; }
.render-stage-list { display:grid; gap:9px; }
.render-stage { padding:9px 12px; border-radius:10px; color:#6f7b94; background:rgba(255,255,255,.025); font-size:.84rem; }
.render-stage.active { color:#fff; background:rgba(108,92,231,.16); border:1px solid rgba(139,124,255,.28); }
.render-stage.done { color:#67e8c5; }
@keyframes operationSpin { to { transform:rotate(360deg); } }
@keyframes operationTravel { 0% { transform:translateX(-110%); } 50% { transform:translateX(165%); } 100% { transform:translateX(-110%); } }
@media (max-width: 768px) {
  .grid-2, .grid-3 { grid-template-columns: 1fr; }
  .project-item { flex-direction: column; align-items: stretch; gap: 12px; }
  .render-status-grid { grid-template-columns:1fr; justify-items:center; }
  .render-stage-list { width:100%; }
}

/* Studio redesign */
body {
  background:
    radial-gradient(circle at 8% 0%, rgba(0,214,170,.15), transparent 28rem),
    radial-gradient(circle at 92% 5%, rgba(108,92,231,.19), transparent 32rem),
    #070b14;
  padding: 28px;
}
.app { max-width: 1280px; }
.topbar {
  position: sticky; top: 12px; z-index: 50; padding: 12px 16px;
  border: 1px solid rgba(255,255,255,.08); border-radius: 18px;
  background: rgba(9,14,25,.82); backdrop-filter: blur(20px);
}
.card {
  background: linear-gradient(145deg, rgba(20,28,45,.94), rgba(12,18,31,.96));
  border-color: rgba(255,255,255,.08); border-radius: 20px;
  box-shadow: 0 24px 70px rgba(0,0,0,.34);
}
.hero {
  position: relative; overflow: hidden; padding: 38px;
  background: linear-gradient(130deg, rgba(20,31,52,.98), rgba(20,24,48,.97));
}
.hero::after {
  content: ''; position: absolute; width: 330px; height: 330px; right: -120px; top: -160px;
  border-radius: 50%; background: linear-gradient(135deg, #00d6aa, #6c5ce7); filter: blur(10px); opacity: .24;
}
.hero h1 { font-size: clamp(2.2rem,5vw,4rem); line-height: 1; max-width: 760px; }
.eyebrow { color:#67e8c5; text-transform:uppercase; letter-spacing:.14em; font-size:.72rem; font-weight:800; margin-bottom:12px; }
.feature-row { display:flex; gap:9px; flex-wrap:wrap; margin-top:20px; position:relative; z-index:1; }
.feature-chip { padding:7px 11px; border:1px solid rgba(255,255,255,.1); border-radius:999px; color:#c7d2e8; background:rgba(255,255,255,.035); font-size:.78rem; }
.btn { min-height: 42px; border-radius: 11px; padding: 10px 17px; }
.btn-primary { background: linear-gradient(135deg,#6c5ce7,#8b7cff); box-shadow:0 8px 24px rgba(108,92,231,.23); }
.btn-success { background:linear-gradient(135deg,#009f7a,#00bb90); box-shadow:0 8px 24px rgba(0,187,144,.18); }
input, select, textarea { background:#0a1120; border-color:rgba(255,255,255,.1); min-height:45px; }
textarea { line-height:1.75; font-size:1rem; }
.modal-backdrop { position:fixed; inset:0; z-index:1500; display:grid; place-items:center; padding:18px; background:rgba(2,6,16,.82); backdrop-filter:blur(12px); overflow:auto; }
.modal-backdrop[hidden] { display:none; }
.modal-card { width:min(760px,100%); max-height:92vh; overflow:auto; background:#111827; border:1px solid var(--border); border-radius:22px; padding:22px; box-shadow:var(--shadow); }
.blur-stage { position:relative; margin:18px auto; width:min(100%,520px); aspect-ratio:9/16; background:#05070d; overflow:hidden; touch-action:none; border-radius:14px; }
.blur-stage.landscape { aspect-ratio:16/9; width:min(100%,820px); }
.blur-stage.square { aspect-ratio:1/1; width:min(100%,600px); }
.blur-stage video { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }
.blur-layer { position:absolute; inset:0; cursor:crosshair; touch-action:none; }
.blur-region { position:absolute; min-width:2%; min-height:2%; border:2px solid #00e1b0; background:rgba(0,225,176,.18); backdrop-filter:blur(9px); cursor:move; }
.blur-region button { position:absolute; right:-11px; top:-14px; width:23px; height:23px; border:0; border-radius:50%; background:#ef476f; color:#fff; font-weight:800; cursor:pointer; }
.blur-region .resize { position:absolute; width:14px; height:14px; right:-7px; bottom:-7px; background:#8b7cff; border:2px solid white; border-radius:4px; cursor:nwse-resize; }
.setting-provider { padding:12px; border:1px solid var(--border); border-radius:12px; background:#0a1120; }
.radio { background:#0a1120; border-color:rgba(255,255,255,.09); padding:14px; }
.radio:has(input:checked) { border-color:#7869ef; background:rgba(108,92,231,.13); box-shadow:inset 0 0 0 1px rgba(108,92,231,.2); }
.creator { border-color:rgba(103,232,197,.2); }
.scene { background:rgba(8,14,25,.7); border-color:rgba(255,255,255,.075); padding:20px; }
.video-grid { grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:14px; }
.video-card { background:#080e19; border-radius:16px; border-color:rgba(255,255,255,.08); }
.video-card video { aspect-ratio:9/16; max-height:360px; object-fit:cover; }
.video-card:hover { transform:translateY(-3px); box-shadow:0 16px 35px rgba(0,0,0,.32); }
.project-item { background:rgba(8,14,25,.7); border-color:rgba(255,255,255,.075); }
.progress-bar > div { background:linear-gradient(90deg,#00c997,#7c6df2); }
@media (max-width: 600px) {
  body { padding:12px; }
  .card,.hero { padding:18px; border-radius:16px; }
  .hero h1 { font-size:2.25rem; }
  .video-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
}
"""

JS = r"""
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m]));

let operationState = { timer:null, stepTimer:null, started:0, steps:[] };
let ttsSettingsState = { providers:[], available:[], default_provider:'edge' };
let ttsProviderInitialized = false;

function ensureOperationUI() {
  if ($('#operationOverlay')) return;
  document.body.insertAdjacentHTML('beforeend', `
    <div id="operationOverlay" class="operation-overlay" role="status" aria-live="polite" aria-busy="true">
      <div class="operation-card">
        <div class="operation-top">
          <div class="operation-spinner"></div>
          <div><div id="operationTitle" class="operation-title">Working…</div><div id="operationDetail" class="operation-detail">Preparing your request</div></div>
        </div>
        <div class="operation-track"><div id="operationFill" class="operation-fill indeterminate"></div></div>
        <div class="operation-meta"><span id="operationPercent">In progress</span><span id="operationTime">0:00</span></div>
        <div id="operationSteps" class="operation-steps"></div>
      </div>
    </div>`);
}

function beginOperation(title, detail, steps = [], percent = null) {
  ensureOperationUI();
  clearInterval(operationState.timer); clearInterval(operationState.stepTimer);
  operationState = { timer:null, stepTimer:null, started:Date.now(), steps, step:0 };
  $('#operationTitle').textContent = title;
  $('#operationDetail').textContent = detail;
  $('#operationSteps').innerHTML = steps.map((s,i)=>`<div class="operation-step ${i===0?'active':''}">${esc(s)}</div>`).join('');
  setOperationProgress(percent);
  $('#operationTime').textContent = '0:00';
  $('#operationOverlay').classList.add('show');
  document.body.style.overflow = 'hidden';
  operationState.timer = setInterval(() => {
    const total = Math.floor((Date.now()-operationState.started)/1000);
    $('#operationTime').textContent = `${Math.floor(total/60)}:${String(total%60).padStart(2,'0')}`;
  }, 500);
  if (steps.length > 1 && percent == null) operationState.stepTimer = setInterval(() => {
    if (operationState.step < steps.length-1) setOperationStep(operationState.step+1);
  }, 2400);
}

function setOperationStep(index, detail = '') {
  operationState.step = Math.max(0, Math.min(index, operationState.steps.length-1));
  $$('#operationSteps .operation-step').forEach((el,i) => el.className = `operation-step ${i<operationState.step?'done':i===operationState.step?'active':''}`);
  if (detail) $('#operationDetail').textContent = detail;
}

function setOperationProgress(percent) {
  const fill = $('#operationFill'); if (!fill) return;
  if (percent == null) { fill.className='operation-fill indeterminate'; fill.style.width='38%'; $('#operationPercent').textContent='In progress'; }
  else { const p=Math.max(0,Math.min(100,Number(percent)||0)); fill.className='operation-fill'; fill.style.width=p+'%'; $('#operationPercent').textContent=Math.round(p)+'%'; }
}

function endOperation() {
  clearInterval(operationState.timer); clearInterval(operationState.stepTimer);
  $('#operationOverlay')?.classList.remove('show');
  document.body.style.overflow = '';
}

function failOperation(message) { endOperation(); toast(message); }

async function api(url, opt = {}) {
  const r = await fetch(url, opt);
  let d = {};
  try { d = await r.json(); } catch {}
  if (!r.ok) throw new Error(d.detail || 'Request failed');
  return d;
}

function toast(msg, ok = false) {
  const x = $('#toast');
  if (!x) return;
  x.textContent = msg;
  x.className = 'toast ' + (ok ? 'ok' : 'error');
  x.hidden = false;
  clearTimeout(x._timeout);
  x._timeout = setTimeout(() => x.hidden = true, 5000);
}

async function logout() { await api('/api/auth/logout', { method: 'POST' }); location = '/login'; }

function providerOptionsHtml(selected) {
  const items = new Map();
  [...(ttsSettingsState.available||[]), ...(ttsSettingsState.providers||[])].forEach(p => items.set(p.id, p));
  return [...items.values()].map(p => `<option value="${esc(p.id)}" ${p.id===selected?'selected':''}>${esc(p.name)}</option>`).join('');
}

function refreshProviderSelects() {
  const def = $('#defaultTtsProvider');
  const make = $('#ttsProviderSelect');
  if (def) def.innerHTML = providerOptionsHtml(ttsSettingsState.default_provider);
  if (make) {
    const previous = make.value;
    make.innerHTML = providerOptionsHtml(previous || ttsSettingsState.default_provider);
    if ([...make.options].some(option => option.value === previous)) make.value = previous;
    else if ([...make.options].some(option => option.value === ttsSettingsState.default_provider)) make.value = ttsSettingsState.default_provider;
    toggleTtsVoice();
  }
}

function renderTtsProviders() {
  const box = $('#ttsProvidersList');
  if (!box) return;
  box.innerHTML = (ttsSettingsState.providers||[]).map(p => `
    <div class="setting-provider flex" style="justify-content:space-between">
      <div><b>${esc(p.name)}</b><div class="muted small">${esc(p.model)} · ${p.has_api_key?'API key saved':'API key missing'}</div></div>
      <div class="flex"><button type="button" class="btn btn-ghost" onclick="editTtsProvider('${esc(p.id)}')">Edit</button><button type="button" class="btn btn-danger" onclick="removeTtsProvider('${esc(p.id)}')">Remove</button></div>
    </div>`).join('') || '<p class="muted small">No custom providers yet. Free voices are still available.</p>';
  refreshProviderSelects();
}

async function loadTtsSettings() {
  try {
    const data = await api('/api/settings/tts');
    ttsSettingsState = data;
    renderTtsProviders();
    if (!ttsProviderInitialized && $('#ttsProviderSelect') && [...$('#ttsProviderSelect').options].some(o=>o.value===data.default_provider)) {
      $('#ttsProviderSelect').value = data.default_provider;
      toggleTtsVoice(); ttsProviderInitialized = true;
    }
    if ($('#pexelsKeyInput')) $('#pexelsKeyInput').placeholder = data.pexels_api_key_set ? 'A key is saved; leave blank to keep it' : 'Paste your Pexels API key';
  } catch (error) { toast(error.message); }
}

async function openSettings() {
  $('#ttsSettingsModal').hidden = false;
  await loadTtsSettings();
}

function closeSettings() { $('#ttsSettingsModal').hidden = true; }

function resetProviderForm() {
  $('#providerForm')?.reset();
  $('#providerId').value = '';
  $('#providerVoice').value = 'alloy';
  $('#providerFormTitle').textContent = 'Add compatible speech provider';
  $('#providerSubmit').textContent = 'Add provider';
}

function useOpenAIPreset() {
  resetProviderForm();
  $('#providerName').value = 'OpenAI';
  $('#providerModel').value = 'gpt-4o-mini-tts';
  $('#providerUrl').value = 'https://api.openai.com/v1';
  $('#providerVoice').value = 'alloy';
  $('#providerFormat').value = 'mp3';
  $('#providerFormTitle').textContent = 'Configure OpenAI speech';
}

function editTtsProvider(id) {
  const p = ttsSettingsState.providers.find(item => item.id === id);
  if (!p) return;
  $('#providerId').value = p.id;
  $('#providerName').value = p.name;
  $('#providerModel').value = p.model;
  $('#providerUrl').value = p.base_url;
  $('#providerKey').value = '';
  $('#providerKey').placeholder = p.has_api_key ? 'Key saved — blank keeps the existing key' : 'API key';
  $('#providerVoice').value = p.default_voice || 'alloy';
  $('#providerFormat').value = p.response_format || 'mp3';
  $('#providerFormTitle').textContent = `Edit ${p.name}`;
  $('#providerSubmit').textContent = 'Update provider';
}

function saveProviderForm(event) {
  event.preventDefault();
  const id = $('#providerId').value || ('tts' + Date.now().toString(36) + Math.random().toString(36).slice(2,7));
  const provider = {
    id, name:$('#providerName').value.trim(), model:$('#providerModel').value.trim(),
    base_url:$('#providerUrl').value.trim(), api_key:$('#providerKey').value.trim(),
    default_voice:$('#providerVoice').value.trim() || 'alloy', response_format:$('#providerFormat').value,
    has_api_key:ttsSettingsState.providers.find(item=>item.id===id)?.has_api_key || false,
  };
  const index = ttsSettingsState.providers.findIndex(item=>item.id===id);
  if (index < 0) ttsSettingsState.providers.push(provider); else ttsSettingsState.providers[index] = provider;
  renderTtsProviders(); resetProviderForm();
  if ($('#defaultTtsProvider')) $('#defaultTtsProvider').value = id;
}

function removeTtsProvider(id) {
  ttsSettingsState.providers = ttsSettingsState.providers.filter(item=>item.id!==id);
  if (ttsSettingsState.default_provider === id) ttsSettingsState.default_provider = ttsSettingsState.available[0]?.id || 'edge';
  renderTtsProviders();
}

async function saveTtsSettings() {
  try {
    const data = await api('/api/settings/tts', {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        providers:ttsSettingsState.providers,
        default_provider:$('#defaultTtsProvider').value,
        pexels_api_key:$('#pexelsKeyInput').value,
        clear_pexels_api_key:$('#clearPexelsKey').checked,
      }),
    });
    ttsSettingsState = data;
    $('#pexelsKeyInput').value = '';
    $('#clearPexelsKey').checked = false;
    renderTtsProviders();
    if ($('#ttsProviderSelect') && [...$('#ttsProviderSelect').options].some(o=>o.value===data.default_provider)) {
      $('#ttsProviderSelect').value = data.default_provider;
      toggleTtsVoice();
    }
    toast('Settings saved on this computer.', true);
  } catch (error) { toast(error.message); }
}

function toggleTtsVoice() {
  const selected = $('#ttsProviderSelect')?.value || 'edge';
  const custom = !['edge','gtts'].includes(selected);
  if ($('#builtinVoiceWrap')) $('#builtinVoiceWrap').style.display = custom ? 'none' : '';
  if ($('#customVoiceWrap')) $('#customVoiceWrap').style.display = custom ? '' : 'none';
  if (custom && $('#tts_custom_voice')) {
    const provider = ttsSettingsState.providers.find(p=>p.id===selected);
    $('#tts_custom_voice').placeholder = provider?.default_voice || 'Use configured provider default';
  }
}

function setup() {
  $('#setupForm')?.addEventListener('submit', async e => {
    e.preventDefault();
    beginOperation('Creating your studio', 'Securing the local Story Tools account', ['Validating details','Creating local account','Opening your studio']);
    try { await api('/api/setup', { method: 'POST', body: new FormData(e.target) }); location = '/login'; }
    catch (x) { failOperation(x.message); }
  });
}

function login() {
  $('#loginForm')?.addEventListener('submit', async e => {
    e.preventDefault();
    beginOperation('Opening Story Tools', 'Checking your local account', ['Authenticating','Loading projects','Preparing dashboard']);
    try { await api('/api/auth/login', { method: 'POST', body: new FormData(e.target) }); location = '/dashboard'; }
    catch (x) { failOperation(x.message); }
  });
}

async function loadProjects(filter = '', status = '') {
  const d = await api('/api/projects');
  const box = $('#projects');
  if (!box) return;
  let projects = d.projects;
  if (status) projects = projects.filter(p => p.status === status);
  if (filter) projects = projects.filter(p => p.name.toLowerCase().includes(filter.toLowerCase()));
  if (!projects.length) { box.innerHTML = '<div class="card muted">No projects match your criteria.</div>'; return; }
  box.innerHTML = projects.map(p => `
    <div class="project-item">
      <div>
        <h3>${esc(p.name)}</h3>
        <div class="muted small">${new Date(p.created_at).toLocaleString()}</div>
        <div style="margin-top:6px"><span class="status ${esc(p.status)}">${esc(p.status)}</span></div>
      </div>
      <div class="flex">
        <button class="btn btn-primary" onclick="openProject('${p.id}')">Open</button>
        ${p.status === 'Completed' ? `<a class="btn btn-success" href="/api/projects/${p.id}/download">Download</a>` : ''}
        <button class="btn btn-danger" onclick="deleteProject('${p.id}')">Delete</button>
      </div>
    </div>
  `).join('');
}

function applyFilters() {
  const f = $('#searchInput')?.value || '';
  const s = $('#statusFilter')?.value || '';
  loadProjects(f, s);
}

async function deleteProject(id) {
  if (!confirm('Delete this project?')) return;
  beginOperation('Removing project', 'Cleaning its local video and audio files', ['Locating project','Removing files','Refreshing dashboard']);
  try { await api('/api/projects/' + id, { method: 'DELETE' }); await applyFilters(); endOperation(); }
  catch (x) { failOperation(x.message); }
}

function openProject(id) { location.href = '/project/' + encodeURIComponent(id); }

function createProject() {
  $('#createForm')?.addEventListener('submit', async e => {
    e.preventDefault();
    const f = new FormData(e.target);
    const submit = e.submitter;
    const oldLabel = submit?.textContent;
    if (submit) { submit.disabled = true; submit.textContent = f.get('mode') === 'tts' ? 'Creating narration…' : 'Creating project…'; }
    const scriptMode = f.get('mode') === 'tts';
    beginOperation(
      scriptMode ? 'Turning your story into narration' : 'Creating your video project',
      scriptMode ? 'The voice and scene timing are generated together for accurate sync' : 'Preparing your uploaded narration',
      scriptMode ? ['Understanding the script','Choosing the natural voice','Generating narration','Aligning words and timing','Creating subtitle scenes'] : ['Creating project','Uploading narration','Reading audio timing','Preparing subtitle workflow']
    );
    try {
      const d = await api('/api/projects', { method: 'POST', body: f });
      const projectId = d.project.id;
      const mode = f.get('mode');
      if (mode === 'tts') {
        location.href = `/project/${projectId}?view=videos`;
      } else {
        await uploadFiles(projectId, mode);
      }
    } catch (x) {
      failOperation(x.message);
      if (submit) { submit.disabled = false; submit.textContent = oldLabel; }
    }
  });
}

async function uploadFiles(projectId, mode) {
  const audio = $('#audio'), srt = $('#srt');
  if (!audio?.files?.length) { toast('Choose an audio file.'); return; }
  if (mode === 'existing' && !srt?.files?.length) { toast('Choose an SRT file.'); return; }
  const fd = new FormData();
  fd.append('audio', audio.files[0]);
  if (srt?.files?.length) fd.append('srt', srt.files[0]);
  setOperationStep(1, 'Uploading the selected files securely to the local studio');
  const d = await api(`/api/projects/${projectId}/upload`, { method: 'POST', body: fd });
  if (mode === 'automatic') location.href = `/project/${projectId}?view=transcribe`;
  else location.href = `/project/${projectId}?view=scenes`;
}

async function previewVoice(button) {
  const form = $('#createForm');
  const text = form?.querySelector('[name=script]')?.value?.trim();
  if (!text) { toast('Write a little of your script first.'); return; }
  const fd = new FormData();
  fd.append('text', text.slice(0, 240));
  fd.append('voice', form.querySelector('[name=tts_voice]').value);
  fd.append('style', form.querySelector('[name=tts_style]').value);
  fd.append('provider_id', form.querySelector('[name=tts_provider]').value);
  fd.append('custom_voice', form.querySelector('[name=tts_custom_voice]').value);
  const old = button.textContent;
  button.disabled = true; button.textContent = 'Creating preview…';
  beginOperation('Creating voice preview', 'Generating a natural sample from your script', ['Detecting the language','Preparing pronunciation','Generating neural voice','Finishing the audio sample']);
  try {
    const response = await fetch('/api/voice-preview', {method:'POST', body:fd});
    if (!response.ok) {
      let data = {}; try { data = await response.json(); } catch {}
      throw new Error(data.detail || 'Voice preview failed');
    }
    const audio = $('#voicePreview');
    if (audio.src) URL.revokeObjectURL(audio.src);
    audio.src = URL.createObjectURL(await response.blob());
    audio.hidden = false; endOperation(); await audio.play();
  } catch (x) { failOperation(x.message); }
  finally { button.disabled = false; button.textContent = old; }
}

function toggleMode() {
  const applyMode = () => {
    const mode = $('input[name=mode]:checked')?.value || 'tts';
    const ex = mode === 'existing';
    const isTTS = mode === 'tts';
    $('#srtWrap').style.display = ex ? 'block' : 'none';
    $('#autoWrap').style.display = (ex || isTTS) ? 'none' : 'block';
    $('#scriptWrap').style.display = isTTS ? 'block' : 'none';
    $('#voiceWrap').style.display = isTTS ? 'grid' : 'none';
    $('#voicePreviewWrap').style.display = isTTS ? 'flex' : 'none';
    const script = $('#scriptWrap textarea');
    if (isTTS) script?.setAttribute('required', 'required'); else script?.removeAttribute('required');
    const audioWrap = $('#audioWrap');
    if (isTTS) {
      audioWrap.style.display = 'none';
      audioWrap.querySelector('input')?.removeAttribute('required');
    } else {
      audioWrap.style.display = 'block';
      audioWrap.querySelector('input')?.setAttribute('required', 'required');
    }
  };
  $$('input[name=mode]').forEach(x => x.onchange = applyMode);
  applyMode();
}

async function projectPage() {
  const q = new URLSearchParams(location.search);
  const view = q.get('view') || 'home';
  if (!window.projectId) { toast('No project specified'); return; }
  const d = await api('/api/projects/' + window.projectId);
  renderProject(d.project, d.scenes, view);
}

function renderProject(p, scenes, view) {
  const app = $('#projectView');
  if (!app) return;
  let body = '';
  if (view === 'transcribe') body = transcribeView(p);
  else if (view === 'scenes') body = scenesView(p, scenes);
  else if (view === 'videos') body = videosView(p, scenes);
  else if (view === 'processing') body = processingView(p);
  else if (view === 'result') body = resultView(p);
  else body = projectHome(p, scenes);
  app.innerHTML = `
    <div class="topbar flex" style="justify-content:space-between; margin-bottom:20px; flex-wrap:wrap;">
      <div class="brand flex" style="gap:10px; font-weight:700; font-size:1.4rem;">
        <span style="background:linear-gradient(135deg,var(--primary),#9b6dff); padding:6px 12px; border-radius:12px;">ST</span>
        Story Tools
      </div>
      <div class="flex">
        <button class="btn btn-ghost" onclick="location.href='/dashboard'">Dashboard</button>
        <button class="btn btn-ghost" onclick="logout()">Logout</button>
      </div>
    </div>
    ${body}
    <div id="toast" hidden></div>
  `;
  if (view === 'transcribe') bindTranscribe();
  if (view === 'scenes') bindSubtitleEditor();
  if (view === 'videos') bindVideos();
  if (view === 'processing') pollProgress();
}

function projectHome(p, scenes) {
  const hasSubtitles = !!p.srt_name;
  const allVideosSelected = scenes.every(s => s.selected_video);
  return `
    <div class="card hero">
      <h1>${esc(p.name)}</h1>
      <p class="muted">Turn your narration into a complete short video.</p>
      <div class="steps">
        ${step('1','Upload Audio',true)}
        ${step('2','Subtitles',!!p.srt_name)}
        ${step('3','Scenes',!!scenes.length)}
        ${step('4','Videos',scenes.some(s=>s.selected_video))}
        ${step('5','Create Video',p.status==='Processing'||p.status==='Completed')}
        ${step('6','Download',p.status==='Completed')}
      </div>
    </div>
    <div class="grid grid-2">
      <div class="card">
        <h2>Status</h2>
        <span class="status ${esc(p.status)}">${esc(p.status)}</span>
        <p class="muted small">${esc(p.current_task||'Ready')}</p>
        <div class="progress-bar"><div style="width:${p.progress}%"></div></div>
      </div>
      <div class="card">
        <h2>Output</h2>
        <div style="font-size:2rem; font-weight:700;">${esc(p.output_format)}</div>
        <p class="muted small">Format</p>
        <div class="flex">
          ${hasSubtitles ? `<button class="btn btn-primary" onclick="location.href='/project/${p.id}?view=videos'">Select Videos</button>` : `<button class="btn btn-primary" onclick="location.href='/project/${p.id}?view=transcribe'">Generate Subtitles</button>`}
          ${allVideosSelected ? `<button class="btn btn-success" onclick="createVideo()">Create Video</button>` : ''}
          ${p.status==='Completed'?`<a class="btn btn-success" href="/api/projects/${p.id}/download">Download</a>`:''}
        </div>
      </div>
    </div>
  `;
}

function step(n, t, done) {
  return `<div class="step ${done?'done':''}"><b>${n}</b> ${t}</div>`;
}

function transcribeView(p) {
  return `
    <div class="card">
      <h1>Generate subtitles</h1>
      <p class="muted">Whisper will transcribe the uploaded audio and create timestamps.</p>
      <form id="transcribeForm" class="grid grid-2">
        <div><label>Language</label><select name="language"><option value="auto">Auto Detect</option><option>Arabic</option><option>English</option><option>French</option><option>Spanish</option></select></div>
        <div><label>Whisper model</label><select name="model"><option>tiny</option><option>base</option><option selected>small</option><option>medium</option><option>large-v3</option></select></div>
        <div class="flex"><button class="btn btn-primary">Generate Subtitles</button><button type="button" class="btn btn-ghost" onclick="location.href='/project/${p.id}'">Back</button></div>
      </form>
    </div>
  `;
}

function bindTranscribe() {
  $('#transcribeForm').onsubmit = async e => {
    e.preventDefault();
    beginOperation('Starting transcription', 'Preparing your audio for timestamped subtitles', ['Checking audio','Loading speech model','Starting transcription']);
    try {
      await api(`/api/projects/${window.projectId}/transcribe`, { method: 'POST', body: new FormData(e.target) });
      location.href = `/project/${window.projectId}?view=processing`;
    } catch (x) { failOperation(x.message); }
  };
}

function scenesView(p, scenes) {
  return `
    <div class="card">
      <div class="flex" style="justify-content:space-between; flex-wrap:wrap;">
        <div><h1>Subtitle Editor</h1><p class="muted">Edit text and timestamps. Arabic is supported.</p></div>
        <div class="flex"><button class="btn btn-ghost" onclick="addScene()">Add subtitle</button><button class="btn btn-primary" onclick="saveSubtitles()">Save</button></div>
      </div>
      <div id="sceneEditor">${scenes.map((s,i)=>sceneEditor(s,i)).join('')}</div>
      <div class="flex mt-2"><button class="btn btn-primary" onclick="saveSubtitles()">Save & Continue</button><button class="btn btn-ghost" onclick="location.href='/project/${p.id}'">Back</button></div>
    </div>
  `;
}

function sceneEditor(s, i) {
  return `
    <div class="scene" data-index="${i}">
      <div class="scene-head"><b>Scene ${i+1}</b><button class="btn btn-danger" onclick="this.closest('.scene').remove()">Delete</button></div>
      <div class="grid grid-3">
        <div><label>Start</label><input class="start" value="${secToSrt(s.start)}"></div>
        <div><label>End</label><input class="end" value="${secToSrt(s.end)}"></div>
        <div><label>Duration</label><input disabled value="${(s.end-s.start).toFixed(3)}s"></div>
      </div>
      <div><label>Text</label><textarea class="text" dir="auto">${esc(s.text)}</textarea></div>
    </div>
  `;
}

function secToSrt(v) {
  v = Number(v) || 0;
  let ms = Math.round(v * 1000), h = Math.floor(ms / 3600000);
  ms %= 3600000; let m = Math.floor(ms / 60000);
  ms %= 60000; let s = Math.floor(ms / 1000);
  ms %= 1000;
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')},${String(ms).padStart(3,'0')}`;
}

function srtToSec(v) {
  const m = v.match(/(\d+):(\d{2}):(\d{2})[,.](\d{3})/);
  if (!m) throw new Error('Invalid timestamp: ' + v);
  return +m[1] * 3600 + +m[2] * 60 + +m[3] + +m[4] / 1000;
}

function addScene() {
  const box = $('#sceneEditor');
  const i = $$('.scene').length;
  box.insertAdjacentHTML('beforeend', sceneEditor({ start: 0, end: 1, text: '' }, i));
}

async function saveSubtitles() {
  try {
    const scenes = $$('.scene').map((el, i) => {
      const start = srtToSec(el.querySelector('.start').value);
      const end = srtToSec(el.querySelector('.end').value);
      if (end < start) throw new Error(`Scene ${i+1}: end before start`);
      return { index: i + 1, start, end, text: el.querySelector('.text').value.trim(), selected_video: null, videos: [] };
    });
    beginOperation('Saving timed scenes', 'Checking subtitle timing and preparing video matching', ['Validating timestamps','Saving subtitles','Preparing scene search']);
    await api(`/api/projects/${window.projectId}/subtitles`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scenes }) });
    location.href = `/project/${window.projectId}?view=videos`;
  } catch (x) { failOperation(x.message); }
}

// ---- VIDEOS VIEW with dynamic updates (no page reload) ----
function videosView(p, scenes) {
  return `
    <div class="card">
      <div class="flex" style="justify-content:space-between; flex-wrap:wrap;">
        <div><h1>Choose matching footage</h1><p class="muted">Suggestions are created from each timed narration scene. Add blur areas to selected clips when needed.</p></div>
        <div class="flex"><button class="btn btn-ghost" onclick="searchAllVideos()">Refresh suggestions</button><button class="btn btn-success" onclick="autoSelectAll()">🎲 Randomize selection</button></div>
      </div>
      <div id="videoScenes">${scenes.map((s,i)=>videoScene(s,i,p.output_format)).join('')}</div>
      <div class="flex" style="justify-content:space-between; flex-wrap:wrap; margin-top:16px;">
        <label class="toggle-label flex" style="gap:10px; align-items:center;">
          <span class="muted">Movie captions · small at bottom</span>
          <span class="switch">
            <input type="checkbox" id="burnSubtitles" ${p.burn_subtitles!==0?'checked':''}>
            <span class="slider"></span>
          </span>
        </label>
        <div style="min-width:260px; flex:1;">
          <label>Background music (optional)</label>
          <input id="backgroundMusic" type="file" accept=".mp3,.wav,.m4a,.aac,.flac,.ogg,.webm">
          <label class="small muted">Music volume <span id="musicVolumeLabel">12%</span></label>
          <input id="musicVolume" type="range" min="0" max="0.5" step="0.01" value="${Number(p.music_volume ?? 0.12)}" oninput="$('#musicVolumeLabel').textContent=Math.round(this.value*100)+'%'">
        </div>
        <div class="flex">
          <button class="btn btn-ghost" onclick="location.href='/project/${p.id}?view=scenes'">Edit scenes</button>
          <button class="btn btn-primary" onclick="createVideo()">Create Video</button>
          <button class="btn btn-ghost" onclick="location.href='/project/${p.id}'">Back</button>
        </div>
      </div>
    </div>
  `;
}

function videoScene(s, i, format) {
  const selectedId = s.selected_video?.id || null;
  return `
    <div class="scene" data-scene-index="${i}" data-video-url="${esc(s.selected_video?.mp4_url || '')}" data-blur-regions="${esc(JSON.stringify(s.blur_regions||[]))}" data-output-format="${esc(format||'9:16')}">
      <div class="scene-head">
        <div><b>Scene ${i+1}</b> <span class="badge">${s.duration}s</span></div>
        <span class="muted small">${esc(s.text)}</span>
      </div>
      <label>Search query</label>
      <div class="flex"><input id="q${i}" value="${esc(s.query||'')}" /><button class="btn btn-ghost" onclick="searchOneVideo(${i})">Search</button></div>
      <label class="flex small" style="gap:8px; margin-top:10px;"><input type="checkbox" style="width:auto" ${s.blur?'checked':''} onchange="setSceneBlur(${i},this.checked)"> Blur this entire scene if it contains sensitive imagery</label>
      <div class="flex mt-2"><button class="btn btn-ghost blur-edit-button" onclick="openBlurEditor(${i})" ${selectedId?'':'disabled'}>Blur selected parts (${(s.blur_regions||[]).length})</button><span class="muted small">Select a clip to edit blur areas.</span></div>
      <div class="video-grid" id="results${i}" style="margin-top:10px;">${(s.videos||[]).map(v=>videoCard(v,i,selectedId)).join('')}</div>
    </div>
  `;
}

function videoCard(v, sceneIdx, selectedId) {
  const isSelected = v.id === selectedId;
  return `
    <div class="video-card" data-video-id="${v.id}" data-scene="${sceneIdx}">
      <video muted preload="metadata" src="${esc(v.mp4_url)}" onmouseenter="this.play()" onmouseleave="this.pause(); this.currentTime=0;"></video>
      <div class="body">
        <div class="small muted">${esc(v.width)}×${esc(v.height)} · ${esc(v.duration)}s</div>
        <div class="flex" style="margin-top:6px;">
          <button class="btn ${isSelected?'btn-success':'btn-primary'}" onclick='selectVideo(${sceneIdx},${JSON.stringify(v).replace(/'/g,"&#39;")})'>${isSelected?'Selected':'Select'}</button>
          <a class="btn btn-ghost" href="${esc(v.pexels_url||'#')}" target="_blank" rel="noopener">Pexels</a>
        </div>
      </div>
    </div>
  `;
}

// ----- Dynamic API calls without reload -----
async function searchAllVideos() {
  beginOperation('Finding footage for every scene', 'Matching visual ideas to the meaning and timing of your narration', ['Reading scene meaning','Creating visual keywords','Searching stock footage','Ranking matching clips','Preparing previews']);
  try {
    const d = await api(`/api/projects/${window.projectId}/search-videos`, { method: 'POST' });
    d.scenes.forEach((s, i) => {
      const grid = document.getElementById('results' + i);
      if (grid) {
        const selectedId = s.selected_video?.id || null;
        grid.innerHTML = s.videos.map(v => videoCard(v, i, selectedId)).join('');
      }
    });
    endOperation(); toast('Search completed', true);
  } catch (x) { failOperation(x.message); }
}

async function searchOneVideo(i) {
  beginOperation(`Searching footage for scene ${i+1}`, 'Looking for clips that match this scene', ['Reading your query','Searching stock footage','Ranking results','Preparing previews']);
  try {
    const query = document.getElementById('q' + i)?.value || '';
    const d = await api(`/api/projects/${window.projectId}/search-videos`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({scene_index:i+1, query}) });
    d.scenes.forEach((s, idx) => {
      const grid = document.getElementById('results' + idx);
      if (grid) {
        const selectedId = s.selected_video?.id || null;
        grid.innerHTML = s.videos.map(v => videoCard(v, idx, selectedId)).join('');
      }
    });
    endOperation(); toast('Search updated', true);
  } catch (x) { failOperation(x.message); }
}

async function selectVideo(i, v) {
  beginOperation(`Selecting scene ${i+1} footage`, 'Saving the chosen clip for the final edit', ['Checking clip','Saving selection']);
  try {
    await api(`/api/projects/${window.projectId}/select-video`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scene_index: i + 1, video: v }) });
    const sceneDiv = document.querySelector(`.scene[data-scene-index="${i}"]`);
    if (sceneDiv) {
      const cards = sceneDiv.querySelectorAll('.video-card');
      cards.forEach(card => {
        const btn = card.querySelector('.btn');
        if (btn) {
          if (card.dataset.videoId == v.id) {
            btn.className = 'btn btn-success';
            btn.textContent = 'Selected';
          } else {
            btn.className = 'btn btn-primary';
            btn.textContent = 'Select';
          }
        }
      });
      sceneDiv.dataset.videoUrl = v.mp4_url;
      const editButton = sceneDiv.querySelector('.blur-edit-button');
      if (editButton) editButton.disabled = false;
    }
    endOperation(); toast('Video selected', true);
  } catch (x) { failOperation(x.message); }
}

async function autoSelectAll() {
  beginOperation('Randomizing all scene footage', 'Choosing a fresh set of matching clips', ['Reviewing suggestions','Mixing visual choices','Saving the new selection']);
  try {
    const d = await api(`/api/projects/${window.projectId}/auto-select`, {method:'POST'});
    const scenes = d.scenes;
    for (let i = 0; i < scenes.length; i++) {
      const s = scenes[i];
      if (s.selected_video) {
        const first = s.selected_video;
        const sceneDiv = document.querySelector(`.scene[data-scene-index="${i}"]`);
        if (sceneDiv) {
          const cards = sceneDiv.querySelectorAll('.video-card');
          cards.forEach(card => {
            const btn = card.querySelector('.btn');
            if (btn) {
              if (card.dataset.videoId == first.id) {
                btn.className = 'btn btn-success';
                btn.textContent = 'Selected';
              } else {
                btn.className = 'btn btn-primary';
                btn.textContent = 'Select';
              }
            }
          });
          sceneDiv.dataset.videoUrl = first.mp4_url;
          const editButton = sceneDiv.querySelector('.blur-edit-button');
          if (editButton) editButton.disabled = false;
        }
      }
    }
    endOperation(); toast(`Randomly selected ${d.selected} videos`, true);
  } catch (x) { failOperation(x.message); }
}

async function setSceneBlur(i, blur) {
  try {
    await api(`/api/projects/${window.projectId}/scene-settings`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({scene_index:i+1, blur})});
    toast(blur ? 'Scene blur enabled' : 'Scene blur disabled', true);
  } catch (x) { toast(x.message); }
}

let blurEditorState = null;

function openBlurEditor(sceneIndex) {
  closeBlurEditor();
  const scene = document.querySelector(`.scene[data-scene-index="${sceneIndex}"]`);
  const videoUrl = scene?.dataset.videoUrl;
  if (!videoUrl) { toast('Select a video clip before adding blur areas.'); return; }
  let regions = [];
  try { regions = JSON.parse(scene.dataset.blurRegions || '[]'); } catch {}
  const format = scene.dataset.outputFormat || '9:16';
  const ratioClass = format === '16:9' ? 'landscape' : format === '1:1' ? 'square' : '';
  document.body.insertAdjacentHTML('beforeend', `
    <div id="blurEditorModal" class="modal-backdrop" style="z-index:1600">
      <div class="modal-card" style="width:min(920px,100%)">
        <div class="flex" style="justify-content:space-between"><div><div class="eyebrow">Scene ${sceneIndex+1}</div><h1>Blur selected areas</h1></div><button class="btn btn-ghost" onclick="closeBlurEditor()">Close</button></div>
        <p class="muted small">Choose “Add area”, then drag over the part you want blurred. Drag a marked area to move it; use its corner handle to resize.</p>
        <div class="blur-stage ${ratioClass}" id="blurStage"><video src="${esc(videoUrl)}" controls muted playsinline></video><div class="blur-layer" id="blurLayer"></div></div>
        <div class="flex"><button class="btn btn-ghost" id="drawBlurArea">Add blur area</button><span id="blurEditorHint" class="muted small">Existing areas are shown in green.</span></div>
        <div class="flex mt-2" style="justify-content:flex-end"><button class="btn btn-primary" onclick="saveBlurEditor()">Save areas</button></div>
      </div>
    </div>`);
  blurEditorState = { sceneIndex, scene, regions:regions.map(r=>({...r})), draw:false, operation:null };
  const layer = $('#blurLayer');
  $('#drawBlurArea').onclick = () => {
    if (blurEditorState.regions.length >= 12) { toast('A scene can have up to 12 blur areas.'); return; }
    blurEditorState.draw = true;
    $('#blurEditorHint').textContent = 'Drag on the video to draw a blur area.';
  };
  layer.addEventListener('pointerdown', startBlurPointer);
  layer.addEventListener('pointermove', moveBlurPointer);
  layer.addEventListener('pointerup', endBlurPointer);
  layer.addEventListener('pointercancel', endBlurPointer);
  renderBlurRegions();
}

function blurPointerPosition(event) {
  const rect = $('#blurLayer').getBoundingClientRect();
  return { x:Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)), y:Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height)) };
}

function startBlurPointer(event) {
  if (!blurEditorState) return;
  const handle = event.target.closest('.resize');
  const regionElement = event.target.closest('.blur-region');
  const layer = $('#blurLayer');
  const point = blurPointerPosition(event);
  if (regionElement) {
    const index = Number(regionElement.dataset.index);
    const region = blurEditorState.regions[index];
    event.preventDefault(); event.stopPropagation();
    blurEditorState.operation = {
      type:handle?'resize':'move', index, pointer:point,
      origin:{x:region.x,y:region.y,width:region.width,height:region.height},
    };
    layer.setPointerCapture(event.pointerId);
    return;
  }
  if (!blurEditorState.draw) return;
  event.preventDefault();
  const region = {x:point.x,y:point.y,width:0.001,height:0.001};
  blurEditorState.regions.push(region);
  blurEditorState.operation = {type:'draw',index:blurEditorState.regions.length-1,pointer:point,origin:{...region}};
  blurEditorState.draw = false;
  $('#blurEditorHint').textContent = 'Drag to set the size. You can move or resize it afterwards.';
  layer.setPointerCapture(event.pointerId);
  renderBlurRegions();
}

function moveBlurPointer(event) {
  const op = blurEditorState?.operation;
  if (!op) return;
  const point = blurPointerPosition(event), dx = point.x-op.pointer.x, dy = point.y-op.pointer.y;
  const region = blurEditorState.regions[op.index];
  if (op.type === 'move') {
    region.x = Math.max(0,Math.min(1-op.origin.width,op.origin.x+dx));
    region.y = Math.max(0,Math.min(1-op.origin.height,op.origin.y+dy));
  } else if (op.type === 'resize') {
    region.width = Math.max(.015,Math.min(1-op.origin.x,op.origin.width+dx));
    region.height = Math.max(.015,Math.min(1-op.origin.y,op.origin.height+dy));
  } else {
    region.x = Math.min(op.pointer.x,point.x); region.y = Math.min(op.pointer.y,point.y);
    region.width = Math.max(.001,Math.abs(dx)); region.height = Math.max(.001,Math.abs(dy));
  }
  renderBlurRegions();
}

function endBlurPointer() {
  if (!blurEditorState?.operation) return;
  const op = blurEditorState.operation;
  const region = blurEditorState.regions[op.index];
  if (op.type === 'draw' && (region.width < .015 || region.height < .015)) blurEditorState.regions.splice(op.index,1);
  blurEditorState.operation = null;
  renderBlurRegions();
}

function renderBlurRegions() {
  const layer = $('#blurLayer');
  if (!layer || !blurEditorState) return;
  layer.innerHTML = blurEditorState.regions.map((region,index)=>`
    <div class="blur-region" data-index="${index}" style="left:${region.x*100}%;top:${region.y*100}%;width:${region.width*100}%;height:${region.height*100}%">
      <button type="button" title="Remove area" onclick="removeBlurRegion(${index});event.stopPropagation()">×</button><span class="resize"></span>
    </div>`).join('');
}

function removeBlurRegion(index) {
  blurEditorState?.regions.splice(index,1);
  renderBlurRegions();
}

async function saveBlurEditor() {
  if (!blurEditorState) return;
  const {sceneIndex,scene,regions} = blurEditorState;
  try {
    await api(`/api/projects/${window.projectId}/scene-settings`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({scene_index:sceneIndex+1,blur_regions:regions}),
    });
    scene.dataset.blurRegions = JSON.stringify(regions);
    const button = scene.querySelector('.blur-edit-button');
    if (button) button.textContent = `Blur selected parts (${regions.length})`;
    closeBlurEditor(); toast('Blur areas saved for this scene.', true);
  } catch (error) { toast(error.message); }
}

function closeBlurEditor() {
  $('#blurEditorModal')?.remove();
  blurEditorState = null;
}

function bindVideos() {
  const grids = $$('.video-grid');
  if (grids.length && grids.every(grid => !grid.children.length)) searchAllVideos();
}

async function createVideo() {
  const burn = $('#burnSubtitles')?.checked ? 1 : 0;
  try {
    beginOperation('Starting the final video', 'Saving your edit and preparing the render engine', ['Checking every scene','Uploading background music','Saving caption style','Starting video renderer']);
    const fd = new FormData();
    fd.append('burn_subtitles', String(burn));
    fd.append('music_volume', String($('#musicVolume')?.value || 0.12));
    const music = $('#backgroundMusic');
    if (music?.files?.length) fd.append('music', music.files[0]);
    await api(`/api/projects/${window.projectId}/create-video`, { method: 'POST', body: fd });
    location.href = `/project/${window.projectId}?view=processing`;
  } catch (x) { failOperation(x.message); }
}

// ---- Processing view with enhanced design ----
function processingView(p) {
  const pipeline = p.status === 'Transcribing' ? 'transcribe' : 'render';
  const stages = pipeline === 'transcribe'
    ? ['Analyze narration','Load speech model','Transcribe words','Align subtitle timing','Prepare scenes']
    : ['Check project assets','Download and fit footage','Combine scene edit','Mix voice and music','Render captions and export'];
  sessionStorage.setItem(`storyPipeline:${window.projectId}`, pipeline);
  if (!sessionStorage.getItem(`storyStarted:${window.projectId}`)) sessionStorage.setItem(`storyStarted:${window.projectId}`, String(Date.now()));
  return `
    <div class="card">
      <div class="eyebrow">Live production progress</div>
      <h1>${pipeline==='transcribe'?'Creating timed subtitles':'Building your final video'}</h1>
      <p class="muted">You can keep this page open and watch every stage as it completes.</p>
      <div class="render-status-grid">
        <div class="render-ring" id="renderRing" style="--progress:${p.progress}"><strong id="renderPercent">${p.progress}%</strong></div>
        <div>
          <div id="progressText" style="font-size:1.08rem;font-weight:650;line-height:1.5;">${esc(p.current_task||'Starting production…')}</div>
          <div class="progress-bar" style="margin:14px 0 9px;height:10px;"><div id="progressBar" style="width:${p.progress}%;height:100%;"></div></div>
          <div class="operation-meta"><span id="stepLabel">${getStepLabel(p.progress,pipeline)}</span><span id="renderElapsed">Elapsed 0:00</span></div>
        </div>
      </div>
      <div id="renderStages" class="render-stage-list mt-2">${stages.map((s,i)=>`<div class="render-stage ${i===0?'active':''}">${i+1}. ${s}</div>`).join('')}</div>
      <div class="flex mt-2"><button class="btn btn-ghost" onclick="location.href='/project/${p.id}'">Project</button>${pipeline==='transcribe'?'':`<button class="btn btn-ghost" onclick="location.href='/project/${p.id}?view=videos'">Edit video and blur areas</button>`}</div>
    </div>
  `;
}

function getStepLabel(progress, pipeline = 'render') {
  if (pipeline === 'transcribe') {
    if (progress < 10) return 'Analyzing narration';
    if (progress < 20) return 'Loading speech model';
    if (progress < 70) return 'Transcribing words';
    if (progress < 85) return 'Aligning timestamps';
    return 'Preparing scenes';
  }
  if (progress < 4) return 'Checking project assets';
  if (progress < 89) return 'Downloading and fitting scene footage';
  if (progress < 94) return 'Combining the edit';
  if (progress < 95) return 'Mixing voice and music';
  if (progress < 100) return 'Rendering captions and final export';
  return 'Complete';
}

function updateRenderStages(progress, pipeline) {
  let active;
  if (pipeline === 'transcribe') active = progress < 10 ? 0 : progress < 20 ? 1 : progress < 70 ? 2 : progress < 85 ? 3 : 4;
  else active = progress < 4 ? 0 : progress < 89 ? 1 : progress < 94 ? 2 : progress < 95 ? 3 : 4;
  $$('#renderStages .render-stage').forEach((el,i)=>el.className=`render-stage ${i<active?'done':i===active?'active':''}`);
}

async function pollProgress() {
  const loop = async () => {
    try {
      const d = await api(`/api/projects/${window.projectId}/progress`);
      const pct = d.percent;
      const pipeline = sessionStorage.getItem(`storyPipeline:${window.projectId}`) || (d.status==='Transcribing'?'transcribe':'render');
      $('#progressBar').style.width = pct + '%';
      $('#renderRing')?.style.setProperty('--progress', pct);
      if ($('#renderPercent')) $('#renderPercent').textContent = pct + '%';
      $('#progressText').textContent = d.task || getStepLabel(pct,pipeline);
      if ($('#stepLabel')) $('#stepLabel').textContent = getStepLabel(pct,pipeline);
      const started = Number(sessionStorage.getItem(`storyStarted:${window.projectId}`)) || Date.now();
      const elapsed = Math.max(0,Math.floor((Date.now()-started)/1000));
      if ($('#renderElapsed')) $('#renderElapsed').textContent = `Elapsed ${Math.floor(elapsed/60)}:${String(elapsed%60).padStart(2,'0')}`;
      updateRenderStages(pct,pipeline);
      if (d.status === 'Ready' && d.percent >= 85) { location.href = `/project/${window.projectId}?view=videos`; return; }
      if (d.status === 'Completed') { location.href = `/project/${window.projectId}?view=result`; return; }
      if (d.status === 'Error') { toast(d.task || 'Processing failed'); return; }
      setTimeout(loop, 1200);
    } catch (x) { toast(x.message); return; }
  };
  loop();
}

function resultView(p) {
  const source = String(p.script || p.name || '').trim();
  const hook = source.split(/[.!?؟\n]/)[0].trim().slice(0, 180) || p.name;
  const arabic = /[\u0600-\u06FF]/.test(source);
  const postText = `${hook}\n\n${arabic ? '#قصص #حكايات #الدارجة #ريلز #شورتس' : '#story #storytelling #shorts #reels #tiktok'}`;
  return `
    <div class="card">
      <h1>✅ Video completed!</h1>
      <p class="muted">${esc(p.name)}</p>
      <video class="preview" style="width:100%; max-height:70vh; border-radius:16px; background:#000;" controls src="/api/projects/${p.id}/download"></video>
      <div class="grid grid-3" style="margin-top:16px;">
        <div class="card"><b>Format</b><div style="font-size:1.8rem; font-weight:700;">${esc(p.output_format)}</div></div>
        <div class="card"><b>Mastered audio</b><div style="font-size:1.8rem; font-weight:700;">−16 LUFS</div></div>
        <div class="card"><b>Export</b><div style="font-size:1.8rem; font-weight:700;">1080p</div></div>
      </div>
      <div class="flex mt-2">
        <button class="btn btn-primary" onclick="location.href='/project/${p.id}?view=videos'">Edit video / blur areas</button>
        <a class="btn btn-success" href="/api/projects/${p.id}/download">Download Video</a>
        <a class="btn btn-ghost" href="/api/projects/${p.id}/download-srt">Download SRT</a>
        <a class="btn btn-ghost" href="/api/projects/${p.id}/download-cover">Download Cover</a>
        <button class="btn btn-primary" onclick="publishTo('youtube','${p.id}')">YouTube</button>
        <button class="btn btn-primary" onclick="publishTo('facebook','${p.id}')">Facebook</button>
        <button class="btn btn-primary" onclick="publishTo('tiktok','${p.id}')">TikTok</button>
        <button class="btn btn-ghost" onclick="location.href='/project/${p.id}'">Back</button>
      </div>
      <div class="card" style="margin-top:18px;box-shadow:none;">
        <div class="flex" style="justify-content:space-between"><div><b>Social caption pack</b><div class="muted small">A clean hook and useful hashtags from your story.</div></div><button class="btn btn-ghost" onclick="copySocialCaption()">Copy caption</button></div>
        <textarea id="socialCaption" readonly dir="auto" style="margin-top:12px;min-height:100px">${esc(postText)}</textarea>
      </div>
      <p class="muted small mt-2">Platform buttons download the finished video and open the official uploader. Your browser will ask you to choose the downloaded file because YouTube, Facebook, and TikTok require account authorization.</p>
    </div>
  `;
}

async function copySocialCaption() {
  const value = $('#socialCaption')?.value || '';
  try { await navigator.clipboard.writeText(value); toast('Caption copied', true); }
  catch { $('#socialCaption')?.select(); document.execCommand('copy'); toast('Caption copied', true); }
}

function publishTo(platform, projectId) {
  const destinations = {
    youtube: 'https://studio.youtube.com/channel/UC/videos/upload',
    facebook: 'https://www.facebook.com/reels/create',
    tiktok: 'https://www.tiktok.com/upload'
  };
  const a = document.createElement('a');
  a.href = `/api/projects/${projectId}/download`;
  a.download = '';
  document.body.appendChild(a); a.click(); a.remove();
  window.open(destinations[platform], '_blank', 'noopener');
  toast('Video downloaded. Choose it in the opened publisher.', true);
}

function dashboard() {
  loadProjects();
  loadTtsSettings();
  applyFilters();
  $('#searchInput')?.addEventListener('input', applyFilters);
  $('#statusFilter')?.addEventListener('change', applyFilters);
  $('#newProject')?.addEventListener('click', () => $('#createCard').hidden = false);
  $('#openSettings')?.addEventListener('click', openSettings);
  $('#cancelCreate')?.addEventListener('click', () => $('#createCard').hidden = true);
  createProject();
  toggleMode();
}

document.addEventListener('DOMContentLoaded', () => {
  ensureOperationUI();
  setup();
  login();
  if ($('#dashboardPage')) dashboard();
  if ($('#projectView')) {
    if (typeof window.projectId === 'undefined' || !window.projectId) {
      const m = window.location.pathname.match(/^\/project\/([^\/?#]+)/);
      if (m) window.projectId = m[1];
    }
    if (window.projectId) projectPage(); else toast('No project ID found');
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href*="/download"]');
    if (!link || link.closest('video')) return;
    const label = link.href.includes('download-cover') ? 'Preparing social cover' : link.href.includes('download-srt') ? 'Preparing subtitle file' : 'Preparing finished video';
    beginOperation(label, 'The download will begin in your browser', ['Checking the export','Preparing the file','Starting download']);
    setTimeout(endOperation, 2600);
  });
});
window.addEventListener('pageshow', endOperation);
"""

def render_page(kind: str, title: str, project_id: Optional[str] = None) -> str:
    if kind == "setup":
        body = """
        <div class="center" style="min-height:100vh; display:grid; place-items:center; padding:20px;">
          <div class="card" style="max-width:460px; width:100%;">
            <div class="brand flex" style="gap:10px; font-size:1.4rem; font-weight:700;">
              <span style="background:linear-gradient(135deg,var(--primary),#9b6dff); padding:6px 12px; border-radius:12px;">ST</span>
              Story Tools
            </div>
            <h1 style="margin-top:20px;">First‑time setup</h1>
            <p class="muted">Create the local administrator account.</p>
            <div id="toast" hidden></div>
            <form id="setupForm" class="grid" style="margin-top:16px;">
              <div><label>Username / Email</label><input name="username" required autocomplete="username"></div>
              <div><label>Password</label><input name="password" type="password" minlength="8" required autocomplete="new-password"></div>
              <button class="btn btn-primary btn-block">Create administrator</button>
            </form>
          </div>
        </div>
        """
    elif kind == "login":
        body = """
        <div class="center" style="min-height:100vh; display:grid; place-items:center; padding:20px;">
          <div class="card" style="max-width:420px; width:100%;">
            <div class="brand flex" style="gap:10px; font-size:1.4rem; font-weight:700;">
              <span style="background:linear-gradient(135deg,var(--primary),#9b6dff); padding:6px 12px; border-radius:12px;">ST</span>
              Story Tools
            </div>
            <h1 style="margin-top:20px;">Welcome back</h1>
            <p class="muted">Sign in to your local workspace.</p>
            <div id="toast" hidden></div>
            <form id="loginForm" class="grid" style="margin-top:16px;">
              <div><label>Username / Email</label><input name="username" required autocomplete="username"></div>
              <div><label>Password</label><input name="password" type="password" required autocomplete="current-password"></div>
              <button class="btn btn-primary btn-block">Login</button>
            </form>
          </div>
        </div>
        """
    elif kind == "dashboard":
        body = """
        <div class="app" id="dashboardPage">
          <div class="flex" style="justify-content:space-between; flex-wrap:wrap; margin-bottom:20px;">
            <div class="brand flex" style="gap:10px; font-size:1.5rem; font-weight:700;">
              <span style="background:linear-gradient(135deg,var(--primary),#9b6dff); padding:6px 12px; border-radius:12px;">ST</span>
              Story Tools
            </div>
            <div class="flex"><button class="btn btn-ghost" id="openSettings">Settings</button><button class="btn btn-primary" id="newProject">+ New Project</button><button class="btn btn-ghost" onclick="logout()">Logout</button></div>
          </div>
          <div class="card hero" style="margin-bottom:20px;">
            <div class="eyebrow">AI assisted video studio</div>
            <h1>Turn a story into a publish-ready video.</h1>
            <p class="muted" style="max-width:680px;margin-top:14px;font-size:1.05rem;">Natural narration, timed captions, matching stock footage, music, and social formats in one workflow.</p>
            <div class="feature-row"><span class="feature-chip">Arabic + Darija voices</span><span class="feature-chip">Exact scene timing</span><span class="feature-chip">1080p vertical video</span><span class="feature-chip">Free Pexels footage</span></div>
          </div>
          <div id="createCard" class="card creator" hidden style="margin-bottom:24px;">
            <div class="eyebrow">New production</div><h2>Create your story video</h2>
            <form id="createForm" class="grid">
              <div><label>Project name</label><input name="name" required maxlength="120" placeholder="My Sahara Story"></div>
              <div class="grid grid-2">
                <div><label>Input mode</label>
                  <div class="radio-group">
                    <label class="radio"><input type="radio" name="mode" value="tts" checked> Write a script → narration + timed scenes</label>
                    <label class="radio"><input type="radio" name="mode" value="automatic"> Upload audio → generate SRT</label>
                    <label class="radio"><input type="radio" name="mode" value="existing"> Upload audio + existing SRT</label>
                  </div>
                </div>
                <div><label>Output format</label><select name="output_format"><option selected>9:16</option><option>16:9</option><option>1:1</option></select></div>
              </div>
              <div id="scriptWrap"><label>Story script</label><textarea name="script" rows="7" required dir="auto" placeholder="كتب القصة بالدارجة أو العربية. استعمل الفواصل والنقط باش يكون الصوت طبيعي..."></textarea><div class="muted small" style="margin-top:7px;">Tip: punctuation controls pauses. Arabic script gives the best Darija pronunciation; common Latin Darija words are converted automatically.</div></div>
              <div id="voiceWrap" class="grid grid-2">
                <div><label>Speech provider</label><select id="ttsProviderSelect" name="tts_provider" onchange="toggleTtsVoice()"><option value="edge">Microsoft Edge Neural (free)</option></select></div>
                <div id="builtinVoiceWrap"><label>Narrator voice</label><select name="tts_voice">
                  <optgroup label="Automatic for script language"><option value="auto_female" selected>Natural female</option><option value="auto_male">Natural male</option></optgroup>
                  <optgroup label="Moroccan and Arabic"><option value="ma_female">Morocco · Mouna</option><option value="ma_male">Morocco · Jamal</option>
                  <option value="dz_female">Algeria · Amina</option><option value="dz_male">Algeria · Ismael</option>
                  <option value="tn_female">Tunisia · Reem</option><option value="tn_male">Tunisia · Hedi</option>
                  <option value="eg_female">Egypt · Salma</option><option value="eg_male">Egypt · Shakir</option>
                  <option value="sa_female">Saudi · Zariyah</option><option value="sa_male">Saudi · Hamed</option></optgroup>
                  <optgroup label="English"><option value="en_us_female">US · Aria</option><option value="en_us_male">US · Guy</option><option value="en_uk_female">UK · Sonia</option><option value="en_uk_male">UK · Ryan</option></optgroup>
                </select></div>
                <div id="customVoiceWrap" style="display:none"><label>Provider voice ID</label><input name="tts_custom_voice" placeholder="Configured default voice"></div>
                <div><label>Narration style</label><select name="tts_style"><option value="story">Natural storyteller</option><option value="cinematic">Deep cinematic</option><option value="energetic">Fast social hook</option><option value="calm">Calm documentary</option></select></div>
              </div>
              <div id="voicePreviewWrap" class="flex"><button type="button" class="btn btn-ghost" onclick="previewVoice(this)">▶ Preview voice</button><audio id="voicePreview" controls hidden></audio></div>
              <div id="audioWrap" style="display:none"><label>Audio (MP3 or supported)</label><input id="audio" type="file" accept=".mp3,.wav,.m4a,.aac,.flac,.ogg,.webm"></div>
              <div id="autoWrap" style="display:none"><div class="muted small">Whisper model and language are selected on the next step.</div></div>
              <div id="srtWrap" style="display:none;"><label>Existing SRT</label><input id="srt" type="file" accept=".srt"></div>
              <div><label>Audio language</label><select name="language"><option value="auto">Auto</option><option>Arabic</option><option>English</option><option>French</option><option>Spanish</option></select></div>
              <div class="flex"><button class="btn btn-primary">Create & Upload</button><button type="button" class="btn btn-ghost" id="cancelCreate">Cancel</button></div>
            </form>
          </div>
          <h2>Your projects</h2>
          <div class="filter-bar">
            <input id="searchInput" placeholder="🔍 Search projects..." style="flex:2;">
            <select id="statusFilter" style="flex:1;">
              <option value="">All statuses</option>
              <option value="Draft">Draft</option>
              <option value="Ready">Ready</option>
              <option value="Processing">Processing</option>
              <option value="Completed">Completed</option>
              <option value="Error">Error</option>
            </select>
          </div>
          <div id="projects" class="project-list"></div>
        </div>
        <div id="ttsSettingsModal" class="modal-backdrop" hidden>
          <div class="modal-card">
            <div class="flex" style="justify-content:space-between"><div><div class="eyebrow">Local settings</div><h1>Services and API keys</h1></div><button class="btn btn-ghost" type="button" onclick="closeSettings()">Close</button></div>
            <p class="muted small">Keys are saved on this Windows computer. Custom speech services must support the OpenAI-compatible audio/speech API.</p>
            <div class="card" style="margin:16px 0;box-shadow:none">
              <h2>Pexels video search</h2><label>API key</label><input id="pexelsKeyInput" type="password" autocomplete="new-password" placeholder="Leave blank to keep the saved key">
              <label class="flex small" style="gap:8px;margin-top:8px"><input id="clearPexelsKey" type="checkbox" style="width:auto"> Remove saved Pexels key</label>
            </div>
            <div class="card" style="margin:16px 0;box-shadow:none">
              <h2>Default speech provider</h2><select id="defaultTtsProvider"></select>
              <div id="ttsProvidersList" class="grid mt-2"></div>
            </div>
            <div class="card" style="margin:16px 0;box-shadow:none">
              <div class="flex" style="justify-content:space-between"><h2 id="providerFormTitle">Add compatible speech provider</h2><button class="btn btn-ghost" type="button" onclick="useOpenAIPreset()">OpenAI preset</button></div>
              <form id="providerForm" class="grid grid-2" onsubmit="saveProviderForm(event)">
                <input type="hidden" id="providerId">
                <div><label>Provider name</label><input id="providerName" required maxlength="60" placeholder="My TTS provider"></div>
                <div><label>Model</label><input id="providerModel" required placeholder="tts-1"></div>
                <div><label>API endpoint or base URL</label><input id="providerUrl" type="url" required placeholder="https://api.example.com/v1"></div>
                <div><label>API key</label><input id="providerKey" type="password" autocomplete="new-password" placeholder="Required for new providers; blank keeps existing"></div>
                <div><label>Default voice ID</label><input id="providerVoice" value="alloy" placeholder="alloy"></div>
                <div><label>Audio format</label><select id="providerFormat"><option value="mp3">MP3</option><option value="wav">WAV</option></select></div>
                <div class="flex"><button class="btn btn-primary" id="providerSubmit">Add provider</button><button class="btn btn-ghost" type="button" onclick="resetProviderForm()">Clear</button></div>
              </form>
            </div>
            <div class="flex" style="justify-content:flex-end"><button class="btn btn-primary" type="button" onclick="saveTtsSettings()">Save settings</button></div>
          </div>
        </div>
        """
    elif kind == "project":
        script = f"<script>var projectId = '{project_id}';</script>" if project_id else ""
        body = f"""
        <div class="app">
          <div id="projectView">
            <div class="card">Loading project...</div>
          </div>
          <div id="toast" hidden></div>
        </div>
        {script}
        """
    else:
        body = """
        <div class="app"><div id="projectView"><div class="card">Loading project...</div></div></div>
        """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Story Tools</title>
<style>{CSS}</style>
</head>
<body>{body}<script>{JS}</script></body>
</html>"""


# ============================================================
# APP START
# ============================================================

init_db()
startup_checks()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
