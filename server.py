import os
import json
import time
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import requests
from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

# =========================================================
# Config
# =========================================================
SCOPES = ["https://www.googleapis.com/auth/blogger"]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "").strip()

# Render/Flask session secret
SESSION_SECRET = os.environ.get("SESSION_SECRET") or os.environ.get("SECRET_KEY") or "BaseOne-Dev-Secret"
TOKEN_FILE = os.environ.get("TOKEN_FILE", "/tmp/google_token.json")

# Task DB (예약발행 큐)
DB_PATH = os.environ.get("TASK_DB_PATH", "/tmp/baseone_tasks.db")

# Optional: run scheduler loop inside this web process (주의: gunicorn multi-worker면 중복 실행 위험)
RUN_SCHEDULER = os.environ.get("RUN_SCHEDULER", "0").strip() == "1"
SCHEDULER_POLL_SEC = int(os.environ.get("SCHEDULER_POLL_SEC", "15"))

# =========================================================
# Flask App
# =========================================================
app = Flask(__name__, static_folder=".", static_url_path="")
app.secret_key = SESSION_SECRET
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)


# =========================================================
# Utils
# =========================================================
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def now_utc_dt() -> datetime:
    return datetime.now(timezone.utc)

def safe_str(x) -> str:
    return "" if x is None else str(x)

def file_exists_in_root(filename: str) -> bool:
    try:
        return os.path.isfile(os.path.join(os.getcwd(), filename))
    except Exception:
        return False


# =========================================================
# Token Save/Load (Blogger OAuth)
# =========================================================
def save_token(creds: Credentials) -> None:
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_token() -> Optional[Credentials]:
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Credentials(**data)
    except Exception:
        return None

def get_blogger_client():
    creds = load_token()
    if not creds:
        return None
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            save_token(creds)
    except Exception:
        return None
    return build("blogger", "v3", credentials=creds)

def make_flow() -> Flow:
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAUTH_REDIRECT_URI):
        raise RuntimeError("OAuth env vars missing: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / OAUTH_REDIRECT_URI")

    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )


# =========================================================
# DB (Tasks)
# =========================================================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      platform TEXT NOT NULL,
      blog_id TEXT,
      blog_url TEXT,
      title TEXT NOT NULL,
      html TEXT NOT NULL,
      run_at TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending', -- pending/running/ok/err/canceled
      result_url TEXT,
      error TEXT,
      created_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

def add_task(platform: str, blog_id: str, blog_url: str, title: str, html: str, run_at_iso_
