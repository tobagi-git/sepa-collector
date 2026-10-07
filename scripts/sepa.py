#!/usr/bin/env python3
"""
sepa.py — 미너비니 SEPA 발굴 엔진 (토큰 0 계산기)

미국·한국 전 상장사를 대상으로 추세 템플릿(Trend Template) 8기준 + RS 백분위를
기계적으로 계산한다. 판단(셋업 품질·리더십·펀더멘털 해석)은 스킬(모델)이 한다.

사용법:
  python3 sepa.py universe us|kr              # 유니버스 구축 (나스닥 심볼 목록 / KRX 상장사)
  python3 sepa.py fetch us|kr [--limit N] [--threads 6] [--force]
                                              # 가격 데이터 수집 (2y 일봉, 증분 — 신선하면 스킵)
  python3 sepa.py screen us|kr [--rs 70]      # 추세템플릿 + RS 스크리닝 → CSV/JSON
  python3 sepa.py check <티커>                 # 단일 종목 8기준 상세 판정 (한국 6자리 코드 허용)
  python3 sepa.py vcp <티커>                   # 베이스/VCP 수축 분석 (수치만 — 판정은 스킬)
  python3 sepa.py daily us|kr                 # 전일 대비 diff + 브레드스 추이 (데일리 리포트 원재료)
  python3 sepa.py groups us|kr                # 그룹 로테이션 — 핵심 로스터 통과율·테마 분포·Stage 1→2 전환 (주말 구조 패스용)

데이터: ~/.claude/skills/minervini-workspace/ 아래 (universe/, data/, out/)
의존성: 표준 라이브러리만. Yahoo v8 chart API + nasdaqtrader + KRX kind.
"""
import sys, os, json, csv, time, re, math, argparse
import urllib.request, urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SEPA_DIR = Path(__file__).resolve().parent
WS = Path(os.environ.get("SEPA_WS", str(Path.home() / ".claude" / "skills" / "minervini-workspace"))).expanduser()  # GitHub Actions에선 SEPA_WS=저장소 루트
UNIV = WS / "universe"
DATA = WS / "data"
OUT = WS / "out"
# ⚠️ Yahoo는 풀 브라우저 UA 문자열(TLS 핑거프린트 불일치)을 429로 차단 — 짧은 UA가 통과 (2026-07 실측)
UA = "Mozilla/5.0"

# 유동성 하한 (스크리닝 잡음 제거 — 필요 시 CLI로 조정)
MIN_PRICE = {"us": 10.0, "kr": 1000.0}          # 저가주 제외 (미너비니: 저가주 회피)
MIN_DVOL = {"us": 5_000_000, "kr": 1_000_000_000}  # 50일 평균 거래대금: $5M / 10억원

# 지수 레벨 국면(regime) 감시 — 오닐 FTD(팔로우스루 데이) 상태 머신용
INDEXES = {
    "kospi": {"ticker": "^KS11", "market": "kr"},
    "nasdaq": {"ticker": "^IXIC", "market": "us"},
    "sox": {"ticker": "^SOX", "market": "us"},
}
DRAWDOWN_TRIGGER = -8.0   # 252세션 고점 대비 이 이하로 빠지면 조정 국면 시작
FTD_MIN_DAY, FTD_MAX_DAY = 4, 10
FTD_MIN_RET = 1.7
GAP_MIN_PCT = 1.5   # 시가-전일종가 갭이 이 이상일 때만 갭/되돌림 필드를 채운다 (잡음 제외)


def http_get(url, timeout=20, retries=3):
    # Yahoo가 python urllib의 TLS 핑거프린트를 429로 차단하므로 curl을 쓴다 (2026-07 실측)
    #
    # ⚠️ 타임아웃은 짧게 잡는다: 2년 일봉 JSON은 20~50KB라 정상 응답은 1초 안에 끝난다.
    #    30초를 기다리는 건 사실상 전부 스로틀 대기이고, 실패 1건당 3회 × 30초 = 90초를
    #    태워 KR 전량 수집이 4.5시간으로 늘어난 주범이었다 (2026-07-27 실측:
    #    fail=308 × 96초 ≈ 스레드시간 35,000초 = 전체의 36%).
    import subprocess, random
    last = None
    for i in range(retries):
        r = subprocess.run(
            ["curl", "-s", "--compressed", "--max-time", str(timeout),
             "--connect-timeout", "8",
             "-A", UA, "-w", "\n%{http_code}", url],
            capture_output=True)
        body, _, code = r.stdout.rpartition(b"\n")
        code = code.strip().decode("ascii", "ignore")
        if code == "200" and body:
            return body
        last = RuntimeError(f"HTTP {code or 'timeout'}: {url}")
        if code in ("429", "999", "502", "503", "000", ""):
            # 지수 백오프 + 지터 — 6스레드가 같은 박자로 재시도해 429를 자가증폭하는 것을 막는다
            time.sleep(min(2 ** i, 8) + random.uniform(0, 1.5))
            continue
        break
    raise last


# ---------------- universe ----------------

BAD_NAME = re.compile(
    r"Warrant|Right(s)?\b|\bUnit(s)?\b|Preferred|Depositary|ETN|Notes? due|"
    r"Acquisition Corp|Blank Check|기업인수목적", re.I)


def universe_us():
    UNIV.mkdir(parents=True, exist_ok=True)
    rows = []
    nas = http_get("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt").decode("utf-8", "ignore")
    for line in nas.splitlines()[1:]:
        f = line.split("|")
        if len(f) < 8 or f[0] == "" or line.startswith("File Creation"):
            continue
        sym, name, test, etf = f[0], f[1], f[3], f[6]
        if test == "Y" or etf == "Y" or BAD_NAME.search(name):
            continue
        rows.append((sym, name))
    oth = http_get("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt").decode("utf-8", "ignore")
    for line in oth.splitlines()[1:]:
        f = line.split("|")
        if len(f) < 8 or f[0] == "" or line.startswith("File Creation"):
            continue
        sym, name, exch, etf, test = f[0], f[1], f[2], f[4], f[6]
        if test == "Y" or etf == "Y" or exch == "P" or BAD_NAME.search(name):
            continue  # P(Arca)는 대부분 ETF
        if "$" in sym or "." in sym:
            continue  # 우선주·클래스 표기
        rows.append((sym.replace("/", "-"), name))
    seen, out = set(), []
    for s, n in rows:
        if s not in seen:
            seen.add(s)
            out.append((s, n))
    p = UNIV / "us.tsv"
    p.write_text("\n".join(f"{s}\t{n}" for s, n in out), encoding="utf-8")
    print(f"US universe: {len(out)} tickers -> {p}")


def universe_kr():
    UNIV.mkdir(parents=True, exist_ok=True)
    out = []
    html = http_get("https://kind.krx.co.kr/corpgeneral/corpList.do?method=download",
                    timeout=60).decode("cp949", "ignore")
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        tds = [re.sub(r"<[^>]+>", "", td).strip() for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        # 컬럼: 회사명, 시장구분, 종목코드, 업종, ...
        if len(tds) < 3 or not re.fullmatch(r"\d{6}", tds[2]):
            continue
        name, mkt, code = tds[0], tds[1], tds[2]
        if "코스닥" in mkt:
            suffix = ".KQ"
        elif "유가" in mkt or "KOSPI" in mkt.upper():
            suffix = ".KS"
        else:
            continue  # 코넥스 등 제외
        if BAD_NAME.search(name) or "스팩" in name:
            continue
        out.append((code + suffix, name))
    p = UNIV / "kr.tsv"
    p.write_text("\n".join(f"{s}\t{n}" for s, n in out), encoding="utf-8")
    print(f"KR universe: {len(out)} tickers -> {p}")


def load_universe(market):
    p = UNIV / f"{market}.tsv"
    if not p.exists():
        sys.exit(f"유니버스 없음: {p} — 먼저 `sepa.py universe {market}` 실행")
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if "\t" in line:
            s, n = line.split("\t", 1)
            rows.append((s, n))
    return rows


# ---------------- fetch ----------------

def yahoo_daily(ticker, rng="2y"):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(ticker)}"
           f"?range={rng}&interval=1d&events=div%2Csplit")
    # HTTP 200인데 본문이 잘려 오는 경우가 있다("Expecting ',' delimiter" — 2026-07 실측 KR/US 합 6건).
    # http_get은 코드만 보므로 파싱 실패는 재시도되지 않아 그대로 실패로 굳었다 — 여기서 한 번 더 준다.
    for attempt in range(2):
        try:
            d = json.loads(http_get(url))
            break
        except json.JSONDecodeError:
            if attempt == 1:
                raise
            time.sleep(1.0)
    r = d.get("chart", {}).get("result")
    if not r:
        raise ValueError(d.get("chart", {}).get("error") or "no result")
    r = r[0]
    ts = r.get("timestamp") or []
    q = r["indicators"]["quote"][0]
    adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    close = adj if adj else q.get("close")
    rows = []
    for i, t in enumerate(ts):
        c, h, l, v = close[i], q["high"][i], q["low"][i], q["volume"][i]
        rc = q["close"][i]  # 거래대금은 비조정 종가로
        o = q.get("open", [None] * len(ts))[i]
        if c is None or h is None or l is None:
            continue
        rows.append({"d": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"),
                     "c": round(c, 4), "h": round(h, 4), "l": round(l, 4),
                     "o": round(o, 4) if o is not None else None,
                     "rc": round(rc, 4) if rc is not None else round(c, 4),
                     "v": int(v or 0)})
    if len(rows) < 30:
        raise ValueError(f"데이터 부족 ({len(rows)} bars)")
    return rows


def data_path(market, ticker):
    d = DATA / market
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{ticker.replace('/', '-')}.json"


def last_expected_session(market):
    """이 시장의 '마지막으로 끝난 세션' 날짜. 이보다 오래된 캐시는 재수집.
    각 시장의 현지 시계로 '장 마감+30분'을 게이트로 쓴다 — Yahoo 일봉의 날짜는
    세션 현지 날짜와 같으므로(미국 09:30 ET = 13:30 UTC, KRX 09:00 KST = 00:00 UTC)
    현지 시계로 재는 것이 정본.

    ⚠️ us를 'KST 어제'로 어림하면 안 된다: 22:30~05:00 KST는 미국 장중인데
    어제(=진행 중인 세션)를 끝난 것으로 판정해, 미확정 장중 봉을 종가로 캐시하고
    is_fresh까지 통과시킨다. 2026-07-24 00:56 KST 실측 — ^IXIC 7/23 봉이 5분 간격
    두 호출 사이에 25,035.36 → 25,093.22(거래량 2.98bn → 3.07bn, 평시 7~8bn의 40%)로
    변했고 regime이 이를 종가로 읽었다. 지수뿐 아니라 fetch/load_or_fetch 경로도
    같은 게이트를 쓰므로, 그 시간대에 돌면 미국 유니버스 전체가 장중 봉으로
    오염된 채 'fresh'로 굳는다."""
    if market == "kr":
        now = datetime.now()  # 이 머신은 KST
        close_at = (16, 0)    # 15:30 마감 + 30분
    else:
        # 미국 동부 현지시각 — DST 전환에 영향받지 않는다 (EDT/EST 자동)
        now = datetime.now(ZoneInfo("America/New_York"))
        close_at = (16, 30)   # 16:00 마감 + 30분
    d = now.date()
    if not (d.weekday() < 5 and (now.hour, now.minute) >= close_at):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def settled_bars(bars, market):
    """캐시에 쓸 수 있는 **확정** 봉만 남긴다 — 마지막 끝난 세션보다 뒤의 봉(진행 중인
    장중 봉)과 종가가 비어 있는 봉을 버린다.
    ⚠️ 신선도 게이트(last_expected_session)만으로는 부족하다: 게이트는 '다시 받을지'만
    정하고, 받은 응답에 오늘 장중 봉이 섞여 오면 그대로 써 버린다. 그러면 bars[-1]["d"]가
    target 이상이 돼 다시는 재수집되지 않고 장중가가 영구 '종가'로 굳는다. 2026-09-30
    10:20 KST 실측 — 전일 봉이 없던 078930.KS·031980.KQ·278470.KS·222040.KQ에 09-30
    장중 봉이 캐시됐다(같은 시각 미국 09-29 봉은 close=null로 왔다)."""
    cutoff = last_expected_session(market)
    return [b for b in bars if b["d"] <= cutoff and b.get("c") is not None]


# 영구 실패(상장폐지·신규상장·심볼 오류)와 일시 실패(스로틀·타임아웃)를 가른다.
# 영구만 쿨다운을 주고, 일시 실패는 다음 회차에 바로 재시도한다.
# ⚠️ 429는 4xx지만 순수 스로틀이라 영구가 아니다 — 여기 넣으면 가장 흔한 일시 실패에
#    7일 쿨다운이 걸려 고치려던 래칫이 그대로 재현된다. 408(타임아웃)도 같은 이유로 제외.
PERMANENT_ERR = re.compile(r"HTTP 4(?!29|08)\d\d|데이터 부족|no result|not found", re.I)


def is_fresh(p, target):
    if not p.exists():
        return False
    try:
        j = json.loads(p.read_text())
    except Exception:
        return False
    if "error" in j and not (j.get("bars") or []):
        # ⚠️ 일시 실패에 쿨다운을 주면 안 된다: 한 번 스로틀당한 종목이 7일간 재시도조차 되지
        #    않아 풀에서 사라진 채 굳는다. 2026-07 KR 풀이 1,256 → 957로 단조 감소한 원인이
        #    정확히 이것 — 매일의 일시 실패가 일주일씩 누적되는 래칫이었다.
        if PERMANENT_ERR.search(j.get("error", "")):
            return (time.time() - p.stat().st_mtime) < 7 * 86400
        return False
    bars = j.get("bars") or []
    # 휴장일엔 target 봉이 영영 안 생기므로 target 직전 3거래일 내면 신선으로 간주하지 않고
    # 재수집한다 — 다만 하루 1회 스로틀은 daily_collect.sh 몫이라 과다 호출은 없다.
    return bool(bars) and bars[-1]["d"] >= target


