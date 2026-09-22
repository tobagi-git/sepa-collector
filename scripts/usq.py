#!/usr/bin/env python3
"""
usq.py — 미국 상장사 분기 실적 시계열 (SEC XBRL companyfacts) — krq.py의 미국판

미너비니 2단계(실적 가속 필터)용: 분기 매출·희석 EPS와 YoY를 뽑는다.
Q4는 대개 별도 공시가 없어 FY − (Q1+Q2+Q3)로 유도한다. 판정은 스킬이 한다.

사용법: python3 usq.py <티커> [<티커2> ...]
캐시: minervini-workspace/edgar/ (companyfacts 회사당 1회)
"""
import sys, json, time, re
import urllib.request
from pathlib import Path
from datetime import datetime

WS = Path.home() / ".claude" / "skills" / "minervini-workspace"
CACHE = WS / "edgar"
UA = "personal-research [메일]"

REV_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"]
EPS_TAGS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def cik_map():
    p = CACHE / "company_tickers.json"
    CACHE.mkdir(parents=True, exist_ok=True)
    if not p.exists() or (time.time() - p.stat().st_mtime) > 30 * 86400:
        p.write_bytes(get("https://www.sec.gov/files/company_tickers.json"))
    d = json.loads(p.read_text())
    return {v["ticker"].upper(): (str(v["cik_str"]).zfill(10), v["title"]) for v in d.values()}


def facts(cik):
    p = CACHE / f"facts_{cik}.json"
    if not p.exists() or (time.time() - p.stat().st_mtime) > 5 * 86400:
        p.write_bytes(get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"))
        time.sleep(0.3)
    return json.loads(p.read_text())


def dur(e):
    try:
        s = datetime.strptime(e["start"], "%Y-%m-%d")
        t = datetime.strptime(e["end"], "%Y-%m-%d")
        return (t - s).days
    except Exception:
        return -1


def series(gaap, tags, unit_pref):
    """분기(60~120일) + 연간(330~380일) 엔트리. 같은 (end,기간구분) 중 최신 제출본."""
    qs, fys = {}, {}
    for tag in tags:
        if tag not in gaap:
            continue
        units = gaap[tag]["units"]
        unit = next((u for u in unit_pref if u in units), None)
        if not unit:
            continue
        for e in units[unit]:
            d = dur(e)
            v, end, filed = e.get("val"), e.get("end"), e.get("filed", "")
            if v is None or not end:
                continue
            if 60 <= d <= 120:
                if end not in qs or filed > qs[end][1]:
                    qs[end] = (v, filed)
            elif 330 <= d <= 380:
                if end not in fys or filed > fys[end][1]:
                    fys[end] = (v, filed)
        if qs or fys:
            break  # 첫 번째로 데이터가 있는 태그만 (태그 혼용 방지)
    return {k: v[0] for k, v in qs.items()}, {k: v[0] for k, v in fys.items()}


def build_quarters(qs, fys):
    """Q4 유도(FY − 직전 3분기) 포함, end 날짜순 [(end, val)]."""
    out = dict(qs)
    for fy_end, fy_val in fys.items():
        if fy_end in out:
            continue
        fy_dt = datetime.strptime(fy_end, "%Y-%m-%d")
        prior = [v for e, v in qs.items()
                 if 0 < (fy_dt - datetime.strptime(e, "%Y-%m-%d")).days <= 290]
        if len(prior) == 3:
            out[fy_end] = fy_val - sum(prior)
    return sorted(out.items())


def yoy_pairs(rows):
    out = []
    for end, v in rows:
        d = datetime.strptime(end, "%Y-%m-%d")
        prev = [(e, pv) for e, pv in rows if 330 <= (d - datetime.strptime(e, "%Y-%m-%d")).days <= 380]
        py = prev[-1][1] if prev else None
        g = round((v / py - 1) * 100, 1) if py and py > 0 else None
        out.append({"end": end, "val": round(v, 2) if isinstance(v, float) else v, "yoy": g})
    return out


def main():
    tickers = [t.upper() for t in sys.argv[1:]]
    if not tickers:
        sys.exit("사용법: usq.py <티커> ...")
    cmap = cik_map()
    for t in tickers:
        if t not in cmap:
            print(json.dumps({"ticker": t, "error": "CIK 없음"}))
            continue
        cik, name = cmap[t]
        try:
            gaap = facts(cik).get("facts", {}).get("us-gaap", {})
        except Exception as e:
            print(json.dumps({"ticker": t, "error": str(e)[:100]}))
            continue
        rev = yoy_pairs(build_quarters(*series(gaap, REV_TAGS, ["USD"])))[-6:]
        eps = yoy_pairs(build_quarters(*series(gaap, EPS_TAGS, ["USD/shares"])))[-6:]
        print(json.dumps({"ticker": t, "name": name,
                          "rev_q": rev, "eps_q": eps,
                          "note": "rev 단위 USD. Q4는 FY−3분기 유도. YoY는 전년 동기 양수일 때만."},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
