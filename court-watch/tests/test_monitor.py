"""Offline tests. HTML fixtures imitate the structure observed on the real pages
(2026-11-05); they are NOT copies of the sites' pages. Run: python -m pytest -q"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402

KST = monitor.KST


def gy_grid(free):
    """free = set of (court_idx, row_idx) that are available. 2 courts x 3 time rows."""
    times = ["06:00 ~ 08:00", "18:00 ~ 20:00", "20:00 ~ 22:00"]
    left = ('<table class="custom"><tr><td class="diagonal-line">코트</td></tr>'
            + "".join(f'<tr><td class="wide">{t}</td></tr>' for t in times) + "</table>")
    cols = ""
    for ci, name in enumerate(["9 코트", "10 코트"]):
        rows = f'<tr><td class="courtTag">{name}</td></tr>'
        for ri in range(3):
            if (ci, ri) in free:
                rows += '<tr><td class="resTag"><a href="/reserve?x=1">예약</a></td></tr>'
            else:
                rows += ('<tr><td class="resTag"><div class="public-tooltip-trigger" data-kind="R">'
                         '<i class="fa-solid fa-user-clock"></i></div></td></tr>')
        cols += f"<table>{rows}</table>"
    return f'<table class="wholeTable"><tr><td>{left}</td><td>{cols}</td></tr></table>'


def gy_calendar(counts, cid=4):
    cells = "".join(
        f'<td><a href="/daily/{cid}/{d}">{d[-2:]}</a><a>가능 {n}</a><a>마감 9</a></td>'
        for d, n in counts.items())
    return f"<table><tr>{cells}</tr></table>"


def ep_calendar_html(states):
    cells = ""
    for i, (day, st) in enumerate(states.items()):
        cls = "state_10" if st else "state_20"
        cells += (f'<td class="weekday{i % 7 + 1}"><a class="{cls}"><strong>{day}</strong><br>'
                  f'<span class="{cls}">x</span></a></td>')
    return f"<table><tr>{cells}</tr></table>"


def ep_time_html(rows):
    body = "".join(f"<tr><td>x</td><td>{t}</td><td>8,800</td><td>{s}</td></tr>" for t, s in rows)
    return ('<table><tr><th>선택</th><th>회차</th><th>시간</th><th>요금</th><th>비고</th></tr>'
            + body + "</table>")


def test_filter_rules():
    cfg = {"weekday_night_start_hour": 18, "extra_holidays": ["2026-12-25"]}
    now = dt.datetime(2026, 11, 5, 12, 0, tzinfo=KST)
    wed, sat, xmas = dt.date(2026, 11, 11), dt.date(2026, 11, 14), dt.date(2026, 12, 25)
    assert not monitor.want(wed, "08:00~10:00", cfg, now)      # weekday day -> skip
    assert monitor.want(wed, "18:00~20:00", cfg, now)          # weekday night -> keep
    assert monitor.want(sat, "06:00~08:00", cfg, now)          # weekend day -> keep
    assert monitor.want(xmas, "10:00~11:00", cfg, now)         # extra holiday -> keep
    assert not monitor.want(now.date(), "09:00~10:00", cfg, now)  # already started today


def test_goyang_parsers():
    slots = monitor.gy_slots(gy_grid({(0, 1), (1, 0)}))
    free = {(c, s) for c, s, ok in slots if ok}
    assert free == {("9 코트", "18:00~20:00"), ("10 코트", "06:00~08:00")}
    assert len(slots) == 6
    cal = monitor.gy_calendar_counts(gy_calendar({"2026-11-05": 18, "2026-11-06": 0}), 4)
    assert cal == {"2026-11-05": 18, "2026-11-06": 0}


def test_eunpyeong_parsers():
    cal = monitor.ep_calendar(ep_calendar_html({1: False, 2: True, 3: True}), 2026, 11)
    assert cal == {dt.date(2026, 11, 1): False, dt.date(2026, 11, 2): True, dt.date(2026, 11, 3): True}
    slots = monitor.ep_slots(ep_time_html([("06:00~07:00", "예약가능"), ("18:00~19:00", "예약완료")]))
    assert slots == [("06:00~07:00", True), ("18:00~19:00", False)]


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None):
        key = (url, tuple(sorted((params or {}).items())))
        self.calls.append(key)
        return self.pages[key]


def test_goyang_scan_detects_new_slot_and_reuses_unchanged_days():
    cfg = {"horizon_days": 1, "weekday_night_start_hour": 18}
    now = dt.datetime(2026, 11, 9, 7, 0, tzinfo=KST)  # Monday
    today = now.date()
    d0, d1 = "2026-11-09", "2026-11-10"
    base = f"{monitor.GY_BASE}/4"
    pages = {
        (f"{base}/{d0}", ()): gy_calendar({d0: 1, d1: 0}) + gy_grid({(0, 1)}),
        (f"{base}/{d1}", ()): gy_grid(set()),
    }
    court = {"name": "성사전천후", "id": 4}
    r1 = monitor.scan_goyang(FakeFetcher(pages), court, cfg, None, today, now)
    assert r1["avail"] == [f"{d0}|9 코트|18:00~20:00"]
    # second run, same counts -> must not refetch the day grid
    ff = FakeFetcher({(f"{base}/{d0}", ()): pages[(f"{base}/{d0}", ())]})
    r2 = monitor.scan_goyang(ff, court, cfg, r1, today, now)
    assert r2["avail"] == r1["avail"] and len(ff.calls) == 1
    # a cancellation: count 1 -> 2 and a new free slot appears
    pages[(f"{base}/{d0}", ())] = gy_calendar({d0: 2, d1: 0}) + gy_grid({(0, 1), (1, 2)})
    r3 = monitor.scan_goyang(FakeFetcher(pages), court, cfg, r1, today, now)
    assert set(r3["avail"]) - set(r1["avail"]) == {f"{d0}|10 코트|20:00~22:00"}


def test_eunpyeong_scan():
    cfg = {"horizon_days": 1, "weekday_night_start_hour": 18}
    now = dt.datetime(2026, 11, 9, 7, 0, tzinfo=KST)
    court = {"name": "선정 1면", "part": 23, "place": 12}
    cal_p = tuple(sorted(monitor.ep_params("C", "20261109", 23, 12).items()))
    t0 = tuple(sorted(monitor.ep_params("T", "20261109", 23, 12).items()))
    pages = {
        (monitor.EP_URL, cal_p): ep_calendar_html({9: True, 10: False}),
        (monitor.EP_URL, t0): ep_time_html([("08:00~09:00", "예약가능"), ("19:00~20:00", "예약가능"),
                                            ("20:00~21:00", "예약완료")]),
    }
    r = monitor.scan_eunpyeong(FakeFetcher(pages), court, cfg, None, now.date(), now)
    assert r["avail"] == ["2026-11-09|선정 1면|19:00~20:00"]  # 08:00 is weekday daytime -> filtered


def test_messages_group_and_split():
    items = [("고양", "성사실외 9 코트", dt.date(2026, 11, 14), "06:00~08:00", "https://x/?a=1&b=2"),
             ("고양", "성사실외 9 코트", dt.date(2026, 11, 14), "08:00~10:00", "https://x/?a=1&b=2")]
    msgs = monitor.build_messages(items, "🎾 빈자리 2건")
    assert len(msgs) == 1 and "11/14(토)" in msgs[0] and "&amp;" in msgs[0]
    many = [("은평", f"코트{i}", dt.date(2026, 11, 14), "06:00~07:00", "https://x") for i in range(400)]
    assert all(len(m) <= 4096 for m in monitor.build_messages(many, "t"))


def test_state_roundtrip_first_run_then_diff(tmp_path, monkeypatch):
    """End-to-end with a fake fetcher: baseline run, then a cancellation triggers one alert."""
    cfgp, stp = tmp_path / "c.json", tmp_path / "s.json"
    cfgp.write_text(json.dumps({"horizon_days": 0, "weekday_night_start_hour": 18,
                                "goyang": [{"name": "성사전천후", "id": 4}], "eunpyeong": []}))
    fixed = dt.datetime(2026, 11, 14, 5, 0, tzinfo=KST)  # Saturday, before the 06:00 slot

    class FixedDT(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed
    monkeypatch.setattr(monitor.dt, "datetime", FixedDT)
    d = "2026-11-14"
    base = f"{monitor.GY_BASE}/4/{d}"
    cur = {"html": gy_calendar({d: 1}) + gy_grid({(0, 2)})}
    monkeypatch.setattr(monitor, "Fetcher", lambda: type("F", (), {"get": lambda s, u, p=None: cur["html"]})())
    sent = []
    monkeypatch.setattr(monitor, "tg_send", lambda t: sent.append(t))
    argv = ["monitor.py", "--config", str(cfgp), "--state", str(stp)]
    monkeypatch.setattr(sys, "argv", argv)
    assert monitor.main() == 0 and "감시 시작" in sent[0]
    sent.clear()
    assert monitor.main() == 0 and sent == []                   # nothing changed -> silent
    cur["html"] = gy_calendar({d: 2}) + gy_grid({(0, 2), (1, 0)})
    assert monitor.main() == 0 and len(sent) == 1 and "빈자리 1건" in sent[0]
    assert "06:00~08:00" in sent[0] and "10 코트" in sent[0]
