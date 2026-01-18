from flask import Flask, send_from_directory, request, jsonify
import os, re, time, uuid
from dotenv import load_dotenv
import requests
from google import genai

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- env ---
load_dotenv()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
PEXELS_API_KEY = (os.getenv("PEXELS_API_KEY") or "").strip()

if not GEMINI_API_KEY:
    print("❌ GEMINI_API_KEY가 .env에 없습니다.")
if not PEXELS_API_KEY:
    print("⚠️ PEXELS_API_KEY가 .env에 없습니다. (이미지 자동 삽입이 안 될 수 있어요)")

client = genai.Client(api_key=GEMINI_API_KEY)

MODEL_CANDIDATES = [
    "gemini-3-flash-preview",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
]

OUTPUT_DIR = os.path.join(BASE_DIR, "output")
IMG_DIR = os.path.join(OUTPUT_DIR, "images")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(IMG_DIR, exist_ok=True)

# ---- in-memory jobs ----
JOBS = {}  # job_id -> dict(status, total, done, logs, results)

def gemini_text(prompt: str) -> str:
    last_err = None
    for m in MODEL_CANDIDATES:
        try:
            r = client.models.generate_content(model=m, contents=prompt)
            t = (r.text or "").strip()
            if t:
                return t
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Gemini 호출 실패: {last_err}")

def parse_topics(text: str) -> list:
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        s = s.lstrip("-•").strip()
        # 앞 번호 제거
        s = re.sub(r"^\s*\d+[\.\)\-]\s*", "", s)
        if s:
            lines.append(s)
    # 중복 제거(순서 유지)
    seen = set()
    uniq = []
    for x in lines:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:50]

def safe_filename(title: str) -> str:
    # 윈도우 파일명에 위험한 문자 제거
    name = re.sub(r'[\\/:*?"<>|]', "", title).strip()
    name = re.sub(r"\s+", "-", name)
    if not name:
        name = "post-" + uuid.uuid4().hex[:8]
    return name[:80]

def pexels_search_image(query: str) -> str:
    if not PEXELS_API_KEY:
        return ""

    url = "https://api.pexels.com/v1/search"
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": 1, "orientation": "landscape"}
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    photos = data.get("photos") or []
    if not photos:
        return ""
    # 원본/large 중 하나
    src = photos[0].get("src") or {}
    return src.get("large2x") or src.get("large") or src.get("original") or ""

def download_image(img_url: str, save_path: str) -> bool:
    if not img_url:
        return False
    try:
        r = requests.get(img_url, timeout=60)
        r.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(r.content)
        return True
    except Exception:
        return False

