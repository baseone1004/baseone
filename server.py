import os
import json
import time
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import requests
from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS

# Blogger OAuth / API
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build


# -----------------------------
# App
# -----------------------------
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_change_me")

DB_FILE = os.environ.get("BASEONE_DB", "baseone.db")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")  # ex) https://xxxx.onrender.com/oauth/blogger/callback

SCOPES = ["https://www.googleapis.com/auth/blogger"]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(dt_str: str) -> datetime:
    # "2026-01-22T14:30" (no tz) or ISO
    try:
        if dt_str.endswith("Z"):
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return datetime.fromisoformat(dt_str)
    except Exception:
        # fallback: local naive -> treat as local time (no tz)
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")


# -----------------------------
# DB helpers
# -----------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS kv (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,            -- "blogspot" | "naver" | "tistory"
        blog_id TEXT,                      -- blogspot uses blog_id
        blog_url TEXT,
        title TEXT NOT NULL,
        html TEXT NOT NULL,
        run_at TEXT NOT NULL,              -- ISO string
        status TEXT NOT NULL DEFAULT 'pending', -- pending | running | ok | err | canceled
        result_url TEXT,
        error TEXT,
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()


def kv_set(k: str, v: Dict[str, Any]):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
    conn.commit()
    conn.close()


def kv_get(k: str) -> Optional[Dict[str, Any]]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT v FROM kv WHERE k=?", (k,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        return json.loads(row["v"])
    except Exception:
        return None


# -----------------------------
# Static
# -----------------------------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")


@app.route("/settings")
def settings():
    return send_from_directory(".", "settings.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})


# -----------------------------
# Gemini (Text)
#   - key is passed from client for simplicity (solo use)
#   - REST endpoint + x-goog-api-key header :contentReference[oaicite:5]{index=5}
# -----------------------------
def gemini_generate_text(*, api_key: str, model: str, prompt: str, temperature: float = 0.6) -> str:
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
        }
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:500]}")
    data = r.json()
    # candidates[0].content.parts[0].text
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return json.dumps(data, ensure_ascii=False)


def money_topic_prompt(count: int = 30) -> str:
    return f"""
너는 한국 수익형 정보블로그(애드센스/애드포스트) 전문 기획자다.

조건:
- 클릭을 부르는 제목 {count}개
- 돈 되는 키워드 위주(절약/세금/보험/대출/연금/부동산/신용/통신비/전기요금/정부지원금/환급/연말정산/카드혜택)
- 과장 금지, 합법적/현실적 내용
- 출력 형식: JSON 배열(문자열만) 예) ["제목1","제목2",...]

JSON만 출력해.
""".strip()


def article_html_prompt(topic: str, category: str) -> str:
    return f"""
너는 한국 수익형 정보블로그 전문 작가다.
아래 조건으로 "{topic}" 글을 한국어로 작성해줘.

- 카테고리: {category}
- 분량: 6,000~10,000자 (너무 길게 말고 실제로 읽히게)
- 구조: H2 6~8개 + 필요시 H3
- 표 1개 포함(<table>)
- 박스형 안내 2개 이상 (✅ TIP / ⚠️ 주의 / 💡 핵심요약 등)
- 마지막: 5줄 요약 + FAQ 5개 + 행동유도(댓글/구독/다음글)

중요:
- 애드센스 정책 위반될 만한 과장/확정수익 표현 금지
- 의료/법률/투자 조언은 "일반 정보" 고지 포함

출력은 블로그에 바로 붙여넣기 좋은 HTML로만 출력해.
""".strip()


# -----------------------------
# Pexels
# -----------------------------
def pexels_search_image_url(pexels_key: str, query: str) -> str:
    if not pexels_key:
        return ""
    url = "https://api.pexels.com/v1/search"
    headers = {"Authorization": pexels_key}
    params = {"query": query, "per_page": 1, "orientation": "landscape", "size": "large"}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code != 200:
            return ""
        data = r.json()
        photos = data.get("photos", [])
        if not photos:
            return ""
        src = photos[0].get("src", {})
        return src.get("large2x") or src.get("large") or src.get("original") or ""
    except Exception:
        return ""


# -----------------------------
# Blogger OAuth + Client
# -----------------------------
def make_blogger_flow() -> Flow:
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAUTH_REDIRECT_URI):
        raise RuntimeError("OAuth env vars missing. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OAUTH_REDIRECT_URI")
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(client_config=client_config, scopes=SCOPES, redirect_uri=OAUTH_REDIRECT_URI)


