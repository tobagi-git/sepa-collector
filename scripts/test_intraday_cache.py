"""장중 봉이 캐시에 굳지 않는지 확인하는 회귀 테스트 (네트워크·실캐시 무접촉).
2026-09-30 10:20 KST로 시계를 고정하고 Yahoo 응답을 가짜로 바꿔 끼운다 —
응답에 09-30 장중 봉과 close=None 봉이 섞여 와도 캐시 마지막 봉은 09-29여야 하고,
재호출 시 다시 수집을 시도해야 한다(= 영구 '종가'로 굳지 않는다).

    SEPA_WS=$(mktemp -d) python3 test_intraday_cache.py
"""
import os, sys, json, tempfile
from datetime import datetime as _dt, timedelta
from zoneinfo import ZoneInfo

os.environ.setdefault("SEPA_WS", tempfile.mkdtemp(prefix="sepa_test_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sepa  # noqa: E402

FROZEN = _dt(2026, 9, 30, 10, 20, tzinfo=ZoneInfo("Asia/Seoul"))


class FrozenDT(_dt):
    @classmethod
    def now(cls, tz=None):
        return FROZEN.astimezone(tz) if tz else FROZEN.replace(tzinfo=None)


sepa.datetime = FrozenDT
assert sepa.last_expected_session("kr") == "2026-09-29"
assert sepa.last_expected_session("us") == "2026-09-29"  # 09-29 21:20 ET → 09-29 끝남


def fake_bars(last_day, n=40, null_last=False):
    d0 = _dt.strptime(last_day, "%Y-%m-%d")
    days, d = [], d0
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    rows = [{"d": x, "c": 100.0 + i, "h": 101.0 + i, "l": 99.0 + i, "o": 100.0 + i,
             "rc": 100.0 + i, "v": 1000} for i, x in enumerate(reversed(days))]
    if null_last:
        rows[-1]["c"] = None
    return rows


calls = []


def run(market, sym, cache_last, yahoo_last, null_last=False, fn=None):
    calls.clear()
    p = sepa.data_path(market, sym)
    p.write_text(json.dumps({"t": sym, "bars": fake_bars(cache_last)}))

    def fake_yahoo(t, rng="2y"):
        calls.append(t)
        return fake_bars(yahoo_last, null_last=null_last)
    sepa.yahoo_daily = fake_yahoo
    (fn or (lambda: sepa.load_or_fetch(market, sym)))()
    return json.loads(p.read_text())["bars"]


ok = True
def check(name, cond):
    global ok
    ok &= cond
    print(("PASS " if cond else "FAIL ") + name)


# 1) load_or_fetch: 캐시 09-26, Yahoo가 09-30 장중 봉 포함 → 09-29까지만 저장
b = run("kr", "078930.KS", "2026-09-26", "2026-09-30")
check("load_or_fetch: 장중 봉 제외", b[-1]["d"] == "2026-09-29")
run("kr", "078930.KS", "2026-09-29", "2026-09-30")
check("load_or_fetch: 09-29 확정 봉 있으면 재수집 안 함", calls == [])

# 2) 장중 봉이 없던 시절 버그 재현 여부: 캐시 09-26 + 09-30만 오면 09-30이 남으면 안 된다
b = run("kr", "031980.KQ", "2026-09-26", "2026-09-30")
check("캐시 last < 09-30", all(x["d"] <= "2026-09-29" for x in b))

# 3) close=None 봉 제외 (미국 09-29 null 케이스)
b = run("us", "AAPL", "2026-09-25", "2026-09-29", null_last=True)
check("close=None 봉 제외", b[-1]["d"] == "2026-09-28" and all(x["c"] is not None for x in b))

# 4) 지수 경로
tk = sepa.INDEXES["kospi"]["ticker"]
b = run("index", tk, "2026-09-26", "2026-09-30", fn=lambda: sepa.load_or_fetch_index("kospi"))
check("load_or_fetch_index: 장중 봉 제외", b[-1]["d"] == "2026-09-29")

# 5) 벌크 fetch 경로
b = run("kr", "278470.KS", "2026-09-26", "2026-09-30",
        fn=lambda: sepa.fetch_one("kr", "278470.KS", sepa.last_expected_session("kr")))
check("fetch_one: 장중 봉 제외", b[-1]["d"] == "2026-09-29")

# 6) 회귀 가드 유지: Yahoo가 캐시보다 뒤처지면 기존 캐시 보존
b = run("kr", "222040.KQ", "2026-09-29", "2026-09-24",
        fn=lambda: sepa._keep_newer("kr", "222040.KQ", sepa.load_bars("kr", "222040.KQ"),
                         sepa.settled_bars(sepa.yahoo_daily("x"), "kr")))
check("_keep_newer 뒤처진 응답 거부", b[-1]["d"] == "2026-09-29")

# 7) 전부 걸러져 빈 응답이어도 캐시를 비우지 않는다
before = sepa.load_bars("kr", "222040.KQ")
sepa._keep_newer("kr", "222040.KQ", before, [])
check("빈 응답이 캐시 안 지움", sepa.load_bars("kr", "222040.KQ") == before)

sys.exit(0 if ok else 1)
