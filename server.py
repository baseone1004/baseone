import os, json, time, sqlite3
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

# =========================
# 기본 설정
# =========================
APP_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(APP_DIR, "google_token.json")
DB_FILE = os.path.join(APP_DIR, "tasks.db")

SCOPES = ["https://www.googleapis.com/auth/blogger"]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

SESSION_SECRET = os.environ.get("SESSION_SECRET", "BaseOne!Session#Secret@2026$Prod")
TASK_RUNNER_TOKEN = os.environ.get("TASK_RUNNER_TOKEN", "")  # 선택: 워커 호출 보호용

def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def parse_iso(dt: str) -> datetime:
    # ISO8601 -> datetime (UTC)
    return datetime.fromisoformat(dt.replace("Z", "+00:00")).astimezone(timezone.utc)

# =========================
# Flask 앱
# =========================
app = Flask(__name__, static_folder=APP_DIR, static_url_path="")
app.secret_key = SESSION_SECRET
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

CORS(app, supports_credentials=True)

# =========================
# DB (예약 발행 큐)
# =========================
def db_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL,
        platform TEXT NOT NULL,
        blog_id TEXT,
        blog_url TEXT,
        title TEXT,
        html TEXT,
        run_at TEXT,
        created_at TEXT,
        result_url TEXT,
        error TEXT
      )
    """)
    conn.commit()
    conn.close()

db_init()

# =========================
# OAuth Token Save/Load
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
# Static Pages
# =========================
@app.route("/")
def home():
    return send_from_directory(APP_DIR, "index.html")

@app.route("/settings")
def settings_page():
    return send_from_directory(APP_DIR, "settings.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})

@app.route("/__routes")
def __routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))

# =========================
# OAuth Routes
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

def blogger_post(blog_id: str, title: str, html: str) -> Dict[str, Any]:
    svc = get_blogger_client()
    if not svc:
        return {"ok": False, "error": "OAuth not connected. Visit /oauth/start"}

    post_body = {"kind": "blogger#post", "title": title, "content": html}
    res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
    return {"ok": True, "id": res.get("id"), "url": res.get("url")}

@app.route("/api/blogger/post", methods=["POST"])
def api_blogger_post():
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
        j = blogger_post(blog_id, title, html)
        if not j.get("ok"):
            return jsonify(j), 401
        return jsonify(j)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# 이미지: Pexels
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

def wrap_image_html(image_url: str, alt: str) -> str:
    if not image_url:
        return ""
    safe_alt = (alt or "").replace('"', "'")
    return f"""
    <figure style="margin:18px 0;padding:0">
      <img src="{image_url}" alt="{safe_alt}" style="width:100%;max-width:920px;border-radius:14px;border:1px solid #e5e7eb;display:block;margin:0 auto"/>
      <figcaption style="text-align:center;color:#6b7280;font-size:12px;margin-top:8px">이미지 출처: Pexels</figcaption>
    </figure>
    """.strip()

# =========================
# 글 생성 프롬프트
# =========================
def make_body_prompt(topic: str, category: str) -> str:
    return f"""너는 수익형 정보블로그 작가다.
아래 조건으로 '{topic}' 글을 한국어로 작성해줘.

- 카테고리: {category}
- 분량: 6,000~10,000자 (너무 과하면 안 됨)
- H2 소제목 8개 이상
- 각 소제목 아래 실무적으로 바로 쓰는 내용 (예: 체크리스트, 실수 방지)
- 표 1개 포함(<table>)
- 아이콘/박스 디자인(✅💡⚠️) div로 포함
- 마지막: 요약(3~5줄) + FAQ 5개 + 행동유도

