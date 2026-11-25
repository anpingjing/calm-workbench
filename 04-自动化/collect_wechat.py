#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信候选事项抽取（规则版，不调用 LLM）

输入：local/merged.ndjson（微信流水线每天 08:00 产出）
输出：03-数据/wechat_candidates.json

定位：**只做粗筛**，把 1.4 万条消息收敛到 20–40 条候选；是否真的待办、归到哪个项目，
交给「收件箱整理」agent 任务或人来判断。宁可多留，不要漏。

用法：
    python3 04-自动化/collect_wechat.py --days 7
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "local" / "merged.ndjson"
OUT = ROOT / "03-数据" / "wechat_candidates.json"
INBOX = ROOT / "03-数据" / "inbox.json"


def touch_inbox(payload):
    """只更新来源状态，不动人工挑过的条目。"""
    try:
        inbox = json.loads(INBOX.read_text(encoding="utf-8"))
    except Exception:
        return
    meta = payload.get("meta") or {}
    items = payload.get("items") or []
    sources = inbox.setdefault("sources", [])
    confirmed = sum(1 for i in (inbox.get("items") or []) if i.get("source") == "微信")
    upstream = ""
    try:
        upstream = datetime.fromtimestamp(SRC.stat().st_mtime).strftime("%m-%d %H:%M")
    except Exception:
        pass
    if meta.get("status") == "ok":
        status = "partial" if items and confirmed < len(items) else "ok"
        detail = f"候选 {len(items)} 条 / {meta.get('conversations', 0)} 个会话，已人工确认 {confirmed} 条"
    else:
        status = "blocked"
        detail = "上游 merged.ndjson 不可用"
    now = datetime.now().strftime("%m-%d %H:%M")
    detail += f" · 读取 {now}" + (f" · 上游 {upstream}" if upstream else "")
    entry = {"key": "wechat", "label": "微信", "status": status, "detail": detail,
             "candidates": len(items), "generated_at": datetime.now().isoformat(timespec="seconds")}
    for i, s in enumerate(sources):
        if s.get("key") == "wechat":
            sources[i] = entry
            break
    else:
        sources.insert(0, entry)
    INBOX.write_text(json.dumps(inbox, ensure_ascii=False, indent=1), encoding="utf-8")

BLACK_ROOM = re.compile(
    r"家长|资料|团购|福利|优惠|红包|砍价|拼团|代购|微商|快递|外卖|招聘|广告|课程|"
    r"小学|初中|高中|考研|书友|薅羊毛|内购|分享群|免费|试玩|游戏|早安|行业交流"
)
BOT_SENDER = re.compile(r"管家|客服|助手|机器人|通知|小助手|系统|推送官|公众号")
WORK = re.compile(
    r"项目|对接|商务|保司|供应链|订单|售后|产品|运营|理赔|药|商城|会员|数据|"
    r"财务|合规|合同|结算|采购|渠道|科技|健康|保险|机构"
)
PROMO = re.compile(
    r"优惠券|扫码|满\d+减|秒杀|活动时间|直播|预告|领取|报名|点赞|转发|内购|折扣|特价|"
    r"进群|关注公众号|首充|返货|晒单"
)
REQUEST = re.compile(
    r"麻烦|请(你|您)?|帮我|帮忙|需要你|你那边|能否|尽快|急|截止|今天|明天|本周|之前|"
    r"确认一下|回复|回我|发我|给我|安排|处理|核实|整改|催|看下|看一下"
)
TIME_WORD = re.compile(r"今天|明天|本周|截止|尽快|急|下班前|之前")
NOISE_START = re.compile(r"^(收到|好的|OK|嗯|谢谢|👍|\[|哈)")


def load(days):
    if not SRC.exists():
        return [], {"status": "missing", "path": str(SRC)}
    cut = time.time() - days * 86400
    rows = []
    with SRC.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if float(m.get("ts") or 0) < cut:
                continue
            rows.append(m)
    return rows, {"status": "ok", "path": str(SRC), "window_days": days, "messages_in_window": len(rows)}


def extract(rows):
    cands = []
    for m in rows:
        if str(m.get("msg_type")) != "1":
            continue
        room = (m.get("chat_room") or "").strip()
        sender = (m.get("sender") or "").strip()
        text = (m.get("content") or "").strip()
        if not text or len(text) > 300:
            continue
        if BLACK_ROOM.search(room) or BOT_SENDER.search(sender) or PROMO.search(text):
            continue
        if NOISE_START.match(text):
            continue
        if not WORK.search(room + " " + text):
            continue
        if not REQUEST.search(text):
            continue
        is_self = bool(m.get("is_self"))
        direct = "@chatroom" not in (m.get("conversation_id") or "")
        score = (4 if direct else 1) + (3 if TIME_WORD.search(text) else 0) + (1 if is_self else 0)
        cands.append({
            "score": score,
            "ts": float(m.get("ts") or 0),
            "time": datetime.fromtimestamp(float(m.get("ts") or 0)).strftime("%Y-%m-%d %H:%M"),
            "room": room,
            "sender": sender,
            "self": is_self,
            "direct": direct,
            "text": text,
        })

    # 每个会话最多留 3 条，避免一个群刷屏
    by_room, keep = {}, []
    for c in sorted(cands, key=lambda x: (-x["score"], -x["ts"])):
        n = by_room.get(c["room"], 0)
        if n >= 3:
            continue
        by_room[c["room"]] = n + 1
        keep.append(c)
    keep.sort(key=lambda x: (-x["score"], -x["ts"]))
    return keep


def main():
    days = 7
    for i, a in enumerate(sys.argv):
        if a == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])
    rows, meta = load(days)
    if meta["status"] != "ok":
        print(f"[warn] 微信数据不可用：{meta}")
        payload = {"meta": {**meta, "generated_at": datetime.now().isoformat(timespec="seconds")}, "items": []}
    else:
        items = extract(rows)
        payload = {
            "meta": {**meta, "generated_at": datetime.now().isoformat(timespec="seconds"),
                     "kept": len(items), "conversations": len({i["room"] for i in items})},
            "items": items,
        }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    touch_inbox(payload)
    print(f"微信候选 {len(payload['items'])} 条 / {payload['meta'].get('conversations', 0)} 个会话 → {OUT}")


if __name__ == "__main__":
    main()
