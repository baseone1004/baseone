# ===============================
# BaseOne Backend - server.py
# ===============================

from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS
import os, json
from datetime import datetime
from typing import Optional
import requests

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

# -------------------------------------------------
# App basic
# -------------------------------------------------
app = Flask(
    __name__,
    static_folder=".",      # index.html, settings.html 위치
    static_url_path=""
)
CORS(app)

app.secret_key = os.environ.get("SESSION_SECRET", "baseone-dev-secret")

# -------------------------------------------------
# Env
# -------------------------------------------------
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

SCOPES = ["https://www.googleapis.com/auth/blogger"]
TOKEN_FILE = "google_token.json"

# -------------------------------------------------
# Utils
# -------------------------------------------------
def now_utc():
    return datetime.utcnow().isoformat() + "Z"

# -------------------------------------------------
# Static pages
# -------------------------------------------------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings_page():
    path = os.path.join(os.getcwd(), "settings.html")
    if not os.path.exists(path):
        return jsonify({
            "ok": False,
            "error": "settings.html not found in deploy root",
            "hint": "settings.html must be in the same folder as server.py and committed to git"
        }), 404
    return send_from_directory(".", "settings.html")


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "baseone-backend",
        "time": now_utc()
    })

@app.route("/__routes")
def routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))

# -------------------------------------------------
# Token save / load
# -------------------------------------------------
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

# -------------------------------------------------
# OAuth
# -------------------------------------------------
def make_flow():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAUTH_REDIRECT_URI):
        raise RuntimeError("OAuth env missing")
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
    try:
        flow = make_flow()
        flow.fetch_token(authorization_response=request.url)
        save_token(flow.credentials)
        return redirect("/?oauth=ok")
    except Exception as e:
        return f"OAuth error: {e}", 500

@app.route("/api/oauth/status")
def oauth_status():
    return jsonify({
        "ok": True,
        "connected": bool(load_token())
    })

# -------------------------------------------------
# Blogger
# -------------------------------------------------
@app.route("/api/blogger/blogs")
def blogger_blogs():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected"}), 401

    res = svc.blogs().listByUser(userId="self").execute()
    items = res.get("items", [])
    blogs = [{
        "id": b.get("id"),
        "name": b.get("name"),
        "url": b.get("url")
    } for b in items]

    return jsonify({"ok": True, "items": blogs})

@app.route("/api/blogger/post", methods=["POST"])
def blogger_post():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected"}), 401

    data = request.get_json() or {}
    blog_id = data.get("blog_id")
    title = data.get("title")
    html = data.get("html")

    if not blog_id or not title or not html:
        return jsonify({"ok": False, "error": "missing fields"}), 400

    try:
        post = svc.posts().insert(
            blogId=blog_id,
            body={
                "title": title,
                "content": html
            },
            isDraft=False
        ).execute()

        return jsonify({
            "ok": True,
            "id": post.get("id"),
            "url": post.get("url")
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# -------------------------------------------------
# Image (Pexels)
# -------------------------------------------------
def pexels_image(pexels_key: str, query: str) -> str:
    if not pexels_key:
        return ""
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": pexels_key},
            params={"query": query, "per_page": 1},
            timeout=10
        )
        if r.status_code != 200:
            return ""
        photos = r.json().get("photos", [])
        if not photos:
            return ""
        return photos[0]["src"].get("large", "")
    except Exception:
        return ""

# -------------------------------------------------
# Generate (실제 글 생성은 다음 단계에서 고도화)
# -------------------------------------------------
@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json() or {}
    topic = data.get("topic", "").strip()
    category = data.get("category", "정보")
    pexels_key = data.get("pexels_key", "")

    if not topic:
        return jsonify({"ok": False, "error": "topic required"}), 400

    # 🔹 임시 HTML (지금은 구조 안정화가 목적)
    html = f"""
    <h1>{topic}</h1>
    <p>이 글은 <b>{category}</b> 주제로 생성되었습니다.</p>
    <p>다음 단계에서 Gemini / GPT 실제 본문 생성이 연결됩니다.</p>
    """

    image_url = pexels_image(pexels_key, topic)

    return jsonify({
        "ok": True,
        "html": html,
        "image_url": image_url,
        "image_prompt": f"{category} 관련 이미지"
    })

# -------------------------------------------------
# Run
# -------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

