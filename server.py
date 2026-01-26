# server.py
# BaseOne Backend (Flask) - Paste & Run
#
# ✅ 포함 기능
# - Google Blogger OAuth (블로그 목록/발행)
# - Gemini / OpenAI 글 생성
# - Pexels 이미지 검색
# - "안전한" 키워드 자동수집(외부 트렌드 API 없이 LLM로 롱테일 키워드/주제 생성)
# - 예약발행(Task Queue): SQLite + 백그라운드 워커(주기적으로 pending 실행)
#
# ✅ Render 실행 가이드(권장)
# - START COMMAND: gunicorn server:app
# - ENV (필수/권장):
#   - BASEONE_SECRET_KEY=아무거나_긴값
#   - GOOGLE_CLIENT_SECRETS_JSON=/opt/render/project/src/client_secrets.json  (또는 repo에 client_secrets.json 포함)
#   - GOOGLE_OAUTH_REDIRECT_URL=https://<너의 render 도메인>/oauth/callback
#   - FRONTEND_ORIGIN=https://<너의 프론트 도메인> (없으면 * 허용)
#   - TASK_WORKER=1  (기본 1; 0이면 예약 실행 워커 off)
#   - TASK_POLL_SECONDS=20 (기본 20초)
#
# ✅ OAuth 준비
# - Google Cloud Console에서 OAuth Client 생성(웹 애플리케이션)
# - 승인된 리디렉션 URI에: https://<도메인>/oauth/callback 추가
# - client_secrets.json 형태:
#   {
#     "web": {
#       "client_id": "...",
#       "project_id": "...",
#       "auth_uri": "https://accounts.google.com/o/oauth2/auth",
#       "token_uri": "https://oauth2.googleapis.com/token",
#       "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
#       "client_secret": "...",
#       "redirect_uris": ["https://<도메인>/oauth/callback"]
#     }
#   }
#
# ✅ 주의
# - 토큰은 서버 로컬 파일(token.json)에 저장됨(Render는 배포마다 초기화될 수 있음)
#   => 장기적으로는 DB/볼륨 저장 권장. 일단 "완성본" 목적상 파일 저장으로 간단 구현.

import os
import json
import time
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify, request, redirect
from flask_cors import CORS

# Google OAuth / Blogger
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build


# -----------------------------
# Config
# -----------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "baseone.db")
TOKEN_PATH = os.path.join(APP_DIR, "token.json")  # Google OAuth token cache
SECRET_KEY = os.environ.get("BASEONE_SECRET_KEY", "baseone_dev_secret_change_me")

CLIENT_SECRETS = os.environ.get("GOOGLE_CLIENT_SECRETS_JSON", os.path.join(APP_DIR, "client_secrets.json"))
REDIRECT_URL = os.environ.get("GOOGLE_OAUTH_REDIRECT_URL", "").strip()

FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "*").strip()
TASK_WORKER = os.environ.get("TASK_WORKER", "1").strip() != "0"
TASK_POLL_SECONDS = int(os.environ.get("TASK_POLL_SECONDS", "20"))

# OAuth scopes for Blogger
SCOPES = [
    "https://www.googleapis.com/auth/blogger",
]

# -----------------------------
# Flask app
# -----------------------------
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
from datetime import datetime

app = Flask(
    __name__,
    static_folder=".",      # ← index.html 위치
    static_url_path=""      # ← 루트(/)로 접근
)
CORS(app)


# CORS (프론트가 따로 있어도 호출 가능)
if FRONTEND_ORIGIN == "*":
    CORS(app, resources={r"/api/*": {"origins": "*"}})
else:
    CORS(app, resources={r"/api/*": {"origins": [FRONTEND_ORIGIN]}})


