from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

import os, json, re, threading, time
from datetime import datetime, timezone
import requests
from typing import Optional

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build


# =========================
# App
# =========================
app = Flask(__name__, static_folder=".", static_url_path="")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

CORS(app, resources={r"/*": {"origins": "*"}})

app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_change_me")

# Files (Render free는 디스크가 날아갈 수 있음: 임시 저장용)
TOKEN_FILE = "google_token.json"
TASK_FILE = "tasks.json"

# OAuth env
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

SCOPES = ["https://www.googleapis.com/auth/blogger"]

# Optional server-side keys (프론트에서 보내도 되지만, 서버 env가 더 안전)
ENV_GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
ENV_PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
# OAuth Token Save/Load
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
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            save_token(creds)
    except Exception:
        return None
    return build("blogger", "v3", credentials=creds)


# =========================
# OAuth Routes
# =========================
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

    return Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )


@app.route("/oauth/start")
def oauth_start():
    try:
        flow = make_flow()
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
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
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


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
# Gemini (text generation)
# =========================
def _strip_code_fences(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def gemini_generate_json(gemini_key: str, model: str, prompt: str) -> dict:
    """
    Calls Gemini generateContent REST endpoint.
    Returns dict parsed from model output (expects JSON).
    """
    if not gemini_key:
        raise RuntimeError("GEMINI_API_KEY missing")

    model = model or "gemini-1.5-flash"
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    params = {"key": gemini_key}

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192,
        },
    }

    r = requests.post(endpoint, params=params, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:500]}")

    j = r.json()
    text = ""
    try:
        text = j["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        raise RuntimeError("Gemini response parse failed")

    raw = _strip_code_fences(text)

    # Try JSON parse
    try:
        return json.loads(raw)
    except Exception:
        # If not JSON, treat as html only
        return {
            "html": raw,
            "pexels_query": "",
            "image_prompt": "",
        }


def make_article_prompt(topic: str, category: str) -> str:
    return f"""
너는 “수익형 정보블로그” 전문 작가다.

주제: {topic}
카테고리: {category}

아래 JSON만 출력해. (설명 금지, 코드블록 금지)
{{
  "html": "블로그에 바로 붙여넣는 완성 HTML",
  "pexels_query": "Pexels 검색에 최적화된 영어 키워드 3~6개(콤마로)",
  "image_prompt": "썸네일 이미지 프롬프트(텍스트 없음, 미니멀, 16:9, 고해상도)"
}}

[HTML 조건]
- 분량: 최소 6,000자 이상 (너무 짧으면 수익형으로 약함)
- H2 소제목 8개
- 소제목마다 500자 이상
- 표 1개 포함 (<table> 사용)
- ✅💡⚠️ 박스형 안내 3개 (<div>로)
- 마지막: 요약(3~5줄) + FAQ 5개 + 행동유도(CTA)
- 과장/허위 금지, 실무적으로 쓸 수 있는 체크리스트 중심
""".strip()


def make_money_topics_prompt(count: int) -> str:
    return f"""
너는 “애드센스/애드포스트 수익형” 주제 기획자다.
한국어로 “돈 되는 정보성 글 제목” {count}개를 만들어라.

조건:
- 클릭을 부르는 구체 제목(숫자/체크리스트/조건/신청/환급/절세/비교/갈아타기 등)
- 중복 금지
- 너무 선정적/허위 금지
- 아래 JSON만 출력:
{{"items":["제목1","제목2", ...]}}
""".strip()


@app.route("/api/topics/money", methods=["POST"])
def api_topics_money():
    payload = request.get_json(silent=True) or {}
    gemini_key = (payload.get("gemini_key") or ENV_GEMINI_KEY or "").strip()
    model = (payload.get("gemini_model") or "gemini-1.5-flash").strip()
    count = int(payload.get("count") or 30)
    count = max(5, min(60, count))

    try:
        out = gemini_generate_json(gemini_key, model, make_money_topics_prompt(count))
        items = out.get("items") or []
        items = [str(x).strip() for x in items if str(x).strip()]
        return jsonify({"ok": True, "count": len(items), "items": items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}
    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"

    img_provider = (payload.get("img_provider") or "pexels").strip().lower()

    # keys: prefer request payload, fallback env
    gemini_key = (payload.get("gemini_key") or ENV_GEMINI_KEY or "").strip()
    gemini_model = (payload.get("gemini_model") or "gemini-1.5-flash").strip()

    pexels_key = (payload.get("pexels_key") or ENV_PEXELS_KEY or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    try:
        data = gemini_generate_json(gemini_key, gemini_model, make_article_prompt(topic, category))
        html = (data.get("html") or "").strip()
        pexels_query = (data.get("pexels_query") or "").strip()
        image_prompt = (data.get("image_prompt") or "").strip()

        # Pexels 이미지 URL
        image_url = ""
        if img_provider == "pexels":
            q = pexels_query or f"{topic} {category}"
            image_url = pexels_search_image_url(pexels_key, q) or pexels_search_image_url(pexels_key, topic)

        # gemini 이미지 생성은 계정/기능/모델에 따라 불안정해서 여기서는 "프롬프트 제공" 중심
        return jsonify({
            "ok": True,
            "generated_at": now_str(),
            "topic": topic,
            "category": category,
            "html": html,
            "image_provider": img_provider,
            "image_prompt": image_prompt,
            "pexels_query": pexels_query,
            "image_url": image_url,
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================
# Simple Task Queue (예약발행)
# =========================
def _load_tasks():
    if not os.path.exists(TASK_FILE):
        return []
    try:
        with open(TASK_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or []
    except Exception:
        return []


def _save_tasks(tasks):
    with open(TASK_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def _next_task_id(tasks):
    mx = 0
    for t in tasks:
        try:
            mx = max(mx, int(t.get("id", 0)))
        except Exception:
            pass
    return mx + 1


@app.route("/api/tasks/add", methods=["POST"])
def api_tasks_add():
    payload = request.get_json(silent=True) or {}
    platform = str(payload.get("platform", "")).strip()
    blog_id = str(payload.get("blog_id", "")).strip()
    title = str(payload.get("title", "")).strip()
    html = str(payload.get("html", "")).strip()
    run_at = str(payload.get("run_at", "")).strip()

    if platform != "blogspot":
        return jsonify({"ok": False, "error": "only platform=blogspot supported"}), 400
    if not blog_id:
        return jsonify({"ok": False, "error": "blog_id missing"}), 400
    if not title:
        return jsonify({"ok": False, "error": "title missing"}), 400
    if not html:
        return jsonify({"ok": False, "error": "html missing"}), 400
    if not run_at:
        return jsonify({"ok
