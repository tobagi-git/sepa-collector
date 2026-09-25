#!/usr/bin/env python3
"""미너비니 텔레그램 알림 — 수집·스크리닝 직후 시장별로 1회 호출 (토큰 0, GitHub Actions·맥 공통).

보내는 것:
  1. 시장 요약 (항상)            — 시장 체력·추세 통과 수·막 올라탄 종목·지켜보는 후보
  2. 모멘텀 6개 (항상)           — 1주/1개월/3개월 × 강한 업종/강한 종목
  3. 조건부 알림                 — 후보 50일선 재진입 자리 / 후보 50일선 이탈 / 강한 종목 추세 진입 /
                                  데이터 주의 / 업종 그룹 상태 변화
표시 형식(2026-09-25): tg_style.py — 두 줄 구성·굵은 제목·메시지 뜻 한 줄·필요한 용어만 풀이. HTML 발송.
시크릿: 환경변수 TELEGRAM_TOKEN/TELEGRAM_CHAT_ID(Actions) → 없으면 ~/.config/ke-award-alert/secrets.env
상태: alerts/sepa_notify.state.json
사용: sepa_notify.py us|kr [--dry] [--momentum-only] | --send-file <파일>
"""
import json, os, re, subprocess, sys
import urllib.request, urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
WS = HERE.parent
SEPA = WS / "scripts" / "sepa.py"          # 워크스페이스 안의 스크립트 (맥·GitHub Actions 공통)
sys.path.insert(0, str(HERE))
import tg_style as T
E = T.E


def _creds():
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        return tok, chat
    try:
        import apr_breakout
        s = apr_breakout.secrets()
        return s.get("TELEGRAM_BOT_TOKEN"), s.get("TELEGRAM_CHAT_ID")
    except Exception:
        return None, None


def telegram(text, html_mode=True):
    tok, chat = _creds()
    if not (tok and chat):
        print("[sepa_notify] 텔레그램 미설정 — 발송 생략"); return False
    data = {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    if html_mode:
        data["parse_mode"] = "HTML"
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=urllib.parse.urlencode(data).encode()), timeout=20)
        return r.status == 200
    except Exception as e:
        print(f"[sepa_notify] 텔레그램 전송 실패: {str(e)[:120]}"); return False


STATE = HERE / "sepa_notify.state.json"
FLAG = {"us": "🇺🇸", "kr": "🇰🇷"}
MKT = {"us": "미국", "kr": "한국"}


def run(*args):
    return subprocess.run([sys.executable, str(SEPA), *args], capture_output=True, text=True, timeout=600).stdout


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def tk(t):
    return t.split(".")[0]


def _names(market):
    p = WS / "universe" / f"{market}.tsv"
    out = {}
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            if "\t" in ln:
                t, n = ln.split("\t", 1); out[t] = n
    return out


def label(market, ticker, names):
    """한국은 종목명, 미국은 티커 + 짧은 회사명."""
    if market == "kr":
        return names.get(ticker) or tk(ticker)
    return f"{tk(ticker)} {T.short_name(names.get(ticker, ''))}".strip()


EARN_KO = {"Code33": "실적 가속", "실적↑": "실적 개선"}


# ---------- 모멘텀 6개 ----------
HORIZON = {"1w": "1주", "1m": "1개월", "3m": "3개월"}
RKEY = {"1w": "r1w", "1m": "r1m", "3m": "r3m"}


