#!/usr/bin/env python3
"""미너비니 텔레그램 알림 — daily_collect.sh 가 수집·스크리닝을 끝낸 직후 시장별로 1회 호출 (토큰 0).

보내는 것 (2026-09-22 배선):
  1. 수집 완료 요약 (항상)      — 세션일·통과 Δ·200MA/50MA%·순신고가·모수·결측·Stage 2 전이·0군 한 줄
  2. 수집 사고 경보 (조건)      — 모수 1일 5%+ 변동 또는 결측 3%+
  3. 셋업 알림 (조건, 세션당 1회) — 0군 종목 50MA 되돌림 전조건(§7-1 A) 충족 / 0군 템플릿 이탈 신규 / Stage 2 전이 RS 90+
  4. 그룹 게이트 변화 (조건)    — groupgate 라벨(진입 예외·보유 경고)이 전 실행 대비 바뀐 로스터만
시크릿: ~/.config/ke-award-alert/secrets.env (apr_breakout.py 와 공유). 상태: alerts/sepa_notify.state.json
사용: sepa_notify.py us|kr [--dry]
"""
import json, os, re, subprocess, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
WS = HERE.parent
SEPA = WS / "scripts" / "sepa.py"          # 워크스페이스 안의 스크립트 (맥·GitHub Actions 공통)
sys.path.insert(0, str(HERE))
import urllib.request, urllib.parse


class _TG:
    """텔레그램 전송 — 환경변수(TELEGRAM_TOKEN/TELEGRAM_CHAT_ID, GitHub Actions) 우선, 없으면 로컬 시크릿 파일(apr_breakout)."""
    def telegram(self, text):
        tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if not (tok and chat):
            try:
                import apr_breakout
                return apr_breakout.telegram(text)
            except Exception as e:
                print(f"[sepa_notify] 텔레그램 미설정/실패 — 발송 생략: {e}"); return False
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                f"https://api.telegram.org/bot{tok}/sendMessage",
                data=urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()), timeout=20)
            return r.status == 200
        except Exception as e:
            print(f"[sepa_notify] 텔레그램 전송 실패: {str(e)[:80]}"); return False


tg = _TG()
STATE = HERE / "sepa_notify.state.json"
FLAG = {"us": "🇺🇸", "kr": "🇰🇷"}


def run(*args):
    return subprocess.run([sys.executable, str(SEPA), *args], capture_output=True, text=True, timeout=600).stdout


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def _pct(x):
    return "-" if x is None else f"{x:+.0f}%"


def _short(name, n=10):
    for suf in (" 제조업", " 및 공급업", " 서비스업", "업"):
        pass
    name = re.sub(r"\s*(제조업|도매업|공급업|서비스업|건조업|건설업)$", "", name).strip("[]")
    if "," in name:
        name = name.split(",")[0].strip() + " 등"
    return name[:n]


HORIZON = {"1w": "1주", "1m": "1개월", "3m": "3개월"}
RKEY = {"1w": "r1w", "1m": "r1m", "3m": "r3m"}


def _gname(market, g):
    g = g.strip("[]")
    if market == "us":
        g = re.sub(r"^(Biotechnology|Computer|Industrial|Medical|Electronic|Retail|Oil|Other)[:/]?\s*", "", g)
        return g[:24]
    return _short(g, 16)


def momentum_messages(market):
    """sepa momentum JSON → 기간별(1주/1개월/3개월) 섹터 메시지 3개 + 종목 메시지 3개. 데이터 근거만."""
    try:
        m = json.loads(run("momentum", market, "--json"))
    except Exception:
        return []
    f = FLAG[market]; out = []
    for h in ("1w", "1m", "3m"):
        hz = HORIZON[h]; k = RKEY[h]; d = m["by_horizon"][h]
        # --- 섹터 ---
        L = [f"📊 {f} {hz} 섹터 모멘텀 · {m['as_of']}", f"{hz} 수익률 순. 괄호는 1주/1개월/3개월, 통과는 상승 추세 종목 비율"]
        for i, a in enumerate(d["sectors"], 1):
            lead = " ".join(l["ticker"].split(".")[0] for l in a["leaders"])
            L.append(f"{i}. {_gname(market, a['group'])} {_pct(a[k])} ({_pct(a['r1w'])}/{_pct(a['r1m'])}/{_pct(a['r3m'])}) {a['n']}종·통과 {a['pass_rate']:.0%}" + (f"·고점권 {a['near_high']:.0%}" if a["near_high"] else "") + (f" · {lead}" if lead else ""))
        if not d["sectors"]:
            L.append("해당 없음 — 통과율 10% 또는 50일선 위 60%를 넘는 업종이 없다")
        L.append("읽는 법: 수익률은 그 업종 종목들의 중앙값(한 종목 급등에 안 끌려감) · 통과=상승 추세 조건 8개 전부 만족 · 고점권=52주 고점 5% 안. 업종 종목이 5개 넘고 통과율 10% 이상이거나 50일선 위가 60% 이상인 업종만 올림.")
        out.append("\n".join(L))
        # --- 종목 ---
        L = [f"📈 {f} {hz} 종목 모멘텀 · {m['as_of']}", f"{hz} 수익률 순. 상승 추세 조건을 전부 만족하고 거래대금이 충분한 종목만. 괄호는 1주/1개월/3개월"]
        for i, r in enumerate(d["stocks"], 1):
            t = r["ticker"].split(".")[0]
            nm = f" {r['name'][:8]}" if market == "kr" else ""
            ind = _gname(market, r["industry"] or r["sector"] or "")
            L.append(f"{i}. {t}{nm} {_pct(r[k])} ({_pct(r['r1w'])}/{_pct(r['r1m'])}/{_pct(r['r3m'])}) RS{r['rs']:.0f} 고점{r['hi']:+.0f}% {r['tag']}" + (f" {r['earn']}" if r["earn"] else "") + (f" · {ind}" if ind else ""))
        if not d["stocks"]:
            L.append("해당 없음")
        L.append("읽는 법: RS=최근 1년 상대강도(100이 최상) · 고점=52주 고점 대비 · 자리 표시 — 연장: 50일선보다 20% 넘게 위라 지금 사면 추격, 신고가권: 고점 3% 안(돌파 직후), 50일선 근처: 쉬어가는 재진입 자리, 베이스: 고점에서 내려와 조정 중")
        out.append("\n".join(L))
    return out


