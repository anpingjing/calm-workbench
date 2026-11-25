#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取三份 C 端经营报表的头部指标 → 03-数据/reports.json

报表是 JS 渲染的单文件页面（CDN），所以用 playwright(chrome) 渲染后读 DOM。
失败不写坏：保留上一次快照，标 status=stale，页面原样展示抓取时间与原因。

用法：
    python3 04-自动化/collect_reports.py
    python3 04-自动化/collect_reports.py --quiet   # 成功静默（给 cron 用）
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "03-数据" / "reports.json"

CONFIG = ROOT / "local" / "report_sources.json"

EXTRACT_JS = """(kind) => {
  if (kind === 'claims') {
    return [...document.querySelectorAll('.kpi-card')].map(e => ({
      label: (e.querySelector('.kpi-label') || {}).innerText || '',
      value: (e.querySelector('.kpi-value') || {}).innerText || '',
      sub: ''
    }));
  }
  return [...document.querySelectorAll('.kpi')].map(e => {
    const l = e.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
    return { label: l[0] || '', value: l[1] || '', sub: l[2] || '' };
  });
}"""


def norm_metrics(rows):
    out = []
    for r in rows:
        label = (r.get("label") or "").replace("\u00a0", " ").strip()
        val = (r.get("value") or "").replace("\u00a0", " ").strip()
        sub = (r.get("sub") or "").replace("\u00a0", " ").strip()
        if val in ("自付口径", "渠道口径") and sub:
            val, sub = sub, ""
        if not label or not val:
            continue
        out.append({"label": label, "value": val, "sub": sub})
    return out


def pick(metrics, keep):
    """先精确匹配，再按子串兜底（季度标签会从 Q3 变 Q4）。"""
    out, seen = [], set()
    for k in keep:
        m = next((x for x in metrics if x["label"] == k), None)
        if not m:
            m = next((x for x in metrics if k in x["label"]), None)
        if m and m["label"] not in seen:
            seen.add(m["label"])
            out.append(m)
    return out


def scrape(browser, key, cfg):
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
    pg = ctx.new_page()
    try:
        pg.goto(cfg["url"], wait_until="domcontentloaded", timeout=60000)
        sel = ".kpi-card" if key == "claims" else ".kpi"
        pg.wait_for_selector(sel, timeout=25000)
        pg.wait_for_timeout(1200)
        rows = pg.evaluate(EXTRACT_JS, "claims" if key == "claims" else "kpi")
        lines = pg.evaluate("() => document.body.innerText.split('\\n').map(s => s.trim()).filter(Boolean)")
        title = pg.title() or cfg["title"]
    finally:
        ctx.close()

    metrics = pick(norm_metrics(rows), cfg["keep"])
    as_of, updated, month = "", "", None
    if key == "cend":
        for ln in lines:
            if ln.startswith("数据日"):
                as_of = ln.replace("数据日", "").replace("\u00a0", " ").strip()
            elif ln.startswith("渠道源生成"):
                updated = ln.replace("渠道源生成", "").replace("\u00a0", " ").strip()
        note = next((ln for ln in lines if "双口径有差异" in ln), cfg["note"])
        for ln in lines:
            mm = re.search(r"([\d.]+)%\s*（¥([\d.]+)\s*/\s*([\d.]+)\s*万）\s*·\s*时间进度\s*([\d.]+)%(?:\s*·\s*预计\s*¥([\d.]+)\s*万)?", ln.replace("\u00a0", " "))
            if mm:
                month = {
                    "progress": float(mm.group(1)), "actual": float(mm.group(2)),
                    "target": float(mm.group(3)), "time_progress": float(mm.group(4)),
                    "forecast": float(mm.group(5)) if mm.group(5) else None,
                }
                break
    elif key == "selfpay":
        for ln in lines:
            if ln.startswith("数据截止"):
                as_of = ln.split("｜")[0].replace("数据截止：", "").strip()
                if "生成：" in ln:
                    updated = ln.split("生成：")[-1].strip()
        note = cfg["note"]
    else:
        for ln in lines:
            if ln.startswith("数据更新时间"):
                as_of = ln.replace("数据更新时间：", "").strip()
        note = cfg["note"]

    return {
        "key": key, "title": title, "url": cfg["url"],
        "as_of": as_of, "updated": updated,
        "metrics": metrics, "note": note,
        "month": month,
    }


