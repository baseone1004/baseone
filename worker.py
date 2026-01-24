# worker.py
import os, time
import requests

WEB_BASE_URL = os.environ.get("WEB_BASE_URL", "").rstrip("/")

def main():
    if not WEB_BASE_URL:
        raise RuntimeError("WEB_BASE_URL is required (e.g. https://yourapp.onrender.com)")

    while True:
        try:
            r = requests.post(WEB_BASE_URL + "/api/tasks/run_due", timeout=60)
            # 401이면 OAuth 토큰이 web/worker에서 공유 안 되는 경우일 수 있음
            # (이 경우는 token 파일이 worker 환경에 없어서 그래요)
            # 해결: web/worker가 같은 token 파일을 보게 하거나, token을 DB/외부 저장소로 옮겨야 함
            print("run_due:", r.status_code, r.text[:200])
        except Exception as e:
            print("worker error:", e)

        time.sleep(25)

if __name__ == "__main__":
    main()
