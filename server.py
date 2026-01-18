from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from datetime import datetime
from pathlib import Path
import requests
import json

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

BASE_DIR = Path(__file__).resolve().parent
QUEUE_DIR = BASE_DIR / "queue"
QUEUE_DIR.mkdir(exist_ok=True)
QUEUE_FILE = QUEUE_DIR / "publish_queue.jsonl"

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def safe_send(file_name: str):
    path = BASE_DIR / file_name
    if not path.exists():
        return jsonify({
            "ok": False,
            "error": f"{file_name} 파일이 BaseOne 폴더에 없습니다.",
            "hint": f"{BASE_DIR} 위치에 {file_name}을(를) 만들어 주세요."
        }), 404
    return send_from_directory(str(BASE_DIR), file_name)

def append_queue(item: dict):
    item = dict(item)
    item["saved_at"] = now_str()
    with open(QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")

# -----------------------------
# 페이지 라우팅
# -----------------------------
@app.route("/")
def home():
    return safe_send("index.html")

@app.route("/settings")
def settings():
    return safe_send("settings.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})

# -----------------------------
# Pexels 무료 이미지 검색
# -----------------------------
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

# -----------------------------
# API: 생성
# -----------------------------
@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}

    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"
    blog = (payload.get("blog") or "").strip() or "local"

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
        "blog": blog,
        "generated_at": now_str(),
        "title": topic,
        "body_prompt": body_prompt,
        "image_prompt": image_prompt,
        "image_provider": img_provider,
        "image_url": image_url
    })

# -----------------------------
# ✅ 발행 요청 저장(예약/즉시)
# (실제 업로드는 다음 단계에서 OAuth 붙여서 구현)
# -----------------------------
@app.route("/api/publish/schedule", methods=["POST"])
def api_publish_schedule():
    payload = request.get_json(silent=True) or {}
    blog_type = (payload.get("blog_type") or "").strip()  # blogspot/naver/tistory
    blog_url = (payload.get("blog_url") or "").strip()
    category = (payload.get("category") or "").strip()
    topic = (payload.get("topic") or "").strip()
    times = payload.get("schedule_times") or []

    if not blog_type:
        return jsonify({"ok": False, "error": "blog_type is required"}), 400
    if not blog_url:
        return jsonify({"ok": False, "error": "blog_url is required"}), 400
    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400
    if not isinstance(times, list) or len(times) == 0:
        return jsonify({"ok": False, "error": "schedule_times(list) is required"}), 400

    item = {
        "type": "schedule",
        "blog_type": blog_type,
        "blog_url": blog_url,
        "category": category,
        "topic": topic,
        "schedule_times": times
    }
    append_queue(item)

    return jsonify({
        "ok": True,
        "message": f"예약 요청 저장 ✅ ({blog_type}) {blog_url} / {(' / '.join(times))}",
        "saved_to": str(QUEUE_FILE)
    })

@app.route("/api/publish/now", methods=["POST"])
def api_publish_now():
    payload = request.get_json(silent=True) or {}
    blog_type = (payload.get("blog_type") or "").strip()
    blog_url = (payload.get("blog_url") or "").strip()
    category = (payload.get("category") or "").strip()
    topic = (payload.get("topic") or "").strip()
    start_time = (payload.get("start_time") or "").strip()  # "09:00"
    interval_hours = payload.get("interval_hours")

    if not blog_type:
        return jsonify({"ok": False, "error": "blog_type is required"}), 400
    if not blog_url:
        return jsonify({"ok": False, "error": "blog_url is required"}), 400
    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400
    if not start_time:
        return jsonify({"ok": False, "error": "start_time is required (HH:MM)"}), 400

    try:
        interval_hours = int(interval_hours)
        if interval_hours <= 0:
            raise ValueError()
    except Exception:
        return jsonify({"ok": False, "error": "interval_hours must be a positive integer"}), 400

    item = {
        "type": "now_interval",
        "blog_type": blog_type,
        "blog_url": blog_url,
        "category": category,
        "topic": topic,
        "start_time": start_time,
        "interval_hours": interval_hours
    }
    append_queue(item)

    return jsonify({
        "ok": True,
        "message": f"즉시 발행 요청 저장 ✅ ({blog_type}) {blog_url} / 시작 {start_time}, 간격 {interval_hours}시간",
        "saved_to": str(QUEUE_FILE)
    })

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
