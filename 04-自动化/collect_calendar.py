#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书日历采集：今日/本周日程、冲突、空闲块、待办 → 03-数据/calendar.json

前提：本机用本人账号授权（bot 身份读不到你自己的日历）
    lark-cli auth login --domain calendar,task

用法：
    python3 04-自动化/collect_calendar.py            # 今天 + 未来 7 天
    python3 04-自动化/collect_calendar.py --days 14
    python3 04-自动化/collect_calendar.py --fixture path.json   # 离线自测

设计：
1. 只读。不创建/修改任何日程（防守层的时间块走人工确认，见 skill ai-schedule-automation）。
2. 失败不阻断构建：未授权/超时都写成 status=blocked|error + reason，页面据此显示「未授权」，不是空白。
3. 口径固定：工作窗口 09:00–19:00；有效会议 = busy 且 self_rsvp_status != decline；重叠会议分别计入。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "03-数据" / "calendar.json"

WORK_START, WORK_END = 9 * 60, 19 * 60
FRAG_LO, FRAG_HI = 15, 45      # 15–45 分钟空隙算碎片
DEEP_MIN = 90                  # ≥90 分钟算可深度工作块
HINT_AUTH = "lark-cli auth login --domain calendar,task"
CONTACTS = ROOT / "local" / "contacts.json"
ME_NAME = os.environ.get("WORKBENCH_NAME", "")


# ---------- lark-cli ----------
def lark(args, timeout=120):
    try:
        p = subprocess.run(["lark-cli"] + args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return None, {"type": "cli", "message": str(e)}
    raw = (p.stdout or "").strip() or (p.stderr or "").strip()
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        # 失败时 CLI 可能把 JSON 混在 stderr 日志里
        i = raw.find("{")
        try:
            j = json.JSONDecoder().raw_decode(raw[i:])[0] if i >= 0 else None
        except json.JSONDecodeError:
            j = None
        if j is None:
            return None, {"type": "parse", "message": raw[:300]}
    if not j.get("ok"):
        return None, j.get("error") or {"message": "unknown"}
    return j.get("data", j), None


def err_text(e):
    if not isinstance(e, dict):
        return str(e)
    sub = e.get("subtype") or e.get("type") or ""
    msg = e.get("message") or ""
    if sub == "token_missing":
        return "还没有用本人账号授权日历：先跑 " + HINT_AUTH
    missing = e.get("missing_scopes") or []
    if missing:
        return "%s（缺 scope：%s）" % (msg, ", ".join(missing))
    return ("%s: %s" % (sub, msg)) if sub else msg


# ---------- 时间 ----------
def parse_dt(v):
    if v is None:
        return None
    if isinstance(v, dict):
        if v.get("timestamp"):
            try:
                return datetime.fromtimestamp(int(v["timestamp"]))
            except (ValueError, OSError):
                return None
        for k in ("datetime", "date"):
            if v.get(k):
                return parse_dt(v[k])
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(int(v))
    s = str(v).strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s))
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def hm(dt):
    return dt.strftime("%H:%M") if dt else None


def unwrap(obj):
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        return []
    for k in ("data", "items", "events", "result"):
        if k in obj:
            v = obj[k]
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                inner = unwrap(v)
                if inner:
                    return inner
    return []


