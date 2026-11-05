#!/usr/bin/env python3
"""Tennis court vacancy watcher (personal use).

Notify-only: reads public reservation pages, compares with the previous run,
and sends a Telegram message when a slot that matches the filter becomes free.
It never logs in and never books anything.

Usage:
    python monitor.py                 # normal run (needs TELEGRAM_* env vars)
    python monitor.py --dry-run       # print messages, do not send, do not save state
    python monitor.py --test-telegram # send one test message and exit
"""
import argparse
import datetime as dt
import html as htmllib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

KST = dt.timezone(dt.timedelta(hours=9))
UA = "court-watch/1.0 (personal vacancy notifier)"
WEEKDAY_KO = "월화수목금토일"
GY_BASE = "https://www.gytennis.or.kr/daily"
EP_URL = "https://www.efmc.or.kr/fmcs/32"
REQUEST_DELAY_SEC = 1.0
FAIL_WARN_AT = 6  # consecutive failed runs before a warning is sent


# ----------------------------------------------------------------- HTTP ---
class Fetcher:
    def __init__(self, delay=REQUEST_DELAY_SEC):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.delay = delay
        self._last = 0.0

    def get(self, url, params=None):
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        last = None
        for i in range(3):
            try:
                r = self.s.get(url, params=params, timeout=20)
                self._last = time.time()
                r.raise_for_status()
                if not r.encoding or r.encoding.lower() == "iso-8859-1":
                    r.encoding = "utf-8"
                return r.text
            except requests.RequestException as e:
                last = e
                time.sleep(2 * (i + 1))
        raise last


# -------------------------------------------------------------- filters ---
def want(d, slot, cfg, now):
    """Weekday: night slots only. Weekend/holiday: every slot. Skip past slots."""
    start_h, start_m = int(slot[:2]), int(slot[3:5])
    if d == now.date() and start_h * 60 + start_m <= now.hour * 60 + now.minute:
        return False
    is_weekend = d.weekday() >= 5 or d.isoformat() in set(cfg.get("extra_holidays", []))
    if is_weekend:
        return True
    return start_h >= cfg.get("weekday_night_start_hour", 18)


def month_starts(today, horizon):
    out, d = [], today
    while d <= horizon:
        first = max(today, d.replace(day=1))
        if all((first.year, first.month) != (x.year, x.month) for x in out):
            out.append(first)
        d += dt.timedelta(days=1)
    return out


def date_range(today, horizon):
    return [today + dt.timedelta(i) for i in range((horizon - today).days + 1)]


# --------------------------------------------------------------- Goyang ---
def gy_calendar_counts(html, court_id):
    """{'YYYY-MM-DD': available_count} from the month calendar ({} if unrecognised)."""
    soup = BeautifulSoup(html, "html.parser")
    href_re = re.compile(rf"/daily/{court_id}/(\d{{4}}-\d{{2}}-\d{{2}})")
    out = {}
    for td in soup.find_all("td"):
        if td.find("table"):
            continue
        a = td.find("a", href=href_re)
        if not a:
            continue
        m = href_re.search(a["href"])
        g = re.search(r"가능\s*(\d+)", td.get_text(" ", strip=True))
        if m and g:
            out[m.group(1)] = int(g.group(1))
    return out


def gy_slots(html):
    """[(court_label, 'HH:MM~HH:MM', is_free)] from the court x time grid."""
    soup = BeautifulSoup(html, "html.parser")
    whole = soup.find("table", class_="wholeTable")
    if whole is None:
        raise ValueError("goyang: wholeTable not found (page layout changed?)")
    times, courts = [], []
    for t in whole.find_all("table"):
        rows = t.find_all("tr")
        if t.find("td", class_="wide"):
            for r in rows:
                if r.find("td", class_="wide"):
                    txt = re.sub(r"\s+", "", r.get_text())
                    m = re.search(r"(\d{2}:\d{2})~(\d{2}:\d{2})", txt)
                    if m:
                        times.append(f"{m[1]}~{m[2]}")
        elif t.find("td", class_="courtTag"):
            label = rows[0].get_text(strip=True)
            cells = [r.find("td") for r in rows[1:]]
            courts.append((label, cells))
    if not times or not courts:
        raise ValueError("goyang: time/court columns not found (page layout changed?)")
    out = []
    for label, cells in courts:
        for slot, cell in zip(times, cells):
            booked = cell is not None and (
                cell.find(class_="public-tooltip-trigger") is not None
                or cell.find("i", class_="fa-user-clock") is not None
            )
            out.append((label, slot, not booked))
    return out