def save_google_token(creds: Credentials):
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    kv_set("google_token", data)


def load_google_token() -> Optional[Credentials]:
    data = kv_get("google_token")
    if not data:
        return None
    try:
        return Credentials(**data)
    except Exception:
        return None


def get_blogger_client():
    creds = load_google_token()
    if not creds:
        return None
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            save_google_token(creds)
    except Exception:
        return None
    return build("blogger", "v3", credentials=creds)


@app.route("/oauth/blogger/start")
def oauth_blogger_start():
    flow = make_blogger_flow()
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/oauth/blogger/callback")
def oauth_blogger_callback():
    flow = make_blogger_flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    save_google_token(creds)
    return redirect("/settings?oauth=blogger_ok")


@app.route("/api/blogger/status")
def blogger_status():
    return jsonify({"ok": True, "connected": bool(load_google_token())})


@app.route("/api/blogger/blogs")
def blogger_blogs():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "Blogger OAuth not connected. Go /oauth/blogger/start"}), 401

    res = svc.blogs().listByUser(userId="self").execute()
    items = res.get("items", [])
    out = [{"id": b.get("id"), "name": b.get("name"), "url": b.get("url")} for b in items]
    return jsonify({"ok": True, "items": out})


@app.route("/api/blogger/post", methods=["POST"])
def blogger_post_now():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "Blogger OAuth not connected. Go /oauth/blogger/start"}), 401

    payload = request.get_json(silent=True) or {}
    blog_id = str(payload.get("blog_id", "")).strip()
    title = str(payload.get("title", "")).strip()
    html = str(payload.get("html", "")).strip()

    if not blog_id:
        return jsonify({"ok": False, "error": "blog_id is required"}), 400
    if not title:
        return jsonify({"ok": False, "error": "title is required"}), 400
    if not html:
        return jsonify({"ok": False, "error": "html is required"}), 400

    try:
        post_body = {"kind": "blogger#post", "title": title, "content": html}
        # posts.insert 
        res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
        return jsonify({"ok": True, "id": res.get("id"), "url": res.get("url")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# -----------------------------
# API: money topics / generate article
# -----------------------------
@app.route("/api/topics/money", methods=["POST"])
def api_topics_money():
    payload = request.get_json(silent=True) or {}
    api_key = str(payload.get("gemini_key", "")).strip()
    model = str(payload.get("gemini_model", "")).strip() or "gemini-3-flash-preview"
    count = int(payload.get("count", 30) or 30)

    text = gemini_generate_text(api_key=api_key, model=model, prompt=money_topic_prompt(count), temperature=0.7)
    # expect JSON array
    try:
        arr = json.loads(text)
        arr = [str(x).strip() for x in arr if str(x).strip()]
        return jsonify({"ok": True, "items": arr[:count]})
    except Exception:
        # fallback: split lines
        lines = [x.strip("-• \t") for x in text.splitlines() if x.strip()]
        return jsonify({"ok": True, "items": lines[:count], "raw": text})


@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}
    topic = str(payload.get("topic", "")).strip()
    category = str(payload.get("category", "")).strip() or "돈/재테크"

    gemini_key = str(payload.get("gemini_key", "")).strip()
    gemini_model = str(payload.get("gemini_model", "")).strip() or "gemini-3-flash-preview"

    img_provider = str(payload.get("img_provider", "")).strip() or "pexels"  # pexels | gemini(prompt only)
    pexels_key = str(payload.get("pexels_key", "")).strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    html = gemini_generate_text(
        api_key=gemini_key,
        model=gemini_model,
        prompt=article_html_prompt(topic, category),
        temperature=0.6
    )

    image_prompt = f'{category} 블로그 썸네일, 주제 "{topic}", 텍스트 없음, 미니멀, 고해상도, 16:9'
    image_url = ""
    if img_provider == "pexels":
        q = f"{topic} {category}".strip()
        image_url = pexels_search_image_url(pexels_key, q) or pexels_search_image_url(pexels_key, topic)

    return jsonify({
        "ok": True,
        "topic": topic,
        "category": category,
        "generated_at": now_str(),
        "html": html,
        "image_provider": img_provider,
        "image_prompt": image_prompt,
        "image_url": image_url
    })


# -----------------------------
# Scheduler / Tasks
# -----------------------------
def task_add(platform: str, blog_id: str, blog_url: str, title: str, html: str, run_at: str) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tasks(platform, blog_id, blog_url, title, html, run_at, status, created_at)
        VALUES(?,?,?,?,?,?, 'pending', ?)
    """, (platform, blog_id, blog_url, title, html, run_at, utc_now_iso()))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def task_list(limit: int = 200):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def task_cancel(tid: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status='canceled' WHERE id=? AND status IN ('pending')", (tid,))
    conn.commit()
    conn.close()


def task_claim_due() -> Optional[Dict[str, Any]]:
    """Pick one due task and mark running"""
    conn = db()
    cur = conn.cursor()
    now_iso = utc_now_iso()
    # run_at may be naive; store ISO; compare as string is ok if all ISO UTC. We'll store UTC in UI.
    cur.execute("""
        SELECT * FROM tasks
        WHERE status='pending' AND run_at <= ?
        ORDER BY run_at ASC, id ASC
        LIMIT 1
    """, (now_iso,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    tid = row["id"]
    cur.execute("UPDATE tasks SET status='running' WHERE id=? AND status='pending'", (tid,))
    conn.commit()
    # re-read
    cur.execute("SELECT * FROM tasks WHERE id=?", (tid,))
    got = cur.fetchone()
    conn.close()
    return dict(got) if got else None


def task_finish(tid: int, ok: bool, result_url: str = "", error: str = ""):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE tasks
        SET status=?, result_url=?, error=?
        WHERE id=?
    """, ("ok" if ok else "err", result_url, error, tid))
    conn.commit()
    conn.close()


