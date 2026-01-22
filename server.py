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
CORS(app)

app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_change_me")

TOKEN_FILE = "google_token.json"   # Render 무료는 재시작 시 날아갈 수 있음(임시)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

# Gemini
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")  # 필수
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")  # 또는 gemini-1.5-pro

SCOPES = ["https://www.googleapis.com/auth/blogger"]

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ---------- Static Pages ----------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings():
    return send_from_directory(".", "settings.html")

@app.route("/health")
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


# ---------- OAuth ----------
def make_flow():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAUTH_REDIRECT_URI):
        raise RuntimeError(
            "OAuth env vars missing. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OAUTH_REDIRECT_URI in Render Environment."
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


# ---------- Gemini (Generate HTML) ----------
def gemini_generate_html(prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing")

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192
        }
    }

    r = requests.post(endpoint, headers=headers, params=params, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini API error: {r.status_code} {r.text}")

    data = r.json()
    # 안전하게 텍스트 합치기
    parts = []
    for cand in data.get("candidates", []):
        c = cand.get("content", {})
        for p in c.get("parts", []):
            t = p.get("text")
            if t:
                parts.append(t)
    return "\n".join(parts).strip()


# ---------- Prompt ----------
def make_body_prompt(topic: str, category: str) -> str:
    return f"""너는 '수익형 정보 블로그' 전문 작가다.
아래 조건으로 '{topic}' 글을 한국어로 작성해줘.

[조건]
- 카테고리: {category}
- 출력: 블로그에 바로 붙여넣기 좋은 'HTML'
- SEO: 제목/소제목에 핵심키워드 자연스럽게 포함
- 구성:
  - 도입 400~700자 (문제제기+이득)
  - H2 소제목 8개
  - 각 H2 아래 500~900자
  - 표 1개 포함 (<table>)
  - 체크/주의/팁 박스 3개 (✅💡⚠️) <div>로
  - 마지막: 요약 3~5줄 + FAQ 5개 + 행동유도(댓글/구독/다음 글)
- 금지: 과장/허위, 의료/법률 확정 표현(“반드시”, “100%”) 금지

[출력 규칙]
- HTML만 출력 (설명 금지)
""".strip()

def make_image_prompt(topic: str, category: str) -> str:
    return f'{category} 관련 블로그 썸네일, 주제 "{topic}", 텍스트 없음, 깔끔한 미니멀, 고해상도, 16:9'


# ---------- API generate ----------
@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}
    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"
    img_provider = (payload.get("img_provider") or "").strip() or "pexels"
    pexels_key = (payload.get("pexels_key") or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    body_prompt = make_body_prompt(topic, category)
    image_prompt = make_image_prompt(topic, category)

    # 1) 글 생성 (Gemini)
    try:
        html = gemini_generate_html(body_prompt)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # 2) 이미지 (Pexels)
    image_url = ""
    if img_provider == "pexels":
        q = f"{topic} {category}".strip()
        image_url = pexels_search_image_url(pexels_key, q) or pexels_search_image_url(pexels_key, topic)

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
        return jsonify({"ok": False, "error": "OAuth required. Visit /oauth/start"}), 401

    res = svc.blogs().listByUser(userId="self").execute()
    items = res.get("items", [])
    out = [{"id": b.get("id"), "name": b.get("name"), "url": b.get("url")} for b in items]
    return jsonify({"ok": True, "count": len(out), "items": out})


# ---------- Blogger: post ----------
@app.route("/api/blogger/post", methods=["POST"])
def api_blogger_post():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth required. Visit /oauth/start"}), 401

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
