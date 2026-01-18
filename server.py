from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os
from datetime import datetime

# server.py가 있는 폴더 (BaseOne)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=".")
CORS(app)

# -----------------------------
# 화면(HTML) 제공
# -----------------------------
@app.route("/")
def home_page():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/settings")
def settings_page():
    return send_from_directory(BASE_DIR, "settings.html")

@app.route("/style.css")
def style_css():
    return send_from_directory(BASE_DIR, "style.css")

@app.route("/baseone.ico")
def favicon():
    return send_from_directory(BASE_DIR, "baseone.ico")

# -----------------------------
# 테스트용 API
# -----------------------------
@app.route("/api/blogspot/test")
def blogspot_test():
    return jsonify({
        "ok": True,
        "message": "서버 연결 OK"
    })

@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True) or {}
    topic = (data.get("topic") or "").strip()
    category = (data.get("category") or "").strip()
    blog = (data.get("blog") or "").strip()

    if not topic:
        return jsonify({"ok": False, "message": "topic(주제)가 비어있어요."}), 400

    body_prompt = f"""
블로그 글 생성용 프롬프트

- 블로그: {blog}
- 카테고리: {category}
- 주제: {topic}

조건:
1) 제목 5개
2) H2/H3 구조
3) 표 1개
4) 요약 + 다음글 추천
"""

    image_prompt = f"{category} 주제 썸네일, 주제: {topic}, 텍스트 없음"

    return jsonify({
        "ok": True,
        "blog": blog,
        "category": category,
        "topic": topic,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "body_prompt": body_prompt.strip(),
        "image_prompt": image_prompt,
        "title": topic
    })

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
