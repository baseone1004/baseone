from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import json
import re
from datetime import datetime

import requests

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings():
    return send_from_directory(".", "settings.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})

# -----------------------------
# Pexels 무료 이미지 검색
# -----------------------------
def pexels_search_image_url(pexels_key: str, query: str) -> str:
    """
    Pexels API로 query 검색 → 첫 번째 사진 URL 반환
    """
    if not pexels_key:
        return ""

    url = "https://api.pexels.com/v1/search"
    headers = {"Authorization": pexels_key}
    params = {
        "query": query,
        "per_page": 1,
        "orientation": "landscape",
        "size": "large"
    }

    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code != 200:
            return ""
        data = r.json()
        photos = data.get("photos", [])
        if not photos:
            return ""
        src = photos[0].get("src", {})
        # 가장 보기 좋은 크기 우선
        return src.get("large2x") or src.get("large") or src.get("original") or ""
    except Exception:
        return ""

# -----------------------------
# (임시) 글 생성: 지금은 "프롬프트+구조"만 생성
# 다음 단계에서 Gemini/ChatGPT/Genspark 실제 호출로 교체 가능
# -----------------------------
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

    # 설정에서 보내는 값들(프론트에서 같이 보내게 할 예정)
    img_provider = (payload.get("img_provider") or "").strip() or "pexels"
    pexels_key = (payload.get("pexels_key") or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    body_prompt = make_body_prompt(topic, category)
    image_prompt = make_image_prompt(topic, category)

    # ✅ 무료 이미지(pexels)면 URL까지 찾아서 반환
    image_url = ""
    if img_provider == "pexels":
        # 검색어는 topic + category 섞어서 정확도 올림
        q = f"{topic} {category}".strip()
        image_url = pexels_search_image_url(pexels_key, q)

        # 혹시 검색이 0건이면 topic만으로 재시도
        if not image_url:
            image_url = pexels_search_image_url(pexels_key, topic)

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

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
