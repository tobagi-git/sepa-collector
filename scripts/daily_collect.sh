#!/bin/zsh
# minervini 데일리 수집기 — fetch(증분) → screen → 브레드스 축적. Claude를 거치지 않으므로 토큰 0.
#
# launchd 두 잡이 시장별로 호출한다 (2026-07-25 분리):
#   com.ruby.minervini-us — 06:00 KST 화~토  (= 17:00 ET 월~금, 미국 마감 16:00 ET +1h)
#   com.ruby.minervini-kr — 16:40 KST 월~금  (한국 마감 15:30 KST +70분)
#
# ⚠️ 미국이 "화~토"인 이유: 미국 세션은 각각 다음날 KST 새벽에 끝난다.
#    금요일장(예: 7/24)은 토요일 05:00 KST 마감 → 토요일에 돌지 않으면 금요일 종가가
#    월요일 아침까지 공백으로 남는다. 구 통합 잡의 `dow > 5` 주말 스킵이 정확히
#    이 구멍을 만들었다 (2026-07-25 실측: 데일리 미국 절이 as-of 07-23으로 하루 낡음).
#
# 인자: us | kr | (없으면 둘 다 — 수동 실행용. 스킬 문서가 인자 없이 호출한다)

set -u
WS="$HOME/.claude/skills/minervini-workspace"
SEPA_DIR="$HOME/.claude/skills/minervini/scripts"
SEPA="$SEPA_DIR/sepa.py"
LOG="$WS/daily.log"

mkdir -p "$WS"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

dow=$(date +%u)          # 1=월 … 7=일
today=$(date +%Y-%m-%d)

# 시장별 정기 수집일 (KST 기준)
should_run() {
  case "$1" in
    us) [[ "$dow" -ge 2 && "$dow" -le 6 ]] ;;   # 화~토
    kr) [[ "$dow" -le 5 ]] ;;                    # 월~금
    *)  return 1 ;;
  esac
}

