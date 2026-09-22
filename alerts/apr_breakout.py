#!/usr/bin/env python3
"""에이피알(278470) 473,000 돌파 알림.

베이스 고점 473,000 = data/kr/278470.KS.json 의 252봉 `h` 최고가 (as-of 2026-08-25 확인).
KR 수집기(16:40 시작, 실측 ~4h10m 소요)가 당일 종가를 다 넣은 뒤 21:30에 돌고,
조건 성립 시 텔레그램 1회 발사한다. 2026-08-28까지는 17:00에 돌아 **항상 직전 세션**을
판정하는 결함이 있었다 — 발사 시각 이동 + as_of 검증(아래 session_asof)으로 함께 고친다.

판정은 하지 않는다 — §6 돌파 유효 조건(거래량 50일 평균 +40%)을 계산해 같이 보내고,
살지 말지는 사람이 정한다. 상한가 잠김(±30%) 경고도 붙인다.
"""
import json, os, sys, datetime, urllib.request, urllib.parse

TICKER   = "278470.KS"
NAME     = "에이피알"
PIVOT    = 473_000.0          # 베이스 고점 (구조적 저항). 사후 상향 금지.
BARS     = os.path.expanduser(f"~/.claude/skills/minervini-workspace/data/kr/{TICKER}.json")
STATE    = os.path.expanduser("~/.claude/skills/minervini-workspace/alerts/apr_breakout.state.json")
SECRETS  = os.path.expanduser("~/.config/ke-award-alert/secrets.env")
LOG      = os.path.expanduser("~/.claude/skills/minervini-workspace/alerts/apr_breakout.log")
INBOX    = os.path.expanduser("~/.claude/skills/minervini-workspace/alerts/INBOX.md")
STALE_SESSIONS = 3


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def secrets():
    d = {}
    try:
        for ln in open(SECRETS):
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    except OSError as e:
        log(f"[!] 시크릿 읽기 실패: {e}")
    return d


def telegram(text):
    """텔레그램 시크릿이 채워져 있을 때만 사용. 비어 있으면 조용히 False."""
    s = secrets()
    tok, chat = s.get("TELEGRAM_BOT_TOKEN"), s.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": chat, "text": text}).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status == 200
    except Exception as e:
        log(f"[!] 텔레그램 전송 실패: {e}")
        return False
    log("텔레그램 전송 " + ("성공" if ok else "실패"))
    return ok


def mac_notify(title, body):
    """macOS 알림 센터. 시크릿 불필요."""
    import subprocess
    esc = lambda t: t.replace('\\', '\\\\').replace('"', '\\"')
    one = " / ".join(body.splitlines()[:3])
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{esc(one)[:230]}" with title "{esc(title)}" sound name "Glass"'],
            check=True, capture_output=True, timeout=15)
        log("macOS 알림 표시")
        return True
    except Exception as e:
        log(f"[!] macOS 알림 실패: {e}")
        return False


def notify(title, text):
    """다중 경로. 하나라도 성공하면 True. 파일 드롭은 항상 남긴다(놓칠 수 없게)."""
    with open(INBOX, "a") as f:
        f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====\n{title}\n{text}\n")
    ok_tg = telegram(f"{title}\n\n{text}")
    ok_mac = mac_notify(title, text)
    if not (ok_tg or ok_mac):
        log("[!] 즉시 알림 경로 전부 실패 — 파일 드롭만 남았다: " + INBOX)
    return ok_tg or ok_mac


DATA_DIR = os.path.expanduser("~/.claude/skills/minervini-workspace/data/kr")
ASOF_SAMPLE = 40