def scan_goyang(f, court, cfg, prev, today, now):
    cid = court["id"]
    horizon = today + dt.timedelta(days=cfg.get("horizon_days", 14))
    prev_counts = (prev or {}).get("counts", {})
    prev_avail = set((prev or {}).get("avail", []))
    pages, cal = {}, {}
    for first in month_starts(today, horizon):
        h = f.get(f"{GY_BASE}/{cid}/{first.isoformat()}")
        pages[first] = h
        cal.update(gy_calendar_counts(h, cid))
    counts, avail = {}, set()
    for d in date_range(today, horizon):
        iso = d.isoformat()
        n = cal.get(iso) if cal else None  # None = unknown, must fetch
        if n == 0:
            counts[iso] = 0
            continue
        if n is not None and prev_counts.get(iso) == n and prev:
            counts[iso] = n
            avail |= {k for k in prev_avail if k.startswith(iso + "|")}
            continue
        h = pages.get(d) or f.get(f"{GY_BASE}/{cid}/{iso}")
        for label, slot, free in gy_slots(h):
            if free and want(d, slot, cfg, now):
                avail.add(f"{iso}|{label}|{slot}")
        if n is not None:
            counts[iso] = n
    return {"counts": counts, "avail": sorted(avail)}


# ------------------------------------------------------------ Eunpyeong ---
def ep_params(kind, ymd, part, place):
    return {"facilities_type": kind, "base_date": ymd, "rent_type": "1001",
            "center": "EFMC01", "part": str(part), "place": str(place)}


