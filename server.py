import os, json, time, re, sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

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
# App
# =========================================================
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
CORS(app, supports_credentials=True)

NOW = lambda: datetime.now(timezone.utc)
NOW_STR = lambda: NOW().strftime("%Y-%m-%d %H:%M:%S UTC")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "google_token.json")
DB_FILE = os.path.join(BASE_DIR, "tasks.db")

SCOPES = ["https://www.googleapis.com/auth/blogger"]
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")


# =========================================================
# DB (예약발행)
# =========================================================
def db_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      platform TEXT NOT NULL,
      blog_id TEXT NOT NULL,
      blog_url TEXT,
      title TEXT NOT NULL,
      html TEXT NOT NULL,
      run_at TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      result_url TEXT,
      error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

db_init()


# =========================================================
# OAuth Token
# =========================================================
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


# =========================================================
# Static
# =========================================================
@app.route("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/settings")
def settings_page():
    return send_from_directory(BASE_DIR, "settings.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": NOW_STR()})


# =========================================================
# OAuth routes
# =========================================================
@app.route("/oauth/start")
def oauth_start():
    try:
        flow = make_flow()
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent"
        )
        session["oauth_state"] = state
        return redirect(auth_url)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/oauth/callback")
def oauth_callback():
    try:
        flow = make_flow()
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        save_token(creds)
        return redirect("/?oauth=ok")
    except Exception as e:
        return jsonify({"ok": False, "error": f"OAuth callback failed: {str(e)}"}), 500

@app.route("/api/oauth/status")
def oauth_status():
    creds = load_token()
    return jsonify({"ok": True, "connected": bool(creds)})

@app.route("/__routes")
def __routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))


# =========================================================
# Pexels image
# =========================================================
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