def fetch_one(market, ticker, target):
    p = data_path(market, ticker)
    if is_fresh(p, target):
        return "skip"
    try:
        bars = settled_bars(yahoo_daily(ticker), market)
        p.write_text(json.dumps({"t": ticker, "bars": bars}))
        return "ok"
    except Exception as e:
        msg = str(e)[:200]
        # ⚠️ 실패했다고 기존 캐시를 덮지 않는다 — 이 한 줄이 2026-07-27에 KR 126종목의
        #    2년치 이력을 지웠다(SK이터닉스는 07-24 템플릿 8/8 통과 종목이었는데 파일이
        #    error 마커 한 줄만 남아 풀에서 증발, 데일리가 '템플릿 이탈'로 오보). 전송 실패는
        #    데이터에 대한 정보가 아니므로 직전 이력을 그대로 두고 실패 사실만 덧붙인다.
        prev = {}
        if p.exists():
            try:
                prev = json.loads(p.read_text())
            except Exception:
                prev = {}
        if prev.get("bars"):
            prev["stale_error"] = msg
            prev["stale_since"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            p.write_text(json.dumps(prev))
            return "stale"
        p.write_text(json.dumps({"t": ticker, "error": msg}))
        return "fail"


def cmd_fetch(market, limit=None, threads=6, force=False):
    univ = load_universe(market)
    if limit:
        univ = univ[:limit]
    if force:
        for s, _ in univ:
            p = data_path(market, s)
            if p.exists():
                p.unlink()
    stats = {"ok": 0, "skip": 0, "fail": 0, "stale": 0}
    t0 = time.time()
    target = last_expected_session(market)
    print(f"신선도 기준 세션: {target}")
    log = WS / f"fetch_{market}.log"
    lines = []

    def emit(msg):
        print(msg, flush=True)
        lines.append(msg)
        log.write_text("\n".join(lines[-12:]))

    misses = []
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = {ex.submit(fetch_one, market, s, target): s for s, _ in univ}
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            stats[res] += 1
            if res in ("fail", "stale"):
                misses.append(futs[fut])
            if i % 200 == 0 or i == len(futs):
                emit(f"[{i}/{len(futs)}] ok={stats['ok']} skip={stats['skip']} "
                     f"fail={stats['fail']} stale={stats['stale']} {time.time()-t0:.0f}s")
            time.sleep(0.02)  # 전역 살짝 감속

    # 재시도 스윕 — 1차 실패의 대부분은 종목 문제가 아니라 순간 스로틀이다. 동시성을 낮춰
    # 한 번 더 훑으면 대부분 회수된다. 영구 실패(404·데이터 부족)는 http_get이 재시도하지
    # 않으므로 여기서도 금방 끝난다.
    if misses:
        emit(f"재시도 스윕: {len(misses)}종목 (threads=2)")
        recovered = 0
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = {ex.submit(fetch_one, market, s, target): s for s in misses}
            for fut in as_completed(futs):
                if fut.result() == "ok":
                    recovered += 1
                time.sleep(0.05)
        emit(f"재시도 회수: {recovered}/{len(misses)}")

    # 최종 상태를 파일에서 다시 센다 (스윕 결과 반영)
    final = {"ok": 0, "stale": 0, "fail": 0}
    for s, _ in univ:
        p = data_path(market, s)
        try:
            j = json.loads(p.read_text())
        except Exception:
            final["fail"] += 1
            continue
        if j.get("bars"):
            final["stale" if (j["bars"][-1]["d"] < target) else "ok"] += 1
        else:
            final["fail"] += 1
    emit(f"fetch {market} 완료 {time.time()-t0:.0f}s | 최신={final['ok']} "
         f"낡음(이력보존)={final['stale']} 데이터없음={final['fail']}")


# ---------------- 지표 계산 ----------------

def sma(vals, n, at=-1):
    idx = len(vals) + at if at < 0 else at
    if idx + 1 < n:
        return None
    return sum(vals[idx + 1 - n: idx + 1]) / n


def pct_ret(closes, n):
    if len(closes) <= n:
        return None
    prev = closes[-1 - n]
    return (closes[-1] / prev - 1) * 100 if prev else None


def rs_raw(closes):
    """IBD류 가중 수익률: 최근 3개월 2배 가중 + 6/9/12개월."""
    r63, r126, r189, r252 = (pct_ret(closes, n) for n in (63, 126, 189, 252))
    if r63 is None:
        return None
    parts = [2 * r63] + [r for r in (r126, r189, r252) if r is not None]
    return sum(parts)


def trend_template(bars, rs_pct=None):
    """추세 템플릿 8기준. rs_pct(백분위)가 없으면 기준8은 None."""
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    c = closes[-1]
    s50, s150, s200 = sma(closes, 50), sma(closes, 150), sma(closes, 200)
    s200_22 = sma(closes, 200, at=len(closes) - 1 - 22)
    # 200MA 상승 개월 수 (최대 5개월 확인)
    up_months = 0
    for m in range(1, 6):
        a, b2 = sma(closes, 200, at=len(closes) - 1 - 22 * (m - 1)), sma(closes, 200, at=len(closes) - 1 - 22 * m)
        if a is None or b2 is None or a <= b2:
            break
        up_months = m
    lo52 = min(lows[-252:]) if len(lows) >= 60 else min(lows)
    hi52 = max(highs[-252:]) if len(highs) >= 60 else max(highs)
    vs_low = (c / lo52 - 1) * 100 if lo52 else None
    from_high = (c / hi52 - 1) * 100 if hi52 else None
    crit = {
        "1_price_gt_150_200": None if not (s150 and s200) else (c > s150 and c > s200),
        "2_150_gt_200": None if not (s150 and s200) else s150 > s200,
        "3_200ma_rising_1m": None if not (s200 and s200_22) else s200 > s200_22,
        "4_50_gt_150_gt_200": None if not (s50 and s150 and s200) else (s50 > s150 > s200),
        "5_price_gt_50": None if not s50 else c > s50,
        "6_ge_30pct_above_52wlow": None if vs_low is None else vs_low >= 30,
        "7_within_25pct_of_52whigh": None if from_high is None else from_high >= -25,
        "8_rs_ge_70": None if rs_pct is None else rs_pct >= 70,
    }
    vals = {"close": c, "sma50": s50, "sma150": s150, "sma200": s200,
            "sma200_up_months": up_months, "pct_vs_52w_low": vs_low,
            "pct_from_52w_high": from_high, "rs_raw": rs_raw(closes), "rs_pct": rs_pct}
    return crit, vals


def dollar_vol_50d(bars):
    tail = bars[-50:]
    if not tail:
        return 0
    return sum(b["rc"] * b["v"] for b in tail) / len(tail)


# ---------------- 시가총액 ----------------

def fetch_mcap(market):
    """전 종목 시가총액 맵 {티커: 시총(현지통화)}. 실패 시 캐시 폴백 — 시총은 정렬용이라 하루 이틀 낡아도 무방."""
    cache = OUT / f"mcap_{market}.json"
    try:
        m = {}
        if market == "us":
            d = json.loads(http_get(
                "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25000", timeout=60))
            for r in d.get("data", {}).get("table", {}).get("rows", []) or []:
                mc = (r.get("marketCap") or "").replace(",", "").strip()
                if mc and mc not in ("0.00", "NA"):
                    m[r["symbol"].replace("/", "-")] = float(mc)
        else:
            for cat, suffix in (("KOSPI", ".KS"), ("KOSDAQ", ".KQ")):
                page = 1
                while page < 40:
                    d = json.loads(http_get(
                        f"https://m.stock.naver.com/api/stocks/marketValue/{cat}?page={page}&pageSize=100"))
                    stocks = d.get("stocks") or []
                    if not stocks:
                        break
                    for s in stocks:
                        mv = (s.get("marketValue") or "").replace(",", "")
                        if mv.isdigit():
                            m[s["itemCode"] + suffix] = int(mv) * 1e8  # 억원 → 원
                    page += 1
                    time.sleep(0.15)
        if m:
            OUT.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(m))
            return m
    except Exception as e:
        print(f"⚠️ 시총 수집 실패({e}) — 캐시 폴백")
    return json.loads(cache.read_text()) if cache.exists() else {}


# ---------------- screen ----------------

def load_bars(market, ticker):
    p = data_path(market, ticker)
    if not p.exists():
        return None
    try:
        j = json.loads(p.read_text())
    except Exception:
        return None
    return j.get("bars")


# ---- 수집 게이트 (2026-09-30) ----
# fetch가 대부분 실패한 채 screen이 돌면 모수가 몇십 종목으로 쪼그라든 스냅샷이 브레드스 행·
# rs_{market}.json(check가 읽는 RS 분포)에 그대로 굳는다. 2026-09-29 실측: 미국 09-28이 모수 26
# (정상 ~2,360)으로 기록됐고, 워크플로 가드(have==target)가 그 손상 행을 '이미 수집됨'으로 읽어
# 재실행까지 막았다. 그래서 산출물을 쓰기 *전에* 직전 모수와 비교해 급감이면 중단한다.
# 중단 시 have가 그대로라 같은 날의 다음 크론 슬롯이 알아서 재시도한다(스로틀은 대개 일시적).
GATE_MIN_RATIO = 0.7     # 직전 5행 모수 중앙값의 70% 미만이면 중단. 정상 일변동 ±1~5%, 09-18 저모수(2,230)도 94%라 통과
GATE_LOOKBACK = 5
GATE_MIN_HISTORY = 3     # 기준선이 될 이력이 이보다 적으면(신규 시장·초기화 직후) 게이트 없이 통과
GATE_EXIT_CODE = 3       # 워크플로가 '게이트 발동'과 일반 실패를 가르는 코드