def momentum_messages(market):
    """sepa momentum JSON → 기간별 강한 업종 3개 + 강한 종목 3개 메시지."""
    try:
        m = json.loads(run("momentum", market, "--json"))
    except Exception:
        return []
    f, out = FLAG[market], []
    for h in ("1w", "1m", "3m"):
        hz, k, d = HORIZON[h], RKEY[h], m["by_horizon"][h]
        # --- 업종 ---
        L = [f"<b>📊 {f} {hz} 강한 업종</b> · {m['as_of']}",
             f"<i>최근 {hz} 많이 오른 업종 순. 상승 추세 종목이 어느 정도 있는 업종만 골랐다</i>", ""]
        for i, a in enumerate(d["sectors"], 1):
            lead = ", ".join(tk(l["ticker"]) for l in a["leaders"])
            L.append(f"<b>{i}. {E(T.industry(market, a['group']))}</b> {T.pct(a[k])}")
            L.append(f"   1주 {T.pct(a['r1w'])} · 1개월 {T.pct(a['r1m'])} · 3개월 {T.pct(a['r3m'])}")
            L.append(f"   {a['n']}종 중 {a['pass_rate']:.0%}가 상승 추세" + (f" · 고점 근처 {a['near_high']:.0%}" if a["near_high"] else "")
                     + (f" · 앞장선 종목 {E(lead)}" if lead else ""))
        if not d["sectors"]:
            L.append("해당 없음 — 상승 추세 종목을 가진 업종이 없다")
        L += ["", T.glossary("수익률은 그 업종 종목들의 가운데 값이라 한 종목 급등에 끌려가지 않는다 · "
                             "상승 추세 = 미너비니 추세 조건 8개를 모두 만족 · 고점 근처 = 52주 최고가 5% 안")]
        out.append("\n".join(L))
        # --- 종목 ---
        L = [f"<b>📈 {f} {hz} 강한 종목</b> · {m['as_of']}",
             f"<i>상승 추세 조건을 모두 만족하고 거래가 충분한 종목 중 최근 {hz} 많이 오른 순</i>", ""]
        for i, r in enumerate(d["stocks"], 1):
            tag = r.get("tag") or ""
            head = (f"<b>{i}. {E(r['name'][:12])}</b> {tk(r['ticker'])}" if market == "kr"
                    else f"<b>{i}. {E(tk(r['ticker']))}</b> {E(T.short_name(r['name']))}")
            L.append(f"{head} {T.pct(r[k])} · {T.TAG_ICON.get(tag, '')} {E(T.TAG_KO.get(tag, tag))}")
            ind = T.industry(market, r.get("industry") or r.get("sector") or "")
            L.append(f"   1주 {T.pct(r['r1w'])} · 1개월 {T.pct(r['r1m'])} · 3개월 {T.pct(r['r3m'])}")
            L.append(f"   RS {r['rs']:.0f} · 고점 {T.pct(r['hi'])}" + (f" · {E(ind)}" if ind else "")
                     + (f" · {E(EARN_KO.get(r['earn'], r['earn']))}" if r.get("earn") else ""))
        if not d["stocks"]:
            L.append("해당 없음")
        L += ["", T.glossary("RS = 최근 1년 주가 강도 순위(100이 최상) · 고점 = 52주 최고가 대비 · "
                             "🔸연장 = 50일선보다 20% 넘게 위라 지금 사면 추격 · 🔹신고가권 = 고점 3% 안 · "
                             "🟢50일선 근처 = 쉬어가는 재진입 자리 · ⚪베이스 = 고점에서 내려와 조정 중 · "
                             "실적 가속 = 최근 두 분기 이익·매출이 25% 넘게 늘고 증가 속도도 빨라짐")]
        out.append("\n".join(L))
    return out