def load_prev():
    try:
        data = json.loads(OUT.read_text(encoding="utf-8"))
        return {r.get("key"): r for r in data.get("reports", []) if isinstance(r, dict)}
    except Exception:
        return {}


def pick_value(report, label):
    for m in report.get("metrics") or []:
        if m.get("label") == label:
            return m.get("value", ""), m.get("sub", "")
    return "", ""


def build_alerts(recs):
    out = []
    for r in recs:
        m = r.get("month") or {}
        if not m:
            continue
        gap = round(m.get("time_progress", 0) - m.get("progress", 0), 1)
        if gap >= 10:
            out.append({
                "key": r["key"], "level": "warn",
                "text": f"月自付进度 {m['progress']:g}% vs 时间进度 {m['time_progress']:g}%，落后 {gap:g}pp；实际 ¥{m['actual']:g} 万 / 目标 ¥{m['target']:g} 万",
            })
    return out


def build_headline(recs):
    by = {r["key"]: r for r in recs}
    out = []

    def add(label, rep_key, mlabel, sub_override=None):
        r = by.get(rep_key) or {}
        val, sub = pick_value(r, mlabel)
        if not val:
            return
        out.append({
            "label": label, "value": val,
            "sub": sub_override or sub,
            "source": rep_key,
            "as_of": r.get("as_of") or r.get("updated") or "",
        })

    add("昨日 GMV", "cend", "昨日 GMV", "渠道口径 · 含券抵扣/运费")
    add("昨日自付", "cend", "昨日自付金额", "渠道口径")
    add("自付率", "cend", "自付率（自付/GMV）", "自付 ÷ GMV")
    add("理赔型药诊卡核销率", "claims", "核销率", "累计 GMV ÷ 投放额度")
    return out


def main():
    if not CONFIG.is_file():
        raise SystemExit("请先配置 local/report_sources.json")
    reports = json.loads(CONFIG.read_text(encoding="utf-8"))
    quiet = "--quiet" in sys.argv
    prev = load_prev()
    recs, errors = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        for key, cfg in reports.items():
            try:
                rec, last_err = None, None
                for attempt in (1, 2):
                    try:
                        rec = scrape(browser, key, cfg)
                        break
                    except Exception as e:
                        last_err = e
                        if attempt == 1:
                            time.sleep(2)
                if rec is None:
                    raise last_err or RuntimeError("抓取失败")
                if not rec["metrics"]:
                    raise RuntimeError("页面未解析到指标")
                rec["status"] = "ok"
                rec["fetched_at"] = datetime.now().isoformat(timespec="seconds")
            except Exception as e:
                err = str(e).splitlines()[0][:200]
                errors.append(f"{key}: {err}")
                old = prev.get(key)
                if old:
                    rec = dict(old)
                    rec["status"] = "stale"
                    rec["error"] = err
                else:
                    rec = {"key": key, "title": cfg["title"], "url": cfg["url"], "status": "blocked",
                           "error": err, "metrics": [], "as_of": "", "updated": "", "note": cfg["note"]}
            recs.append(rec)
        browser.close()

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reports": recs,
        "headline": build_headline(recs),
        "alerts": build_alerts(recs),
        "source": "04-自动化/collect_reports.py",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    if errors:
        print("[报表] 抓取异常：" + "；".join(errors))
        print("[报表] 已保留上一次快照，页面会标 stale。")
        sys.exit(0 if any(r.get("metrics") for r in recs) else 1)
    if not quiet:
        ok = sum(1 for r in recs if r["status"] == "ok")
        print(f"[报表] {ok}/{len(recs)} 份已更新 · headline {len(payload['headline'])} 项 → {OUT}")


if __name__ == "__main__":
    main()
