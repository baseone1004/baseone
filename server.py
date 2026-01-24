# server.py
import os, json, time, sqlite3
from datetime import datetime, timezone
from typing import Optional

import requests
from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

# (선택) OpenAI
# pip install openai
try:
    from openai import OpenAI
except Exception:
    OpenAI = None


# ---------------- App ----------------
app = Flask(__name__, static_folder=".", static_url_path="")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
CORS(app, supports_credentials=True)

app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_change_me")

DB_PATH = os.environ.get("DB_PATH", "baseone.db")
TOKEN_FILE = os.environ.get("TOKEN_FILE", "google_token.json")

SCOPES = ["https://www.googleapis.com/auth/blogger"]
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

# Gemini
GEMINI_API_KEY_DEFAULT = os.environ.get("GEMINI_API_KEY", "")

# OpenAI
OPENAI_API_KEY_DEFAULT = os.environ.get("OPENAI_API_KEY", "")


# ---------------- Utils ----------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      status TEXT NOT NULL DEFAULT 'pending',
      platform TEXT NOT NULL DEFAULT 'blogspot',
      blog_id TEXT,
      blog_url TEXT,
      title TEXT,
      html TEXT,
      run_at TEXT,        -- ISO UTC
      created_at TEXT,
      updated_at TEXT,
      result_url TEXT,
      error TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()


# ---------------- Static Pages ----------------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings_page():
    return send_from_directory(".", "settings.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})

@app.route("/__routes")
def __routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))


# ---------------- Google Token Save/Load ----------------
def save_token(creds: Credentials):
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
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

def make_flow():
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
        redirect_uri=OAUTH_REDIRECT_URI
    )


# ---------------- OAuth Routes ----------------
@app.route("/oauth/start")
def oauth_start():
    flow = make_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )
    session["oauth_state"] = state
    return redirect(auth_url)

@app.route("/oauth/callback")
def oauth_callback():
    try:
        flow = make_flow()
        flow.fetch_token(authorization_response=request.url)
        save_token(flow.credentials)
        return redirect("/?oauth=ok")
    except Exception as e:
        return jsonify({"ok": False, "error": f"OAuth callback failed: {str(e)}"}), 500

@app.route("/api/oauth/status")
def oauth_status():
    return jsonify({"ok": True, "connected": bool(load_token())})


# ---------------- Blogger APIs ----------------
@app.route("/api/blogger/blogs", methods=["GET"])
def api_blogger_blogs():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected. Visit /oauth/start"}), 401
    res = svc.blogs().listByUser(userId="self").execute()
    items = res.get("items", [])
    out = [{"id": b.get("id"), "name": b.get("name"), "url": b.get("url")} for b in items]
    return jsonify({"ok": True, "count": len(out), "items": out})