def collection_gate(market, pool_n, stale_excluded=0):
    """(통과 여부, 메시지). 이력 기준선 대비 모수가 급감했는지만 본다 — 판정은 산출물 기록 전에."""
    hist_p = OUT / f"breadth_{market}.jsonl"
    if not hist_p.exists():
        return True, ""
    pools = []
    for line in hist_p.read_text().splitlines():
        if line.strip():
            try:
                p = json.loads(line).get("pool")
            except ValueError:
                continue
            if isinstance(p, int):
                pools.append(p)
    pools = pools[-GATE_LOOKBACK:]
    if len(pools) < GATE_MIN_HISTORY:
        return True, ""
    base = sorted(pools)[len(pools) // 2]          # 중앙값 — 직전 1행이 이미 손상돼도 기준선이 안 무너진다
    if not base or pool_n / base >= GATE_MIN_RATIO:
        return True, ""
    return False, (f"⛔ 수집 게이트 발동 [{market}] — 모수 {pool_n:,} (직전 {len(pools)}행 중앙값 {base:,}의 "
                   f"{pool_n / base:.0%}, 하한 {GATE_MIN_RATIO:.0%}). 세션 봉 없어 제외 {stale_excluded:,}종목 → "
                   f"fetch가 대량 실패(스로틀 의심)한 것으로 보고 산출물을 기록하지 않았다. "
                   f"정상 급감이 확실하면 `screen {market} --allow-drop`")


def cmd_screen(market, rs_cut=70, min_price=None, min_dvol=None, as_of=None, allow_drop=False):
    """as_of가 주어지면 그 세션까지로 봉을 잘라 '그날 시점의' 스크리닝을 재현한다(소급 계산).
    RS·52주 고저·MA·거래대금 모두 후행 윈도우라 절단만으로 정확히 재현된다.
    ⚠️ 단 시가총액은 현재값 캐시다 — 정렬용 보조 필드이므로 소급 모드에선 네트워크 조회를 건너뛴다."""
    backfill = as_of is not None
    OUT.mkdir(parents=True, exist_ok=True)
    univ = load_universe(market)
    names = dict(univ)
    min_price = min_price if min_price is not None else MIN_PRICE[market]
    min_dvol = min_dvol if min_dvol is not None else MIN_DVOL[market]

    # 1패스: RS raw 수집 (유동성 통과 종목만 백분위 모수로)
    #
    # fetch가 실패해도 이력을 보존하게 된 뒤로는(2026-07-27) '봉은 있는데 이번 세션 봉만 없는'
    # 종목이 생긴다. 그대로 담으면 낡은 종가가 브레드스에 섞이므로 여기서 제외하고 세어 둔다.
    # 소급(backfill) 모드는 봉을 as_of로 자르는 재현 경로라 이 필터를 적용하지 않는다.
    min_session = None if backfill else last_expected_session(market)
    stale_excluded = 0
    pool = []
    for s, _ in univ:
        bars = load_bars(market, s)
        if backfill and bars:
            bars = [b for b in bars if b["d"] <= as_of]
        if not bars or len(bars) < 220:
            continue
        c = bars[-1]["rc"]
        dv = dollar_vol_50d(bars)
        if c < min_price or dv < min_dvol:
            continue
        # 유동성 통과분에 대해서만 센다 — 앞에 두면 어차피 걸러질 저가·거래정지 소형주까지
        # 세어 경고가 상시 울린다(2026-07-27 실측: 110건 중 대부분이 1,000원 미만 종목).
        if min_session and bars[-1]["d"] < min_session:
            stale_excluded += 1
            continue
        raw = rs_raw([b["c"] for b in bars])
        if raw is None:
            continue
        pool.append((s, raw, bars, dv))
    if not pool:
        sys.exit("스크리닝 모수 0 — fetch가 됐는지 확인")
    # 소급(backfill) 모드는 과거 시점 재현이라 모수가 원래 작을 수 있어 게이트 대상이 아니다
    if not backfill and not allow_drop:
        gate_ok, gate_msg = collection_gate(market, len(pool), stale_excluded)
        if not gate_ok:
            print(gate_msg)
            sys.exit(GATE_EXIT_CODE)   # rs_/mcap_/screen_/breadth_ 어느 것도 쓰기 전
    ranked = sorted(pool, key=lambda x: x[1])
    rs_pct_map = {s: round((i + 1) / len(ranked) * 99, 1) for i, (s, *_rest) in enumerate(ranked)}
    if not backfill:
        # 소급 모드에선 rs_{market}.json(=check가 쓰는 현재 RS 분포)을 과거 값으로 덮지 않는다
        (OUT / f"rs_{market}.json").write_text(json.dumps(rs_pct_map))
    if backfill:
        cache = OUT / f"mcap_{market}.json"
        mcap = json.loads(cache.read_text()) if cache.exists() else {}
    else:
        mcap = fetch_mcap(market)

    # 2패스: 추세 템플릿 (+ 브레드스 집계)
    results = []
    br = {"above_200ma": 0, "above_50ma": 0, "new_52w_high": 0, "new_52w_low": 0}
    for s, raw, bars, dv in pool:
        crit, vals = trend_template(bars, rs_pct_map[s])
        c = vals["close"]
        if vals["sma200"] and c > vals["sma200"]:
            br["above_200ma"] += 1
        if vals["sma50"] and c > vals["sma50"]:
            br["above_50ma"] += 1
        hs = [b["h"] for b in bars]
        ls = [b["l"] for b in bars]
        # 신고가·신저가는 브레드스 카운트만 하지 말고 종목별로도 남긴다 (2026-08-15 추가).
        # 카운트만 있으면 "몇 개"는 알아도 "무엇"을 알 수 없어 후보 발굴에 못 쓴다.
        nh = nl = False
        if len(hs) > 30:
            nh = hs[-1] >= max(hs[-252:])
            nl = ls[-1] <= min(ls[-252:])
            if nh:
                br["new_52w_high"] += 1
            if nl:
                br["new_52w_low"] += 1
        n_pass = sum(1 for v in crit.values() if v)
        results.append({
            "ticker": s, "name": names.get(s, ""), "close": vals["close"],
            "rs": rs_pct_map[s], "mcap": mcap.get(s), "dvol_50d": round(dv),
            "vs_52w_low": round(vals["pct_vs_52w_low"], 1),
            "from_52w_high": round(vals["pct_from_52w_high"], 1),
            "sma200_up_months": vals["sma200_up_months"],
            "n_pass": n_pass, "pass_all": n_pass == 8,
            "new_52w_high": nh, "new_52w_low": nl,
            **{k: crit[k] for k in crit},
        })
    passed = [r for r in results if r["pass_all"] and r["rs"] >= rs_cut]
    near = [r for r in results if r["n_pass"] == 7 and r["rs"] >= rs_cut]
    passed.sort(key=lambda r: -r["rs"])
    near.sort(key=lambda r: -r["rs"])

    # ⚠️ 산출물 키는 '실행일'이 아니라 '세션일(as_of)'이다.
    #    실행일로 키를 잡으면 (a) 같은 세션이 여러 파일로 중복되고 (b) 그 중복끼리 diff가 나서
    #    daily가 "변화 없음"을 허위 보고한다 (2026-07 실측: screen_us_20260718/20260720이
    #    둘 다 as_of 07-17이라 07-20 데일리 diff가 공허했다).
    session = max(b[2][-1]["d"] for b in pool[:50])   # 실제 데이터 기준일
    if backfill and session != as_of:
        print(f"⚠️ 요청 as_of={as_of} 인데 실제 최종 세션은 {session} — 휴장일로 보고 그 세션으로 기록")
    key = session.replace("-", "")
    computed_on = datetime.now().strftime("%Y%m%d")

    cols = list(results[0].keys())
    csv_p = OUT / f"screen_{market}_{key}.csv"
    # 2026-09-22: 소급 모드도 CSV를 쓴다. 구 "JSON만" 정책 때문에 backfill 세션이 직전이 되면
    # daily의 stage2_crossovers·groups가 그 세션을 빈 명단으로 읽어 조용히 0건이 됐다
    # (실측: KR 09-15 CSV 부재, 09-16은 실패 런의 642행 부분 CSV 위에 backfill JSON 898).
    if True:
        with open(csv_p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in sorted(results, key=lambda r: -r["rs"]):
                w.writerow(r)
    breadth = {
        "date": key, "as_of": session, "computed_on": computed_on, "backfilled": backfill,
        "pool": len(pool),
        "pct_above_200ma": round(br["above_200ma"] / len(pool) * 100, 1),
        "pct_above_50ma": round(br["above_50ma"] / len(pool) * 100, 1),
        "new_52w_high": br["new_52w_high"], "new_52w_low": br["new_52w_low"],
        "net_new_highs": br["new_52w_high"] - br["new_52w_low"],
        "pass_all_count": len(passed), "near_miss_count": len(near),
    }
    hist_p = OUT / f"breadth_{market}.jsonl"
    rows = [json.loads(l) for l in (hist_p.read_text().splitlines() if hist_p.exists() else []) if l.strip()]
    rows = [r for r in rows if r.get("as_of") != session]      # 세션일로 dedupe
    rows.append(breadth)
    rows.sort(key=lambda r: r.get("as_of") or "")
    hist_p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    summary = {
        "market": market, "date": key, "as_of": session, "computed_on": computed_on,
        "backfilled": backfill, "universe": len(univ),
        "with_data": len(pool), "stale_excluded": stale_excluded,
        "filters": {"min_price": min_price, "min_dvol": min_dvol, "rs_cut": rs_cut},
        "breadth": breadth,
        "pass_all_count": len(passed), "near_miss_count": len(near),
        "pass_all": passed, "near_miss_7of8": near[:50],
        # 신고가 ∩ 템플릿 8/8 ∩ RS컷 — 발굴 깔때기의 1단계 산출 (2026-08-15 추가).
        # 원본 신고가 리스트(SPAC·CEF·우선주·저유동성 포함)와 달리 이미 유니버스·유동성
        # 필터를 통과한 것만이라 바로 후보로 쓸 수 있다. 절삭하지 않는다.
        "new_high_pass": [r for r in passed if r["new_52w_high"]],
        "new_high_pass_count": sum(1 for r in passed if r["new_52w_high"]),
    }
    js_p = OUT / f"screen_{market}_{key}.json"
    js_p.write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"[as_of {session}] 모수 {len(pool)} (유동성 통과) / 8기준 전부 통과 {len(passed)} / 7기준 {len(near)}")
    if stale_excluded:
        print(f"⚠️ 이번 세션 봉이 없어 제외 {stale_excluded}종목 — 수집 실패분(이력은 보존됨). "
              f"이 수가 크면 브레드스 %를 전일과 직접 비교하지 말 것")
    print(f"-> {js_p}\n-> {csv_p} (전 종목)")
    if not backfill:
        for r in passed[:30]:
            print(f"  {r['ticker']:>8} RS {r['rs']:>4} | 저점比 +{r['vs_52w_low']}% | 고점比 {r['from_52w_high']}% | {r['name'][:40]}")
    return session


# ---------------- check ----------------

def resolve_ticker(t):
    t = t.upper().strip()
    if re.fullmatch(r"\d{6}", t):
        # 유니버스에 있는 접미사를 우선 (같은 코드가 .KS로도 조회되는 경우 오배정 방지)
        up = UNIV / "kr.tsv"
        if up.exists():
            for line in up.read_text(encoding="utf-8").splitlines():
                if line.startswith(t):
                    return [(line.split("\t", 1)[0], "kr")]
        return [(t + ".KS", "kr"), (t + ".KQ", "kr")]
    if t.endswith(".KS") or t.endswith(".KQ"):
        return [(t, "kr")]
    return [(t, "us")]


def _keep_newer(bucket, ticker, old, new):
    """수집 결과를 캐시에 반영할지 판정. 상류가 기존 캐시보다 **뒤처진** 응답을 주면
    쓰지 않는다 — Yahoo가 일시적으로 최신 봉을 누락한 채 응답하는 일이 있고, 그때
    덮어쓰면 이미 갖고 있던 확정 봉이 사라진다(2026-07-24 실측: ^KS11 캐시에 있던
    7/23 봉이, 7/22까지만 담긴 Yahoo 응답에 덮여 소실. 대체 소스로 보강해 둔 봉도
    같은 방식으로 날아간다). 같은 소스에서 온 더 짧은 시계열이 더 긴 것의 올바른
    대체본인 경우는 사실상 없으므로 뒤처진 응답은 버린다."""
    if not new:
        return old  # 확정 봉만 거르고 나니 빈 응답 — 캐시를 비우지 않는다
    if old and new[-1]["d"] < old[-1]["d"]:
        return old
    data_path(bucket, ticker).write_text(json.dumps({"t": ticker, "bars": new}))
    return new


def load_or_fetch(market, sym):
    bars = load_bars(market, sym)
    if not bars or bars[-1]["d"] < last_expected_session(market):
        try:
            bars = _keep_newer(market, sym, bars, settled_bars(yahoo_daily(sym), market))
        except Exception:
            pass  # 낡았지만 캐시라도 있으면 그걸 쓴다 (as_of로 드러남)
    return bars


# ---------------- regime (지수 레벨 FTD/드로다운) ----------------

def load_or_fetch_index(label):
    """INDEXES[label] 하나를 캐시-aware로 로드/수집. 저장 버킷은 'index',
    신선도 판정 캘린더는 해당 지수의 소속 시장(kr/us) — 두 개념을 분리해
    기존 load_or_fetch/주식 경로는 건드리지 않는다."""
    cfg = INDEXES[label]
    ticker, cal_market = cfg["ticker"], cfg["market"]
    bars = load_bars("index", ticker)
    if not bars or bars[-1]["d"] < last_expected_session(cal_market):
        try:
            bars = _keep_newer("index", ticker, bars,
                               settled_bars(yahoo_daily(ticker), cal_market))
        except Exception:
            pass  # 낡았지만 캐시라도 있으면 그걸 쓴다 (as_of로 드러남)
    return bars


def compute_regime(bars):
    """지수 일봉 전체를 처음부터 재계산(stateless)해 조정 국면/FTD 상태를 판정.
    영속 상태를 두지 않는 이유: 국면 시작→FTD 확정(or 윈도우 경과로 종료)까지의
    전체 이력을 매번 재구성하므로 cold-start·마이그레이션 문제가 없고,
    FTD 윈도우가 그냥 지나갔는데 계속 in_correction으로 눌어붙는 버그(실측으로 확인됨 —
    영속 상태였다면 옛 조정이 이후의 새 급락까지 통째로 삼켜버렸다)도 원천적으로 막힌다."""
    closes = [b["c"] for b in bars]
    dates = [b["d"] for b in bars]
    vols = [b.get("v") or 0 for b in bars]
    in_correction = False
    baseline_low = baseline_date = None
    rebound_days = 0
    ftd_status, ftd_date, ftd_vol_ok = "none", None, None
    for i in range(len(closes)):
        if i < 1:
            continue
        win_start = max(0, i - 251)
        high252 = max(closes[win_start:i + 1])
        dd = (closes[i] / high252 - 1) * 100 if high252 else 0
        if not in_correction:
            # dd<=TRIGGER만으로 진입시키면 FTD 확정 직후(in_correction=False로 리셋된) 다음날처럼
            # 지수가 오른 날도 252일 고점이 그대로라 dd가 여전히 트리거 이하라는 이유만으로
            # "신저점"으로 재진입해버리는 사고가 난다(실측: 2026-08-14 KOSPI +2.42%인데도 재진입).
            # 전일 대비 실제로 밀린 날에만 진입시켜 new_low_today가 "진짜 신저점"만 가리키게 한다.
            if dd <= DRAWDOWN_TRIGGER and closes[i] < closes[i - 1]:
                in_correction = True
                baseline_low, baseline_date = closes[i], dates[i]
                rebound_days, ftd_status, ftd_date, ftd_vol_ok = 0, "none", None, None
            continue
        if closes[i] < baseline_low:
            baseline_low, baseline_date = closes[i], dates[i]
            rebound_days, ftd_status, ftd_date, ftd_vol_ok = 0, "none", None, None
            continue
        rebound_days += 1
        if FTD_MIN_DAY <= rebound_days <= FTD_MAX_DAY:
            ret = (closes[i] / closes[i - 1] - 1) * 100
            vol_ok = None if not vols[i] or not vols[i - 1] else vols[i] > vols[i - 1]
            if ret >= FTD_MIN_RET and vol_ok is not False:
                ftd_status, ftd_date, ftd_vol_ok = "confirmed", dates[i], vol_ok
                # FTD 확정 = 이 조정 국면의 종료. 완전 회복(전고점 돌파)까지 기다리지 않는다 —
                # 실측(나스닥 2025-04)상 기다리면 다음 급락을 새 국면으로 못 잡는 동일한 정체 버그가 재현됨.
                in_correction = False
                continue
        if rebound_days > FTD_MAX_DAY:
            # FTD 윈도우 경과 — 확정 못 했으면 이 조정 국면은 종료(랩스).
            # 다음 -8% 재하락이 새 국면으로 잡히도록 리셋.
            in_correction, ftd_status = False, "expired"
    last = len(closes) - 1
    day_change_pct = round((closes[last] / closes[last - 1] - 1) * 100, 2) if last >= 1 else None
    vol_missing = not any(vols[-30:])
    opens = [b.get("o") for b in bars]
    gap_pct = gap_fade_pct = gap_dir = None
    gap_reversal_bearish = gap_reversal_bullish = False
    if last >= 1 and opens[last] is not None and closes[last - 1]:
        gap_pct = round((opens[last] / closes[last - 1] - 1) * 100, 2)
        if abs(gap_pct) >= GAP_MIN_PCT:
            gap_span = opens[last] - closes[last - 1]
            if gap_span:
                gap_dir = "up" if gap_span > 0 else "down"
                # 갭이 열린 방향과 반대로 종가가 얼마나 되돌아왔는지(%) — 100%면 갭 전부 메움,
                # 음수면 갭 방향으로 더 밀고 나감(되돌림 없음). 부호가 갭 방향에 대해
                # 정규화돼 있어 크기만 나타내고 방향은 담지 않는다 → 아래에서 쪼갠다.
                gap_fade_pct = round((opens[last] - closes[last]) / gap_span * 100, 1)
                # ⚠️ 되돌림의 강세/약세 함의는 갭 방향에 따라 정반대다. 하나의 불리언으로
                # 합치면 안 된다 — 2026-07-22 SOX가 갭다운 -1.88%를 fade 123.4%로 완전히
                # 메우고 전일 종가 위에서 마감한 강세 반전이었는데, 방향 무관 플래그를
                # "갭 되돌림 = 매도 우위"로 읽어 약세로 보고한 실측 사고가 있었다.
                faded = gap_fade_pct >= 50
                gap_reversal_bearish = faded and gap_dir == "up"    # 강세 갭을 종가까지 반납
                gap_reversal_bullish = faded and gap_dir == "down"  # 약세 갭을 종가까지 회복
    return {
        "as_of": dates[last],
        "close": closes[last],
        "day_change_pct": day_change_pct,
        "in_correction": in_correction,
        "baseline_low": round(baseline_low, 2) if baseline_low else None,
        "baseline_date": baseline_date,
        "rebound_days": rebound_days if in_correction else None,
        "ftd_status": ftd_status,
        "ftd_date": ftd_date,
        "ftd_vol_ok": ftd_vol_ok,
        "new_low_today": baseline_date == dates[last] if in_correction else False,
        "ftd_confirmed_today": ftd_date == dates[last] if ftd_date else False,
        "gap_pct": gap_pct,
        "gap_dir": gap_dir,
        "gap_fade_pct": gap_fade_pct,
        "gap_reversal_bearish": gap_reversal_bearish,
        "gap_reversal_bullish": gap_reversal_bullish,
        "note": "거래량 데이터 없음(최근 30봉 전부 0) — 가격 조건만으로 판정" if vol_missing else None,
    }


def cmd_regime():
    out = []
    for label, cfg in INDEXES.items():
        bars = load_or_fetch_index(label)
        if not bars:
            out.append({"label": label, "ticker": cfg["ticker"], "error": "데이터 조회 실패"})
            continue
        r = compute_regime(bars)
        out.append({"label": label, "ticker": cfg["ticker"], **r})
    print(json.dumps(out, ensure_ascii=False, indent=1))


def cmd_check(ticker):
    for sym, market in resolve_ticker(ticker):
        bars = load_or_fetch(market, sym)
        if not bars:
            continue
        rs_map_p = OUT / f"rs_{market}.json"
        rs_pct = None
        rs_note = "RS 백분위 없음 — screen 실행 이력 필요 (raw만 표기)"
        if rs_map_p.exists():
            m = json.loads(rs_map_p.read_text())
            rs_pct = m.get(sym)
            age_d = (time.time() - rs_map_p.stat().st_mtime) / 86400
            rs_note = f"RS 분포 기준일: {age_d:.0f}일 전 screen" + (" ⚠️ 오래됨" if age_d > 7 else "")
            if rs_pct is None:
                # 분포엔 있는데 이 종목이 모수 밖(유동성 미달 등) — raw로 근사 백분위
                raw = rs_raw([b["c"] for b in bars])
                if raw is not None:
                    dist = sorted(m.values())  # 백분위 분포는 균등이라 근사 불가 → raw만
                rs_note += " | 이 종목은 모수 밖(유동성/데이터) — raw만"
        crit, vals = trend_template(bars, rs_pct)
        n_pass = sum(1 for v in crit.values() if v)
        n_known = sum(1 for v in crit.values() if v is not None)
        out = {
            "ticker": sym, "as_of": bars[-1]["d"], "bars": len(bars),
            "criteria": crit, "values": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in vals.items()},
            "dvol_50d": round(dollar_vol_50d(bars)),
            "n_pass": n_pass, "n_known": n_known, "rs_note": rs_note,
            "verdict_hint": "PASS(8/8)" if n_pass == 8 else f"{n_pass}/{n_known} 통과",
        }
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    sys.exit(f"데이터 조회 실패: {ticker}")


# ---------------- vcp ----------------

def zigzag(bars, threshold=0.04):
    """종가 기준 스윙 포인트. (index, price, 'H'|'L') 목록."""
    closes = [b["c"] for b in bars]
    piv = []
    last_ext_i, last_ext_p = 0, closes[0]
    direction = 0  # 1 up, -1 down
    for i, c in enumerate(closes):
        if direction >= 0 and c > last_ext_p:
            last_ext_i, last_ext_p = i, c
            direction = 1
        elif direction <= 0 and c < last_ext_p:
            last_ext_i, last_ext_p = i, c
            direction = -1
        if direction == 1 and c < last_ext_p * (1 - threshold):
            piv.append((last_ext_i, last_ext_p, "H"))
            last_ext_i, last_ext_p, direction = i, c, -1
        elif direction == -1 and c > last_ext_p * (1 + threshold):
            piv.append((last_ext_i, last_ext_p, "L"))
            last_ext_i, last_ext_p, direction = i, c, 1
    piv.append((last_ext_i, last_ext_p, "H" if direction == 1 else "L"))
    return piv


