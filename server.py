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

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app, supports_credentials=True)

app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_change_me")

TOKEN_FILE = os.environ.get("TOKEN_FILE", "google_token.json")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

SCOPES = ["https://www.googleapis.com/auth/blogger"]

# Gemini
GEMINI_API_KEY_ENV = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")

# ---------- Utils ----------
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def jload(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def jsave(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ---------- Static Pages ----------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings():
    return send_from_directory(".", "settings.html")

@app.route("/api/health")
def health():
    return jsonify({"ok": True, "time": now_str()})

# ---------- Token Save/Load ----------
def save_token(creds: Credentials):
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    jsave(TOKEN_FILE, data)

def load_token() -> Optional[Credentials]:
    data = jload(TOKEN_FILE, None)
    if not data:
        return None
    try:
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

# ---------- OAuth ----------
def make_flow():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAUTH_REDIRECT_URI):
        raise RuntimeError(
            "OAuth env vars missing. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OAUTH_REDIRECT_URI"
        )

    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI
    )
    return flow

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

    # state 검증(강제는 아님). state가 없거나 다르면 경고만 하고 진행
    saved_state = session.get("oauth_state")
    got_state = request.args.get("state")
    if saved_state and got_state and saved_state != got_state:
        # 그래도 진행은 하되, 문제 가능성 로그
        print("WARN: oauth state mismatch")

    try:
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

# ---------- Pexels ----------
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
        photos = data.get("photos", [])
        if not photos:
            return ""
        src = photos[0].get("src", {})
        return src.get("large2x") or src.get("large") or src.get("original") or ""
    except Exception:
        return ""

# ---------- Gemini (Text) ----------
def gemini_generate_html(gemini_key: str, prompt: str) -> str:
    key = gemini_key or GEMINI_API_KEY_ENV
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing")

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    params = {"key": key}

    payload = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192
        }
    }

    r = requests.post(endpoint, params=params, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini API error {r.status_code}: {r.text[:300]}")
    data = r.json()

    # 안전하게 텍스트 합치기
    text = ""
    for cand in (data.get("candidates") or []):
        content = cand.get("content") or {}
        parts = content.get("parts") or []
        for p in parts:
            if "text" in p:
                text += p["text"]
    return text.strip()

# ---------- Prompt Builder ----------
def build_money_prompt(topic: str, category: str) -> str:
    # 수익형/애드센스용 구조(너무 과하게 길게 안 함. 필요하면 설정에서 확장 가능)
    return f"""
너는 한국어 수익형 정보 블로그의 전문 작가다.
아래 주제로 "블로그에 바로 붙여넣어 발행 가능한 HTML"을 작성하라.

[주제] {topic}
[카테고리] {category}

필수 조건:
- HTML만 출력(설명 금지)
- 제목은 본문에 <h1>로 1회 포함
- 소제목은 <h2> 8개 (각 500~900자)
- 중간에 <table> 1개 (비교/체크리스트 형식)
- ✅💡⚠️ 아이콘이 들어간 박스(예: <div>로 스타일) 3개 이상
- 마지막에 요약(3~5줄) + FAQ 5개 + 행동유도(댓글/구독/다른글 이동 유도)

SEO:
- 핵심키워드 자연 반복(과다 금지)
- 문단 짧게, 리스트 적절히 사용

주의:
- 과장/허위 수치 금지
- 의료/법률은 "일반 정보"임을 한 줄 고지
""".strip()

def build_image_prompt(topic: str, category: str) -> str:
    return f'{category} 관련 블로그 썸네일, 주제 "{topic}", 텍스트 없음, 깔끔한 미니멀, 고해상도, 16:9'

# ---------- API: generate ----------
@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}
    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "").strip() or "돈되는정보"
    img_provider = (payload.get("img_provider") or "").strip() or "pexels"
    pexels_key = (payload.get("pexels_key") or "").strip()
    gemini_key = (payload.get("gemini_key") or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    # 본문 생성
    body_prompt = build_money_prompt(topic, category)
    try:
        html = gemini_generate_html(gemini_key, body_prompt)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # 이미지
    image_prompt = build_image_prompt(topic, category)
    image_url = ""

    if img_provider == "pexels":
        q = f"{topic} {category}".strip()
        image_url = pexels_search_image_url(pexels_key, q) or pexels_search_image_url(pexels_key, topic)
    elif img_provider == "gemini":
        # ✅ 텍스트-프롬프트는 주되, URL은 비워둠(이미지 생성 API는 별도 구성 필요)
        image_url = ""

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

# ---------- Blogger: list blogs ----------
@app.route("/api/blogger/blogs", methods=["GET"])
def api_blogger_blogs():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected. Visit /oauth/start"}), 401

    res = svc.blogs().listByUser(userId="self").execute()
    items = res.get("items", [])
    out = [{"id": b.get("id"), "name": b.get("name"), "url": b.get("url")} for b in items]
    return jsonify({"ok": True, "count": len(out), "items": out})

# ---------- Blogger: post now ----------
@app.route("/api/blogger/post", methods=["POST"])
def api_blogger_post():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected. Visit /oauth/start"}), 401

    payload = request.get_json(silent=True) or {}
    blog_id = str(payload.get("blog_id", "")).strip()
    title = str(payload.get("title", "")).strip()
    html = str(payload.get("html", "")).strip()
    labels = payload.get("labels") or []

    if not blog_id:
        return jsonify({"ok": False, "error": "blog_id missing"}), 400
    if not title:
        return jsonify({"ok": False, "error": "title missing"}), 400
    if not html:
        return jsonify({"ok": False, "error": "html missing"}), 400

    try:
        post_body = {"kind": "blogger#post", "title": title, "content": html}
        if isinstance(labels, list) and labels:
            post_body["labels"] = [str(x) for x in labels[:10]]

        res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
        return jsonify({"ok": True, "id": res.get("id"), "url": res.get("url")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