def normalize(events):
    out = []
    for e in events:
        if not isinstance(e, dict):
            continue
        s, t = parse_dt(e.get("start_time")), parse_dt(e.get("end_time"))
        if not s or not t or t <= s:
            continue  # 全天日程不计入时长
        out.append({
            "id": e.get("event_id") or e.get("id") or "",
            "summary": (e.get("summary") or "(无标题)").strip()[:60],
            "start": s, "end": t,
            "minutes": int((t - s).total_seconds() // 60),
            "busy": (e.get("free_busy_status") or "busy") != "free",
            "rsvp": e.get("self_rsvp_status") or "",
            "recurring": bool(e.get("recurring_event_id")),
        })
    return sorted(out, key=lambda x: x["start"])


def gaps(day_events, day):
    """工作窗口内的空隙 → (分钟列表, 深度块数, 碎片数)"""
    cur = datetime.combine(day, dtime(WORK_START // 60, WORK_START % 60))
    lim = datetime.combine(day, dtime(WORK_END // 60, WORK_END % 60))
    out, deep, frag = [], 0, 0
    for e in day_events:
        s = max(e["start"], cur)
        if s > cur:
            mins = int((s - cur).total_seconds() // 60)
            if mins > 0:
                out.append(mins)
                if mins >= DEEP_MIN:
                    deep += 1
                elif FRAG_LO <= mins <= FRAG_HI:
                    frag += 1
        cur = max(cur, min(e["end"], lim))
    if cur < lim:
        mins = int((lim - cur).total_seconds() // 60)
        if mins > 0:
            out.append(mins)
            if mins >= DEEP_MIN:
                deep += 1
            elif FRAG_LO <= mins <= FRAG_HI:
                frag += 1
    return out, deep, frag


def day_view(day, events):
    same = [e for e in events if e["start"].date() == day and e["busy"] and e["rsvp"] != "decline"]
    conflicts = []
    for i in range(len(same) - 1):
        a, b = same[i], same[i + 1]
        if b["start"] < a["end"]:
            conflicts.append({
                "a": a["summary"], "b": b["summary"],
                "overlap": int((min(a["end"], b["end"]) - b["start"]).total_seconds() // 60),
            })
    g, deep, frag = gaps(same, day)
    return {
        "date": day.isoformat(),
        "weekday": "一二三四五六日"[day.weekday()],
        "events": [{
            "id": e["id"], "summary": e["summary"],
            "start": hm(e["start"]), "end": hm(e["end"]), "minutes": e["minutes"],
            "busy": e["busy"], "rsvp": e["rsvp"], "recurring": e["recurring"],
            "anon": bool(e.get("anon")),
        } for e in sorted(same, key=lambda x: x["start"])],
        "conflicts": conflicts,
        "meetings": len(same),
        "meeting_minutes": sum(e["minutes"] for e in same),
        "first_start": hm(min(e["start"] for e in same)) if same else None,
        "last_end": hm(max(e["end"] for e in same)) if same else None,
        "fragments": frag,
        "deep_blocks": deep,
        "free_minutes": sum(g),
        "back_to_back": sum(1 for i in range(len(same) - 1)
                            if 0 <= (same[i + 1]["start"] - same[i]["end"]).total_seconds() / 60 < 5),
    }


def free_slots(start, end, fixture=None, day=None):
    """今日可用空闲块（≥30 分钟）；day 给定时只保留当天，避免多天窗口混进「今日」"""
    if fixture:
        try:
            body = json.load(open(fixture, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    else:
        body, _ = lark(["calendar", "+freebusy", "--type", "free", "--min-duration", "30m",
                        "--start", start.isoformat(), "--end", end.isoformat()])
        if not body:
            return []
    if isinstance(body, dict) and "data" in body:
        body = body["data"]
    slots = []
    users = body.get("users") or [] if isinstance(body, dict) else []
    for u in users:
        for s in u.get("free") or []:
            a, b = parse_dt(s.get("start_time")), parse_dt(s.get("end_time"))
            if a and b and b > a and (day is None or a.date() == day):
                slots.append({"start": hm(a), "end": hm(b), "minutes": int((b - a).total_seconds() // 60)})
    for s in (body.get("common_free") or []) if isinstance(body, dict) else []:
        a, b = parse_dt(s.get("start_time")), parse_dt(s.get("end_time"))
        if a and b and b > a and (day is None or a.date() == day):
            slots.append({"start": hm(a), "end": hm(b), "minutes": int((b - a).total_seconds() // 60)})
    return sorted(slots, key=lambda x: x["start"])


def free_from_busy(busy, day):
    """工作窗口内、扣掉忙时段后的空闲块（≥30 分钟）。"""
    cur = datetime.combine(day, dtime(WORK_START // 60, WORK_START % 60))
    lim = datetime.combine(day, dtime(WORK_END // 60, WORK_END % 60))
    spans = sorted([b for b in busy if b[1] > cur and b[0] < lim], key=lambda x: x[0])
    out = []
    for a, b in spans:
        a, b = max(a, cur), min(b, lim)
        if a > cur:
            m = int((a - cur).total_seconds() // 60)
            if m >= 30:
                out.append({"start": hm(cur), "end": hm(a), "minutes": m})
        cur = max(cur, b)
    if cur < lim:
        m = int((lim - cur).total_seconds() // 60)
        if m >= 30:
            out.append({"start": hm(cur), "end": hm(lim), "minutes": m})
    return out


def my_union_id():
    """本人 union_id：同租户稳定，应用身份读忙闲要用它（open_id 跨应用会报 cross app）。"""
    env = os.environ.get("LARK_ME_UNION_ID")
    if env:
        return env
    try:
        d = json.load(open(CONTACTS, encoding="utf-8"))
        for c in (d.get("contacts") or {}).values():
            if isinstance(c, dict) and c.get("name") == ME_NAME and c.get("union_id"):
                return c["union_id"]
    except (OSError, json.JSONDecodeError):
        pass
    return None


def busy_intervals(start, end):
    """应用身份读本人忙闲（freebusy/list）。返回 [(start_dt, end_dt)] 或 (None, 错误)。"""
    uid = my_union_id()
    if not uid:
        return None, {"message": "找不到本人 union_id（contacts.json 里没有 %s）" % ME_NAME}
    body = {"time_min": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "time_max": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user_id": uid, "only_busy": True}
    out = subprocess.run(["lark-cli", "api", "POST", "/open-apis/calendar/v4/freebusy/list",
                          "--params", json.dumps({"user_id_type": "union_id"}),
                          "--data", json.dumps(body), "--format", "json"],
                         capture_output=True, text=True, timeout=120)
    raw = (out.stdout or "").strip() or (out.stderr or "").strip()
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        return None, {"message": raw[:200] or "freebusy 无输出"}
    if not j.get("ok"):
        return None, j.get("error") or {"message": "unknown"}
    items = ((j.get("data") or {}).get("freebusy_list")) or []
    spans = []
    for it in items:
        a, b = parse_dt(it.get("start_time")), parse_dt(it.get("end_time"))
        if a and b and b > a:
            spans.append((a, b))
    return spans, None


def my_tasks():
    """我的未完成待办（只读参考，不并入收件箱，避免与 collect_feishu 重复）"""
    end = (date.today() + timedelta(days=14)).isoformat() + "T23:59:59+08:00"
    data, err = lark(["task", "+get-my-tasks", "--due-end", end])
    if err:
        return {"status": "blocked", "reason": err_text(err), "items": []}
    items = []
    for t in unwrap(data)[:20]:
        if not isinstance(t, dict):
            continue
        due = t.get("due") or {}
        ts = due.get("timestamp") if isinstance(due, dict) else None
        items.append({
            "id": t.get("id") or t.get("task_id") or "",
            "summary": (t.get("summary") or t.get("title") or "").strip()[:60],
            "due": datetime.fromtimestamp(int(ts)).strftime("%m-%d") if ts else "",
        })
    return {"status": "ok", "items": [x for x in items if x["summary"]]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--fixture")
    ap.add_argument("--freebusy-fixture")
    args = ap.parse_args()

    today = date.today()
    start = datetime.combine(today, datetime.min.time()) + timedelta(hours=WORK_START // 60)
    end_day = today + timedelta(days=args.days)
    end = datetime.combine(end_day, datetime.min.time())

    payload = {"status": "ok", "synced_at": datetime.now().isoformat(timespec="seconds"),
               "date": today.isoformat(), "days": args.days, "hint": HINT_AUTH, "partial": False,
               "reason": None, "today": None, "week": None, "tasks": {"status": "none", "items": []}}
    partial_busy = None

    if args.fixture:
        try:
            events = normalize(unwrap(json.load(open(args.fixture, encoding="utf-8"))))
        except (OSError, json.JSONDecodeError) as e:
            payload.update({"status": "error", "reason": "fixture 不可用：%s" % e})
            OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            print(json.dumps(payload, ensure_ascii=False)[:300])
            return 0
    else:
        data, err = lark(["calendar", "+agenda", "--start", start.isoformat(), "--end", end.isoformat()])
        if err:
            if err.get("subtype") == "token_missing":
                # 退一步：应用身份读忙闲（无需本人 OAuth），但只有占用时段、没有标题
                spans, bern = busy_intervals(start, end)
                if spans is not None:
                    events = [{"id": "", "summary": "(日程占用)", "start": a, "end": b,
                               "minutes": int((b - a).total_seconds() // 60),
                               "busy": True, "rsvp": "", "recurring": False, "anon": True}
                              for a, b in spans]
                    partial_busy = spans
                    payload.update({"status": "partial",
                                    "reason": "只看到占用时段：标题/参会人需要本人授权（" + HINT_AUTH + "）"})
                else:
                    payload.update({"status": "blocked", "reason": err_text(err)})
                    payload["tasks"] = {"status": "blocked", "reason": err_text(err), "items": []}
                    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                    print("[calendar] %s：%s" % (payload["status"], payload["reason"]))
                    return 0
            else:
                payload.update({"status": "error", "reason": err_text(err)})
                payload["tasks"] = my_tasks()
                OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                print("[calendar] %s：%s" % (payload["status"], payload["reason"]))
                return 0
        else:
            events = normalize(unwrap(data))
            partial_busy = None
    dates = [today + timedelta(days=i) for i in range(args.days + 1)]
    days = [day_view(d, events) for d in dates]
    workdays = [d for d in days if datetime.fromisoformat(d["date"]).weekday() < 5] or days

    payload["today"] = days[0]
    payload["days_detail"] = days
    if partial_busy is not None:
        payload["free_blocks"] = free_from_busy(partial_busy, today)
        payload["partial"] = True
        payload["tasks"] = my_tasks()
    else:
        payload["free_blocks"] = free_slots(start, datetime.combine(today, datetime.min.time()) + timedelta(hours=23, minutes=59),
                                            args.freebusy_fixture, day=today)
    payload["week"] = {
        "workdays": len(workdays),
        "meetings": sum(d["meetings"] for d in workdays),
        "meeting_minutes": sum(d["meeting_minutes"] for d in workdays),
        "meeting_hours": round(sum(d["meeting_minutes"] for d in workdays) / 60, 1),
        "meeting_share": round(sum(d["meeting_minutes"] for d in workdays) /
                               max(len(workdays) * (WORK_END - WORK_START), 1), 3),
        "conflicts": sum(len(d["conflicts"]) for d in workdays),
        "fragments": sum(d["fragments"] for d in workdays),
        "deep_blocks": sum(d["deep_blocks"] for d in workdays),
        "back_to_back": sum(d["back_to_back"] for d in workdays),
    }
    payload["tasks"] = my_tasks()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    t = payload["today"]
    print("[calendar] %s 今日 %d 段 / %d 分钟 / 冲突 %d / 空闲块 %d · 本周占工作窗口 %.0f%%"
          % (payload["status"], t["meetings"], t["meeting_minutes"], len(t["conflicts"]),
             len(payload["free_blocks"]), payload["week"]["meeting_share"] * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main())