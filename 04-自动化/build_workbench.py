#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作台数据构建：扫描项目卡 / 近30天产出 / Hermes 定时任务 → 生成 data.json 与 工作台.html

用法：
    python3 04-自动化/build_workbench.py            # 正常构建
    python3 04-自动化/build_workbench.py --dry-run  # 只打印统计，不写文件

设计原则：
1. 项目卡（01-项目/*.md 的 frontmatter）是唯一事实源，本脚本只读。
2. 页面是纯静态单文件，数据在构建时内嵌（file:// 下不能 fetch）。
3. 任何一步失败都不阻断整体：分段 try，缺数据就给空列表。
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / "local"
TASK_ROOTS = [ROOT / "02-产出"]
CRON_JOBS = LOCAL / "hermes" / "cron" / "jobs.json"
STATE_DB = LOCAL / "hermes" / "state.db"
INBOX = ROOT / "03-数据" / "inbox.json"
NEXT_FILE = ROOT / "03-数据" / "next_overrides.json"
EMAIL = ROOT / "03-数据" / "email_candidates.json"
WECHAT = ROOT / "03-数据" / "wechat_candidates.json"
WX_SRC = LOCAL / "merged.ndjson"
OKR = ROOT / "03-数据" / "okr.json"
OKR_MANUAL = ROOT / "03-数据" / "okr_manual.json"
CALENDAR = ROOT / "03-数据" / "calendar.json"
TEMPLATE = ROOT / "04-自动化" / "workbench_template.html"
OUT_HTML = ROOT / "工作台.html"
OUT_JSON = ROOT / "03-数据" / "data.json"
SNAP_DIR = ROOT / "03-数据" / "snapshots"
DAYLOG_DIR = ROOT / "00-今日"
DAYLOG_SOURCES = [LOCAL / "logs"]
DAYLOG_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-日志(?: \(\d+\))?\.md$")
REASONS = ["需求变更", "时间不足", "等待确认", "被插单", "其他"]
WORKDAY_MIN = 360  # 可用工时（分钟），页面据此判断今日估时是否超载
AWAIT_KINDS = {"请求", "审核", "邀请", "@我"}
AWAIT_FLAGS = {"客户", "老板"}
# 「出题」相关默认隐藏：项目卡写 hidden: true，或名称/tags 命中关键词；任务按标题/项目/依据/下一步命中
HIDE_KEYWORDS = ("出题", "题目", "考点", "密考", "命题", "题库")
REPORTS = ROOT / "03-数据" / "reports.json"
CRON_HIST = ROOT / "03-数据" / "cron_history.json"
# 报表 key → 月度目标的默认叫法；reports.json 里 month.label 可覆盖
GOAL_LABELS = {"cend": "月自付（渠道口径）", "selfpay": "月自付（自付口径）", "claims": "月度核销率"}


def is_hidden_text(*parts):
    s = " ".join(str(x) for x in parts if x)
    return any(k in s for k in HIDE_KEYWORDS)

DOC_EXT = {".md", ".docx", ".pptx", ".xlsx", ".pdf", ".html"}
SKIP_PARTS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", ".idea",
    "99-归档", "06-参考", "_workbuddy_raw", "平静的个人工作台",
    "snapshots", "Library", "Trash",
}
OUTPUT_DAYS = 30
OUTPUT_CAP = 200


# ---------- 项目卡 ----------
def parse_frontmatter(text: str):
    fm, body = {}, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]
            for line in parts[1].splitlines():
                line = line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                v = v.strip().strip('"').strip("'")
                if v.startswith("[") and v.endswith("]"):
                    v = [x.strip() for x in v[1:-1].split(",") if x.strip()]
                fm[k.strip()] = v
    return fm, body.strip()


def load_projects():
    out = []
    for p in sorted((ROOT / "01-项目").glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            fm, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] 项目卡读取失败 {p.name}: {e}")
            continue
        if not fm.get("name"):
            fm["name"] = p.stem
        tags = fm.get("tags") or []
        hidden = str(fm.get("hidden", "")).strip().lower() in ("true", "1", "yes") \
            or is_hidden_text(fm.get("name"), " ".join(tags))
        status = fm.get("status") or "推进中"
        out.append({
            "name": fm.get("name"),
            "stage": fm.get("stage", ""),
            "status": status,
            "next": fm.get("next", ""),
            "blocker": fm.get("blocker", "无"),
            "updated": fm.get("updated", ""),
            "deadline": fm.get("deadline", ""),
            "dir": fm.get("dir", ""),
            "tags": tags,
            "hidden": hidden,
            "file": str(p.relative_to(ROOT)),
        })
    return out


# ---------- 近 30 天产出 ----------
def scan_outputs():
    cutoff = time.time() - OUTPUT_DAYS * 86400
    rows, seen = [], set()
    for root in TASK_ROOTS:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_PARTS and not d.startswith(".")]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() not in DOC_EXT:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff or st.st_size < 800:
                    continue
                rel = p.relative_to(root)
                parts = rel.parts
                if not parts or parts[0] in SKIP_PARTS:
                    continue
                key = str(p)
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "name": fn,
                    "path": str(p),
                    "project": parts[0],
                    "src": "本地产出",
                    "mtime": round(st.st_mtime * 1000),
                    "size": st.st_size,
                })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


# ---------- Hermes 定时任务 ----------
def humanize(sched: str) -> str:
    s = (sched or "").strip()
    m = re.match(r"^every\s+(\d+)\s*m$", s, re.I)
    if m:
        return "每小时" if m.group(1) == "60" else f"每 {m.group(1)} 分钟"
    m = re.match(r"^every\s+(\d+)\s*h$", s, re.I)
    if m:
        return f"每 {m.group(1)} 小时"
    if re.match(r"^every hour$", s, re.I):
        return "每小时"
    m = re.match(r"^every day at (\d{1,2})(am|pm)$", s, re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(2).lower() == "pm" else 0)
        return f"每天 {h:02d}:00"
    parts = s.split()
    if len(parts) == 5:
        mi, hr, dom, mon, dow = parts
        if mi.isdigit() and hr.startswith("*/") and hr[2:].isdigit():
            n = int(hr[2:])
            return "每小时" if n == 1 else f"每 {n} 小时"
        if mi.isdigit() and hr.isdigit():
            hh, mm = int(hr), int(mi)
            base = f"{hh:02d}:{mm:02d}"
            if dow in ("*", "?"):
                return f"每天 {base}"
            if dow in ("1-5", "1,2,3,4,5"):
                return f"工作日 {base}"
            if dow in ("0", "7"):
                return f"周日 {base}"
            if dow.isdigit():
                names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
                return f"每{names[int(dow) % 7]} {base}"
            return f"{base}（{dow}）"
    return s or "—"