@app.route("/api/blogger/post", methods=["POST"])
def api_blogger_post():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected. Visit /oauth/start"}), 401

    payload = request.get_json(silent=True) or {}
    blog_id = str(payload.get("blog_id", "")).strip()
    title = str(payload.get("title", "")).strip()
    html = str(payload.get("html", "")).strip()
    is_draft = bool(payload.get("is_draft", False))

    if not blog_id:
        return jsonify({"ok": False, "error": "blog_id missing"}), 400
    if not title:
        return jsonify({"ok": False, "error": "title missing"}), 400
    if not html:
        return jsonify({"ok": False, "error": "html missing"}), 400

    try:
        post_body = {"kind": "blogger#post", "title": title, "content": html}
        res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=is_draft).execute()
        return jsonify({"ok": True, "id": res.get("id"), "url": res.get("url")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------- Image: Pexels ----------------
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


# ---------------- Gemini (REST) ----------------
def gemini_generate_html(api_key: str, model: str, prompt: str) -> str:
    """
    Google Generative Language API (v1beta) generateContent.
    """
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY missing")

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    params = {"key": api_key}
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192
        }
    }
    r = requests.post(endpoint, params=params, json=body, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini error: {r.status_code} {r.text[:400]}")
    j = r.json()
    # candidates[0].content.parts[0].text
    cands = j.get("candidates", [])
    if not cands:
        raise RuntimeError("Gemini returned no candidates")
    parts = (cands[0].get("content", {}) or {}).get("parts", [])
    if not parts:
        raise RuntimeError("Gemini returned empty parts")
    return parts[0].get("text", "")


# ---------------- OpenAI (optional) ----------------
def openai_generate_html(api_key: str, model: str, prompt: str) -> str:
    if not OpenAI:
        raise RuntimeError("openai package not installed")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing")

    client = OpenAI(api_key=api_key)
    # Responses API style (official docs 흐름) :contentReference[oaicite:1]{index=1}
    resp = client.responses.create(
        model=model,
        input=[{"role": "user", "content": prompt}],
    )
    # best-effort extract text
    try:
        return resp.output_text
    except Exception:
        return str(resp)


# ---------------- Prompt ----------------
def build_blog_prompt(topic: str, category: str) -> str:
    # “수익형” 포맷: HTML로 바로 붙여넣기
    return f"""
너는 한국어 수익형 정보블로그 전문 작가다.
아래 주제로 '블로그스팟에 바로 붙여넣기 가능한 HTML' 글을 작성해라.

[주제] {topic}
[카테고리] {category}

요구사항:
- 분량: 최소 6,000자 이상 (너무 짧으면 안 됨)
- H2 소제목 8개 이상
- 소제목마다 400자 이상
- 중간에 <table> 1개 포함 (비교/체크리스트 형태)
- 박스 디자인을 div로 3개 포함 (✅ 팁 / ⚠️ 주의 / 💡 핵심)
- 마지막에 "요약(3~5줄) + FAQ 5개 + 행동유도(구독/저장/댓글)" 포함
- 과장/허위 금지. 애매하면 '일반적으로' 표현.

출력형식:
- 오직 HTML만 출력 (``` 금지)
""".strip()

def inject_image_into_html(html: str, image_url: str, alt: str) -> str:
    if not image_url:
        return html
    img = f'<div style="margin:14px 0"><img src="{image_url}" alt="{alt}" style="width:100%;max-width:980px;border-radius:12px;display:block"/></div>'
    return img + "\n" + html

def make_image_query(topic: str, category: str) -> str:
    # Pexels 검색용
    return f"{topic} {category}".strip()


# ---------------- Generate API ----------------
@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}
    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "돈/재테크").strip()

    writer = (payload.get("writer") or "gemini").strip().lower()  # gemini | gpt
    gemini_key = (payload.get("gemini_key") or GEMINI_API_KEY_DEFAULT).strip()
    gemini_model = (payload.get("gemini_model") or "gemini-1.5-flash").strip()

    openai_key = (payload.get("openai_key") or OPENAI_API_KEY_DEFAULT).strip()
    openai_model = (payload.get("openai_model") or "gpt-4o-mini").strip()

    img_provider = (payload.get("img_provider") or "pexels").strip().lower()
    pexels_key = (payload.get("pexels_key") or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    prompt = build_blog_prompt(topic, category)

    # 1) 이미지 먼저(있으면)
    image_url = ""
    image_prompt = ""
    if img_provider == "pexels":
        q = make_image_query(topic, category)
        image_url = pexels_search_image_url(pexels_key, q) or pexels_search_image_url(pexels_key, topic)
        image_prompt = f"Pexels search: {q}"

    # 2) 본문 생성
    try:
        if writer == "gpt":
            html = openai_generate_html(openai_key, openai_model, prompt)
        else:
            html = gemini_generate_html(gemini_key, gemini_model, prompt)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    html = (html or "").strip()
    if not html:
        return jsonify({"ok": False, "error": "empty html generated"}), 500

    # 3) 이미지 삽입
    html2 = inject_image_into_html(html, image_url, topic)

    return jsonify({
        "ok": True,
        "topic": topic,
        "category": category,
        "generated_at": now_str(),
        "title": topic,
        "html": html2,
        "writer": writer,
        "gemini_model": gemini_model,
        "openai_model": openai_model,
        "image_provider": img_provider,
        "image_prompt": image_prompt,
        "image_url": image_url
    })


# ---------------- Tasks API (예약 큐) ----------------
@app.route("/api/tasks/add", methods=["POST"])
def api_tasks_add():
    p = request.get_json(silent=True) or {}
    run_at = (p.get("run_at") or "").strip()  # ISO UTC
    if not run_at:
        return jsonify({"ok": False, "error": "run_at required (ISO UTC)"}), 400

    conn = db()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO tasks(status, platform, blog_id, blog_url, title, html, run_at, created_at, updated_at)
      VALUES(?,?,?,?,?,?,?,?,?)
    """, (
        "pending",
        p.get("platform", "blogspot"),
        p.get("blog_id", ""),
        p.get("blog_url", ""),
        p.get("title", ""),
        p.get("html", ""),
        run_at,
        utc_now_iso(),
        utc_now_iso()
    ))
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": task_id})

@app.route("/api/tasks/list", methods=["GET"])
def api_tasks_list():
    conn = db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    return jsonify({"ok": True, "items": [dict(r) for r in rows]})

@app.route("/api/tasks/cancel/<int:task_id>", methods=["POST"])
def api_tasks_cancel(task_id: int):
    conn = db()
    conn.execute("UPDATE tasks SET status='canceled', updated_at=? WHERE id=? AND status='pending'", (utc_now_iso(), task_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# worker가 호출: 지금 시간이 지난 pending만 실행
@app.route("/api/tasks/run_due", methods=["POST"])
def api_tasks_run_due():
    """
    Background worker가 20~30초마다 호출해서 실행.
    """
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected"}), 401

    now = datetime.now(timezone.utc)

    conn = db()
    rows = conn.execute("""
      SELECT * FROM tasks
      WHERE status='pending'
      ORDER BY id ASC
      LIMIT 10
    """).fetchall()

    ran = 0
    for r in rows:
        try:
            run_at = datetime.fromisoformat(r["run_at"].replace("Z","+00:00"))
        except Exception:
            # invalid time => fail
            conn.execute("UPDATE tasks SET status='err', error=?, updated_at=? WHERE id=?",
                         ("invalid run_at", utc_now_iso(), r["id"]))
            continue

        if run_at > now:
            continue

        # mark running
        conn.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (utc_now_iso(), r["id"]))
        conn.commit()

        try:
            if (r["platform"] or "blogspot") != "blogspot":
                raise RuntimeError("Only blogspot supported for auto publish")

            blog_id = (r["blog_id"] or "").strip()
            title = (r["title"] or "").strip()
            html = (r["html"] or "").strip()

            if not (blog_id and title and html):
                raise RuntimeError("missing blog_id/title/html")

            post_body = {"kind": "blogger#post", "title": title, "content": html}
            res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()

            conn.execute("""
              UPDATE tasks
              SET status='ok', result_url=?, updated_at=?
              WHERE id=?
            """, (res.get("url",""), utc_now_iso(), r["id"]))
            conn.commit()
            ran += 1

        except Exception as e:
            conn.execute("""
              UPDATE tasks
              SET status='err', error=?, updated_at=?
              WHERE id=?
            """, (str(e), utc_now_iso(), r["id"]))
            conn.commit()

    conn.close()
    return jsonify({"ok": True, "ran": ran})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
