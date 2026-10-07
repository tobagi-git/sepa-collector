#!/usr/bin/env python3
"""코스피·코스닥 투자자별 순매수(당일, 억원)를 네이버 모바일 API에서 받아 out/kr_flows.json에 쌓는다.
네이버는 '오늘' 값만 준다 → kr 수집 런(장 마감 뒤)에서 매일 돌아야 이력이 생긴다. 같은 날짜는 덮어쓴다.
맥의 money-flow mf_collect.py와 같은 소스·형식(루틴 보드 클라우드 갱신용 사본)."""
import json, subprocess, os
from pathlib import Path

WS = Path(os.environ.get("SEPA_WS", str(Path.home() / ".claude/skills/minervini-workspace")))
LEDGER = WS / "out" / "kr_flows.json"
KEYS = {"personalValue": "개인", "foreignValue": "외국인", "institutionalValue": "기관"}


def fetch(idx):
    r = subprocess.run(["curl", "-s", "-m", "20", "-A", "Mozilla/5.0", "-e", "https://m.stock.naver.com/",
                        f"https://m.stock.naver.com/api/index/{idx}/trend"], capture_output=True)
    d = json.loads(r.stdout.decode("utf-8"))
    return d.get("bizdate"), {v: int(str(d.get(k, "0")).replace(",", "").replace("+", "")) for k, v in KEYS.items()}


led = json.loads(LEDGER.read_text()) if LEDGER.exists() else {}
for idx in ("KOSPI", "KOSDAQ"):
    try:
        day, vals = fetch(idx)
        if day:
            led.setdefault(idx, {})[day] = vals
            print(idx, day, vals)
    except Exception as e:
        print(idx, "실패", e)
LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=1))