def load_cron():
    if not CRON_JOBS.exists():
        return []
    try:
        raw = json.loads(CRON_JOBS.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[warn] 定时任务读取失败: {e}")
        return []
    jobs = raw.get("jobs") if isinstance(raw, dict) else raw
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    out = []
    for j in jobs or []:
        if not isinstance(j, dict):
            continue
        sched = j.get("schedule") or ""
        if isinstance(sched, dict):
            sched = sched.get("expr") or sched.get("display") or sched.get("kind") or ""
        sched = str(sched)
        last_run = j.get("last_run_at")
        if isinstance(last_run, str):
            try:
                last_run = round(datetime.fromisoformat(last_run).timestamp() * 1000)
            except ValueError:
                last_run = None
        elif isinstance(last_run, (int, float)):
            last_run = round(last_run * 1000)
        else:
            last_run = None
        out.append({
            "name": j.get("name") or j.get("id") or "未命名",
            "schedule": sched,
            "schedule_h": humanize(sched),
            "enabled": bool(j.get("enabled", True)),
            "last_status": j.get("last_status"),
            "last_run": last_run,
            "no_agent": bool(j.get("no_agent")),
        })
    out.sort(key=lambda x: (not x["enabled"], x["schedule_h"]))
    return out


# ---------- 会话数 ----------
def count_sessions(days=7):
    if not STATE_DB.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=3)
        cut = (datetime.now() - timedelta(days=days)).timestamp()
        n = con.execute(
            "select count(*) from sessions where started_at > ? and source != 'cron'", (cut,)
        ).fetchone()[0]
        con.close()
        return int(n)
    except Exception as e:
        print(f"[warn] 会话统计失败: {e}")
        return 0


def count_sessions_range(start: date, end: date):
    """[start, end) 两个自然日之间的非 cron 会话数。"""
    if not STATE_DB.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=3)
        a = datetime.combine(start, dtime.min).timestamp()
        b = datetime.combine(end, dtime.min).timestamp()
        n = con.execute(
            "select count(*) from sessions where started_at >= ? and started_at < ? and source != 'cron'", (a, b)
        ).fetchone()[0]
        con.close()
        return int(n)
    except Exception as e:
        print(f"[warn] 会话区间统计失败: {e}")
        return 0


# ---------- 日终日志：回收 + 解析（收工复盘的唯一历史源） ----------
def ingest_daylogs(verbose=True):
    """把浏览器下载的「YYYY-MM-DD-日志.md」收回 00-今日/YYYY-MM/。

    页面只导出、不写文件；回收由构建脚本做，避免双写。
    目标文件比下载文件更新时跳过，防止冲掉手工改过的日志。
    """
    moved = []
    DAYLOG_DIR.mkdir(parents=True, exist_ok=True)
    for src_root in DAYLOG_SOURCES:
        if not src_root.exists():
            continue
        for f in sorted(src_root.glob("*-日志.md")):
            m = DAYLOG_NAME.match(f.name)
            if not m:
                continue
            try:
                day = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            dst_dir = DAYLOG_DIR / day.strftime("%Y-%m")
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / f"{m.group(1)}.md"
            try:
                if dst.exists() and dst.stat().st_mtime >= f.stat().st_mtime:
                    f.unlink()
                    continue
                shutil.move(str(f), str(dst))
                try:
                    moved.append(str(dst.relative_to(ROOT)))
                except ValueError:
                    moved.append(str(dst))
            except OSError as e:
                print(f"[warn] 日志回收失败 {f.name}: {e}")
    if verbose and moved:
        print(f"回收日终日志 {len(moved)} 份：" + ", ".join(moved[-5:]))
    return moved


ITEM_RE = re.compile(r"^- \[( |x|X)\]\s*(.+?)\s*$")
REASON_RE = re.compile(r"[（(]\s*原因\s*[:：]\s*([^）)]+?)\s*[）)]")
EST_RE = re.compile(r"[（(]\s*(\d+)\s*分钟\s*[）)]")
OUTPUT_RE = re.compile(r"[（(]\s*产出\s*[:：]\s*([^）)]+?)\s*[）)]")


def parse_daylog(path: Path):
    """解析单份日终日志：三件事的计划/完成、未完成原因、复盘一句话。"""
    m0 = re.match(r"^(\d{4}-\d{2}-\d{2})", path.stem)
    if not m0:
        return None
    day = m0.group(1)
    try:
        date.fromisoformat(day)
    except ValueError:
        return None
    planned = done = 0
    reasons, review, nexts, links = {}, "", {}, {}
    items, todos = [], []
    closed = False
    sec = ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    closed = bool(re.search(r"^closed:\s*true", "\n".join(lines[:15]), re.M | re.I))
    for line in lines:
        s = line.strip()
        if s.startswith("## "):
            sec = s[3:].strip()
            continue
        if s == "---":
            sec = ""
            continue
        if sec == "复盘" and s and not s.startswith("-"):
            review = (review + " " + s).strip() if review else s
            continue
        if sec == "下一步补充" and s.startswith("-"):
            mm = re.match(r"-\s*(\S+)\s*[|｜]\s*(.+)", s)
            if mm and mm.group(2).strip():
                nexts[mm.group(1)] = mm.group(2).strip()[:200]
            continue
        if sec == "任务来源" and s.startswith("-"):
            mm = re.match(r"-\s*(.+?)\s*[|｜]\s*(\S+)(?:\s*[|｜]\s*(.+))?$", s)
            if mm:
                links[mm.group(1).strip()[:200]] = {"id": mm.group(2).strip()[:64], "output": (mm.group(3) or "").strip()[:500]}
            continue
        if sec == "待办":
            m = ITEM_RE.match(s)
            if not m:
                continue
            text = EST_RE.sub("", m.group(2)).strip()
            om = OUTPUT_RE.search(text)
            output = om.group(1).strip()[:500] if om else ""
            text = REASON_RE.sub("", OUTPUT_RE.sub("", text)).strip()
            if text:
                todos.append({"t": text, "done": m.group(1).lower() == "x", "output": output})
            continue
        if sec != "三件事":
            continue
        m = ITEM_RE.match(s)
        if not m:
            continue
        text = m.group(2)
        text = EST_RE.sub("", text).strip()
        om = OUTPUT_RE.search(text)
        output = om.group(1).strip()[:500] if om else ""
        if om:
            text = OUTPUT_RE.sub("", text).strip()
        rm = REASON_RE.search(text)
        if rm:
            r = rm.group(1).strip()
            text = REASON_RE.sub("", text).strip()
            if text:
                reasons[r] = reasons.get(r, 0) + 1
        elif m.group(1).lower() != "x" and text:
            reasons["未标原因"] = reasons.get("未标原因", 0) + 1
        if text:
            planned += 1
            if m.group(1).lower() == "x":
                done += 1
            items.append({"t": text, "done": m.group(1).lower() == "x",
                          "reason": rm.group(1).strip() if rm else "", "output": output})
    return {
        "date": day,
        "planned": planned,
        "done": done,
        "closed": closed,
        "rate": round(done / planned * 100) if planned else 0,
        "reasons": reasons,
        "review": review[:200],
        "nexts": nexts,
        "items": items,
        "todos": todos,
        "links": links,
    }


