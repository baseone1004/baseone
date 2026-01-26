import os, json, time, sqlite3, threading
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

# =========================
# App
# =========================
app = Flask(__name__, static_folder=".", static_url_path="")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# 세션 키(없으면 랜덤)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

def now_utc_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

# =========================
# ENV
# =========================
SCOPES = ["https://www.googleapis.com/auth/blogger"]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

TOKEN_FILE = os.environ.get("TOKEN_FILE", "google_token.json")

DB_PATH = os.environ.get("DB_PATH", "baseone.db")
ENABLE_SCHEDULER = os.environ.get("ENABLE_SCHEDULER", "0") == "1"
SCHEDULER_INTERVAL_SEC = int(os.environ.get("SCHEDULER_INTERVAL_SEC", "25"))

# =========================
# DB (SQLite)
# =========================
def db_conn():
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

def db_init():
    con = db_conn()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      platform TEXT NOT NULL,
      blog_id TEXT NOT NULL,
      blog_url TEXT,
      title TEXT NOT NULL,
      html TEXT NOT NULL,
      run_at TEXT NOT NULL,       -- ISO UTC
      status TEXT NOT NULL DEFAULT 'pending', -- pending|running|ok|err|canceled
      result_url TEXT,
      error TEXT,
      created_at TEXT NOT NULL
    )
    """)
    con.commit()
    con.close()

db_init()

# =========================
# OAuth Token
# =========================
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

# =========================
# Static pages
# =========================
@app.route("/")
def home():
    # index.html이 있으면 화면, 없으면 JSON
    if os.path.exists("index.html"):
        return send_from_directory(".", "index.html")
    return jsonify({"ok": True, "service": "baseone-backend", "time": now_utc_iso()})

@app.route("/settings")
def settings():
    if os.path.exists("settings.html"):
        return send_from_directory(".", "settings.html")
    return jsonify({"ok": False, "error": "settings.html not found"}), 404

@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "baseone-backend", "time": now_utc_iso()})

@app.route("/__routes")
def __routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))

# =========================
# OAuth routes
# =========================
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
    flow = make_flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    save_token(creds)
    return redirect("/?oauth=ok")

@app.route("/api/oauth/status")
def oauth_status():
    return jsonify({"ok": True, "connected": bool(load_token())})

# =========================
# Blogger API
# =========================
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

# =========================
# Pexels
# =========================
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

# =========================
# Writer: Gemini / OpenAI
# =========================
def gemini_generate(gemini_key: str, model: str, prompt: str) -> str:
    """
    Google Generative Language REST.
    - v1 먼저 시도, 실패하면 v1beta 시도
    - model이 "models/..." 형태면 자동 보정
    """
    if not gemini_key:
        raise RuntimeError("gemini_key missing")

    m = (model or "gemini-1.5-flash-latest").strip()
    if m.startswith("models/"):
        m = m.split("models/", 1)[1]

    body = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192
        }
    }

    # v1
    url_v1 = f"https://generativelanguage.googleapis.com/v1/models/{m}:generateContent?key={gemini_key}"
    r = requests.post(url_v1, json=body, timeout=60)
    if r.status_code == 200:
        j = r.json()
        return (j.get("candidates", [{}])[0]
                  .get("content", {})
                  .get("parts", [{}])[0]
                  .get("text", "")).strip()

    # v1beta fallback
    url_v1b = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={gemini_key}"
    r2 = requests.post(url_v1b, json=body, timeout=60)
    if r2.status_code != 200:
        raise RuntimeError(f"Gemini API error: {r2.status_code} {r2.text}")
    j2 = r2.json()
    return (j2.get("candidates", [{}])[0]
              .get("content", {})
              .get("parts", [{}])[0]
              .get("text", "")).strip()

def openai_generate(openai_key: str, model: str, prompt: str) -> str:
    """
    OpenAI Responses API (권장) - 단순 텍스트 생성
    """
    if not openai_key:
        raise RuntimeError("openai_key missing")
    m = (model or "gpt-5.2-mini").strip()
    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"}
    payload = {
        "model": m,
        "input": prompt
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI API error: {r.status_code} {r.text}")

    j = r.json()
    # responses output text 추출(여러 형태 대응)
    text = ""
    out = j.get("output", [])
    for item in out:
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                text += c.get("text", "")
    return text.strip()

# =========================
# Prompts
# =========================
def make_body_prompt(topic: str, category: str) -> str:
    return f"""너는 수익형 정보블로그 작가다.
아래 조건으로 '{topic}' 글을 한국어로 작성해줘.

- 카테고리: {category}
- 출력: 블로그에 붙여넣기 좋은 HTML
- 구성: H2 소제목 8~9개 (각 700자 이상)
- 표 1개 포함(<table>)
- 아이콘/박스 디자인(✅💡⚠️) div로 포함
- 마지막: 요약(3~5줄) + FAQ 5개 + 행동유도
- SEO: 검색 의도(정보/비교/방법) 충족, 과장/허위 금지, 최신성은 "확인 필요" 문구로 처리