def market_asof():
    """유니버스 표본의 최신 봉 날짜 = 시장 전체가 도달한 세션일.

    종목 파일 하나만 보면 «내 종목이 뒤처진 것»과 «시장이 아직 그 세션에 도달하지
    않은 것»(휴장·수집 미완)을 구분할 수 없다. 표본의 최댓값으로 시장 기준선을 잡는다.
    읽기 실패는 조용히 건너뛴다 — 기준선은 최댓값이라 일부 결측에 강하다.
    """
    best = None
    try:
        names = sorted(os.listdir(DATA_DIR))
    except OSError as e:
        log(f"[!] 유니버스 디렉터리 읽기 실패: {e}")
        return None
    step = max(1, len(names) // ASOF_SAMPLE)
    for n in names[::step]:
        if not n.endswith(".json"):
            continue
        try:
            bars = json.load(open(os.path.join(DATA_DIR, n)))["bars"]
            d = bars[-1]["d"]
        except Exception:
            continue
        if best is None or d > best:
            best = d
    return best


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(st):
    json.dump(st, open(STATE, "w"), ensure_ascii=False, indent=2)


def main():
    try:
        bars = json.load(open(BARS))["bars"]
    except Exception as e:
        log(f"[!] 봉 데이터 읽기 실패: {e}")
        sys.exit(1)

    last = bars[-1]
    st = load_state()

    # ── 신선도: 수집이 멈춘 걸 «조건 미성립»과 혼동하지 않는다 ──────────────
    d = datetime.date.fromisoformat(last["d"])
    age = (datetime.date.today() - d).days
    if age > STALE_SESSIONS:
        if st.get("stale_warned") != last["d"]:
            notify(f"⚠️ {NAME} 돌파 감시 — 데이터 낡음",
                   f"마지막 봉 {last['d']} ({age}일 전).\n"
                   f"KR 수집기(com.ruby.minervini-kr)를 확인하세요. 감시는 계속 돌지만 판정 불가 상태입니다.")
            st["stale_warned"] = last["d"]
            save_state(st)
        log(f"데이터 낡음 {last['d']} ({age}일) — 판정 보류")
        return
    st.pop("stale_warned", None)

    # ── as_of 검증: «내 종목이 뒤처짐»과 «시장이 아직 그 세션에 없음»을 가른다 ──
    # 2026-08-28 결함: 17:00 발사 + 이 검증 부재로 알림이 항상 직전 세션을 판정했고,
    # 로그의 «거리 -8.1%»가 실제(-2.4%)와 5.7%p 어긋난 채 조용히 지나갔다.
    mkt = market_asof()
    today = datetime.date.today().isoformat()
    if mkt and last["d"] < mkt:
        # 시장은 도달했는데 이 종목만 없다 = 종목 단위 수집 사고. 즉시 알린다.
        if st.get("asof_warned") != mkt:
            notify(f"⚠️ {NAME} 돌파 감시 — 종목 데이터가 시장보다 뒤처짐",
                   f"{NAME} 마지막 봉 {last['d']} / 시장 최신 세션 {mkt}.\n"
                   f"이 종목만 갱신되지 않았습니다 — 판정을 보류합니다.\n"
                   f"`sepa.py fetch kr` 재수집 또는 data/kr/{TICKER}.json 확인이 필요합니다.")
            st["asof_warned"] = mkt
            save_state(st)
        log(f"[!] as_of 뒤처짐: 종목 {last['d']} < 시장 {mkt} — 판정 보류")
        return
    st.pop("asof_warned", None)

    if mkt and mkt < today:
        # 시장 전체가 오늘 세션에 없다 = 휴장이거나 수집 미완. 판정할 새 데이터가
        # 없으므로 보류하되 알리지는 않는다 — 휴장마다 울리면 경보가 무뎌진다.
        # 진짜 다일 장애는 위 STALE_SESSIONS 게이트가 잡는다.
        log(f"오늘({today}) 세션 데이터 없음 — 시장 최신 {mkt} (휴장 또는 수집 미완) · 판정 보류")
        return

    log(f"as_of 검증 통과: 종목·시장 모두 {last['d']}")

    c, h, v = last["c"], last["h"], last["v"]
    v50 = sum(b["v"] for b in bars[-50:]) / 50.0
    vr = v / v50 if v50 else 0.0
    log(f"{last['d']} c={c:,.0f} h={h:,.0f} v/v50={vr:.2f} (피벗 {PIVOT:,.0f}, 거리 {(c/PIVOT-1)*100:+.1f}%)")

    touched = h >= PIVOT
    closed_above = c >= PIVOT
    kind = "close" if closed_above else ("intraday" if touched else None)
    if kind is None:
        return
    if st.get("fired_kind") == "close" or (st.get("fired_kind") == kind and st.get("fired_date") == last["d"]):
        return  # 종가 돌파를 이미 쐈으면 종료, 같은 날 같은 종류 중복 금지

    vol_ok = vr >= 1.4      # §6: 돌파일 거래량 50일 평균 +40%+
    limit_up = len(bars) > 1 and bars[-2]["c"] and (c / bars[-2]["c"] - 1) >= 0.295  # 상한가 잠김 경계

    head = f"🚨 {NAME}({TICKER.split('.')[0]}) 베이스 고점 {PIVOT:,.0f} " + ("종가 돌파" if closed_above else "장중 터치")
    msg = (
        f"판정 세션(as_of) {last['d']}  종가 {c:,.0f} / 고가 {h:,.0f}\n"
        f"거래량 {v:,.0f} = 50일평균 대비 {vr:.2f}x  → 돌파 거래량 {'✅ 충족' if vol_ok else '❌ 미달'} (§6 기준 1.4x)\n\n"
        + ("⚠️ 상한가 잠김 가능 — §6은 상한가 따라잡기를 금지한다. 되돌림 대기.\n\n" if limit_up else "")
        + ("🔴 저거래량 돌파는 §8 실패 돌파 유형이다. 돌파 후 3일 내 베이스 안으로 되밀리는지(squat) 확인.\n\n" if not vol_ok else "")
        + "확인할 것: ① 분산(피델리티 4.85% 비가시 잔여분 소화 여부) ② 되돌림 시 피벗·10일선 지지\n"
        "이 알림은 판정이 아니다. 사이징·스탑은 invest-decision, 구조 재판정은 주말 구조 패스."
    )
    if notify(head, msg):
        st["fired_kind"] = kind
        st["fired_date"] = last["d"]
        save_state(st)


if __name__ == "__main__":
    main()