collect() {
  local mkt="$1"
  local stamp="$WS/.daily_last_session_$mkt"

  should_run "$mkt" || { log "$mkt 스킵 (정기 수집일 아님, dow=$dow)"; return 0; }

  # 목표 세션 = sepa.py last_expected_session (단일 진실원, 시장 현지 마감+30분 기준)
  local target
  target=$(python3 -c "
import sys; sys.path.insert(0, '$SEPA_DIR')
import sepa; print(sepa.last_expected_session('$mkt'))" 2>>"$LOG")
  if [[ -z "$target" ]]; then
    log "⚠️ $mkt target 계산 실패 — 스킵"; return 0
  fi

  # 스로틀: 그 세션을 이미 받아뒀으면 스킵.
  # ⚠️ 실행 '달력일'이 아니라 '세션일'로 재는 것이 핵심 — 달력일로 재면 아침 RunAtLoad가
  #    스탬프를 찍어 당일 16:40 한국 종가 수집을 통째로 막는다(구 로직이 "아침 실행 후
  #    장 마감 재실행 허용"으로 처리하던 케이스). 세션일 기준이면 아침엔 target=전일이라
  #    받고, 16:40엔 target=당일로 바뀌어 다시 받는다.
  if [[ -f "$stamp" ]]; then
    local got_prev="$(cat "$stamp")"
    [[ "$got_prev" == "$target" || "$got_prev" > "$target" ]] && return 0
  fi

  # 겹침 가드 (2026-09-22): 다른 시장의 fetch가 아직 돌고 있으면 기다린다. US·KR을 동시에
  # 돌리면 Yahoo 백오프가 걸려 둘 다 늘어지고 세션봉 결측이 는다 (09-17 실측: US 06:08→22:08
  # 16시간, 그 위에 KR 16:40 겹쳐 KR 09-18 브레드스가 당일 리포트에 못 들어감).
  local other; [[ "$mkt" == "us" ]] && other="kr" || other="us"
  local waited=0
  while pgrep -f "sepa.py fetch $other" >/dev/null 2>&1; do
    (( waited == 0 )) && log "⏸ $mkt 대기 — $other fetch 진행 중 (5분 간격 재확인, 최대 6시간)"
    sleep 300; waited=$((waited+300))
    if (( waited >= 21600 )); then log "⚠️ $mkt 6시간 대기 초과 — $other fetch 여전히 진행, 이번 트리거 포기(스탬프 미기록, 다음 트리거 재시도)"; return 0; fi
  done
  (( waited > 0 )) && log "▶ $mkt 대기 종료 ($((waited/60))분) — 수집 시작"
  log "=== $mkt collect 시작 (target=$target) ==="
  # 월 1회(1일) 유니버스 재구축 — 신규 상장 반영
  if [[ "$(date +%d)" == "01" || ! -f "$WS/universe/$mkt.tsv" ]]; then
    python3 "$SEPA" universe "$mkt" >> "$LOG" 2>&1
    python3 "$SEPA" sectors "$mkt" >> "$LOG" 2>&1 || log "⚠️ $mkt sectors 재구축 실패(기존 매핑 유지)"
  fi
  if python3 "$SEPA" fetch "$mkt" --threads 6 >> "$LOG" 2>&1 \
     && python3 "$SEPA" screen "$mkt" >> "$LOG" 2>&1; then
    # 실제로 받은 세션일을 기록 (target이 아니라 산출물의 as_of — 휴장일이면 둘이 다르다)
    local got
    got=$(python3 -c "
import json
rows=[json.loads(l) for l in open('$WS/out/breadth_$mkt.jsonl') if l.strip()]
print(rows[-1]['as_of'])" 2>>"$LOG")
    echo "${got:-$target}" > "$stamp"
    log "=== $mkt collect 완료 (as_of=${got:-$target}) ==="
    # 텔레그램 알림 (2026-09-22): 수집 완료 요약 + 조건부 알림(수집 사고·0군 셋업·Stage2 전이·그룹 게이트 변화)
    python3 "$WS/alerts/sepa_notify.py" "$mkt" >> "$LOG" 2>&1 || log "⚠️ $mkt sepa_notify 실패(알림 생략)"
    [[ -n "$got" && "$got" != "$target" ]] && log "ℹ️ $mkt as_of($got) ≠ target($target) — 휴장 가능성"

    # 수집 건강성 — 이게 없어서 2026-07-27 fail=308(전량의 12%)이 "완료" 한 줄에 묻혔고,
    # 풀이 1,090 → 957로 꺼진 걸 데일리에서야 발견했다. 모수 급감은 브레드스 추이를
    # 통째로 무의미하게 만들므로 로그에서 먼저 보이게 한다.
    python3 - "$WS" "$mkt" >> "$LOG" 2>&1 <<'PYEOF'
import json, sys, glob, os
ws, mkt = sys.argv[1], sys.argv[2]
rows = [json.loads(l) for l in open(f"{ws}/out/breadth_{mkt}.jsonl") if l.strip()]
cur = rows[-1]
prev = rows[-2] if len(rows) > 1 else None
pool, pp = cur.get("pool"), (prev or {}).get("pool")
msgs = []
if pp and pool:
    drop = (pp - pool) / pp * 100
    if drop >= 5:
        msgs.append(f"⚠️ {mkt} 모수 급감 {pp} → {pool} ({drop:.1f}%↓) — 브레드스 % 전일 대비 비교 금지")
scr = sorted(glob.glob(f"{ws}/out/screen_{mkt}_*.json"), key=os.path.getmtime)
if scr:
    s = json.load(open(scr[-1]))
    st = s.get("stale_excluded") or 0
    if st:
        # 분모는 유니버스가 아니라 '유동성 통과 모수 + 제외분' — 실제로 잃은 비율이 이것이다
        pct = st / max(1, s.get("with_data", 0) + st) * 100
        tag = "⚠️" if pct >= 3 else "ℹ️"
        msgs.append(f"{tag} {mkt} 이번 세션 봉 없어 제외 {st}종목 ({pct:.1f}%) — 이력은 보존됨")
n_err = sum(1 for f in glob.glob(f"{ws}/data/{mkt}/*.json")
            if '"error"' in open(f).read(400))
if n_err:
    msgs.append(f"ℹ️ {mkt} 데이터 없는 종목 {n_err} (상장폐지·신규상장 포함)")
for m in msgs:
    print(m)
PYEOF
  else
    # 스탬프를 남기지 않아 다음 트리거(RunAtLoad 포함)에 재시도된다
    log "⚠️ $mkt 수집/스크리닝 실패 — 스탬프 미기록, 다음 트리거에 재시도"
  fi
}

markets=("$@")
(( ${#markets[@]} == 0 )) && markets=(us kr)
for m in "${markets[@]}"; do collect "$m"; done

# 30일 넘은 일별 스크리닝 스냅샷 정리 (브레드스 이력은 out/breadth_*.jsonl에 영구 보존)
find "$WS/out" -name "screen_*_*.csv"  -mtime +30 -delete 2>/dev/null
find "$WS/out" -name "screen_*_*.json" -mtime +30 -delete 2>/dev/null
