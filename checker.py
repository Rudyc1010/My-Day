#!/usr/bin/env python3
"""
Overdue-task checker for the My Day assistant.
Runs on a schedule (GitHub Actions). Sends ntfy pushes for:
  - tasks due today that passed their nag_time without being completed
  - an end-of-day wrap-up (done / skipped / still open)
  - a Sunday weekly review (completion counts, skips, screen-time streak)
Understands recurrence: daily, weekly, biweekly, monthly, once.
"""
import os
import calendar
import urllib.request
import urllib.error
import json
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
MYDAY_EMAIL = os.environ["MYDAY_EMAIL"]
MYDAY_PASSWORD = os.environ["MYDAY_PASSWORD"]
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/Chicago"))

NAG_WINDOW_MIN = 35  # matches the 30-minute schedule, with a little slack
BRIEFING_TIME = os.environ.get("BRIEFING_TIME", "07:00") # morning briefing push
SUMMARY_TIME = os.environ.get("SUMMARY_TIME", "20:30")   # end-of-day wrap-up push
WEEKLY_TIME = os.environ.get("WEEKLY_TIME", "19:00")     # Sunday weekly review push

# ntfy topics are public -- anyone who learns the topic name receives every push.
# "minimal" (the default) sends counts and times but never task titles, so an
# eavesdropper learns nothing personal. Set PUSH_DETAIL=full in checker.yml to
# put the task names back in the notifications.
DETAILED = os.environ.get("PUSH_DETAIL", "minimal").strip().lower() == "full"


def fmt12(t):
    h, m = int(t[:2]), int(t[3:5])
    ap = "PM" if h >= 12 else "AM"
    return f"{(h % 12) or 12}:{m:02d} {ap}"


_TOKEN = None


