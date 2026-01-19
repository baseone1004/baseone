from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
CORS(app)

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@app.route("/")
def home():
    return "BaseOne API 서버 실행중"

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})

@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json()

    topic = data.get("topic","")
    category = data.get("category","")

    return jsonify({
        "ok": True,
        "title": topic,
        "topic": topic,
        "category": category,
        "generated_at": now_str(),
        "body_prompt": f"{topic} 글을 생성하는 프롬프트입니다.",
        "image_prompt": f"{topic} 썸네일 이미지"
    })

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)