def main(market, dry=False):
    st = load_state(); ms = st.setdefault(market, {})
    d = json.loads(run("daily", market))
    as_of = d["as_of"]; b = d["breadth_today"]; p = d.get("breadth_prev") or {}; pt = d["pool_trend"]
    f = FLAG[market]
    msgs, alerts = [], []

    # ---- 1. 요약 ----
    x = [c for c in d["stage2_crossovers"]]
    x90 = [c for c in x if (c["rs"] or 0) >= 90]
    reg = json.loads((WS / f"registry_{market}.json").read_text()) if (WS / f"registry_{market}.json").exists() else {}
    zero = reg.get("0군", [])
    lines = [f"🧭 SEPA {f} {as_of} 수집 완료",
             f"추세 통과 종목 {d['pass_all_count']}개 (전일 대비 {d['pass_all_delta']:+d}) · 새로 통과 {d['new_entrants_count']} · 빠짐 {d['dropped_count']}",
             f"200일선 위 {p.get('pct_above_200ma','-')}→{b['pct_above_200ma']}% · 50일선 위 {b['pct_above_50ma']}% · 신고가−신저가 {b['net_new_highs']:+d}",
             f"조사 대상 {b['pool']}종 ({pt['flag']}) 1일 {pt['vs_1d_pct']:+.1f}% / 20일 {pt['vs_20d_pct']:+.1f}% · 데이터 누락 {pt['stale_excluded']}"]
    if x:
        lines.append("오늘 상승 추세에 막 올라탄 종목: " + ", ".join(f"{c['ticker'].split('.')[0]}(RS{c['rs']:.0f})" for c in x[:8]) + ("" if d["stage2_crossovers_reliable"] else " ⚠️판정불가"))
    else:
        lines.append("오늘 상승 추세에 막 올라탄 종목: 없음" if d["stage2_crossovers_reliable"] else f"막 올라탄 종목: 판정 불가 — {d['stage2_crossovers_note']}")

    # ---- 3. 0군 셋업 (vcp) ----
    zero_lines, prev_fail = [], set(ms.get("zero_fail", []))
    now_fail = set()
    for t in zero:
        try:
            v = json.loads(run("vcp", t))
        except Exception:
            continue
        pb = v["alt_setups"]["pullback_50ma"]; w = v["weekly"]
        cond = (pb["status"] == "at_50ma" and pb["sma50_rising"] and pb["had_run_60d"] and pb["touch_episodes_60d"] <= 2
                and (pb["pullback_downday_vol_over_v50"] or 9) < 1.0 and pb["closed_below_sma50_last5"] == 0)
        tag = "🟢50MA" if cond else ("🟡50MA근접" if pb["status"] == "approaching" else ("🔴50MA하회" if pb["status"] == "below_50ma" else ""))
        zero_lines.append(f"{t.split('.')[0]} 피벗{v['pct_to_pivot']:+.1f}% {w['weeks_elapsed']}주 {tag}".strip())
        key = f"{t}:{as_of}"
        if cond and ms.get("setup_" + t) != as_of:
            alerts.append(f"🟢 SEPA {f} 자리 신호 — {t}가 상승 중인 50일선까지 내려와 닿았다\n50일선 {pb['sma50']} · 지금 가격은 그 위 {pb['pct_vs_sma50']:+.1f}% · 최근 60일 안 이 자리에 온 게 {pb['touch_episodes_60d']}번째(1~2번째가 좋은 자리) · 내려올 때 거래량 평소의 {pb['pullback_downday_vol_over_v50']}배(1 미만이면 조용한 조정) · 반등 확인 {'됨' if pb['bounce_hint'] else '아직'}\n뜻: 달리던 종목이 쉬어가는 전형적 재진입 자리. 판정은 데일리 리포트에서.")
            ms["setup_" + t] = as_of
        if pb["status"] == "below_50ma":
            now_fail.add(t)
    for t in sorted(now_fail - prev_fail):
        alerts.append(f"🔴 SEPA {f} 후보 {t} — 종가가 50일선 아래로 내려갔다. 추세 종목이 50일선을 잃으면 셋업이 무효가 될 수 있어 데일리에서 확인한다.")
    ms["zero_fail"] = sorted(now_fail)
    if zero_lines:
        lines.append("지켜보는 후보(0군): " + " | ".join(zero_lines))
    lines.append("읽는 법: 추세 통과=상승 추세 조건 8개를 전부 만족 · 200일선 위 비율이 60% 넘으면 시장 우호, 40% 아래면 악화 · 신고가−신저가가 음수면 힘이 빠지는 중 · 후보 표기: 피벗=돌파 기준가(+면 아직 그 아래), n주=조정 기간, 🟢=50일선 재진입 자리 🟡=50일선에 접근 🔴=50일선 아래(위험)")
    msgs.append("\n".join(lines))
    msgs.extend(momentum_messages(market))

    # ---- 2. 수집 사고 ----
    if pt["flag"] == "수집의심" or (pt["stale_excluded"] or 0) / max(1, b["pool"] + (pt["stale_excluded"] or 0)) >= 0.03:
        alerts.append(f"⚠️ SEPA {f} 데이터 주의 — 조사 대상 종목 수가 하루 만에 {pt['vs_1d_pct']:+.1f}% 변했고 누락이 {pt['stale_excluded']}종이다. 시장이 아니라 수집 문제일 수 있으니 오늘 비율 수치는 어제와 비교하지 않는다.")

    # ---- 3b. Stage 2 전이 RS90+ ----
    if x90:
        alerts.append(f"🚀 SEPA {f} 강한 종목이 오늘 상승 추세에 올라탐 — " + ", ".join(f"{c['ticker'].split('.')[0]} {c['name'][:14]} (RS {c['rs']:.0f}, 고점 대비 {c['from_52w_high']:+.1f}%)" for c in x90) + "\nRS는 최근 1년 상대강도(100이 최상, 90 이상이면 상위 10%). 방금 추세가 시작된 종목이라 아직 살 자리는 아니고 지켜볼 명단에 올릴 재료.")

    # ---- 4. 그룹 게이트 변화 ----
    gg = run("groupgate", market, "--holdings")
    labels = {}
    for ln in gg.splitlines():
        m = re.match(r"\|\s*([^|]+?)\s*\|\s*(\d+)/(\d+)\s*\|[^|]*\|[^|]*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", ln)
        if m and m.group(1) not in ("그룹",):
            labels[m.group(1)] = {"pass": f"{m.group(2)}/{m.group(3)}", "entry": m.group(4), "warn": m.group(5)}
    prev = ms.get("groupgate", {})
    changed = []
    for g, v in labels.items():
        pv = prev.get(g)
        if pv and (pv["entry"] != v["entry"] or pv["warn"] != v["warn"]):
            changed.append(f"{g} {v['pass']}: 진입 {pv['entry']}→{v['entry']} · 경고 {pv['warn']}→{v['warn']}")
    if changed:
        alerts.append(f"🧩 SEPA {f} 업종 그룹 상태 변화\n" + "\n".join(changed) + "\n읽는 법: ✅열림=그 업종 종목 대부분이 추세를 타고 고점 근처라 시장이 나빠도 소액 진입 허용 / 🟠🔴=그 업종이 무너지는 중이라 보유 종목 손절선을 올릴 때")
    ms["groupgate"] = labels
    ms["last_as_of"] = as_of

    out = msgs + alerts
    for m in out:
        if dry:
            print("---\n" + m)
        else:
            tg.telegram(m)
    if not dry:
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    print(f"[sepa_notify] {market} {as_of}: 요약 1 + 알림 {len(alerts)}건 {'(dry)' if dry else '전송'}")


if __name__ == "__main__":
    if "--send-file" in sys.argv:            # 아침 브리핑이 쓴 텍스트(섹터 전망 등)를 그대로 발송
        fp = Path(sys.argv[sys.argv.index("--send-file") + 1])
        txt = fp.read_text().strip()
        for chunk in [txt[i:i + 3800] for i in range(0, len(txt), 3800)]:   # 텔레그램 4096자 제한
            tg.telegram(chunk)
        print(f"[sepa_notify] 파일 발송 {fp} ({len(txt)}자)"); sys.exit(0)
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if not a or a[0] not in ("us", "kr"):
        sys.exit("사용: sepa_notify.py us|kr [--dry] [--momentum-only] | --send-file <파일>")
    if "--momentum-only" in sys.argv:          # 모멘텀 보고만 (알림 상태 건드리지 않음)
        for mm in momentum_messages(a[0]):
            print("---\n" + mm) if "--dry" in sys.argv else tg.telegram(mm)
        sys.exit(0)
    main(a[0], dry="--dry" in sys.argv)
