import time, json, os
from datetime import datetime
from server import get_blogger, load_json, save_json, TASK_FILE

def run():
    while True:
        tasks = load_json(TASK_FILE, [])
        now = datetime.utcnow().isoformat()

        for t in tasks:
            if t["status"] != "pending":
                continue
            if t["run_at"] > now:
                continue

            try:
                svc = get_blogger()
                svc.posts().insert(
                    blogId=t["blog_id"],
                    body={"title": t["title"], "content": t["html"]},
                    isDraft=False
                ).execute()
                t["status"] = "ok"
            except Exception as e:
                t["status"] = "err"
                t["error"] = str(e)

        save_json(TASK_FILE, tasks)
        time.sleep(30)

if __name__ == "__main__":
    run()
