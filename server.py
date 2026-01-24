from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS
import os, json, requests
from datetime import datetime
from typing import Optional

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

# ---------------- 기본 설정 ----------------
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

app.secret_key = os.environ.get("SESSION_SECRET", "BaseOne_Secret_Change")

SCOPES = ["https://www.googleapis.com/auth/blogger"]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI")

TOKEN_FILE = "google_token.json"

# ---------------- 유틸 ----------------
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ---------------- Static ----------------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings():
    return send_from_directory(".", "settings.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now()})

@app.route("/__routes")
def routes():
    return jsonify([str(r) for r in app.url_map.iter_rules()])

# ---------------- OAuth ----------------
def save_token(creds: Credentials):
    with open(TOKEN_FILE, "w") as f:
        json.dump({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes
        }, f)

def load_token():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        return Credentials(**json.load(f))

def blogger():
    creds = load_token()
    if not creds:
        return None
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())
        save_token(creds)
    return build("blogger", "v3", credentials=creds)

def oauth_flow():
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
    flow = oauth_flow()
    url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true"
    )
    session["state"] = state
    return redirect(url)

@app.route("/oauth/callback")
def oauth_callback():
    flow = oauth_flow()
    flow.fetch_token(authorization_response=request.url)
    save_token(flow.credentials)
    return redirect("/?oauth=ok")

@app.route("/api/oauth/status")
def oauth_status():
    return jsonify({"ok": True, "connected": bool(load_token())})

# ---------------- Blogger API ----------------
@app.route("/api/blogger/blogs")
def blogger_blogs():
    svc = blogger()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth 필요"}), 401

    res = svc.blogs().listByUser(userId="self").execute()
    items = res.get("items", [])
    return jsonify({
        "ok": True,
        "items": [{"id": b["id"], "name": b["name"], "url": b["url"]} for b in items]
    })

@app.route("/api/blogger/post", methods=["POST"])
def blogger_post():
    svc = blogger()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth 필요"}), 401

    data = request.json
    blog_id = data.get("blog_id")
    title = data.get("title")
    html = data.get("html")

    if not all([blog_id, title, html]):
        return jsonify({"ok": False, "error": "필수값 누락"}), 400

    post = svc.posts().insert(
        blogId=blog_id,
        body={"title": title, "content": html},
        isDraft=False
    ).execute()

    return jsonify({"ok": True, "url": post.get("url")})

# ---------------- Gemini / GPT ----------------
def gemini_generate(api_key, topic):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent?key={api_key}"
    payload = {
        "contents": [{
            "parts": [{"text": f"{topic}에 대한 수익형 블로그 글을 HTML로 작성해줘"}]
        }]
    }
    r = requests.post(url, json=payload, timeout=60)
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]

def pexels_image(key, query):
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": key},
        params={"query": query, "per_page": 1}
    )
    if r.status_code != 200:
        return ""
    photos = r.json().get("photos", [])
    return photos[0]["src"]["large"] if photos else ""

@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.json
    topic = data["topic"]
    gemini_key = data["gemini_key"]
    pexels_key = data.get("pexels_key", "")

    html = gemini_generate(gemini_key, topic)
    img = pexels_image(pexels_key, topic)

    if img:
        html = f'<img src="{img}" style="width:100%"><br>' + html

    return jsonify({"ok": True, "html": html})

# ---------------- run ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