※ 출력은 블로그에 붙여넣기 좋은 HTML로 작성해줘.
""".strip()

def make_image_query(topic: str, category: str) -> str:
    # Pexels 검색용: 한글/영문 혼합해도 됨
    return f"{topic} {category} finance people document".strip()

# =========================
# Gemini / OpenAI 호출 (공식 키 기반)
# =========================
def call_gemini_generate_html(gemini_key: str, model: str, prompt: str) -> str:
    if not gemini_key:
        raise RuntimeError("gemini_key missing")

    model = model or "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    params = {"key": gemini_key}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192}
    }
    r = requests.post(url, params=params, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:2000]}")
    data = r.json()
    # candidates[0].content.parts[].text
    text = ""
    cands = data.get("candidates") or []
    if cands:
        parts = (((cands[0] or {}).get("content") or {}).get("parts") or [])
        text = "\n".join([p.get("text","") for p in parts if isinstance(p, dict)])
    return (text or "").strip()

def call_openai_generate_html(openai_key: str, model: str, prompt: str) -> str:
    if not openai_key:
        raise RuntimeError("openai_key missing")

    model = model or "gpt-5.2-mini"
    url = "https://api.openai.com/v1/responses"
    headers = {"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": 3000
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI error {r.status_code}: {r.text[:2000]}")
    data = r.json()

    # responses API: output_text가 있으면 가장 간단
    if "output_text" in data and isinstance(data["output_text"], str):
        return data["output_text"].strip()

    # fallback: output[].content[].text
    out = []
    for item in data.get("output", []) or []:
        for c in (item.get("content", []) or []):
            if c.get("type") == "output_text":
                out.append(c.get("text",""))
    return "\n".join(out).strip()

def ensure_html(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    # 모델이 마크다운으로 주는 경우 대충 html로 감싸기
    if "<h" in t or "<p" in t or "<div" in t or "<table" in t:
        return t
    return f"<div style='line-height:1.75'>\n{t.replace('\n','<br/>')}\n</div>"

# =========================
# 글 생성 API
# =========================
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

    # 1) 이미지 URL 만들기
    image_url = ""
    image_prompt = make_image_query(topic, category)
    if img_provider == "pexels":
        image_url = pexels_search_image_url(pexels_key, image_prompt) or pexels_search_image_url(pexels_key, topic)

    # 2) 본문 생성
    try:
        if writer == "openai":
            raw = call_openai_generate_html(openai_key, openai_model, body_prompt)
        else:
            raw = call_gemini_generate_html(gemini_key, gemini_model, body_prompt)
        html = ensure_html(raw)
    except Exception as e:
        return jsonify({"ok": False, "error": f"generate failed: {str(e)}"}), 500

    # 3) 이미지가 있으면 본문 상단에 삽입
    if image_url:
        html = wrap_image_html(image_url, topic) + "\n\n" + html

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

# =========================
# ✅ 키워드 자동수집(안전한 방식: AI 기반)
# =========================
KEYWORD_PROMPT = """너는 수익형 블로그 SEO 기획자다.
아래 조건에 맞는 '검색 키워드(롱테일)' 목록을 만들어라.

[입력]
- 카테고리: {category}
- 씨드(기준 키워드): {seed}
- 목표 개수: {count}

[출력 규칙]
- 한국어 키워드만
- 각 줄에 키워드 1개
- 너무 일반적인 단어(예: 재테크, 보험)는 피하고 '의도'가 명확한 롱테일로
- 광고/수익으로 이어질 확률이 높은 쿼리(비교/추천/조건/신청/절약/방법/후기/주의사항) 중심
- 중복 제거
""".strip()

TOPICIZE_PROMPT = """너는 수익형 정보블로그 편집장이다.
아래 키워드 목록을 보고, 클릭을 유도하는 '글 제목(주제)'로 변환해라.

[입력]
- 카테고리: {category}
- 키워드 목록:
{keywords}