def load_review(days=14):
    rows = []
    for p in sorted(DAYLOG_DIR.glob("*/*.md")):
        row = parse_daylog(p)
        if row:
            rows.append(row)
    all_rows = [r for r in rows if r["planned"]]
    closed_rows = [r for r in all_rows if r.get("closed")]
    since = closed_rows[0]["date"] if closed_rows else None
    rows = all_rows[-days:]
    reason_counts = {r: 0 for r in REASONS}
    for row in closed_rows[-days:]:
        for k, v in row["reasons"].items():
            reason_counts[k] = reason_counts.get(k, 0) + v
    total_planned = sum(r["planned"] for r in closed_rows[-days:])
    total_done = sum(r["done"] for r in closed_rows[-days:])
    done_sources = {}
    for row in rows:
        links = row.get("links") or {}
        for item in (row.get("items") or []) + (row.get("todos") or []):
            if not item.get("done"):
                continue
            lk = links.get(item.get("t") or "")
            if lk and lk.get("id"):
                done_sources[lk["id"]] = {"date": row.get("date"), "output": lk.get("output") or item.get("output") or ""}
    return {
        "days": rows,
        "reason_counts": reason_counts,
        "reason_order": REASONS,
        "covered": len(closed_rows[-days:]),
        "unclosed": sum(1 for r in rows if not r.get("closed")),
        "planned": total_planned,
        "done": total_done,
        "since": since,
        "done_sources": done_sources,
        "rate": round(total_done / total_planned * 100) if total_planned else None,
        "caliber": "完成率只算「已收口」的日志（导出时写 closed: true）：已完成 / 已填；未收口的天不计入分母，以免把没导出的日子当成 0 分。",
        "source_note": "历史来自 00-今日/*.md（收工页「下载今日日志」→ 构建时自动回收）",
    }


def load_next_overrides():
    """「下一步动作」补充：文件里已有的 + 日终日志里新增的（后者优先，旧的先读）。"""
    base = {}
    if NEXT_FILE.exists():
        try:
            base = json.loads(NEXT_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] next_overrides.json 解析失败: {e}")
    out = {}
    if isinstance(base, dict):
        out = {str(k): str(v)[:200] for k, v in base.items() if k and str(v).strip()}
    for p in sorted(DAYLOG_DIR.glob("*/*.md")):
        row = parse_daylog(p)
        if not row:
            continue
        for k, v in (row.get("nexts") or {}).items():
            if v:
                out[k] = v[:200]
    return out


def load_templates():
    out = []
    tdir = ROOT / "05-模板"
    if not tdir.exists():
        return out
    for p in sorted(tdir.glob("*.md")):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            "name": p.stem,
            "file": p.name,
            "path": str(p),
            "mtime": round(st.st_mtime * 1000),
        })
    return out


def load_carryover(max_back=3):
    """上一份日志里未完成的承诺/待办 → 今日「可结转」清单。
    只看不写：是否占承诺位由人点，脚本不替用户承诺。
    今日日志已存在时返回空（今天已经写过，不打扰）。"""
    d0 = date.today()
    if (ROOT / "00-今日" / d0.strftime("%Y-%m") / (d0.isoformat() + ".md")).exists():
        return {"from": "", "items": [], "todos": [], "note": "今日日志已存在"}
    for back in range(1, max_back + 1):
        d = d0 - timedelta(days=back)
        p = ROOT / "00-今日" / d.strftime("%Y-%m") / (d.isoformat() + ".md")
        if not p.exists():
            continue
        sec, items, todos = "", [], []
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            s = line.strip()
            if s.startswith("## "):
                sec = s[3:].strip()
                continue
            m = ITEM_RE.match(s)
            if not m or m.group(1).lower() == "x":
                continue
            if sec not in ("三件事", "待办"):
                continue
            text = m.group(2)
            em = EST_RE.search(text)
            est = min(600, int(em.group(1))) if em else 0
            om = OUTPUT_RE.search(text)
            output = om.group(1).strip()[:500] if om else ""
            text = REASON_RE.sub("", OUTPUT_RE.sub("", EST_RE.sub("", text))).strip()
            if not text:
                continue
            rec = {"t": text, "est": est}
            if output:
                rec["output"] = output
            (items if sec == "三件事" else todos).append(rec)
        if items or todos:
            try:
                links = (parse_daylog(p) or {}).get("links") or {}
            except Exception:
                links = {}
            for rec in items + todos:
                lk = links.get(rec.get("t") or "")
                if lk:
                    rec["from"] = lk.get("id") or ""
                    if lk.get("output"):
                        rec["output"] = lk["output"]
            closed = bool(re.search(r"^closed:\s*true", "\n".join(lines[:15]), re.M | re.I))
            return {"from": d.isoformat(), "items": items[:3], "todos": todos[:20],
                    "unclosed": not closed, "note": "上一份日志里的未完成项"}
    return {"from": "", "items": [], "todos": [], "note": "近 %d 天没有可结转的日志" % max_back}


def load_today():
    """读取今日日志 md 的「三件事」，作为页面首次打开时的种子。"""
    d = date.today()
    p = ROOT / "00-今日" / d.strftime("%Y-%m") / f"{d.isoformat()}.md"
    three = []
    if not p.exists():
        return {"date": d.isoformat(), "three": three, "file": ""}
    sec = ""
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            sec = line[3:].strip()
            continue
        if sec == "三件事" and line.strip().startswith("- ["):
            m = re.match(r"- \[( |x|X)\]\s*(.+)", line.strip())
            if m:
                raw = m.group(2)
                em = EST_RE.search(raw)
                est = min(600, int(em.group(1))) if em else 0
                om = OUTPUT_RE.search(raw)
                output = om.group(1).strip()[:500] if om else ""
                text = EST_RE.sub("", raw).strip()
                text = REASON_RE.sub("", OUTPUT_RE.sub("", text)).strip()
                if text:
                    three.append({"t": text, "done": m.group(1).lower() == "x", "est": est, "output": output})
    return {"date": d.isoformat(), "three": three[:3], "file": str(p.relative_to(ROOT))}


SOURCE_W = {"飞书任务": 25, "飞书纪要": 18, "飞书群": 15, "微信": 15, "邮件": 20, "异常": 40, "项目": 20, "OKR": 20}
FLAG_W = {"客户": 15, "老板": 25, "OKR": 10, "承诺": 10}


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[warn] 读取 {path.name} 失败: {e}")
        return default