# -----------------------------
# DB helpers
# -----------------------------
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            blog_id TEXT,
            blog_url TEXT,
            title TEXT NOT NULL,
            html TEXT NOT NULL,
            run_at TEXT NOT NULL,         -- ISO8601 UTC
            status TEXT NOT NULL,         -- pending|running|ok|err|canceled
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            result_url TEXT,
            error TEXT
        );
        """
    )
    conn.commit()
    conn.close()


# -----------------------------
# Utilities
# -----------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(dt_str: str) -> datetime:
    # supports "Z"
    s = (dt_str or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def ok(data: Dict[str, Any] = None, **kwargs):
    payload = {"ok": True}
    if data:
        payload.update(data)
    payload.update(kwargs)
    return jsonify(payload)


def err(message: str, status: int = 400, **kwargs):
    payload = {"ok": False, "error": message}
    payload.update(kwargs)
    return jsonify(payload), status


def safe_json_loads(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except Exception:
        return None


def require_redirect_url() -> Optional[Tuple[Any, int]]:
    if not REDIRECT_URL:
        return err(
            "GOOGLE_OAUTH_REDIRECT_URL 환경변수가 비어있습니다. 예) https://<도메인>/oauth/callback",
            status=500,
        )
    return None


# -----------------------------
# Google OAuth / Blogger
# -----------------------------
def load_google_creds() -> Optional[Credentials]:
    if not os.path.exists(TOKEN_PATH):
        return None
    try:
        data = json.load(open(TOKEN_PATH, "r", encoding="utf-8"))
        creds = Credentials.from_authorized_user_info(data, SCOPES)
        # Refresh if needed
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            save_google_creds(creds)
        return creds
    except Exception:
        return None


def save_google_creds(creds: Credentials) -> None:
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def blogger_service() -> Optional[Any]:
    creds = load_google_creds()
    if not creds:
        return None
    return build("blogger", "v3", credentials=creds, cache_discovery=False)


@app.get("/api/oauth/status")
def api_oauth_status():
    creds = load_google_creds()
    connected = bool(creds and creds.valid)
    return ok({"connected": connected})


@app.get("/oauth/start")
def oauth_start():
    maybe = require_redirect_url()
    if maybe:
        return maybe

    if not os.path.exists(CLIENT_SECRETS):
        return err(f"client_secrets.json 파일을 찾을 수 없습니다: {CLIENT_SECRETS}", status=500)

    # Flow
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URL,
    )
    auth_url, _state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return redirect(auth_url)


@app.get("/oauth/callback")
def oauth_callback():
    maybe = require_redirect_url()
    if maybe:
        return maybe

    if not os.path.exists(CLIENT_SECRETS):
        return err(f"client_secrets.json 파일을 찾을 수 없습니다: {CLIENT_SECRETS}", status=500)

    flow = Flow.from_client_secrets_file(
        CLIENT_SECRETS,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URL,
    )
    # full URL includes code
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    save_google_creds(creds)

    # 간단 안내 페이지
    return """
    <html><body style="font-family:system-ui;padding:24px;line-height:1.6">
      <h2>✅ OAuth 연결 완료</h2>
      <p>이 창을 닫고 BaseOne으로 돌아가서 <b>“내 블로그 불러오기”</b>를 눌러주세요.</p>
    </body></html>
    """


@app.get("/api/blogger/blogs")
def api_blogger_blogs():
    svc = blogger_service()
    if not svc:
        return err("OAuth not connected", status=401)

    try:
        resp = svc.blogs().listByUser(userId="self").execute()
        items = resp.get("items", []) or []
        out = [{"id": it.get("id"), "name": it.get("name"), "url": it.get("url")} for it in items]
        return ok({"items": out})
    except Exception as e:
        return err(f"Blogger API error: {e}", status=500)


@app.post("/api/blogger/post")
def api_blogger_post():
    svc = blogger_service()
    if not svc:
        return err("OAuth not connected", status=401)

    data = request.get_json(force=True, silent=True) or {}
    blog_id = (data.get("blog_id") or "").strip()
    title = (data.get("title") or "").strip()
    html = (data.get("html") or "").strip()

    if not blog_id or not title or not html:
        return err("blog_id, title, html가 필요합니다.", status=400)

    try:
        post_body = {
            "kind": "blogger#post",
            "title": title,
            "content": html,
        }
        created = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
        url = created.get("url")
        return ok({"url": url})
    except Exception as e:
        return err(f"Blogger post error: {e}", status=500)


# -----------------------------
# LLM: OpenAI (Responses API)
# -----------------------------
def call_openai(openai_key: str, model: str, prompt: str) -> str:
    """
    OpenAI Responses API 우선 사용.
    실패하면 Chat Completions로 fallback.
    """
    openai_key = (openai_key or "").strip()
    if not openai_key:
        raise RuntimeError("OPENAI key missing")

    model = (model or "gpt-5.2-mini").strip()

    # 1) Responses API
    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": prompt,
                "temperature": 0.7,
            },
            timeout=60,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI responses error {r.status_code}: {r.text[:400]}")
        j = r.json()
        # Extract text
        # responses output 형태: output -> content -> text
        text_parts = []
        for out in j.get("output", []) or []:
            for c in out.get("content", []) or []:
                if c.get("type") == "output_text":
                    text_parts.append(c.get("text", ""))
        text = "\n".join([t for t in text_parts if t]).strip()
        if text:
            return text
        # fallback if empty
        raise RuntimeError("OpenAI responses returned empty text")
    except Exception:
        pass

    # 2) Chat Completions fallback
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {openai_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful writing assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
        },
        timeout=60,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI chat error {r.status_code}: {r.text[:400]}")
    j = r.json()
    return (j["choices"][0]["message"]["content"] or "").strip()


# -----------------------------
# LLM: Gemini (Generative Language API)
# -----------------------------
def normalize_gemini_model(model: str) -> str:
    m = (model or "").strip()
    if not m:
        return "gemini-1.5-flash-latest"
    # 흔히 쓰는 축약을 -latest로 보정
    if m == "gemini-1.5-flash":
        return "gemini-1.5-flash-latest"
    if m == "gemini-1.5-pro":
        return "gemini-1.5-pro-latest"
    return m


def call_gemini(gemini_key: str, model: str, prompt: str) -> str:
    """
    Gemini endpoint 문제(404) 회피:
    - v1 먼저 시도 -> 실패하면 v1beta 시도
    - model 자동 보정(예: gemini-1.5-flash -> gemini-1.5-flash-latest)
    """
    gemini_key = (gemini_key or "").strip()
    if not gemini_key:
        raise RuntimeError("GEMINI key missing")

    model = normalize_gemini_model(model)

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2048,
        },
    }

    def _try(version: str) -> Optional[str]:
        url = f"https://generativelanguage.googleapis.com/{version}/models/{model}:generateContent?key={gemini_key}"
        r = requests.post(url, json=payload, timeout=60)
        if r.status_code >= 400:
            return None
        j = r.json()
        # candidates[0].content.parts[0].text
        cands = j.get("candidates") or []
        if not cands:
            return None
        parts = ((cands[0].get("content") or {}).get("parts") or [])
        text = "\n".join([p.get("text", "") for p in parts if p.get("text")]).strip()
        return text or None

    text = _try("v1")
    if text:
        return text
    text = _try("v1beta")
    if text:
        return text

    # last: model이 완전 틀렸을 수도 있으니 원본 모델로 1회 더(혹시 -latest가 문제인 경우)
    model2 = (model or "").replace("-latest", "").strip()
    if model2 and model2 != model:
        def _try2(version: str) -> Optional[str]:
            url = f"https://generativelanguage.googleapis.com/{version}/models/{model2}:generateContent?key={gemini_key}"
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code >= 400:
                return None
            j = r.json()
            cands = j.get("candidates") or []
            if not cands:
                return None
            parts = ((cands[0].get("content") or {}).get("parts") or [])
            t = "\n".join([p.get("text", "") for p in parts if p.get("text")]).strip()
            return t or None

        text = _try2("v1") or _try2("v1beta")
        if text:
            return text

    raise RuntimeError("Gemini API error: 모델/버전 불일치 가능. 설정의 gemini_model을 확인하세요.")


def llm_text(writer: str, gemini_key: str, gemini_model: str, openai_key: str, openai_model: str, prompt: str) -> str:
    w = (writer or "gemini").lower().strip()
    if w == "openai":
        return call_openai(openai_key, openai_model, prompt)
    # default: gemini, but if gemini key missing and openai exists -> fallback
    if not (gemini_key or "").strip() and (openai_key or "").strip():
        return call_openai(openai_key, openai_model, prompt)
    return call_gemini(gemini_key, gemini_model, prompt)


# -----------------------------
# Pexels Image
# -----------------------------
def pexels_search(pexels_key: str, query: str) -> Optional[str]:
    key = (pexels_key or "").strip()
    if not key:
        return None
    q = (query or "").strip()
    if not q:
        return None

    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": key},
        params={"query": q, "per_page": 1},
        timeout=30,
    )
    if r.status_code >= 400:
        return None
    j = r.json()
    photos = j.get("photos") or []
    if not photos:
        return None
    src = (photos[0].get("src") or {})
    # large > original > medium
    return src.get("large") or src.get("original") or src.get("medium")


# -----------------------------
# Content builders (Topics / Keywords / Article)
# -----------------------------
def prompt_topics(category: str, count: int) -> str:
    return f"""