def token():
    """Sign in once per run and reuse the access token.

    Row Level Security is on, so the anon key alone can no longer read
    anything. Signing in as you gives this script exactly your access --
    and nothing more. (The service_role key would also work here, but it
    bypasses RLS entirely and would be far too much power for a nag bot.)
    """
    global _TOKEN
    if _TOKEN is None:
        body = json.dumps({"email": MYDAY_EMAIL, "password": MYDAY_PASSWORD}).encode()
        req = urllib.request.Request(
            f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
            data=body,
            headers={"apikey": SUPABASE_KEY, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as r:
                _TOKEN = json.loads(r.read())["access_token"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise SystemExit(
                f"Could not sign in to Supabase (HTTP {e.code}). Check the "
                f"MYDAY_EMAIL and MYDAY_PASSWORD repository secrets.\n{detail}"
            )
    return _TOKEN


def sb_get(path):
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {token()}"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def push(message, title="My Day", tags="alarm_clock"):
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode(),
        headers={"Title": title, "Priority": "high", "Tags": tags},
        method="POST",
    )
    urllib.request.urlopen(req)


def due_today(t, today):
    rec = t.get("recurrence") or "daily"
    if rec == "daily":
        return True
    anchor = date.fromisoformat(t["anchor_date"]) if t.get("anchor_date") else today
    diff = (today - anchor).days
    if diff < 0:
        return False
    if rec == "once":
        return True  # due on its date, keeps nagging daily until completed
    if rec == "weekly":
        return today.weekday() == anchor.weekday()
    if rec == "biweekly":
        return today.weekday() == anchor.weekday() and (diff // 7) % 2 == 0
    if rec == "monthly":
        last = calendar.monthrange(today.year, today.month)[1]
        return today.day == min(anchor.day, last)
    return False


def in_window(now, hhmm):
    h, m = map(int, hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t <= now < t + timedelta(minutes=NAG_WINDOW_MIN)


def main():
    now = datetime.now(TZ)
    today = now.date()

    tasks = sb_get("tasks?active=eq.true")
    due = [t for t in tasks if t.get("kind") != "checkin" and due_today(t, today)]

    comps_today = sb_get(f"completions?day=eq.{today.isoformat()}")
    by_id = {c["task_id"]: c for c in comps_today}
    done_ids = set(by_id)

    # one-time tasks count as done if completed on ANY day
    once_ids = [t["id"] for t in due if (t.get("recurrence") or "daily") == "once"]
    if once_ids:
        ever = sb_get("completions?select=task_id&task_id=in.(" + ",".join(once_ids) + ")")
        done_ids |= {c["task_id"] for c in ever}

    # --- overdue nags ---
    nagged = 0
    for t in due:
        if not t.get("nag_time") or t["id"] in done_ids:
            continue
        h, m = int(t["nag_time"][:2]), int(t["nag_time"][3:5])
        deadline = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if deadline <= now < deadline + timedelta(minutes=NAG_WINDOW_MIN):
            if DETAILED:
                push(f"Heads up: “{t['title']}” isn't done yet "
                     f"(deadline was {fmt12(t['nag_time'])}).")
            else:
                push(f"An item was due at {fmt12(t['nag_time'])} and isn't done yet. "
                     f"Open My Day.")
            nagged += 1

    # --- morning briefing ---
    if due and in_window(now, BRIEFING_TIME):
        timed = sorted([t for t in due if t.get("time_of_day")], key=lambda t: t["time_of_day"])
        if DETAILED:
            untimed = [t for t in due if not t.get("time_of_day")]
            parts = ([f"{t['title']} at {fmt12(t['time_of_day'])}" for t in timed]
                     + [t["title"] for t in untimed])
            msg = f"Today ({len(due)}): " + "; ".join(parts)
        else:
            msg = f"{len(due)} item(s) on today's list"
            if timed:
                msg += f", first at {fmt12(timed[0]['time_of_day'])}"
            msg += ". Open My Day for the details."
        push(msg, title="My Day - morning briefing", tags="sunrise")

    # --- end-of-day summary (with skip tracking) ---
    if due and in_window(now, SUMMARY_TIME):
        done = [t["title"] for t in due if by_id.get(t["id"]) and by_id[t["id"]].get("value") != "skip"]
        skipped = [t["title"] for t in due if by_id.get(t["id"]) and by_id[t["id"]].get("value") == "skip"]
        open_ = [t["title"] for t in due if t["id"] not in done_ids]
        msg = f"Day wrap-up: {len(done)}/{len(due)} done."
        if DETAILED:
            if skipped:
                msg += "\nSkipped: " + ", ".join(skipped)
            if open_:
                msg += "\nStill open: " + ", ".join(open_)
        else:
            if skipped:
                msg += f" {len(skipped)} skipped."
            if open_:
                msg += f" {len(open_)} still open."
        push(msg, title="My Day - evening summary", tags="crescent_moon")

    # --- Sunday weekly review ---
    if now.weekday() == 6 and in_window(now, WEEKLY_TIME):
        week = [today - timedelta(days=i) for i in range(6, -1, -1)]
        comps_week = sb_get(f"completions?day=gte.{week[0].isoformat()}")
        lines = []
        tot_done = tot_due = tot_skip = 0
        for t in tasks:
            rec = t.get("recurrence") or "daily"
            if t.get("kind") == "checkin" or rec == "once":
                continue
            due_days = [d for d in week if due_today(t, d)]
            if not due_days:
                continue
            cs = [c for c in comps_week if c["task_id"] == t["id"]]
            dn = sum(1 for c in cs if c.get("value") != "skip")
            sk = sum(1 for c in cs if c.get("value") == "skip")
            tot_done += dn
            tot_due += len(due_days)
            tot_skip += sk
            lines.append(f"{t['title']}: {dn}/{len(due_days)}" + (f" ({sk} skipped)" if sk else ""))
        screen_line = None
        ci = next((t for t in tasks if t.get("kind") == "checkin"), None)
        if ci:
            yes = sum(1 for c in comps_week if c["task_id"] == ci["id"] and c.get("value") == "yes")
            # a bare count, so this one is safe to send either way
            screen_line = f"Screen-time: under limit {yes}/7 days"
        if lines or screen_line:
            if DETAILED:
                body = "Weekly review:\n" + "\n".join(
                    (lines + ([screen_line] if screen_line else []))[:14])
            else:
                body = f"Weekly review: {tot_done}/{tot_due} completed"
                if tot_skip:
                    body += f", {tot_skip} skipped"
                body += "."
                if screen_line:
                    body += "\n" + screen_line
            push(body, title="My Day - weekly review", tags="bar_chart")

    print(f"{now.isoformat()} - {len(due)} task(s) due today, sent {nagged} nag(s)")


if __name__ == "__main__":
    main()
