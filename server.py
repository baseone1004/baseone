from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def home():
    return "BaseOne 서버 실행중"

@app.route("/api/blogspot/test")
def blogspot_test():
    # 지금은 '서버 연결 확인'만 해줌
    return jsonify({
        "ok": True,
        "message": "서버 연결 OK (실제 Blogger 연동은 OAuth 단계 필요)"
    })

if __name__ == "__main__":
    app.run(port=5000)