너는 블로그 제목(주제) 생성기야.
카테고리: {category}
요구사항:
- 한국어
- 클릭 유도형 제목
- 과장/허위 금지, 실용적인 톤
- 숫자/체크리스트/비교/실수/순서 같은 형식 선호
- 제목만 {count}개를 JSON 배열로만 출력해라. (설명 금지)

출력 예시:
["제목1","제목2",...]
""".strip()


def prompt_keywords_to_topics(seed: str, category: str, count: int) -> str:
    return f"""
너는 키워드 자동수집 + 주제화 도우미야.
시드 키워드: {seed}
카테고리: {category}

1) 롱테일 키워드 30~80개 생성(구체/질문형/비교형/실수형/순서형 포함)
2) 그 키워드를 기반으로 클릭 유도형 '주제(제목)' {count}개 생성

반드시 아래 JSON 형식으로만 출력해:
{{
  "keywords": ["...","..."],
  "topics": ["...","..."]
}}
설명/문장/코드블록 금지.
""".strip()


def prompt_generate_article(topic: str, category: str) -> str:
    return f"""
너는 한국어 블로그 글 작성자야. 아래 주제로 바로 게시 가능한 HTML 본문을 만들어라.

주제(제목): {topic}
카테고리: {category}

요구사항:
- HTML만 반환(<!doctype> 없이 body 내부만)
- H2/H3 구조로 6~10개 섹션
- 표 1개(비교/체크리스트/요약)
- "주의사항/실수 TOP" 섹션 포함
- 결론에 행동 유도(체크리스트/다음 단계)
- 과장/허위 금지. 모르면 "일반적으로"로 표현
- 마지막에 참고 키워드 8개를 <ul>로(SEO용)

