from __future__ import annotations

import os
import json
import time
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional, Any, Dict, List

import requests
from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build


# -----------------------------
# App
# -----------------------------
app = Flask(__name__, static_folder=".", static_url_path="")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
CORS(app, resources={r"/*": {"origins": "*"}})

# 세션 쿠키용
app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_change_me")

# -----------------------------
# Env
# -----------------------------
SCOPES = ["https://www.googleapis.com/auth/blogger"]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

# (선택) 서버에 저장해두면 settings.html에서 키를 굳이 안 보내도 됨
DEFAULT_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
DEFAULT_OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
DEFAULT_PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

TOKEN_FILE = os.environ.get("TOKEN_FILE", "google_token.json")
DB_FILE = os.environ.get("TASKS_DB", "tasks.db")

RUN_SCHEDULER = os.environ.get("RUN_SCHEDULER", "1").strip().lower() in ("1", "true", "yes")
SCHEDULER_POLL_SECONDS = int(os.environ.get("SCHEDULER_POLL_SECONDS", "20"))

# -----------------------------
# Utils
# -----------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def parse_iso(s: str) -> datetime:
    # accepts "2026-01-22T10:00:00Z" or with offset
    s = (s or "").strip()
    if not s:
        return now_utc()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)

def strip_code_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        # remove leading ```xxx
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()

# -----------------------------
# Static
# -----------------------------
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

# -----------------------------
# Token Save/Load
# -----------------------------
def save_token(creds: Credentials) -> None:
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

# -----------------------------
# OAuth
# -----------------------------
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

# -----------------------------
# Blogger APIs
# -----------------------------
@app.route("/api/blogger/blogs", methods=["GET"])
def api_blogger_blogs():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected. Visit /oauth/start"}), 401

    res = svc.blogs().listByUser(userId="self").execute()
    items = res.get("items", []) or []
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
        r = requests.get(url, headers=headers, params=params, timeout=20)
        if r.status_code != 200:
            return ""
        data = r.json()
        photos = data.get("photos", []) or []
        if not photos:
            return ""
        src = (photos[0] or {}).get("src", {}) or {}
        return src.get("large2x") or src.get("large") or src.get("original") or ""
    except Exception:
        return ""

# -----------------------------
# Content prompts
# -----------------------------
def writer_prompt(topic: str, category: str) -> str:
    return f"""
너는 수익형 정보 블로그 전문 작가다.
아래 조건을 만족하는 글을 "한국어"로 작성하라.

[주제] {topic}
[카테고리] {category}

[필수 조건]
- 출력은 "블로그에 바로 붙여넣는 HTML"만 출력 (마크다운 금지)
- 도입 400~700자
- H2 소제목 8~9개
- 각 H2 섹션 650~900자
- 표 1개 포함(<table> 사용, 비교/정리 형태)
- 독자가 행동하게 만드는 체크리스트/박스 2개 이상 (✅💡⚠️ 아이콘 포함)
- 마지막에 요약 3~5줄 + FAQ 5개(질문/답변 형식) + 행동유도(CTA)

[형식]
- <h1>은 넣지 말고, 바로 본문부터 시작
- 각 섹션은 <h2> + <p>들로 구성
- 과장/허위 금지, 정보는 일반적인 범위에서 정확하게
""".strip()

def money_topics_prompt(category: str, count: int) -> str:
    return f"""
너는 수익형 블로그 편집자다.
'{category}' 카테고리에서 애드센스/애드포스트에 유리한 "검색형" 제목을 {count}개 만들어라.

조건:
- 한국어
- 제목만 한 줄에 하나씩
- 클릭 유도는 하되 과장/낚시 금지
- 숫자/체크리스트/비교/방법/주의사항 형태 섞기
""".strip()

def image_prompt_for(topic: str, category: str) -> str:
    return f'{category} 주제 "{topic}" 블로그 썸네일, 텍스트 없음, 미니멀, 깔끔한 인포그래픽 느낌, 16:9, 고해상도'

def inject_image_into_html(html: str, image_url: str, alt_text: str) -> str:
    if not image_url:
        return html
    img_block = f"""
<div style="margin:14px 0;padding:12px;border:1px solid #e5e7eb;border-radius:12px;background:#fafafa">
  <img src="{image_url}" alt="{alt_text}" style="max-width:100%;border-radius:10px;display:block"/>
  <div style="font-size:12px;color:#6b7280;margin-top:8px">이미지 출처: Pexels</div>
</div>
""".strip()
    return img_block + "\n\n" + html

