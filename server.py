from flask import Flask, request, jsonify, send_from_directory, redirect, session
from flask_cors import CORS
import os, json, time, threading
from datetime import datetime
import requests
from typing import Optional, List, Dict, Any

# Google OAuth / Blogger
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

app.secret_key = os.environ.get("SESSION_SECRET", "dev_secret_change_me")

TOKEN_FILE = "google_token.json"         # Render free는 재시작시 날아갈 수 있음(임시)
QUEUE_FILE = "publish_queue.json"        # 예약/요청 저장
SCOPES = ["https://www.googleapis.com/auth/blogger"]

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "")

# Gemini
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- Static Pages ----------------
@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings_page():
    return send_from_directory(".", "settings.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})


# ---------------- File Queue ----------------
def load_queue() -> List[Dict[str, Any]]:
    if not os.path.exists(QUEUE_FILE):
        return []
    try:
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or []
        return data if isinstance(data, list) else []
    except Exception:
        return []

def save_queue(items: List[Dict[str, Any]]):
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

def add_queue(item: Dict[str, Any]):
    q = load_queue()
    q.append(item)
    save_queue(q)


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


# ---------------- OAuth ----------------
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

    return Flow.from_client_config(
        client_config=client_config,
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
    flow = make_flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    save_token(creds)
    return redirect("/?oauth=ok")

@app.route("/api/oauth/status")
def oauth_status():
    return jsonify({"ok": True, "connected": bool(load_token())})


# ---------------- Pexels ----------------
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


# ---------------- Gemini (text) ----------------
def gemini_generate_text(api_key: str, model: str, prompt: str) -> str:
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY missing")

    url = f"{GEMINI_API_BASE}/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192,
        }
    }
    r = requests.post(url, json=payload, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Gemini error {r.status_code}: {r.text[:400]}")
    data = r.json()

    # candidates[0].content.parts[].text
    cands = data.get("candidates", [])
    if not cands:
        return ""
    parts = (cands[0].get("content", {}) or {}).get("parts", []) or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
    return "\n".join([t for t in texts if t]).strip()


# ---------------- Prompts ----------------
def make_money_topic_prompt(n: int = 30) -> str:
    return f"""
너는 애드센스/애드포스트 수익형 블로그 기획자다.
한국어로 "클릭/검색수/광고단가"가 높은 주제 위주로 제목 {n}개만 뽑아줘.

조건:
- 생활비 절약, 세금/연말정산, 보험, 통신비, 대출/금리, 정부지원금, 신용점수, 자격증, IT문제해결(보안/속도), 자동차 비용절감, 건강검진/실비 같은 실용 주제 위주
- 제목은 과장 금지(허위/사기 느낌 X), 하지만 클릭 유도는 가능
- 형식: 한 줄에 제목 1개, 번호 붙이지 말 것
""".strip()

def make_body_prompt(topic: str, category: str) -> str:
    return f"""
너는 수익형 정보블로그 작가다.
아래 조건으로 '{topic}' 글을 한국어로 작성해줘.

- 카테고리: {category}
- 분량: 5,000~8,000자 (너무 길면 품질이 떨어짐)
- H2 소제목 7~9개
- 표 1개 포함(<table>)
- 체크박스/박스 디자인(✅💡⚠️)을 <div>로 2~4개 사용
- 마지막: 요약(3~5줄) + FAQ 5개 + 행동유도 2줄
- 출력은 블로그에 붙여넣기 좋은 "HTML"로 작성

중요:
- 의료/금융은 과장 금지, "개인차/상황별 상이" 고지
- 출처가 필요한 수치/정책은 일반화하지 말고 "확인 필요"로 처리
""".strip()

def make_image_prompt(topic: str, category: str) -> str:
    return f'{category} 블로그 썸네일, 주제 "{topic}", 텍스트 없음, 미니멀, 고해상도, 16:9'


# ---------------- API: profit topics ----------------
@app.route("/api/topics/profit", methods=["POST"])
def api_topics_profit():
    payload = request.get_json(silent=True) or {}
    api_key = (payload.get("gemini_key") or "").strip()
    model = (payload.get("gemini_model") or "gemini-2.0-flash").strip()
    n = int(payload.get("n") or 30)

    # Gemini 키 없으면 간단 폴백
    if not api_key:
        fallback = [
            "연말정산 환급 늘리는 공제 항목 체크리스트",
            "통신비 한 달에 2만원 줄이는 순서",
            "실손보험 청구할 때 자주 놓치는 서류 7가지",
            "신용점수 올리는 가장 쉬운 습관 5가지",
            "전기요금 줄이는 집안 설정 10분 점검표",
        ]
        while len(fallback) < n:
            fallback.append(f"생활비 절약 체크리스트 {len(fallback)+1}")
        return jsonify({"ok": True, "items": fallback[:n], "source": "fallback"})

    try:
        txt = gemini_generate_text(api_key, model, make_money_topic_prompt(n))
        items = [line.strip() for line in txt.splitlines() if line.strip()]
        # 너무 길거나 이상한 줄 제거
        items = [x[:80] for x in items if len(x) >= 6]
        return jsonify({"ok": True, "items": items[:n], "source": "gemini"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------- API: generate post ----------------
@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}
    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "정보").strip()
    gemini_key = (payload.get("gemini_key") or "").strip()
    gemini_model = (payload.get("gemini_model") or "gemini-2.0-flash").strip()

    img_provider = (payload.get("img_provider") or "pexels").strip()  # pexels|gemini(추후)
    pexels_key = (payload.get("pexels_key") or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    body_prompt = make_body_prompt(topic, category)
    image_prompt = make_image_prompt(topic, category)

    # 글 생성(Gemini)
    html = ""
    if gemini_key:
        try:
            html = gemini_generate_text(gemini_key, gemini_model, body_prompt)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Gemini failed: {str(e)}"}), 500
    else:
        # 키 없으면 프롬프트만 반환(테스트)
        html = body_prompt

    # 이미지: Pexels(안정)
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
        "body_prompt": body_prompt,
        "image_prompt": image_prompt,
        "image_provider": img_provider,
        "image_url": image_url
    })