[출력 규칙]
- 제목만 출력
- 한 줄에 1개
- 총 {count}개
- 과장 금지, 하지만 클릭 유도되는 구조(체크리스트/조건/비교/실수방지/한눈에 정리)
""".strip()

def ai_lines(writer: str, gemini_key: str, gemini_model: str, openai_key: str, openai_model: str, prompt: str) -> List[str]:
    if writer == "openai":
        text = call_openai_generate_html(openai_key, openai_model, prompt)
    else:
        text = call_gemini_generate_html(gemini_key, gemini_model, prompt)

    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        # 번호/불릿 제거
        s = s.lstrip("-•").strip()
        if s[:2].isdigit() and (s[2:3] == "." or s[2:3] == ")"):
            s = s[3:].strip()
        if s and s not in lines:
            lines.append(s)
    return lines

@app.route("/api/keywords/collect_topics", methods=["POST"])
def api_keywords_collect_topics():
    """
    input:
      {
        seed: "정부지원금",
        category: "정부지원",
        count: 30,
        writer: "gemini"|"openai",
        gemini_key, gemini_model,
        openai_key, openai_model
      }
    output:
      { ok:true, keywords:[...], topics:[...] }
    """
    payload = request.get_json(silent=True) or {}
    seed = (payload.get("seed") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"
    count = int(payload.get("count") or 30)
    count = max(5, min(60, count))

    writer = (payload.get("writer") or "gemini").strip().lower()
    gemini_key = (payload.get("gemini_key") or "").strip()
    gemini_model = (payload.get("gemini_model") or "gemini-1.5-flash").strip()
    openai_key = (payload.get("openai_key") or "").strip()
    openai_model = (payload.get("openai_model") or "gpt-5.2-mini").strip()

    if not seed:
        return jsonify({"ok": False, "error": "seed is required"}), 400

    # 1) 키워드 수집
    try:
        k_prompt = KEYWORD_PROMPT.format(category=category, seed=seed, count=count*2)
        keywords = ai_lines(writer, gemini_key, gemini_model, openai_key, openai_model, k_prompt)
        keywords = keywords[: max(count, 30)]  # 넉넉히
    except Exception as e:
        return jsonify({"ok": False, "error": f"keyword collect failed: {str(e)}"}), 500

    # 2) 주제화
    try:
        kw_block = "\n".join(keywords[:60])
        t_prompt = TOPICIZE_PROMPT.format(category=category, keywords=kw_block, count=count)
        topics = ai_lines(writer, gemini_key, gemini_model, openai_key, openai_model, t_prompt)
        topics = topics[:count]
    except Exception as e:
        return jsonify({"ok": False, "error": f"topicize failed: {str(e)}"}), 500

    return jsonify({"ok": True, "seed": seed, "category": category, "keywords": keywords, "topics": topics})

# =========================
# (선택) 예약발행 Task API
# - index.html에서 /api/tasks/add/list/cancel 호출함
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

    if platform != "blogspot":
        return jsonify({"ok": False, "error": "only blogspot supported"}), 400
    if not blog_id:
        return jsonify({"ok": False, "error": "blog_id missing"}), 400
    if not title or not html:
        return jsonify({"ok": False, "error": "title/html missing"}), 400
    if not run_at:
        run_at = utc_now_iso()

    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO tasks(status, platform, blog_id, blog_url, title, html, run_at, created_at)
      VALUES(?,?,?,?,?,?,?,?)
    """, ("pending", platform, blog_id, blog_url, title, html, run_at, utc_now_iso()))
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
    cur.execute("UPDATE tasks SET status='canceled' WHERE id=? AND status='pending'", (task_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# =========================
# (선택) 워커용: due tasks 실행
# - Render Background Worker나 cron에서 호출 가능
# =========================
@app.route("/api/tasks/run_due", methods=["POST"])
def api_tasks_run_due():
    if TASK_RUNNER_TOKEN:
        tok = request.headers.get("X-Runner-Token","").strip()
        if tok != TASK_RUNNER_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

    now = datetime.now(timezone.utc)
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
      SELECT * FROM tasks
      WHERE status='pending'
      ORDER BY run_at ASC
      LIMIT 20
    """)
    rows = [dict(r) for r in cur.fetchall()]
    ran = 0

    for t in rows:
        try:
            run_at = parse_iso(t["run_at"])
        except Exception:
            run_at = now

        if run_at > now:
            continue

        # running
        cur.execute("UPDATE tasks SET status='running', error=NULL WHERE id=?", (t["id"],))
        conn.commit()

        try:
            res = blogger_post(t["blog_id"], t["title"], t["html"])
            if res.get("ok"):
                cur.execute("""
                  UPDATE tasks SET status='ok', result_url=?, error=NULL WHERE id=?
                """, (res.get("url",""), t["id"]))
            else:
                cur.execute("""
                  UPDATE tasks SET status='err', error=? WHERE id=?
                """, (res.get("error","unknown"), t["id"]))
            conn.commit()
            ran += 1
        except Exception as e:
            cur.execute("UPDATE tasks SET status='err', error=? WHERE id=?", (str(e), t["id"]))
            conn.commit()

    conn.close()
    return jsonify({"ok": True, "ran": ran, "time": now_str()})

# =========================
# main
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
