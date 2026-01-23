from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

import os, json
from datetime import datetime
import requests
from typing import Optional

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# =========================
# App
# =========================
app = Flask(__name__, static_folder=".", static_url_path="")
app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret")

# ✅ Render + HTTPS 필수
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Session secret (Render ENV: SESSION_SECRET)
app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_change_me")

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# =========================
# Env
# =========================
SCOPES = ["https://www.googleapis.com/auth/blogger"]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

# ✅ Render에서 토큰 파일 날아가는 문제 대비: Persistent Disk 쓰면 /var/data 추천
TOKEN_FILE = os.environ.get("TOKEN_FILE", "google_token.json")

# (선택) 서버 환경변수로도 Gemini/Pexels 기본값 설정 가능
DEFAULT_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
DEFAULT_PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")


# =========================
# Static Pages
# =========================
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


# =========================
# Token Save/Load
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

    # ✅ 만료 시 refresh
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            save_token(creds)
    except Exception:
        return None

    return build("blogger", "v3", credentials=creds)


# =========================
# OAuth
# =========================
def make_flow():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAUTH_REDIRECT_URI):
        raise RuntimeError(
            "OAuth env vars missing. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OAUTH_REDIRECT_URI in Render Env."
        )

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
    return jsonify({"ok": True, "connected": bool(load_token())})


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
# Gemini (REST) - HTML 글 생성
# =========================
def gemini_generate_html(api_key: str, model: str, topic: str, category: str) -> str:
    """
    Google Generative Language API (Gemini) REST 호출로 HTML 생성.
    """
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY missing")

    # 모델 기본값
    model = (model or "gemini-1.5-flash").strip()

    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    params = {"key": api_key}

    prompt = f"""
너는 수익형 정보 블로그 작가다.
아래 조건으로 '{topic}' 글을 한국어로 작성해줘.

- 카테고리: {category}
- 분량: 가능하면 길게(최소 5,000자 이상 권장)
- H2 소제목 8~9개
- 각 소제목 아래 400자 이상
- 표 1개 포함(<table>)
- 아이콘/박스 디자인(✅💡⚠️) div로 포함
- 마지막: 요약(3~5줄) + FAQ 5개 + 행동유도

출력 규칙:
- 출력은 블로그에 붙여넣기 좋은 HTML만 출력 (설명/머리말 금지)
- <h1>제목</h1> 포함
""".strip()

    body = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192
        }
    }

    r = requests.post(endpoint, params=params, json=body, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini error {r.status_code}: {r.text}")

    j = r.json()
    try:
        text = j["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        raise RuntimeError(f"Gemini response parse failed: {j}")

    return text.strip()


def make_image_prompt(topic: str, category: str) -> str:
    # 실제 이미지 생성은 'pexels'로 URL을 가져오고,
    # gemini 선택 시에는 프롬프트만 제공(이미지 모델은 별도 연동이 필요)
    return f'{category} 블로그 썸네일, 주제 "{topic}", 텍스트 없음, 깔끔한 미니멀, 고해상도, 16:9'


# =========================
# API: generate (Gemini 글 + Pexels 이미지 URL)
# =========================
@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}
    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"

    # ✅ 프론트에서 보내거나, 서버 ENV 기본값 사용
    gemini_key = (payload.get("gemini_key") or DEFAULT_GEMINI_KEY or "").strip()
    gemini_model = (payload.get("gemini_model") or "gemini-1.5-flash").strip()

    img_provider = (payload.get("img_provider") or "pexels").strip()
    pexels_key = (payload.get("pexels_key") or DEFAULT_PEXELS_KEY or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    # 1) Gemini로 HTML 생성
    try:
        html = gemini_generate_html(gemini_key, gemini_model, topic, category)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # 2) 이미지 (pexels면 URL까지)
    image_prompt = make_image_prompt(topic, category)
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
        "image_provider": img_provider,
        "image_prompt": image_prompt,
        "image_url": image_url
    })


# =========================
# Blogger APIs
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
    except HttpError as e:
        try:
            detail = e.content.decode("utf-8") if hasattr(e, "content") and e.content else str(e)
        except Exception:
            detail = str(e)
        return jsonify({"ok": False, "error": detail}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================
# Run
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

