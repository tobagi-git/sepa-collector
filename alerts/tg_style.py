"""미너비니 텔레그램 표시 전용 (2026-09-25) — 모멘텀 모니터 tg_format.py와 같은 원칙.

한 건 = 굵은 첫 줄(무엇이 얼마) + 들여쓴 둘째 줄(맥락). 메시지 첫머리에 이 메시지가 무슨 뜻인지 한 줄,
끝에 그 메시지에 필요한 용어만 풀이. 영문 업종은 짧은 한국어로. 계산·판정 로직은 건드리지 않는다.
IND_KO는 ~/Claude/PP/모멘텀 모니터/tg_format.py의 복사본 — 저장소가 달라(GitHub Actions) 따로 둔다.
"""
import html
import re

E = html.escape

IND_KO = {
    "Biotechnology: Pharmaceutical Preparations": "바이오·신약",
    "Biotechnology: Biological Products (No Diagnostic Substances)": "바이오·생물의약품",
    "Biotechnology: In Vitro & In Vivo Diagnostic Substances": "진단",
    "Biotechnology: Laboratory Analytical Instruments": "분석장비",
    "Biotechnology: Electromedical & Electrotherapeutic Apparatus": "전자 의료기기",
    "Biotechnology: Commercial Physical & Biological Resarch": "위탁연구(CRO)",
    "Medical/Dental Instruments": "의료기기",
    "Medical Specialities": "특수 의료",
    "Medical/Nursing Services": "의료 서비스",
    "Hospital/Nursing Management": "병원 운영",
    "Other Pharmaceuticals": "제약",
    "Major Banks": "대형 은행", "Commercial Banks": "상업 은행", "Banks": "은행",
    "Savings Institutions": "저축은행",
    "Computer Software: Prepackaged Software": "소프트웨어",
    "Computer Software: Programming Data Processing": "소프트웨어·데이터",
    "EDP Services": "IT 서비스",
    "Computer Manufacturing": "컴퓨터 제조",
    "Computer peripheral equipment": "컴퓨터 주변기기·보안장비",
    "Computer Communications Equipment": "네트워크 장비",
    "Telecommunications Equipment": "통신장비",
    "Semiconductors": "반도체", "Electronic Components": "전자부품",
    "Electrical Products": "전기장비",
    "Industrial Machinery/Components": "산업기계",
    "Real Estate Investment Trusts": "리츠", "Real Estate": "부동산",
    "Finance: Consumer Services": "소비자 금융", "Finance Companies": "금융",
    "Finance/Investors Services": "금융 서비스",
    "Investment Managers": "자산운용", "Investment Bankers/Brokers/Service": "증권",
    "Property-Casualty Insurers": "손해보험", "Life Insurance": "생명보험",
    "Specialty Insurers": "특수보험",
    "Oil & Gas Production": "석유·가스 생산", "Integrated oil Companies": "종합 석유",
    "Oil Refining/Marketing": "정유", "Oilfield Services/Equipment": "유전 서비스",
    "Oil/Gas Transmission": "파이프라인", "Oil and Gas Field Machinery": "유전 장비",
    "Marine Transportation": "해운", "Air Freight/Delivery Services": "항공 화물",
    "Trucking Freight/Courier Services": "트럭 운송", "Railroads": "철도",
    "Integrated Freight & Logistics": "물류",
    "Electric Utilities: Central": "전력", "Power Generation": "발전",
    "Natural Gas Distribution": "가스", "Water Supply": "수도",
    "Major Chemicals": "화학", "Specialty Chemicals": "특수화학",
    "Agricultural Chemicals": "비료·농화학",
    "Precious Metals": "귀금속·금광", "Metal Mining": "금속 광업",
    "Steel/Iron Ore": "철강", "Other Metals and Minerals": "기타 광물",
    "Coal Mining": "석탄",
    "Aerospace": "항공우주", "Military/Government/Technical": "방산",
    "Ordnance And Accessories": "방산·탄약",
    "Restaurants": "외식", "Hotels/Resorts": "호텔·리조트",
    "Packaged Foods": "가공식품", "Beverages (Production/Distribution)": "음료",
    "Package Goods/Cosmetics": "생활용품·화장품",
    "Other Specialty Stores": "전문 소매", "Department/Specialty Retail Stores": "백화점·소매",
    "Clothing/Shoe/Accessory Stores": "의류 소매", "Apparel": "의류",
    "Auto Manufacturing": "자동차", "Auto Parts:O.E.M.": "자동차 부품",
    "Homebuilding": "주택건설", "Engineering & Construction": "건설·엔지니어링",
    "Building Products": "건자재", "RETAIL: Building Materials": "건자재 소매",
    "Business Services": "기업 서비스", "Professional Services": "전문 서비스",
    "Diversified Commercial Services": "상업 서비스",
    "Advertising": "광고", "Broadcasting": "방송",
    "Movies/Entertainment": "영화·엔터", "Cable & Other Pay Television Services": "유료방송",
    "Services-Misc. Amusement & Recreation": "레저",
    "Recreational Games/Products/Toys": "게임·완구",
    "Consumer Electronics/Appliances": "가전",
    "Construction/Ag Equipment/Trucks": "건설·농기계",
    "Containers/Packaging": "포장재", "Metal Fabrications": "금속가공",
    "Industrial Specialties": "산업 소재",
    "Environmental Services": "환경 서비스",
    "Plastic Products": "플라스틱", "Fluid Controls": "유체제어", "Newspapers/Magazines": "신문·잡지",
    "Specialty Foods": "특수식품", "Food Distributors": "식품 유통", "Food Chains": "식품 소매",
    "Retail: Computer Software & Peripheral Equipment": "IT 유통", "Office Equipment/Supplies/Services": "사무용품",
    "Paper": "제지", "Forest Products": "목재", "Textiles": "섬유", "Publishing": "출판",
    "Water Sewer Pipeline Comm & Power Line Construction": "전력망·배관 공사", "Multi-Sector Companies": "복합기업",
    "Miscellaneous manufacturing industries": "기타 제조", "Ophthalmic Goods": "안과용품",
    "Misc Health and Biotechnology Services": "헬스케어 서비스", "Motor Vehicles": "자동차",
    "Automotive Aftermarket": "자동차 애프터마켓", "Rental/Leasing Companies": "렌탈·리스",
    "Home Furnishings": "가구", "Trusts Except Educational Religious and Charitable": "신탁",
    "Blank Checks": "스팩", "Transportation Services": "운송 서비스", "Farming/Seeds/Milling": "농업",
    "Mining & Quarrying of Nonmetallic Minerals (No Fuels)": "비금속 광업", "Building Materials": "건자재",
    "Oil Refining/Marketing": "정유", "Medical Electronics": "의료 전자",
    "Radio And Television Broadcasting And Communications Equipment": "방송·통신장비",
    "Retail-Auto Dealers and Gas Stations": "자동차 딜러·주유소", "Consumer Specialties": "소비재",
    "Other Consumer Services": "소비자 서비스", "Catalog/Specialty Distribution": "전문 유통",
}