# ---------- 본체 ----------
def main(market, dry=False):
    st = load_state(); ms = st.setdefault(market, {})
    d = json.loads(run("daily", market))
    as_of = d["as_of"]; b = d["breadth_today"]; p = d.get("breadth_prev") or {}; pt = d["pool_trend"]
    f, mk = FLAG[market], MKT[market]
    msgs, alerts = [], []

    # ---- 1. 시장 요약 ----
    x = list(d["stage2_crossovers"])
    x90 = [c for c in x if (c["rs"] or 0) >= 90]
    reg = json.loads((WS / f"registry_{market}.json").read_text()) if (WS / f"registry_{market}.json").exists() else {}
    zero = reg.get("0군", [])
    names = _names(market)
    p200, n200 = p.get("pct_above_200ma"), b["pct_above_200ma"]
    arrow = "" if p200 is None else (" ↑" if n200 > p200 else (" ↓" if n200 < p200 else ""))
    L = [f"<b>🧭 {f} {mk} 미너비니 · {as_of} 장 마감</b>",
         "<i>오르는 추세에 있는 종목이 얼마나 되는지로 시장 체력을 본다</i>", "",
         f"<b>시장 체력: {T.breadth_word(n200)}</b>{arrow}",
         f"   200일선 위 종목 {p200 if p200 is not None else '-'} → {n200}% · 50일선 위 {b['pct_above_50ma']}%",
         f"   신고가 {b.get('new_52w_high', '-')} − 신저가 {b.get('new_52w_low', '-')} = {b['net_new_highs']:+d}",
         f"<b>상승 추세 종목 {d['pass_all_count']}개</b> (전일 대비 {d['pass_all_delta']:+d})",
         f"   새로 들어옴 {d['new_entrants_count']} · 빠짐 {d['dropped_count']}"]
    if not d["stage2_crossovers_reliable"]:
        L.append(f"<b>막 올라탄 종목</b>: 판정 불가 — {E(d['stage2_crossovers_note'] or '')}")
    elif x:
        L.append(f"<b>오늘 상승 추세에 막 올라탄 종목</b> {len(x)}개")
        L.append("   " + " · ".join(f"{E(tk(c['ticker']) if market == 'us' else c['name'][:8])} RS{c['rs']:.0f}" for c in x[:6])
                 + (f" 외 {len(x)-6}" if len(x) > 6 else ""))
    else:
        L.append("<b>오늘 상승 추세에 막 올라탄 종목</b>: 없음")

    # ---- 지켜보는 후보 (0군) ----
    zero_rows, prev_fail, now_fail = [], set(ms.get("zero_fail", [])), set()
    for t in zero:
        try:
            v = json.loads(run("vcp", t))
        except Exception:
            continue
        pb = v["alt_setups"]["pullback_50ma"]; w = v["weekly"]
        cond = (pb["status"] == "at_50ma" and pb["sma50_rising"] and pb["had_run_60d"] and pb["touch_episodes_60d"] <= 2
                and (pb["pullback_downday_vol_over_v50"] or 9) < 1.0 and pb["closed_below_sma50_last5"] == 0)
        state_txt = ("🟢 50일선 재진입 자리" if cond else "🟡 50일선에 접근 중" if pb["status"] == "approaching"
                     else "🔴 50일선 아래" if pb["status"] == "below_50ma" else "⚪ 50일선 위 여유")
        piv = v["pct_to_pivot"]
        piv_txt = f"돌파 기준가까지 {piv:+.1f}%" if piv > 0 else f"돌파 기준가 위 {abs(piv):.1f}%"
        zero_rows.append(f"<b>{E(label(market, t, names))}</b> {state_txt}\n   {piv_txt} · 조정 {w['weeks_elapsed']}주째")
        if cond and ms.get("setup_" + t) != as_of:
            alerts.append("\n".join([
                f"<b>🟢 {f} {E(label(market, t, names))} — 50일선 재진입 자리</b>",
                "<i>달리던 종목이 쉬어가며 오르는 50일선까지 내려왔다. 미너비니가 추가 진입하는 전형적인 자리</i>", "",
                f"   50일선 {pb['sma50']} · 지금 가격은 그 위 {pb['pct_vs_sma50']:+.1f}%",
                f"   최근 60일 안 {pb['touch_episodes_60d']}번째 닿음 (1~2번째가 좋은 자리)",
                f"   내려올 때 거래량 평소의 {pb['pullback_downday_vol_over_v50']}배 (1 미만이면 조용한 조정)",
                f"   반등 확인 {'됨 ✅' if pb['bounce_hint'] else '아직'}", "",
                "<i>판정은 데일리 리포트에서</i>"]))
            ms["setup_" + t] = as_of
        if pb["status"] == "below_50ma":
            now_fail.add(t)
    for t in sorted(now_fail - prev_fail):
        alerts.append(f"<b>🔴 {f} {E(label(market, t, names))} — 50일선 아래로 내려감</b>\n"
                      "<i>추세 종목이 50일선을 잃으면 셋업이 무효가 될 수 있다. 데일리에서 확인</i>")
    ms["zero_fail"] = sorted(now_fail)
    if zero_rows:
        L += ["", "<b>👀 지켜보는 후보</b>"] + zero_rows
    L += ["", f"<i>조사 대상 {b['pool']:,}종 · 데이터 누락 {pt['stale_excluded']}</i>",
          T.glossary("시장 체력은 200일선 위 종목 비율로 본다(60% 넘으면 우호, 40% 아래면 악화) · "
                     "신고가 − 신저가가 음수면 시장 힘이 빠지는 중 · 돌파 기준가 = 조정 구간 위쪽 끝, 이걸 넘을 때가 매수 자리 · "
                     "조정 n주째 = 최근 고점에서 쉬고 있는 기간")]
    msgs.append("\n".join(L))
    msgs.extend(momentum_messages(market))

    # ---- 데이터 주의 ----
    if pt["flag"] == "수집의심" or (pt["stale_excluded"] or 0) / max(1, b["pool"] + (pt["stale_excluded"] or 0)) >= 0.03:
        alerts.append(f"<b>⚠️ {f} 데이터 주의</b>\n"
                      f"   조사 대상 종목 수가 하루 만에 {pt['vs_1d_pct']:+.1f}% 변했고 누락이 {pt['stale_excluded']}종\n"
                      "<i>시장이 아니라 수집 문제일 수 있어 오늘 비율 수치는 어제와 비교하지 않는다</i>")

    # ---- 강한 종목 추세 진입 ----
    if x90:
        rows = [f"<b>{E(label(market, c['ticker'], names))}</b>\n   RS {c['rs']:.0f} · 고점 {c['from_52w_high']:+.1f}%" for c in x90]
        alerts.append("\n".join([f"<b>🚀 {f} 강한 종목이 오늘 상승 추세에 올라탐</b>",
                                 "<i>방금 추세가 시작된 종목이라 아직 살 자리는 아니고, 지켜볼 명단에 올릴 재료</i>", ""]
                                + rows + ["", T.glossary("RS 90 이상 = 최근 1년 주가 강도 상위 10%")]))

    # ---- 업종 그룹 상태 변화 ----
    gg = run("groupgate", market, "--holdings")
    labels = {}
    for ln in gg.splitlines():
        mm = re.match(r"\|\s*([^|]+?)\s*\|\s*(\d+)/(\d+)\s*\|[^|]*\|[^|]*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", ln)
        if mm and mm.group(1) not in ("그룹",):
            labels[mm.group(1)] = {"pass": f"{mm.group(2)}/{mm.group(3)}", "entry": mm.group(4), "warn": mm.group(5)}
    prev = ms.get("groupgate", {})
    changed = []
    ENTRY = lambda s: "소액 진입 허용" if "✅" in s else ("1주째 조건 충족" if "◑" in s else "닫힘")
    for g, v in labels.items():
        pv = prev.get(g)
        if pv and (pv["entry"] != v["entry"] or pv["warn"] != v["warn"]):
            row = f"<b>{E(g)}</b> 추세 종목 {v['pass']}"
            parts = []
            if pv["entry"] != v["entry"]:
                parts.append(f"진입: {ENTRY(pv['entry'])} → {ENTRY(v['entry'])}")
            if pv["warn"] != v["warn"]:
                parts.append(f"경고: {E(pv['warn'])} → {E(v['warn'])}")
            changed.append(row + "\n   " + " · ".join(parts))
    if changed:
        alerts.append("\n".join([f"<b>🧩 {f} 업종 그룹 상태 변화</b>",
                                 "<i>같은 업종 종목들이 함께 강해지거나 무너지는지 본다</i>", ""] + changed + ["",
                                 T.glossary("소액 진입 허용 = 그 업종 대부분이 추세를 타고 고점 근처라 시장이 나빠도 조금은 들어갈 수 있음 · "
                                            "🟠 2주 하락·🔴 붕괴 = 그 업종이 무너지는 중이라 보유 종목 손절선을 올릴 때")]))
    ms["groupgate"] = labels
    ms["last_as_of"] = as_of

    out = msgs + alerts
    for m in out:
        if dry:
            print("---\n" + m)
        else:
            telegram(m)
    if not dry:
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    print(f"[sepa_notify] {market} {as_of}: 요약 1 + 모멘텀 {len(msgs)-1} + 알림 {len(alerts)}건 {'(dry)' if dry else '전송'}")


if __name__ == "__main__":
    if "--send-file" in sys.argv:            # 아침 브리핑이 쓴 줄글(섹터 전망 등) — 평문으로 발송
        fp = Path(sys.argv[sys.argv.index("--send-file") + 1])
        txt = fp.read_text().strip()
        for chunk in [txt[i:i + 3800] for i in range(0, len(txt), 3800)]:   # 텔레그램 4096자 제한
            telegram(chunk, html_mode=False)
        print(f"[sepa_notify] 파일 발송 {fp} ({len(txt)}자)"); sys.exit(0)
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if not a or a[0] not in ("us", "kr"):
        sys.exit("사용: sepa_notify.py us|kr [--dry] [--momentum-only] | --send-file <파일>")
    if "--momentum-only" in sys.argv:          # 모멘텀 보고만 (알림 상태 건드리지 않음)
        for mm in momentum_messages(a[0]):
            print("---\n" + mm) if "--dry" in sys.argv else telegram(mm)
        sys.exit(0)
    main(a[0], dry="--dry" in sys.argv)