def execute_task(task: Dict[str, Any]):
    tid = int(task["id"])
    platform = task["platform"]
    title = task["title"]
    html = task["html"]
    blog_id = task.get("blog_id") or ""
    blog_url = task.get("blog_url") or ""

    if platform == "blogspot":
        svc = get_blogger_client()
        if not svc:
            task_finish(tid, False, error="Blogger OAuth not connected")
            return
        if not blog_id:
            task_finish(tid, False, error="blog_id missing (fetch blogs first)")
            return
        try:
            post_body = {"kind": "blogger#post", "title": title, "content": html}
            res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
            task_finish(tid, True, result_url=res.get("url") or "")
        except Exception as e:
            task_finish(tid, False, error=str(e))
        return

    if platform == "naver":
        # Official posting API is not provided in a stable way for personal Naver Blog.
        task_finish(tid, False, error="Naver auto-post not supported (no stable official posting API). Use copy/paste.")
        return

    if platform == "tistory":
        # Tistory Open API is officially marked as ended in their docs, so we don't execute.
        task_finish(tid, False, error="Tistory Open API is marked ended. Auto-post not supported reliably.")
        return

    task_finish(tid, False, error=f"Unknown platform: {platform}")


def worker_loop():
    while True:
        try:
            task = task_claim_due()
            if task:
                execute_task(task)
        except Exception:
            pass
        time.sleep(20)


@app.route("/api/tasks/add", methods=["POST"])
def api_tasks_add():
    payload = request.get_json(silent=True) or {}
    platform = str(payload.get("platform", "")).strip()  # blogspot | naver | tistory
    blog_id = str(payload.get("blog_id", "")).strip()
    blog_url = str(payload.get("blog_url", "")).strip()
    title = str(payload.get("title", "")).strip()
    html = str(payload.get("html", "")).strip()
    run_at = str(payload.get("run_at", "")).strip()  # ISO UTC

    if platform not in ("blogspot", "naver", "tistory"):
        return jsonify({"ok": False, "error": "platform must be blogspot/naver/tistory"}), 400
    if not title or not html:
        return jsonify({"ok": False, "error": "title/html required"}), 400
    if not run_at:
        return jsonify({"ok": False, "error": "run_at required (ISO UTC)"}), 400

    tid = task_add(platform, blog_id, blog_url, title, html, run_at)
    return jsonify({"ok": True, "id": tid})


@app.route("/api/tasks/list")
def api_tasks_list():
    return jsonify({"ok": True, "items": task_list()})


@app.route("/api/tasks/cancel/<int:tid>", methods=["POST"])
def api_tasks_cancel(tid: int):
    task_cancel(tid)
    return jsonify({"ok": True})


# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    init_db()

    # start worker thread
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