def ep_calendar(html, year, month):
    """{date: day_has_free_slot} from the month calendar (state_10 = free)."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for td in soup.find_all("td", class_=re.compile(r"weekday\d")):
        m = re.match(r"\s*(\d{1,2})", td.get_text(" ", strip=True))
        a = td.find("a")
        if not m or a is None:
            continue
        classes = " ".join(a.get("class", []))
        out[dt.date(year, month, int(m.group(1)))] = "state_10" in classes
    return out


def ep_slots(html):
    """[('HH:MM~HH:MM', is_free)] from the hourly list of one day."""
    soup = BeautifulSoup(html, "html.parser")
    tb = None
    for t in soup.find_all("table"):
        tx = t.get_text(" ", strip=True)
        if "회차" in tx and "요금" in tx and not t.find("table"):
            tb = t
            break
    if tb is None:
        raise ValueError("eunpyeong: time table not found (page layout changed?)")
    out = []
    for tr in tb.find_all("tr"):
        txt = tr.get_text(" ", strip=True)
        m = re.search(r"(\d{2}:\d{2})\s*~\s*(\d{2}:\d{2})", txt)
        if m:
            out.append((f"{m[1]}~{m[2]}", "예약가능" in txt))
    return out


def scan_eunpyeong(f, court, cfg, prev, today, now):
    part, place, name = court["part"], court["place"], court["name"]
    horizon = today + dt.timedelta(days=cfg.get("horizon_days", 14))
    cal = {}
    for first in month_starts(today, horizon):
        h = f.get(EP_URL, params=ep_params("C", first.strftime("%Y%m%d"), part, place))
        cal.update(ep_calendar(h, first.year, first.month))
    if not cal:
        raise ValueError("eunpyeong: calendar not recognised (page layout changed?)")
    avail = set()
    for d in date_range(today, horizon):
        if not cal.get(d):
            continue  # fully booked / not open
        h = f.get(EP_URL, params=ep_params("T", d.strftime("%Y%m%d"), part, place))
        for slot, free in ep_slots(h):
            if free and want(d, slot, cfg, now):
                avail.add(f"{d.isoformat()}|{name}|{slot}")
    return {"avail": sorted(avail)}


# ---------------------------------------------------------- notification ---
def link_for(site, court, d):
    if site == "goyang":
        return f"{GY_BASE}/{court['id']}/{d.isoformat()}"
    q = ep_params("C", d.strftime("%Y%m%d"), court["part"], court["place"])
    return EP_URL + "?" + "&".join(f"{k}={v}" for k, v in q.items())


def fmt_date(d):
    return f"{d.month}/{d.day}({WEEKDAY_KO[d.weekday()]})"


def build_messages(items, title):
    """items: [(site_label, court_name, date, slot, link)] -> list of HTML messages."""
    groups = {}
    for site, court, d, slot, link in items:
        groups.setdefault((site, court), {}).setdefault((d, link), []).append(slot)
    blocks = []
    for (site, court), days in sorted(groups.items()):
        lines = [f"<b>[{htmllib.escape(site)}] {htmllib.escape(court)}</b>"]
        for (d, link), slots in sorted(days.items(), key=lambda x: x[0][0]):
            lines.append(f'<a href="{htmllib.escape(link)}">{fmt_date(d)}</a> '
                         + ", ".join(sorted(slots)))
        blocks.append("\n".join(lines))
    msgs, cur = [], f"<b>{htmllib.escape(title)}</b>"
    for b in blocks:
        if len(cur) + len(b) + 2 > 3500:
            msgs.append(cur)
            cur = f"<b>{htmllib.escape(title)} (계속)</b>"
        cur += "\n\n" + b
    msgs.append(cur)
    return msgs


def tg_send(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        r.raise_for_status()
    except requests.RequestException as e:  # never print the URL: it contains the token
        code = getattr(getattr(e, "response", None), "status_code", None)
        raise RuntimeError(f"telegram send failed ({type(e).__name__}, status={code})") from None


# ------------------------------------------------------------------ main ---
def parse_key(key):
    d, court, slot = key.split("|")
    return dt.date.fromisoformat(d), court, slot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("--state", default=str(Path(__file__).with_name("state.json")))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    args = ap.parse_args()

    if args.test_telegram:
        tg_send("🎾 court-watch 연결 테스트: 이 메시지가 보이면 알림 설정이 정상입니다.")
        print("test message sent")
        return 0

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    sp = Path(args.state)
    state = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    first_run = not state.get("sites")
    prev_sites = state.get("sites", {})
    fails = dict(state.get("fail", {}))

    now = dt.datetime.now(KST)
    today = now.date()
    f = Fetcher()

    jobs = []
    for c in cfg.get("goyang", []):
        jobs.append(("goyang", "고양", f"goyang:{c['id']}", c, scan_goyang, c["name"]))
    for c in cfg.get("eunpyeong", []):
        jobs.append(("eunpyeong", "은평", f"eunpyeong:{c['part']}:{c['place']}", c,
                     scan_eunpyeong, c["name"]))

    new_sites, new_items, warnings = {}, [], []
    for site, label, key, court, fn, cname in jobs:
        prev = prev_sites.get(key)
        try:
            res = fn(f, court, cfg, prev, today, now)
            fails[key] = 0
        except Exception as e:  # keep previous state so a recovery does not re-alert
            fails[key] = fails.get(key, 0) + 1
            print(f"[warn] {key} failed ({fails[key]}x): {type(e).__name__}: {e}", file=sys.stderr)
            if fails[key] == FAIL_WARN_AT:
                warnings.append(f"⚠️ {label} {cname}: {FAIL_WARN_AT}회 연속 조회 실패 (사이트 구조 변경/차단 가능성)")
            if prev is not None:
                new_sites[key] = prev
            continue
        new_sites[key] = res
        if not first_run:
            old = set((prev or {}).get("avail", []))
            for k in sorted(set(res["avail"]) - old):
                d, cl, slot = parse_key(k)
                new_items.append((label, f"{cname} {cl}" if site == "goyang" else cname, d, slot,
                                  link_for(site, court, d)))

    messages = []
    if first_run:
        total = sum(len(v["avail"]) for v in new_sites.values())
        per = ", ".join(f"{k}: {len(v['avail'])}" for k, v in new_sites.items())
        messages.append(f"🎾 감시 시작: 조건에 맞는 현재 빈 슬롯 {total}개\n{per}\n"
                        f"이후 새로 비는 슬롯만 알려드립니다.")
    elif new_items:
        messages += build_messages(new_items, f"🎾 빈자리 {len(new_items)}건")
    messages += warnings

    try:
        for m in messages:
            if args.dry_run:
                print("---- message ----\n" + m)
            else:
                tg_send(m)
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        return 1  # state not saved -> alert retried next run

    if not args.dry_run:
        sp.write_text(json.dumps({"version": 1, "sites": new_sites, "fail": fails},
                                 ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"done: sites={len(new_sites)} new={len(new_items)} first_run={first_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
