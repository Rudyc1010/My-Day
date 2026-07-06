#!/usr/bin/env python3
"""
Overdue-task checker for the My Day assistant.
Runs on a schedule (GitHub Actions). For every active task with a nag_time
that has passed and no completion today, it sends a push via ntfy.sh.
Nags only within ~35 minutes after the deadline so you get one nag, not spam.
"""
import os
import urllib.request
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Chicago"))

NAG_WINDOW_MIN = 35  # matches the 30-minute schedule, with a little slack


def sb_get(path):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def push(message):
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode(),
        headers={"Title": "My Day", "Priority": "high", "Tags": "alarm_clock"},
        method="POST",
    )
    urllib.request.urlopen(req)


def main():
    now = datetime.now(TZ)
    today = now.date().isoformat()

    tasks = sb_get("tasks?active=eq.true&nag_time=not.is.null")
    comps = sb_get(f"completions?day=eq.{today}")
    done_ids = {c["task_id"] for c in comps}

    nagged = 0
    for t in tasks:
        if t["id"] in done_ids:
            continue
        h, m = int(t["nag_time"][:2]), int(t["nag_time"][3:5])
        deadline = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if deadline <= now < deadline + timedelta(minutes=NAG_WINDOW_MIN):
            push(f"Heads up: “{t['title']}” isn't done yet (deadline was {t['nag_time'][:5]}).")
            nagged += 1

    print(f"{now.isoformat()} - checked {len(tasks)} task(s), sent {nagged} nag(s)")


if __name__ == "__main__":
    main()
