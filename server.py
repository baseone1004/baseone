import os
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from datetime import datetime
import requests

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ✅ Render 루트 확인용 (이게 떠야 정상)
@app.get("/")
def root():
    return "BaseOne API 서버 실행중 ✅"

@app.get("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})

# (로컬에서만 쓰는 화면 라우트 - Render에서도 파일 있으면 열림)
@app.get("/app")
def app_page():
    return send_from_directory(".", "index.html")

@app.get("/settings")
def settings():
    return send_from_directory(".", "settings.html")

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

@app.post("/api/generate")
def api_generate():
    payload = request.get_json(silent=True) or {}

    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "").strip() or "정보"

    img_provider = (payload.get("img_provider") or "pexels").strip()
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