※ 절대 빈값 없이 HTML만 출력.
""".strip()

def make_image_prompt(topic: str, category: str) -> str:
    # Pexels 검색용 문구(영문이 더 잘 맞는 편)
    return f"{category} infographic thumbnail, topic: {topic}, clean minimal, no text, high quality, 16:9"

# =========================
# Generate API
# =========================
@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}
    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"

    writer = (payload.get("writer") or "gemini").strip().lower()
    gemini_key = (payload.get("gemini_key") or "").strip()
    gemini_model = (payload.get("gemini_model") or "gemini-1.5-flash-latest").strip()
    openai_key = (payload.get("openai_key") or "").strip()
    openai_model = (payload.get("openai_model") or "gpt-5.2-mini").strip()

    img_provider = (payload.get("img_provider") or "pexels").strip().lower()
    pexels_key = (payload.get("pexels_key") or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    body_prompt = make_body_prompt(topic, category)
    image_prompt = make_image_prompt(topic, category)

    # 글 생성
    html = ""
    try:
        if writer == "openai":
            html = openai_generate(openai_key, openai_model, body_prompt)
        else:
            html = gemini_generate(gemini_key, gemini_model, body_prompt)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # 이미지(pexels)
    image_url = ""
    if img_provider == "pexels":
        q = f"{topic} {category}".strip()
        image_url = pexels_search_image_url(pexels_key, q) or pexels_search_image_url(pexels_key, topic)

    return jsonify({
        "ok": True,
        "topic": topic,
        "category": category,
        "generated_at": now_utc_iso(),
        "title": topic,
        "html": html,
        "body_prompt": body_prompt,
        "image_prompt": image_prompt,
        "image_provider": img_provider,
        "image_url": image_url
    })

# =========================
# Topic / Keyword APIs (LLM로 안전 생성)
# =========================
def llm_text(writer: str, gemini_key: str, gemini_model: str, openai_key: str, openai_model: str, prompt: str) -> str:
    w = (writer or "gemini").lower()
    if w == "openai":
        return openai_generate(openai_key, openai_model, prompt)
    return gemini_generate(gemini_key, gemini_model, prompt)

@app.route("/api/topics/money", methods=["POST"])
def api_topics_money():
    p = request.get_json(silent=True) or {}
    count = int(p.get("count") or 30)
    count = max(5, min(60, count))
    category = (p.get("category") or "돈/재테크").strip()

    writer = (p.get("writer") or "gemini").strip().lower()
    gemini_key = (p.get("gemini_key") or "").strip()
    gemini_model = (p.get("gemini_model") or "gemini-1.5-flash-latest").strip()
    openai_key = (p.get("openai_key") or "").strip()
    openai_model = (p.get("openai_model") or "gpt-5.2-mini").strip()

    prompt = f"""너는 한국 수익형 블로그 주제 기획자다.
카테고리: {category}
조건:
- 클릭 유도형 제목 {count}개
- 과장/허위 금지
- 제목만 한 줄에 하나씩 (번호/불릿 없이)
- 가능한 한 서로 겹치지 않게

출력:""".strip()

    try:
        txt = llm_text(writer, gemini_key, gemini_model, openai_key, openai_model, prompt)
        lines = [x.strip() for x in txt.splitlines() if x.strip()]
        # 번호 제거 보정
        clean = []
        for ln in lines:
            ln = ln.lstrip("-•").strip()
            ln = ln.split(") ", 1)[-1] if ln[:3].endswith(")") else ln
            clean.append(ln)
        clean = [x for x in clean if len(x) >= 6]
        return jsonify({"ok": True, "items": clean[:count]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/keywords/collect_topics", methods=["POST"])
def api_keywords_collect_topics():
    p = request.get_json(silent=True) or {}
    seed = (p.get("seed") or "").strip()
    if not seed:
        return jsonify({"ok": False, "error": "seed is required"}), 400

    count = int(p.get("count") or 30)
    count = max(5, min(60, count))
    category = (p.get("category") or "정보").strip()

    writer = (p.get("writer") or "gemini").strip().lower()
    gemini_key = (p.get("gemini_key") or "").strip()
    gemini_model = (p.get("gemini_model") or "gemini-1.5-flash-latest").strip()
    openai_key = (p.get("openai_key") or "").strip()
    openai_model = (p.get("openai_model") or "gpt-5.2-mini").strip()

    prompt = f"""너는 수익형 키워드 리서처다.
시드 키워드: {seed}
카테고리: {category}

1) 시드로부터 롱테일 키워드 25개 생성 (검색 의도 분명하게)
2) 위 키워드를 기반으로 클릭 유도형 블로그 제목 {count}개 생성
규칙:
- 과장/허위 금지
- 제목은 한 줄에 하나
- 번호/불릿 없이

