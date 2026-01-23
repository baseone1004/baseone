import os, json, time
from datetime import datetime
from typing import Optional

import requests
from flask import Flask, request, jsonify, redirect, send_from_directory, session
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

# ================== 기본 설정 ==================
app = Flask(__name__, static_folder=".")
app.secret_key = os.environ.get("SESSION_SECRET", "BaseOneSecret")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
CORS(app)

SCOPES = ["https://www.googleapis.com/auth/blogger"]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI")

TOKEN_FILE = "google_token.json"
TASK_FILE = "tasks.json"

# ================== 유틸 ==================
def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ================== OAuth ==================
def save_token(creds: Credentials):
    save_json(TOKEN_FILE, {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    })

def load_token() -> Optional[Credentials]:
    if not os.path.exists(TOKEN_FILE):
        return None
    return Credentials(**load_json(TOKEN_FILE, {}))

def get_blogger():
    creds = load_token()
    if not creds:
        return None
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        save_token(creds)
    return build("blogger", "v3", credentials=creds)

def make_flow():
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )

@app.route("/oauth/start")
def oauth_start():
    flow = make_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true"
    )
    session["oauth_state"] = state
    return redirect(url)

@app.route("/oauth/callback")
def oauth_callback():
    flow = make_flow()
    flow.fetch_token(authorization_response=request.url)
    save_token(flow.credentials)
    return redirect("/?oauth=ok")

@app.route("/api/oauth/status")
def oauth_status():
    return jsonify({"ok": True, "connected": bool(load_token())})

# ================== Gemini ==================
def gemini_generate(api_key: str, prompt: str) -> str:
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {"key": api_key}

    body = {
        "contents": [{
            "parts": [{"text": prompt}]
        }]
    }

    r = requests.post(url, params=params, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]

# ================== Pexels ==================
def pexels_image(key: str, query: str) -> str:
    if not key:
        return ""
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": key},
        params={"query": query, "per_page": 1},
        timeout=15
    )
    if r.status_code != 200:
        return ""
    photos = r.json().get("photos", [])
    if not photos:
        return ""
    return photos[0]["src"]["large"]

# ================== API ==================
@app.route("/api/generate", methods=["POST"])
def api_generate():
    d = request.json or {}
    topic = d.get("topic")
    category = d.get("category", "정보")
    gemini_key = d.get("gemini_key")
    pexels_key = d.get("pexels_key")

    if not topic or not gemini_key:
        return jsonify({"ok": False, "error": "topic / gemini_key required"}), 400

    prompt = f"""
너는 수익형 블로그 전문 작가다.

주제: {topic}
카테고리: {category}

조건:
- HTML 형식
- H2 소제목 8개 이상
- 각 소제목 600자 이상
- 표 1개
- 체크리스트/주의사항/FAQ 포함
- 마지막에 행동 유도 문구
"""

    html = gemini_generate(gemini_key, prompt)
    img = pexels_image(pexels_key, f"{topic} {category}")

    if img:
        html = f'<img src="{img}" style="max-width:100%"><hr>' + html

    return jsonify({
        "ok": True,
        "html": html,
        "image_url": img,
        "generated_at": now()
    })

@app.route("/api/blogger/blogs")
def blogger_blogs():
    svc = get_blogger()
    if not svc:
        return jsonify({"ok": False}), 401
    res = svc.blogs().listByUser(userId="self").execute()
    items = [
        {"id": b["id"], "name": b["name"], "url": b["url"]}
        for b in res.get("items", [])
    ]
    return jsonify({"ok": True, "items": items})

@app.route("/api/blogger/post", methods=["POST"])
def blogger_post():
    svc = get_blogger()
    if not svc:
        return jsonify({"ok": False}), 401

    d = request.json
    res = svc.posts().insert(
        blogId=d["blog_id"],
        body={"title": d["title"], "content": d["html"]},
        isDraft=False
    ).execute()

    return jsonify({"ok": True, "url": res.get("url")})

# ================== 예약 ==================
@app.route("/api/tasks/add", methods=["POST"])
def task_add():
    tasks = load_json(TASK_FILE, [])
    d = request.json
    d["id"] = int(time.time() * 1000)
    d["status"] = "pending"
    tasks.append(d)
    save_json(TASK_FILE, tasks)
    return jsonify({"ok": True})

@app.route("/api/tasks/list")
def task_list():
    return jsonify({"ok": True, "items": load_json(TASK_FILE, [])})

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now()})

@app.route("/__routes")
def routes():
    return jsonify([str(r) for r in app.url_map.iter_rules()])

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings():
    return send_from_directory(".", "settings.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
