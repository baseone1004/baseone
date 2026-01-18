from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import json
import re
from datetime import datetime

# ✅ 같은 폴더(BaseOne) 안에 index.html, settings.html이 있어야 함
app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

@app.route("/")
def home():
    return send_from_directory(".", "index.html")

@app.route("/settings")
def settings():
    # settings.html 파일이 BaseOne 폴더에 있어야 함
    return send_from_directory(".", "settings.html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": now_str()})

def call_gemini(api_key: str, model: str, topic: str, category: str):
    """
    google-genai(권장) 우선 사용, 없으면 google.generativeai로 fallback
    응답은 JSON 형태로 유도한 뒤 파싱
    """
    prompt = f"""
너는 블로그 글 작성 도우미야.
주제: {topic}
카테고리: {category}

아래 JSON 형식으로만 출력해. 다른 문장/설명 금지.
- title: 블로그 제목(한글)
- html: 블로그에 바로 붙여넣을 수 있는 HTML 본문(소제목/목록/강조 포함, 1200~2000자)
- image_prompt: 대표 이미지 생성 프롬프트(한글 또는 영어, 1~2문장)

JSON:
{{"title":"...","html":"...","image_prompt":"..."}}
"""

    # 1) google-genai (new)
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=model,
            contents=prompt
        )
        text = getattr(resp, "text", None) or str(resp)
    except Exception:
        # 2) google-generativeai (old)
        import google.generativeai as genai_old
        genai_old.configure(api_key=api_key)
        m = genai_old.GenerativeModel(model)
        resp = m.generate_content(prompt)
        text = resp.text

    # JSON만 뽑아내기(앞뒤 잡문 있어도 파싱되게)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        # 파싱 실패시 최소 형태로 반환
        return {
            "title": f"{topic}",
            "html": f"<h2>{topic}</h2><p>생성 결과를 파싱하지 못했습니다. 모델 응답을 확인해주세요.</p>",
            "image_prompt": f"{topic} 관련 고퀄리티 썸네일, 미니멀, 선명한 조명"
        }

    raw = m.group(0).strip()
    try:
        data = json.loads(raw)
    except Exception:
        # 가끔 따옴표가 깨진 경우 대비(최소 복구)
        return {
            "title": f"{topic}",
            "html": f"<h2>{topic}</h2><p>JSON 파싱 실패. 서버 로그/응답을 확인해주세요.</p><pre>{raw}</pre>",
            "image_prompt": f"{topic} thumbnail, clean, high quality"
        }

    return {
        "title": data.get("title", topic),
        "html": data.get("html", ""),
        "image_prompt": data.get("image_prompt", "")
    }

@app.route("/api/generate", methods=["POST"])
def api_generate():
    payload = request.get_json(silent=True) or {}
    topic = (payload.get("topic") or "").strip()
    category = (payload.get("category") or "").strip()
    model = (payload.get("model") or "gemini-2.0-flash").strip()
    gemini_key = (payload.get("geminiKey") or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    if not gemini_key:
        return jsonify({
            "ok": False,
            "error": "Gemini API Key가 없습니다. 설정에서 Gemini Key 저장 후 다시 시도하세요."
        }), 400

    try:
        out = call_gemini(gemini_key, model, topic, category)
        return jsonify({
            "ok": True,
            "topic": topic,
            "category": category,
            "model": model,
            "generated_at": now_str(),
            "title": out["title"],
            "html": out["html"],
            "image_prompt": out["image_prompt"]
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    # ✅ 다른 기기에서도 접속하려면 host="0.0.0.0"
    app.run(host="127.0.0.1", port=5000, debug=True)