def industry(market, name):
    name = (name or "").strip().strip("[]")
    if market == "us":
        if name in IND_KO:
            return IND_KO[name]
        return re.sub(r"^(Biotechnology|Computer Software|Computer)[:]?\s*", "", name)[:22]
    name = re.sub(r"\s*(제조업|도매업|공급업|서비스업|건조업|건설업)$", "", name)
    return name.split(",")[0][:14]


def pct(x, sign=True):
    return "-" if x is None else (f"{x:+.0f}%" if sign else f"{x:.0f}%")


def glossary(text):
    return f"<i>📖 {E(text)}</i>"


TAG_KO = {"연장": "연장 · 추격 금지", "신고가권": "신고가권 · 돌파 직후", "50일선 근처": "50일선 근처 · 재진입 자리", "베이스": "베이스 · 조정 중"}
TAG_ICON = {"연장": "🔸", "신고가권": "🔹", "50일선 근처": "🟢", "베이스": "⚪"}


def breadth_word(pct200):
    if pct200 is None:
        return "?"
    return "우호" if pct200 > 60 else ("악화" if pct200 < 40 else "중립")


def short_name(name, n=16):
    """'Everpure, Inc. Class A common stock' → 'Everpure'."""
    name = re.split(r",| - |\s+(Inc|Corp|Corporation|Holdings|Ltd|Limited|plc|PLC|N\.V\.|S\.A\.|Co\.|Company|Group|Class|Common|Ordinary)\b", name or "")[0]
    return name.strip()[:n]