# -----------------------------
# Gemini (REST)
# -----------------------------
def gemini_generate_text(api_key: str, model: str, prompt: str) -> str:
    api_key = (api_key or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY missing")

    model = (model or "").strip() or "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192
        }
    }
    r = requests.post(url, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:500]}")
    j = r.json()
    cands = j.get("candidates", []) or []
    if not cands:
        raise RuntimeError("Gemini returned no candidates")
    parts = (((cands[0] or {}).get("content") or {}).get("parts") or [])
    text = ""
    for p in parts:
        if "text" in p:
            text += p["text"]
    return strip_code_fences(text)

# -----------------------------
# OpenAI (Responses API)
# -----------------------------
def openai_generate_text(api_key: str, model: str, prompt: str) -> str:
    api_key = (api_key or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing")

    model = (model or "").strip() or "gpt-5.2-mini"
    url = "https://api.openai.com/v1/responses"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "input": prompt
    }
    r = requests.post(url, headers=headers, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI error {r.status_code}: {r.text[:500]}")
    j = r.json()
    # Responses API has output_text convenience in SDK; in raw JSON it's often present as "output_text" too.
    if "output_text" in j and isinstance(j["output_text"], str):
        return strip_code_fences(j["output_text"])
    # fallback: try to stitch
    out = ""
    for item in (j.get("output") or []):
        for c in (item.get("content") or []):
            if c.get("type") == "output_text" and "text" in c:
                out += c["text"]
    return strip_code_fences(out)

# -----------------------------
# /api/generate
# -----------------------------
@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}

    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "돈/재테크").strip()

    writer_provider = (payload.get("writer_provider") or "gemini").strip().lower()
    gemini_key = (payload.get("gemini_key") or DEFAULT_GEMINI_API_KEY).strip()
    gemini_model = (payload.get("gemini_model") or "gemini-1.5-flash").strip()

    openai_key = (payload.get("openai_key") or DEFAULT_OPENAI_API_KEY).strip()
    openai_model = (payload.get("openai_model") or "gpt-5.2-mini").strip()

    img_provider = (payload.get("img_provider") or "pexels").strip().lower()
    pexels_key = (payload.get("pexels_key") or DEFAULT_PEXELS_API_KEY).strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    # 1) 본문 생성
    prompt = writer_prompt(topic, category)
    try:
        if writer_provider == "openai":
            html = openai_generate_text(openai_key, openai_model, prompt)
        else:
            html = gemini_generate_text(gemini_key, gemini_model, prompt)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # 2) 이미지
    image_prompt = image_prompt_for(topic, category)
    image_url = ""

    if img_provider == "pexels":
        q = f"{topic} {category}".strip()
        image_url = pexels_search_image_url(pexels_key, q) or pexels_search_image_url(pexels_key, topic)
    elif img_provider == "gemini":
        # Gemini 이미지 "생성"은 모델/권한이 케이스가 많아서 여기선 안전하게 프롬프트만 제공
        image_url = ""
    elif img_provider == "none":
        image_url = ""

    # 3) HTML에 이미지 자동삽입(pexels인 경우)
    if image_url:
        html = inject_image_into_html(html, image_url, alt_text=topic)

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

# -----------------------------
# /api/topics/money
# -----------------------------
@app.route("/api/topics/money", methods=["POST"])
def api_topics_money():
    payload = request.get_json(silent=True) or {}
    category = (payload.get("category") or "돈/재테크").strip()
    count = int(payload.get("count") or 30)
    count = max(5, min(60, count))

    writer_provider = (payload.get("writer_provider") or "gemini").strip().lower()
    gemini_key = (payload.get("gemini_key") or DEFAULT_GEMINI_API_KEY).strip()
    gemini_model = (payload.get("gemini_model") or "gemini-1.5-flash").strip()
    openai_key = (payload.get("openai_key") or DEFAULT_OPENAI_API_KEY).strip()
    openai_model = (payload.get("openai_model") or "gpt-5.2-mini").strip()

    prompt = money_topics_prompt(category, count)

    try:
        if writer_provider == "openai":
            text = openai_generate_text(openai_key, openai_model, prompt)
        else:
            text = gemini_generate_text(gemini_key, gemini_model, prompt)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    lines = [ln.strip(" -•\t").strip() for ln in (text or "").splitlines()]
    items = [ln for ln in lines if ln]
    # 중복 제거 + count 제한
    seen = set()
    out = []
    for x in items:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
        if len(out) >= count:
            break

    return jsonify({"ok": True, "count": len(out), "items": out})