# =========================================================
# LLM: Gemini / OpenAI (HTTP)
# =========================================================
def strip_code_fences(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    # ```html ... ```
    t = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()

def call_gemini(gemini_key: str, model: str, prompt: str) -> str:
    if not gemini_key:
        raise RuntimeError("gemini_key missing")
    model = model or "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "topP": 0.9,
            "maxOutputTokens": 8192
        }
    }
    r = requests.post(url, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini API error: {r.status_code} {r.text[:400]}")
    j = r.json()
    text = ""
    try:
        text = j["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        text = ""
    return strip_code_fences(text)

def call_openai(openai_key: str, model: str, prompt: str) -> str:
    if not openai_key:
        raise RuntimeError("openai_key missing")
    model = model or "gpt-5.2-mini"
    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {openai_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "input": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ]
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"OpenAI API error: {r.status_code} {r.text[:400]}")
    j = r.json()

    # output_text가 있으면 그걸 우선
    if isinstance(j, dict) and j.get("output_text"):
        return strip_code_fences(j["output_text"])

    # fallback 파싱
    out = ""
    try:
        for item in j.get("output", []):
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    out += c.get("text", "")
    except Exception:
        pass
    return strip_code_fences(out)


# =========================================================
# Prompts
# =========================================================
def make_body_prompt(topic: str, category: str) -> str:
    return f"""너는 수익형 정보블로그 작가다.
아래 조건으로 '{topic}' 글을 한국어로 작성해줘.

- 카테고리: {category}
- 분량: 4,000~7,000자 (너무 과하면 발행/로딩이 느려짐)
- H2 소제목 7~9개
- 각 소제목 아래 350자 이상
- 표 1개 포함(<table>)
- 아이콘/박스 디자인(✅💡⚠️) div로 포함
- 마지막: 요약(3~5줄) + FAQ 5개 + 행동유도

반드시 블로그에 붙여넣기 좋은 HTML로만 출력해줘.
(설명/머리말/코드펜스 없이 HTML만)
""".strip()

def make_image_prompt(topic: str, category: str) -> str:
    return f'{category} 관련 블로그 썸네일, 주제 "{topic}", 텍스트 없음, 깔끔한 미니멀, 고해상도, 16:9'


# =========================================================
# API: generate (실제 글 생성)
# =========================================================
@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}

    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"

    writer = (payload.get("writer") or "gemini").strip().lower()
    gemini_key = (payload.get("gemini_key") or "").strip()
    gemini_model = (payload.get("gemini_model") or "gemini-1.5-flash").strip()
    openai_key = (payload.get("openai_key") or "").strip()
    openai_model = (payload.get("openai_model") or "gpt-5.2-mini").strip()

    img_provider = (payload.get("img_provider") or "pexels").strip().lower()
    pexels_key = (payload.get("pexels_key") or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    body_prompt = make_body_prompt(topic, category)
    image_prompt = make_image_prompt(topic, category)

    # 글 생성
    try:
        if writer == "openai":
            html = call_openai(openai_key, openai_model, body_prompt)
        else:
            html = call_gemini(gemini_key, gemini_model, body_prompt)
        if not html or "<" not in html:
            return jsonify({"ok": False, "error": "LLM returned empty/invalid html"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # 이미지
    image_url = ""
    if img_provider == "pexels":
        q = f"{topic} {category}".strip()
        image_url = pexels_search_image_url(pexels_key, q) or pexels_search_image_url(pexels_key, topic)

    return jsonify({
        "ok": True,
        "topic": topic,
        "category": category,
        "generated_at": NOW_STR(),
        "title": topic,
        "html": html,
        "image_prompt": image_prompt,
        "image_provider": img_provider,
        "image_url": image_url
    })


# =========================================================
# API: topics money
# =========================================================
def parse_list_from_text(text: str) -> List[str]:
    t = strip_code_fences(text)
    # JSON 배열 우선
    try:
        arr = json.loads(t)
        if isinstance(arr, list):
            return [str(x).strip() for x in arr if str(x).strip()]
    except Exception:
        pass
    # 줄바꿈 리스트
    lines = [re.sub(r"^\s*[-•\d\.\)]\s*", "", x).strip() for x in t.splitlines()]
    lines = [x for x in lines if x]
    return lines

@app.route("/api/topics/money", methods=["POST"])
def api_topics_money():
    payload = request.get_json(silent=True) or {}
    count = int(payload.get("count") or 30)
    category = (payload.get("category") or "돈/재테크").strip()

    writer = (payload.get("writer") or "gemini").strip().lower()
    gemini_key = (payload.get("gemini_key") or "").strip()
    gemini_model = (payload.get("gemini_model") or "gemini-1.5-flash").strip()
    openai_key = (payload.get("openai_key") or "").strip()
    openai_model = (payload.get("openai_model") or "gpt-5.2-mini").strip()

    count = max(5, min(60, count))

    prompt = f"""너는 한국어 수익형 블로그 편집자다.
카테고리: {category}

아래 조건을 만족하는 '클릭 유도형 글 제목'을 {count}개 만들어줘.
- 과장/허위 금지, 정보성
- 숫자/체크리스트/가이드 형태 선호
- 서로 중복 최소화
- 출력은 JSON 배열로만 (예: ["제목1","제목2",...])
""".strip()

    try:
        if writer == "openai":
            txt = call_openai(openai_key, openai_model, prompt)
        else:
            txt = call_gemini(gemini_key, gemini_model, prompt)
        items = parse_list_from_text(txt)[:count]
        if not items:
            return jsonify({"ok": False, "error": "no topics generated"}), 500
        return jsonify({"ok": True, "items": items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================================================
# API: keyword collect -> topics
# =========================================================
@app.route("/api/keywords/collect_topics", methods=["POST"])
def api_keywords_collect_topics():
    payload = request.get_json(silent=True) or {}
    seed = (payload.get("seed") or "").strip()
    category = (payload.get("category") or "돈/재테크").strip()
    count = int(payload.get("count") or 30)

    writer = (payload.get("writer") or "gemini").strip().lower()
    gemini_key = (payload.get("gemini_key") or "").strip()
    gemini_model = (payload.get("gemini_model") or "gemini-1.5-flash").strip()
    openai_key = (payload.get("openai_key") or "").strip()
    openai_model = (payload.get("openai_model") or "gpt-5.2-mini").strip()

    if not seed:
        return jsonify({"ok": False, "error": "seed is required"}), 400

    count = max(5, min(60, count))

    prompt = f"""너는 SEO 키워드 기획자다.
시드 키워드: {seed}
카테고리: {category}

1) 롱테일 키워드 30~60개 생성
2) 그 중에서 수익형 정보글로 좋은 '글 제목(주제)' {count}개로 변환

조건:
- 과장/허위 금지
- “방법/조건/필요서류/체크리스트/비교/주의사항” 형태 선호
- 출력은 아래 JSON만 반환:

{{
  "keywords": ["키워드1","키워드2",...],
  "topics": ["제목1","제목2",...]
}}
""".strip()

    try:
        if writer == "openai":
            txt = call_openai(openai_key, openai_model, prompt)
        else:
            txt = call_gemini(gemini_key, gemini_model, prompt)

        t = strip_code_fences(txt)
        j = {}
        try:
            j = json.loads(t)
        except Exception:
            # JSON 찾기 fallback
            m = re.search(r"\{[\s\S]*\}", t)
            if m:
                j = json.loads(m.group(0))

        keywords = [str(x).strip() for x in (j.get("keywords") or []) if str(x).strip()]
        topics = [str(x).strip() for x in (j.get("topics") or []) if str(x).strip()]
        topics = topics[:count]

        if not topics:
            return jsonify({"ok": False, "error": "no topics generated"}), 500

        return jsonify({"ok": True, "keywords": keywords, "topics": topics})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================================================
# Blogger APIs
# =========================================================
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

    if not blog_id: return jsonify({"ok": False, "error": "blog_id missing"}), 400
    if not title: return jsonify({"ok": False, "error": "title missing"}), 400
    if not html: return jsonify({"ok": False, "error": "html missing"}), 400

    try:
        post_body = {"kind": "blogger#post", "title": title, "content": html}
        res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
        return jsonify({"ok": True, "id": res.get("id"), "url": res.get("url")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================================================
# Tasks APIs (예약발행 큐)
# =========================================================
@app.route("/api/tasks/add", methods=["POST"])
def api_tasks_add():
    payload = request.get_json(silent=True) or {}
    platform = (payload.get("platform") or "blogspot").strip()
    blog_id = str(payload.get("blog_id") or "").strip()
    blog_url = str(payload.get("blog_url") or "").strip()
    title = str(payload.get("title") or "").strip()
    html = str(payload.get("html") or "").strip()
    run_at = str(payload.get("run_at") or "").strip()

    if not blog_id or not title or not html or not run_at:
        return jsonify({"ok": False, "error": "blog_id/title/html/run_at required"}), 400

    conn = db_conn()
    cur = conn.cursor()
    now = NOW_STR()
    cur.execute("""
      INSERT INTO tasks(platform, blog_id, blog_url, title, html, run_at, status, created_at, updated_at)
      VALUES(?,?,?,?,?,?, 'pending', ?, ?)
    """, (platform, blog_id, blog_url, title, html, run_at, now, now))
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": task_id})

@app.route("/api/tasks/list", methods=["GET"])
def api_tasks_list():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 200")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return jsonify({"ok": True, "items": rows})

@app.route("/api/tasks/cancel/<int:task_id>", methods=["POST"])
def api_tasks_cancel(task_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status='canceled', updated_at=? WHERE id=? AND status='pending'", (NOW_STR(), task_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/tasks/run_due", methods=["POST"])
def api_tasks_run_due():
    """
    Render Background Worker를 쓰면 worker.py에서 돌리고,
    안 쓰면 cron(예: uptimerobot/webhook)으로 이 엔드포인트를 주기적으로 호출해도 됨.
    """
    ran = run_due_tasks(max_jobs=5)
    return jsonify({"ok": True, "ran": ran})


def run_due_tasks(max_jobs: int = 5) -> int:
    svc = get_blogger_client()
    if not svc:
        return 0

    conn = db_conn()
    cur = conn.cursor()

    now_iso = NOW().isoformat()
    cur.execute("""
      SELECT * FROM tasks
      WHERE status='pending' AND run_at <= ?
      ORDER BY run_at ASC
      LIMIT ?
    """, (now_iso, max_jobs))
    rows = [dict(r) for r in cur.fetchall()]

    ran = 0
    for t in rows:
        task_id = t["id"]
        try:
            cur.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (NOW_STR(), task_id))
            conn.commit()

            post_body = {"kind": "blogger#post", "title": t["title"], "content": t["html"]}
            res = svc.posts().insert(blogId=t["blog_id"], body=post_body, isDraft=False).execute()
            url = res.get("url")

            cur.execute("""
              UPDATE tasks
              SET status='ok', result_url=?, error=NULL, updated_at=?
              WHERE id=?
            """, (url, NOW_STR(), task_id))
            conn.commit()
            ran += 1

        except Exception as e:
            cur.execute("""
              UPDATE tasks
              SET status='err', error=?, updated_at=?
              WHERE id=?
            """, (str(e), NOW_STR(), task_id))
            conn.commit()

    conn.close()
    return ran


# =========================================================
# main
# =========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
