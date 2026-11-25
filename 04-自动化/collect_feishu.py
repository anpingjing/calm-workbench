#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞书数据采集：OKR / 我的待办 / 妙记纪要待办 / 群内 @我 → 03-数据/{okr.json,inbox.json}

前提（两步，缺一不可）：
  1. 飞书开发者后台给自己的应用 申请 scope：
     okr:okr.period:readonly, okr:okr.content:readonly,
     minutes:minutes.search:read, task:task:read, im:message:readonly
  2. 本机用本人账号授权：lark-cli auth login --domain okr,task,minutes,im

用法：
    python3 04-自动化/collect_feishu.py --okr        # 只拉 OKR
    python3 04-自动化/collect_feishu.py --tasks      # 只拉我的待办
    python3 04-自动化/collect_feishu.py --minutes    # 只拉妙记待办（近 7 天）
    python3 04-自动化/collect_feishu.py --all        # 全拉（默认）

设计：任何一步失败都不中断；失败原因写进 okr.json / inbox.json 的 sources，
页面「OKR」「收件箱」会直接显示卡在哪一步，而不是静默空白。
"""
import os
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "03-数据" / "inbox.json"
OKR = ROOT / "03-数据" / "okr.json"
CONTACTS = ROOT / "local" / "contacts.json"
APP_ID = os.environ.get("LARK_APP_ID", "")

SCOPE_OKR = ["okr:okr.period:readonly", "okr:okr.content:readonly"]
CONSOLE_OKR = (f"https://open.feishu.cn/page/scope-apply?clientID={APP_ID}"
               "&scopes=okr%3Aokr.period%3Areadonly%2Cokr%3Aokr.content%3Areadonly")


# ---------- lark-cli ----------
def lark(args, timeout=120):
    try:
        p = subprocess.run(["lark-cli"] + args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return None, {"type": "cli", "message": str(e)}
    raw = p.stdout.strip() or p.stderr.strip()
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
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
        return "还没有用本人账号授权：先跑 lark-cli auth login --domain okr,task,minutes,im"
    missing = e.get("missing_scopes") or []
    if missing:
        return f"{msg}（缺 scope：{', '.join(missing)}）"
    return f"{sub}: {msg}" if sub else msg


def my_open_id():
    try:
        d = json.loads(CONTACTS.read_text(encoding="utf-8"))
        for c in (d.get("contacts") or {}).values():
            if isinstance(c, dict) and c.get("name") == os.environ.get("WORKBENCH_NAME", "") and c.get("open_id"):
                return c["open_id"]
    except Exception:
        pass
    data, err = lark(["contact", "+get-user", "--user-id", "me", "--as", "user"])
    if data:
        return (data.get("user") or {}).get("open_id")
    return None


def first_list(data, *keys):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            return v
    for v in data.values():
        if isinstance(v, dict):
            r = first_list(v, *keys)
            if r:
                return r
    return []


def blocks_text(content):
    """把 OKR 富文本（JSON 字符串或对象）里的文字抽出来。"""
    if not content:
        return ""
    obj = content
    if isinstance(content, str):
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return content.strip()
    out = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in ("text", "plain_text", "name") and isinstance(v, str) and v.strip():
                    out.append(v.strip())
                else:
                    walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)

    walk(obj)
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return " ".join(uniq)[:300]


def to_date(v):
    """毫秒时间戳 / ISO / 'YYYY-MM-DD HH:MM:SS' → YYYY-MM-DD"""
    if v in (None, "", 0, "0"):
        return ""
    if isinstance(v, dict):
        v = v.get("timestamp") or v.get("date") or v.get("value") or ""
    try:
        n = int(str(v))
        if n > 1e11:
            n = n / 1000
        return datetime.fromtimestamp(n).date().isoformat()
    except (ValueError, OverflowError, OSError):
        pass
    s = str(v)[:10]
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return ""


# ---------- OKR ----------
def collect_okr():
    open_id = my_open_id()
    if not open_id:
        return None, "拿不到你的 open_id（通讯录与 contact +get-user 都失败）"
    data, err = lark(["okr", "+cycle-list", "--user-id", open_id, "--as", "user"])
    if err:
        return None, err_text(err)
    cycles = first_list(data, "cycles", "items")
    if not cycles:
        return None, "没有查到 OKR 周期（可能账号未启用 OKR，或 scope 不足）"
    today = date.today()

    def span(c):
        return to_date(c.get("start_time")), to_date(c.get("end_time"))

    current = None
    for c in cycles:
        s, e = span(c)
        if s and e and s <= today.isoformat() <= e:
            current = c
            break
    if not current:
        current = cycles[0]
    cid = current.get("id")
    detail, err = lark(["okr", "+cycle-detail", "--cycle-id", str(cid), "--as", "user"])
    if err:
        return None, err_text(err)
    objs = first_list(detail, "objectives", "items")
    objectives = []
    for o in objs:
        krs = []
        for kr in (o.get("key_results") or o.get("krs") or []):
            krs.append({
                "id": str(kr.get("id") or ""),
                "name": blocks_text(kr.get("content")) or "未命名 KR",
                "progress": float(kr.get("score") or 0),
                "weight": kr.get("weight"),
                "due": to_date(kr.get("deadline")),
            })
        objectives.append({
            "id": str(o.get("id") or ""),
            "name": blocks_text(o.get("content")) or "未命名目标",
            "progress": float(o.get("score") or 0),
            "weight": o.get("weight"),
            "krs": krs,
        })
    s, e = span(current)
    payload = {
        "source": "feishu", "status": "ok", "synced_at": datetime.now().isoformat(timespec="seconds"),
        "cycle": {"name": f"{s[:7]} → {e[:7]}", "start": s, "end": e, "id": str(cid)},
        "objectives": objectives, "blocked_reason": "", "need": [], "console_url": CONSOLE_OKR,
    }
    if not objectives:
        payload["status"] = "empty"
        payload["blocked_reason"] = "周期已取到，但周期内没有目标（可能还没写 OKR）"
    OKR.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return f"{len(objectives)} 个目标 / {sum(len(o['krs']) for o in objectives)} 个 KR", None


# ---------- 待办 ----------
def collect_tasks(cap=40):
    data, err = lark(["task", "+get-my-tasks", "--as", "user", "--page-all"])
    if err:
        return [], err_text(err)
    tasks = first_list(data, "items", "tasks")
    items = []
    for t in tasks:
        if t.get("completed_at") or t.get("completed"):
            continue
        title = t.get("summary") or t.get("description") or ""
        if not title:
            continue
        items.append({
            "id": "ft-" + str(t.get("id") or len(items)),
            "title": title.strip()[:160],
            "source": "飞书任务",
            "project": "",
            "due": to_date(t.get("due")),
            "kind": "待办",
            "who": "",
            "flags": [],
            "evidence": "飞书任务" + ("· 截止 " + to_date(t.get("due")) if t.get("due") else ""),
            "url": (t.get("url") or ""),
        })
        if len(items) >= cap:
            break
    return items, None


# ---------- 妙记 ----------
def collect_minutes(days=7, cap_minutes=8, cap_todos=20):
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=days)).isoformat()
    data, err = lark(["minutes", "+search", "--start", start, "--end", end, "--as", "user", "--page-size", "30"])
    if err:
        return [], err_text(err)
    mins = first_list(data, "minutes", "items")
    items = []
    for m in mins[:cap_minutes]:
        token = m.get("minute_token") or m.get("token") or m.get("id")
        if not token:
            continue
        d, err2 = lark(["minutes", "+detail", "--minute-tokens", str(token), "--todo", "--summary", "--as", "user"], timeout=90)
        if err2 or not d:
            continue
        title = m.get("title") or m.get("topic") or "会议纪要"
        todos = first_list(d, "todos", "todo", "todo_list")
        for td in todos:
            text = td.get("content") or td.get("text") or td.get("title") or ""
            if isinstance(text, dict):
                text = blocks_text(text)
            if not text:
                continue
            items.append({
                "id": "fm-" + str(td.get("id") or len(items)),
                "title": str(text).strip()[:160],
                "source": "飞书纪要",
                "project": "",
                "due": to_date(td.get("due") or td.get("deadline")),
                "kind": "纪要待办",
                "who": "",
                "flags": [],
                "evidence": f"妙记《{title}》（{start}~{end}）",
                "url": m.get("url") or "",
            })
            if len(items) >= cap_todos:
                return items, None
    return items, None


# ---------- 合并 ----------
def merge_inbox(new_items, statuses):
    try:
        inbox = json.loads(INBOX.read_text(encoding="utf-8"))
    except Exception:
        inbox = {"sources": [], "items": [], "curator": "auto"}
    keep = [i for i in (inbox.get("items") or []) if i.get("source") not in ("飞书任务", "飞书纪要", "飞书群", "OKR")]
    seen = {i.get("id") for i in keep}
    merged = keep + [i for i in new_items if i.get("id") not in seen]
    src_map = {s.get("key"): s for s in (inbox.get("sources") or [])}
    for key, st in statuses.items():
        src_map[key] = st
    order = ["wechat", "feishu_task", "feishu_minutes", "feishu_im", "okr"]
    inbox["sources"] = [src_map[k] for k in order if k in src_map] + [v for k, v in src_map.items() if k not in order]
    inbox["items"] = merged
    inbox["generated_at"] = datetime.now().isoformat(timespec="seconds")
    INBOX.write_text(json.dumps(inbox, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(merged)


def main():
    do_all = "--all" in sys.argv or len(sys.argv) == 1
    results, items, statuses = [], [], {}

    if do_all or "--okr" in sys.argv:
        msg, err = collect_okr()
        results.append(("OKR", msg, err))
        statuses["okr"] = {"key": "okr", "label": "飞书 OKR",
                           "status": "ok" if msg else "blocked",
                           "detail": msg or err or "", "candidates": 0}
    if do_all or "--tasks" in sys.argv:
        got, err = collect_tasks()
        results.append(("待办", f"{len(got)} 条", err))
        items += got
        statuses["feishu_task"] = {"key": "feishu_task", "label": "飞书待办",
                                   "status": "ok" if not err else "blocked",
                                   "detail": f"{len(got)} 条" if not err else err, "candidates": len(got)}
    if do_all or "--minutes" in sys.argv:
        got, err = collect_minutes()
        results.append(("纪要", f"{len(got)} 条待办", err))
        items += got
        statuses["feishu_minutes"] = {"key": "feishu_minutes", "label": "飞书纪要",
                                      "status": "ok" if not err else "blocked",
                                      "detail": f"{len(got)} 条待办" if not err else err, "candidates": len(got)}

    if items or statuses:
        n = merge_inbox(items, statuses)
        print(f"inbox 合并后 {n} 项")
    for name, msg, err in results:
        print(f"{name}: {'FAIL ' + err_text(err) if err else 'OK ' + str(msg)}")


if __name__ == "__main__":
    main()
