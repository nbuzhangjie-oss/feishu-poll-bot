#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, json, datetime, threading, uuid, os, requests
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# ============================================================
# 配置
# ============================================================

APP_ID              = os.environ.get("APP_ID", "cli_a96da38ecf7a5cc2")
APP_SECRET          = os.environ.get("APP_SECRET", "J81c4P2GgtPVTx7j6kzNGbO0HnsmiyPf")
CHAT_ID             = os.environ.get("CHAT_ID", "oc_5cbe0bef879d1f9a736dd05d3edf1868")
POLL_DURATION_HOURS = 48
PORT                = int(os.environ.get("PORT", 5000))

SCHEDULES = [
    {
        "name": "每周三MIRC例跑",
        "cron": {"day_of_week": "wed", "hour": 9, "minute": 0},
        "question": "周四晚上18:00 MIRC例跑，科技园西门集合",
        "options": ["参加", "不参加"],
    },
]

# ============================================================
# 共享状态
# ============================================================

votes: dict = {}
votes_lock  = threading.Lock()

# ============================================================
# Token
# ============================================================

_token_cache = {"token": None, "expire_at": 0}

def get_token() -> str:
    import time
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expire_at"] - 60:
        return _token_cache["token"]
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10,
    )
    data = resp.json()
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expire_at"] = now + data.get("expire", 7200)
    return _token_cache["token"]

def send_text(chat_id: str, text: str):
    requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {get_token()}"},
        json={"receive_id": chat_id, "msg_type": "text",
              "content": json.dumps({"text": text}, ensure_ascii=False)},
        timeout=10,
    )

def get_user_name(open_id: str) -> str:
    try:
        resp = requests.get(
            f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}",
            headers={"Authorization": f"Bearer {get_token()}"},
            params={"user_id_type": "open_id"}, timeout=5,
        )
        return resp.json().get("data", {}).get("user", {}).get("name", open_id)
    except Exception:
        return open_id

# ============================================================
# 卡片构建
# ============================================================

EMOJIS = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]

def build_card(poll_id, question, options, records, deadline_str) -> dict:
    counts = [0] * len(options)
    for idx in records.values():
        if 0 <= idx < len(options):
            counts[idx] += 1
    total = sum(counts)

    lines = []
    for i, opt in enumerate(options):
        bar = "█" * int((counts[i] / total * 12) if total > 0 else 0)
        lines.append(f"{EMOJIS[i] if i < len(EMOJIS) else str(i+1)} **{opt}**  {counts[i]}票  {bar}")

    buttons = [
        {"tag": "button",
         "text": {"tag": "plain_text", "content": opt},
         "type": "default",
         "value": {"poll_id": poll_id, "option_index": i}}
        for i, opt in enumerate(options)
    ]

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": f"📊  {question}"}, "template": "blue"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"共 **{total}** 人投票 | 截止 **{deadline_str}**"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "hr"},
            {"tag": "action", "actions": buttons},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "点击按钮参与投票，可随时改选"}]},
        ],
    }

# ============================================================
# 发送投票
# ============================================================

def send_poll(question: str, options: list, chat_id: str = CHAT_ID):
    poll_id      = f"poll_{datetime.datetime.now().strftime('%Y%m%d%H%M')}_{uuid.uuid4().hex[:6]}"
    deadline     = datetime.datetime.now() + datetime.timedelta(hours=POLL_DURATION_HOURS)
    deadline_str = deadline.strftime("%m月%d日 %H:%M")

    with votes_lock:
        votes[poll_id] = {
            "question": question, "options": options,
            "records": {}, "deadline_str": deadline_str, "chat_id": chat_id,
        }

    card = build_card(poll_id, question, options, {}, deadline_str)
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {get_token()}"},
        json={"receive_id": chat_id, "msg_type": "interactive",
              "content": json.dumps(card, ensure_ascii=False)},
        timeout=10,
    )
    result = resp.json()
    if result.get("code") == 0:
        print(f"✅ 投票已发送: {question} ({poll_id})")
    else:
        print(f"❌ 发送失败: {result}")

# ============================================================
# Flask 回调
# ============================================================

flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "polls": len(votes)})

@flask_app.route("/send_test", methods=["GET"])
def send_test():
    send_poll("【测试】机器人配置验证", ["收到 ✅", "没收到 ❌"])
    return jsonify({"status": "ok"})

@flask_app.route("/callback", methods=["POST"])
def callback():
    data = request.get_json(silent=True) or {}

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data.get("challenge", "")})

    if data.get("schema") == "2.0":
        event   = data.get("event", {})
        open_id = event.get("operator", {}).get("open_id", "")
        action  = event.get("action", {})
        chat_id = event.get("context", {}).get("open_chat_id", "") or CHAT_ID
    else:
        open_id = data.get("open_id", "")
        action  = data.get("action", {})
        chat_id = data.get("open_chat_id", "") or CHAT_ID

    value        = action.get("value", {})
    poll_id      = value.get("poll_id")
    option_index = value.get("option_index")

    print(f"收到点击: {open_id} 选{option_index} poll={poll_id} in_votes={poll_id in votes}")

    if poll_id and option_index is not None and poll_id in votes:
        threading.Thread(
            target=process_vote,
            args=(poll_id, open_id, option_index, chat_id),
            daemon=True,
        ).start()

    return jsonify({}), 200


def process_vote(poll_id, open_id, option_index, chat_id):
    try:
        with votes_lock:
            if poll_id not in votes:
                return
            poll      = votes[poll_id]
            is_change = open_id in poll["records"]
            old_index = poll["records"].get(open_id)
            poll["records"][open_id] = option_index
            records   = dict(poll["records"])
            options   = poll["options"]

        counts   = [0] * len(options)
        for idx in records.values():
            if 0 <= idx < len(options):
                counts[idx] += 1
        total    = sum(counts)
        opt_name = options[option_index] if option_index < len(options) else "?"
        name     = get_user_name(open_id)

        if is_change and old_index != option_index:
            old_name = options[old_index] if isinstance(old_index, int) and old_index < len(options) else "?"
            msg = f"🔄 {name} 改选了「{opt_name}」（原：{old_name}）"
        else:
            msg = f"✅ {name} 投票「{opt_name}」"

        summary = "  |  ".join(
            f"{EMOJIS[i] if i < len(EMOJIS) else str(i+1)} {opt}: {counts[i]}票"
            for i, opt in enumerate(options)
        )
        msg += f"\n当前票数（共{total}人）：{summary}"
        print(f"-> {msg}")
        send_text(chat_id, msg)

    except Exception as e:
        import traceback
        print(f"process_vote 异常: {e}")
        traceback.print_exc()

# ============================================================
# 启动定时任务
# ============================================================

def start_scheduler():
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    for cfg in SCHEDULES:
        scheduler.add_job(
            lambda q=cfg["question"], o=cfg["options"]: send_poll(q, o),
            trigger="cron", **cfg["cron"],
            id=cfg["name"], misfire_grace_time=300,
        )
        print(f"已注册定时任务: {cfg['name']}")
    scheduler.start()
    print("定时任务已启动")

start_scheduler()

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)