def weekly_bars(bars, base_start):
    """base_start(베이스 고점일) '당일부터' 주봉 집계 — 첫 주는 부분주가 된다.

    ⚠️ 주 전체를 넣으면 베이스 고점 *이전* 저점이 첫 수축으로 잡힌다 (안국약품
    실측: base_high 05-27 12,730 vs 05-26 저가 10,000 → 시간순 역행 레그 -21.4%
    가 생성됐다). 베이스는 고점에서 시작하므로 그 이전 봉은 구조에서 제외한다.
    """
    wk = {}
    for b in bars[base_start:]:
        d = datetime.strptime(b["d"], "%Y-%m-%d").date()
        k = d - timedelta(days=d.weekday())
        w = wk.setdefault(k, {"h": -1e18, "l": 1e18, "c": None, "v": 0, "n": 0})
        w["h"] = max(w["h"], b["h"]); w["l"] = min(w["l"], b["l"])
        w["c"] = b["c"]; w["v"] += b["v"]; w["n"] += 1
    return [dict(d=str(k), **v) for k, v in sorted(wk.items())]


def weekly_legs(wb, base_high, threshold=0.04):
    """주봉 고/저 스윙 수축 레그. 첫 피벗을 base_high로 고정한다.

    ⚠️ 주봉 '종가'에 일봉용 zigzag를 돌리면 안 된다 — 주내 고저가 뭉개져 초기
    급락 레그가 통째로 사라진다 (DELL 실측: 종가법 [-3.7,-8.9] vs 고저법
    [-23.9,-14.7,-17.8]). 문턱 4%는 0군·1군 10종 + 고점근접 20종 스윕에서
    2.5~4.0% 전이 0의 안정 구간 상단 (4.5%↑은 5.7% 수준 실제 레그를 파괴).
    """
    if not wb:
        return []
    piv = []
    ext_i, ext_p, direction = 0, base_high, 1
    for i, b in enumerate(wb):
        if direction == 1:
            if b["h"] > ext_p:
                ext_i, ext_p = i, b["h"]
            elif b["l"] < ext_p * (1 - threshold):
                piv.append((ext_i, ext_p, "H"))
                ext_i, ext_p, direction = i, b["l"], -1
        else:
            if b["l"] < ext_p:
                ext_i, ext_p = i, b["l"]
            elif b["h"] > ext_p * (1 + threshold):
                piv.append((ext_i, ext_p, "L"))
                ext_i, ext_p, direction = i, b["h"], 1
    piv.append((ext_i, ext_p, "H" if direction == 1 else "L"))
    legs = []
    for i in range(len(piv) - 1):
        a, b2 = piv[i], piv[i + 1]
        if a[2] == "H" and b2[2] == "L":
            legs.append({"from": wb[a[0]]["d"], "high": round(a[1], 2),
                         "to": wb[b2[0]]["d"], "low": round(b2[1], 2),
                         "pct": round((b2[1] / a[1] - 1) * 100, 1),
                         # 같은 주봉 안에서 끝난 레그 = 주내 고저 순서 미확정.
                         # 크기를 과대평가할 수 있으니 판정 시 주봉 차트로 확인.
                         "same_week": a[0] == b2[0]})
    return legs


def alt_setups(bars, closes, highs, lows, vols, v50):
    """연장 리더용 대체 셋업 2종 — 수치만 (판정은 references §7).

    2026-09-22 도입. 신고가를 계속 찍는 주도주는 weeks_elapsed가 반복 리셋돼 VCP 조건이
    영영 안 켜진다(VLO·DINO 4회+ 리셋 실측). 원전이 연장 리더에 따로 두는 두 자리를 계산한다:
      A. 50일선(10주선) 되돌림 — 달린 뒤 첫·둘째 저거래량 눌림이 상승 중인 50MA에 닿는 자리
      B. 3주 타이트 — 주간 종가 3주+가 서로 1~1.5% 이내로 붙는 자리 (피벗 = 구간 고가)
    """
    n = len(bars)
    c = closes[-1]
    out = {}
    # ---------- A. 50MA 되돌림 ----------
    s50 = sma(closes, 50)
    if s50:
        s50_p10 = sma(closes, 50, at=n - 11) if n >= 61 else None
        s50_p20 = sma(closes, 50, at=n - 21) if n >= 71 else None
        pct_vs = (c / s50 - 1) * 100
        rising = bool(s50_p10 and s50 > s50_p10)
        low5 = min(lows[-5:])
        low5_vs = (low5 / s50 - 1) * 100
        # 되돌림 전에 "달렸는가": 최근 60봉 종가/50MA 최대 이격
        ext = []
        for i in range(max(49, n - 60), n):
            si = sma(closes, 50, at=i)
            if si:
                ext.append(closes[i] / si - 1)
        max_ext = max(ext) * 100 if ext else None
        # 터치 에피소드(최근 120봉): 저가가 50MA+2% 이내로 들어오면 시작, 종가가 50MA+4% 위로 나가면 종료.
        # 1~2번째 터치가 원전상 매수 가능, 3번째+는 늦은 자리.
        episodes, ep60, in_ep, last_touch = 0, 0, False, None
        for i in range(max(49, n - 120), n):
            si = sma(closes, 50, at=i)
            if not si:
                continue
            if not in_ep and lows[i] <= si * 1.02:
                in_ep, episodes, last_touch = True, episodes + 1, i
                if i >= n - 60:
                    ep60 += 1
            elif in_ep:
                if lows[i] <= si * 1.02:
                    last_touch = i
                if closes[i] > si * 1.04:
                    in_ep = False
        closed_below5 = sum(1 for i in range(n - 5, n)
                            if closes[i] < (sma(closes, 50, at=i) or 0))
        dnv = [vols[i] for i in range(n - 10, n) if closes[i] < closes[i - 1]]
        pb_vol = round(sum(dnv) / len(dnv) / v50, 2) if dnv and v50 else None
        bounce = bool(c > closes[-2] and c > s50 and low5_vs <= 3.0)
        if c < s50:
            status = "below_50ma"
        elif pct_vs <= 3.0 and rising:
            status = "at_50ma"
        elif pct_vs <= 8.0:
            status = "approaching"
        else:
            status = "not_pulled_back"
        out["pullback_50ma"] = {
            "status": status, "sma50": round(s50, 2), "pct_vs_sma50": round(pct_vs, 1),
            "sma50_rising": rising,
            "sma50_slope_20d_pct": round((s50 / s50_p20 - 1) * 100, 1) if s50_p20 else None,
            "low5_vs_sma50_pct": round(low5_vs, 1),
            "max_ext_above_sma50_60d_pct": round(max_ext, 1) if max_ext is not None else None,
            "had_run_60d": bool(max_ext is not None and max_ext >= 10),
            "touch_episodes_120d": episodes,
            "touch_episodes_60d": ep60,   # 판정은 이 값 — 완만한 추세는 120봉에 5~10회 닿는다(VLO 6·GS 10 실측)
            "sessions_since_last_touch": (n - 1 - last_touch) if last_touch is not None else None,
            "closed_below_sma50_last5": closed_below5,
            "pullback_downday_vol_over_v50": pb_vol,
            "bounce_hint": bounce,
            "stop_ref": round(s50, 2),
        }
    # ---------- B. 3주 타이트 ----------
    wk = {}
    for b in bars[-100:]:
        d = datetime.strptime(b["d"], "%Y-%m-%d").date()
        k = d - timedelta(days=d.weekday())
        w = wk.setdefault(k, {"h": -1e18, "l": 1e18, "c": None, "v": 0, "n": 0, "last": None})
        w["h"] = max(w["h"], b["h"]); w["l"] = min(w["l"], b["l"])
        w["c"] = b["c"]; w["v"] += b["v"]; w["n"] += 1; w["last"] = d
    wl = [dict(d=str(k), **v) for k, v in sorted(wk.items())]
    partial = bool(wl and wl[-1]["last"].weekday() != 4)   # 금요일 봉이 없으면 미확정 주
    comp = wl[:-1] if partial else wl
    tb = {"includes_partial_week": partial, "weeks_evaluated": len(comp)}
    if len(comp) >= 3:
        cw = [w["c"] for w in comp]
        tight = 1
        for i in range(len(cw) - 1, 0, -1):
            if abs(cw[i] / cw[i - 1] - 1) * 100 <= 1.5:
                tight += 1
            else:
                break
        win = comp[-tight:]
        wc = [w["c"] for w in win]
        spread = (max(wc) - min(wc)) / min(wc) * 100
        pivot = max(w["h"] for w in win)
        pre = comp[:-tight]
        prior_low = min(w["l"] for w in pre[-12:]) if pre else None
        adv = (win[0]["c"] / prior_low - 1) * 100 if prior_low else None
        pre10 = pre[-10:]
        avg10 = sum(w["v"] for w in pre10) / len(pre10) if len(pre10) >= 4 else None
        if tight >= 3 and spread <= 3.0 and adv is not None and adv >= 20:
            status = "valid"
        elif tight >= 3 and spread <= 3.0:
            status = "tight_no_run"     # 붙어 있긴 한데 앞에 상승이 없다 — 저변동 종목의 평상시
        elif tight == 2:
            status = "forming"
        else:
            status = "none"
        tb.update({
            "status": status, "n_tight_weeks": tight if tight >= 2 else 0,
            "weekly_closes": [round(x, 2) for x in wc], "spread_pct": round(spread, 1),
            "week_to_week_max_pct": round(max(abs(wc[i] / wc[i - 1] - 1) * 100
                                             for i in range(1, len(wc))), 1) if len(wc) > 1 else None,
            "pivot": round(pivot, 2), "pct_to_pivot": round((pivot / c - 1) * 100, 1),
            "prior_advance_pct": round(adv, 1) if adv is not None else None,
            "vol_last_tight_wk_over_10w": round(win[-1]["v"] / avg10, 2) if avg10 else None,
            "window_start": win[0]["d"],
        })
    else:
        tb["status"] = "insufficient_history"
    out["tight_3w"] = tb
    out["note"] = ("연장 리더 전용 대체 셋업. pullback_50ma: at_50ma + sma50_rising + had_run_60d + "
                   "touch_episodes_60d≤2 + 하락일 거래량<1.0 + closed_below 0이면 후보(판정은 §7). "
                   "tight_3w: valid(3주+ 종가 스프레드≤3%·주간 ≤1.5%·직전 상승≥20%)면 피벗=구간 고가, 금요일 확정봉 기준.")
    return out


def cmd_vcp(ticker):
    for sym, market in resolve_ticker(ticker):
        bars = load_or_fetch(market, sym)
        if not bars:
            continue
        closes = [b["c"] for b in bars]
        highs = [b["h"] for b in bars]
        lows = [b["l"] for b in bars]
        c = closes[-1]
        # 베이스 시작 = 최근 252봉 내 최고 '장중 고가' (종가 기준이면 갭 고점을 3%씩 놓친다)
        win = min(len(highs), 252)
        base_start = len(highs) - win + max(range(win), key=lambda i: highs[len(highs) - win + i])
        base_high = highs[base_start]
        weeks_in_base = round((len(closes) - 1 - base_start) / 5, 1)
        base_low = min(lows[base_start:]) if base_start < len(closes) - 1 else c
        base_depth = (base_low / base_high - 1) * 100
        # 베이스 구간 수축 시퀀스 (스윙은 종가 zigzag — 잡음에 강함)
        piv = zigzag(bars[max(0, base_start - 1):], threshold=0.035)
        contractions = []
        for i in range(len(piv) - 1):
            a, b2 = piv[i], piv[i + 1]
            if a[2] == "H" and b2[2] == "L":
                contractions.append(round((b2[1] / a[1] - 1) * 100, 1))
        vols = [b["v"] for b in bars]
        v50 = sum(vols[-50:]) / 50 if len(vols) >= 50 else sum(vols) / len(vols)
        v5, v10 = sum(vols[-5:]) / 5, sum(vols[-10:]) / 10
        rng10 = (max(highs[-10:]) - min(lows[-10:])) / c * 100
        # ATR%(14) 현재 vs 6주 전 — 변동성 수축/확장 방향
        def atr_pct(end_idx, n=14):
            if end_idx < n + 1:
                return None
            trs = []
            for i in range(end_idx - n + 1, end_idx + 1):
                pc = closes[i - 1]
                trs.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
            return sum(trs) / n / closes[end_idx] * 100
        atr_now = atr_pct(len(bars) - 1)
        atr_6w = atr_pct(len(bars) - 1 - 30)
        # 최근 20봉: 하락일 vs 상승일 거래량 (분산/매집 힌트)
        dn = sum(b["v"] for i, b in enumerate(bars[-20:], len(bars) - 20) if closes[i] < closes[i - 1])
        up = sum(b["v"] for i, b in enumerate(bars[-20:], len(bars) - 20) if closes[i] > closes[i - 1])
        # 피벗 후보 = 마지막 종가 스윙 하이 주변 ±2봉의 장중 고가
        base_off = max(0, base_start - 1)
        last_h = next((p for p in reversed(piv) if p[2] == "H"), None)
        if last_h:
            hi_i = base_off + last_h[0]
            pivot = max(highs[max(0, hi_i - 2): hi_i + 3])
        else:
            pivot = base_high
        # 주봉 구조 (승격 판정용 — 일봉 수축 시퀀스는 주내 잡음에 취약하다)
        wb = weekly_bars(bars, base_start)
        wlegs = weekly_legs(wb, base_high)
        # ⚠️ weeks(버킷 수)로 3주 게이트를 걸면 안 된다 — 베이스가 주중에 시작하면
        # 부분주가 한 주로 계상돼 조기 통과한다 (RVMD 실측: 버킷 3개 = 실경과 2.2주).
        # 게이트는 일봉 경과주수(weeks_in_base)로 건다.
        wk_block = {"weeks": len(wb), "weeks_elapsed": weeks_in_base,
                    "enough_history": weeks_in_base >= 3.0,
                    "last_week_sessions": wb[-1]["n"] if wb else 0,
                    "legs_pct_seq": [l["pct"] for l in wlegs], "n_legs": len(wlegs),
                    "legs": wlegs}
        if wb:
            lw = wb[-1]
            wk_block["range_last_wk_pct"] = round((lw["h"] - lw["l"]) / lw["c"] * 100, 1)
            w3 = wb[-3:]
            wk_block["range_last_3w_pct"] = round(
                (max(x["h"] for x in w3) - min(x["l"] for x in w3)) / lw["c"] * 100, 1)
            # 베이스가 어리면 분모에 부분주(base_start 주)만 남아 비율이 왜곡된다
            # (RVMD 실측: 버킷 3개일 때 1.43 — 2세션짜리 첫 주가 분모). 4주 미만이면 낸다.
            w10 = wb[-11:-1] if len(wb) >= 11 else wb[:-1]
            avg10 = (sum(x["v"] for x in w10) / len(w10)) if len(w10) >= 4 else 0
            wk_block["vol_last_wk_over_10w"] = round(lw["v"] / avg10, 2) if avg10 else None
        wk_block["note"] = ("주 1회(금 마감) 갱신되는 구조 지표. 3주 미만이면 §6상 베이스 아님 — 판정 보류."
                            if weeks_in_base < 3.0 else "수축 점감·재확대 판정은 이 legs_pct_seq로 한다.")
        out = {
            "ticker": sym, "as_of": bars[-1]["d"], "close": round(c, 2),
            "base": {"weeks_in_base": weeks_in_base, "base_high": round(base_high, 2),
                     "base_depth_pct": round(base_depth, 1),
                     "pct_below_base_high": round((c / base_high - 1) * 100, 1)},
            "contractions_pct_seq": contractions,
            "n_contractions": len(contractions),
            "tightness_range_10d_pct": round(rng10, 1),
            "volatility": {"atr14_pct_now": round(atr_now, 2) if atr_now else None,
                           "atr14_pct_6w_ago": round(atr_6w, 2) if atr_6w else None,
                           "contracting": bool(atr_now and atr_6w and atr_now < atr_6w)},
            "volume": {"v5_over_v50": round(v5 / v50, 2) if v50 else None,
                       "v10_over_v50": round(v10 / v50, 2) if v50 else None,
                       "dryup_hint": bool(v50 and v5 / v50 < 0.7),
                       "down_over_up_vol_20d": round(dn / up, 2) if up else None,
                       "distribution_hint": bool(up and dn / up > 1.3)},
            "pivot_candidate": round(pivot, 2),
            "pct_to_pivot": round((pivot / c - 1) * 100, 1),
            "weekly": wk_block,
            "alt_setups": alt_setups(bars, closes, highs, lows, vols, v50),
            "note": "수치만 제공 — VCP 유효성(수축 점감·고갈·피벗 근접)은 스킬 기준으로 판정. "
                    "수축 시퀀스 판정은 weekly.legs_pct_seq 우선, 일봉 contractions_pct_seq는 참고용.",
        }
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    sys.exit(f"데이터 조회 실패: {ticker}")


