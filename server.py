from flask import Flask, request, jsonify, send_from_directory, redirect
from flask_cors import CORS
import os
import json
from datetime import datetime, timedelta
import requests
import secrets
import urllib.parse

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

PUBLISH_FILE = "publish_queue.json"
TOKEN_FILE = "oauth_tokens.json"
OAUTH_STATE_FILE = "oauth_state.json"

# =========================
# Utils / Storage
# =========================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = json.load(f)
            return v if v is not None else default
    except Exception:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_queue():
    return load_json(PUBLISH_FILE, [])

def save_queue(items):
    save_json(PUBLISH_FILE, items)

def load_tokens():
    return load_json(TOKEN_FILE, {})

def save_tokens(tokens):
    save_json(TOKEN_FILE, tokens)

def save_state(state: str):
    save_json(OAUTH_STATE_FILE, {"state": state, "created_at": now_str()})

def load_state():
    return load_json(OAUTH_STATE_FILE, {})

# =========================
# Pages
# =========================
@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings():
    return send_from_directory(".", "settings.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})

# =========================
# Pexels image
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
# Prompt makers
# =========================
def make_body_prompt(topic: str, category: str) -> str:
    return f"""너는 수익형 정보블로그 작가다.
아래 조건으로 '{topic}' 글을 한국어로 작성해줘.

- 카테고리: {category}
- 분량: 14,000자 이상
- H2 소제목 8~9개
- 각 소제목 아래 700자 이상
- 표 1개 포함(<table>)
- 아이콘/박스 디자인(✅💡⚠️) div로 포함
- 마지막: 요약(3~5줄) + FAQ 5개 + 행동유도

※ 출력은 블로그에 붙여넣기 좋은 HTML로 작성해줘.
""".strip()

def make_image_prompt(topic: str, category: str) -> str:
    return f'{category} 관련 블로그 썸네일, 주제 "{topic}", 텍스트 없음, 깔끔한 미니멀, 고해상도, 16:9'

# =========================
# API: generate
# =========================
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
        "body_prompt": body_prompt,
        "image_prompt": image_prompt,
        "image_provider": img_provider,
        "image_url": image_url
    })

# =========================
# OAuth (Blogger)
# =========================
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()  # ex) https://baseone11.onrender.com/oauth/callback

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Blogger scope (posting)
BLOGGER_SCOPE = "https://www.googleapis.com/auth/blogger"

def env_ready_for_oauth():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI)

@app.route("/oauth/status")
def oauth_status():
    tokens = load_tokens()
    has_refresh = bool(tokens.get("refresh_token"))
    return jsonify({
        "ok": True,
        "env_ready": env_ready_for_oauth(),
        "connected": has_refresh
    })

@app.route("/oauth/start")
def oauth_start():
    if not env_ready_for_oauth():
        return jsonify({
            "ok": False,
            "error": "OAuth env missing. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI in Render."
        }), 400

    state = secrets.token_urlsafe(24)
    save_state(state)

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": BLOGGER_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state
    }
    url = GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)
    return redirect(url)

@app.route("/oauth/callback")
def oauth_callback():
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    err = request.args.get("error", "")

    if err:
        return f"OAuth 실패: {err}", 400

    saved = load_state()
    if not saved or saved.get("state") != state:
        return "OAuth state mismatch. 다시 시도해 주세요.", 400

    if not code:
        return "OAuth code 없음", 400

    data = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code"
    }
    r = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=20)
    if r.status_code != 200:
        return f"토큰 교환 실패: {r.text}", 400

    token = r.json()
    # token: access_token, expires_in, refresh_token(optional), token_type, scope
    tokens = load_tokens()
    tokens["access_token"] = token.get("access_token", "")
    tokens["expires_at"] = (datetime.utcnow() + timedelta(seconds=int(token.get("expires_in", 3600)))).isoformat() + "Z"
    if token.get("refresh_token"):
        tokens["refresh_token"] = token["refresh_token"]
    tokens["scope"] = token.get("scope", "")
    save_tokens(tokens)

    return "✅ OAuth 연결 완료! 이제 BaseOne에서 '즉시발행(실제 업로드)'를 사용할 수 있어요. 이 창은 닫아도 됩니다."

def refresh_access_token_if_needed():
    tokens = load_tokens()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return None, "no refresh_token"

    # always refresh (simple & safe)
    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }
    r = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=20)
    if r.status_code != 200:
        return None, r.text

    j = r.json()
    tokens["access_token"] = j.get("access_token", "")
    tokens["expires_at"] = (datetime.utcnow() + timedelta(seconds=int(j.get("expires_in", 3600)))).isoformat() + "Z"
    save_tokens(tokens)
    return tokens["access_token"], None