def build_post_html(title: str, category: str, blog: str, image_rel_path: str, body_html: str) -> str:
    # 아이콘/박스/표 스타일 포함
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f6f7fb;margin:0;}}
  .wrap{{max-width:860px;margin:0 auto;padding:24px 16px;}}
  .card{{background:#fff;border-radius:16px;box-shadow:0 12px 30px rgba(0,0,0,.08);padding:18px;}}
  h1{{font-size:28px;line-height:1.25;margin:0 0 10px;}}
  .meta{{font-size:13px;color:#666;margin-bottom:14px;}}
  .hero img{{width:100%;border-radius:14px;display:block;}}
  .toc{{background:#f1f4ff;border-radius:14px;padding:12px 14px;margin:16px 0;}}
  .toc b{{display:block;margin-bottom:8px;}}
  .toc a{{display:block;color:#1f3b8f;text-decoration:none;font-size:14px;line-height:1.5;margin:4px 0;}}
  .box{{border:1px solid #e8ebf5;border-radius:14px;padding:12px 14px;margin:12px 0;background:#fbfcff;}}
  .box .t{{font-weight:700;margin-bottom:6px;}}
  .icons{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0;}}
  .ico{{border:1px solid #e8ebf5;border-radius:14px;padding:12px;background:#fff;}}
  .ico .k{{font-weight:700;margin-bottom:6px;}}
  table{{width:100%;border-collapse:collapse;margin:14px 0;}}
  th,td{{border:1px solid #e6e8f2;padding:10px;text-align:left;font-size:14px;}}
  th{{background:#f5f6ff;}}
  .footer{{font-size:12px;color:#777;margin-top:18px;}}
</style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>{title}</h1>
      <div class="meta">카테고리: {category} · 대상 블로그: {blog}</div>
      <div class="hero">{f'<img src="{image_rel_path}" alt="{title}">' if image_rel_path else ''}</div>
      {body_html}
      <div class="footer">※ 자동 생성 글입니다. 게시 전 사실/날짜/정책을 꼭 확인하세요.</div>
    </div>
  </div>
</body>
</html>
"""

def generate_article_html(title: str, category: str, blog: str) -> str:
    # 요구사항: 목차 8~9개 + 각 소제목 아래 700자 이상 + 표/아이콘/박스 포함 + 길게(대략 14000자+)
    prompt = f"""
너는 한국어 정보블로그 전문 작가다.

제목: {title}
카테고리: {category}
플랫폼: {blog}

요구사항(매우 중요):
- 목차는 8~9개만 만들기.
- 각 소제목 아래 본문은 '최소 700자 이상' (한국어 기준)으로 충분히 길고 자세하게.
- 글 전체 분량은 아주 길게(대략 14,000자 이상이 되도록) 작성.
- 중간에 "아이콘 박스 3개" 섹션을 포함해라. (예: ✅ 체크, ⚠️ 주의, 💡 팁 같은 아이콘 느낌)
- 중간에 "표(table)" 1개 이상 포함 (비교표/체크리스트/요약표)
- 중간중간 박스(요약/주의/팁) 형태의 문단을 포함.
- 최종 출력은 'HTML body 조각'만. (즉 <h2>, <p>, <ul>, <table> 등만, <html><head>는 쓰지 말 것)
- 광고/과장/허위 금지. 사실 확인이 필요한 부분은 "확인 필요"라고 표시.

형식:
- <div class="toc"> 안에 목차 링크(앵커) 생성
- 각 섹션은 <h2 id="s1"> ... </h2> 형태로 앵커와 함께.
- 아이콘 박스 섹션은 <div class="icons"> 안에 <div class="ico"> 3개 구성
- 표는 <table>...</table> 로 작성

지금 작성 시작.
"""
    body = gemini_text(prompt)

    # Gemini가 body 규칙을 어기면 최소한의 안전장치로 감싸기
    if "<html" in body.lower():
        # 대충 body만 남기기
        body = re.sub(r"(?is).*<body[^>]*>", "", body)
        body = re.sub(r"(?is)</body>.*", "", body)

    # 아이콘 박스가 빠졌을 때 보강(최소 보정)
    if 'class="icons"' not in body:
        body += """
<div class="box"><div class="t">핵심 요약</div><p>이 글의 핵심만 먼저 확인하고 싶다면 아래 3가지를 기억하세요.</p></div>
<div class="icons">
  <div class="ico"><div class="k">✅ 체크</div><p>실행 전 필요한 준비물/조건을 먼저 확인하세요.</p></div>
  <div class="ico"><div class="k">⚠️ 주의</div><p>제도/정책/가격은 바뀔 수 있으니 최종 확인은 꼭 하세요.</p></div>
  <div class="ico"><div class="k">💡 팁</div><p>시간을 줄이려면 단계별 체크리스트로 진행하세요.</p></div>
</div>
"""
    if "<table" not in body.lower():
        body += """
<div class="box"><div class="t">한눈에 보는 체크표</div></div>
<table>
  <tr><th>항목</th><th>체크</th><th>메모</th></tr>
  <tr><td>준비물 확인</td><td>□</td><td></td></tr>
  <tr><td>절차 순서 정리</td><td>□</td><td></td></tr>
  <tr><td>주의사항 확인</td><td>□</td><td></td></tr>
</table>
"""
    return body

# --- routes ---
@app.route("/")
def home():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/<path:filename>")
def files(filename):
    return send_from_directory(BASE_DIR, filename)

@app.route("/api/topics", methods=["POST"])
def api_topics():
    data = request.get_json(force=True)
    category = (data.get("category") or "").strip()
    blog = (data.get("blog") or "").strip()

    if not category or not blog:
        return jsonify({"ok": False, "error": "category/blog가 비었습니다."}), 400
    if not GEMINI_API_KEY:
        return jsonify({"ok": False, "error": "GEMINI_API_KEY가 설정되지 않았습니다(.env 확인)."}), 400

    prompt = f"""
너는 한국어 정보블로그 편집자다.
카테고리: {category}
업로드 플랫폼: {blog}

요청:
- {category} 카테고리에서 사람들이 검색할 만한 "정보성 글 주제" 50개를 만들어라.
- 제목은 클릭하고 싶게, 그러나 과장 금지.
- 각 줄에 하나씩만.
- 맨 앞에 번호나 기호 없이 제목만 출력.
"""
    try:
        raw = gemini_text(prompt)
        topics = parse_topics(raw)
        return jsonify({"ok": True, "topics": topics})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/start_generate", methods=["POST"])
def api_start_generate():
    data = request.get_json(force=True)
    category = (data.get("category") or "").strip()
    blog = (data.get("blog") or "").strip()
    topics = data.get("topics") or []

    if not category or not blog or not isinstance(topics, list) or len(topics) == 0:
        return jsonify({"ok": False, "error": "category/blog/topics가 올바르지 않습니다."}), 400

    # 안전: 한 번에 너무 많이 생성하면 키/시간이 폭발할 수 있어서 제한
    if len(topics) > 10:
        return jsonify({"ok": False, "error": "처음에는 10개까지만 선택해주세요. (안정화 후 제한 해제 가능)"}), 400

    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {
        "status": "running",
        "total": len(topics),
        "done": 0,
        "logs": [],
        "results": []
    }

    # 동기 처리(간단). 원하면 다음 단계에서 백그라운드 스레드로 개선 가능.
    try:
        for t in topics:
            title = str(t).strip()
            if not title:
                continue

            JOBS[job_id]["logs"].append(f"시작: {title}")

            # 1) 이미지: Pexels에서 검색 후 다운로드
            img_url = ""
            img_rel = ""
            try:
                img_url = pexels_search_image(query=title)
                if img_url:
                    fname = safe_filename(title) + ".jpg"
                    save_path = os.path.join(IMG_DIR, fname)
                    ok = download_image(img_url, save_path)
                    if ok:
                        img_rel = f"images/{fname}"
            except Exception:
                pass

            # 2) 글 생성 (긴 글)
            body_html = generate_article_html(title=title, category=category, blog=blog)

            # 3) 저장
            html_name = safe_filename(title) + ".html"
            html_path = os.path.join(OUTPUT_DIR, html_name)
            full_html = build_post_html(title, category, blog, img_rel, body_html)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(full_html)

            JOBS[job_id]["results"].append({
                "title": title,
                "html_file": html_name,
                "image_file": img_rel
            })
            JOBS[job_id]["done"] += 1
            JOBS[job_id]["logs"].append(f"완료: {title} → output/{html_name}")
            time.sleep(0.2)

        JOBS[job_id]["status"] = "done"
        return jsonify({"ok": True, "job_id": job_id})
    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["logs"].append("에러: " + str(e))
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/job/<job_id>", methods=["GET"])
def api_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job_id가 없습니다."}), 404
    return jsonify({"ok": True, "job": job})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