출력 포맷:
[KEYWORDS]
(키워드 한 줄에 하나)
[TOPICS]
(제목 한 줄에 하나)
""".strip()

    try:
        txt = llm_text(writer, gemini_key, gemini_model, openai_key, openai_model, prompt)

        def split_section(tag):
            if tag not in txt:
                return []
            part = txt.split(tag, 1)[1]
            # 다음 섹션 전까지
            for nxt in ["[KEYWORDS]", "[TOPICS]"]:
                if nxt != tag and nxt in part:
                    part = part.split(nxt, 1)[0]
            return [x.strip() for x in part.splitlines() if x.strip()]

        keywords = split_section("[KEYWORDS]")
        topics = split_section("[TOPICS]")

        # 혹시 섹션이 안 지켜지면 fallback
        if not topics:
            lines = [x.strip() for x in txt.splitlines() if x.strip()]
            topics = lines[-count:]

        topics = [t.lstrip("-•").strip() for t in topics]
        return jsonify({"ok": True, "seed": seed, "keywords": keywords[:50], "topics": topics[:count]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# Tasks APIs (예약 저장/조회/취소)
# =========================
@app.route("/api/tasks/add", methods=["POST"])
def api_tasks_add():
    p = request.get_json(silent=True) or {}
    platform = (p.get("platform") or "blogspot").strip()
    blog_id = (p.get("blog_id") or "").strip()
    blog_url = (p.get("blog_url") or "").strip()
    title = (p.get("title") or "").strip()
    html = (p.get("html") or "").strip()
    run_at = (p.get("run_at") or "").strip()

    if not blog_id: return jsonify({"ok": False, "error": "blog_id missing"}), 400
    if not title: return jsonify({"ok": False, "error": "title missing"}), 400
    if not html: return jsonify({"ok": False, "error": "html missing"}), 400
    if not run_at: return jsonify({"ok": False, "error": "run_at missing (ISO UTC)"}), 400

    # run_at 파싱 검증
    try:
        # "Z" 처리
        ra = run_at.replace("Z", "+00:00")
        datetime.fromisoformat(ra)
    except Exception:
        return jsonify({"ok": False, "error": "run_at must be ISO format (e.g. 2026-01-26T03:00:00.000Z)"}), 400

    con = db_conn()
    cur = con.cursor()
    cur.execute("""
      INSERT INTO tasks(platform, blog_id, blog_url, title, html, run_at, status, created_at)
      VALUES(?,?,?,?,?,?, 'pending', ?)
    """, (platform, blog_id, blog_url, title, html, run_at, now_utc_iso()))
    con.commit()
    task_id = cur.lastrowid
    con.close()
    return jsonify({"ok": True, "id": task_id})

@app.route("/api/tasks/list", methods=["GET"])
def api_tasks_list():
    con = db_conn()
    cur = con.cursor()
    cur.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 200")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return jsonify({"ok": True, "items": rows})

@app.route("/api/tasks/cancel/<int:task_id>", methods=["POST"])
def api_tasks_cancel(task_id: int):
    con = db_conn()
    cur = con.cursor()
    cur.execute("UPDATE tasks SET status='canceled' WHERE id=? AND status='pending'", (task_id,))
    con.commit()
    changed = cur.rowcount
    con.close()
    return jsonify({"ok": True, "canceled": bool(changed)})

# =========================
# Task Runner (스케줄러)
# - Render Background Worker가 없으면 이걸 ENABLE_SCHEDULER=1 로 켜서
#   웹서버 프로세스가 살아있는 동안 예약 실행 가능
# =========================
_runner_lock = threading.Lock()

def run_due_tasks_once():
    with _runner_lock:
        con = db_conn()
        cur = con.cursor()
        now = datetime.now(timezone.utc)

        cur.execute("""
          SELECT * FROM tasks
          WHERE status='pending'
          ORDER BY id ASC
          LIMIT 10
        """)
        rows = [dict(r) for r in cur.fetchall()]

        for t in rows:
            try:
                ra = t["run_at"].replace("Z", "+00:00")
                run_dt = datetime.fromisoformat(ra)
            except Exception:
                # 잘못된 run_at
                cur.execute("UPDATE tasks SET status='err', error=? WHERE id=?",
                            ("invalid run_at", t["id"]))
                con.commit()
                continue

            if run_dt > now:
                continue

            # 실행
            cur.execute("UPDATE tasks SET status='running' WHERE id=? AND status='pending'", (t["id"],))
            con.commit()

            try:
                # Blogger post
                svc = get_blogger_client()
                if not svc:
                    raise RuntimeError("OAuth not connected on server (token missing/expired)")

                post_body = {"kind": "blogger#post", "title": t["title"], "content": t["html"]}
                res = svc.posts().insert(blogId=t["blog_id"], body=post_body, isDraft=False).execute()
                url = res.get("url") or ""

                cur.execute("UPDATE tasks SET status='ok', result_url=?, error=NULL WHERE id=?",
                            (url, t["id"]))
                con.commit()
            except Exception as e:
                cur.execute("UPDATE tasks SET status='err', error=? WHERE id=?",
                            (str(e), t["id"]))
                con.commit()

        con.close()

def scheduler_loop():
    while True:
        try:
            run_due_tasks_once()
        except Exception:
            pass
        time.sleep(SCHEDULER_INTERVAL_SEC)

if ENABLE_SCHEDULER:
    th = threading.Thread(target=scheduler_loop, daemon=True)
    th.start()

# =========================
# Main
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