# ---------------- daily ----------------

def pool_trend(hist, cur):
    """모수(유동성 하한 통과 종목 수) 추이 — 2026-07-27 도입.

    모수는 오래 분모로만 쓰였지만 그 자체가 지표다: 거래대금 하한을 넘지 못해 스크리닝
    자격을 잃는 종목이 늘면 시장 참여가 마르고 있다는 뜻이다. 다만 **하루치 급변과 주 단위
    완만한 축소는 원인이 정반대**라 반드시 갈라 읽어야 한다 —
      · 하루 만에 5%+ → 수집 사고를 먼저 의심한다 (2026-07-27 실측: KR 1,090→957이
        전량 fetch 실패였고, 데일리가 이를 '브레드스 개선'으로 오독할 뻔했다)
      · 20세션에 걸쳐 5%+ → 시장 신호 (같은 기간 KR -16.1% vs US -0.4%로 갈렸고
        소급 재계산으로도 살아남았다)
    두 시장 값을 나란히 보면 수집 문제(양쪽 동시)와 시장 문제(한쪽만)가 구분된다.
    """
    pools = [(r["as_of"], r["pool"]) for r in hist if r.get("pool")]
    if not pools:
        return None
    as_of, today = pools[-1]

    def back(n):
        return pools[-1 - n][1] if len(pools) > n else None

    def pct(old):
        return round((today / old - 1) * 100, 1) if old else None

    p1, p5, p20 = back(1), back(5), back(20)
    v1, v5, v20 = pct(p1), pct(p5), pct(p20)
    stale = cur.get("stale_excluded")
    if v1 is not None and abs(v1) >= 5:
        flag, note = "수집의심", ("하루 만에 모수가 5%+ 변했다 — 시장보다 수집을 먼저 의심한다. "
                                "stale_excluded와 fetch_{market}.log의 실패 수를 확인하고, "
                                "확인 전까지 브레드스 %를 전일과 직접 비교하지 않는다")
    elif v20 is not None and v20 <= -5:
        flag, note = "축소", "20세션에 걸친 완만한 축소 — 거래대금이 말라 자격을 잃는 종목이 늘고 있다(참여 위축)"
    elif v20 is not None and v20 >= 5:
        flag, note = "확대", "20세션에 걸친 확대 — 유동성 유입"
    else:
        flag, note = "안정", "모수 변화가 작아 브레드스 % 추이를 그대로 읽어도 된다"
    return {"as_of": as_of, "pool": today, "sessions": len(pools),
            "pool_1d": p1, "pool_5d": p5, "pool_20d": p20,
            "vs_1d_pct": v1, "vs_5d_pct": v5, "vs_20d_pct": v20,
            "stale_excluded": stale, "flag": flag, "note": note}