# =========================
# Blogger API helpers
# =========================
def blogger_get_blog_id(access_token: str) -> str:
    # Use "blogs/byurl?url=..."
    # Requires actual blog URL, and "auth/blogger" scope
    return ""

def blogger_create_post(access_token: str, blog_id: str, title: str, content_html: str, is_draft: bool = False):
    url = f"https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts/"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=utf-8"}
    payload = {
        "kind": "blogger#post",
        "title": title,
        "content": content_html
    }
    params = {"isDraft": "true" if is_draft else "false"}
    r = requests.post(url, headers=headers, params=params, json=payload, timeout=30)
    return r.status_code, r.text

def blogger_blogid_by_url(access_token: str, blog_url: str):
    url = "https://www.googleapis.com/blogger/v3/blogs/byurl"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"url": blog_url}
    r = requests.get(url, headers=headers, params=params, timeout=20)
    if r.status_code != 200:
        return None, r.text
    j = r.json()
    return j.get("id"), None

# =========================
# API: 실제 업로드 (Blogger)
# =========================
@app.route("/api/blogger/publish", methods=["POST"])
def api_blogger_publish():
    """
    body_json:
    {
      "blog_url": "https://xxx.blogspot.com",
      "title": "...",
      "content_html": "<h1>...</h1>....",
      "is_draft": false
    }
    """
    if not env_ready_for_oauth():
        return jsonify({"ok": False, "error": "OAuth env missing in Render env vars"}), 400

    payload = request.get_json(silent=True) or {}
    blog_url = (payload.get("blog_url") or "").strip()
    title = (payload.get("title") or "").strip()
    content_html = payload.get("content_html") or ""
    is_draft = bool(payload.get("is_draft", False))

    if not blog_url:
        return jsonify({"ok": False, "error": "blog_url is required"}), 400
    if not title:
        return jsonify({"ok": False, "error": "title is required"}), 400
    if not content_html:
        return jsonify({"ok": False, "error": "content_html is required"}), 400

    access_token, err = refresh_access_token_if_needed()
    if err or not access_token:
        return jsonify({"ok": False, "error": f"OAuth not connected or refresh failed: {err}"}), 400

    blog_id, err2 = blogger_blogid_by_url(access_token, blog_url)
    if err2 or not blog_id:
        return jsonify({"ok": False, "error": f"blog_id lookup failed: {err2}"}), 400

    code, text = blogger_create_post(access_token, blog_id, title, content_html, is_draft=is_draft)
    if code not in (200, 201):
        return jsonify({"ok": False, "error": text}), 400

    return jsonify({"ok": True, "message": "✅ 블로그스팟 업로드 완료!", "raw": text})

# =========================
# API: publish queue (existing)
# =========================
@app.route("/api/publish/schedule", methods=["POST"])
def api_publish_schedule():
    payload = request.get_json(silent=True) or {}

    blog_type = (payload.get("blog_type") or "").strip()
    blog_url = (payload.get("blog_url") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"
    topic = (payload.get("topic") or "").strip()
    schedule_times = payload.get("schedule_times") or []

    if not blog_type or not blog_url:
        return jsonify({"ok": False, "error": "blog_type/blog_url is required"}), 400
    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400
    if not isinstance(schedule_times, list) or len(schedule_times) == 0:
        return jsonify({"ok": False, "error": "schedule_times is required"}), 400

    item = {
        "type": "schedule",
        "created_at": now_str(),
        "blog_type": blog_type,
        "blog_url": blog_url,
        "category": category,
        "topic": topic,
        "schedule_times": schedule_times
    }

    q = load_queue()
    q.append(item)
    save_queue(q)

    return jsonify({"ok": True, "message": "예약 발행 요청 저장 완료 ✅", "saved": item})

@app.route("/api/publish/now", methods=["POST"])
def api_publish_now():
    payload = request.get_json(silent=True) or {}

    blog_type = (payload.get("blog_type") or "").strip()
    blog_url = (payload.get("blog_url") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"
    topic = (payload.get("topic") or "").strip()
    start_time = (payload.get("start_time") or "").strip() or "09:00"
    interval_hours = str(payload.get("interval_hours") or "1").strip()

    if not blog_type or not blog_url:
        return jsonify({"ok": False, "error": "blog_type/blog_url is required"}), 400
    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    item = {
        "type": "now_interval",
        "created_at": now_str(),
        "blog_type": blog_type,
        "blog_url": blog_url,
        "category": category,
        "topic": topic,
        "start_time": start_time,
        "interval_hours": interval_hours
    }

    q = load_queue()
    q.append(item)
    save_queue(q)

    return jsonify({"ok": True, "message": "즉시 발행(간격) 요청 저장 완료 ✅", "saved": item})

@app.route("/api/publish/list", methods=["GET"])
def api_publish_list():
    q = load_queue()
    return jsonify({"ok": True, "count": len(q), "items": q})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
