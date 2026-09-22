#!/usr/bin/env python3
"""
krq.py — 한국 상장사 분기 실적 시계열 (OpenDART fnlttSinglAcnt)

미너비니 2단계(실적 가속 필터)용: 매출액·영업이익의 분기 시계열과 YoY를 뽑는다.
반기·연간 보고서는 누적치라 분기로 분해(diff)한다. 판정(가속/감속)은 스킬이 한다.

사용법: python3 krq.py <6자리 종목코드> [<코드2> ...]
사전 준비: ~/.config/opendart/api_key (corp-research와 공유)
"""
import sys, json, time, zipfile, io, re
import urllib.request
from pathlib import Path

WS = Path.home() / ".claude" / "skills" / "minervini-workspace"
KEY = (Path.home() / ".config" / "opendart" / "api_key").read_text().strip()


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def corp_map():
    p = WS / "dart_corpcode.json"
    if p.exists() and (time.time() - p.stat().st_mtime) < 30 * 86400:
        return json.loads(p.read_text())
    raw = get(f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={KEY}")
    xml = zipfile.ZipFile(io.BytesIO(raw)).read("CORPCODE.xml").decode("utf-8")
    m = {}
    for blk in re.findall(r"<list>(.*?)</list>", xml, re.S):
        stock = re.search(r"<stock_code>(\d{6})</stock_code>", blk)
        corp = re.search(r"<corp_code>(\d{8})</corp_code>", blk)
        name = re.search(r"<corp_name>(.*?)</corp_name>", blk)
        if stock and corp:
            m[stock.group(1)] = {"corp": corp.group(1), "name": name.group(1) if name else ""}
    p.write_text(json.dumps(m, ensure_ascii=False))
    return m


def _num(s):
    s = (s or "").replace(",", "").strip()
    try:
        return int(s)
    except ValueError:
        return None


def pick(items, patterns):
    """IS 계정에서 (당기 3개월 thstrm, 누적 thstrm_add) 쌍을 뽑는다."""
    for pat in patterns:
        for it in items:
            if it.get("sj_div") == "IS" and re.search(pat, it.get("account_nm", "")):
                cur, add = _num(it.get("thstrm_amount")), _num(it.get("thstrm_add_amount"))
                if cur is not None or add is not None:
                    return cur, add
    return None, None


def report(corp, year, reprt):
    url = (f"https://opendart.fss.or.kr/api/fnlttSinglAcnt.json?crtfc_key={KEY}"
           f"&corp_code={corp}&bsns_year={year}&reprt_code={reprt}")
    d = json.loads(get(url))
    if d.get("status") != "000":
        return None
    items = d.get("list", [])
    for fs in ("CFS", "OFS"):  # 연결 우선
        sub = [i for i in items if i.get("fs_div") == fs]
        if not sub:
            continue
        rc, ra = pick(sub, [r"^매출액$", r"수익\(매출액\)", r"영업수익", r"매출"])
        oc, oa = pick(sub, [r"^영업이익$", r"영업이익\(손실\)", r"영업이익"])
        if rc is not None or ra is not None or oc is not None:
            return {"rev": rc, "rev_add": ra, "op": oc, "op_add": oa}
    return None


def quarters(corp, years):
    """분기 실적 시계열. OpenDART 필드 의미: 11013(1Q)·11012(반기)·11014(3Q)의
    thstrm_amount = 당기 3개월, thstrm_add_amount = 누적. 11011(사업보고서) = 연간.
    Q4 = 연간 − 3Q 누적."""
    out = []
    for y in years:
        rp = {}
        for reprt, q in (("11013", 1), ("11012", 2), ("11014", 3), ("11011", 4)):
            r = report(corp, y, reprt)
            time.sleep(0.1)
            if r:
                rp[q] = r

        def disc(q, key):
            r = rp.get(q)
            if not r:
                return None
            cur, add = r[key], r[key + "_add"]
            if q == 1:
                return cur if cur is not None else add
            if q in (2, 3):
                if cur is not None and add is not None and cur != add:
                    return cur  # 3개월치가 따로 있으면 그것
                # 누적만 있으면 이전 분기 합으로 분해
                prev = [disc(i, key) for i in range(1, q)]
                base = add if add is not None else cur
                if base is not None and all(p is not None for p in prev):
                    return base - sum(prev)
                return cur
            # q == 4: 연간 − 3Q 누적
            annual = cur if cur is not None else add
            r3 = rp.get(3)
            cum3 = None
            if r3:
                cum3 = r3[key + "_add"]
                if cum3 is None:
                    prev = [disc(i, key) for i in range(1, 4)]
                    cum3 = sum(p for p in prev) if all(p is not None for p in prev) else None
            return annual - cum3 if annual is not None and cum3 is not None else None

        for q in (1, 2, 3, 4):
            if q not in rp:
                continue
            out.append((f"{y}Q{q}", disc(q, "rev"), disc(q, "op")))
    return out


def yoy(series):
    idx = {q: (r, o) for q, r, o in series}
    rows = []
    for q, r, o in series:
        py = str(int(q[:4]) - 1) + q[4:]
        pr, po = idx.get(py, (None, None))
        ry = round((r / pr - 1) * 100, 1) if r and pr and pr > 0 else None
        oy = round((o / po - 1) * 100, 1) if o and po and po > 0 else None
        rows.append({"q": q, "rev": r, "rev_yoy": ry, "op": o, "op_yoy": oy})
    return rows


def main():
    codes = [c for c in sys.argv[1:] if re.fullmatch(r"\d{6}", c)]
    if not codes:
        sys.exit("사용법: krq.py <6자리코드> ...")
    cmap = corp_map()
    thisyear = time.localtime().tm_year
    for code in codes:
        info = cmap.get(code)
        if not info:
            print(json.dumps({"code": code, "error": "DART 고유번호 없음"}, ensure_ascii=False))
            continue
        series = quarters(info["corp"], [thisyear - 2, thisyear - 1, thisyear])
        rows = yoy(series)[-6:]
        print(json.dumps({"code": code, "name": info["name"],
                          "quarters": rows,
                          "note": "단위 원, 연결 우선. YoY는 전년 동기 양수일 때만."},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