반드시 아래 JSON으로만 출력해:
{{
  "html": "<h2>...</h2>...",
  "image_query": "pexels에서 찾을 이미지 검색어(짧게)",
  "image_prompt": "이미지 설명 1문장"
}}
설명/문장/코드블록 금지.
""".strip()


def try_parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """
    LLM이 가끔 앞/뒤에 군더더기 텍스트를 붙일 수 있어서,
    JSON 객체 부분만 슬라이싱해서 파싱을 시도.
    """
    if not text:
        return None
    t = text.strip()
    # 바로 파싱
    j = safe_json_loads(t)
    if isinstance(j, dict):
        return j

    # 객체 시작/끝 찾기
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        sub = t[start : end + 1]
        j2 = safe_json_loads(sub)
        if isinstance(j2, dict):
            return j2
    return None


def try_parse_json_array(text: str) -> Optional[List[Any]]:
    if not text:
        return None
    t = text.strip()
    j = safe_json_loads(t)
    if isinstance(j, list):
        return j
    start = t.find("[")
    end = t.rfind("]")
    if start != -1 and end != -1 and end > start:
        sub = t[start : end + 1]
        j2 = safe_json_loads(sub)
        if isinstance(j2, list):
            return j2
    return None


# -----------------------------
# API: Topics / Keywords / Generate
# -----------------------------
@app.post("/api/topics/money")
def api_topics_money():
    data = request.get_json(force=True, silent=True) or {}
    count = int(data.get("count") or 30)
    count = max(5, min(60, count))
    category = (data.get("category") or "돈/재테크").strip()

    writer = data.get("writer") or "gemini"
    gemini_key = data.get("gemini_key") or ""
    gemini_model = data.get("gemini_model") or "gemini-1.5-flash"
    openai_key = data.get("openai_key") or ""
    openai_model = data.get("openai_model") or "gpt-5.2-mini"

    try:
        text = llm_text(writer, gemini_key, gemini_model, openai_key, openai_model, prompt_topics(category, count))
        arr = try_parse_json_array(text) or []
        items = [str(x).strip() for x in arr if str(x).strip()]
        if not items:
            return err("LLM 응답 파싱 실패(주제 배열).", status=500, raw=text[:800])
        return ok({"items": items[:count]})
    except Exception as e:
        return err(str(e), status=500)


@app.post("/api/keywords/collect_topics")
def api_keywords_collect_topics():
    data = request.get_json(force=True, silent=True) or {}
    seed = (data.get("seed") or "").strip()
    if not seed:
        return err("seed(키워드)가 필요합니다.", status=400)

    count = int(data.get("count") or 30)
    count = max(5, min(60, count))
    category = (data.get("category") or "돈/재테크").strip()

    writer = data.get("writer") or "gemini"
    gemini_key = data.get("gemini_key") or ""
    gemini_model = data.get("gemini_model") or "gemini-1.5-flash"
    openai_key = data.get("openai_key") or ""
    openai_model = data.get("openai_model") or "gpt-5.2-mini"

    try:
        text = llm_text(writer, gemini_key, gemini_model, openai_key, openai_model, prompt_keywords_to_topics(seed, category, count))
        obj = try_parse_json_object(text) or {}
        kws = [str(x).strip() for x in (obj.get("keywords") or []) if str(x).strip()]
        tops = [str(x).strip() for x in (obj.get("topics") or []) if str(x).strip()]
        if not tops:
            return err("LLM 응답 파싱 실패(keywords/topics).", status=500, raw=text[:900])
        return ok({"keywords": kws[:120], "topics": tops[:count]})
    except Exception as e:
        return err(str(e), status=500)


@app.post("/api/generate")
def api_generate():
    data = request.get_json(force=True, silent=True) or {}
    topic = (data.get("topic") or "").strip()
    category = (data.get("category") or "돈/재테크").strip()
    if not topic:
        return err("topic이 필요합니다.", status=400)

    writer = data.get("writer") or "gemini"
    gemini_key = data.get("gemini_key") or ""
    gemini_model = data.get("gemini_model") or "gemini-1.5-flash"
    openai_key = data.get("openai_key") or ""
    openai_model = data.get("openai_model") or "gpt-5.2-mini"

    img_provider = (data.get("img_provider") or "pexels").strip().lower()
    pexels_key = data.get("pexels_key") or ""

    try:
        text = llm_text(writer, gemini_key, gemini_model, openai_key, openai_model, prompt_generate_article(topic, category))
        obj = try_parse_json_object(text)
        if not obj:
            return err("LLM 응답 파싱 실패(글 JSON).", status=500, raw=text[:900])

        html = (obj.get("html") or "").strip()
        image_query = (obj.get("image_query") or "").strip()
        image_prompt = (obj.get("image_prompt") or "").strip()

        if not html:
            return err("LLM이 html을 반환하지 않았습니다.", status=500, raw=text[:900])

        image_url = ""
        if img_provider == "pexels":
            image_url = pexels_search(pexels_key, image_query) or ""

        return ok({"html": html, "image_url": image_url, "image_prompt": image_prompt})
    except Exception as e:
        return err(str(e), status=500)


# -----------------------------
# Tasks API (예약발행)
# -----------------------------
@app.post("/api/tasks/add")
def api_tasks_add():
    data = request.get_json(force=True, silent=True) or {}
    platform = (data.get("platform") or "blogspot").strip()
    blog_id = (data.get("blog_id") or "").strip()
    blog_url = (data.get("blog_url") or "").strip()
    title = (data.get("title") or "").strip()
    html = (data.get("html") or "").strip()
    run_at = (data.get("run_at") or "").strip()

    if not title or not html or not run_at:
        return err("title, html, run_at(ISO)이 필요합니다.", status=400)

    # validate iso
    try:
        _ = parse_iso(run_at)
    except Exception:
        return err("run_at 형식이 올바르지 않습니다. 예) 2026-01-25T03:00:00.000Z", status=400)

    conn = db_conn()
    cur = conn.cursor()
    now = utc_now_iso()
    cur.execute(
        """
        INSERT INTO tasks(platform, blog_id, blog_url, title, html, run_at, status, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (platform, blog_id, blog_url, title, html, run_at, "pending", now, now),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return ok({"id": tid})


@app.get("/api/tasks/list")
def api_tasks_list():
    conn = db_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, platform, blog_id, blog_url, title, run_at, status, result_url, error, created_at, updated_at
        FROM tasks
        ORDER BY id DESC
        LIMIT 200
        """
    )
    rows = cur.fetchall()
    conn.close()
    items = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "platform": r["platform"],
                "blog_id": r["blog_id"],
                "blog_url": r["blog_url"],
                "title": r["title"],
                "run_at": r["run_at"],
                "status": r["status"],
                "result_url": r["result_url"],
                "error": r["error"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
        )
    return ok({"items": items})


@app.post("/api/tasks/cancel/<int:task_id>")
def api_tasks_cancel(task_id: int):
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("SELECT status FROM tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return err("task not found", status=404)

    if row["status"] not in ("pending",):
        conn.close()
        return err("pending 상태만 취소할 수 있습니다.", status=400)

    now = utc_now_iso()
    cur.execute(
        "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
        ("canceled", now, task_id),
    )
    conn.commit()
    conn.close()
    return ok({"id": task_id})


# -----------------------------
# Task Worker (background)
# -----------------------------
def run_task_publish(task_row: sqlite3.Row) -> Tuple[bool, str, str]:
    """
    Returns: (success, result_url, error_message)
    """
    platform = task_row["platform"]
    blog_id = task_row["blog_id"]
    title = task_row["title"]
    html = task_row["html"]

    if platform != "blogspot":
        return False, "", f"Unsupported platform: {platform}"

    svc = blogger_service()
    if not svc:
        return False, "", "OAuth not connected"

    if not blog_id:
        return False, "", "blog_id missing"

    try:
        post_body = {"kind": "blogger#post", "title": title, "content": html}
        created = svc.posts().insert(blogId=blog_id, body=post_body, isDraft=False).execute()
        url = created.get("url") or ""
        return True, url, ""
    except Exception as e:
        return False, "", f"Blogger post error: {e}"


def worker_loop():
    while True:
        try:
            conn = db_conn()
            cur = conn.cursor()

            # pick due pending tasks
            cur.execute(
                """
                SELECT *
                FROM tasks
                WHERE status='pending'
                ORDER BY id ASC
                LIMIT 10
                """
            )
            rows = cur.fetchall()

            now_dt = datetime.now(timezone.utc)
            for r in rows:
                try:
                    run_at_dt = parse_iso(r["run_at"])
                except Exception:
                    run_at_dt = now_dt

                if run_at_dt > now_dt:
                    continue

                # mark running
                now = utc_now_iso()
                cur.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?", ("running", now, r["id"]))
                conn.commit()

                # execute
                success, result_url, error_msg = run_task_publish(r)

                now2 = utc_now_iso()
                if success:
                    cur.execute(
                        "UPDATE tasks SET status=?, result_url=?, error=?, updated_at=? WHERE id=?",
                        ("ok", result_url, None, now2, r["id"]),
                    )
                else:
                    cur.execute(
                        "UPDATE tasks SET status=?, result_url=?, error=?, updated_at=? WHERE id=?",
                        ("err", "", error_msg, now2, r["id"]),
                    )
                conn.commit()

            conn.close()
        except Exception:
            # worker 자체가 죽지 않게
            pass

        time.sleep(max(5, TASK_POLL_SECONDS))


# -----------------------------
# Root
# -----------------------------
@app.get("/")
def root():
    return ok({"service": "baseone-backend", "time": utc_now_iso()})


# -----------------------------
# Boot
# -----------------------------
db_init()

if TASK_WORKER:
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()

# Local run (Render는 gunicorn 사용)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