# ---------------- Blogger: list blogs ----------------
@app.route("/api/blogger/blogs", methods=["GET"])
def api_blogger_blogs():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected. Visit /oauth/start"}), 401
    res = svc.blogs().listByUser(userId="self").execute()
    items = res.get("items", [])
    out = [{"id": b.get("id"), "name": b.get("name"), "url": b.get("url")} for b in items]
    return jsonify({"ok": True, "count": len(out), "items": out})


# ---------------- Blogger: post now ----------------
@app.route("/api/blogger/post", methods=["POST"])
def api_blogger_post():
    svc = get_blogger_client()
    if not svc:
        return jsonify({"ok": False, "error": "OAuth not connected. Visit /oauth/start"}), 401

    payload = request.get_json(silent=True) or {}
    blog_id = str(payload.get("blog_id", "")).strip()
    title = str(payload.get("title", "")).strip()
    html = str(payload.get("html", "")).strip()
    is_draft = bool(payload.get("is_draft", False))

    if not blog_id:
        return jsonify({"ok": False, "error": "blog_id missing"}), 400
    if not title:
        return jsonify({"ok": False, "error": "title missing"}), 400
    if not html:
        return jsonify({"ok": False, "error": "html missing"}), 400

    try:
        post_body = {"kind": "blogger#post", "title": title, "content": html}
        res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=is_draft).execute()
        return jsonify({"ok": True, "id": res.get("id"), "url": res.get("url")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------- Publish Queue: add (all platforms) ----------------
@app.route("/api/publish/add", methods=["POST"])
def api_publish_add():
    """
    공통 예약/요청 저장.
    - blog_type: blogspot|naver|tistory
    - blog_id: (blogspot만 필요)
    - blog_url: 선택
    - run_at: "YYYY-MM-DD HH:MM" (없으면 NOW)
    - title/html/image_url...
    """
    payload = request.get_json(force=True)
    blog_type = str(payload.get("blog_type", "")).strip()
    blog_id = str(payload.get("blog_id", "")).strip()
    blog_url = str(payload.get("blog_url", "")).strip()
    title = str(payload.get("title", "")).strip()
    html = str(payload.get("html", "")).strip()

    run_at = str(payload.get("run_at", "NOW")).strip()
    image_url = str(payload.get("image_url", "")).strip()
    category = str(payload.get("category", "정보")).strip()

    if blog_type not in ("blogspot", "naver", "tistory"):
        return jsonify({"ok": False, "error": "blog_type must be blogspot|naver|tistory"}), 400
    if not title or not html:
        return jsonify({"ok": False, "error": "title/html required"}), 400

    item = {
        "id": f"q_{int(time.time()*1000)}",
        "created_at": now_str(),
        "blog_type": blog_type,
        "blog_id": blog_id,
        "blog_url": blog_url,
        "category": category,
        "title": title,
        "html": html,
        "image_url": image_url,
        "run_at": run_at,   # NOW or "YYYY-MM-DD HH:MM"
        "status": "queued",
        "result_url": ""
    }
    add_queue(item)
    return jsonify({"ok": True, "saved": item})


@app.route("/api/publish/list", methods=["GET"])
def api_publish_list():
    q = load_queue()
    return jsonify({"ok": True, "count": len(q), "items": q})


@app.route("/api/publish/delete", methods=["POST"])
def api_publish_delete():
    payload = request.get_json(silent=True) or {}
    qid = str(payload.get("id", "")).strip()
    q = load_queue()
    nq = [x for x in q if x.get("id") != qid]
    save_queue(nq)
    return jsonify({"ok": True, "count": len(nq)})


# ---------------- Worker: run due BLOGSPOT only ----------------
def parse_run_at(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s or s.upper() == "NOW":
        return datetime.now()
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except Exception:
        return None

def worker_loop():
    while True:
        try:
            q = load_queue()
            changed = False
            now = datetime.now()

            for item in q:
                if item.get("status") != "queued":
                    continue

                run_at = parse_run_at(item.get("run_at", "NOW"))
                if not run_at or run_at > now:
                    continue

                # 자동발행은 blogspot만 (naver/tistory는 큐 저장까지만)
                if item.get("blog_type") != "blogspot":
                    item["status"] = "ready_for_manual"
                    changed = True
                    continue

                svc = get_blogger_client()
                if not svc:
                    item["status"] = "error"
                    item["error"] = "OAuth not connected"
                    changed = True
                    continue

                blog_id = str(item.get("blog_id") or "").strip()
                if not blog_id:
                    item["status"] = "error"
                    item["error"] = "blog_id missing for blogspot"
                    changed = True
                    continue

                try:
                    post_body = {"kind": "blogger#post", "title": item["title"], "content": item["html"]}
                    res = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
                    item["status"] = "done"
                    item["result_url"] = res.get("url") or ""
                    changed = True
                except Exception as e:
                    item["status"] = "error"
                    item["error"] = str(e)
                    changed = True

            if changed:
                save_queue(q)

        except Exception:
            pass

        time.sleep(20)

# 백그라운드 워커 시작(서버 실행 중일 때만 동작)
threading.Thread(target=worker_loop, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
