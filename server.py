import os
import json
import time
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Any, Dict, List

import requests
from flask import Flask, request, jsonify, redirect, session, send_from_directory
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

# ----------------------------
# App setup
# ----------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "baseone.db")
TOKEN_PATH = os.path.join(DATA_DIR, "google_token.json")

SCOPES = ["https://www.googleapis.com/auth/blogger"]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "BaseOne!Session#Secret@2026$Prod")

app = Flask(__name__, static_folder=".", static_url_path="")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = SESSION_SECRET
CORS(app, supports_credentials=True)

# ----------------------------
# Utils
# ----------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_str() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%S")

def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def parse_iso(s: str) -> datetime:
    # accepts "2026-01-24T12:00:00Z" or ISO with offset
    if not s:
        raise ValueError("empty iso")
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)

def strip_code_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        # remove first fence line
        t = t.split("\n", 1)[1] if "\n" in t else ""
        # remove last fence
        if "```" in t:
            t = t.rsplit("```", 1)[0]
    return t.strip()

# ----------------------------
# DB
# ----------------------------
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
      status TEXT NOT NULL,
      platform TEXT NOT NULL,
      blog_id TEXT,
      blog_url TEXT,
      title TEXT NOT NULL,
      html TEXT NOT NULL,
      run_at TEXT NOT NULL,
      result_url TEXT,
      error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ----------------------------
# Token save/load
# ----------------------------
def save_token(creds: Credentials) -> None:
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_token() -> Optional[Credentials]:
    if not os.path.exists(TOKEN_PATH):
        return None
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
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

# ----------------------------
# OAuth
# ----------------------------
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
        redirect_uri=OAUTH_REDIRECT_URI
    )

@app.get("/oauth/start")
def oauth_start():
    flow = make_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )
    session["oauth_state"] = state
    return redirect(auth_url)

@app.get("/oauth/callback")
def oauth_callback():
    flow = make_flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    save_token(creds)
    return redirect("/?oauth=ok")

@app.get("/api/oauth/status")
def api_oauth_status():
    return jsonify({"ok": True, "connected": bool(load_token())})

# ----------------------------
# Static pages
# ----------------------------
@app.get("/")
def home():
    return send_from_directory(".", "index.html")

@app.get("/settings")
def settings_page():
    return send_from_directory(".", "settings.html")

@app.get("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})

@app.get("/__routes")
def __routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))

# ----------------------------
# Blogger APIs
# ----------------------------
@app.get("/api/blogger/blogs")
def api_blogger_blogs():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected. Visit /oauth/start"}), 401

    res = svc.blogs().listByUser(userId="self").execute()
    items = res.get("items", []) or []
    out = [{"id": b.get("id"), "name": b.get("name"), "url": b.get("url")} for b in items]
    return jsonify({"ok": True, "count": len(out), "items": out})

