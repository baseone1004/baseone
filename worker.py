import os
import time
import sqlite3
from datetime import datetime, timezone

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "baseone.db")
TOKEN_PATH = os.path.join(DATA_DIR, "google_token.json")

def now_utc():
    return datetime.now(timezone.utc)

def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def parse_iso(s: str) -> datetime:
    s = (s or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def load_token():
    if not os.path.exists(TOKEN_PATH):
        return None
    import json
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Credentials(**data)
    except Exception:
        return None

def save_token(creds: Credentials):
    import json
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_blogger_client():
    creds = load_token()
    if not creds:
        return None
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        save_token(creds)
    return build("blogger", "v3", credentials=creds)

def mark(conn, task_id: int, status: str, result_url: str = "", error: str = ""):
    cur = conn.cursor()
    cur.execute(
        "UPDATE tasks SET status=?, result_url=?, error=?, updated_at=? WHERE id=?",
        (status, result_url, error, iso_utc(now_utc()), task_id)
    )
    conn.commit()

def fetch_due_tasks(conn, limit=5):
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM tasks WHERE status='pending' ORDER BY run_at ASC LIMIT ?",
        (limit,)
    )
    rows = cur.fetchall()
    due = []
    now = now_utc()
    for r in rows:
        try:
            if parse_iso(r["run_at"]) <= now:
                due.append(r)
        except Exception:
            due.append(r)
    return due

def main():
    print("[worker] started")
    while True:
        try:
            conn = db()
            due = fetch_due_tasks(conn, limit=10)
            if not due:
                conn.close()
                time.sleep(10)
                continue

            svc = get_blogger_client()
            if not svc:
                # OAuth 끊기면 에러로 마킹
                for r in due:
                    mark(conn, r["id"], "err", error="OAuth not connected (token missing)")
                conn.close()
                time.sleep(15)
                continue

            for r in due:
                task_id = r["id"]
                blog_id = (r["blog_id"] or "").strip()
                title = (r["title"] or "").strip()
                html = (r["html"] or "").strip()

                if not blog_id or not title or not html:
                    mark(conn, task_id, "err", error="missing blog_id/title/html")
                    continue

                # running
                mark(conn, task_id, "running")

                try:
                    post_body = {"kind": "blogger#post", "title": title, "content": html}
                    res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
                    url = res.get("url") or ""
                    mark(conn, task_id, "ok", result_url=url, error="")
                    print(f"[worker] OK task={task_id} url={url}")
                except Exception as e:
                    mark(conn, task_id, "err", error=str(e))
                    print(f"[worker] ERR task={task_id} {e}")

            conn.close()
            time.sleep(3)

        except Exception as e:
            print("[worker] fatal:", e)
            time.sleep(10)

if __name__ == "__main__":
    main()
