from flask import Flask, request, jsonify, send_from_directory, redirect, session, url_for
from flask_cors import CORS
import os, json
from datetime import datetime
import requests

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

app = Flask(__name__)
(__name__, static_folder=".", static_url_path="")
CORS(app)

# 세션 쿠키용 (필수)
app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_change_me")

PUBLISH_FILE = "publish_queue.json"
TOKEN_FILE = "google_token.json"  # ⚠️ Render 무료는 재배포/재시작 시 파일이 날아갈 수 있음

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

SCOPES = [
    "https://www.googleapis.com/auth/blogger"
]

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

def load_token() -> Credentials | None:
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
    # googleapiclient이 필요 시 refresh는 내부적으로 처리할 수 있지만,
    # refresh_token이 없으면 만료 시 다시 로그인 필요
    return build("blogger", "v3", credentials=creds)

# ---------- OAuth ----------
def make_flow():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAUTH_REDIRECT_URI):
        raise RuntimeError("OAuth env vars missing (GOOGLE_CLIENT_ID/SECRET, OAUTH_REDIRECT_URI)")
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
    state = session.get("oauth_state")
    flow.fetch_token(authorization_response=request.url)

    creds = flow.credentials
    save_token(creds)

    return redirect("/?oauth=ok")

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

# ---------- Prompt ----------
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

# ---------- Queue ----------
def load_queue():
    if not os.path.exists(PUBLISH_FILE):
        return []
    try:
        with open(PUBLISH_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []

def save_queue(items):
    with open(PUBLISH_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

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

# ---------- Blogger: list blogs ----------
@app.route("/api/blogger/blogs", methods=["GET"])
def api_blogger_blogs():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected. Visit /oauth/start"}), 401

    # 내 블로그 목록 가져오기
    res = svc.blogs().listByUser(userId="self").execute()
    items = res.get("items", [])
    out = [{"id": b.get("id"), "name": b.get("name"), "url": b.get("url")} for b in items]
    return jsonify({"ok": True, "count": len(out), "items": out})

# ---------- Blogger: publish post ----------
@app.route("/api/blogger/publish", methods=["POST"])
def api_blogger_publish():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected. Visit /oauth/start"}), 401

    payload = request.get_json(silent=True) or {}
    blog_id = (payload.get("blog_id") or "").strip()
    title = (payload.get("title") or "").strip()
    html = (payload.get("html") or "").strip()

    if not blog_id:
        return jsonify({"ok": False, "error": "blog_id is required"}), 400
    if not title or not html:
        return jsonify({"ok": False, "error": "title/html is required"}), 400

    post_body = {
        "kind": "blogger#post",
        "title": title,
        "content": html
    }

    created = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
    return jsonify({"ok": True, "id": created.get("id"), "url": created.get("url")})

# ---------- 기존 publish queue ----------
@app.route("/api/publish/list", methods=["GET"])
def api_publish_list():
    q = load_queue()
    return jsonify({"ok": True, "count": len(q), "items": q})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)