@app.post("/api/blogger/post")
def api_blogger_post():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected. Visit /oauth/start"}), 401

    payload = request.get_json(silent=True) or {}
    blog_id = str(payload.get("blog_id", "")).strip()
    title = str(payload.get("title", "")).strip()
    html = str(payload.get("html", "")).strip()

    if not blog_id:
        return jsonify({"ok": False, "error": "blog_id missing"}), 400
    if not title:
        return jsonify({"ok": False, "error": "title missing"}), 400
    if not html:
        return jsonify({"ok": False, "error": "html missing"}), 400

    try:
        post_body = {"kind": "blogger#post", "title": title, "content": html}
        res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
        return jsonify({"ok": True, "id": res.get("id"), "url": res.get("url")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ----------------------------
# Pexels image
# ----------------------------
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
        photos = data.get("photos", []) or []
        if not photos:
            return ""
        src = photos[0].get("src", {}) or {}
        return src.get("large2x") or src.get("large") or src.get("original") or ""
    except Exception:
        return ""

# ----------------------------
# Gemini / OpenAI
# ----------------------------
def gemini_list_models(gemini_key: str) -> List[str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={gemini_key}"
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        return []
    j = r.json() or {}
    models = j.get("models", []) or []
    names = []
    for m in models:
        name = (m.get("name") or "").replace("models/", "")
        supported = m.get("supportedGenerationMethods") or []
        if "generateContent" in supported and name:
            names.append(name)
    return names

def gemini_pick_model(gemini_key: str, preferred: str = "") -> str:
    names = gemini_list_models(gemini_key)
    if preferred:
        p = preferred.replace("models/", "").strip()
        if p in names:
            return p
    # prefer flash
    for cand in ["gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-2.0-flash", "gemini-2.0-flash-lite"]:
        if cand in names:
            return cand
    # any gemini
    for n in names:
        if "gemini" in n:
            return n
    # fallback
    return preferred.replace("models/", "").strip() or "gemini-1.5-flash-latest"

def call_gemini(gemini_key: str, model: str, prompt: str) -> str:
    if not gemini_key:
        raise RuntimeError("gemini_key missing")

    picked = gemini_pick_model(gemini_key, preferred=model or "")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{picked}:generateContent?key={gemini_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "topP": 0.9,
            "maxOutputTokens": 8192
        }
    }
    r = requests.post(url, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini API error: {r.status_code} {r.text}")
    j = r.json() or {}
    try:
        text = j["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        text = ""
    return strip_code_fences(text)

def call_openai(openai_key: str, model: str, prompt: str) -> str:
    if not openai_key:
        raise RuntimeError("openai_key missing")
    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {openai_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": (model or "gpt-5.2-mini"),
        "input": prompt,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI API error: {r.status_code} {r.text}")
    j = r.json() or {}

    # responses API: output_text convenience may not exist; parse outputs
    text = ""
    if "output_text" in j and isinstance(j["output_text"], str):
        text = j["output_text"]
    else:
        out = j.get("output", []) or []
        # try to collect text parts
        parts = []
        for item in out:
            content = item.get("content", []) or []
            for c in content:
                if c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
        text = "\n".join([p for p in parts if p]).strip()

    return strip_code_fences(text)

# ----------------------------
# Prompts
# ----------------------------
def make_body_prompt(topic: str, category: str) -> str:
    return f"""
너는 수익형 정보블로그 작가다.
아래 조건으로 '{topic}' 글을 한국어로 작성해줘.

- 카테고리: {category}
- 독자: 초보자도 이해 가능
- 구조: H2 소제목 8~9개
- 각 소제목 아래 500~900자 내외로 풍부하게
- 표 1개 포함(<table>)
- 아이콘/박스(✅💡⚠️)를 <div>로 포함
- 마지막: 요약(3~5줄) + FAQ 5개 + 행동유도(댓글/구독/다음 글 유도)

출력은 "블로그에 붙여넣기 좋은 HTML"로만 작성해줘.
""".strip()

def make_image_query(topic: str, category: str) -> str:
    # pexels 검색용
    return f"{topic} {category} concept minimal".strip()

# ----------------------------
# Generate API (HTML + image)
# ----------------------------
@app.post("/api/generate")
def api_generate():
    payload = request.get_json(silent=True) or {}
    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"
    writer = (payload.get("writer") or "gemini").strip().lower()

    gemini_key = (payload.get("gemini_key") or "").strip()
    gemini_model = (payload.get("gemini_model") or "").strip()
    openai_key = (payload.get("openai_key") or "").strip()
    openai_model = (payload.get("openai_model") or "").strip()

    img_provider = (payload.get("img_provider") or "pexels").strip().lower()
    pexels_key = (payload.get("pexels_key") or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    body_prompt = make_body_prompt(topic, category)

    # image
    image_url = ""
    image_prompt = make_image_query(topic, category)
    if img_provider == "pexels" and pexels_key:
        image_url = pexels_search_image_url(pexels_key, image_prompt) or pexels_search_image_url(pexels_key, topic)

    # text
    try:
        if writer == "openai":
            if not openai_key:
                return jsonify({"ok": False, "error": "openai_key missing"}), 400
            html = call_openai(openai_key, openai_model, body_prompt)
        else:
            if not gemini_key:
                return jsonify({"ok": False, "error": "gemini_key missing"}), 400
            html = call_gemini(gemini_key, gemini_model, body_prompt)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # optionally inject image on top
    if image_url:
        hero = f"""
<div style="margin:0 0 16px 0;padding:0;">
  <img src="{image_url}" alt="{topic}" style="width:100%;max-width:100%;border-radius:14px;border:1px solid rgba(0,0,0,.08)"/>
</div>
""".strip()
        if "<body" in html.lower():
            # leave as is (user already full doc)
            pass
        else:
            html = hero + "\n" + html

    return jsonify({
        "ok": True,
        "topic": topic,
        "category": category,
        "generated_at": now_str(),
        "title": topic,
        "html": html,
        "image_prompt": image_prompt,
        "image_provider": img_provider,
        "image_url": image_url
    })

# ----------------------------
# Topics (AI)
# ----------------------------
@app.post("/api/topics/money")
def api_topics_money():
    payload = request.get_json(silent=True) or {}
    gemini_key = (payload.get("gemini_key") or "").strip()
    gemini_model = (payload.get("gemini_model") or "").strip()
    count = int(payload.get("count") or 30)
    count = max(5, min(60, count))

    if not gemini_key:
        return jsonify({"ok": False, "error": "gemini_key missing"}), 400

    prompt = f"""
너는 수익형 블로그 편집장이다.
한국어로, 애드센스 승인에 유리한 '정보성' 주제(제목) {count}개를 만들어줘.
조건:
- 과장/낚시 금지, 하지만 클릭 유도는 자연스럽게
- 숫자/체크리스트/비교/조건정리 스타일 선호
- 중복 금지
- 출력은 JSON 배열로만: ["제목1", "제목2", ...]
""".strip()

    try:
        text = call_gemini(gemini_key, gemini_model, prompt)
        items = json.loads(text)
        if not isinstance(items, list):
            raise RuntimeError("model did not return JSON array")
        items = [str(x).strip() for x in items if str(x).strip()]
        return jsonify({"ok": True, "count": len(items), "items": items[:count]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ----------------------------
# Keyword collect -> topics (safe 방식: 외부 트렌드 스크래핑 X)
# ----------------------------
@app.post("/api/keywords/collect_topics")
def api_keywords_collect_topics():
    payload = request.get_json(silent=True) or {}
    seed = (payload.get("seed") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"
    count = int(payload.get("count") or 30)
    count = max(5, min(60, count))

    writer = (payload.get("writer") or "gemini").strip().lower()
    gemini_key = (payload.get("gemini_key") or "").strip()
    gemini_model = (payload.get("gemini_model") or "").strip()
    openai_key = (payload.get("openai_key") or "").strip()
    openai_model = (payload.get("openai_model") or "").strip()

    if not seed:
        return jsonify({"ok": False, "error": "seed missing"}), 400

    prompt = f"""
너는 SEO 키워드 리서처다.
입력 키워드(씨드): "{seed}"
카테고리: "{category}"

1) 롱테일 키워드 25~40개를 만든다. (한국어, 검색의도 다양화)
2) 위 키워드를 바탕으로 클릭 유도형 '블로그 제목(주제)' {count}개를 만든다.
3) 과장/허위/의학/금융 확정수익 같은 위험 표현은 피한다.
출력은 반드시 JSON 한 덩어리로만:
{{
  "keywords":[...],
  "topics":[...]
}}
""".strip()

    try:
        if writer == "openai":
            if not openai_key:
                return jsonify({"ok": False, "error": "openai_key missing"}), 400
            text = call_openai(openai_key, openai_model, prompt)
        else:
            if not gemini_key:
                return jsonify({"ok": False, "error": "gemini_key missing"}), 400
            text = call_gemini(gemini_key, gemini_model, prompt)

        obj = json.loads(text)
        keywords = obj.get("keywords", []) if isinstance(obj, dict) else []
        topics = obj.get("topics", []) if isinstance(obj, dict) else []
        keywords = [str(x).strip() for x in keywords if str(x).strip()]
        topics = [str(x).strip() for x in topics if str(x).strip()]
        return jsonify({"ok": True, "keywords": keywords, "topics": topics[:count]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ----------------------------
# Tasks: add/list/cancel  (멀티 블로그 지원)
# ----------------------------
@app.post("/api/tasks/add")
def api_tasks_add():
    payload = request.get_json(silent=True) or {}
    platform = (payload.get("platform") or "blogspot").strip().lower()
    blog_id = (payload.get("blog_id") or "").strip()
    blog_url = (payload.get("blog_url") or "").strip()
    title = (payload.get("title") or "").strip()
    html = (payload.get("html") or "").strip()
    run_at = (payload.get("run_at") or "").strip()  # ISO UTC

    if platform != "blogspot":
        return jsonify({"ok": False, "error": "only blogspot supported"}), 400
    if not blog_id:
        return jsonify({"ok": False, "error": "blog_id missing"}), 400
    if not title:
        return jsonify({"ok": False, "error": "title missing"}), 400
    if not html:
        return jsonify({"ok": False, "error": "html missing"}), 400
    try:
        dt = parse_iso(run_at)
        run_at_iso = iso_utc(dt)
    except Exception:
        return jsonify({"ok": False, "error": "run_at must be ISO (e.g. 2026-01-24T12:00:00Z)"}), 400

    conn = db()
    cur = conn.cursor()
    now = iso_utc(now_utc())
    cur.execute(
        """INSERT INTO tasks(status, platform, blog_id, blog_url, title, html, run_at, result_url, error, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("pending", platform, blog_id, blog_url, title, html, run_at_iso, "", "", now, now)
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": task_id})

@app.get("/api/tasks/list")
def api_tasks_list():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 200")
    rows = cur.fetchall()
    conn.close()
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "status": r["status"],
            "platform": r["platform"],
            "blog_id": r["blog_id"],
            "blog_url": r["blog_url"],
            "title": r["title"],
            "run_at": r["run_at"],
            "result_url": r["result_url"],
            "error": r["error"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return jsonify({"ok": True, "items": items})

@app.post("/api/tasks/cancel/<int:task_id>")
def api_tasks_cancel(task_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "not found"}), 404
    if row["status"] != "pending":
        conn.close()
        return jsonify({"ok": False, "error": "only pending can be canceled"}), 400
    now = iso_utc(now_utc())
    cur.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", ("canceled", now, task_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ----------------------------
# Run
# ----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