def score_item(it, today):
    reasons = []
    score = SOURCE_W.get(it.get("source"), 10)
    due = it.get("due") or ""
    if due:
        try:
            days = (date.fromisoformat(due) - today).days
        except ValueError:
            days = None
        if days is not None:
            if days < 0:
                score += 100 + min(20, -days * 2)
                reasons.append(f"逾期 {-days} 天")
            elif days == 0:
                score += 80
                reasons.append("今天到期")
            elif days <= 2:
                score += 60
                reasons.append(f"{days} 天内到期")
            elif days <= 7:
                score += 35
                reasons.append(f"{days} 天后到期")
            else:
                score += 15
    for f in it.get("flags") or []:
        if f in FLAG_W:
            score += FLAG_W[f]
            reasons.append(f)
    if it.get("kind") == "承诺":
        score += 10
        reasons.append("自己承诺")
    if it.get("source") == "邮件" and it.get("kind") == "请求":
        score += 25
        reasons.append("邮件待回应")
    if it.get("source") == "项目" and it.get("status") == "卡点":
        score += 40
        reasons.append("卡点")
    if it.get("stale"):
        score += 10
        reasons.append("7 天未更新")
    it["score"] = score
    it["level"] = "P0" if score >= 100 else ("P1" if score >= 55 else "P2")
    it["reasons"] = reasons
    return it


def build_priority(projects, inbox, okr, today, overrides=None):
    items = []
    ov = overrides or {}
    for raw in inbox.get("items") or []:
        if not isinstance(raw, dict) or not raw.get("title"):
            continue
        iid = raw.get("id") or raw["title"][:24]
        items.append({
            "id": iid,
            "title": raw["title"], "source": raw.get("source") or "收件箱",
            "project": raw.get("project") or "", "due": raw.get("due") or "",
            "kind": raw.get("kind") or "待办", "who": raw.get("who") or "",
            "evidence": raw.get("evidence") or "", "url": raw.get("url") or "",
            "next": ov.get(iid) or raw.get("next") or "",
            "flags": raw.get("flags") or [],
            "awaiting": (raw.get("kind") or "") in AWAIT_KINDS
                        or bool(set(raw.get("flags") or []) & AWAIT_FLAGS),
            "hidden": is_hidden_text(raw.get("title"), raw.get("project"), raw.get("evidence"), raw.get("next")),
        })
    seen = {i["title"] for i in items}
    for p in projects:
        if not p.get("next") or p["next"] in seen:
            continue
        items.append({
            "id": "proj:" + p["name"], "title": p["next"], "source": "项目",
            "project": p["name"], "due": p.get("deadline") or "", "kind": "下一步",
            "who": "", "evidence": f"项目卡：{p.get('file','')}", "url": "",
            "flags": [], "status": p.get("status"),
            "stale": bool(p.get("updated")) and (today - date.fromisoformat(p["updated"])).days > 7,
            "hidden": bool(p.get("hidden")),
        })
        seen.add(p["next"])
    for obj in (okr.get("objectives") or []):
        for kr in obj.get("krs") or []:
            if kr.get("queue") is False:
                continue
            try:
                prog = float(kr.get("progress") or 0)
            except (TypeError, ValueError):
                prog = 0
            if prog >= 1:
                continue
            items.append({
                "id": "okr:" + str(kr.get("id") or kr.get("name"))[:24], "title": kr.get("name") or "",
                "source": "OKR", "project": obj.get("name") or "", "due": kr.get("due") or "",
                "kind": "KR", "who": kr.get("owner") or "",                "evidence": f"进度 {round(prog*100)}%",
                "url": "", "flags": ["OKR"],
                "hidden": is_hidden_text(obj.get("name"), kr.get("name")),
            })
    for it in items:
        score_item(it, today)
    items.sort(key=lambda x: (-x["score"], x.get("due") or "9999"))
    visible = [i for i in items if not i.get("hidden")]
    counts = {"P0": sum(1 for i in visible if i["level"] == "P0"),
              "P1": sum(1 for i in visible if i["level"] == "P1"),
              "P2": sum(1 for i in visible if i["level"] == "P2")}
    return items, counts


def build_trend(outputs, days=14):
    today = date.today()
    buckets = {today - timedelta(days=i): 0 for i in range(days - 1, -1, -1)}
    for o in outputs:
        d = datetime.fromtimestamp(o["mtime"] / 1000).date()
        if d in buckets:
            buckets[d] += 1
    labels = [d.strftime("%Y-%m-%d") for d in buckets]
    return {"labels": labels, "values": list(buckets.values())}


def week_stats(all_outputs, review, blockers):
    """本周 vs 上周：只放能回溯的指标，算不出来的显式留空。"""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    last_monday = monday - timedelta(days=7)

    def outs(a, b):
        lo = datetime.combine(a, dtime.min).timestamp() * 1000
        hi = datetime.combine(b, dtime.min).timestamp() * 1000
        return sum(1 for o in all_outputs if lo <= o["mtime"] < hi)

    def delta(cur, prev):
        if not prev:
            return None
        return round((cur - prev) / prev * 100)

    o_this = outs(monday, today + timedelta(days=1))
    o_prev = outs(last_monday, monday)
    s_this = count_sessions_range(monday, today + timedelta(days=1))
    s_prev = count_sessions_range(last_monday, monday)
    wk = [d for d in (review.get("days") or []) if d["date"] >= monday.isoformat()]
    wk_closed = [d for d in wk if d.get("closed")]
    wk_open = [d for d in wk if not d.get("closed")]
    rp = sum(d["planned"] for d in wk_closed)
    rd = sum(d["done"] for d in wk_closed)
    return {
        "start": monday.isoformat(),
        "end": (monday + timedelta(days=6)).isoformat(),
        "outputs_this": o_this, "outputs_prev": o_prev, "outputs_delta": delta(o_this, o_prev),
        "sessions_this": s_this, "sessions_prev": s_prev, "sessions_delta": delta(s_this, s_prev),
        "review_days": len(wk_closed),
        "review_unclosed": len(wk_open),
        "review_rate": round(rd / rp * 100) if rp else None,
        "review_since": review.get("since"),
        "blockers": blockers,
        "note": "文档改动按文件更新时间计（含修订，不等于新交付）；会话按非 cron 会话计；完成率只算已收口日志；卡点环比要等下周快照。",
    }