def cmd_daily(market):
    """오늘 vs 직전 스크리닝 diff + 브레드스 추이 — 데일리 리포트의 원재료(JSON)."""
    snaps = sorted(OUT.glob(f"screen_{market}_*.json"))
    if not snaps:
        sys.exit(f"screen 산출물 없음 — `sepa.py screen {market}` 먼저")
    cur = json.loads(snaps[-1].read_text())
    prev = json.loads(snaps[-2].read_text()) if len(snaps) > 1 else None
    cur_set = {r["ticker"] for r in cur["pass_all"]}
    prev_set = {r["ticker"] for r in prev["pass_all"]} if prev else set()
    names = dict(load_universe(market))
    new_in = sorted(cur_set - prev_set)
    dropped = sorted(prev_set - cur_set)
    hist_p = OUT / f"breadth_{market}.jsonl"
    hist = [json.loads(l) for l in hist_p.read_text().splitlines() if l.strip()] if hist_p.exists() else []
    top = [{k: r[k] for k in ("ticker", "name", "rs", "from_52w_high", "vs_52w_low", "dvol_50d")}
           for r in cur["pass_all"][:20]]
    # --- Stage 2 전이 감지 (2026-09-22 추가) ---
    # "전환 직전"(미달이 기준 2/3/4 MA 정렬뿐)이던 종목이 오늘 8/8로 넘어온 것 = Stage 2 첫날.
    # 그룹 로테이션(groups)은 주 1회라 이 전이를 최대 1주 늦게 봤다 — 평일 데일리가 diff만 찍는다(판정 없음).
    prev_trans = _transition_map(Path(str(snaps[-2]).replace(".json", ".csv"))) if prev else {}
    cur_trans = _transition_map(Path(str(snaps[-1]).replace(".json", ".csv")))
    # CSV가 JSON 모수와 어긋나면(결측·부분 파일) 전이 0건은 "없음"이 아니라 "판정 불가"다.
    def _csv_ok(js, snap):
        cp = Path(str(snap).replace(".json", ".csv"))
        if not cp.exists():
            return False, f"{cp.name} 없음"
        n = sum(1 for _ in open(cp)) - 1
        wd = js.get("with_data") or 0
        if wd and n < wd * 0.95:
            return False, f"{cp.name} {n}행 < 모수 {wd} (부분 파일)"
        return True, ""
    x_ok = []
    for js, sn in ((prev, snaps[-2]) if prev else (None, None), (cur, snaps[-1])):
        if js is not None:
            x_ok.append(_csv_ok(js, sn))
    x_reliable = all(ok for ok, _ in x_ok)
    x_reason = "; ".join(r for ok, r in x_ok if not ok)
    cur_rows = {r["ticker"]: r for r in cur["pass_all"]}
    crossovers = []
    for t in sorted(cur_set & set(prev_trans)):
        r = cur_rows[t]
        crossovers.append({"ticker": t, "name": r.get("name", ""), "rs": r.get("rs"),
                           "from_52w_high": r.get("from_52w_high"),
                           "sma200_up_months": r.get("sma200_up_months"),
                           "prev_fails": prev_trans[t]})
    crossovers.sort(key=lambda x: -(x["rs"] or 0))
    pending = sorted(cur_trans.items(), key=lambda kv: -kv[1]["rs"])
    out = {
        "market": market, "date": cur["date"], "as_of": cur.get("breadth", {}).get("as_of"),
        "prev_date": prev["date"] if prev else None,
        "breadth_today": cur.get("breadth"),
        "breadth_prev": prev.get("breadth") if prev else None,
        "pool_trend": pool_trend(hist, cur),
        "breadth_history_tail": hist[-15:],
        "pass_all_count": cur["pass_all_count"],
        "pass_all_delta": cur["pass_all_count"] - prev["pass_all_count"] if prev else None,
        # 절삭 제거 (2026-08-15). 구 [:30]은 실제 diff가 30건을 넘을 때 조용히 잘려
        # 리포트가 편입·이탈 규모를 과소보고했다 (watch_us.md 2026-07-29 기록).
        "new_entrants": [{"ticker": t, "name": names.get(t, "")} for t in new_in],
        "dropped": [{"ticker": t, "name": names.get(t, "")} for t in dropped],
        "new_entrants_count": len(new_in),
        "dropped_count": len(dropped),
        "top20_by_rs": top,
        "new_high_pass_count": cur.get("new_high_pass_count"),
        "new_high_pass": cur.get("new_high_pass", []),
        # 전환 직전 → 통과 전이 (직전 세션에 MA 정렬만 남았던 종목이 오늘 8/8)
        "stage2_crossovers": crossovers,
        "stage2_crossovers_count": len(crossovers),
        "stage2_crossovers_reliable": x_reliable,
        "stage2_crossovers_note": x_reason or None,   # False면 0건을 "없음"으로 쓰지 말 것
        # 오늘 기준 전환 직전 명단 (내일 넘어올 후보 — RS 90+만, 최대 15)
        "transition_pending_count": len(cur_trans),
        "transition_pending_rs90": [{"ticker": t, "name": v["name"], "rs": v["rs"],
                                     "from_52w_high": v["from_52w_high"], "fails": v["fails"]}
                                    for t, v in pending if v["rs"] >= 90][:15],
        "note": "장세 판정·후보 선별은 스킬이 한다 (references §10). 신규 진입은 vcp로 셋업 확인. "
                "new_high_pass는 신고가∩템플릿8/8∩RS컷 — 주말 구조 패스의 발굴 절 재료다. "
                "stage2_crossovers는 '전환 직전(미달 2/3/4뿐)→8/8' 전이 = Stage 2 첫날 후보, 베이스 0주라 관찰 명단 재료다.",
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


# ---------------- backfill ----------------

def session_calendar(market, days):
    """이 시장의 최근 `days` 세션일 목록. 지수 봉을 거래일 달력으로 쓴다
    (개별 종목은 상장·거래정지로 구멍이 나지만 지수는 안 난다)."""
    label = "kospi" if market == "kr" else "nasdaq"
    p = data_path("index", INDEXES[label]["ticker"])
    if not p.exists():
        sys.exit(f"지수 데이터 없음({p}) — `sepa.py regime` 먼저 실행")
    bars = json.loads(p.read_text()).get("bars") or []
    return [b["d"] for b in bars][-days:]


def cmd_backfill(market, days=20, force=False):
    """브레드스 이력을 과거 세션까지 소급 계산해 채운다.
    §10 추이 판정은 15세션 창을 전제하는데, 수집기가 놓친 날은 영영 구멍으로 남는다 —
    저장된 2년 일봉이 있으므로 그 시점 스크리닝은 결정론적으로 재현 가능하다."""
    hist_p = OUT / f"breadth_{market}.jsonl"
    have = set()
    if hist_p.exists() and not force:
        have = {json.loads(l).get("as_of")
                for l in hist_p.read_text().splitlines() if l.strip()}
    cal = session_calendar(market, days)
    todo = [d for d in cal if d not in have]
    print(f"[{market}] 달력 {len(cal)}세션 / 보유 {len(cal)-len(todo)} / 계산 대상 {len(todo)}")
    if not todo:
        print("채울 구멍 없음")
        return
    for i, d in enumerate(todo, 1):
        print(f"  ({i}/{len(todo)}) {d} 계산 중…")
        try:
            cmd_screen(market, as_of=d)
        except SystemExit as e:
            print(f"  ⚠️ {d} 스킵: {e}")
    rows = [json.loads(l) for l in hist_p.read_text().splitlines() if l.strip()]
    print(f"완료 — 브레드스 이력 {len(rows)}세션 "
          f"({rows[0]['as_of']} ~ {rows[-1]['as_of']})")


# ---------------- passlist ----------------

def cmd_passlist(market, top=None):
    """최신 스크리닝의 통과 종목을 마크다운 표로 — 데일리 리포트 부록용 (그대로 붙여넣기)."""
    snaps = sorted(OUT.glob(f"screen_{market}_*.json"))
    if not snaps:
        sys.exit(f"screen 산출물 없음 — `sepa.py screen {market}` 먼저")
    d = json.loads(snaps[-1].read_text())
    rows = d["pass_all"]
    total = d["pass_all_count"]
    # 시가총액 내림차순 (시총 미확보 종목은 맨 뒤, RS순)
    rows = sorted(rows, key=lambda r: (-(r.get("mcap") or 0), -r["rs"]))
    # 기본: 60개 이하면 전체, 넘으면 시총 상위 30 (US처럼 수백 개일 때 표 폭주 방지)
    n = top if top else (len(rows) if total <= 60 else 30)
    rows = rows[:n]
    as_of = d.get("breadth", {}).get("as_of", d["date"])
    flag = "🇰🇷" if market == "kr" else "🇺🇸"
    print(f"#### {flag} 템플릿 통과 {total}종목" + (f" 중 시총 상위 {len(rows)}" if len(rows) < total else " 전체")
          + f" — 시총순 (as-of {as_of})")
    print()
    print("| 티커 | 종목명 | 시총 | 종가 | RS | 저점比 | 고점比 | 200MA↑ | 거래대금(50d) |")
    print("|---|---|--:|--:|--:|--:|--:|:-:|--:|")
    for r in rows:
        mc = r.get("mcap")
        if market == "kr":
            price = f"{r['close']:,.0f}"
            dv = f"{r['dvol_50d'] / 1e8:,.0f}억"
            mcs = "-" if not mc else (f"{mc / 1e12:,.1f}조" if mc >= 1e12 else f"{mc / 1e8:,.0f}억")
        else:
            price = f"${r['close']:,.2f}"
            dv = f"${r['dvol_50d'] / 1e9:.1f}B" if r["dvol_50d"] >= 1e9 else f"${r['dvol_50d'] / 1e6:,.0f}M"
            mcs = "-" if not mc else (f"${mc / 1e12:.2f}T" if mc >= 1e12 else
                                      f"${mc / 1e9:.1f}B" if mc >= 1e9 else f"${mc / 1e6:,.0f}M")
        name = r["name"][:24]
        print(f"| {r['ticker']} | {name} | {mcs} | {price} | {r['rs']} | +{r['vs_52w_low']:.0f}% | "
              f"{r['from_52w_high']}% | {r['sma200_up_months']} | {dv} |")
    print()
    csv_p = str(snaps[-1]).replace(".json", ".csv")
    if len(rows) < total:
        print(f"전체 {total}종목(8기준 개별 판정 포함): `{csv_p}`")
    else:
        print(f"8기준 개별 판정 포함 전체 데이터: `{csv_p}`")


# ---------------- groups (그룹 로테이션 — 주말 구조 패스용) ----------------

# 이름 기반 테마 분류 규칙 (미국 전용, 공식 GICS 아님 — groups_us.json의 themes가 우선)
US_THEME_RULES = [
    ("바이오텍(신약)", r"Therapeutic|Pharmaceutic|Biosciences|Biopharma|Oncolog|Medicines|Immuno|Bio, Inc|Biotech|Pharma|Sciences Inc|Neuroscience"),
    ("진단·생명과학툴", r"Genomic|Diagnostic|Laborator|Life Science|Molecular|Bioscience"),
    ("의료기기·병원·헬스서비스", r"Health|Medical|Surgical|Care|Hospital|Dental|Ortho|Vascular|Cardio"),
    ("은행·저축은행", r"Bancorp|Bancshares|\bBank|Savings|Banco|Bankshares"),
    ("보험", r"Insurance|Assurance|Insur|Reinsurance|Underwrit"),
    ("자산운용·증권·핀테크", r"Capital|Asset Manage|Securities|Financial|Payments|Payment|Exchange|Brokerage"),
    ("REIT·부동산", r"REIT|Properties|Realty|Real Estate|Trust Common|Hotel|Resorts|Lodging"),
    ("에너지·정유", r"Energy|Petroleum|\bOil|\bGas|Drilling|Midstream|Refin|Pipeline"),
    ("해운·탱커", r"Tanker|Shipping|Maritime|Seaways|Bulk Carrier|Ship Lease|LPG"),
    ("소재·화학·철강", r"Steel|Metals|Chemical|Materials|Mining|Gold|Copper|Corporation Common Stock Chemical"),
    ("반도체·전자부품", r"Semiconductor|Micro Devices|Electronics|Circuit|Photonic|Silicon"),
    ("소프트웨어·인터넷·미디어", r"Software|Cloud|Cyber|Digital|Network|Internet|Media|Communications|Interactive|Data\b"),
    ("소비재·유통·외식", r"Restaurant|Food|Beverage|Brands|Retail|Stores|Grill|Apparel|Footwear|Beauty|Cosmetic|Automotive|Motors"),
    ("산업재·운송·방산", r"Industrie|Industrial|Construction|Engineering|Building|Aero|Defense|Machinery|Manufactur|Equipment|Transport|Airlines|Logistics|Rail"),
]
TRANSITION_MA_FAILS = {"2_150_gt_200", "3_200ma_rising_1m", "4_50_gt_150_gt_200"}
CRIT_COLS = ["1_price_gt_150_200", "2_150_gt_200", "3_200ma_rising_1m", "4_50_gt_150_gt_200",
             "5_price_gt_50", "6_ge_30pct_above_52wlow", "7_within_25pct_of_52whigh", "8_rs_ge_70"]


def _truthy(v):
    return str(v).strip().lower() in ("true", "1", "yes")


def _transition_map(csv_p):
    """screen CSV → {ticker: {...}} of '전환 직전' 종목 (미통과 & 미달 기준이 2/3/4 MA 정렬뿐).

    cmd_groups §4와 같은 정의 — daily의 Stage 2 전이 감지(stage2_crossovers)가 공유한다.
    JSON의 near_miss_7of8은 50건 절삭이라 쓰지 않는다 — CSV가 전 종목 정본.
    """
    csv_p = Path(csv_p)
    if not csv_p.exists():
        return {}
    out = {}
    with open(csv_p, newline="") as f:
        for r in csv.DictReader(f):
            if _truthy(r["pass_all"]):
                continue
            fails = {c for c in CRIT_COLS if not _truthy(r[c])}
            if fails and fails <= TRANSITION_MA_FAILS:
                out[r["ticker"]] = {"name": r["name"], "rs": float(r["rs"] or 0),
                                    "from_52w_high": float(r["from_52w_high"] or 0),
                                    "fails": sorted(c.split("_")[0] for c in fails)}
    return out


def _fmt_mcap(mc, market):
    if not mc:
        return "-"
    if market == "kr":
        return f"{mc / 1e12:,.1f}조" if mc >= 1e12 else f"{mc / 1e8:,.0f}억"
    return (f"${mc / 1e12:.2f}T" if mc >= 1e12 else
            f"${mc / 1e9:.1f}B" if mc >= 1e9 else f"${mc / 1e6:,.0f}M")


def load_groups_map(market):
    """로스터·테마 시드 (사용자 편집 가능). 없으면 빈 구조."""
    p = WS / f"groups_{market}.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"rosters": {}, "themes": {}}


def cmd_groups(market):
    """그룹 로테이션 스냅샷 — 핵심 로스터 통과율 + 테마 분포 + Stage 1→2 전환 리스트.

    데이터는 최신 screen CSV(전 종목 8기준 판정 포함). 판정·비교(전주 대비 변화)는 스킬이 한다.
    """
    import statistics
    snaps = sorted(OUT.glob(f"screen_{market}_*.csv"))
    if not snaps:
        sys.exit(f"screen 산출물 없음 — `sepa.py screen {market}` 먼저")
    csv_p = snaps[-1]
    with open(csv_p, newline="") as f:
        rows = list(csv.DictReader(f))
    # as_of는 짝이 되는 JSON에서 (파일명 키 = 세션일)
    jp = Path(str(csv_p).replace(".csv", ".json"))
    as_of = json.loads(jp.read_text()).get("as_of", "?") if jp.exists() else "?"
    for r in rows:
        r["_rs"] = float(r["rs"] or 0)
        r["_mcap"] = float(r["mcap"] or 0)
        r["_hi"] = float(r["from_52w_high"] or 0)
        r["_pass"] = _truthy(r["pass_all"])
    by_ticker = {r["ticker"]: r for r in rows}
    passers = [r for r in rows if r["_pass"]]
    gm = load_groups_map(market)
    flag = "🇰🇷" if market == "kr" else "🇺🇸"
    print(f"## {flag} 그룹 로테이션 — {market.upper()} (as-of {as_of}, 모수 {len(rows)} / 통과 {len(passers)})")

    # --- 1. 핵심 그룹 로스터 통과율 ---
    print("\n### 핵심 그룹 로스터 (전원 통과 = 주도 신호, 이탈자는 기준 번호와 함께)")
    print("\n| 그룹 | 통과 | RS중앙(통과분) | 이탈/모수밖 |")
    print("|---|---|--:|---|")
    for gname, tickers in gm.get("rosters", {}).items():
        present = [(t, by_ticker[t]) for t in tickers if t in by_ticker]
        absent = [t for t in tickers if t not in by_ticker]
        ok = [(t, r) for t, r in present if r["_pass"]]
        fail = [(t, r) for t, r in present if not r["_pass"]]
        mark = "✅" if present and not fail else ("❌" if len(ok) == 0 else "◑")
        med = f"{statistics.median([r['_rs'] for _, r in ok]):.1f}" if ok else "-"
        detail = []
        for t, r in fail:
            miss = [c.split("_")[0] for c in CRIT_COLS if not _truthy(r[c])]
            detail.append(f"{t}({r['n_pass']}/8, 미달 {'·'.join(miss)})")
        detail += [f"{t}(모수밖)" for t in absent]
        print(f"| {gname} | {len(ok)}/{len(present)}{'+' + str(len(absent)) if absent else ''} {mark} | {med} | {', '.join(detail) or '—'} |")

    # --- 2. 테마 분포 (통과 종목 전체 — 시드 매핑 우선, 미국은 이름 정규식 폴백) ---
    theme_of = {}
    for th, ts in gm.get("themes", {}).items():
        for t in ts:
            theme_of[t] = th

    def classify(r):
        if r["ticker"] in theme_of:
            return theme_of[r["ticker"]]
        if market == "us":
            for th, pat in US_THEME_RULES:
                if re.search(pat, r["name"], re.I):
                    return th
        return "미분류"

    groups = {}
    for r in passers:
        groups.setdefault(classify(r), []).append(r)
    print(f"\n### 테마 분포 — 통과 {len(passers)}종 (이름 기반 분류, 공식 산업분류 아님)")
    print("\n| 테마 | n | RS중앙 | RS90+ | 고점-5%이내 | 중앙시총 |")
    print("|---|--:|--:|--:|--:|--:|")
    for th, v in sorted(groups.items(), key=lambda x: -len(x[1])):
        rs = [r["_rs"] for r in v]
        near = sum(1 for r in v if r["_hi"] > -5)
        mc = statistics.median([r["_mcap"] for r in v])
        print(f"| {th} | {len(v)} | {statistics.median(rs):.1f} | {sum(1 for x in rs if x >= 90)} "
              f"| {near} ({near / len(v) * 100:.0f}%) | {_fmt_mcap(mc, market)} |")
    if groups.get("미분류") and market == "us":
        top_un = sorted(groups["미분류"], key=lambda r: -r["_rs"])[:15]
        print(f"\n미분류 상위(RS순): {' '.join(r['ticker'] for r in top_un)}")

    # --- 3. Stage 1→2 갓 전환 (통과 + 200MA 상승 ≤2개월) ---
    fresh = sorted([r for r in passers if int(r["sma200_up_months"] or 0) <= 2], key=lambda r: -r["_rs"])
    print(f"\n### Stage 1→2 갓 전환 — 통과 + 200MA 상승 1~2개월 ({len(fresh)}종)")
    print("\n| 티커 | 종목명 | RS | 200MA↑ | 고점比 | 시총 |")
    print("|---|---|--:|:-:|--:|--:|")
    for r in fresh:
        print(f"| {r['ticker']} | {r['name'][:24]} | {r['_rs']:.1f} | {r['sma200_up_months']}m "
              f"| {r['_hi']:.1f}% | {_fmt_mcap(r['_mcap'], market)} |")

    # --- 4. 전환 직전 (MA 정렬 미완 — 가격>200MA 스택·RS≥70, 미달이 기준 2/3/4뿐) ---
    trans = []
    for r in rows:
        if r["_pass"]:
            continue
        fails = {c for c in CRIT_COLS if not _truthy(r[c])}
        if fails and fails <= TRANSITION_MA_FAILS:
            trans.append((r, sorted(c.split("_")[0] for c in fails)))
    trans.sort(key=lambda x: -x[0]["_rs"])
    print(f"\n### 전환 직전 — MA 정렬 미완, 나머지 전부 충족 ({len(trans)}종, 상위 40)")
    print("\n| 티커 | 종목명 | RS | 미달 | 고점比 | 시총 |")
    print("|---|---|--:|---|--:|--:|")
    for r, fails in trans[:40]:
        print(f"| {r['ticker']} | {r['name'][:24]} | {r['_rs']:.1f} | {'·'.join(fails)} "
              f"| {r['_hi']:.1f}% | {_fmt_mcap(r['_mcap'], market)} |")
    print(f"\n로스터·테마 시드 편집: `{WS / f'groups_{market}.json'}` / 원본: `{csv_p}`")


# ---------------- queue (소싱 큐 — 2026-09-22) ----------------

QUEUE_CUT = {"us": {"dvol": 20_000_000}, "kr": {"dvol": 10_000_000_000}}
QUEUE_TTL_SESSIONS = 10        # 마지막 관측 후 10세션(2주) 안 보이면 만료
QUEUE_MAX_AGE_SESSIONS = 15    # 처음 본 뒤 15세션(3주) 지나면 pending 강제 만료
QUEUE_CAP = 15                 # pending 상한 — 넘으면 모델이 reject/defer로 줄여야 한다


def load_registry(market):
    """등록 명부 — 0군/1군/2군/killed/hold 티커. 모델이 파이프라인 변경 때마다 갱신한다.
    watch 파일 grep은 로스터 언급·약어와 섞여 오제외를 냈다(2026-08-15 MPC·RHI 실측) — 명부는 별도 파일."""
    p = WS / f"registry_{market}.json"
    if not p.exists():
        return {}
    d = json.loads(p.read_text())
    out = {}
    for tier, lst in d.items():
        if tier.startswith("_"):
            continue
        for t in lst:
            out[t if isinstance(t, str) else t["ticker"]] = tier
    return out


def _earn_flags(market, ej):
    """usq/krq JSON → 기계 요약 (판정 아님). 최근 2분기 성장률·가속 여부·Code33 힌트."""
    if not ej or ej.get("error"):
        return {"error": (ej or {}).get("error", "no data")}
    if market == "us":
        if not ej.get("rev_q") and not ej.get("eps_q"):
            return {"error": "us-gaap XBRL 없음(20-F 외국계 가능) — 원문 수동"}
        rev = [q for q in ej.get("rev_q", []) if q.get("yoy") is not None]
        eps = [q for q in ej.get("eps_q", []) if q.get("yoy") is not None]
        r2 = [q["yoy"] for q in rev[-2:]]; e2 = [q["yoy"] for q in eps[-2:]]
        last_q = (rev or eps or [{}])[-1].get("end")
        eps_pos = bool(eps) and eps[-1].get("val", 0) > 0
    else:
        qs = [q for q in ej.get("quarters", [])]
        r2 = [q["rev_yoy"] for q in qs[-2:] if q.get("rev_yoy") is not None]
        e2 = [q["op_yoy"] for q in qs[-2:] if q.get("op_yoy") is not None]
        last_q = qs[-1]["q"] if qs else None
        eps_pos = bool(qs) and (qs[-1].get("op") or 0) > 0
    accel_e = len(e2) == 2 and e2[1] > e2[0]
    accel_r = len(r2) == 2 and r2[1] > r2[0]
    strong = bool(e2 and r2 and e2[-1] >= 25 and r2[-1] >= 25)
    return {"last_q": last_q, "rev_yoy_2q": r2, "eps_yoy_2q": e2,
            "profit_pos": eps_pos, "accel_eps": accel_e, "accel_rev": accel_r,
            "code33_hint": strong and (accel_e or accel_r),
            "pass_hint": bool(e2 and (e2[-1] >= 20 or accel_e) and eps_pos)}


def _earnings_fetch(market, tickers):
    """usq.py / krq.py 서브프로세스 — 토큰 0. 실패는 error로 남기고 계속."""
    import subprocess
    if not tickers:
        return {}
    script = SEPA_DIR / ("usq.py" if market == "us" else "krq.py")
    args = [t.split(".")[0] if market == "kr" else t for t in tickers]
    try:
        out = subprocess.run([sys.executable, str(script)] + args, capture_output=True,
                             text=True, timeout=600).stdout
    except Exception as e:
        return {t: {"error": str(e)[:80]} for t in tickers}
    res = {}
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        key = j.get("ticker") or j.get("code")
        res[key] = j
    got = {}
    for t in tickers:
        k = t.split(".")[0] if market == "kr" else t
        got[t] = res.get(k, {"error": "출력 없음"})
    return got


def cmd_queue(market, mark=None, no_earnings=False, sessions=5):
    """소싱 큐 — 주간 신고가∩8/8∩RS컷 + Stage 2 전이 합집합에 3단계 컷·등록 제외·만료·실적 트리아지를
    기계적으로 적용해 pending 표를 낸다. 모델은 표를 읽고 심층 1~2종을 고르며 나머지는 --mark로 처분한다.
    이월은 명시적 defer 아니면 자동 만료다 — 3주째 무언 이월(2026-09 실측)을 구조적으로 막는다."""
    qp = WS / f"sourcing_queue_{market}.json"
    q = json.loads(qp.read_text()) if qp.exists() else {"entries": {}, "log": []}
    ent = q["entries"]
    snaps = sorted(OUT.glob(f"screen_{market}_*.json"))
    if not snaps:
        sys.exit("screen 산출물 없음")
    cur = json.loads(snaps[-1].read_text())
    as_of = cur.get("as_of")
    # --mark 처리
    if mark:
        for m in mark:
            t, _, rest = m.partition("=")
            st, _, note = rest.partition(":")
            if t not in ent:
                print(f"⚠️ {t} 큐에 없음"); continue
            ent[t]["status"] = st; ent[t]["marked"] = as_of
            if note:
                ent[t]["note"] = note
            q["log"].append({"d": as_of, "t": t, "to": st, "note": note})
        qp.write_text(json.dumps(q, ensure_ascii=False, indent=1))
        print(f"마크 완료 → {qp}")
        return
    # 세션 순서 인덱스 (만료 계산용)
    sess_list = [json.loads(p.read_text()).get("as_of") for p in snaps]
    sess_idx = {d: i for i, d in enumerate(sess_list)}
    def age(d):
        return sess_idx.get(as_of, 0) - sess_idx.get(d, sess_idx.get(as_of, 0))
    cut = QUEUE_CUT[market]
    reg = load_registry(market)
    roster_of = {t: g for g, ts in load_groups_map(market).get("rosters", {}).items() for t in ts}
    cur_pass = {r["ticker"]: r for r in cur["pass_all"]}
    # --- 합집합 (최근 N세션): 신고가 + Stage 2 전이 ---
    found = {}   # ticker -> {"src": set, "first": date}
    win = snaps[-sessions:]
    for i, p in enumerate(win):
        js = json.loads(p.read_text()); d = js.get("as_of")
        for r in js.get("new_high_pass", []):
            f = found.setdefault(r["ticker"], {"src": set(), "first": d}); f["src"].add("신고가")
        gi = snaps.index(p)
        if gi > 0:
            pt = _transition_map(Path(str(snaps[gi - 1]).replace(".json", ".csv")))
            for r in js.get("pass_all", []):
                if r["ticker"] in pt:
                    f = found.setdefault(r["ticker"], {"src": set(), "first": d}); f["src"].add("전이")
    # --- 3단계 컷 (현재 세션 값 기준) ---
    rediscovered, added, refreshed = [], [], []
    for t, f in found.items():
        r = cur_pass.get(t)
        if not r:
            continue                      # 오늘 8/8 아님 → 후보 아님
        hi_cut = -5.0 if "신고가" in f["src"] else -15.0
        if (r["rs"] or 0) < 90 or (r["from_52w_high"] or -99) < hi_cut or (r["dvol_50d"] or 0) < cut["dvol"]:
            continue
        if t in reg:
            rediscovered.append((t, reg[t], r["rs"])); continue
        e = ent.get(t)
        if e and e["status"] in ("registered", "rejected"):
            # 처분 후 8주(40세션) 안에는 재진입 안 함
            if age(e.get("marked", e["first_seen"])) < 40:
                continue
            e = None
        if e is None:
            ent[t] = {"ticker": t, "name": r["name"], "status": "pending", "first_seen": f["first"],
                      "last_seen": as_of, "src": sorted(f["src"]), "rs": r["rs"],
                      "from_52w_high": r["from_52w_high"], "dvol_50d": r["dvol_50d"],
                      "group": roster_of.get(t)}
            added.append(t)
        else:
            e.update({"last_seen": as_of, "rs": r["rs"], "from_52w_high": r["from_52w_high"],
                      "src": sorted(set(e.get("src", [])) | f["src"])})
            refreshed.append(t)
    # --- 만료 ---
    expired = []
    for t, e in ent.items():
        if e["status"] not in ("pending", "deferred"):
            continue
        reason = None
        if t not in cur_pass:
            reason = "템플릿 이탈"
        elif age(e["last_seen"]) > QUEUE_TTL_SESSIONS:
            reason = f"미관측 {age(e['last_seen'])}세션"
        elif e["status"] == "pending" and age(e["first_seen"]) > QUEUE_MAX_AGE_SESSIONS:
            reason = f"pending {age(e['first_seen'])}세션 경과(미처분)"
        if reason:
            e["status"] = "expired"; e["expired"] = as_of; e["expire_reason"] = reason
            expired.append((t, reason))
            q["log"].append({"d": as_of, "t": t, "to": "expired", "note": reason})
    # --- 실적 트리아지 (pending/deferred 중 미보유분만) ---
    if not no_earnings:
        need = [t for t, e in ent.items() if e["status"] in ("pending", "deferred") and "earnings_raw" not in e]
        got = _earnings_fetch(market, need[:40])
        for t, ej in got.items():
            ent[t]["earnings_raw"] = ej
            ent[t]["earnings_asof"] = as_of
    for t, e in ent.items():
        if e["status"] in ("pending", "deferred"):
            e["group"] = roster_of.get(t)
            if "earnings_raw" in e:
                e["earnings"] = _earn_flags(market, e["earnings_raw"])   # 플래그 정의가 바뀌면 자동 반영
    qp.write_text(json.dumps(q, ensure_ascii=False, indent=1))
    # --- 출력 ---
    pend = sorted([e for e in ent.values() if e["status"] in ("pending", "deferred")],
                  key=lambda e: -(e.get("rs") or 0))
    flag = "🇰🇷" if market == "kr" else "🇺🇸"
    print(f"## {flag} 소싱 큐 — {market.upper()} (as-of {as_of}, 최근 {len(win)}세션 합집합 {len(found)}종 → "
          f"컷 통과 {len(added)+len(refreshed)+len(rediscovered)} / 신규 {len(added)} · 갱신 {len(refreshed)} · 재발견 {len(rediscovered)})")
    print(f"\n### pending {sum(1 for e in pend if e['status']=='pending')} · deferred {sum(1 for e in pend if e['status']=='deferred')}"
          f"  (상한 {QUEUE_CAP} — 넘으면 이번 패스에서 reject/defer로 줄일 것)")
    print("\n| 티커 | 종목명 | 상태 | 소스 | 그룹 | RS | 고점比 | 첫관측 | 경과 | 실적(최근2Q EPS/영업익 YoY) | 매출 YoY | 가속 | 힌트 | 메모 |")
    print("|---|---|---|---|---|--:|--:|---|--:|---|---|:-:|:-:|---|")
    for e in pend:
        er = e.get("earnings", {})
        if er.get("error"):
            es, rs_, ac, hint = f"⚠️ {er['error'][:28]}", "", "", "원문"
        elif er:
            es = "→".join(f"{x:+.0f}%" for x in er.get("eps_yoy_2q", [])) or "-"
            rs_ = "→".join(f"{x:+.0f}%" for x in er.get("rev_yoy_2q", [])) or "-"
            ac = "✅" if er.get("accel_eps") else ("◑" if er.get("accel_rev") else "❌")
            hint = "Code33" if er.get("code33_hint") else ("통과" if er.get("pass_hint") else "미달")
        else:
            es, rs_, ac, hint = "(미조회)", "", "", ""
        print(f"| {e['ticker']} | {e['name'][:18]} | {e['status']} | {'·'.join(e.get('src', []))} | {e.get('group') or ''} | {e.get('rs', 0):.1f} "
              f"| {e.get('from_52w_high', 0):.1f}% | {e['first_seen'][5:]} | {age(e['first_seen'])}s | {es} | {rs_} | {ac} | {hint} | {e.get('note', '')} |")
    if expired:
        print(f"\n### 이번 실행 만료 {len(expired)}종 (무언 이월 금지 — 리포트에 그대로 적을 것)")
        for t, r in expired:
            print(f"- {t} ({ent[t]['name'][:18]}): {r}")
    if rediscovered:
        print(f"\n### 재발견 {len(rediscovered)}종 (등록 명부에 있음 — 신규 아님)")
        print(", ".join(f"{t}[{tier}, RS {rs:.0f}]" for t, tier, rs in rediscovered))
    if len(pend) > QUEUE_CAP:
        print(f"\n🔴 pending+deferred {len(pend)} > 상한 {QUEUE_CAP} — 이번 패스에서 `queue {market} --mark T=rejected:사유` 로 줄여야 한다")
    print(f"\n처분: `sepa queue {market} --mark TICKER=registered|rejected|deferred[:메모]` (복수 가능) / 명부: `{WS / f'registry_{market}.json'}` / 큐: `{qp}`")


# ---------------- groupgate (그룹 브레드스 게이트 — 2026-09-22, IPS v2.2 §5·§3) ----------------

GROUP_GATE = {"pass_rate": 0.80, "near_high": 0.50, "near_high_pct": -5.0, "weeks": 2,
              "collapse_drop": 2}   # 붕괴: 전원 통과(✅)에서 2종+ 이탈 또는 통과율 2주 연속 하락


def _roster_stats(csv_p, tickers):
    """한 세션 CSV에서 로스터 통과율·고점근접률."""
    if not Path(csv_p).exists():
        return None
    with open(csv_p, newline="") as f:
        by = {r["ticker"]: r for r in csv.DictReader(f) if r["ticker"] in set(tickers)}
    n = len(by)
    if n == 0:
        return None
    passed = [t for t, r in by.items() if _truthy(r["pass_all"])]
    near = [t for t in passed if float(by[t]["from_52w_high"] or -99) >= GROUP_GATE["near_high_pct"]]
    return {"n": n, "pass": len(passed), "pass_rate": round(len(passed) / n, 2),
            "near_high": len(near), "near_rate": round(len(near) / max(1, len(passed)), 2),
            "failed": sorted(set(by) - set(passed))}


def cmd_groupgate(market, holdings=False):
    """그룹 브레드스 게이트 — ① 진입 예외(IPS §5): 시장 국면 '위험회피'여도 로스터가 조건(통과율≥80%·
    고점-5% 이내≥50%·2주 연속)을 만족하면 Sleeve B pilot 허용 ② 보유 경고(IPS §3): 로스터 통과율 2주 연속
    하락 또는 ✅ 붕괴(2종+ 이탈) → 그 그룹 보유 종목 스탑 상향 제안 재료. 판정 라벨은 기계, 실행은 invest-ops/decision."""
    snaps = sorted(OUT.glob(f"screen_{market}_*.csv"))
    if len(snaps) < 3:
        sys.exit("screen CSV 3세션 미만 — 추이 계산 불가")
    gm = load_groups_map(market)
    rosters = dict(gm.get("rosters", {}))
    hg = gm.get("holding_groups", {})
    if holdings:
        rosters.update(hg)
    as_of_p = Path(str(snaps[-1]).replace(".csv", ".json"))
    as_of = json.loads(as_of_p.read_text()).get("as_of", "?") if as_of_p.exists() else "?"
    # 주간 스냅샷: 오늘 / 5세션 전 / 10세션 전
    idx = [len(snaps) - 1, max(0, len(snaps) - 6), max(0, len(snaps) - 11)]
    flag = "🇰🇷" if market == "kr" else "🇺🇸"
    print(f"## {flag} 그룹 브레드스 게이트 — {market.upper()} (as-of {as_of}; 주간 스냅샷 = 0/-5/-10세션)")
    print(f"\n조건: 진입 예외 = 통과율 ≥{GROUP_GATE['pass_rate']:.0%} & 고점-5%이내 ≥{GROUP_GATE['near_high']:.0%} & 2주 연속 / "
          f"보유 경고 = 통과율 2주 연속 하락 또는 ✅→2종+ 이탈")
    print("\n| 그룹 | 통과(0) | 통과율 0/-5/-10 | 고점근접 0/-5 | 진입 예외 | 보유 경고 | 이탈(0) |")
    print("|---|--:|---|---|:-:|:-:|---|")
    out = {}
    for g, tk in rosters.items():
        st = [_roster_stats(snaps[i], tk) for i in idx]
        if not st[0]:
            print(f"| {g} | — | 데이터 없음 | | | | |"); continue
        pr = [x["pass_rate"] if x else None for x in st]
        nr = [x["near_rate"] if x else None for x in st]
        ok_now = pr[0] >= GROUP_GATE["pass_rate"] and nr[0] >= GROUP_GATE["near_high"]
        ok_prev = (pr[1] is not None and nr[1] is not None and
                   pr[1] >= GROUP_GATE["pass_rate"] and nr[1] >= GROUP_GATE["near_high"])
        entry = "✅ 열림" if (ok_now and ok_prev) else ("◑ 1주째" if ok_now else "❌")
        falling = (pr[1] is not None and pr[2] is not None and pr[0] < pr[1] < pr[2])
        collapse = (pr[1] == 1.0 and st[0]["pass"] <= st[1]["pass"] - GROUP_GATE["collapse_drop"]) if st[1] else False
        warn = "🔴 붕괴" if collapse else ("🟠 2주 하락" if falling else ("🟡 1주 하락" if (pr[1] is not None and pr[0] < pr[1]) else "—"))
        out[g] = {"pass_rate": pr, "near_rate": nr, "entry_exception": entry, "holding_warn": warn,
                  "failed": st[0]["failed"], "pass": st[0]["pass"], "n": st[0]["n"]}
        print(f"| {g}{' (보유)' if g in hg else ''} | {st[0]['pass']}/{st[0]['n']} | "
              f"{' / '.join('-' if x is None else f'{x:.0%}' for x in pr)} | "
              f"{' / '.join('-' if x is None else f'{x:.0%}' for x in nr[:2])} | {entry} | {warn} | "
              f"{', '.join(st[0]['failed'][:6])}{'…' if len(st[0]['failed']) > 6 else ''} |")
    if holdings and hg:
        # 보유 종목 매핑: positions.json theme → holding_groups 키
        pj = Path.home() / "Claude" / "PP" / "투자 잔고 동기화" / "positions.json"
        if pj.exists():
            pos = json.load(open(pj)).get("positions", {})
            print("\n### 보유 종목 × 그룹 경고 (positions.json theme ↔ holding_groups)")
            print("\n| 종목 | 슬리브 | theme | 그룹 경고 | 진입 예외 |")
            print("|---|:-:|---|:-:|:-:|")
            for code, m in pos.items():
                if m.get("exit"):
                    continue
                is_kr = code.isdigit()
                if (market == "kr") != is_kr:
                    continue
                th = m.get("theme")
                g = out.get(th)
                print(f"| {code} | {m.get('sleeve','?')} | {th or '-'} | {g['holding_warn'] if g else '(그룹 미정의)'} | {g['entry_exception'] if g else '-'} |")
    print(f"\n라벨은 기계 판정 — 진입 예외 적용·스탑 상향은 invest-decision §8 / invest-ops §4 규칙으로. 로스터·holding_groups 편집: `{WS / f'groups_{market}.json'}`")


# ---------------- sectors / momentum (2026-09-22 — 텔레그램 모니터링 보고 재료) ----------------

def cmd_sectors(market):
    """티커 → 섹터/업종 매핑 구축 → out/sectors_{market}.json. 월 1회면 충분(유니버스 재구축과 함께)."""
    out = {}
    if market == "us":
        d = json.loads(http_get("https://api.nasdaq.com/api/screener/stocks?limit=25000&download=true", timeout=90))
        for r in d["data"]["rows"]:
            out[r["symbol"].replace("/", "-")] = {"sector": r.get("sector") or "", "industry": r.get("industry") or ""}
    else:
        html = http_get("https://kind.krx.co.kr/corpgeneral/corpList.do?method=download", timeout=60).decode("cp949", "ignore")
        for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
            tds = [re.sub(r"<[^>]+>", "", td).strip() for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(tds) < 4 or not re.fullmatch(r"\d{6}", tds[2]):
                continue
            sfx = ".KQ" if "코스닥" in tds[1] else ".KS"
            out[tds[2] + sfx] = {"sector": tds[3], "industry": tds[3]}
    p = OUT / f"sectors_{market}.json"
    p.write_text(json.dumps(out, ensure_ascii=False))
    print(f"{market} sectors: {len(out)} -> {p}")


def load_sectors(market):
    p = OUT / f"sectors_{market}.json"
    if not p.exists():
        cmd_sectors(market)
    return json.loads(p.read_text())


def _ret(closes, n):
    return round((closes[-1] / closes[-1 - n] - 1) * 100, 1) if len(closes) > n and closes[-1 - n] else None


def cmd_momentum(market, top=8, top_stocks=10, as_json=False):
    """섹터·종목 모멘텀 랭킹 (1주/1개월/3개월 = 5/21/63세션) — 텔레그램 모니터링 보고 재료.

    섹터 점수 = z(1주)*0.2 + z(1개월)*0.4 + z(3개월)*0.4 (구성 중앙값) + 템플릿 통과율*1.0
    종목 점수(8/8 통과·유동성 컷 대상) = z(1개월)*0.3 + z(3개월)*0.3 + z(RS)*0.4, 50MA 이격 20%+는 '연장' 표기.
    근거 문장은 데이터로만 만든다 — 섹터 전망(외부 드라이버)은 아침 브리핑이 원문으로 덧붙인다."""
    import statistics
    snaps = sorted(OUT.glob(f"screen_{market}_*.csv"))
    if not snaps:
        sys.exit("screen CSV 없음")
    csv_p = snaps[-1]
    jp = Path(str(csv_p).replace(".csv", ".json"))
    as_of = json.loads(jp.read_text()).get("as_of", "?") if jp.exists() else "?"
    sec = load_sectors(market)
    cut = QUEUE_CUT[market]["dvol"]
    rows = []
    with open(csv_p, newline="") as f:
        for r in csv.DictReader(f):
            bars = load_bars(market, r["ticker"])
            if not bars or len(bars) < 70:
                continue
            c = [b["c"] for b in bars]
            s50 = sma(c, 50)
            rows.append({"ticker": r["ticker"], "name": r["name"], "rs": float(r["rs"] or 0),
                         "pass": _truthy(r["pass_all"]), "hi": float(r["from_52w_high"] or 0),
                         "dvol": float(r["dvol_50d"] or 0), "mcap": float(r["mcap"] or 0),
                         "r1w": _ret(c, 5), "r1m": _ret(c, 21), "r3m": _ret(c, 63),
                         "vs50": round((c[-1] / s50 - 1) * 100, 1) if s50 else None,
                         "above50": bool(s50 and c[-1] > s50),
                         "sector": (sec.get(r["ticker"]) or {}).get("sector", ""),
                         "industry": (sec.get(r["ticker"]) or {}).get("industry", "")})
    # ---- 섹터 집계 (업종 우선, 구성 5종 미만이면 섹터로 합침) ----
    def z(vals):
        v = [x for x in vals if x is not None]
        if len(v) < 2:
            return lambda x: 0.0
        m, sd = statistics.mean(v), statistics.pstdev(v) or 1.0
        return lambda x: 0.0 if x is None else (x - m) / sd
    groups = {}
    for r in rows:
        key = r["industry"] or r["sector"] or "미분류"
        groups.setdefault(key, []).append(r)
    small = {k: v for k, v in groups.items() if len(v) < 5}
    for k, v in small.items():
        del groups[k]
        for r in v:
            sk = r["sector"] or "미분류"
            groups.setdefault("[" + sk + "]", []).append(r)
    groups = {k: v for k, v in groups.items() if len(v) >= 5 and k != "미분류" and k != "[미분류]" and k != "[]"}
    agg = []
    for k, v in groups.items():
        med = lambda key: statistics.median([r[key] for r in v if r[key] is not None]) if any(r[key] is not None for r in v) else None
        agg.append({"group": k, "n": len(v), "r1w": med("r1w"), "r1m": med("r1m"), "r3m": med("r3m"),
                    "pass_rate": round(sum(r["pass"] for r in v) / len(v), 2),
                    "above50": round(sum(r["above50"] for r in v) / len(v), 2),
                    "near_high": round(sum(1 for r in v if r["pass"] and r["hi"] >= -5) / len(v), 2),
                    "leaders": sorted([r for r in v if r["pass"]], key=lambda r: -r["rs"])[:3]})
    z1, z2, z3 = z([a["r1w"] for a in agg]), z([a["r1m"] for a in agg]), z([a["r3m"] for a in agg])
    for a in agg:
        # 브레드스 가중을 크게 — 미너비니의 '주도 그룹'은 수익률보다 통과율·고점권 비율로 정의된다
        a["score"] = round(0.2 * z1(a["r1w"]) + 0.4 * z2(a["r1m"]) + 0.4 * z3(a["r3m"]) + 2.0 * a["pass_rate"] + 1.0 * a["near_high"], 2)
    # 매력도 자격: 통과율 10%+ 또는 50MA 위 60%+ (수익률만 높고 추세 종목이 없는 업종은 제외)
    agg.sort(key=lambda a: -a["score"])
    qualified = [a for a in agg if a["pass_rate"] >= 0.10 or a["above50"] >= 0.60]
    others = [a for a in agg if a not in qualified]
    agg = qualified + others
    # ---- 종목 랭킹 (8/8 + 유동성) ----
    cand = [r for r in rows if r["pass"] and r["dvol"] >= cut]
    s1, s3, sr = z([r["r1m"] for r in cand]), z([r["r3m"] for r in cand]), z([r["rs"] for r in cand])
    for r in cand:
        r["score"] = round(0.3 * s1(r["r1m"]) + 0.3 * s3(r["r3m"]) + 0.4 * sr(r["rs"]), 2)
        if r["vs50"] is not None and r["vs50"] >= 20:
            r["tag"] = "연장"
        elif r["hi"] >= -3:
            r["tag"] = "신고가권"
        elif r["vs50"] is not None and -3 <= r["vs50"] <= 5:
            r["tag"] = "50일선 근처"
        else:
            r["tag"] = "베이스"
    cand.sort(key=lambda r: -r["score"])
    # 큐 실적 힌트 병기
    qp = WS / f"sourcing_queue_{market}.json"
    qe = json.loads(qp.read_text())["entries"] if qp.exists() else {}
    def earn(t):
        e = (qe.get(t) or {}).get("earnings") or {}
        if not e or e.get("error"):
            return ""
        return "Code33" if e.get("code33_hint") else ("실적↑" if e.get("pass_hint") else "")
    buyable = [r for r in cand[:40] if r["tag"] != "연장" and r["hi"] >= -10 and (r["vs50"] is not None and r["vs50"] < 15)][:6]
    # 기간별 랭킹 (텔레그램 메시지 분리용): 섹터 = 자격 통과 업종을 그 기간 수익률로, 종목 = 8/8·유동성 컷을 그 기간 수익률로
    by_h = {}
    for h, key in (("1w", "r1w"), ("1m", "r1m"), ("3m", "r3m")):
        secs = sorted([a for a in qualified if a[key] is not None], key=lambda a: -a[key])[:8]
        stks = sorted([r for r in cand if r[key] is not None], key=lambda r: -r[key])[:8]
        by_h[h] = {"sectors": [{k: a[k] for k in ("group", "n", "r1w", "r1m", "r3m", "pass_rate", "near_high", "above50")} | {"leaders": [{"ticker": r["ticker"], "rs": r["rs"]} for r in a["leaders"][:2]]} for a in secs],
                   "stocks": [{**{k: r[k] for k in ("ticker", "name", "rs", "hi", "r1w", "r1m", "r3m", "vs50", "tag", "industry", "sector")}, "earn": earn(r["ticker"])} for r in stks]}
    result = {"market": market, "as_of": as_of, "pool": len(rows), "by_horizon": by_h,
              "stocks_buyable": [{**{k: r[k] for k in ("ticker", "name", "rs", "hi", "r1w", "r1m", "r3m", "vs50", "tag", "industry", "sector")}, "earn": earn(r["ticker"])} for r in buyable],
              "sectors_top": agg[:top], "sectors_bottom": agg[-3:][::-1] if len(agg) > 3 else [],
              "stocks_top": [{**{k: r[k] for k in ("ticker", "name", "rs", "hi", "r1w", "r1m", "r3m", "vs50", "tag", "score", "industry", "sector", "dvol", "mcap")},
                              "earn": earn(r["ticker"])} for r in cand[:top_stocks]]}
    for a in result["sectors_top"] + result["sectors_bottom"]:
        a["leaders"] = [{"ticker": r["ticker"], "name": r["name"], "rs": r["rs"], "hi": r["hi"]} for r in a["leaders"]]
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return result
    flag = "🇰🇷" if market == "kr" else "🇺🇸"
    fmt = lambda x: "-" if x is None else f"{x:+.1f}%"
    print(f"## {flag} 모멘텀 랭킹 — {market.upper()} (as-of {as_of}, 모수 {len(rows)}, 업종 {len(agg)}개)")
    print("\n### 섹터 상위 (구성 중앙값)\n\n| 순위 | 업종 | n | 1주 | 1개월 | 3개월 | 통과율 | 50MA위 | 고점권 | 리더 |\n|--:|---|--:|--:|--:|--:|--:|--:|--:|---|")
    for i, a in enumerate(result["sectors_top"], 1):
        print(f"| {i} | {a['group'][:26]} | {a['n']} | {fmt(a['r1w'])} | {fmt(a['r1m'])} | {fmt(a['r3m'])} | {a['pass_rate']:.0%} | {a['above50']:.0%} | {a['near_high']:.0%} | {', '.join(l['ticker'].split('.')[0] for l in a['leaders'])} |")
    print("\n### 섹터 하위 (회피)\n\n| 업종 | n | 1개월 | 3개월 | 통과율 |\n|---|--:|--:|--:|--:|")
    for a in result["sectors_bottom"]:
        print(f"| {a['group'][:26]} | {a['n']} | {fmt(a['r1m'])} | {fmt(a['r3m'])} | {a['pass_rate']:.0%} |")
    print(f"\n### 종목 상위 (8/8·거래대금 컷, 후보 {len(cand)})\n\n| 순위 | 티커 | 종목 | 업종 | RS | 1주 | 1개월 | 3개월 | 고점比 | 50MA이격 | 자리 | 실적 |\n|--:|---|---|---|--:|--:|--:|--:|--:|--:|---|---|")
    for i, r in enumerate(result["stocks_top"], 1):
        print(f"| {i} | {r['ticker'].split('.')[0]} | {r['name'][:14]} | {(r['industry'] or r['sector'])[:18]} | {r['rs']:.0f} | {fmt(r['r1w'])} | {fmt(r['r1m'])} | {fmt(r['r3m'])} | {r['hi']:.1f}% | {fmt(r['vs50'])} | {r['tag']} | {r['earn']} |")
    print(f"\n### 자리 있는 종목 (상위 40 중 연장 제외·고점 -10% 이내)\n\n| 티커 | 종목 | RS | 1개월 | 3개월 | 고점比 | 50MA이격 | 자리 |\n|---|---|--:|--:|--:|--:|--:|---|")
    for r in result["stocks_buyable"]:
        print(f"| {r['ticker'].split('.')[0]} | {r['name'][:14]} | {r['rs']:.0f} | {fmt(r['r1m'])} | {fmt(r['r3m'])} | {r['hi']:.1f}% | {fmt(r['vs50'])} | {r['tag']} |")
    print("\n점수: 섹터 = 0.2·z(1주)+0.4·z(1개월)+0.4·z(3개월)+2·통과율+고점권비율 / 종목 = 0.3·z(1개월)+0.3·z(3개월)+0.4·z(RS). 섹터 매핑: " + str(OUT / f"sectors_{market}.json"))
    return result


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_u = sub.add_parser("universe"); p_u.add_argument("market", choices=["us", "kr"])
    p_f = sub.add_parser("fetch"); p_f.add_argument("market", choices=["us", "kr"])
    p_f.add_argument("--limit", type=int); p_f.add_argument("--threads", type=int, default=6)
    p_f.add_argument("--force", action="store_true")
    p_s = sub.add_parser("screen"); p_s.add_argument("market", choices=["us", "kr"])
    p_s.add_argument("--rs", type=float, default=70)
    p_s.add_argument("--min-price", type=float); p_s.add_argument("--min-dvol", type=float)
    p_s.add_argument("--allow-drop", dest="allow_drop", action="store_true",
                     help="수집 게이트 우회 — 모수 급감이 정상임을 확인했을 때만 (기본: 직전 중앙값의 70%% 미만이면 중단)")
    p_s.add_argument("--as-of", dest="as_of", metavar="YYYY-MM-DD",
                     help="그 세션까지로 봉을 잘라 소급 스크리닝 (브레드스 이력 보정용)")
    p_b = sub.add_parser("backfill"); p_b.add_argument("market", choices=["us", "kr"])
    p_b.add_argument("--days", type=int, default=20, help="소급할 최근 세션 수 (기본 20)")
    p_b.add_argument("--force", action="store_true", help="이미 있는 세션도 재계산")
    p_c = sub.add_parser("check"); p_c.add_argument("ticker")
    p_v = sub.add_parser("vcp"); p_v.add_argument("ticker")
    p_d = sub.add_parser("daily"); p_d.add_argument("market", choices=["us", "kr"])
    p_l = sub.add_parser("passlist"); p_l.add_argument("market", choices=["us", "kr"])
    p_l.add_argument("--top", type=int)
    p_g = sub.add_parser("groups"); p_g.add_argument("market", choices=["us", "kr"])
    p_q = sub.add_parser("queue"); p_q.add_argument("market", choices=["us", "kr"])
    p_q.add_argument("--mark", nargs="+", metavar="T=status[:memo]")
    p_q.add_argument("--no-earnings", action="store_true")
    p_q.add_argument("--sessions", type=int, default=5)
    p_gg = sub.add_parser("groupgate"); p_gg.add_argument("market", choices=["us", "kr"])
    p_gg.add_argument("--holdings", action="store_true", help="positions.json 보유 종목을 holding_groups로 대조")
    p_sc = sub.add_parser("sectors"); p_sc.add_argument("market", choices=["us", "kr"])
    p_mo = sub.add_parser("momentum"); p_mo.add_argument("market", choices=["us", "kr"])
    p_mo.add_argument("--top", type=int, default=8); p_mo.add_argument("--stocks", type=int, default=10)
    p_mo.add_argument("--json", action="store_true")
    sub.add_parser("regime")
    a = ap.parse_args()
    if a.cmd == "universe":
        (universe_us if a.market == "us" else universe_kr)()
    elif a.cmd == "fetch":
        cmd_fetch(a.market, a.limit, a.threads, a.force)
    elif a.cmd == "screen":
        cmd_screen(a.market, a.rs, a.min_price, a.min_dvol, a.as_of, allow_drop=a.allow_drop)
    elif a.cmd == "backfill":
        cmd_backfill(a.market, a.days, a.force)
    elif a.cmd == "check":
        cmd_check(a.ticker)
    elif a.cmd == "vcp":
        cmd_vcp(a.ticker)
    elif a.cmd == "daily":
        cmd_daily(a.market)
    elif a.cmd == "passlist":
        cmd_passlist(a.market, a.top)
    elif a.cmd == "groups":
        cmd_groups(a.market)
    elif a.cmd == "queue":
        cmd_queue(a.market, a.mark, a.no_earnings, a.sessions)
    elif a.cmd == "groupgate":
        cmd_groupgate(a.market, a.holdings)
    elif a.cmd == "sectors":
        cmd_sectors(a.market)
    elif a.cmd == "momentum":
        cmd_momentum(a.market, a.top, a.stocks, a.json)
    elif a.cmd == "regime":
        cmd_regime()


if __name__ == "__main__":
    main()
