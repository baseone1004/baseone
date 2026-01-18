from flask import Flask, jsonify, request
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
CORS(app)  # index.html(웹)에서 서버로 요청 가능하게 해줌

@app.route("/")
def home():
    return "BaseOne 서버 실행중 ✅"

@app.route("/api/blogspot/test")
def blogspot_test():
    # 지금은 '서버 연결 확인'만 해줌
    return jsonify({
        "ok": True,
        "message": "서버 연결 OK (실제 Blogger 연동은 OAuth 단계 필요)"
    })

# ✅ 2단계 시작: 주제 받으면 "글/이미지용 데이터" 만들어서 돌려주기
# 사이트(index.html)에서 POST로 topic을 보내면 JSON을 반환함
@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    category = (data.get("category") or "").strip()
    blog = (data.get("blog") or "").strip()

    if not topic:
        return jsonify({"ok": False, "error": "topic(주제)가 비어있어요"}), 400

    # ✅ 지금은 AI 호출 대신 "템플릿 결과"를 만들어줌
    # 다음 단계에서 여기만 Gemini 연결로 바꾸면 됨.
    title = topic
    image_prompt = f'블로그 썸네일용 고퀄 이미지, 주제: "{topic}", 텍스트 없음, 미니멀, 고대비, 16:9'
    body_prompt = (
        "다음 주제로 한국어 블로그 글을 작성해줘.\n"
        f"- 주제: {topic}\n"
        f"- 카테고리: {category or '미지정'}\n"
        "- SEO: 제목/소제목(H2,H3), 리스트, 표 1개, FAQ 5개 포함\n"
        "- 소제목은 8~9개\n"
        "- 각 소제목 아래는 700자 이상\n"
        "- 전체 길이: 14000자 이상\n"
        "- 톤: 친절/전문, 초보자도 이해\n"
        "- 마지막에 요약 + 행동유도(구독/댓글)\n"
        "- HTML로 출력(아이콘 박스/카드/표 스타일 포함)\n"
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return jsonify({
        "ok": True,
        "generated_at": now,
        "blog": blog,
        "category": category,
        "topic": topic,
        "title": title,
        "body_prompt": body_prompt,
        "image_prompt": image_prompt
    })

if __name__ == "__main__":
    # 외부에서 접속할 필요는 없고, 내 PC에서만 쓰는 거니까 host는 기본 그대로 OK
    app.run(port=5000, debug=True)