def detect_anomalies(m, reports, email, review, projects, today, calendar=None):
    """数据异常 → 带等级的待办。阈值写死在这里，页面只展示。"""
    out = []

    def add(level, key, title, evidence, action):
        out.append({"id": "anom:" + key, "level": level, "key": key,
                    "title": title, "evidence": evidence, "action": action})

    reps = [r for r in (reports.get("reports") or []) if isinstance(r, dict)]
    ok = [r for r in reps if r.get("status") == "ok"]
    if not reps:
        add("P0", "reports_missing", "报表快照不存在", "collect_reports.py 从未成功运行", "跑 04-自动化/collect_reports.py")
    elif not ok:
        add("P0", "reports_down", "报表全部不可用", "3 份报表均为 stale / blocked", "检查 CDN 与抓取脚本")
    else:
        for r in reps:
            if r.get("status") != "ok":
                add("P1", "report_" + str(r.get("key")) + "_bad",
                    "报表「" + str(r.get("title") or r.get("key")) + "」抓取异常",
                    str(r.get("error") or ("status=" + str(r.get("status")))), "重跑 collect_reports")

    headline = reports.get("headline") or []
    if ok and len(headline) < 4:
        add("P1", "headline_missing", "经营指标缺项", f"headline 只有 {len(headline)}/4 项", "看报表中心的 as_of 与缺项")

    for r in ok:
        mo = r.get("month") or {}
        if not mo:
            continue
        gap = round(float(mo.get("time_progress") or 0) - float(mo.get("progress") or 0), 1)
        lvl = "P0" if gap >= 30 else ("P1" if gap >= 10 else None)
        if lvl:
            add(lvl, "month_gap", f"月自付进度落后 {gap:g}pp",
                f"进度 {float(mo.get('progress') or 0):g}% vs 时间 {float(mo.get('time_progress') or 0):g}%；实际 ¥{float(mo.get('actual') or 0):g} 万 / 目标 ¥{float(mo.get('target') or 0):g} 万",
                "看综合看板的目标达成，排下半月动作")

    for r in ok:
        if r.get("key") != "cend":
            continue
        mm = re.search(r"(\d{4}-\d{2}-\d{2})", r.get("as_of") or "")
        if mm:
            try:
                lag = (today - date.fromisoformat(mm.group(1))).days
            except ValueError:
                lag = 0
            if lag > 1:
                add("P1", "cend_lag", f"综合看板数据日滞后 {lag} 天", f"as_of {r.get('as_of')}", "检查报表生成任务")

    if WX_SRC.exists():
        try:
            wx_age = (datetime.now() - datetime.fromtimestamp(WX_SRC.stat().st_mtime)).total_seconds() / 3600
        except Exception:
            wx_age = 0
        now_dt = datetime.now()
        wx_limit = 3 if (now_dt.weekday() < 5 and 9 <= now_dt.hour < 20) else 26
        if wx_age > wx_limit:
            add("P1", "wechat_upstream", f"微信上游数据 {wx_age:.0f} 小时未更新",
                "merged.ndjson mtime " + datetime.fromtimestamp(WX_SRC.stat().st_mtime).strftime("%m-%d %H:%M"),
                "跑 wx_run.sh 或检查微信流水线")
    else:
        add("P2", "wechat_src_missing", "微信上游数据不存在", str(WX_SRC), "跑一次微信流水线")

    if email.get("status") in ("stale", "blocked"):
        add("P1", "email_bad", "邮件来源异常" if email.get("status") == "stale" else "邮件来源不可用",
            str(email.get("error") or "未取到未读"), "检查 himalaya 登录 / 网络")
    elif (email.get("candidates") or 0) > 12:
        add("P2", "email_backlog", f"未读邮件积压 {email['candidates']} 条", "阈值 12 条", "抽 10 分钟清理收件箱")

    old = [d for d in (review.get("days") or [])
           if d.get("planned") and not d.get("closed") and d.get("date") and d["date"] < today.isoformat()]
    if len(old) >= 3:
        add("P2", "logs_unclosed", f"{len(old)} 天日志未收口", "最早 " + str(old[0].get("date")), "收工页补原因并收口")

    for p in projects:
        if p.get("status") != "卡点" or not p.get("updated"):
            continue
        try:
            days = (today - date.fromisoformat(p["updated"])).days
        except ValueError:
            continue
        if days >= 14:
            add("P1", "proj_" + str(p["name"]), f"卡点项目「{p['name']}」{days} 天未更新",
                str(p.get("blocker") or ""), "改项目卡 next / updated")

    # ---------- 日程（calendar.json） ----------
    cal = calendar or {}
    cal_status = cal.get("status") or "none"
    if cal_status in ("blocked", "error", "none"):
        add("P2", "calendar_unlinked",
            {"none": "日历未接入", "blocked": "日历未授权（读不到你的日程）", "error": "日历读取失败"}.get(cal_status, "日历未接入"),
            str(cal.get("reason") or "collect_calendar.py 还没跑过"),
            str(cal.get("hint") or "跑 04-自动化/collect_calendar.py"))
    elif cal_status in ("ok", "partial"):
        tdy = cal.get("today") or {}
        cs = tdy.get("conflicts") or []
        if cs:
            names = " / ".join(f"{c.get('a')}×{c.get('b')}" for c in cs[:3])
            add("P1", "cal_conflict_" + str(tdy.get("date")),
                f"今日 {len(cs)} 组日程冲突",
                names + f"（重叠 {sum(int(c.get('overlap') or 0) for c in cs)} 分钟）",
                "先定哪个会可改期；再用 calendar +suggestion 找共同空闲")
        wk = cal.get("week") or {}
        share = float(wk.get("meeting_share") or 0)
        if share >= 0.5:
            add("P2", "cal_overload",
                f"本周会议占工作窗口 {share:.0%}",
                f"{wk.get('meetings')} 场 / {wk.get('meeting_hours')} 小时；碎片 {wk.get('fragments')} 段",
                "本周新增会议默认 25/50 分钟；把深度工作放进 ≥90 分钟空档")

    order = {"P0": 0, "P1": 1, "P2": 2}
    out.sort(key=lambda x: (order.get(x["level"], 9), x["key"]))
    return out[:6]


def anomaly_item(a, today):
    return {
        "id": a["id"], "title": a["title"], "source": "异常", "project": "数据异常",
        "due": today.isoformat(), "kind": "异常", "who": "数据检查",
        "evidence": a["evidence"] + "｜建议：" + a["action"], "url": "",
        "flags": ["异常"], "anomaly_level": a["level"], "next": a["action"],
    }


def hourly_status(last, now, work_limit=2.5, off_limit=15):
    """工作日 9–19 按 work_limit 判滞后；夜间/周末放宽到 off_limit。"""
    if not last:
        return "未接入"
    try:
        age = (now - datetime.fromisoformat(str(last).replace("Z", "+00:00"))).total_seconds() / 3600
    except Exception:
        return "未知"
    limit = work_limit if (now.weekday() < 5 and 9 <= now.hour < 20) else off_limit
    return "正常" if age <= limit else f"滞后 {age:.0f}h"


