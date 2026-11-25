#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取已配置账号的 收件箱未读邮件 → 03-数据/email_candidates.json

设计：
- 只取 Inbox 最近 7 天、未读、非自投/自动回复/验证码；
- 同主题（相似度≥0.82）合并成一条，取最新一封，避免一个活动 3 封邮件刷屏；
- 失败不写坏：保留上一次候选并标 stale，页面会显示来源异常。
"""
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "03-数据" / "email_candidates.json"
HIMALAYA = "himalaya"
ACCOUNT = os.environ.get("HIMALAYA_ACCOUNT", "")
ACCOUNT_EMAIL = os.environ.get("WORKBENCH_EMAIL", "")
DAYS = 7
CAP = 8
INTERNAL_DOMAINS = {ACCOUNT_EMAIL.rpartition("@")[2]} if ACCOUNT_EMAIL else set()

REQ_WORDS = ("申请", "审批", "确认", "回复", "对接", "合同", "报价", "对账", "发票", "排期", "上架")
AUTO_RE = re.compile(r"no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?@|system@", re.I)
JUNK_RE = re.compile(r"验证码|退订|unsubscribe|自动回复|投递失败|undeliver|mail delivery", re.I)
PROJECT_MAP = ()


def load_prev():
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return {"items": [], "status": "none", "account": ACCOUNT_EMAIL}


def norm_subject(s):
    s = re.sub(r"^(回复|答复|Re|RE|Fwd|FW|转发)[:：]\s*", "", s or "").strip()
    return re.sub(r"\s+", " ", s)


def domain_of(addr):
    return (addr or "").split("@")[-1].lower()


def pick_project(text):
    for k, v in PROJECT_MAP:
        if k in text:
            return v
    return ""


def mail_env():
    """邮件走直连：邮箱在国内，HTTP(S)_PROXY 指向未启动的代理会导致 connect 失败。"""
    env = dict(os.environ)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    return env


def main():
    if not ACCOUNT or not ACCOUNT_EMAIL:
        raise SystemExit("请先配置 HIMALAYA_ACCOUNT 和 WORKBENCH_EMAIL")
    quiet = "--quiet" in sys.argv
    prev = load_prev()
    items, err = [], None
    try:
        r = subprocess.run(
            [HIMALAYA, "--json", "envelope", "list", "-a", ACCOUNT, "-m", "Inbox", "-s", "30"],
            capture_output=True, text=True, timeout=90, env=mail_env(),
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "himalaya 失败").strip().splitlines()[-1][:200])
        envs = json.loads(r.stdout or "{}").get("envelopes") or []
        cutoff = datetime.now().astimezone() - timedelta(days=DAYS)
        raw = []
        unread = 0
        for e in envs:
            if not isinstance(e, dict):
                continue
            flags = e.get("flags") or []
            if any((isinstance(f, dict) and f.get("iana") == "seen") for f in flags):
                continue
            unread += 1
            subject = (e.get("subject") or "").strip()
            frm = (e.get("from") or [{}])[0] or {}
            email = (frm.get("email") or "").lower()
            name = (frm.get("name") or "").strip()
            if not subject or not email:
                continue
            if email == ACCOUNT_EMAIL:
                continue
            if AUTO_RE.search(email) or JUNK_RE.search(subject):
                continue
            try:
                dt = datetime.fromisoformat(e.get("date"))
            except Exception:
                continue
            if dt < cutoff:
                continue
            base = norm_subject(subject)
            raw.append({
                "uid": str(e.get("id") or ""), "message_id": e.get("message-id") or "",
                "subject": subject, "base": base, "name": name, "email": email,
                "date": dt, "size": e.get("size") or 0,
                "external": domain_of(email) not in INTERNAL_DOMAINS,
                "request": any(w in subject for w in REQ_WORDS),
            })

        # 同主题聚类：相似度 ≥0.82 视为同一件事，取最新一封
        groups = []
        for it in sorted(raw, key=lambda x: x["date"]):
            for g in groups:
                if difflib.SequenceMatcher(None, it["base"], g[0]["base"]).ratio() >= 0.82:
                    g.append(it)
                    break
            else:
                groups.append([it])

        for g in groups:
            latest = max(g, key=lambda x: x["date"])
            ext = [x for x in g if x["external"]]
            who_src = max(ext, key=lambda x: x["date"]) if ext else latest
            title = re.sub(r"\d{4}$", "", latest["base"]).strip() or latest["subject"]
            flags = []
            if ext:
                flags.append("客户")
            if any(x["request"] for x in g):
                flags.append("待确认")
            ev = "邮件｜" + str(len(g)) + " 封同主题"
            if ext:
                ev += "｜客户 " + (who_src["name"] or who_src["email"]) + " " + who_src["date"].strftime("%m-%d %H:%M") + "：" + who_src["subject"][:60]
            ev += "｜最新 " + (latest["name"] or latest["email"]) + " <" + latest["email"] + ">｜" + latest["date"].strftime("%Y-%m-%d %H:%M")
            items.append({
                "id": "mail:" + hashlib.sha1((latest["message_id"] or latest["uid"]).encode()).hexdigest()[:16],
                "title": title[:120],
                "source": "邮件",
                "project": pick_project(title + " " + " ".join(x["subject"] for x in g)),
                "due": "",
                "kind": "请求" if any(x["request"] for x in g) else "邮件",
                "who": (who_src["name"] or who_src["email"])[:40],
                "evidence": ev[:300],
                "url": "",
                "flags": flags,
                "mail_uid": latest["uid"],
                "mail_count": len(g),
            })

        items.sort(key=lambda x: (0 if "客户" in x["flags"] else 1, x["title"]))
        items = items[:CAP]
        status = "ok"
    except Exception as e:
        err = str(e)[:200]
        items = prev.get("items") or []
        status = "stale" if items else "blocked"

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "account": ACCOUNT_EMAIL,
        "status": status,
        "error": err,
        "candidates": len(items),
        "items": items,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    if err:
        print(f"[邮件] 抓取异常：{err}")
        if status == "stale":
            print("[邮件] 已保留上一次候选，页面标记 stale")
        sys.exit(0 if items else 1)
    if not quiet:
        print(f"[邮件] 未读候选 {len(items)} 条（{ACCOUNT_EMAIL}）→ {OUT}")


if __name__ == "__main__":
    main()
