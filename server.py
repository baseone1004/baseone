from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS
import os, json
from datetime import datetime
import requests
from typing import Optional

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

# ---------------- 기본 설정 ----------------
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)
app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_change_me")

PUBLISH_FILE = "publish_queue.json"
TOKEN_FILE = "google_token.json"

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

SCOPES = ["https://www.googleapis.com/auth/blogger"]

# ---------------- 공통 ----------------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def load_queue():
    if not os.path.exists(PUBLISH_FILE):
        return []
    try:
        with open(PUBLISH_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except:
        return []

def save_queue(items):
    with open(PUBLISH_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

# ---------------- 정적 페이지 ----------------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings():
    return send_from_directory(".", "settings.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})

# ---------------- OAuth 토큰 ----------------
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
    except:
        return None

def get_blogger_client():
    creds = load_token()
    if not creds:
        return None
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            save_token(creds)
    except:
        return None
    return build("blogger", "v3", credentials=creds)

# ---------------- OAuth ----------------
def make_flow():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAUTH_REDIRECT_URI):
        raise RuntimeError("OAuth 환경변수 없음")
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
@app.route("/oauth/start")
def oauth_start():
    try:
        flow = make_flow()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

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

# ---------------- Pexels ----------------
def pexels_search_image_url(pexels_key, query):
    if not pexels_key:
        return ""
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": pexels_key},
            params={"query": query, "per_page": 1}
        )
        data = r.json()
        photos = data.get("photos", [])
        if not photos:
            return ""
        return photos[0]["src"].get("large") or ""
    except:
        return ""

# ---------------- Prompt ----------------
def make_body_prompt(topic, category):
    return f"""너는 수익형 정보블로그 작가다.
'{topic}' 글을 한국어 HTML로 작성해줘.

- 카테고리: {category}
- 분량: 10,000자 이상
- H2 소제목 6개 이상
- 마지막 요약 + FAQ 포함
"""

def make_image_prompt(topic, category):
    return f'{category} 블로그 썸네일, "{topic}", 텍스트 없음, 16:9'

# ---------------- 글 생성 ----------------
@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json() or {}
    topic = payload.get("topic","").strip()
    category = payload.get("category","정보").strip()
    pexels_key = payload.get("pexels_key","").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic required"}), 400

    body_prompt = make_body_prompt(topic, category)
    image_prompt = make_image_prompt(topic, category)
    image_url = pexels_search_image_url(pexels_key, topic)

    return jsonify({
        "ok": True,
        "topic": topic,
        "category": category,
        "body_prompt": body_prompt,
        "image_prompt": image_prompt,
        "image_url": image_url,
        "generated_at": now_str()
    })

# ---------------- Blogger 목록 ----------------
@app.route("/api/blogger/blogs")
def api_blogger_blogs():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth 필요"}), 401
    res = svc.blogs().listByUser(userId="self").execute()
    items = res.get("items", [])
    return jsonify({
        "ok": True,
        "items": [{"id":b["id"],"name":b["name"],"url":b["url"]} for b in items]
    })

# ---------------- Blogger 글 발행 ----------------
@app.route("/api/blogger/post", methods=["POST"])
def api_blogger_post():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth 필요"}), 401

    payload = request.get_json() or {}
    blog_id = payload.get("blog_id","")
    title = payload.get("title","")
    html = payload.get("html","")

    if not blog_id or not title or not html:
        return jsonify({"ok": False, "error": "blog_id, title, html 필요"}), 400

    post_body = {"kind":"blogger#post","title":title,"content":html}
    res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
    return jsonify({"ok": True, "url": res.get("url")})

# ---------------- 발행 저장 ----------------
@app.route("/api/publish/now", methods=["POST"])
def api_publish_now():
    payload = request.get_json() or {}
    item = {
        "created_at": now_str(),
        "blog_type": payload.get("blog_type",""),
        "blog_url": payload.get("blog_url",""),
        "category": payload.get("category","정보"),
        "topic": payload.get("topic",""),
    }
    if not item["topic"]:
        return jsonify({"ok": False, "error": "topic missing"}), 400

    q = load_queue()
    q.append(item)
    save_queue(q)
    return jsonify({"ok": True, "message": "저장 완료", "saved": item})

@app.route("/api/publish/list")
def api_publish_list():
    q = load_queue()
    return jsonify({"ok": True, "count": len(q), "items": q})

# ---------------- 실행 ----------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

