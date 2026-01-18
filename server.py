from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os
from datetime import datetime

# server.py가 있는 폴더 (BaseOne)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=".")
CORS(app)
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import os
from datetime import datetime

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

def make_sample_html(topic: str, category: str) -> str:
    # ✅ 먼저 “동작 확인용”으로 길고 보기 좋게(표/박스 포함) 샘플 HTML 생성
    # 다음 단계에서 이 함수만 Gemini 호출로 바꿀 예정
    sections = []
    for i in range(1, 9):  # 8개 소제목
        sections.append(f"""
        <h2>소제목 {i}. {topic} 핵심 포인트 {i}</h2>
        <div style="background:#0f1530;border:1px solid rgba(255,255,255,.12);padding:12px;border-radius:12px;margin:10px 0;">
          <b>✅ 한 줄 요약</b><br>
          {topic}을(를) 처음 시작하는 분도 이해할 수 있게 정리했어요.
        </div>
        <p>
          {topic}에 대해 사람들이 가장 많이 헷갈려하는 부분은 “어디서부터 시작해야 하는지”예요.
          그래서 이 글에서는 순서대로, 바로 따라할 수 있도록 정리합니다.
          (이 문단은 샘플이며, 다음 단계에서 Gemini가 14,000자 이상으로 자동 작성됩니다.)
        </p>
        <p>
          체크리스트를 하나씩 따라가면 실패 확률이 확 줄어듭니다.  
          특히 초보자는 “기본 원칙”을 먼저 잡는 게 중요해요.
        </p>
        """)

    table = f"""
    <h2>표로 한눈에 정리</h2>
    <table style="width:100%;border-collapse:collapse;background:#0f1530;border:1px solid rgba(255,255,255,.12);border-radius:12px;overflow:hidden;">
      <thead>
        <tr>
          <th style="padding:10px;border-bottom:1px solid rgba(255,255,255,.12);text-align:left;">항목</th>
          <th style="padding:10px;border-bottom:1px solid rgba(255,255,255,.12);text-align:left;">추천</th>
          <th style="padding:10px;border-bottom:1px solid rgba(255,255,255,.12);text-align:left;">주의</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td style="padding:10px;border-bottom:1px solid rgba(255,255,255,.08);">초보 시작</td>
          <td style="padding:10px;border-bottom:1px solid rgba(255,255,255,.08);">기본부터 천천히</td>
          <td style="padding:10px;border-bottom:1px solid rgba(255,255,255,.08);">무리한 실행 금지</td>
        </tr>
        <tr>
          <td style="padding:10px;border-bottom:1px solid rgba(255,255,255,.08);">시간 관리</td>
          <td style="padding:10px;border-bottom:1px solid rgba(255,255,255,.08);">체크리스트 활용</td>
          <td style="padding:10px;border-bottom:1px solid rgba(255,255,255,.08);">계획만 세우지 말기</td>
        </tr>
        <tr>
          <td style="padding:10px;">수익화 연결</td>
          <td style="padding:10px;">키워드/검색 의도 맞추기</td>
          <td style="padding:10px;">과장/허위 정보 금지</td>
        </tr>
      </tbody>
    </table>
    """

    faq = f"""
    <h2>FAQ (자주 묻는 질문)</h2>
    <div style="display:grid;gap:10px;">
      <div style="background:#0f1530;border:1px solid rgba(255,255,255,.12);padding:12px;border-radius:12px;">
        <b>Q1. {topic}은(는) 초보도 가능한가요?</b><br>네. 순서대로만 하면 충분히 가능합니다.
      </div>
      <div style="background:#0f1530;border:1px solid rgba(255,255,255,.12);padding:12px;border-radius:12px;">
        <b>Q2. 어디서부터 시작해야 하나요?</b><br>기본 개념 → 체크리스트 → 실행 순서로 추천해요.
      </div>
      <div style="background:#0f1530;border:1px solid rgba(255,255,255,.12);padding:12px;border-radius:12px;">
        <b>Q3. 시간이 없으면 어떻게 하죠?</b><br>하루 10분씩 작은 습관으로 시작하세요.
      </div>
      <div style="background:#0f1530;border:1px solid rgba(255,255,255,.12);padding:12px;border-radius:12px;">
        <b>Q4. 비용이 드나요?</b><br>대부분은 무료/저비용으로 시작 가능합니다.
      </div>
      <div style="background:#0f1530;border:1px solid rgba(255,255,255,.12);padding:12px;border-radius:12px;">
        <b>Q5. 수익형 글로 연결하려면?</b><br>검색 의도에 맞춘 제목/소제목 구성부터 잡으세요.
      </div>
    </div>
    """

    html = f"""
    <article style="font-family:sans-serif;color:#eef2ff;line-height:1.7">
      <h1 style="margin-top:0">{topic}</h1>
      <div style="color:rgba(238,242,255,.75);margin-bottom:16px;">
        카테고리: {category} · 작성일: {datetime.now().strftime("%Y-%m-%d")}
      </div>

      <div style="background:#1b2238;border:1px solid rgba(255,255,255,.12);padding:14px;border-radius:14px;margin-bottom:16px;">
        <b>📌 서론</b><br>
        이 글은 “샘플 자동 생성 글”입니다. 다음 단계에서 Gemini를 붙이면 14,000자 이상 + 아이콘 박스/표/구조화된 HTML로 자동 생성됩니다.
      </div>

      {table}
      {''.join(sections)}
      {faq}

      <h2>마무리 요약</h2>
      <ul>
        <li>{topic}은(는) 기본 순서가 중요합니다.</li>
        <li>표/체크리스트로 정리하면 실행이 쉬워집니다.</li>
        <li>다음 단계에서 AI 자동 글 생성으로 완성도를 올립니다.</li>
      </ul>

      <div style="background:#1b2238;border:1px solid rgba(255,255,255,.12);padding:14px;border-radius:14px;margin-top:16px;">
        <b>✅ 다음 액션</b><br>
        마음에 들면 “복사”로 본문을 가져가서 블로그에 붙여넣어 보세요!
      </div>
    </article>
    """
    return html.strip()

@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True) or {}
    topic = (data.get("topic") or "").strip()
    category = (data.get("category") or "").strip()
    blog = (data.get("blog") or "").strip()

    if not topic:
        return jsonify({"ok": False, "message": "topic(주제)가 비어있어요."}), 400

    # (다음 단계) 여기서 Gemini API 호출로 바꾸면 됨
    html = make_sample_html(topic, category)

    image_prompt = f'{category} 관련 블로그 썸네일 이미지, 주제 "{topic}", 텍스트 없음, 깔끔한 스타일'

    return jsonify({
        "ok": True,
        "blog": blog,
        "category": category,
        "topic": topic,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": topic,
        "image_prompt": image_prompt,
        "html": html
    })

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)

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