# -----------------------------
# Tasks DB
# -----------------------------
def db_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
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
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()

init_db()

@app.route("/api/tasks/add", methods=["POST"])
def api_tasks_add():
    payload = request.get_json(silent=True) or {}
    platform = (payload.get("platform") or "blogspot").strip().lower()
    blog_id = str(payload.get("blog_id") or "").strip()
    blog_url = str(payload.get("blog_url") or "").strip()
    title = str(payload.get("title") or "").strip()
    html = str(payload.get("html") or "").strip()
    run_at = str(payload.get("run_at") or "").strip()

    if platform != "blogspot":
        return jsonify({"ok": False, "error": "Only platform=blogspot supported for auto publish"}), 400
    if not blog_id or not title or not html or not run_at:
        return jsonify({"ok": False, "error": "blog_id/title/html/run_at required"}), 400

    # validate ISO
    try:
        _ = parse_iso(run_at)
    except Exception:
        return jsonify({"ok": False, "error": "run_at must be ISO datetime (e.g. 2026-01-22T10:00:00Z)"}), 400

    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tasks(platform, blog_id, blog_url, title, html, run_at, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (platform, blog_id, blog_url, title, html, run_at, "pending", iso_utc(now_utc()))
    )
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
    return jsonify({"ok": True, "count": len(rows), "items": rows})

@app.route("/api/tasks/cancel/<int:task_id>", methods=["POST"])
def api_tasks_cancel(task_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE tasks SET status='canceled' WHERE id=? AND status='pending'", (task_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# -----------------------------
# Scheduler (in-process)
# -----------------------------
def run_one_task(row: sqlite3.Row) -> Dict[str, Any]:
    task_id = row["id"]
    blog_id = row["blog_id"]
    title = row["title"]
    html = row["html"]

    svc = get_blogger_client()
    if not svc:
        raise RuntimeError("OAuth not connected in server (token missing/expired). Visit /oauth/start again.")

    post_body = {"kind": "blogger#post", "title": title, "content": html}
    res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
    return {"url": res.get("url") or "", "post_id": res.get("id") or ""}

def scheduler_loop():
    while True:
        try:
            conn = db_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM tasks
                WHERE status='pending'
                ORDER BY run_at ASC
                LIMIT 20
            """)
            rows = cur.fetchall()
            conn.close()

            now_dt = now_utc()
            for row in rows:
                run_at_dt = parse_iso(row["run_at"])
                if run_at_dt > now_dt:
                    continue

                # mark running
                conn = db_conn()
                cur = conn.cursor()
                cur.execute("UPDATE tasks SET status='running' WHERE id=? AND status='pending'", (row["id"],))
                conn.commit()
                conn.close()

                try:
                    result = run_one_task(row)
                    conn = db_conn()
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE tasks SET status='ok', result_url=?, error=NULL WHERE id=?",
                        (result.get("url", ""), row["id"])
                    )
                    conn.commit()
                    conn.close()
                except Exception as e:
                    conn = db_conn()
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE tasks SET status='err', error=? WHERE id=?",
                        (str(e), row["id"])
                    )
                    conn.commit()
                    conn.close()

        except Exception:
            # scheduler 자체 오류는 조용히 재시도
            pass

        time.sleep(SCHEDULER_POLL_SECONDS)

_scheduler_started = False

@app.before_request
def ensure_scheduler_started():
    """
    Render에서 가장 간단히 '예약발행'까지 되게 하려면
    웹 프로세스 안에서 스케줄러를 1개만 돌립니다.
    (중요) gunicorn workers=1 권장
    """
    global _scheduler_started
    if RUN_SCHEDULER and not _scheduler_started:
        _scheduler_started = True
        th = threading.Thread(target=scheduler_loop, daemon=True)
        th.start()

# -----------------------------
# Main
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
