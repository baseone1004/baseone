from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS
import os, json, time, threading
from datetime import datetime, timedelta, timezone
import requests
from typing import Optional, List, Dict

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app, supports_credentials=True)

app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_change_me")

# files (Render free: file can be wiped after restart)
TOKEN_FILE = "google_token.json"
QUEUE_FILE = "publish_queue.json"

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

SCOPES = ["https://www.googleapis.com/auth/blogger"]

KST = timezone(timedelta(hours=9))


# ---------------- Utils ----------------
def now_str():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")

def utc_now():
    return datetime.now(timezone.utc)

def load_json_file(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_file(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_queue() -> List[Dict]:
    return load_json_file(QUEUE_FILE, [])

def save_queue(q: List[Dict]):
    save_json_file(QUEUE_FILE, q)

def iso_to_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z","+00:00"))
    except Exception:
        return None


# ---------------- Static Pages ----------------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings():
    return send_from_directory(".", "settings.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})


# ---------------- Token Save/Load ----------------
def save_token(creds: Credentials):
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    save_json_file(TOKEN_FILE, data)

def load_token() -> Optional[Credentials]:
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        data = load_json_file(TOKEN_FILE, None)
        if not data:
            return None
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


# ---------------- OAuth ----------------
def make_flow():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and OAUTH_REDIRECT_URI):
        raise RuntimeError(
            "OAuth env vars missing. Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OAUTH_REDIRECT_URI."
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
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    save_token(creds)
    return redirect("/?oauth=ok")

@app.route("/api/oauth/status")
def oauth_status():
    return jsonify({"ok": True, "connected": bool(load_token())})


# ---------------- Pexels ----------------
def pexels_search_image_url(query: str) -> str:
    key = PEXELS_API_KEY
    if not key:
        return ""
    url = "https://api.pexels.com/v1/search"
    headers = {"Authorization": key}
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

@app.route("/api/image/pexels", methods=["POST"])
def api_pexels():
    payload = request.get_json(silent=True) or {}
    q = (payload.get("query") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "query required"}), 400
    return jsonify({"ok": True, "image_url": pexels_search_image_url(q)})


# ---------------- Gemini (Text) ----------------
def gemini_generate_html(topic: str, category: str, tone: str = "수익형 정보블로그") -> str:
    """
    Gemini REST API 호출 (SDK 없이 requests로)
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing in environment variables.")

    prompt = f"""
너는 {tone} 전문 작가다.
주제: {topic}
카테고리: {category}

조건:
- 한국어
- 블로그에 붙여넣기 좋은 HTML
- H2 소제목 8개
- 각 소제목 500~900자
- 표 1개 포함(<table>)
- ✅💡⚠️ 박스 2개 이상(div class 사용 가능)
- 마지막: 요약 3~5줄 + FAQ 5개 + 행동유도
- 과장/허위 금지, 일반 정보로 작성
""".strip()

    # 모델은 필요에 따라 바꿔도 됨
    model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192}
    }
    r = requests.post(url, json=body, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini error: {r.status_code} {r.text}")

    data = r.json()
    # 안전하게 텍스트 추출
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        text = ""

    # Gemini가 Markdown 코드블록으로 줄 때 대비
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
    return text


@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}
    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"
    img_provider = (payload.get("img_provider") or "").strip() or "pexels"  # pexels|gemini|none

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    # 1) 본문 HTML (Gemini)
    try:
        html = gemini_generate_html(topic, category)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # 2) 이미지
    image_url = ""
    image_prompt = f'{category} 블로그 썸네일, 주제 "{topic}", 텍스트 없음, 미니멀, 고해상도, 16:9'
    if img_provider == "pexels":
        image_url = pexels_search_image_url(f"{topic} {category}") or pexels_search_image_url(topic)
    elif img_provider == "none":
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


# ---------------- Blogger APIs ----------------
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


# ---------------- Scheduler (Queue) ----------------
@app.route("/api/queue/list", methods=["GET"])
def api_queue_list():
    q = load_queue()
    return jsonify({"ok": True, "count": len(q), "items": q})

def enqueue(item: Dict):
    q = load_queue()
    q.append(item)
    save_queue(q)

def mark_done(item_id: str, status: str, url: str = "", error: str = ""):
    q = load_queue()
    for it in q:
        if it.get("id") == item_id:
            it["status"] = status
            it["done_at"] = now_str()
            if url:
                it["post_url"] = url
            if error:
                it["error"] = error
            break
    save_queue(q)

def publish_due_once() -> Dict:
    """
    예약시간(scheduled_at UTC ISO)이 지난 pending 1개만 발행
    """
    q = load_queue()
    now = utc_now()
    pending = [it for it in q if it.get("status") == "pending"]
    # scheduled_at 없으면 즉시
    def due_time(it):
        dt = iso_to_dt(it.get("scheduled_at") or "")
        return dt or datetime(1970,1,1,tzinfo=timezone.utc)
    pending.sort(key=due_time)

    for it in pending:
        dt = iso_to_dt(it.get("scheduled_at") or "")
        if dt and dt > now:
            continue

        # 발행 시도
        try:
            blog_id = it["blog_id"]
            title = it["title"]
            html = it["html"]
            resp = requests.post(
                request.url_root.rstrip("/") + "/api/blogger/post",
                json={"blog_id": blog_id, "title": title, "html": html},
                timeout=60
            )
            data = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {}
            if resp.status_code != 200 or not data.get("ok"):
                raise RuntimeError(data.get("error") or f"post failed: {resp.status_code}")
            mark_done(it["id"], "done", url=data.get("url",""))
            return {"ok": True, "published": it["id"], "url": data.get("url","")}
        except Exception as e:
            mark_done(it.get("id",""), "error", error=str(e))
            return {"ok": False, "error": str(e), "failed": it.get("id","")}

    return {"ok": True, "message": "no due items"}

@app.route("/api/worker/run_once", methods=["POST"])
def api_worker_run_once():
    return jsonify(publish_due_once())


# ---------------- Distribute Scheduling ----------------
@app.route("/api/schedule/distribute", methods=["POST"])
def api_schedule_distribute():
    """
    입력:
    {
      "blogs": [{"id":"...","name":"...","url":"..."} ...],  # 최대 10
      "titles": ["제목1","제목2"...],                        # 최대 100
      "category":"돈/재테크",
      "per_blog": 10,
      "interval_minutes": 60,
      "start_time": "NOW" or "2026-01-22 21:00"  # KST
      "img_provider": "pexels"|"none"
    }

    규칙:
    - 각 블로그는 같은 start_time에서 시작 (=> 블로그끼리 시간 겹쳐도 OK)
    - 블로그 내부에서는 interval_minutes 만큼만 증가
    """
    payload = request.get_json(silent=True) or {}

    blogs = payload.get("blogs") or []
    titles = payload.get("titles") or []
    category = (payload.get("category") or "돈/재테크").strip()
    per_blog = int(payload.get("per_blog") or 10)
    interval_minutes = int(payload.get("interval_minutes") or 60)
    start_time = str(payload.get("start_time") or "NOW")
    img_provider = (payload.get("img_provider") or "pexels").strip()

    if not blogs or not isinstance(blogs, list):
        return jsonify({"ok": False, "error": "blogs required"}), 400
    if not titles or not isinstance(titles, list):
        return jsonify({"ok": False, "error": "titles required"}), 400

    blogs = blogs[:10]
    titles = titles[: (len(blogs) * per_blog)]

    # start time KST parse
    if start_time == "NOW":
        base_kst = datetime.now(KST) + timedelta(minutes=1)
    else:
        try:
            base_kst = datetime.strptime(start_time, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except Exception:
            return jsonify({"ok": False, "error": "start_time format must be 'YYYY-MM-DD HH:MM' or NOW"}), 400

    created = []
    idx = 0

    for b in blogs:
        blog_id = str(b.get("id") or "").strip()
        if not blog_id:
            continue

        # ✅ 블로그마다 동일한 시작시간(=> 겹쳐도 OK)
        t_kst = base_kst

        for j in range(per_blog):
            if idx >= len(titles):
                break

            title = str(titles[idx]).strip()
            idx += 1
            if not title:
                continue

            # 1) 글 생성 (Gemini)
            try:
                html = gemini_generate_html(title, category)
            except Exception as e:
                return jsonify({"ok": False, "error": f"Gemini generate failed: {str(e)}"}), 500

            # 2) 이미지
            image_url = ""
            if img_provider == "pexels":
                image_url = pexels_search_image_url(f"{title} {category}") or pexels_search_image_url(title)

            # 저장 item
            item_id = f"Q_{int(time.time()*1000)}_{blog_id}_{j}"
            scheduled_utc = t_kst.astimezone(timezone.utc).isoformat()

            item = {
                "id": item_id,
                "status": "pending",
                "created_at": now_str(),
                "scheduled_at": scheduled_utc,
                "blog_type": "blogspot",
                "blog_id": blog_id,
                "blog_name": b.get("name") or "",
                "blog_url": b.get("url") or "",
                "category": category,
                "title": title,
                "html": html,
                "image_url": image_url,
                "img_provider": img_provider
            }
            enqueue(item)
            created.append(item)

            # 블로그 내부 interval 증가
            t_kst = t_kst + timedelta(minutes=interval_minutes)

    return jsonify({"ok": True, "created": len(created), "items": created})


# ---------------- Background Worker ----------------
def worker_loop():
    while True:
        try:
            publish_due_once()
        except Exception:
            pass
        time.sleep(60)

if os.environ.get("ENABLE_WORKER", "1") == "1":
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