def _cron_slots(sched):
    """粗略估算一条任务一天跑几次：支持 cron 五段式与 Hermes 的 "every 30m/6h"。"""
    s = str(sched or "").strip()
    m = re.match(r"every\s+(\d+)\s*([mh])", s, re.I)
    if m:
        mins = int(m.group(1)) * (1 if m.group(2).lower() == "m" else 60)
        return max(1, min(1440, 1440 // max(1, mins)))
    parts = s.split()
    if len(parts) < 5:
        return 1
    hour = parts[1]
    if hour in ("*", "?"):
        return 24
    total = 0
    for part in hour.split(","):
        mm = re.match(r"^(?:(\d+)(?:-(\d+))?|(\*))(?:/(\d+))?$", part)
        if not mm:
            continue
        lo, hi, star, step = mm.groups()
        step = int(step) if step else 1
        if star:
            span = 24
        elif lo is not None and hi is not None:
            a, b = int(lo), int(hi)
            span = b - a + 1 if b >= a else (24 - a + b + 1)
        elif lo is not None:
            span = 1
        else:
            continue
        total += max(1, span if step == 1 else span // step)
    return max(1, min(24, total))


def cron_health(cron, now):
    """给每条定时任务判健康态：off 暂停 / never 待首跑 / fail 失败 / late 逾期 / ok 正常。
    超时阈值按「一天跑几次」推：每小时级工作日 3h，一天几次按 2.5 倍间隔（下限 8h），
    一天一次及更慢给 74h。宁可漏报也不误报——误报几次就没人信这块了。"""
    work = now.weekday() < 5 and 9 <= now.hour < 20
    out = []
    for c in cron:
        item = dict(c)
        last = c.get("last_run")
        age_h = None
        if last:
            try:
                age_h = (now.timestamp() * 1000 - float(last)) / 3600000
            except (TypeError, ValueError):
                age_h = None
        slots = _cron_slots(c.get("schedule"))
        if slots >= 8:
            limit = 3.0 if work else 15.0
        elif slots >= 2:
            limit = max(8.0, 24.0 / slots * 2.5)
        else:
            limit = 74.0
        if not c.get("enabled"):
            health, text = "off", "暂停"
        elif str(c.get("last_status") or "").strip() not in ("", "ok"):
            health, text = "fail", "失败"
        elif not last:
            health, text = "never", "待首跑"
        elif age_h is not None and age_h > limit:
            health, text = "late", "逾期 %.0fh" % age_h
        else:
            health, text = "ok", "正常"
        item.update({"health": health, "health_text": text, "limit_h": round(limit, 1),
                     "age_h": None if age_h is None else round(age_h, 1)})
        out.append(item)
    order = {"fail": 0, "late": 1, "never": 2, "off": 3, "ok": 4}
    out.sort(key=lambda x: order.get(x["health"], 9))
    return out


def update_cron_history(cron, now):
    """按天记一条：每条任务当天的最终健康态，留 30 天。
    每小时构建只覆盖「今天」这一格，不动历史——热力图靠它积累。"""
    hist = {"version": 1, "days": {}}
    if CRON_HIST.exists():
        try:
            old = json.loads(CRON_HIST.read_text(encoding="utf-8"))
            if isinstance(old, dict) and isinstance(old.get("days"), dict):
                hist = old
        except Exception as e:
            print(f"[warn] cron_history.json 解析失败，重建: {e}")
    today = now.date().isoformat()
    day = hist["days"].get(today) if isinstance(hist["days"].get(today), dict) else {}
    for c in cron:
        h = c.get("health")
        if h in ("ok", "fail", "late", "never", "off"):
            day[c["name"]] = h
    hist["days"][today] = day
    cutoff = (now.date() - timedelta(days=29)).isoformat()
    hist["days"] = {k: v for k, v in hist["days"].items() if k >= cutoff}
    hist["since"] = min(hist["days"]) if hist["days"] else today
    return hist


def build_goals(reports):
    """月度目标 vs 实际：只取报表里带 month.target 的指标，没有就返回空，不编数。"""
    out = []
    for rep in (reports.get("reports") or []):
        m = rep.get("month")
        if not isinstance(m, dict):
            continue
        try:
            target = float(m.get("target"))
            actual = float(m.get("actual"))
        except (TypeError, ValueError):
            continue
        if target <= 0:
            continue
        prog = m.get("progress")
        prog = round(actual / target * 100, 1) if prog is None else round(float(prog), 1)
        tp = m.get("time_progress")
        out.append({
            "key": rep.get("key"),
            "label": m.get("label") or GOAL_LABELS.get(rep.get("key")) or rep.get("title") or "月度目标",
            "actual": actual,
            "target": target,
            "unit": m.get("unit") or "万",
            "progress": prog,
            "time_progress": None if tp is None else round(float(tp), 1),
            "forecast": m.get("forecast"),
            "gap": None if tp is None else round(prog - float(tp), 1),
            "as_of": rep.get("as_of"),
            "source": rep.get("key"),
        })
    return out


def build_gate(m, reports, review, calibers, today, now):
    """周报出稿前核对：全部本地可算，不猜业务数字。最差项决定整卡状态。"""
    items = []
    # 1 本周日志收口（周一起算，不含今天；今天在「收工」页单独收）
    wk_start = today - timedelta(days=today.weekday())
    expected = (today - wk_start).days
    closed_n = sum(1 for d in (review.get("days") or [])
                   if wk_start.isoformat() <= str(d.get("date") or "") < today.isoformat() and d.get("closed"))
    if expected == 0:
        items.append({"key": "logs", "label": "本周日志", "status": "pass",
                      "detail": "本周刚起，暂无应填日志"})
    elif closed_n == 0:
        items.append({"key": "logs", "label": "本周日志", "status": "block",
                      "detail": "应填 %d 天，已收口 0 天；数据段会空" % expected})
    elif closed_n < expected:
        items.append({"key": "logs", "label": "本周日志", "status": "warn",
                      "detail": "已收口 %d / 应填 %d 天" % (closed_n, expected)})
    else:
        items.append({"key": "logs", "label": "本周日志", "status": "pass",
                      "detail": "已收口 %d / 应填 %d 天" % (closed_n, expected)})

    # 2 项目卡新鲜度
    stale = m.get("stale_projects") or 0
    items.append({"key": "projects", "label": "项目卡新鲜度",
                  "status": "pass" if stale == 0 else ("warn" if stale <= 2 else "block"),
                  "detail": "全部 7 天内更新过" if stale == 0 else "%d 张卡超 7 天没更新" % stale})

    # 3 报表快照时效
    gen = reports.get("generated_at")
    if not gen:
        items.append({"key": "reports", "label": "经营数据", "status": "block",
                      "detail": "没有报表快照；数据段只能标未接入"})
    else:
        try:
            age = (now - datetime.fromisoformat(str(gen))).total_seconds() / 3600
        except (TypeError, ValueError):
            age = None
        if age is None:
            items.append({"key": "reports", "label": "经营数据", "status": "warn",
                          "detail": "快照时间无法解析"})
        else:
            items.append({"key": "reports", "label": "经营数据",
                          "status": "pass" if age <= 30 else ("warn" if age <= 54 else "block"),
                          "detail": "快照 %s（%.0f 小时前）" % (str(gen)[:16].replace("T", " "), age)})

    # 4 待我回应
    aw = m.get("awaiting_me") or 0
    items.append({"key": "await", "label": "待我回应", "status": "pass" if aw == 0 else "warn",
                  "detail": "没有挂起项" if aw == 0 else "%d 条挂着，出稿前先回掉或标延期" % aw})

    # 5 口径表
    n = len(calibers or [])
    items.append({"key": "caliber", "label": "口径表", "status": "pass" if n >= 5 else "warn",
                  "detail": "%d 条已登记，数据段按此对齐" % n})

    rank = {"pass": 0, "warn": 1, "block": 2}
    worst = max(items, key=lambda x: rank.get(x["status"], 0))["status"]
    return {"status": worst, "items": items,
            "note": "只核对本地算得出来的东西；业务数字对不对，仍要回报表中心和口径表核一遍。"}


def build_wechat_candidates(wechat):
    out = []
    for it in (wechat.get("items") or [])[:8]:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        raw = "|".join([text, str(it.get("room") or ""), str(it.get("time") or "")])
        out.append({
            "id": "wx:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12],
            "text": text[:300], "room": str(it.get("room") or "")[:60],
            "sender": str(it.get("sender") or "")[:40], "time": str(it.get("time") or ""),
            "score": int(it.get("score") or 0),
        })
    return out


def build_frequencies(now, reports, email, inbox, wechat, calendar=None):
    def age_status(last, max_h):
        if not last:
            return "未接入"
        try:
            age = (now - datetime.fromisoformat(str(last).replace("Z", "+00:00"))).total_seconds() / 3600
        except Exception:
            return "未知"
        return "正常" if age <= max_h else f"滞后 {age:.0f}h"

    wmeta = (wechat.get("meta") or {}) if isinstance(wechat, dict) else {}
    wx_last = None
    if WX_SRC.exists():
        try:
            wx_last = datetime.fromtimestamp(WX_SRC.stat().st_mtime).isoformat(timespec="seconds")
        except Exception:
            wx_last = None
    feishu_ok = any(s.get("key") in ("feishu_task", "feishu_minutes", "okr") and s.get("status") == "ok"
                    for s in (inbox.get("sources") or []))
    return [
        {"key": "build", "label": "工作台快照（项目 / 日志 / 队列）", "cadence": "每天 07:40 + 工作日 09–19 每小时", "last": now.isoformat(timespec="seconds"), "status": "正常"},
        {"key": "reports", "label": "经营报表（3 份 CDN）", "cadence": "每天 10:30", "last": reports.get("generated_at"), "status": age_status(reports.get("generated_at"), 30)},
        {"key": "email", "label": "邮件未读", "cadence": "工作日 09–19 每小时", "last": email.get("generated_at"), "status": hourly_status(email.get("generated_at"), now)},
        {"key": "wechat", "label": "微信候选", "cadence": "工作日 09–19 每小时（抓取+分析）", "last": wx_last or wmeta.get("generated_at"), "status": hourly_status(wx_last, now, work_limit=3, off_limit=26) if wx_last else "未接入"},
        {"key": "feishu", "label": "飞书 OKR / 待办 / 纪要", "cadence": "工作日 09–19 每小时（待授权）", "last": inbox.get("generated_at") if feishu_ok else None, "status": "未接入" if not feishu_ok else hourly_status(inbox.get("generated_at"), now)},
        dict(_cal_freq(calendar or {}, now), label="飞书日程（今日 / 本周 · 冲突 · 空闲）", cadence="工作日 09–19 每小时"),
    ]


def _cal_freq(cal, now):
    st = cal.get("status") or "none"
    last = cal.get("synced_at")
    if st == "ok":
        status = hourly_status(last, now)
    elif st == "partial":
        status = "仅忙闲"
    elif st == "blocked":
        status = "未授权"
    elif st == "error":
        status = "读取失败"
    else:
        status = "未接入"
    return {"key": "calendar", "last": last if st == "ok" else None, "status": status}


def main():
    dry = "--dry-run" in sys.argv
    if not dry:
        ingest_daylogs()
    projects_all = load_projects()
    hidden_projects = [p["name"] for p in projects_all if p.get("hidden")]
    projects = [p for p in projects_all if not p.get("hidden")]
    weight = {"卡点": 0, "等待外部": 1, "推进中": 2, "暂停": 3}
    projects.sort(key=lambda p: (weight.get(p["status"], 9), p.get("updated") or "0000-00-00"))
    inbox = load_json(INBOX, {"sources": [], "items": []})
    email = load_json(EMAIL, {"status": "none", "account": "", "items": [], "candidates": 0})
    wechat = load_json(WECHAT, {"meta": {}, "items": []})
    email_items = [x for x in (email.get("items") or []) if isinstance(x, dict) and x.get("title")]
    if email_items:
        existing_titles = {str(x.get("title") or "").strip() for x in (inbox.get("items") or []) if isinstance(x, dict)}
        for x in email_items:
            t = str(x.get("title") or "").strip()
            if t in existing_titles:
                continue
            x = dict(x); x["source"] = "邮件"; x["auto"] = True
            inbox.setdefault("items", []).append(x)
            existing_titles.add(t)
    if email.get("account"):
        inbox.setdefault("sources", []).append({
            "key": "email", "label": "邮件", "status": email.get("status") or "blocked",
            "detail": ("未读候选 " + str(email.get("candidates") or 0) + " 条 · " + str(email.get("account"))) +
                      ((" · " + str(email.get("error"))) if email.get("error") else ""),
            "candidates": email.get("candidates") or 0,
        })
    raw_items = inbox.get("items") or []
    hidden_inbox = [it for it in raw_items if isinstance(it, dict) and is_hidden_text(it.get("title"), it.get("project"), it.get("evidence"), it.get("next"))]
    if hidden_inbox:
        inbox["items"] = [it for it in raw_items if it not in hidden_inbox]
    okr = load_json(OKR, {"source": "none", "status": "no-data", "objectives": []})
    okr_manual = load_json(OKR_MANUAL, {"status": "none", "objectives": []})
    if okr_manual.get("status") == "ok" and (okr.get("status") != "ok" or not okr.get("objectives")):
        okr = okr_manual
        for s in (inbox.get("sources") or []):
            if s.get("key") == "okr":
                s["status"] = "partial"
                s["detail"] = "手工录入 2026 Q4（截图）+ 项目卡/报表；飞书未授权，未自动同步"
    okr_links = {}
    for obj in (okr.get("objectives") or []):
        for kr in (obj.get("krs") or []):
            proj = kr.get("project")
            if proj:
                okr_links.setdefault(proj, []).append({
                    "id": kr.get("id") or "", "status": kr.get("status") or "",
                    "progress": kr.get("progress"),
                })
    for p in projects:
        p["okr_links"] = okr_links.get(p.get("name"), [])
    overrides = load_next_overrides()
    priority_items, priority_counts = build_priority(projects, inbox, okr, date.today(), overrides)
    review = load_review()
    today_log = load_today()
    calendar = load_json(CALENDAR, {"status": "none", "today": None, "week": None,
                                     "days_detail": [], "free_blocks": [],
                                     "tasks": {"status": "none", "items": []}})
    cal_ok = calendar.get("status") == "ok"
    cal_today = (calendar.get("today") or {}) if cal_ok else {}
    cal_week = (calendar.get("week") or {}) if cal_ok else {}

    all_outputs = scan_outputs()
    output_total = len(all_outputs)
    outputs = all_outputs[:OUTPUT_CAP]
    now = datetime.now()
    cron = cron_health(load_cron(), now)

    vis_projects = [p for p in projects if not p.get("hidden")]
    m = {
        "active_projects": sum(1 for p in vis_projects if p["status"] != "暂停"),
        "blockers": sum(1 for p in vis_projects if p["status"] == "卡点"),
        "stale_projects": sum(
            1 for p in vis_projects
            if p.get("updated") and (now.date() - date.fromisoformat(p["updated"])).days > 7
        ),
        "outputs_7d": sum(1 for o in all_outputs if o["mtime"] > (time.time() - 7 * 86400) * 1000),
        "sessions_7d": count_sessions(7),
        "output_total": output_total,
        "outputs_cap": OUTPUT_CAP,
        "p0": priority_counts["P0"],
        "p1": priority_counts["P1"],
        "overdue": sum(1 for i in priority_items if not i.get("hidden") and i.get("due") and i["due"] < date.today().isoformat()),
        "due_today": sum(1 for i in priority_items if not i.get("hidden") and i.get("due") == date.today().isoformat()),
        "cron_total": len(cron),
        "cron_ok": sum(1 for c in cron if c["enabled"]),
        "awaiting_me": sum(1 for i in priority_items if i.get("awaiting") and not i.get("hidden")),
        "today_meetings": cal_today.get("meetings") or 0,
        "today_meeting_minutes": cal_today.get("meeting_minutes") or 0,
        "today_conflicts": len(cal_today.get("conflicts") or []),
        "week_meeting_share": round(float(cal_week.get("meeting_share") or 0) * 100),
        "today_planned": len(today_log["three"]),
        "today_done": sum(1 for x in today_log["three"] if x["done"]),
    }

    reports = load_json(REPORTS, {"generated_at": None, "reports": [], "headline": []})
    anomalies = detect_anomalies(m, reports, email, review, vis_projects, date.today(), calendar)
    lvl_score = {"P0": 130, "P1": 90, "P2": 45}
    for a in anomalies:
        it = anomaly_item(a, date.today())
        score_item(it, date.today())
        it["level"] = a["level"]
        it["score"] = lvl_score.get(a["level"], 45)
        priority_items.append(it)
    priority_items.sort(key=lambda x: (-x["score"], x.get("due") or "9999"))
    priority_counts = {lvl: sum(1 for i in priority_items if not i.get("hidden") and i["level"] == lvl) for lvl in ("P0", "P1", "P2")}
    visible_items = [i for i in priority_items if not i.get("hidden")]
    m.update({
        "p0": priority_counts["P0"], "p1": priority_counts["P1"],
        "overdue": sum(1 for i in visible_items if i.get("due") and i["due"] < date.today().isoformat()),
        "due_today": sum(1 for i in visible_items if i.get("due") == date.today().isoformat()),
        "awaiting_me": sum(1 for i in visible_items if i.get("awaiting")),
    })

    calibers = [
        {"metric": "日完成率", "formula": "已收口日志的三件事已完成 / 已填", "exclude": "未收口不计入分母；待办池不计入", "source": "00-今日/*.md", "refresh": "每日"},
        {"metric": "昨日 GMV", "formula": "自付 + 卡券抵扣 + 运费（渠道口径）", "exclude": "0 元单照计，口径与自付源不同", "source": "cend 报表（T-1）", "refresh": "每日 09:44"},
        {"metric": "自付率（渠道口径）", "formula": "自付 ÷ GMV", "exclude": "自付源口径差异见报表中心提示", "source": "cend 报表", "refresh": "每日 09:44"},
        {"metric": "理赔型药诊卡核销率", "formula": "累计 GMV ÷ 累计投放额度（含锁赔）", "exclude": "仅理赔型药诊卡，不代表全平台", "source": "claims 报表", "refresh": "每日 09:35"},
    ]
    goals = build_goals(reports)
    gate = build_gate(m, reports, review, calibers, date.today(), now)
    cron_hist = update_cron_history(cron, now)

    data = {
        "today": today_log,
        "calendar": calendar,
        "carryover": load_carryover(),
        "meta": {
            "name": "平静的工作台",
            "generated_at": now.isoformat(timespec="seconds"),
            "version": 8,
            "builder": "04-自动化/build_workbench.py",
        },
        "projects": projects,
        "priority": {"items": priority_items, "counts": priority_counts},
        "anomalies": anomalies,
        "frequencies": build_frequencies(now, reports, email, inbox, wechat, calendar),
        "wechat_candidates": build_wechat_candidates(wechat),
        "inbox": {"sources": inbox.get("sources") or [], "curator": inbox.get("curator") or "",
                  "note": inbox.get("note") or "", "total": len(visible_items),
                  "next_n": sum(1 for i in visible_items if i.get("next"))},
        "okr": okr,
        "outputs": outputs,
        "cron": cron,
        "metrics": m,
        "trend": build_trend(all_outputs),
        "review": review,
        "reports": reports,
        "templates": load_templates(),
        "week": week_stats(all_outputs, review, m["blockers"]),
        "workday": {"available_min": WORKDAY_MIN},
        "businessKpis": reports.get("headline") or [
            {"label": "昨日 GMV", "value": None, "caliber": "未接入：等报表快照"},
            {"label": "有效订单", "value": None, "caliber": "未接入：等报表快照"},
            {"label": "用户自付率", "value": None, "caliber": "未接入：等报表快照"},
            {"label": "月度核销率", "value": None, "caliber": "未接入：等报表快照"},
        ],
        "calibers": calibers,
        "goals": goals,
        "gate": gate,
        "cronHistory": cron_hist,
        "quick": [
            {"label": "工作台根目录", "path": str(ROOT)},
            {"label": "今日日志", "path": str(ROOT / "00-今日")},
            {"label": "项目卡", "path": str(ROOT / "01-项目")},
            {"label": "定稿产出", "path": str(ROOT / "02-产出")},
            {"label": "自动化脚本", "path": str(ROOT / "04-自动化")},
        ],
    }

    print(f"项目卡 {len(projects)} 张 · 卡点 {m['blockers']} · 待我回应 {m['awaiting_me']} · 近30天产出 {output_total} 份（收录 {len(outputs)}） · 定时任务 {len(cron)} 条（启用 {m['cron_ok']}）· 近7天会话 {m['sessions_7d']} · 复盘历史 {review['covered']} 天 · 已隐藏 {len(hidden_projects)} 项目/{len(hidden_inbox)} 任务 · 异常 {len(anomalies)} 条 · 月度目标 {len(goals)} 项 · 出稿闸门 {gate['status']}")
    if dry:
        print("dry-run：不写文件")
        return

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    CRON_HIST.write_text(json.dumps(cron_hist, ensure_ascii=False, indent=1), encoding="utf-8")
    NEXT_FILE.write_text(json.dumps(overrides, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if "__DATA_JSON__" not in tpl:
        raise SystemExit("模板缺少 __DATA_JSON__ 占位符")
    OUT_HTML.write_text(tpl.replace("__DATA_JSON__", payload.replace("<", r"\u003c"), 1), encoding="utf-8")

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    (SNAP_DIR / f"{date.today().isoformat()}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    cutoff = time.time() - 60 * 86400
    for f in SNAP_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass

    print(f"已生成 {OUT_HTML} ({OUT_HTML.stat().st_size // 1024} KB) 与 {OUT_JSON}")


if __name__ == "__main__":
    main()
