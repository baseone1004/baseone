from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os
from datetime import datetime

# BaseOne 폴더 경로 (server.py가 있는 폴더)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
CORS(app)  # 로컬 테스트에서 프론트->API 호출 허용

# -----------------------------
# 1) 프론트 파일 제공 (index.html, style.css, 아이콘 등)
# -----------------------------
@app.route("/")
def home_page():
    # BaseOne 화면(index.html) 보여주기
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/style.css")
def style_css():
    return send_from_directory(BASE_DIR, "style.css")

@app.route("/baseone.ico")
def favicon():
    # 파일이 없으면 404 나도 괜찮음
    return send_from_directory(BASE_DIR, "baseone.ico")

# 필요하면 다른 파일도 자동 서빙 (예: images 폴더)
@app.route("/images/<path:filename>")
def images(filename):
    return send_from_directory(os.path.join(BASE_DIR, "images"), filename)


# -----------------------------
# 2) API (연동확인 / 글생성 샘플)
# -----------------------------
@app.route("/api/blogspot/test")
def blogspot_test():
    return jsonify({
        "ok": True,
        "message": "서버 연결 OK (실제 Blogger 연동은 OAuth 단계 필요)"
    })

@app.route("/api/generate", methods=["POST"])
def generate():
    """
    프론트에서 주제/카테고리/블로그를 보내면
    (지금은) 샘플 글/이미지프롬프트를 만들어서 돌려줌
    """
    data = request.get_json(force=True) or {}
    topic = (data.get("topic") or "").strip()
    category = (data.get("category") or "").strip()
    blog = (data.get("blog") or "").strip()

    if not topic:
        return jsonify({"ok": False, "message": "topic(주제)가 비어있어요."}), 400

    # ✅ 지금은 샘플(가짜) 결과
    body_prompt = f"""
아래 조건으로 블로그 글을 한국어로 작성해줘.

- 블로그: {blog}
- 카테고리: {category}
- 주제: {topic}

요구사항:
1) SEO를 고려한 제목 5개 후보
2) 본문은 H2/H3로 구조화
3) 표 1개 포함(가능하면)
4) 마지막에 요약 + 다음 글 추천 3개
5) 광고/과장 표현은 피하고 초보도 따라할 수 있게
""".strip()

    image_prompt = f"{category} 주제의 대표 이미지, 주제: {topic}, 깔끔한 썸네일 스타일, 텍스트 없음"

    return jsonify({
        "ok": True,
        "blog": blog,
        "category": category,
        "topic": topic,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "body_prompt": body_prompt,
        "image_prompt": image_prompt,
        "title": topic
    })


if __name__ == "__main__":
    # 로컬에서만 실행하는 개발용 서버
    app.run(host="127.0.0.1", port=5000, debug=True)
