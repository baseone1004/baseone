from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os
from datetime import datetime

from google import genai
from google.genai import types

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
    return jsonify({"ok": True, "message": "서버 연결 OK"})

def build_prompt(topic: str, category: str) -> str:
    # ✅ 요구사항 그대로 박아넣기 (HTML만 출력)
    return f"""
너는 '수익형 정보블로그' 전문 작가다.
아래 조건을 모두 만족하는 한국어 블로그 글을 **HTML만** 출력해라. (설명 금지, 코드블록 금지)

[주제]
- 카테고리: {category}
- 주제: {topic}

[필수 조건]
1) 전체 분량: **14,000자 이상**
2) 목차/소제목(H2)은 **8~9개만**
3) 각 H2 아래 본문은 **700자 이상**
4) 본문 중간에 **표 1개** 포함 (HTML <table>)
5) 아이콘/박스 디자인 요소 포함:
   - ✅ 체크박스 스타일
   - 💡 팁 박스
   - ⚠️ 주의 박스
   (div + 인라인 스타일로 예쁘게)
6) 마지막에:
   - 요약(3~5줄)
   - FAQ 5개 (질문/답변)
   - 행동유도(댓글/구독 등)

[스타일]
- 초보도 이해하게 친절하게
- 과장/허위 금지
- SEO 고려(자연스러운 키워드 반복, 소제목에 핵심 키워드 포함)

[출력 형식]
- 오직 HTML만 출력
- <h1>제목</h1>로 시작
""".strip()

def gemini_generate_html(api_key: str, model: str, prompt: str) -> str:
    # API 키가 있으면 우선 사용, 없으면 환경변수(GEMINI_API_KEY)를 사용
    if api_key:
        client = genai.Client(api_key=api_key)
    else:
        client = genai.Client()  # 환경변수 GEMINI_API_KEY가 있으면 자동 인식

    # 길게 뽑기 위해 max_output_tokens 크게
    cfg = types.GenerateContentConfig(
        temperature=0.7,
        max_output_tokens=8192
    )

    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=cfg
    )

    text = (resp.text or "").strip()
    try:
        client.close()
    except Exception:
        pass
    return text

def ensure_length(html: str, api_key: str, model: str, topic: str, category: str) -> str:
    # 14,000자 미만이면 보강 요청(최대 2번)
    if len(html) >= 14000:
        return html

    for _ in range(2):
        add_prompt = f"""
아래 HTML 글은 너무 짧다. **전체 14,000자 이상**이 되도록 확장해라.
- H2는 8~9개 유지
- 각 H2 아래를 700자 이상으로 늘려라
- 표 1개 유지
- 아이콘/박스(✅💡⚠️) 유지
- HTML만 출력(설명 금지)

[기존 글]
{html}
""".strip()

        new_html = gemini_generate_html(api_key, model, add_prompt)
        if len(new_html) > len(html):
            html = new_html
        if len(html) >= 14000:
            break

    return html

@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True) or {}
    topic = (data.get("topic") or "").strip()
    category = (data.get("category") or "").strip()
    blog = (data.get("blog") or "").strip()

    # ✅ index.html에서 같이 보내는 geminiKey 사용 (로컬용)
    gemini_key = (data.get("geminiKey") or "").strip()

    if not topic:
        return jsonify({"ok": False, "message": "topic(주제)가 비어있어요."}), 400

    # 모델은 기본값. (필요하면 바꿔도 됨)
    model = (data.get("model") or "").strip() or "gemini-3-flash-preview"

    prompt = build_prompt(topic, category)
    html = gemini_generate_html(gemini_key, model, prompt)
    html = ensure_length(html, gemini_key, model, topic, category)

    image_prompt = f'{category} 블로그 썸네일, 주제 "{topic}", 텍스트 없음, 깔끔한 스타일, 16:9'

    return jsonify({
        "ok": True,
        "blog": blog,
        "category": category,
        "topic": topic,
        "model": model,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": topic,
        "image_prompt": image_prompt,
        "html": html
    })

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
