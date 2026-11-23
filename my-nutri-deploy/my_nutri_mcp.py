import json
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from functools import lru_cache

import requests
from fastmcp import FastMCP

_SERVER_RULES = """My-Nutri-MCP 결과를 사용자에게 전달할 때의 규칙:
1. 도구 결과에 있는 내용만 전달하세요. 도구 결과에 없는 '일반 지식', 추정, 일반적인 건강 조언, 다른 출처의 수치를 덧붙이지 마세요. '도구 결과 밖의 일반 지식' 같은 별도 문단도 쓰지 마세요.
2. 결론을 단정하지 마세요. '삭제해야 합니다', '안전합니다', '문제없습니다' 대신 '수정을 검토해 보세요', '이 데이터에서는 확인되지 않았습니다'처럼 도구가 낸 판정의 범위 안에서만 말하세요. 법 위반·의학적 진단·복용 지시를 하지 마세요.
3. 표·막대(█░)·이모지 판정(🔴🟡🟢)은 도구가 낸 그대로 보여주고, 마지막 ```json 블록은 출력하지 마세요.
4. 도구가 '확인하지 못함', '검색되지 않음'이라고 한 항목을 '안전함'으로 바꿔 말하지 마세요. 출처 서비스 ID와 결과 끝의 안내 문구는 유지하세요.
5. 계산(합계·비율)을 직접 하지 말고 도구가 낸 숫자를 그대로 쓰세요."""
mcp = FastMCP("My-Nutri-MCP", instructions=_SERVER_RULES)

# =====================================================================
# MCP 앱 (선택 기능): 결과가 나오면 채팅 화면 안에 막대 차트·위험도 카드가 자동으로 표시됩니다.
#  - 지원하는 화면(Claude 앱/claude.ai 등)에서만 보이고, 지원하지 않는 화면에서는 기존처럼 글자 표만 나옵니다.
#  - fastmcp 에 앱 기능이 없으면 이 블록은 조용히 꺼지고 서버는 그대로 동작합니다.
#  - 도구 결과 텍스트 끝의 ```json 블록을 읽어 그립니다 (full_safety_check / check_nutrient_adequacy).
# =====================================================================
VIEW_URI = "ui://my-nutri-mcp/result.html"
try:
    from fastmcp.apps import AppConfig, ResourceCSP
    _APPS_OK = True
except Exception:   # 앱 기능이 없는 fastmcp 버전
    _APPS_OK = False

_VIEW_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="color-scheme" content="light dark">
<style>
  :root { --bg:#ffffff; --fg:#1c1c1e; --muted:#6b6b70; --track:#ececf0; --line:#1c1c1e; --card:#f6f6f8;
          --red:#d64545; --amber:#e0a02b; --green:#2e9e6a; --blue:#4a7fd4; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#1c1c1e; --fg:#f2f2f5; --muted:#9a9aa2; --track:#35353a; --line:#f2f2f5; --card:#27272b;
            --red:#ef6b6b; --amber:#f0b84a; --green:#46c08a; --blue:#6c9be8; }
  }
  body { margin:0; padding:14px; background:transparent; color:var(--fg);
         font:14px/1.45 -apple-system, "Segoe UI", "Malgun Gothic", sans-serif; }
  h2 { font-size:15px; margin:0 0 8px; }
  .badge { display:inline-block; padding:6px 12px; border-radius:999px; font-weight:600; color:#fff; margin-bottom:12px; }
  .red{background:var(--red)} .yellow{background:var(--amber)} .green{background:var(--green)}
  .card { background:var(--card); border-radius:10px; padding:12px 14px; margin-bottom:12px; }
  .find { display:flex; gap:8px; margin:4px 0; }
  .dot { flex:none; width:10px; height:10px; border-radius:50%; margin-top:5px; }
  .dot.info { background:var(--blue); }
  .row { display:grid; grid-template-columns:96px 1fr 150px; align-items:center; gap:8px; margin:7px 0; }
  .name { font-weight:500; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .track { position:relative; height:14px; background:var(--track); border-radius:7px; }
  .fill { position:absolute; left:0; top:0; bottom:0; border-radius:7px; }
  .fill.second { opacity:0.35; }
  .mark { position:absolute; top:-3px; bottom:-3px; width:2px; background:var(--line); opacity:0.7; }
  .val { text-align:right; font-variant-numeric:tabular-nums; color:var(--muted); font-size:12px; }
  .legend { color:var(--muted); font-size:12px; margin-top:6px; }
  .note { color:var(--muted); font-size:12px; margin-top:10px; }
</style>
</head>
<body>
<div id="root"></div>
<script type="module">
import { App } from "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps";

const root = document.getElementById("root");
const COLORS = { over:"var(--red)", low:"var(--red)", possible:"var(--amber)", below_rni:"var(--amber)", below_ai:"var(--amber)",
                  ok:"var(--green)", enough:"var(--green)", supp_only:"var(--blue)", none:"var(--blue)", no_ul:"var(--blue)" };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function fmt(mg) {
  if (mg === null || mg === undefined) return "-";
  if (mg < 1) return (+(mg * 1000).toPrecision(3)) + "μg";
  return (+mg.toPrecision(4)) + "mg";
}
function axisMax(values) {
  const m = Math.max(...values.filter(v => v !== null && v !== undefined), 100);
  return Math.max(150, Math.min(300, Math.ceil(m / 50) * 50));
}
function barRow(name, pct, color, valueText, pct2) {
  const max = barRow.max;
  const row = el("div", "row");
  row.appendChild(el("div", "name", name)).title = name;
  const track = el("div", "track");
  if (pct2 !== undefined && pct2 !== null) {
    const f2 = el("div", "fill second"); f2.style.width = Math.min(100, pct2 / max * 100) + "%"; f2.style.background = color; track.appendChild(f2);
  }
  const f = el("div", "fill"); f.style.width = Math.min(100, pct / max * 100) + "%"; f.style.background = color;
  if (pct2 !== undefined && pct2 !== null) { f.style.height = "50%"; f.style.top = "25%"; f.style.bottom = "auto"; }
  track.appendChild(f);
  const mark = el("div", "mark"); mark.style.left = (100 / max * 100) + "%"; track.appendChild(mark);
  row.appendChild(track);
  row.appendChild(el("div", "val", valueText));
  return row;
}

function renderFull(d) {
  const labels = { red:"🔴 주의가 필요한 항목이 있습니다", yellow:"🟡 확인이 필요한 항목이 있습니다", green:"🟢 확인된 문제는 없습니다 (안전 보장은 아님)" };
  root.appendChild(el("div", "badge " + d.overall, labels[d.overall] || d.overall));
  const important = (d.findings || []).filter(f => f.level !== "info");
  if (important.length) {
    const card = el("div", "card"); card.appendChild(el("h2", "", "점검 결과"));
    important.forEach(f => {
      const line = el("div", "find");
      const dot = el("span", "dot"); dot.style.background = f.level === "red" ? "var(--red)" : "var(--amber)";
      line.appendChild(dot); line.appendChild(el("div", "", "[" + f.category + "] " + f.title)); card.appendChild(line);
    });
    root.appendChild(card);
  }
  const rows = (d.intake || []).filter(r => r.pct_max !== null && r.pct_max !== undefined);
  if (rows.length) {
    const card = el("div", "card"); card.appendChild(el("h2", "", "성분별 하루 섭취량 (상한섭취량 UL 대비)"));
    barRow.max = axisMax(rows.map(r => r.pct_max));
    rows.forEach(r => card.appendChild(barRow(r.name, r.pct_label, COLORS[r.level] || COLORS.none,
      Math.round(r.pct_label) + "% (최대 " + Math.round(r.pct_max) + "%)", r.pct_max)));
    card.appendChild(el("div", "legend", "세로선 = UL 100%. 진한 막대 = 표시량, 연한 막대 = 최대 추정치"));
    root.appendChild(card);
  }
  root.appendChild(el("div", "note", "공공 데이터 기반 참고 자료이며 의학적 판단을 대신하지 않습니다. 복용 약·질환이 있으면 의사·약사와 확인하세요."));
}

function renderAdequacy(d) {
  const rows = d.nutrients || [];
  const card = el("div", "card");
  card.appendChild(el("h2", "", "권장·충분섭취량 대비 (" + (d.profile.sex === "M" ? "남" : "여") + " " + d.profile.age + "세)"));
  barRow.max = axisMax(rows.map(r => d.used_diet ? r.pct_total : r.pct_supp));
  const names = { enough:"충족", below_rni:"권장 미달", below_ai:"충분섭취량 미달", low:"평균필요량 미만", supp_only:"" };
  rows.forEach(r => {
    const main = d.used_diet && r.diet_mg !== null ? r.pct_total : r.pct_supp;
    const text = Math.round(main) + "%" + (names[r.status] ? " " + names[r.status] : "");
    card.appendChild(barRow(r.name, main, COLORS[r.status] || COLORS.none, text, d.used_diet && r.diet_mg !== null ? r.pct_supp : null));
  });
  card.appendChild(el("div", "legend", "세로선 = 기준(권장 또는 충분섭취량) 100%" + (d.used_diet ? ". 진한 막대 = 식사+영양제, 연한 막대 = 영양제만" : ". 막대 = 영양제가 채우는 비율(식사량 미반영)")));
  root.appendChild(card);
  if (d.missing && d.missing.length) root.appendChild(el("div", "note", "이 조합에 없는 영양소: " + d.missing.join(", ") + " (식사로 충분하면 문제가 아닙니다)"));
}

function renderEffective(d) {
  const names = { under:"하한 미달", ok:"인정 범위", over:"상한 초과", ref_under:"참고: 하한 미달", ref_ok:"참고: 범위 내", ref_over:"참고: 상한 초과" };
  const col = { under:"var(--red)", over:"var(--amber)", ok:"var(--green)", ref_under:"var(--blue)", ref_ok:"var(--blue)", ref_over:"var(--blue)" };
  d.effective.forEach(p => {
    const card = el("div", "card");
    card.appendChild(el("h2", "", p.product + " (1일 ×" + p.units_per_day + ")"));
    p.rows.forEach(r => {
      const top = r.up_mg !== null && r.up_mg !== undefined ? r.up_mg : (r.low_mg || r.day_mg) * 1.5;
      const max = Math.max(top, r.day_mg) * 1.25;
      const row = el("div", "row");
      row.appendChild(el("div", "name", r.name)).title = r.name;
      const track = el("div", "track");
      const lo = r.low_mg || 0, hi = r.up_mg !== null && r.up_mg !== undefined ? r.up_mg : max;
      const band = el("div", "fill"); band.style.left = (lo / max * 100) + "%"; band.style.width = Math.max(1, (hi - lo) / max * 100) + "%";
      band.style.background = "var(--green)"; band.style.opacity = "0.25"; band.style.borderRadius = "0"; track.appendChild(band);
      const f = el("div", "fill"); f.style.width = Math.min(100, r.day_mg / max * 100) + "%"; f.style.background = col[r.status] || "var(--blue)";
      f.style.height = "50%"; f.style.top = "25%"; f.style.bottom = "auto"; track.appendChild(f);
      row.appendChild(track);
      row.appendChild(el("div", "val", fmt(r.day_mg) + " · " + (names[r.status] || "")));
      card.appendChild(row);
    });
    if (!p.rows.length) card.appendChild(el("div", "note", "비교할 수 있는 인정 기준이 없습니다."));
    card.appendChild(el("div", "legend", "연한 초록 띠 = 식약처 인정 1일 섭취량(하한~상한), 막대 = 이 제품의 1일 함량"));
    root.appendChild(card);
  });
  root.appendChild(el("div", "note", "'미달'은 인정 기준보다 적다는 뜻이며 효과가 없다는 판정이 아닙니다."));
}

function renderOverseas(d) {
  const o = d.overseas;
  const labels = { red:"🔴 차단 목록에 일치 제품이 있습니다", yellow:"🟡 확인이 필요한 일치 항목이 있습니다", green:"🟢 일치 항목 없음 (안전 보장은 아님)" };
  root.appendChild(el("div", "badge " + o.level, labels[o.level]));
  const card = el("div", "card"); card.appendChild(el("h2", "", "해외직구 차단정보 대조"));
  (o.product_hits || []).forEach(h => {
    const line = el("div", "find"); const dot = el("span", "dot"); dot.style.background = h.active ? "var(--red)" : "var(--blue)";
    line.appendChild(dot); line.appendChild(el("div", "", h.product + " (" + (h.country || "-") + ") · " + (h.match === "barcode" ? "바코드" : "제품명") + " 일치 · " + (h.active ? "차단 중" : "기간 종료")));
    card.appendChild(line);
  });
  (o.ingredient_hits || []).forEach(h => {
    const line = el("div", "find"); const dot = el("span", "dot"); dot.style.background = "var(--amber)";
    line.appendChild(dot); line.appendChild(el("div", "", h.term + ": 차단 제품 " + h.count + "건에 사용 (현재 " + h.active_count + "건 차단 중)")); card.appendChild(line);
  });
  if (!(o.product_hits || []).length && !(o.ingredient_hits || []).length) card.appendChild(el("div", "", "일치하는 항목이 없습니다."));
  root.appendChild(card);
  root.appendChild(el("div", "note", "출처: 식약처 해외직구 위해식품 차단정보(I2715). 목록에 없다고 안전하다는 뜻은 아닙니다."));
}

function renderAd(d) {
  const a = d.ad; const card = el("div", "card");
  card.appendChild(el("h2", "", "광고 문구 점검" + (a.product ? " · " + a.product : "")));
  const c = a.counts || {};
  card.appendChild(el("div", "legend", "❌ " + (c.bad || 0) + "  ⚠️ " + (c.warn || 0) + "  ✅ " + (c.ok || 0) + "  ℹ️ " + (c.info || 0)));
  const col = { bad:"var(--red)", warn:"var(--amber)", ok:"var(--green)", info:"var(--blue)" };
  (a.claims || []).forEach(r => {
    const line = el("div", "find"); const dot = el("span", "dot"); dot.style.background = col[r.level] || col.info;
    const box = el("div", ""); box.appendChild(el("div", "", "“" + r.text + "”"));
    (r.reasons || []).forEach(x => box.appendChild(el("div", "legend", x)));
    line.appendChild(dot); line.appendChild(box); card.appendChild(line);
  });
  root.appendChild(card);
  root.appendChild(el("div", "note", "법 위반 판정이 아니라 공식 인정 내용과 다르게 읽힐 수 있는 표현을 알려 주는 참고 자료입니다."));
}

function renderSpend(d) {
  const s = d.spend; const card = el("div", "card"); card.appendChild(el("h2", "", "월 비용 중 중복 추정"));
  const maxv = Math.max(...s.items.map(i => i.monthly || 0), 1);
  s.items.forEach(i => {
    const row = el("div", "row"); row.appendChild(el("div", "name", i.name)).title = i.name;
    const track = el("div", "track");
    const f = el("div", "fill"); f.style.width = ((i.monthly || 0) / maxv * 100) + "%"; f.style.background = "var(--blue)"; f.style.opacity = "0.35"; track.appendChild(f);
    const w = el("div", "fill"); w.style.width = ((i.waste || 0) / maxv * 100) + "%"; w.style.background = "var(--red)"; track.appendChild(w);
    row.appendChild(track); row.appendChild(el("div", "val", (i.monthly ? i.monthly.toLocaleString() : "-") + "원 · 중복 " + i.ratio + "%"));
    card.appendChild(row);
  });
  card.appendChild(el("div", "legend", "연한 막대 = 월 비용, 붉은 막대 = 중복으로 본 금액(추정). 합계 " + s.total.toLocaleString() + "원 중 약 " + s.waste.toLocaleString() + "원"));
  root.appendChild(card);
  root.appendChild(el("div", "note", "겹침이 곧 낭비는 아니며, 금액은 성분 개수 비율 기준의 단순 추정입니다."));
}

function render(content) {
  const text = (content || []).filter(c => c.type === "text").map(c => c.text).join("\n");
  const m = text.match(/```json\s*([\s\S]*?)```/);
  if (!m) return;
  let d;
  try { d = JSON.parse(m[1]); } catch (e) { return; }
  root.replaceChildren();
  if (d.intake !== undefined && d.overall) renderFull(d);
  else if (d.nutrients) renderAdequacy(d);
  else if (d.overseas) renderOverseas(d);
  else if (d.spend) renderSpend(d);
  else if (d.ad) renderAd(d);
  else if (d.effective) renderEffective(d);
}

const app = new App({ name: "My-Nutri-MCP 결과", version: "1.0.0" });
app.ontoolresult = ({ content }) => render(content);
await app.connect();
</script>
</body>
</html>"""

if _APPS_OK:
    try:
        @mcp.resource(VIEW_URI, app=AppConfig(csp=ResourceCSP(resource_domains=["https://unpkg.com"])))
        def nutri_result_view() -> str:
            """영양제 점검 결과 차트 화면"""
            return _VIEW_HTML
    except Exception:
        _APPS_OK = False


def _nutri_tool(fn):
    """결과에 차트 화면(MCP 앱)을 연결하는 도구 등록 데코레이터. 앱 기능이 없으면 일반 도구로 등록"""
    if _APPS_OK:
        try:
            return mcp.tool(app=AppConfig(resource_uri=VIEW_URI))(fn)
        except Exception:
            pass
    return mcp.tool()(fn)


# 인증키는 환경변수 MFDS_API_KEY 로 넣는 것을 권장합니다 (코드에 직접 적으면 유출 위험)
API_KEY = os.environ.get("MFDS_API_KEY", "api 코드")
DATA_TYPE = "json"
BASE_URL = "http://openapi.foodsafetykorea.go.kr/api"
TIMEOUT = 15

# ---- 서비스 ID ----
SERVICE_ID = "C003"  # 17번 품목제조신고(원재료): 제품 검색용
DETAIL_SERVICE_ID = os.environ.get("MFDS_DETAIL_SERVICE_ID", "I0030")  # 19번 품목제조 신고사항 현황: STDR_STND(기준규격)에 성분 함량 포함
LIMIT_SERVICE_ID = os.environ.get("MFDS_LIMIT_SERVICE_ID", "I2710")  # 18번 품목분류정보: 성분별 일일섭취량 하한/상한 (1순위 기준)
INDIV_SERVICE_ID = os.environ.get("MFDS_INDIV_SERVICE_ID", "I-0050")  # 12번 개별인정형: 하한/상한 (2순위 보조 기준)

# 겹쳐도 의미 없는 부형제/첨가물 (성분 중복 분석에서 제외)
EXCIPIENTS = {
    "이산화규소", "스테아린산마그네슘", "스테아린산칼슘", "카복시메틸셀룰로스칼슘",
    "히드록시프로필메틸셀룰로스", "결정셀룰로스", "덱스트린", "말토덱스트린",
    "정제수", "글리세린지방산에스테르", "구연산삼나트륨", "변성전분", "정제포도당",
}

# 같은 성분의 다른 표기를 하나로 묶기 (예: 아스코브산칼슘 = 비타민C 공급원)
ALIASES = {
    "아스코브산칼슘": "비타민C",
    "아스코브산나트륨": "비타민C",
    "아스코르브산": "비타민C",
    "L-아스코르브산": "비타민C",
}

# 함량/기준 비교용 성분명 정규화 별칭 (공백 제거 + 소문자 처리 후 적용)
CANON_ALIASES = {
    "셀렌": "셀레늄",
    "니아신": "나이아신",
}

# 단위 -> mg 환산 (IU 등은 성분마다 달라 자동 환산하지 않음)
UNIT_TO_MG = {"g": 1000.0, "mg": 1.0, "㎎": 1.0, "㎍": 0.001, "μg": 0.001, "µg": 0.001, "mcg": 0.001, "ug": 0.001}
UNIT_PATTERN = r"(㎍|μg|µg|mcg|ug|㎎|mg|g)"


class ApiError(Exception):
    """네트워크/인증키/API 오류 (제품이 '없는' 경우와 구분하기 위함)"""


# =====================================================================
# 공통 유틸
# =====================================================================
def _to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def canon(s: str) -> str:
    """성분명 정규화: 괄호 내용/공백 제거, 소문자, 별칭 통일 ('셀레늄(또는 셀렌)' -> '셀레늄')"""
    s = re.sub(r"^[\s\u2460-\u2473\u24ea\u2776-\u2793\u3251-\u32bf]+", "", s or "")   # ①②③ 번호 접두어 제거
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\s+", "", s).lower()
    return CANON_ALIASES.get(s, s)


def unit_factor(unit: str):
    """'mg', 'μg RAE', 'mg α-TE' 처럼 뒤에 설명이 붙은 단위도 앞부분만 보고 mg 환산 계수를 돌려준다."""
    m = re.match(r"\s*" + UNIT_PATTERN, unit or "", re.I)
    return UNIT_TO_MG.get(m.group(1).lower()) if m else None


def parse_amount_mg(text: str):
    """'500 (mg/일)', '2,400 (mg)' 같은 문자열 -> mg 값. 해석 불가면 None"""
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*\(?\s*" + UNIT_PATTERN, text or "", re.I)
    if not m:
        return None
    num, factor = _to_float(m.group(1)), UNIT_TO_MG.get(m.group(2).lower())
    return num * factor if (num is not None and factor) else None


def short(text: str, n: int) -> str:
    text = text or "정보없음"
    return text[:n] + "..." if len(text) > n else text


def fmt_mg(v):
    return "-" if v is None else f"{v:g}mg"


# =====================================================================
# 제품 조회 (C003 검색 / I0030 상세)
# =====================================================================
@lru_cache(maxsize=256)
def fetch_products(name: str, limit: int = 10, service_id: str = SERVICE_ID) -> tuple:
    """식약처 API 조회. 결과 없음 -> 빈 tuple, 통신/인증 오류 -> ApiError."""
    enc = urllib.parse.quote(name)
    url = f"{BASE_URL}/{API_KEY}/{service_id}/{DATA_TYPE}/1/{limit}/PRDLST_NM={enc}"
    try:
        res = requests.get(url, timeout=TIMEOUT)
        res.raise_for_status()
        data = res.json()
    except (requests.RequestException, ValueError) as e:
        raise ApiError(f"네트워크/응답 오류: {e}")

    # 인증키 오류 등은 서비스 ID 없이 최상위에 RESULT 만 오는 경우가 있음
    service = data.get(service_id) or data
    result = service.get("RESULT", {})
    code = result.get("CODE")

    if code == "INFO-200":
        return ()
    if code != "INFO-000":
        raise ApiError(f"{result.get('MSG', '알 수 없는 오류')} (코드: {code})")
    return tuple(service.get("row", []))


def pick_product(name: str):
    """이름이 정확히 일치하는 제품을 우선 선택, 없으면 첫 번째 후보. (선택된 제품, 전체 후보)"""
    rows = fetch_products(name.strip())
    if not rows:
        return None, ()
    exact = [r for r in rows if r.get("PRDLST_NM", "").strip() == name.strip()]
    return (exact[0] if exact else rows[0]), rows


def pick_detail(official_name: str):
    """I0030(품목제조 신고사항)에서 공식 제품명이 정확히 일치하는 행만 반환. 다른 제품 함량이 섞이지 않도록 정확 일치만 허용."""
    rows = fetch_products(official_name.strip(), 10, DETAIL_SERVICE_ID)
    for r in rows:
        if r.get("PRDLST_NM", "").strip() == official_name.strip():
            return r
    return None


def parse_ingredients(text: str) -> set:
    """원재료 문자열 -> 정규화된 성분 집합 ('고시형' 같은 분류 표기와 부형제는 제외)"""
    text = re.sub(r"\([^)]*\)", "", text or "")  # (고시형), (제2015-1호) 등 괄호 내용 제거
    items = set()
    for x in re.split(r"[,;]", text):
        x = re.sub(r"(혼합제제분말|혼합제제|혼합)$", "", x.strip()).strip()
        x = ALIASES.get(x, x)
        if len(x) > 1 and x not in EXCIPIENTS:
            items.add(x)
    return items


# =====================================================================
# 기준규격(STDR_STND)에서 성분 함량 추출
# 예) '(2) 비타민C : 표시량(1,000 mg/ 1,300 mg)의 80 ~ 150%'
#     '14.비타민C: 표시량(100mg/3,750mg)의 80~150%'
# =====================================================================
_PREFIX_RE = re.compile(r"^\s*(?:\d+\s*[\.\)]|\(\d+\)|[\u2460-\u2473\u24ea\u2776-\u2793\u3251-\u32bf])\s*")
_STD_LINE_RE = re.compile(
    r"^([^:：()]+(?:\([^)]*\))?)\s*[:：]\s*표시량\s*\(\s*([\d,]+(?:\.\d+)?)\s*" + UNIT_PATTERN,
    re.I,
)
# '...)의 80 ~ 150%' 처럼 표시량 대비 허용 범위의 상한 비율
_RATIO_RE = re.compile(r"의\s*(\d+(?:\.\d+)?)\s*[~\-～]\s*(\d+(?:\.\d+)?)\s*%")


def extract_amounts(stdr_stnd: str) -> dict:
    """STDR_STND -> {정규화 성분명: (표시명, 표시량 mg, 최대 추정 mg)}.
    최대 추정 = 표시량 x 규격 상한 비율(예: 80~150% 이면 1.5배). 비율 표기가 없으면 표시량과 같게 둔다.
    정제1/정제2처럼 구간이 나뉜 제품은 같은 성분을 합산한다. CFU 같은 mg 환산 불가 단위는 건너뛴다."""
    found = {}
    for line in (stdr_stnd or "").splitlines():
        line = _PREFIX_RE.sub("", line.strip())
        m = _STD_LINE_RE.match(line)
        if not m:
            continue
        name, num, unit = m.group(1).strip(), _to_float(m.group(2)), m.group(3).lower()
        factor = UNIT_TO_MG.get(unit)
        if num is None or factor is None:
            continue
        r = _RATIO_RE.search(line[m.end():])
        upper_ratio = (_to_float(r.group(2)) / 100.0) if r and _to_float(r.group(2)) else 1.0
        label_mg = num * factor
        key = canon(name)
        disp, prev_label, prev_max = found.get(key, (name, 0.0, 0.0))
        found[key] = (disp, prev_label + label_mg, prev_max + label_mg * max(upper_ratio, 1.0))
    return found


_DEN_RE = re.compile(r"표시량\s*\(\s*[\d,]+(?:\.\d+)?\s*" + UNIT_PATTERN + r"[^/)]*/\s*([\d,]+(?:\.\d+)?)\s*" + UNIT_PATTERN, re.I)


def first_denominator_mg(stdr_stnd: str):
    """기준규격 '표시량(20mg/1.2g)' 에서 분모(1.2g)를 mg로. 없으면 None"""
    m = _DEN_RE.search(stdr_stnd or "")
    if not m:
        return None
    num, factor = _to_float(m.group(2)), UNIT_TO_MG.get(m.group(3).lower())
    return num * factor if (num is not None and factor) else None


def servings_per_day(ntk_mthd: str, den_mg=None):
    """섭취방법 문구에서 1일 섭취 단위 수 추정. '1일 1회, 1회 1정' -> (1, True). 해석 실패 시 (1, False).
    '1회 2캡슐(1.2g)' 처럼 1회 총중량이 적혀 있고 기준규격의 표시량 분모(1.2g)와 같으면, 표시량이 이미 1회분이므로 캡슐 수를 곱하지 않는다."""
    m_times = re.search(r"1일\s*(\d+)\s*회", ntk_mthd or "")
    m_units = re.search(r"1회\s*(\d+)\s*(?:정|포|캡슐|캅셀|알|개|스푼|ml|mL)(?:\s*\(\s*([\d,.]+)\s*" + UNIT_PATTERN + r"\s*\))?", ntk_mthd or "", re.I)
    if not (m_times and m_units):
        return 1, False
    count = int(m_units.group(1))
    if den_mg and m_units.group(2):
        w, factor = _to_float(m_units.group(2)), UNIT_TO_MG.get((m_units.group(3) or "").lower())
        if w and factor and abs(den_mg - w * factor) / (w * factor) <= 0.05:
            count = 1
    return int(m_times.group(1)) * count, True


# =====================================================================
# 섭취 기준(하한/상한) 조회: I2710 -> I-0050 순
# =====================================================================
LIMIT_NAME_FIELDS = ["PRDCT_NM", "SKLL_IX_IRDNT_RAWMTRL"]  # I2710은 성분명이 둘 중 한 곳에만 들어있는 경우가 있음


@lru_cache(maxsize=8)
def fetch_service_rows(service_id: str, filter_str: str = "", max_rows: int = 3000) -> tuple:
    """서비스 전체(또는 필터된) 행을 1000개씩 페이지로 가져옵니다. (호출 제한 1000회/일 -> 캐시 사용)"""
    rows, start = [], 1
    while start <= max_rows:
        end = start + 999
        url = f"{BASE_URL}/{API_KEY}/{service_id}/{DATA_TYPE}/{start}/{end}"
        if filter_str:
            url += f"/{filter_str}"
        try:
            res = requests.get(url, timeout=TIMEOUT)
            res.raise_for_status()
            data = res.json()
        except (requests.RequestException, ValueError) as e:
            raise ApiError(f"네트워크/응답 오류: {e}")

        service = data.get(service_id) or data
        result = service.get("RESULT", {})
        code = result.get("CODE")
        if code == "INFO-200":
            break
        if code != "INFO-000":
            raise ApiError(f"{result.get('MSG', '알 수 없는 오류')} (코드: {code})")

        rows.extend(service.get("row", []))
        total = _to_float(service.get("total_count"))
        if total is None or end >= total:
            break
        start = end + 1
    return tuple(rows)


def _find_in_classification(key: str):
    """I2710에서 성분 기준 찾기. 이름이 정확히 같은 행을 우선, 없으면 부분 일치(2글자 이상만)."""
    rows = fetch_service_rows(LIMIT_SERVICE_ID)
    exact, partial = None, None
    for r in rows:
        names = [canon(str(r.get(f) or "")) for f in LIMIT_NAME_FIELDS]
        if key in names:
            exact = r
            break
        if partial is None and len(key) >= 2 and any(n and key in n for n in names):
            partial = r
    row = exact or partial
    if not row:
        return None
    factor = unit_factor(str(row.get("INTK_UNIT") or "mg"))
    if factor is None:
        return {"error": f"기준 단위 `{row.get('INTK_UNIT')}` 자동 환산 불가"}
    low, up = _to_float(row.get("DAY_INTK_LOWLIMIT")), _to_float(row.get("DAY_INTK_HIGHLIMIT"))
    return {
        "low": low * factor if low is not None else None,
        "up": up * factor if up is not None else None,
        "source": "품목분류",
        "memo": (row.get("INTK_MEMO") or "").strip(),
    }


def _find_in_individual(key: str):
    """I-0050(개별인정형)에서 보조 기준 찾기. 값이 '500 (mg/일)' 같은 문자열이라 파싱한다."""
    if len(key) < 2:
        return None
    rows = fetch_service_rows(INDIV_SERVICE_ID)
    for r in rows:
        if key in canon(str(r.get("RAWMTRL_NM") or "")):
            low, up = parse_amount_mg(r.get("DAY_INTK_LOWLIMIT")), parse_amount_mg(r.get("DAY_INTK_HIGHLIMIT"))
            if low is None and up is None:
                continue
            return {"low": low, "up": up, "source": "개별인정형", "memo": ""}
    return None


def find_limit(key: str):
    """성분 기준 조회. 1순위 I2710, 없으면 I-0050. 못 찾으면 None, 환산 불가면 {'error': ...}"""
    found = _find_in_classification(key)
    if found is None:
        found = _find_in_individual(key)
    return found


def evaluate_limits(totals: dict, display: dict, sources: dict = None) -> str:
    """{정규화 성분명: 합산 mg} -> 상한/하한 비교 마크다운 표"""
    report = "| 성분 | 합산 섭취량 | 하한 | 상한 | 판정 | 기준 |\n| :--- | ---: | ---: | ---: | :---: | :---: |\n"
    no_basis, memos = [], []

    for key, total_mg in totals.items():
        lim = find_limit(key)
        if lim is None:
            no_basis.append(display[key])
            continue
        if "error" in lim:
            no_basis.append(f"{display[key]} ({lim['error']})")
            continue
        low_mg, up_mg = lim["low"], lim["up"]
        if up_mg is not None and total_mg > up_mg:
            verdict = "⚠️ 상한 초과"
        elif low_mg is not None and total_mg < low_mg:
            verdict = "🔻 하한 미만"
        else:
            verdict = "✅ 범위 내"
        report += f"| {display[key]} | {total_mg:g}mg | {fmt_mg(low_mg)} | {fmt_mg(up_mg)} | {verdict} | {lim['source']} |\n"
        if lim["memo"]:
            memos.append(f"{display[key]}: {lim['memo']}")

    if no_basis:
        report += f"\n**기준을 찾지 못한 성분:** {', '.join(no_basis)}\n"
    if memos:
        report += "\n**기준 참고 메모:**\n" + "".join(f"- {m}\n" for m in memos)
    return report


# =====================================================================
# (신규) 한국인 영양소 섭취기준 상한섭취량(UL) - 과섭취 판정의 주 기준
# 출처: 보건복지부 '2025 한국인 영양소 섭취기준' (2025.12 개정, 요약본 표를 옮겨 적은 값)
# ⚠️ 표 추출 과정에서 열이 섞일 수 있어, 실제 서비스에 쓰기 전 원문 PDF와 한 번 대조하세요.
# 성인(19세 이상)만 지원합니다. 임신부·수유부는 별도 기준이라 반영하지 않았습니다.
# =====================================================================
AGE_BANDS = [(19, 29), (30, 49), (50, 64), (65, 74), (75, 200)]  # 값 리스트의 순서와 동일

# 키: canon() 결과.  값: (단위, 값 또는 {"M": [5개], "F": [5개]}, 메모)
UL_TABLE = {
    "비타민a": ("μg", 3000, "RAE 기준입니다. 레티놀·베타카로틴을 구분하지 않고 합산했으니 원문에서 형태별 적용 여부를 확인하세요."),
    "비타민d": ("μg", 100, ""),
    "비타민e": ("mg", 540, "mg α-TE 기준"),
    "비타민c": ("mg", 2000, ""),
    "나이아신": ("mg", 35, "니코틴산 기준(35mg)으로 비교했습니다. 니코틴아미드 형태는 상한이 850mg이니 제품 원료 형태를 확인하세요."),
    "비타민b6": ("mg", 50, ""),
    "엽산": ("μg", 1000, "강화식품·보충제로 섭취한 엽산에 적용되는 기준"),
    "칼슘": ("mg", {"M": [3000, 2500, 2500, 2500, 2500], "F": [2500, 2000, 2000, 2000, 2000]}, ""),
    "인": ("mg", {"M": [3500, 3500, 3500, 3500, 3000], "F": [3500, 3500, 3500, 3500, 3000]}, ""),
    "마그네슘": ("mg", 350, "식품 외 급원(보충제 등)에만 적용되는 기준"),
    "철": ("mg", 45, ""),
    "아연": ("mg", 35, ""),
    "구리": ("μg", 10000, ""),
    "망간": ("mg", 11, ""),
    "셀레늄": ("μg", 400, ""),
    "요오드": ("μg", 2400, ""),
    "몰리브덴": ("μg", {"M": [650, 600, 600, 600, 550], "F": [500, 500, 500, 500, 450]}, ""),
    "콜린": ("mg", {"M": [3000, 3000, 3000, 3000, 3000], "F": [2500, 2500, 2500, 2500, 2500]}, ""),
}
# 상한섭취량이 '설정되지 않은' 영양소 (과다 섭취 판정 대상이 아님)
UL_UNSET = {"비타민b1", "티아민", "비타민b2", "리보플라빈", "비타민b12", "판토텐산", "비오틴", "비타민k", "크롬"}


def norm_sex(sex: str):
    s = (sex or "").strip().lower()
    if s in ("남", "남자", "남성", "m", "male"):
        return "M"
    if s in ("여", "여자", "여성", "f", "female"):
        return "F"
    return None


def age_band_index(age: int):
    for i, (lo, hi) in enumerate(AGE_BANDS):
        if lo <= age <= hi:
            return i
    return None


def ul_lookup(key: str, sex: str, age: int):
    """-> (상태, UL mg, 메모). 상태: 'ok' | 'unset'(UL 미설정) | 'unknown'(표에 없음)"""
    if key in UL_UNSET:
        return "unset", None, ""
    entry = UL_TABLE.get(key)
    if entry is None:
        return "unknown", None, ""
    unit, val, note = entry
    if isinstance(val, dict):
        val = val[sex][age_band_index(age)]
    return "ok", val * UNIT_TO_MG[unit.lower()], note


def validate_profile(sex: str, age: int):
    """성별·나이 검증. 문제가 없으면 (정규화된 성별, None), 문제가 있으면 (None, 안내 문구)"""
    s = norm_sex(sex)
    if s is None:
        return None, "성별은 '남' 또는 '여'로 입력해 주세요."
    if not isinstance(age, int) or age < 19:
        return None, "상한섭취량(UL) 비교는 만 19세 이상 성인만 지원합니다. 나이를 정수로 입력해 주세요."
    return s, None


def fmt_auto(mg: float) -> str:
    """1mg 미만은 μg로 표시 (0.01mg -> 10μg)"""
    if mg < 1:
        return f"{mg * 1000:g}μg"
    return f"{mg:g}mg"


def _bar(pct, width: int = 10) -> str:
    """퍼센트 -> 글자 막대 (100%에서 가득 참, 초과하면 끝에 ＋). 어떤 화면에서도 보이는 간단한 시각화"""
    if pct is None:
        return ""
    filled = max(0, min(width, round(min(pct, 100) / 100 * width)))
    return "█" * filled + "░" * (width - filled) + ("＋" if pct > 100 else "")


def evaluate_ul(totals: dict, totals_max: dict, display: dict, sex: str, age: int) -> str:
    """{정규화 성분명: 표시량 합산 mg}, {정규화 성분명: 최대 추정 mg} -> UL 비교 마크다운 표 (3단계 판정)"""
    report = "| 성분 | 표시량 합산 | 최대 추정 | UL | UL 대비(최대) | 판정 |\n| :--- | ---: | ---: | ---: | ---: | :---: |\n"
    notes, unset, unknown = [], [], []

    for key, label_mg in totals.items():
        max_mg = max(totals_max.get(key, label_mg), label_mg)
        status, ul_mg, note = ul_lookup(key, sex, age)
        if status == "unset":
            unset.append(display[key])
            continue
        if status == "unknown":
            unknown.append(display[key])
            continue
        if label_mg > ul_mg:
            verdict = "⚠️ UL 초과"
        elif max_mg > ul_mg:
            verdict = "🔶 최대 추정 시 초과 가능"
        else:
            verdict = "✅ UL 이하"
        report += (
            f"| {display[key]} | {fmt_auto(label_mg)} | {fmt_auto(max_mg)} | {fmt_auto(ul_mg)} "
            f"| {max_mg / ul_mg * 100:.0f}% | {verdict} |\n"
        )
        if note:
            notes.append(f"{display[key]}: {note}")

    if unset:
        report += f"\n**UL이 설정되지 않은 성분 (과다 섭취 판정 대상 아님):** {', '.join(unset)}\n"
    if unknown:
        report += f"\n**UL 표에 없는 성분 (별도 확인 필요):** {', '.join(unknown)}\n"
    if notes:
        report += "\n**기준 적용 시 참고:**\n" + "".join(f"- {n}\n" for n in notes)
    report += (
        "\n> 💡 '최대 추정'은 기준규격 허용 범위의 상한(예: 표시량의 150%)을 적용한 값입니다. "
        "음식으로 섭취하는 양은 포함하지 않았고, 일부 UL(마그네슘·엽산 등)은 보충제 유래분에만 적용됩니다."
    )
    return report


# =====================================================================
# MCP 도구
# =====================================================================
@mcp.tool()
def search_nutri_product(product_name: str) -> str:
    """[필독] 식약처 허가 정보를 검색합니다. 상위 10개 후보군을 보여주며, 찾는 제품이 없으면 제조사명과 함께 재검색하도록 안내합니다."""
    try:
        rows = fetch_products(product_name.strip())
    except ApiError as e:
        return f"식약처 API 오류: {e}"

    if not rows:
        return f"'{product_name}'과(와) 일치하는 제품을 찾지 못했습니다. 제조사명이나 정확한 제품 전체 이름을 입력해 보세요."

    text = f"### 🔍 식약처 허가 정보 검색 결과: **'{product_name}'** (상위 {len(rows)}개 후보군)\n\n"
    text += "| 번호 | 공식 제품명 (PRDLST_NM) | 제조(판매) 업소 (BSSH_NM) | 주요 원재료명 (요약) |\n"
    text += "| :---: | :--- | :--- | :--- |\n"
    for idx, item in enumerate(rows, 1):
        text += (
            f"| {idx} | **{item.get('PRDLST_NM', '정보없음')}** "
            f"| {item.get('BSSH_NM', '정보없음')} "
            f"| {short(item.get('RAWMTRL_NM'), 40)} |\n"
        )
    text += "\n---\n"
    text += "> 💡 **안내:** 위 후보군 중에 드시고 계신 제품이 없다면, **제조사명(예: 고려은단, 종근당 등)**을 포함하여 더 구체적으로 검색해 주세요."
    return text


@mcp.tool()
def compare_nutri_products(product_name_1: str, product_name_2: str) -> str:
    """[필독] 두 영양제 성분을 교차 분석합니다. 정확한 제품명이 아닐 경우 에러 메시지와 함께 구체적 검색을 유도합니다."""
    try:
        item1, _ = pick_product(product_name_1)
        item2, _ = pick_product(product_name_2)
    except ApiError as e:
        return f"식약처 API 오류: {e}"

    for given, item in ((product_name_1, item1), (product_name_2, item2)):
        if not item:
            return f"❌ '{given}' 정확한 제품 정보를 찾지 못했습니다. 먼저 단건 검색(`search_nutri_product`)으로 정확한 공식 제품명을 확인한 후 비교를 진행해 주세요."

    name1, raw1 = item1.get("PRDLST_NM", product_name_1), item1.get("RAWMTRL_NM", "")
    name2, raw2 = item2.get("PRDLST_NM", product_name_2), item2.get("RAWMTRL_NM", "")
    common = parse_ingredients(raw1) & parse_ingredients(raw2)

    report = "### 📊 영양제 성분 교차 분석 리포트\n\n"
    report += f"> #### 1️⃣ 공식 매칭 제품: **{name1}**\n> - **주요 원재료:** {short(raw1, 100)}\n\n"
    report += f"> #### 2️⃣ 공식 매칭 제품: **{name2}**\n> - **주요 원재료:** {short(raw2, 100)}\n\n"
    report += "---\n\n#### ⚖️ 교차 분석 결과\n\n"

    if common:
        report += f"두 제품에 **공통으로 포함된 성분**이 총 **{len(common)}개** 확인되었습니다:\n\n"
        for m in sorted(common):
            report += f"- ✅ `{m}`\n"
        report += "\n> ⚠️ **주의:** 동일 성분이 중복 함유되어 있으므로 제품 라벨의 함량을 확인해 일일 총 섭취량을 계산하세요."
    else:
        report += "> ✨ **분석 완료:** 두 제품 간에 직접 겹치는 주요 원재료명이 발견되지 않았습니다."
    return report


@mcp.tool()
def analyze_daily_intake(product_names_str: str, sex: str = "", age: int = 0) -> str:
    """[필독] 여러 영양제들의 일일 섭취를 종합 분석합니다. 기준규격(STDR_STND)에서 성분 함량을 추출해 합산하고,
    sex('남'/'여')와 age(만 19세 이상 정수)를 주면 한국인 영양소 섭취기준의 상한섭취량(UL)으로 과다 섭취를 판정합니다.
    제품명 뒤에 'x2'를 붙이면 하루 섭취 단위(정/포) 수를 직접 지정합니다. 예: '속편한 고려은단 비타민C1000 x2, 센트룸 ...'
    목록 중 누락되거나 찾을 수 없는 제품이 있으면 개별적으로 피드백을 제공합니다."""
    # 쉼표, 슬래시, +, 줄바꿈 모두 구분자로 허용. 이름 끝의 'x2' / '×2' 는 하루 섭취 단위 수로 해석
    names, units_by_name = [], {}
    for n in (x.strip() for x in re.split(r"[,\n/+]", product_names_str)):
        if not n:
            continue
        m = re.search(r"\s*[x×]\s*(\d+(?:\.\d+)?)\s*$", n, re.I)
        if m and n[:m.start()].strip():
            n = n[:m.start()].strip()
            units_by_name[n] = float(m.group(1))
        names.append(n)
    if not names:
        return "분석할 영양제 이름을 쉼표로 구분하여 올바르게 입력해주세요. (예: 고려은단 비타민C 1000, 센트룸 우먼)"

    matched = []      # (공식명, 원재료, 섭취방법, 주의사항, 후보 수, 정확일치 여부)
    not_found = []
    api_errors = []
    ingredient_map = {}
    units_by_official = {}  # 사용자가 직접 지정한 하루 섭취 단위 수

    for name in names:
        try:
            item, rows = pick_product(name)
        except ApiError as e:
            api_errors.append((name, str(e)))
            continue
        if not item:
            not_found.append(name)
            continue

        official = item.get("PRDLST_NM", name)
        raw = item.get("RAWMTRL_NM", "")
        exact = official.strip() == name.strip()
        if name in units_by_name:
            units_by_official[official] = units_by_name[name]
        matched.append((official, raw, item.get("NTK_MTHD", ""), item.get("IFTKN_ATNT_MATR_CN", ""), len(rows), exact))
        for ing in parse_ingredients(raw):
            ingredient_map.setdefault(ing, []).append(official)

    if not matched:
        if api_errors:
            detail = "\n".join(f"- `{n}`: {m}" for n, m in api_errors)
            return f"❌ 식약처 API 호출에 문제가 있어 분석하지 못했습니다 (제품이 없는 것이 아닙니다).\n{detail}"
        return "❌ 입력하신 모든 제품의 식약처 허가 정보를 찾지 못했습니다. 단건 검색을 통해 정확한 제품명을 확인해 주세요."

    # ---- 함량 추출 (I0030 기준규격) ----
    totals, totals_max, display, per_product_notes, detail_errors = {}, {}, {}, [], []
    for official, _, ntk, _, _, _ in matched:
        try:
            detail = pick_detail(official)
        except ApiError as e:
            detail_errors.append((official, str(e)))
            continue
        if not detail:
            per_product_notes.append(f"- **{official}**: 품목제조 신고사항(기준규격)에서 정확히 일치하는 제품을 찾지 못해 함량을 계산하지 못했습니다.")
            continue
        amounts = extract_amounts(detail.get("STDR_STND", ""))
        if not amounts:
            per_product_notes.append(f"- **{official}**: 기준규격에서 mg·μg 단위 함량 표기를 찾지 못했습니다 (프로바이오틱스 등은 CFU 단위라 제외). 라벨 확인이 필요합니다.")
            continue
        if official in units_by_official:
            mult, how = units_by_official[official], "직접 지정"
        else:
            mult, parsed = servings_per_day(detail.get("NTK_MTHD") or ntk, first_denominator_mg(detail.get("STDR_STND", "")))
            how = "섭취방법 문구 기준" if parsed else "섭취량 문구 해석 실패로 1회분 기준"
        summary = ", ".join(f"{d} {fmt_auto(label_mg * mult)}" for d, label_mg, _ in amounts.values())
        per_product_notes.append(f"- **{official}** (1일 ×{mult:g}, {how}): {summary}")
        for key, (disp, label_mg, max_mg) in amounts.items():
            totals[key] = totals.get(key, 0.0) + label_mg * mult
            totals_max[key] = totals_max.get(key, 0.0) + max_mg * mult
            display.setdefault(key, disp)

    report = "### 💊 [일일 영양제 섭취 종합 분석 리포트]\n\n"
    report += f"**✅ 매칭된 제품 ({len(matched)}개):**\n"
    for official, _, ntk, caution, n_rows, exact in matched:
        report += f"- **{official}**"
        if not exact and n_rows > 1:
            report += f"  ⚠️ 후보 {n_rows}개 중 첫 번째로 매칭됨 (정확한 제품명으로 다시 확인 권장)"
        report += "\n"
        if ntk:
            report += f"  - 섭취방법: {short(ntk, 120)}\n"
        if caution:
            report += f"  - 섭취 시 주의사항: {short(caution, 120)}\n"

    if not_found:
        report += f"\n**⚠️ 정보를 찾지 못한 제품 ({len(not_found)}개):**\n"
        for fn in not_found:
            report += f"- `{fn}` (단건 검색으로 정확한 명칭을 확인해 보세요)\n"
    if api_errors:
        report += f"\n**❗ API 오류로 조회하지 못한 제품 ({len(api_errors)}개):**\n"
        for n, m in api_errors:
            report += f"- `{n}`: {m}\n"
    if detail_errors:
        report += f"\n**❗ 함량 조회 API 오류 ({len(detail_errors)}개):**\n"
        for n, m in detail_errors:
            report += f"- `{n}`: {m}\n"

    duplicates = {ing: plist for ing, plist in ingredient_map.items() if len(plist) > 1}
    report += "\n---\n\n#### ⚖️ 성분 중복 및 교차 분석 결과\n\n"
    if duplicates:
        report += "⚠️ **주의:** 다음 성분들이 여러 제품에 중복으로 포함되어 있습니다.\n\n"
        for ing, plist in duplicates.items():
            report += f"> - **중복 성분:** `{ing}`\n>   - *함유 제품:* {', '.join(plist)}\n\n"
    else:
        report += "> ✨ 매칭된 제품들 간에 주요 원재료가 직접 겹치는 항목이 발견되지 않았습니다.\n"

    report += "\n---\n\n#### 🧮 성분별 1일 합산 섭취량 (기준규격 표시량 기반)\n\n"
    if per_product_notes:
        report += "\n".join(per_product_notes) + "\n\n"
    if totals:
        profile_given = bool(sex) or bool(age)
        s, profile_msg = validate_profile(sex, age) if profile_given else (None, "성별(sex)과 나이(age)를 함께 입력하면 한국인 영양소 섭취기준의 상한섭취량(UL)으로 과다 섭취 여부를 판정해 드립니다.")
        if s:
            report += f"##### 상한섭취량(UL) 기준 비교 ({'남' if s == 'M' else '여'} {age}세)\n\n"
            report += evaluate_ul(totals, totals_max, display, s, age) + "\n\n"
        else:
            report += f"> ℹ️ {profile_msg}\n\n"
        report += "##### 식약처 제품 기준 범위 비교 (참고)\n\n"
        try:
            report += evaluate_limits(totals, display)
        except ApiError as e:
            report += f"식약처 기준(상한/하한) 조회 오류: {e}\n"
        report += "\n> 💡 함량은 제품 기준규격의 '표시량'에서 자동 추출한 값이며, 1일 섭취 단위는 섭취방법 문구 또는 직접 지정한 값입니다. 정확한 값은 제품 라벨과 대조하세요. 식약처 제품 기준 범위는 제품 허가 기준이라 영양학적 안전 상한(UL)과 다르며, 개인의 건강 상태나 복용 약에 따른 적정량과도 다를 수 있습니다."
    else:
        report += "> 💡 자동으로 추출된 함량이 없습니다. 각 제품 라벨의 함량을 `check_intake_limits`에 직접 입력해 상한량을 확인하세요."
    return report


@mcp.tool()
def debug_service_fields(service_id: str = "", filter_str: str = "", count: int = 2) -> str:
    """식약처 API 응답의 원본 필드(키/값)를 그대로 보여줍니다. 서비스 ID를 비우면 일일섭취량 서비스(I2710)를 조회합니다.
    filter_str 예: 'PRDLST_NM=속편한 고려은단 비타민C1000' (제품별 함량 필드 확인용)"""
    sid = service_id.strip() or LIMIT_SERVICE_ID
    flt = urllib.parse.quote(filter_str, safe="=&") if filter_str else ""
    url = f"{BASE_URL}/{API_KEY}/{sid}/{DATA_TYPE}/1/{max(1, min(count, 5))}"
    if flt:
        url += f"/{flt}"
    try:
        res = requests.get(url, timeout=TIMEOUT)
        res.raise_for_status()
        text = res.text
    except requests.RequestException as e:
        return f"요청 오류: {e}"
    return f"서비스 `{sid}` 원본 응답 (앞부분):\n\n```json\n{text[:4000]}\n```"


def _parse_intake(intake_str: str):
    """'비타민C 1000mg, 아연 8.5mg' -> [(성분명, mg 또는 None, 원문)]"""
    out = []
    for part in re.split(r"[,\n;]", intake_str):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.+?)\s*([\d.,]+)\s*([a-zA-Zμµ㎎㎍]+)$", part)
        if not m:
            out.append((part, None, part))
            continue
        name, num, unit = m.group(1).strip(), _to_float(m.group(2)), m.group(3).lower()
        factor = UNIT_TO_MG.get(unit)
        out.append((name, num * factor if (num is not None and factor) else None, part))
    return out


@mcp.tool()
def check_intake_limits(intake_str: str, sex: str = "", age: int = 0) -> str:
    """성분별 하루 섭취량을 합산해 한국인 영양소 섭취기준의 상한섭취량(UL)과 식약처 제품 기준 범위로 비교합니다.
    함량은 제품 라벨의 값을 직접 입력하세요. 입력 예: '비타민C 1000mg, 비타민C 60mg, 아연 8.5mg'
    UL 비교에는 sex('남'/'여')와 age(만 19세 이상 정수)가 필요합니다."""
    entries = _parse_intake(intake_str)
    if not entries:
        return "섭취량을 '성분명 함량단위' 형식으로 쉼표로 구분해 입력해 주세요. (예: 비타민C 1000mg, 아연 8.5mg)"

    totals, display, skipped = {}, {}, []
    for name, mg, raw in entries:
        if mg is None:
            skipped.append(raw)
            continue
        key = canon(name)
        totals[key] = totals.get(key, 0.0) + mg
        display.setdefault(key, name)

    if not totals:
        return f"해석 가능한 섭취량이 없습니다. (해석하지 못한 입력: {', '.join(skipped)})"

    report = "### ⚖️ 일일섭취량 상한 비교\n\n"
    profile_given = bool(sex) or bool(age)
    s, profile_msg = validate_profile(sex, age) if profile_given else (None, "성별(sex)과 나이(age)를 함께 입력하면 한국인 영양소 섭취기준의 상한섭취량(UL)으로 판정해 드립니다.")
    if s:
        # 직접 입력한 값은 표시량 그대로이므로 최대 추정은 같은 값으로 둔다
        report += f"##### 상한섭취량(UL) 기준 비교 ({'남' if s == 'M' else '여'} {age}세)\n\n"
        report += evaluate_ul(totals, dict(totals), display, s, age) + "\n\n"
    else:
        report += f"> ℹ️ {profile_msg}\n\n"

    report += "##### 식약처 제품 기준 범위 비교 (참고)\n\n"
    try:
        report += evaluate_limits(totals, display)
    except ApiError as e:
        report += f"식약처 기준 조회 오류: {e}\n"

    if skipped:
        report += f"\n**해석하지 못한 입력 (IU 등 자동 환산 불가 단위 포함):** {', '.join(skipped)}\n"
    report += "\n> 💡 식약처 제품 기준 범위는 제품 허가 기준이라 영양학적 안전 상한(UL)과 다르며, 개인의 건강 상태나 복용 약에 따른 적정량과도 다를 수 있습니다."
    return report


# =====================================================================
# (신규) 공공데이터포털(data.go.kr) 연동 + 추가 기능 5종
#   1) check_recall_history           회수·판매중지 이력 조회
#   2) check_manufacturer_status      제조업체 HACCP·폐업·인허가 변경·지도점검 조회
#   3) check_drug_supplement_cautions 복용 중인 약 관련 주의 문구 (제품 허가 주의사항 기반)
#   4) check_condition_cautions       몸 상태(임신·간질환 등) 관련 주의 문구
#   5) recommend_by_function          기능성 키워드 기반 성분 추천
#   + debug_datagokr                  공공데이터포털 원본 응답 확인용
#
# ⚠️ 공공데이터포털 서비스의 '요청변수 이름'과 '응답 필드 이름'은 명세 화면을 확인하지 못한 추정값입니다.
#    그래서 첫 호출 때 후보 이름을 차례로 시도해 서버가 필터를 적용하는 이름을 찾아 기억합니다.
#    debug_datagokr 로 실제 응답을 확인한 뒤, 확정된 이름을 DGK_PARAM_CONFIRMED 에 적어 두세요.
# =====================================================================
DATA_GO_KR_KEY = os.environ.get("DATA_GO_KR_KEY", "")  # 식품안전나라 키(MFDS_API_KEY)와 별개. '일반 인증키(Decoding)'
DGK_BASE = "https://apis.data.go.kr"
DGK_SERVICES = {
    # 키 이름 -> "기관코드/서비스명/오퍼레이션"  (End Point + 상세기능)
    "recall_med":  "1471000/MdcinRtrvlSleStpgeInfoService05/getMdcinRtrvlSleStpgelList05",
    "recall_etc":  "1471000/MdcinRtrvlSleStpgeInfoService05/getMdcinRtrvlSleStpgelEtcList05",
    "haccp":       "B553748/HffCertInfoService/getHffCertInfo",
    "closure":     "1471000/HtfsClsbizInfo01/getHtfsClsbizInfo01",
    "lcns_change": "1471000/HtfsLcnsChgInfo01/getHtfsLcnsChgInfo01",
    "inspection":  "1471000/HtfsMnftSaleBsshCkofGuidceDtlPlan/getHtfsMnftSaleBsshCkofGuidceDtlPlan",
    "target":      "1471000/HtfsTrgetInfoService02/getHtfsInfoList02",
    "hf_list":     "1471000/HtfsInfoService03/getHtfsList01",
    "hf_detail":   "1471000/HtfsInfoService03/getHtfsItem01",
    "dur_prdlst":  "1471000/DURPrdlstInfoService03/getDurPrdlstInfoList03",   # DUR 품목정보 (제품명-성분 연결 후보)
    "dur_ingr":    "1471000/DURIrdntInfoService03/getUsjntTabooInfoList02",  # DUR 성분정보 병용금기 (참고)
    "dur_prdlst_taboo": "1471000/DURPrdlstInfoService03/getUsjntTabooInfoList03",   # DUR 품목정보 병용금기
    "dur_ingr_age":     "1471000/DURIrdntInfoService03/getSpcifyAgrdeTabooInfoList02",  # DUR 성분 특정연령대금기
    "dur_ingr_dose":    "1471000/DURIrdntInfoService03/getCpctyAtentInfoList02",        # DUR 성분 용량주의
    "dur_ingr_period":  "1471000/DURIrdntInfoService03/getMdctnPdAtentInfoList02",      # DUR 성분 투여기간주의
    "dur_ingr_preg":    "1471000/DURIrdntInfoService03/getPwnmTabooInfoList02",         # DUR 성분 임부금기 (요청변수 ingrName)
    "dur_ingr_elderly": "1471000/DURIrdntInfoService03/getOdsnAtentInfoList02",         # DUR 성분 노인주의
    "dur_ingr_efcy":    "1471000/DURIrdntInfoService03/getEfcyDplctInfoList02",         # DUR 성분 효능군중복 (아직 도구에 연결 안 함)
}
# 검색 조건(업체명/제품명)으로 쓸 요청변수 이름 후보 (가능성이 높은 순)
DGK_ROLE_CANDIDATES = {
    "company": ["entp_name", "entrps", "bssh_nm", "company_name", "entpName", "bsshNm", "Entrps", "BSSH_NM", "ENTP_NAME", "entp_nm", "ENTP_NM",
                "entpNm", "bsshnm", "bssh_name", "BSSH_NAME", "company", "COMPANY_NAME", "bizr_nm", "BIZR_NM", "cmpny_nm", "CMPNY_NM", "upso_nm", "UPSO_NM",
                "prcscitypointBsshnm", "PRCSCITYPOINT_BSSHNM", "entrps_nm", "ENTRPS_NM"],
    "product": ["item_name", "prduct", "prdlst_nm", "product_name", "itemName", "prdlstNm", "Prduct", "PRDLST_NM", "ITEM_NAME", "item_nm", "ITEM_NM",
                "prdlst_nm", "prdlstnm", "PRODUCT_NAME"],
}
# 확정된 요청변수 이름 (탐색 생략). 예: {("recall_med", "product"): "prduct", ("haccp", "company"): "company_name"}
DGK_PARAM_CONFIRMED = {
    # 활용가이드 문서에서 확인된 요청변수 이름 (대소문자 그대로)
    ("hf_list", "product"): "Prduct",
    ("hf_list", "company"): "Entrps",
    ("hf_detail", "product"): "Prduct",
    ("target", "product"): "prdlst_nm",   # 문서는 HtfsTrgetInfoService01 기준 (신청 목록은 02) - 실제 응답으로 확인 필요
    ("target", "company"): "bssh_nm",
}
# 요청변수로 업체명 검색을 지원하지 않아, 전체를 받아 업체명으로 거르는 서비스 (HACCP 등)
DGK_SCAN_ONLY = {"haccp", "inspection"}
DGK_FORMAT_PARAM = {"haccp": "returnType"}   # 응답 형식 요청변수 이름이 다른 서비스
DGK_SCAN_PAGE_SIZE = 100
DGK_SCAN_BIG_PAGE = 1000
DGK_SCAN_MAX_PAGES = 40   # 100건 단위면 4,000건, 1,000건 단위면 40,000건까지 확인
_dgk_learned = {}

_DGK_OK_CODES = {"00", "0", "OK", "INFO-000", ""}
_DGK_EMPTY_CODES = {"03", "NODATA_ERROR"}
_DGK_PARAM_CODES = {"10", "11"}  # 요청변수 오류 -> 다른 후보 이름을 시도


class DgkParamError(ApiError):
    """요청변수 이름/값 오류 (다른 후보 이름을 시도하기 위한 신호)"""


def _dgk_key() -> str:
    key = (DATA_GO_KR_KEY or "").strip()
    return urllib.parse.unquote(key) if "%" in key else key  # Encoding 키를 넣어도 이중 인코딩되지 않게


def _parse_dgk(res):
    """공공데이터포털 응답(JSON 또는 XML) -> (code, msg, total, rows)"""
    text = res.text or ""
    try:
        data = res.json()
    except ValueError:
        data = None

    if isinstance(data, dict):
        root = data.get("response", data)
        header = root.get("header") or root.get("cmmMsgHeader") or {}
        body = root.get("body") or {}
        code = str(header.get("resultCode", header.get("returnReasonCode", root.get("resultCode", ""))))
        msg = header.get("resultMsg") or header.get("resultMessage") or header.get("returnAuthMsg") or root.get("resultMsg", "")
        items = body.get("items", [])
        if isinstance(items, dict):
            items = items.get("item", items)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            items = []
        rows = [(it["item"] if isinstance(it, dict) and set(it.keys()) == {"item"} else it) for it in items]
        rows = [r for r in rows if isinstance(r, dict)]
        return code, msg, _to_float(body.get("totalCount")), rows

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise ApiError(f"응답을 해석하지 못했습니다 (HTTP {res.status_code}): {text[:150]}")
    err = root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg")
    if err and root.tag.startswith("OpenAPI"):
        raise ApiError(f"{err} (코드: {root.findtext('.//returnReasonCode') or ''})")
    rows = [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]
    return (root.findtext(".//resultCode") or "", root.findtext(".//resultMsg") or root.findtext(".//resultMessage") or "",
            _to_float(root.findtext(".//totalCount")), rows)


@lru_cache(maxsize=256)
def dgk_fetch(service: str, params_items: tuple = (), num_rows: int = 20, page_no: int = 1):
    """공공데이터포털 호출. -> (rows tuple, 전체 건수). 요청변수 오류 -> DgkParamError, 그 외 오류 -> ApiError"""
    key = _dgk_key()
    if not key:
        raise ApiError("공공데이터포털 인증키가 없습니다. 환경변수 DATA_GO_KR_KEY 에 일반 인증키(Decoding)를 설정해 주세요.")
    url = f"{DGK_BASE}/{DGK_SERVICES[service]}"
    params = {"serviceKey": key, "pageNo": page_no, "numOfRows": num_rows, DGK_FORMAT_PARAM.get(service, "type"): "json"}
    params.update(dict(params_items))
    res = None
    for attempt in range(3):          # 시간 초과 등 일시 오류는 최대 3번까지 재시도
        try:
            res = requests.get(url, params=params, timeout=TIMEOUT + 10 * attempt)
            break
        except requests.RequestException as e:
            if attempt == 2:
                raise ApiError(f"네트워크 오류(3회 재시도 실패): {e}")

    code, msg, total, rows = _parse_dgk(res)
    if code in _DGK_PARAM_CODES:
        raise DgkParamError(f"{msg} (코드: {code})")
    if code in _DGK_EMPTY_CODES:
        return (), 0
    if code not in _DGK_OK_CODES:
        raise ApiError(f"{msg} (코드: {code})")
    return tuple(rows), (int(total) if total is not None else len(rows))


_CORP_RE = re.compile(r"\(주\)|㈜|주식회사|\(유\)|\(합\)|유한회사")


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", "", _CORP_RE.sub("", str(s or ""))).lower()


def _row_text(row: dict) -> str:
    return " ".join(str(v) for v in row.values() if v)


_DGK_PROBE = "zzqx없는검색어9"  # 어떤 데이터에도 없을 값: 서버가 이 요청변수로 실제 필터링하는지 확인하는 용도


@lru_cache(maxsize=64)
def _dgk_scan_page(service: str, size: int, page: int):
    return dgk_fetch(service, (), size, page)


def _dgk_scan_company(service: str, value: str):
    """요청변수로 거를 수 없는 서비스: 페이지를 넘기며 전부 받은 뒤 업체명(또는 제품명)이 포함된 행만 남긴다."""
    needle = _norm_text(value)
    out, page, size = [], 1, DGK_SCAN_BIG_PAGE
    while page <= DGK_SCAN_MAX_PAGES:
        try:
            rows, total = _dgk_scan_page(service, size, page)
        except ApiError:
            if size == DGK_SCAN_PAGE_SIZE:
                raise
            size, page, out = DGK_SCAN_PAGE_SIZE, 1, []     # 큰 페이지를 거부하면 100건씩으로 다시
            continue
        out.extend(r for r in rows if needle and needle in _norm_text(_row_text(r)))
        if not rows or page * size >= (total or 0):
            break
        page += 1
    return out


def dgk_search(service: str, role: str, value: str, num_rows: int = 20):
    """업체명/제품명(role='company'|'product')으로 검색. -> (행 리스트, 메모)
    처음 한 번은 후보 요청변수마다 '없는 값'으로 먼저 찔러 보고(결과가 0건이면 서버가 필터를 적용하는 변수),
    확인된 변수를 기억합니다. 메모 'filter_unknown' = 필터가 적용되는 요청변수 이름을 찾지 못함."""
    if service in DGK_SCAN_ONLY:
        return _dgk_scan_company(service, value), ""
    confirmed = DGK_PARAM_CONFIRMED.get((service, role)) or _dgk_learned.get((service, role))
    candidates = [confirmed] if confirmed else list(DGK_ROLE_CANDIDATES[role])
    for pname in candidates:
        if not confirmed:
            try:
                probe_rows, _ = dgk_fetch(service, ((pname, _DGK_PROBE),), 5)
            except DgkParamError:
                continue          # 서버가 모르는 변수 (오류 코드)
            if probe_rows:
                continue          # 값을 무시하고 전체를 돌려줌 -> 필터로 쓸 수 없는 변수
            _dgk_learned[(service, role)] = pname
        try:
            rows, _ = dgk_fetch(service, ((pname, value),), num_rows)
        except DgkParamError:
            return [], "filter_unknown"
        return list(rows), ""
    return [], "filter_unknown"


def _dgk_union(service: str, queries: list):
    """여러 (역할, 값) 질의 결과를 합쳐 중복 제거. -> (rows, 필터 미확인 여부)"""
    seen, rows, unknown = set(), [], False
    for role, value in queries:
        if not (value or "").strip():
            continue
        found, note = dgk_search(service, role, value.strip())
        unknown = unknown or note == "filter_unknown"
        for row in found:
            k = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if k not in seen:
                seen.add(k)
                rows.append(row)
    return rows, unknown


def row_summary(row: dict, limit: int = 8, width: int = 60) -> str:
    """응답 필드명을 몰라도 쓸 수 있게 값이 있는 필드를 '키: 값' 형태로 요약"""
    parts = []
    for k, v in row.items():
        if v in (None, ""):
            continue
        s = str(v).strip().replace("\n", " ")
        parts.append(f"{k}: {s[:width]}{'…' if len(s) > width else ''}")
        if len(parts) >= limit:
            break
    return " / ".join(parts)


@mcp.tool()
def probe_dgk_params(service: str, sample_value: str = "", role: str = "company") -> str:
    """서비스의 업체명/제품명 검색 요청변수 이름을 후보별로 모두 시험해 결과표를 보여줍니다. ❔(검색 조건 확인 불가)가 뜨는 서비스를 진단할 때 쓰세요.
    service 예: closure, lcns_change, inspection. sample_value 에 실제 있는 업체명(예: '일화')을 넣으면 정확도가 올라갑니다."""
    if service not in DGK_SERVICES:
        return "알 수 없는 서비스입니다. 예: " + ", ".join(DGK_SERVICES)
    out = [f"### 요청변수 후보 시험: `{service}` ({role})\n", "| 후보 | 없는 값 | " + (f"`{sample_value}` | " if sample_value else "") + "판정 |", "| :--- | :--- | " + (":--- | " if sample_value else "") + ":--- |"]
    for pname in DGK_ROLE_CANDIDATES.get(role, []):
        try:
            rows, total = dgk_fetch(service, ((pname, _DGK_PROBE),), 3)
            a = f"{len(rows)}건(총 {total})"
            ignored = bool(rows)
        except DgkParamError as e:
            out.append(f"| {pname} | 변수 오류 | " + ("- | " if sample_value else "") + "❌ 서버가 모름 |")
            continue
        except ApiError as e:
            out.append(f"| {pname} | 오류 {short(str(e), 40)} | " + ("- | " if sample_value else "") + "⚠️ |")
            continue
        b = ""
        if sample_value:
            try:
                r2, t2 = dgk_fetch(service, ((pname, sample_value),), 3)
                b = f"{len(r2)}건(총 {t2}) | "
            except ApiError as e:
                b = f"오류 | "
        out.append(f"| {pname} | {a} | {b}" + ("➖ 값을 무시함(전체 반환)" if ignored else "✅ 필터 적용으로 보임") + " |")
    return "\n".join(out)


@mcp.tool()
def debug_datagokr(service: str = "", params_str: str = "", count: int = 2) -> str:
    """공공데이터포털 서비스의 원본 응답을 보여줍니다. service 를 비우면 사용 가능한 서비스 이름 목록을 보여줍니다.
    params_str 예: 'entp_name=고려은단' (요청변수 이름 확인용). 요청변수·응답 필드 이름을 확정할 때 사용하세요."""
    if not service.strip():
        lines = "\n".join(f"- `{k}` → {v}" for k, v in DGK_SERVICES.items())
        learned = ", ".join(f"{s}.{r}={p}" for (s, r), p in _dgk_learned.items()) or "없음"
        return f"사용 가능한 서비스:\n{lines}\n\n지금까지 자동 확인된 검색 변수: {learned}"
    if service not in DGK_SERVICES:
        return f"알 수 없는 서비스입니다: {service}. service 를 비우면 목록을 보여줍니다."
    key = _dgk_key()
    if not key:
        return "환경변수 DATA_GO_KR_KEY 가 설정되지 않았습니다."
    params = {"serviceKey": key, "pageNo": 1, "numOfRows": max(1, min(count, 5)), DGK_FORMAT_PARAM.get(service, "type"): "json"}
    params.update(dict(urllib.parse.parse_qsl(params_str)))
    try:
        res = requests.get(f"{DGK_BASE}/{DGK_SERVICES[service]}", params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        return f"요청 오류: {e}"
    text = (res.text or "").replace(key, "***KEY***")
    return f"서비스 `{service}` (HTTP {res.status_code}) 원본 응답 (앞부분):\n\n```\n{text[:4000]}\n```"


# ---------------------------------------------------------------------
# 1) 회수·판매중지 이력
# ---------------------------------------------------------------------
@mcp.tool()
def check_recall_history(product_name: str, company: str = "") -> str:
    """제품명(과 제조사명)으로 식약처 회수·판매중지 이력을 조회합니다.
    ⚠️ 이 서비스는 의약품·의약외품 대상이라 건강기능식품 회수는 포함되지 않을 수 있고,
    '검색 결과 없음'이 '문제 없음'을 보장하지는 않습니다."""
    if not (product_name or "").strip() and not (company or "").strip():
        return "제품명 또는 제조사명을 입력해 주세요."
    queries = [("product", product_name), ("company", company)]
    report = f"### 🚨 회수·판매중지 이력 조회: {(product_name or company).strip()}\n\n"
    problems = []
    for svc, label in (("recall_med", "의약품 회수·판매중지"), ("recall_etc", "의약품외 회수·판매중지")):
        try:
            rows, unknown = _dgk_union(svc, queries)
        except ApiError as e:
            problems.append(f"- {label}: {e}")
            continue
        report += f"**{label}**\n"
        if rows:
            report += f"- ⚠️ {len(rows)}건 검색됨 (상위 5건)\n"
            for r in rows[:5]:
                report += f"  - {row_summary(r)}\n"
        elif unknown:
            report += "- ❔ 서버가 검색 조건을 적용하는지 확인하지 못했습니다. `debug_datagokr`로 요청변수 이름을 확인해 주세요.\n"
        else:
            report += "- ✅ 검색된 이력이 없습니다.\n"
        report += "\n"
    if problems:
        report += "**❗ 조회 오류**\n" + "\n".join(problems) + "\n\n"
    report += ("> 💡 위 서비스는 의약품·의약외품 회수 정보입니다. 건강기능식품(식품) 회수·판매중지는 "
               "식품안전나라의 회수 서비스에서 따로 확인해야 할 수 있습니다. 동명 제품이 섞일 수 있으니 업체명을 함께 확인하세요.")
    return report


# ---------------------------------------------------------------------
# 2) 제조업체 확인 (HACCP·폐업·인허가 변경·지도점검)
# ---------------------------------------------------------------------
def _haccp_validity(row: dict) -> str:
    """HACCP 응답의 인증종료일(issueenddate, 예: 2024-01-24)로 만료 여부 표시"""
    end = str(row.get("issueenddate") or row.get("ISSUEENDDATE") or "").strip()
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", end)
    if not m:
        return ""
    import datetime
    try:
        d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return ""
    return "  ⚠️ 인증기간 만료" if d < datetime.date.today() else "  ✅ 인증기간 유효"


@mcp.tool()
def check_manufacturer_status(product_name: str) -> str:
    """제품의 제조업체를 찾아 HACCP 인증업체 등록 여부, 폐업 여부, 인허가 변경 이력, 지도·단속 실적을 확인합니다.
    ⚠️ 건강기능식품 GMP 인증 자체가 아니라 '건강기능식품전문제조 HACCP 인증업체' 목록 기준입니다.
    ※ 결과를 전달할 때 도구 결과에 없는 일반 지식·추정을 덧붙이지 말고, '해야 합니다' 같은 단정 대신 도구가 낸 판정 범위 안에서만 말하세요."""
    try:
        item, rows = pick_product(product_name)
    except ApiError as e:
        return f"식약처 API 오류: {e}"
    if not item:
        return f"'{product_name}' 제품을 찾지 못했습니다. 먼저 `search_nutri_product`로 공식 제품명을 확인해 주세요."

    official = item.get("PRDLST_NM", product_name)
    company = (item.get("BSSH_NM") or "").strip()
    lcns = (item.get("LCNS_NO") or "").strip()
    if not company:
        return f"**{official}** 의 제조업체명을 확인하지 못했습니다."

    report = f"### 🏭 제조업체 확인: **{official}**\n\n- 제조(판매) 업체: **{company}**" + (f" (인허가번호 {lcns})" if lcns else "") + "\n\n"
    others = sorted({(r.get("BSSH_NM") or "").strip() for r in rows if r.get("PRDLST_NM", "").strip() == official.strip()} - {company, ""})
    if others:
        report += (f"> ⚠️ 같은 제품명이 다른 업체({', '.join(others)})에도 있습니다. 위는 그중 첫 번째입니다. "
                   "제품 뒷면의 제조사가 다르면 제품명 뒤에 업체명을 붙여 다시 조회해 주세요.\n\n")
    checks = [
        ("haccp", "HACCP 인증업체 (한국식품안전관리인증원)", "✅ 인증업체 목록에서 검색됨", "🔸 인증업체 목록에서 검색되지 않음 (미보유이거나 업체명 표기 차이일 수 있음)"),
        ("lcns_change", "인허가 변경 이력", "ℹ️ 인허가 변경 이력이 있음", "ℹ️ 인허가 변경 이력이 검색되지 않음"),
        ("inspection", "지도·단속 계획 및 실적", "ℹ️ 지도·단속 관련 기록이 검색됨", "ℹ️ 지도·단속 기록이 검색되지 않음"),
    ]
    problems = []
    for svc, label, found_msg, none_msg in checks:
        try:
            found, unknown = _dgk_union(svc, [("company", company)])
        except ApiError as e:
            problems.append(f"- {label}: {e}")
            continue
        report += f"**{label}**\n"
        if found:
            exact = [r for r in found if lcns and lcns in _row_text(r)]
            report += f"- {found_msg} ({len(found)}건" + (f", 인허가번호 일치 {len(exact)}건" if exact else "") + ")\n"
            for r in (exact or found)[:3]:
                if svc == "lcns_change" and r.get("CHNG_DT"):
                    report += f"  - {r.get('CHNG_DT')} 변경: {short(str(r.get('CHNG_BF_CN') or '-'), 20)} → {short(str(r.get('CHNG_AF_CN') or '-'), 20)} (개인정보 가림 처리됨)\n"
                else:
                    report += f"  - {row_summary(r, limit=12 if svc == 'haccp' else 8)}{_haccp_validity(r) if svc == 'haccp' else ''}\n"
        elif unknown:
            report += "- ❔ 이 항목은 업체명 검색 조건을 확인하지 못해 조회하지 못했습니다(문제가 있다는 뜻이 아님). `probe_dgk_params`로 진단할 수 있습니다.\n"
        else:
            report += f"- {none_msg}\n"
        report += "\n"
    try:
        acts = _admin_actions(company, lcns)
        report += "**행정처분·폐업 이력 (식약처 I0470)**\n"
        if acts:
            sure = [a for a in acts if a["match"] == "lcns"]
            report += (f"- {'🔴' if _admin_level(acts) == 'red' else '⚠️'} 행정처분 {len(acts)}건 검색됨"
                       + (f" (인허가번호 일치 {len(sure)}건)" if sure else " (업체명만 일치 — 동명 업체 가능)") + "\n")
            for a in (sure or acts)[:4]:
                report += f"  - {a['date']} · {a['type'] or '-'} · {short(a['violation'], 70) or '사유 미기재'} ({a['agency'] or '-'})\n"
        else:
            report += "- ✅ 행정처분·영업소폐쇄 이력이 검색되지 않음 (별도 폐업 API는 업체명 검색을 지원하지 않아 이 기록으로 대신 확인)\n"
        report += "\n"
    except ApiError as e:
        problems.append(f"- 행정처분 이력: {e}")
    if problems:
        report += "**❗ 조회 오류**\n" + "\n".join(problems) + "\n\n"
    report += "> 💡 업체명 기준 검색이라 동명 업체가 섞일 수 있습니다. 인허가번호가 일치한 건을 우선 확인하세요."
    return report


# ---------------------------------------------------------------------
# 3·4) 주의 문구 필터 (복용 약 / 몸 상태)
# ---------------------------------------------------------------------
CONDITION_KEYWORDS = {
    "임신": ["임신", "임산부", "임부", "태아"],
    "수유": ["수유"],
    "어린이": ["어린이", "소아", "영유아", "15세 이하", "12세 이하", "6세 이하"],
    "고령": ["노인", "고령"],
    "간질환": ["간질환", "간 질환", "간기능", "간 기능"],
    "신장질환": ["신장질환", "신장 질환", "신부전", "신장"],
    "고혈압": ["고혈압", "혈압"],
    "당뇨": ["당뇨", "혈당"],
    "갑상선": ["갑상선"],
    "수술": ["수술"],
    "알레르기": ["알레르기", "과민반응"],
    "카페인": ["카페인"],
    "흡연": ["흡연"],
    "위장장애": ["위장", "위궤양", "설사"],
}
MED_KEYWORDS = {
    "항응고제": ["항응고제", "와파린", "혈액응고", "혈액 응고", "항혈소판"],
    "혈압약": ["혈압강하제", "혈압약", "고혈압치료제", "항고혈압", "혈압"],
    "당뇨약": ["당뇨치료제", "혈당강하제", "인슐린", "당뇨"],
    "항생제": ["항생제", "퀴놀론", "테트라사이클린"],
    "갑상선약": ["갑상선"],
    "면역억제제": ["면역억제"],
}
# 약 이름 -> 분류 (아주 단순한 예시 매핑입니다. 필요하면 계속 추가하세요. 분류가 틀릴 수 있으니 약사 확인이 필요합니다)
DRUG_ALIAS = {
    "와파린": "항응고제", "쿠마딘": "항응고제", "엘리퀴스": "항응고제", "자렐토": "항응고제", "프라닥사": "항응고제",
    "아스피린": "항응고제", "클로피도그렐": "항응고제", "플라빅스": "항응고제",
    "암로디핀": "혈압약", "로사르탄": "혈압약", "발사르탄": "혈압약", "아테놀롤": "혈압약",
    "메트포르민": "당뇨약", "글리메피리드": "당뇨약", "인슐린": "당뇨약",
    "레보티록신": "갑상선약", "신지로이드": "갑상선약",
    "아목시실린": "항생제", "시프로플록사신": "항생제", "독시사이클린": "항생제",
    "타크로리무스": "면역억제제", "사이클로스포린": "면역억제제",
}


def split_cautions(text: str):
    """주의사항 원문 -> [(구간명, 문장)]. '[비타민 K]' 같은 구간 제목과 (1)(2)·1)2) 번호를 인식"""
    out, section = [], ""
    for line in re.split(r"[\r\n]+", text or ""):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\[(.+?)\]$", line)
        if m:
            section = m.group(1).strip()
            continue
        for part in re.split(r"\s*(?:\(\d+\)|\d+\)|\([가-힣]\)|[①-⑩])\s*", line):
            part = part.strip(" -·•")
            if part:
                out.append((section, part))
    return out


def match_cautions(text: str, keywords: list):
    return [(sec, s) for sec, s in split_cautions(text) if any(k in s for k in keywords)]


def _resolve_terms(terms_str: str, table: dict, alias: dict = None):
    """사용자 입력 -> ({분류: [입력어...]}, [분류 못한 입력어...])"""
    groups, literals = {}, []
    for tok in (t.strip() for t in re.split(r"[,\n;/]", terms_str or "")):
        if not tok:
            continue
        found = None
        for a, g in (alias or {}).items():
            if a in tok:
                found = g
                break
        if not found:
            for g, kws in table.items():
                if g in tok or any(k in tok for k in kws):
                    found = g
                    break
        if found:
            groups.setdefault(found, []).append(tok)
        else:
            literals.append(tok)
    return groups, literals


def _caution_text(item: dict) -> str:
    text = (item.get("IFTKN_ATNT_MATR_CN") or "").strip()
    if not text:
        try:
            detail = pick_detail(item.get("PRDLST_NM", ""))
            text = ((detail or {}).get("IFTKN_ATNT_MATR_CN") or "").strip()
        except ApiError:
            pass
    return text


def _caution_report(title: str, names_str: str, terms_str: str, table: dict, alias: dict, term_label: str,
                    always_targets: dict = None) -> str:
    names = [n.strip() for n in re.split(r"[,\n/+]", names_str or "") if n.strip()]
    if not names:
        return "제품명을 쉼표로 구분해 입력해 주세요."
    groups, literals = _resolve_terms(terms_str, table, alias)
    if not groups and not literals:
        return f"{term_label} 정보를 입력해 주세요. (예: {', '.join(list(table)[:4])})"

    # 분류별 검색 키워드: 분류명 + 키워드 목록 / 분류 못한 입력어는 그대로 검색
    targets = {g: list(dict.fromkeys([g] + table.get(g, []))) for g in groups}
    for lit in literals:
        targets[lit] = [lit]
    for g, kws in (always_targets or {}).items():  # 입력과 무관하게 항상 함께 보여줄 일반 문구
        targets.setdefault(g, kws)

    report = f"### {title}\n\n"
    report += f"**입력한 {term_label}:** " + ", ".join(
        [f"{', '.join(v)} → `{g}`" for g, v in groups.items()] + [f"{lit} (분류 없음, 문구 그대로 검색)" for lit in literals]
    ) + "\n\n"

    not_found, errors = [], []
    for name in names:
        try:
            item, rows = pick_product(name)
        except ApiError as e:
            errors.append(f"- `{name}`: {e}")
            continue
        if not item:
            not_found.append(name)
            continue
        official = item.get("PRDLST_NM", name)
        text = _caution_text(item)
        report += f"#### 💊 {official}\n"
        if not text:
            report += "- 허가 정보에 섭취 시 주의사항이 없습니다. 라벨을 확인하세요.\n\n"
            continue
        any_hit = False
        for g, kws in targets.items():
            hits = match_cautions(text, kws)
            if hits:
                any_hit = True
                report += f"- ⚠️ **{g}** 관련 주의 문구\n"
                for sec, s in hits:
                    report += f"  - {'[' + sec + '] ' if sec else ''}{s}\n"
        if not any_hit:
            report += "- 입력하신 항목과 직접 관련된 주의 문구는 허가 정보에 없습니다. (안전하다는 뜻은 아닙니다)\n"
        report += "\n"

    if not_found:
        report += "**정보를 찾지 못한 제품:** " + ", ".join(f"`{n}`" for n in not_found) + "\n\n"
    if errors:
        report += "**❗ 조회 오류**\n" + "\n".join(errors) + "\n\n"
    return report


@mcp.tool()
def check_drug_supplement_cautions(product_names_str: str, medications_str: str) -> str:
    """복용 중인 약(이름 또는 '항응고제', '혈압약' 같은 분류)과 관련된 영양제 주의 문구를 제품별로 찾아 보여줍니다.
    ⚠️ 의약품 병용금기(DUR) 데이터베이스 조회가 아니라, 건강기능식품 허가 정보의 '섭취 시 주의사항' 문구를 걸러 보여주는 기능입니다.
    입력 예: product_names_str='센트룸 데일리 뉴트리션 팩 30 여성 프로, 락토핏 골드', medications_str='와파린, 메트포르민'"""
    report = _caution_report("💊 복용 약 관련 주의 문구", product_names_str, medications_str, MED_KEYWORDS, DRUG_ALIAS, "복용 약",
                             always_targets={"의약품 복용 일반": ["의약품 복용", "의약품을 복용", "약물"]})
    if report.startswith("###"):
        report += ("> 💡 이 결과는 제품 허가 정보에 적힌 주의 문구를 걸러낸 것이라, 문구가 없어도 병용이 안전하다는 뜻은 아닙니다. "
                   "약 이름의 분류 매핑은 단순 예시이므로 복용 중인 약은 반드시 의사·약사와 확인하세요.")
    return report


@mcp.tool()
def check_condition_cautions(product_names_str: str, conditions_str: str) -> str:
    """임신·수유·어린이·간질환·신장질환·당뇨·고혈압 등 몸 상태와 관련된 영양제 주의 문구를 제품별로 찾아 보여줍니다.
    입력 예: product_names_str='센트룸 데일리 뉴트리션 팩 30 여성 프로', conditions_str='임신, 간질환'"""
    report = _caution_report("🩺 몸 상태 관련 주의 문구", product_names_str, conditions_str, CONDITION_KEYWORDS, None, "몸 상태")
    if report.startswith("###"):
        report += "> 💡 제품 허가 정보의 주의 문구 기준입니다. 질환이 있거나 임신·수유 중이면 섭취 전 의사·약사와 상담하세요."
    return report


# ---------------------------------------------------------------------
# 4-2) DUR 병용금기 + 연령·용량·투여기간 주의 (의약품 성분 기준)
#   약 이름 -> dur_prdlst(MATERIAL_NAME)에서 성분 추출 -> dur_ingr(병용금기, ingrKorName, 부분 일치)
#   연령/용량/기간 주의는 dur_ingr_age / dur_ingr_dose / dur_ingr_period (요청변수 ingrName)
# ---------------------------------------------------------------------
_DUR_STRIP = ("이수화물", "수화물", "무수물", "고체분산체", "제피과립", "염산염", "황산염", "질산염", "인산염", "나트륨", "칼륨", "칼슘")


def _split_terms(s: str) -> list:
    return [t.strip() for t in re.split(r"[,，、/\n]+", s or "") if t.strip()]


def _dur_name_variants(name: str) -> list:
    """'이트라코나졸고체분산체' -> ['이트라코나졸고체분산체', '이트라코나졸'] (제형·염 이름을 떼어 낸 후보)"""
    out, cur, changed = [name], name, True
    while changed:
        changed = False
        for suf in _DUR_STRIP:
            if cur.endswith(suf) and len(cur) - len(suf) >= 4:
                cur = cur[: -len(suf)]
                out.append(cur)
                changed = True
                break
    return out


def _dur_product_ingredients(name: str):
    """약 이름 -> (매칭된 품목명, [성분명], 분류) 또는 (None, [], '')"""
    q = name.replace("%", "").strip()
    tries = [q]
    lead = re.match(r"[^\d\s(]+", q)          # '타이레놀정500밀리그램' -> '타이레놀정'
    if lead and lead.group(0) != q:
        tries.append(lead.group(0))
    for t in tries:
        rows, _ = dgk_fetch("dur_prdlst", (("itemName", t),), 10)
        if rows:
            row = next((r for r in rows if str(r.get("ITEM_NAME") or "").startswith(q)), rows[0])
            ings = []
            for seg in str(row.get("MATERIAL_NAME") or "").split("/"):
                nm = seg.split(",")[0].strip()
                if nm and nm not in ings:
                    ings.append(nm)
            return str(row.get("ITEM_NAME") or ""), ings, str(row.get("CLASS_NO") or "")
    return None, [], ""


def _dur_rows(service: str, param: str, term: str, pages: int = 3) -> list:
    rows = []
    for page in range(1, pages + 1):
        got, total = dgk_fetch(service, ((param, term),), 100, page)
        rows.extend(got)
        if page * 100 >= (total or 0):
            break
    return [r for r in rows if str(r.get("DEL_YN") or "정상").strip() in ("정상", "")]


def _dur_rows_fallback(service: str, param: str, term: str):
    """이름 그대로 조회하고, 0건이면 제형·염 이름을 뗀 이름으로 다시 조회. -> (행, 실제 조회한 이름)"""
    for v in _dur_name_variants(term):
        rows = _dur_rows(service, param, v)
        if rows:
            return rows, v
    return [], term


def _name_match(h: str, b: str) -> bool:
    """사용자 쪽 성분명 h 와 DUR 행의 성분명 b 가 같은 성분인지 (한 글자 성분은 접두·접미만 허용)"""
    if not h or not b:
        return False
    if len(h) == 1:
        return b.startswith(h) or b.endswith(h)
    return h in b or b in h


def _age_applies(age_base: str, age: int):
    """'65세 이상' 같은 문구가 입력한 나이에 해당하는지. 나이를 모르거나 해석 불가면 None"""
    if not age:
        return None
    m = re.search(r"(\d+)\s*(세|개월)\s*(이하|미만|이상|초과)", age_base or "")
    if not m:
        return None
    n = int(m.group(1)) / (12 if m.group(2) == "개월" else 1)
    return {"이하": age <= n, "미만": age < n, "이상": age >= n, "초과": age > n}[m.group(3)]


def _dur_interactions(groups: list) -> list:
    """groups: [(표시 이름, 종류, [성분명])] -> 병용금기 쌍 목록 (양방향 행을 한 쌍으로 묶음)"""
    pairs = {}
    for gi, (gname, _, ings) in enumerate(groups):
        for ing in ings:
            rows, _ = _dur_rows_fallback("dur_ingr", "ingrKorName", ing)
            for r in rows:
                a, b = str(r.get("INGR_KOR_NAME") or ""), str(r.get("MIXTURE_INGR_KOR_NAME") or "")
                for gj, (hname, _, hings) in enumerate(groups):
                    if gj == gi or (groups[gi][1] == "영양소" and groups[gj][1] == "영양소"):
                        continue   # 영양소끼리의 짝은 제외 (예: 칼슘이 '에데트산칼슘나트륨'에 부분 일치해 생기는 잡음)
                    if not any(_name_match(h, b) for h in hings):
                        continue
                    key = (frozenset((gi, gj)), frozenset((a, b)))
                    rec = {"source": a, "reason": str(r.get("PROHBT_CONTENT") or "").strip().replace("\n", " "),
                           "date": str(r.get("NOTIFICATION_DATE") or "")}
                    p = pairs.setdefault(key, {"between": [gname, hname], "ingredients": [a, b], "records": []})
                    if rec not in p["records"]:
                        p["records"].append(rec)
    return list(pairs.values())


_DUR_NOTICE_SERVICES = (
    ("dur_ingr_age", "연령 금기", "AGE_BASE"),
    ("dur_ingr_dose", "용량 주의", "MAX_QTY"),
    ("dur_ingr_period", "투여기간 주의", "MAX_DOSAGE_TERM"),
    ("dur_ingr_preg", "임부 금기", "PROHBT_CONTENT"),
    ("dur_ingr_elderly", "노인 주의", "PROHBT_CONTENT"),
)


def _dur_notices(ings: list, age: int, pregnancy: bool = False) -> list:
    """약 성분별 연령 금기·용량 주의·투여기간 주의. 성분명이 정확히 같은 행만 사용(부분 일치로 다른 약이 섞이는 것을 방지)"""
    out = []
    for ing in ings:
        for svc, label, field in _DUR_NOTICE_SERVICES:
            if svc == "dur_ingr_preg" and not pregnancy:
                continue          # 임신 관련 입력이 있을 때만 조회
            if svc == "dur_ingr_elderly" and age < 65:
                continue          # 만 65세 이상일 때만 조회
            rows, used = _dur_rows_fallback(svc, "ingrName", ing)
            seen = set()
            for r in rows:
                if str(r.get("INGR_NAME") or "").strip() != used:
                    continue
                value = str(r.get(field) or "").strip()
                content = "" if field == "PROHBT_CONTENT" else str(r.get("PROHBT_CONTENT") or "").strip()
                if svc == "dur_ingr_age":
                    applies = _age_applies(value, age)
                    if applies is False:
                        continue
                else:
                    applies = None
                k = (value, content, str(r.get("FORM_NAME") or ""))
                if k in seen:
                    continue
                seen.add(k)
                out.append({"ingredient": ing, "type": label, "value": value, "content": content,
                            "form": str(r.get("FORM_NAME") or ""), "mix": str(r.get("MIX_INGR") or "").strip(),
                            "remark": str(r.get("REMARK") or "").strip(), "applies": applies})
                if sum(1 for o in out if o["ingredient"] == ing and o["type"] == label) >= 4:
                    break
    return out


def _dur_analyze(meds: list, supps: list, age: int = 0, pregnancy: bool = False) -> dict:
    """약 이름들 + 영양소 성분명들 -> {meds, supps, interactions, notices}. API 오류는 ApiError 로 전달"""
    groups, med_info, all_med_ings = [], [], []
    for m in meds:
        matched, ings, cls = _dur_product_ingredients(m)
        if matched:
            med_info.append({"input": m, "matched": matched, "ingredients": ings, "class": cls})
        else:
            ings = [m]
            med_info.append({"input": m, "matched": None, "ingredients": ings, "class": ""})
        groups.append((m, "약", ings))
        all_med_ings.extend(i for i in ings if i not in all_med_ings)
    for s in supps:
        groups.append((s, "영양소", [s]))
    interactions = _dur_interactions(groups) if len(groups) >= 2 else []
    notices = _dur_notices(all_med_ings, age, pregnancy)
    return {"meds": med_info, "supps": supps, "interactions": interactions, "notices": notices}


def _dur_markdown(d: dict, age: int = 0) -> str:
    report = "### ⚠️ DUR 병용금기·주의 조회\n\n**조회한 대상**\n"
    for m in d["meds"]:
        if m["matched"]:
            report += f"- `{m['input']}` → 품목 `{short(m['matched'], 40)}` / 성분: {', '.join(m['ingredients']) or '정보 없음'} / 분류: {m['class'] or '-'}\n"
        else:
            report += f"- `{m['input']}` → DUR 품목에서 찾지 못해 성분명으로 간주했습니다.\n"
    for s in d["supps"]:
        report += f"- `{s}` → 영양소 성분으로 조회\n"
    report += "\n"

    if len(d["meds"]) + len(d["supps"]) >= 2:
        if d["interactions"]:
            report += f"**병용금기 {len(d['interactions'])}건**\n"
            for p in d["interactions"]:
                a, b = p["ingredients"]
                report += f"- **{a}** ↔ **{b}** ({p['between'][0]} / {p['between'][1]})\n"
                for rec in p["records"]:
                    report += f"  - {rec['source']} 기준: {short(rec['reason'], 150) or '사유 미기재'}" + (f" (고시 {rec['date']})" if rec["date"] else "") + "\n"
        else:
            report += "**입력한 대상 사이에서 등록된 병용금기를 찾지 못했습니다.**\n"
        report += "\n"

    if d["notices"]:
        report += "**성분별 DUR 주의 정보**\n"
        for n in d["notices"]:
            line = f"- **{n['ingredient']}** · {n['type']}: {n['value'] or '-'}"
            if n["content"]:
                line += f" ({short(n['content'], 80)})"
            if n["form"]:
                line += f" [{n['form']}]"
            if n["mix"]:
                line += " ※복합제 조건"
            if n["applies"] is True:
                line += f" → ⚠️ 입력한 나이({age}세)에 해당"
            report += line + "\n"
        report += "\n"
    else:
        report += "**성분별 연령·용량·투여기간 주의 정보가 등록되어 있지 않습니다.**\n\n"
    report += ("> 💡 DUR에 등록된 성분 쌍·기준만 확인합니다. 결과가 없어도 안전하다는 뜻이 아니고, "
               "성분명이 DB 표기와 다르면 조회되지 않을 수 있습니다(예: 와파린은 병용금기 DB에서 0건). 복용 전 의사·약사와 확인하세요.")
    return report


@mcp.tool()
def check_drug_interactions(medications_str: str, supplement_ingredients_str: str = "", age: int = 0, pregnancy: bool = False) -> str:
    """복용 중인 약끼리, 또는 약과 영양소 성분(칼슘, 마그네슘, 철 등) 사이의 DUR 병용금기를 식약처 DUR 데이터로 조회하고,
    각 약 성분의 연령 금기·용량 주의·투여기간 주의도 함께 보여줍니다. age(만 나이)를 주면 연령 금기 중 해당하는 것만(65세 이상이면 노인 주의도), pregnancy=True 이면 임부 금기도 보여줍니다.
    medications_str: 약 이름(제품명, 예: '타이레놀, 리피토') 또는 성분명(예: '디곡신'). supplement_ingredients_str: 영양제 성분명(예: '칼슘, 마그네슘').
    ⚠️ DUR에 등록된 내용만 나옵니다. 결과가 없어도 안전하다는 뜻이 아니며, 복용 전 의사·약사와 확인하세요.
    ※ 결과를 전달할 때 도구 결과에 없는 일반 지식·추정을 덧붙이지 말고, '해야 합니다' 같은 단정 대신 도구가 낸 판정 범위 안에서만 말하세요."""
    meds, supps = _split_terms(medications_str), _split_terms(supplement_ingredients_str)
    if not meds:
        return "복용 중인 약 이름이나 성분명을 입력해 주세요. (예: '타이레놀, 디곡신')"
    try:
        return _dur_markdown(_dur_analyze(meds, supps, age, pregnancy), age)
    except ApiError as e:
        return f"공공데이터포털 오류: {e}"


# ---------------------------------------------------------------------
# 6) 종합 안전 점검 (구조화 데이터 + 요약). 이미지/차트 출력은 이 데이터를 그대로 사용합니다.
# ---------------------------------------------------------------------
_SUPP_DUR_KEYS = {"칼슘", "마그네슘", "철", "아연", "구리", "망간", "셀레늄", "요오드", "엽산", "비타민k", "비타민d", "비타민a", "비타민e"}  # DUR 병용금기 조회 대상 영양소
_LEVEL_ORDER = {"red": 3, "yellow": 2, "info": 1, "green": 0}
_LEVEL_ICON = {"red": "🔴", "yellow": "🟡", "info": "ℹ️", "green": "🟢"}


def _parse_product_names(product_names_str: str):
    names, units = [], {}
    for n in (x.strip() for x in re.split(r"[,\n/+]", product_names_str or "")):
        if not n:
            continue
        m = re.search(r"\s*[x×]\s*(\d+(?:\.\d+)?)\s*$", n, re.I)
        if m and n[:m.start()].strip():
            n = n[:m.start()].strip()
            units[n] = float(m.group(1))
        names.append(n)
    return names, units


def _collect_intake(names: list, units_by_name: dict) -> dict:
    """제품명 목록 -> 매칭 제품, 성분별 합산(표시량/최대 추정), 미매칭·오류 목록"""
    matched, not_found, errors, notes = [], [], [], []
    totals, totals_max, display = {}, {}, {}
    for name in names:
        try:
            item, rows = pick_product(name)
        except ApiError as e:
            errors.append(f"{name}: {e}")
            continue
        if not item:
            not_found.append(name)
            continue
        official = item.get("PRDLST_NM", name)
        matched.append({"input": name, "official": official, "company": (item.get("BSSH_NM") or "").strip(),
                        "lcns": (item.get("LCNS_NO") or "").strip(), "item": item, "candidates": len(rows),
                        "exact": official.strip() == name.strip()})
        try:
            detail = pick_detail(official)
        except ApiError as e:
            errors.append(f"{official} 함량 조회: {e}")
            continue
        if not detail:
            notes.append(f"{official}: 기준규격을 찾지 못해 함량을 계산하지 못했습니다.")
            continue
        amounts = extract_amounts(detail.get("STDR_STND", ""))
        if not amounts:
            notes.append(f"{official}: 기준규격에서 mg·μg 함량을 찾지 못했습니다(CFU 등은 제외).")
            continue
        mult = units_by_name[name] if name in units_by_name else servings_per_day(
            detail.get("NTK_MTHD") or item.get("NTK_MTHD", ""), first_denominator_mg(detail.get("STDR_STND", "")))[0]
        for key, (disp, label_mg, max_mg) in amounts.items():
            totals[key] = totals.get(key, 0.0) + label_mg * mult
            totals_max[key] = totals_max.get(key, 0.0) + max_mg * mult
            display.setdefault(key, disp)
    return {"matched": matched, "not_found": not_found, "errors": errors, "notes": notes,
            "totals": totals, "totals_max": totals_max, "display": display}


def _ul_rows(c: dict, sex: str, age: int) -> list:
    rows = []
    for key, label_mg in c["totals"].items():
        max_mg = max(c["totals_max"].get(key, label_mg), label_mg)
        row = {"name": c["display"][key], "label_mg": round(label_mg, 3), "max_mg": round(max_mg, 3), "ul_mg": None,
               "pct_label": None, "pct_max": None, "level": "none"}
        status, ul_mg, _ = ul_lookup(key, sex, age) if sex else ("unknown", None, "")
        if status == "ok":
            row.update(ul_mg=round(ul_mg, 3), pct_label=round(label_mg / ul_mg * 100, 1), pct_max=round(max_mg / ul_mg * 100, 1),
                       level="over" if label_mg > ul_mg else ("possible" if max_mg > ul_mg else "ok"))
        elif status == "unset":
            row["level"] = "no_ul"
        rows.append(row)
    return rows


def _manufacturer_rows(matched: list) -> list:
    out, cache = [], {}
    for p in matched:
        company = p["company"]
        if company not in cache:
            info = {"company": company, "haccp": "unknown", "haccp_expired": None, "closure": "unknown"}
            try:
                ac = _admin_actions(company, p.get("lcns", ""))
                info["closure"] = "found" if any("폐쇄" in a["type"] or "취소" in a["type"] for a in ac) else "none"
            except ApiError:
                info["closure"] = "error"
            for svc, field in (("haccp", "haccp"),):
                try:
                    rows, unknown = _dgk_union(svc, [("company", company)]) if company else ([], True)
                except ApiError:
                    info[field] = "error"
                    continue
                if rows:
                    info[field] = "found"
                    if svc == "haccp":
                        flags = [("만료" in _haccp_validity(r)) for r in rows if _haccp_validity(r)]
                        info["haccp_expired"] = (all(flags) if flags else None)
                elif not unknown:
                    info[field] = "none"
            cache[company] = info
        out.append({"product": p["official"], **cache[company]})
    return out


def _recall_rows(matched: list) -> list:
    out = []
    for p in matched:
        status = "unknown"
        count = 0
        for svc in ("recall_med", "recall_etc"):
            try:
                rows, unknown = _dgk_union(svc, [("product", p["official"])])
            except ApiError:
                continue
            if rows:
                status, count = "found", count + len(rows)
            elif not unknown and status != "found":
                status = "none"
        out.append({"product": p["official"], "status": status, "count": count})
    return out


def _caution_rows(matched: list, meds_str: str, conds_str: str) -> list:
    targets = {}
    for terms, table, alias, kind in ((meds_str, MED_KEYWORDS, DRUG_ALIAS, "복용 약"), (conds_str, CONDITION_KEYWORDS, None, "몸 상태")):
        if not (terms or "").strip():
            continue
        groups, literals = _resolve_terms(terms, table, alias)
        for g in groups:
            targets[(kind, g)] = list(dict.fromkeys([g] + table.get(g, [])))
        for lit in literals:
            targets[(kind, lit)] = [lit]
    if (meds_str or "").strip():   # 약 이름이 분류로 매핑되지 않아도 '의약품 복용' 일반 주의 문구는 항상 함께 확인
        targets.setdefault(("복용 약", "의약품 복용 일반"), ["의약품 복용", "의약품을 복용", "약물"])
    out = []
    if not targets:
        return out
    for p in matched:
        text = _caution_text(p["item"])
        for (kind, g), kws in targets.items():
            hits = match_cautions(text, kws)
            if hits:
                out.append({"product": p["official"], "kind": kind, "topic": g, "sentences": [s for _, s in hits][:4]})
    return out


@_nutri_tool
def full_safety_check(product_names_str: str, sex: str = "", age: int = 0, medications_str: str = "", conditions_str: str = "") -> str:
    """[필독] 영양제 종합 안전 점검. 제품명(여러 개)과 성별·나이, 복용 약, 몸 상태를 한 번에 받아
    ① 성분별 하루 합산량과 상한섭취량(UL) 비교 ② 제조업체 HACCP·폐업 확인 ③ 회수 이력(참고) ④ 복용 약과의 DUR 병용금기·연령/용량/기간 주의
    ⑤ 허가 정보 주의 문구(복용 약·몸 상태)를 점검해 🔴🟡🟢 등급으로 요약합니다.
    결과 끝의 ```json 블록은 차트·카드 같은 이미지 시각화용 구조화 데이터입니다. 차트를 그릴 수 있는 환경이면 intake(UL 대비 %) 막대 차트를 함께 그려 주세요.
    제품명 뒤에 'x2'를 붙이면 하루 섭취 단위 수를 직접 지정합니다. sex 는 '남'/'여', age 는 만 19세 이상 정수.
    입력 예: product_names_str='센트룸 데일리 뉴트리션 팩 30 여성 프로, 속편한 고려은단 비타민C1000 x2', sex='여', age=34, medications_str='디곡신', conditions_str='임신 준비'
    ※ 결과를 전달할 때 도구 결과에 없는 일반 지식·추정을 덧붙이지 말고, '해야 합니다' 같은 단정 대신 도구가 낸 판정 범위 안에서만 말하세요."""
    names, units = _parse_product_names(product_names_str)
    if not names:
        return "점검할 영양제 이름을 쉼표로 구분해 입력해 주세요."
    findings = []

    def add(level, category, title):
        findings.append({"level": level, "category": category, "title": title})

    c = _collect_intake(names, units)
    if not c["matched"]:
        detail = "\n".join(f"- {e}" for e in c["errors"]) or "제품명을 확인해 주세요."
        return f"❌ 식약처 허가 정보에서 제품을 찾지 못했습니다.\n{detail}"
    for n in c["not_found"]:
        add("yellow", "제품", f"'{n}' 허가 정보를 찾지 못해 점검에서 제외됨")
    for e in c["errors"]:
        add("yellow", "조회 오류", e)
    for n in c["notes"]:
        add("info", "함량", n)
    for p in c["matched"]:
        if not p["exact"] and p["candidates"] > 1:
            add("info", "제품", f"'{p['input']}'은 후보 {p['candidates']}개 중 첫 번째({p['official']})로 매칭됨 — 정확한 제품명 확인 권장")

    # ① 섭취량 / UL
    s, profile_msg = validate_profile(sex, age) if (sex or age) else (None, "성별·나이를 입력하면 UL 비교를 합니다.")
    ul = _ul_rows(c, s, age) if c["totals"] else []
    if not s:
        add("info", "섭취량", profile_msg)
    for r in ul:
        if r["level"] == "over":
            add("red", "섭취량", f"{r['name']} {fmt_auto(r['label_mg'])} — UL({fmt_auto(r['ul_mg'])})의 {r['pct_label']:.0f}% (초과)")
        elif r["level"] == "possible":
            add("yellow", "섭취량", f"{r['name']} 표시량 {fmt_auto(r['label_mg'])}, 최대 추정 시 UL의 {r['pct_max']:.0f}%")
    dup = {}
    for p in c["matched"]:
        for ing in parse_ingredients(p["item"].get("RAWMTRL_NM", "")):
            dup.setdefault(ing, []).append(p["official"])
    dup = {k: v for k, v in dup.items() if len(v) > 1}
    for ing, plist in dup.items():
        add("info", "중복", f"'{ing}' 원재료가 {len(plist)}개 제품에 중복")

    # ② 제조업체 ③ 회수
    manu = _manufacturer_rows(c["matched"])
    for m in manu:
        if m["haccp"] == "none":
            add("info", "제조업체", f"{m['company']}: HACCP 인증업체 목록에서 검색되지 않음")
        elif m["haccp"] == "found" and m["haccp_expired"]:
            add("yellow", "제조업체", f"{m['company']}: HACCP 인증기간 만료로 표시됨")
    admin_out = []
    for p in c["matched"]:
        try:
            acts = _admin_actions(p["company"], str(p["item"].get("LCNS_NO") or "").strip())
        except ApiError:
            continue
        if acts:
            lvl = _admin_level(acts)
            admin_out.append({"product": p["official"], "company": p["company"], "level": lvl, "count": len(acts), "actions": acts[:3]})
            a0 = acts[0]
            sure = any(a["match"] == "lcns" for a in acts)
            add("red" if lvl == "red" else "yellow", "행정처분",
                f"{p['company']}: {a0['date']} {a0['type']} 등 {len(acts)}건" + ("" if sure else " (업체명만 일치, 동명 업체 가능)"))
    overseas_out = []
    for n in names:
        try:
            o = _scan_overseas(n, [], "")
        except ApiError:
            break
        if o["product_hits"]:
            overseas_out.append(o)
            act = any(h["active"] for h in o["product_hits"])
            add("red" if act else "yellow", "해외직구 차단", f"'{n}': 해외직구 위해식품 차단 목록과 제품명 일치 {len(o['product_hits'])}건")
    recall = _recall_rows(c["matched"])
    for r in recall:
        if r["status"] == "found":
            add("yellow", "회수", f"{r['product']}: 회수·판매중지 검색 {r['count']}건 (동명 제품 여부 확인)")

    # ④ 복용 약 DUR
    dur = None
    meds = _split_terms(medications_str)
    if meds:
        supp_terms = [d.replace(" ", "") for k, d in c["display"].items() if k in _SUPP_DUR_KEYS]
        try:
            pregnancy = any(k in (conditions_str or "") for k in ("임신", "임부"))
            dur = _dur_analyze(meds, list(dict.fromkeys(supp_terms)), age, pregnancy)
            for p in dur["interactions"]:
                a, b = p["ingredients"]
                add("red", "병용금기", f"{a} ↔ {b} ({p['between'][0]} / {p['between'][1]}): {short(p['records'][0]['reason'], 60) or '사유 미기재'}")
            for n in dur["notices"]:
                if n["type"] == "연령 금기" and n["applies"] is True:
                    add("red", "DUR 연령 금기", f"{n['ingredient']}: {n['value']} — 입력 나이에 해당")
                elif n["type"] == "임부 금기":
                    add("red", "DUR 임부 금기", f"{n['ingredient']}: {short(n['value'], 60) or '임부 금기 등록'}")
                elif n["type"] == "노인 주의":
                    add("yellow", "DUR 노인 주의", f"{n['ingredient']}: {short(n['value'], 60) or '노인 주의 등록'}")
                elif n["type"] == "용량 주의":
                    add("info", "DUR 용량 주의", f"{n['ingredient']}: {n['value']}")
                elif n["type"] == "투여기간 주의":
                    add("info", "DUR 투여기간 주의", f"{n['ingredient']}: {n['value']}")
            for m in dur["meds"]:
                if not m["matched"]:
                    add("info", "약", f"'{m['input']}'은 DUR 품목에서 찾지 못해 성분명으로 조회")
        except ApiError as e:
            add("yellow", "조회 오류", f"DUR 조회 실패: {e}")

    # ⑤ 허가 정보 주의 문구
    cautions = _caution_rows(c["matched"], medications_str, conditions_str)
    for r in cautions:
        add("yellow", "주의 문구", f"{r['product']} · {r['kind']} '{r['topic']}': {short(r['sentences'][0], 70)}")

    findings.sort(key=lambda f: -_LEVEL_ORDER[f["level"]])
    overall = "red" if any(f["level"] == "red" for f in findings) else ("yellow" if any(f["level"] == "yellow" for f in findings) else "green")
    heads = {"red": "🔴 주의가 필요한 항목이 있습니다", "yellow": "🟡 확인이 필요한 항목이 있습니다", "green": "🟢 확인된 문제는 없습니다 (안전 보장은 아님)"}

    report = f"### 🛡️ 영양제 종합 안전 점검\n\n**종합: {heads[overall]}**\n\n"
    report += "**점검한 제품:** " + ", ".join(f"`{p['official']}`" for p in c["matched"])
    report += f"\n**기준:** {('남' if s == 'M' else '여') + ' ' + str(age) + '세' if s else '성별·나이 미입력'}"
    report += f" / 복용 약: {', '.join(meds) or '없음'} / 몸 상태: {conditions_str.strip() or '없음'}\n\n"
    if findings:
        report += "**점검 결과**\n"
        for f in findings:
            report += f"- {_LEVEL_ICON[f['level']]} [{f['category']}] {f['title']}\n"
        report += "\n"
    if ul:
        report += "**성분별 하루 합산량**\n\n| 성분 | 표시량 합산 | 최대 추정 | UL | UL 대비(표시량/최대) | 판정 |\n| :--- | ---: | ---: | ---: | ---: | :---: |\n"
        verdict = {"over": "🔴 UL 초과", "possible": "🟡 최대 시 초과 가능", "ok": "🟢 UL 이하", "no_ul": "UL 미설정", "none": "-"}
        for r in ul:
            report += (f"| {r['name']} | {fmt_auto(r['label_mg'])} | {fmt_auto(r['max_mg'])} | "
                       f"{fmt_auto(r['ul_mg']) if r['ul_mg'] else '-'} | "
                       f"{(_bar(r['pct_label']) + ' %.0f%% / %.0f%%' % (r['pct_label'], r['pct_max'])) if r['pct_label'] is not None else '-'} | {verdict[r['level']]} |\n")
        report += "\n"
    report += ("> 💡 점검 범위: UL 비교(음식 섭취분 제외), HACCP·폐업(업체명 기준), 회수(의약품·의약외품 대상이라 건강기능식품은 빠질 수 있음), "
               "DUR(등록된 성분 쌍만), 허가 문구(문구가 없어도 안전하다는 뜻이 아님). 복용 중인 약이나 질환이 있으면 반드시 의사·약사와 확인하세요.\n\n")

    data = {
        "overall": overall,
        "profile": {"sex": s, "age": age if s else None, "medications": meds, "conditions": _split_terms(conditions_str)},
        "products": [{"name": p["official"], "company": p["company"]} for p in c["matched"]],
        "intake": ul,
        "manufacturers": manu,
        "recalls": recall,
        "admin_actions": admin_out,
        "overseas_hits": [{"query": o["query"], "hits": o["product_hits"][:3]} for o in overseas_out],
        "interactions": (dur or {}).get("interactions", []),
        "dur_notices": (dur or {}).get("notices", []),
        "cautions": cautions,
        "findings": findings,
    }
    return report + "```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


# ---------------------------------------------------------------------
# 7) 권장섭취량 대비 보충제 기여도 / 부족 성분 분석
#   출처: 보건복지부 '2025 한국인 영양소 섭취기준 요약표' (2025.12.29) 의 성인(19세 이상) 비타민·무기질 값
#   형식: 키 -> (단위, 기준종류, 남 평균필요량, 남 권장/충분, 여 평균필요량, 여 권장/충분)
#         값은 숫자 하나(전 연령 동일) 또는 5개 리스트(19-29, 30-49, 50-64, 65-74, 75세 이상). 평균필요량이 없으면 None
#   기준종류: 'RNI'=권장섭취량, 'AI'=충분섭취량 (AI는 '부족 판정' 기준이 아니라 충분하다고 보는 수준)
#   임신부·수유부 부가량은 반영하지 않았습니다.
# ---------------------------------------------------------------------
DRI_TABLE = {
    "비타민a": ("μg", "RNI", [570, 560, 530, 520, 500], [800, 800, 750, 700, 700], [460, 450, 430, 410, 410], [650, 650, 600, 600, 600]),
    "비타민d": ("μg", "AI", None, [10, 10, 10, 15, 15], None, [10, 10, 10, 15, 15]),
    "비타민e": ("mg", "AI", None, 12, None, 12),
    "비타민k": ("μg", "AI", None, 75, None, 65),
    "비타민c": ("mg", "RNI", 75, 100, 75, 100),
    "티아민": ("mg", "RNI", [1.0, 1.0, 1.0, 0.9, 0.9], [1.2, 1.2, 1.2, 1.1, 1.1], [0.9, 0.9, 0.9, 0.8, 0.7], [1.1, 1.1, 1.1, 1.0, 0.8]),
    "리보플라빈": ("mg", "RNI", [1.3, 1.3, 1.3, 1.2, 1.1], [1.5, 1.5, 1.5, 1.4, 1.4], [1.0, 1.0, 1.0, 0.9, 0.8], [1.2, 1.2, 1.2, 1.1, 1.0]),
    "나이아신": ("mg", "RNI", [12, 12, 12, 11, 10], [14, 14, 14, 13, 12], [11, 11, 11, 10, 9], [13, 13, 13, 12, 11]),
    "비타민b6": ("mg", "RNI", 1.3, 1.5, 1.2, 1.4),
    "엽산": ("μg", "RNI", 320, 400, 320, 400),
    "비타민b12": ("μg", "RNI", 2.0, 2.4, 2.0, 2.4),
    "판토텐산": ("mg", "AI", None, 5.0, None, 5.0),
    "비오틴": ("μg", "AI", None, 30, None, 30),
    "콜린": ("mg", "AI", None, 480, None, 390),
    "칼슘": ("mg", "RNI", 650, 800, [500, 500, 650, 600, 600], [650, 650, 750, 750, 750]),
    "인": ("mg", "RNI", 540, 650, 540, 650),
    "마그네슘": ("mg", "RNI", [300, 310, 310, 310, 310], [360, 380, 380, 380, 380], 240, 280),
    "철": ("mg", "RNI", [6, 6, 6, 6, 5], [8, 8, 8, 8, 7], [7, 7, 5, 4, 4], [12, 12, 7, 6, 6]),
    "아연": ("mg", "RNI", [9, 8, 8, 8, 7], [10, 10, 10, 9, 9], [7, 7, 6, 6, 6], [8, 8, 8, 7, 7]),
    "구리": ("μg", "RNI", [650, 650, 650, 600, 600], [850, 850, 850, 800, 800], [500, 500, 500, 460, 460], [650, 650, 650, 600, 600]),
    "불소": ("mg", "AI", None, [3.5, 3.4, 3.2, 3.1, 3.0], None, [2.8, 2.7, 2.6, 2.5, 2.3]),
    "망간": ("mg", "AI", None, 4.0, None, 3.5),
    "요오드": ("μg", "RNI", 100, 150, 100, 150),
    "셀레늄": ("μg", "RNI", 50, 60, 50, 60),
    "몰리브덴": ("μg", "RNI", [25, 25, 25, 25, 20], [30, 30, 30, 30, 25], [20, 20, 20, 20, 15], [25, 25, 25, 25, 20]),
    "크롬": ("μg", "AI", None, [30, 30, 30, 25, 25], None, 20),
}
_DRI_ALIASES = {"비타민b1": "티아민", "비타민b2": "리보플라빈", "니코틴산": "나이아신", "니코틴아미드": "나이아신",
                "폴산": "엽산", "비타민b9": "엽산", "비타민b5": "판토텐산", "비타민h": "비오틴"}


def _dri_key(key: str) -> str:
    return _DRI_ALIASES.get(key, key)


def _pretty_nutrient(key: str) -> str:
    return re.sub(r"(비타민)([a-z])", lambda m: m.group(1) + m.group(2).upper(), key)


def _dri_pick(v, i):
    if v is None:
        return None
    return v[i] if isinstance(v, list) else v


def dri_lookup(key: str, sex: str, age: int):
    """-> {'kind', 'ear_mg', 'value_mg'} 또는 None (표에 없는 성분)"""
    e = DRI_TABLE.get(_dri_key(key))
    if not e or age_band_index(age) is None:
        return None
    unit, kind, m_ear, m_val, f_ear, f_val = e
    i = age_band_index(age)
    ear, val = (m_ear, m_val) if sex == "M" else (f_ear, f_val)
    factor = UNIT_TO_MG[unit.lower()]
    ear_v = _dri_pick(ear, i)
    return {"kind": kind, "ear_mg": ear_v * factor if ear_v is not None else None, "value_mg": _dri_pick(val, i) * factor}


def _add_intake(target: dict, intake_str: str, skipped: list):
    for name, mg, raw in _parse_intake(intake_str):
        if mg is None:
            skipped.append(raw)
            continue
        k = _dri_key(canon(name))
        target[k] = target.get(k, 0.0) + mg


@_nutri_tool
def check_nutrient_adequacy(product_names_str: str = "", sex: str = "", age: int = 0,
                            diet_intake_str: str = "", supplement_intake_str: str = "") -> str:
    """영양제가 한국인 영양소 섭취기준(2025)의 권장섭취량(또는 충분섭취량)을 얼마나 채우는지, 어떤 영양소는 영양제에 없는지 보여줍니다.
    product_names_str: 영양제 제품명(여러 개, 'x2' 가능) / supplement_intake_str: 제품 대신 라벨 함량 직접 입력(예: '비타민C 500mg, 철 12mg').
    diet_intake_str(선택): 식사로 섭취하는 양을 알면 입력(예: '칼슘 500mg, 철 8mg'). 있으면 식사+영양제 합계로 '충족/미달/부족 가능'을 판정합니다.
    sex('남'/'여')와 age(만 19세 이상 정수)는 필수입니다. 식사량을 모르면 '영양제가 권장량의 몇 %를 채우는지'까지만 알려주며, 부족 여부는 단정하지 않습니다.
    결과 끝의 ```json 블록은 차트 같은 이미지 시각화용 데이터입니다. 차트를 그릴 수 있는 환경이면 nutrients(pct_supp, pct_total) 막대 차트를 함께 그려 주세요.
    ※ 결과를 전달할 때 도구 결과에 없는 일반 지식·추정을 덧붙이지 말고, '해야 합니다' 같은 단정 대신 도구가 낸 판정 범위 안에서만 말하세요."""
    s, msg = validate_profile(sex, age)
    if not s:
        return msg
    if not (product_names_str or "").strip() and not (supplement_intake_str or "").strip():
        return "영양제 제품명(product_names_str) 또는 함량(supplement_intake_str)을 입력해 주세요."

    supp, skipped, notes, matched_names = {}, [], [], []
    display = {}
    if (product_names_str or "").strip():
        names, units = _parse_product_names(product_names_str)
        c = _collect_intake(names, units)
        for p in c["matched"]:
            matched_names.append(p["official"])
        for key, mg in c["totals"].items():
            k = _dri_key(key)
            supp[k] = supp.get(k, 0.0) + mg
            display.setdefault(k, c["display"][key])
        notes += [f"'{n}' 제품을 찾지 못했습니다." for n in c["not_found"]] + c["errors"] + c["notes"]
    if (supplement_intake_str or "").strip():
        _add_intake(supp, supplement_intake_str, skipped)
    diet = {}
    _add_intake(diet, diet_intake_str or "", skipped)
    use_diet = bool(diet)

    rows, missing = [], []
    for key in DRI_TABLE:
        ref = dri_lookup(key, s, age)
        supp_mg, diet_mg = supp.get(key), diet.get(key)
        if supp_mg is None and diet_mg is None:
            missing.append(key)
            continue
        total = (supp_mg or 0.0) + (diet_mg or 0.0)
        value, ear = ref["value_mg"], ref["ear_mg"]
        status = "supp_only"
        if use_diet and diet_mg is not None:
            if total >= value:
                status = "enough"
            elif ref["kind"] == "AI":
                status = "below_ai"
            elif ear is not None and total >= ear:
                status = "below_rni"
            else:
                status = "low"
        st_ul, ul_mg, _ = ul_lookup(key, s, age)
        rows.append({
            "name": display.get(key) or _pretty_nutrient(key), "kind": ref["kind"], "ref_mg": round(value, 4),
            "ear_mg": round(ear, 4) if ear is not None else None,
            "supp_mg": round(supp_mg, 4) if supp_mg is not None else None,
            "diet_mg": round(diet_mg, 4) if diet_mg is not None else None,
            "pct_supp": round((supp_mg or 0.0) / value * 100, 1), "pct_total": round(total / value * 100, 1),
            "ul_mg": round(ul_mg, 4) if st_ul == "ok" else None,
            "over_ul": bool(st_ul == "ok" and (supp_mg or 0.0) > ul_mg), "status": status,
        })

    unknown_nutrients = [display.get(k) or k for k in supp if k not in DRI_TABLE]
    label = {"RNI": "권장", "AI": "충분"}
    verdict = {"supp_only": "-", "enough": "🟢 충족", "below_rni": "🟡 권장 미달(평균필요량 이상)",
               "below_ai": "🟡 충분섭취량 미달", "low": "🔴 평균필요량 미만(부족 가능)"}
    report = f"### 🥗 권장섭취량 대비 분석 ({'남' if s == 'M' else '여'} {age}세)\n\n"
    if matched_names:
        report += "**점검한 제품:** " + ", ".join(f"`{n}`" for n in matched_names) + "\n\n"
    if rows:
        report += "| 영양소 | 기준 | 영양제 | 식사 | 영양제 기여율 | 합계 대비 | 판정 |\n| :--- | ---: | ---: | ---: | ---: | ---: | :---: |\n"
        for r in rows:
            report += (f"| {r['name']} | {label[r['kind']]} {fmt_auto(r['ref_mg'])} | "
                       f"{fmt_auto(r['supp_mg']) if r['supp_mg'] is not None else '-'}"
                       f"{' ⚠️UL초과' if r['over_ul'] else ''} | {fmt_auto(r['diet_mg']) if r['diet_mg'] is not None else '-'} | "
                       f"{_bar(r['pct_supp'])} {r['pct_supp']:.0f}% | {(_bar(r['pct_total']) + ' %.0f%%' % r['pct_total']) if r['diet_mg'] is not None else '-'} | {verdict[r['status']]} |\n")
        report += "\n"
    else:
        report += "자동으로 계산된 영양소 함량이 없습니다. 라벨 함량을 `supplement_intake_str`에 직접 입력해 보세요.\n\n"
    if missing:
        report += "**이 영양제 조합에 포함되지 않은 영양소(기준표 기준):** " + ", ".join(_pretty_nutrient(k) for k in missing) + "\n"
        report += "※ 식사로 충분히 섭취하면 영양제에 없어도 문제가 아닙니다. 영양제에 없다는 것이 부족하다는 뜻은 아닙니다.\n\n"
    if unknown_nutrients:
        report += "**기준표에 없는 성분(비교 제외):** " + ", ".join(unknown_nutrients) + "\n\n"
    if skipped:
        report += "**해석하지 못한 입력:** " + ", ".join(skipped) + "\n\n"
    if notes:
        report += "**참고:** " + " / ".join(notes) + "\n\n"
    report += ("> 💡 기준: 보건복지부 2025 한국인 영양소 섭취기준(성인 19세 이상, 임신·수유 제외). 권장섭취량은 건강한 사람 대부분(97.5%)의 필요를 채우는 수준, "
               "충분섭취량은 평균필요량 근거가 부족할 때의 목표 수준입니다. "
               + ("식사량을 입력하지 않아 '부족 여부'는 판정하지 않았습니다. 식사로 섭취하는 양을 알면 `diet_intake_str`로 함께 입력하세요. " if not use_diet else "")
               + "함량은 제품 기준규격 표시량 기준이며, IU 단위 성분은 자동 환산하지 않습니다.")
    data = {"profile": {"sex": s, "age": age}, "used_diet": use_diet, "nutrients": rows, "missing": [_pretty_nutrient(k) for k in missing]}
    return report + "\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


# ---------------------------------------------------------------------
# 8) 유효량 체크: 제품의 1일 함량이 식약처 인정 1일 섭취량(하한~상한)에 미치는가
#    기존 기능은 '과다'를, 이 기능은 '돈 낸 만큼 들어 있나(과소)'를 확인합니다.
#    출처: 제품 함량 = I0030(기준규격) / 인정 기준 = I2710(품목분류), I-0050(개별인정형)
# ---------------------------------------------------------------------
def _find_effective_limit(key: str):
    """성분의 식약처 인정 1일 섭취량. -> dict(low, up, source, kind, name, memo) / None / {'error': ...}
    kind: 'exact'(이름 일치), 'partial'(복합 원료 등 이름 일부 일치 - 참고용), 'indiv'(I-0050 개별인정형)"""
    rows = fetch_service_rows(LIMIT_SERVICE_ID)
    exact, partial = None, None
    for r in rows:
        names = [canon(str(r.get(f) or "")) for f in LIMIT_NAME_FIELDS]
        if key in names:
            exact = r
            break
        if partial is None and len(key) >= 2 and any(n and key in n for n in names):
            partial = r
    row = exact or partial
    if row:
        factor = unit_factor(str(row.get("INTK_UNIT") or "mg"))
        if factor is None:
            return {"error": f"기준 단위 `{row.get('INTK_UNIT')}` 자동 환산 불가"}
        low, up = _to_float(row.get("DAY_INTK_LOWLIMIT")), _to_float(row.get("DAY_INTK_HIGHLIMIT"))
        return {"low": low * factor if low is not None else None, "up": up * factor if up is not None else None,
                "source": LIMIT_SERVICE_ID, "kind": "exact" if exact else "partial",
                "name": str(row.get("PRDCT_NM") or row.get("SKLL_IX_IRDNT_RAWMTRL") or "").strip(),
                "memo": str(row.get("INTK_MEMO") or "").strip()}
    ind = _find_in_individual(key)
    if ind:
        return {"low": ind["low"], "up": ind["up"], "source": INDIV_SERVICE_ID, "kind": "indiv", "name": "", "memo": ""}
    return None


def _effective_status(amount_mg: float, lim: dict) -> str:
    low, up = lim["low"], lim["up"]
    if low is not None and amount_mg < low * 0.995:
        s = "under"
    elif up is not None and amount_mg > up * 1.005:
        s = "over"
    else:
        s = "ok"
    return ("ref_" + s) if lim["kind"] == "partial" else s


_EFF_ICON = {"under": "🔴 인정 하한 미달", "ok": "🟢 인정 범위", "over": "🟡 인정 상한 초과",
             "ref_under": "🔶 참고: 하한 미달(복합 기준)", "ref_ok": "🔶 참고: 범위 내(복합 기준)", "ref_over": "🔶 참고: 상한 초과(복합 기준)"}


@_nutri_tool
def check_effective_dose(product_names_str: str) -> str:
    """영양제의 1일 함량이 식약처가 인정한 기능성 1일 섭취량(하한~상한)에 미치는지 확인합니다. '이 루테인 제품, 효과 볼 만큼 들어 있어?'에 답합니다.
    제품명을 쉼표로 구분해 여러 개 입력할 수 있고, 'x2'를 붙이면 하루 섭취 단위 수를 직접 지정합니다.
    ⚠️ '미달'은 식약처 인정 기준(하한)보다 적다는 뜻이며 효과가 없다는 판정이 아닙니다. 성분명이 복합 원료의 일부만 일치하면 참고용(🔶)으로 표시합니다.
    결과 끝의 ```json 블록은 차트용 데이터입니다.
    ※ 결과를 전달할 때 도구 결과에 없는 일반 지식·추정을 덧붙이지 말고, '해야 합니다' 같은 단정 대신 도구가 낸 판정 범위 안에서만 말하세요."""
    names, units = _parse_product_names(product_names_str)
    if not names:
        return "점검할 영양제 이름을 쉼표로 구분해 입력해 주세요. (예: 루테인 지아잔틴 ...)"

    report, effective, problems = "", [], []
    for name in names:
        try:
            item, rows = pick_product(name)
        except ApiError as e:
            problems.append(f"`{name}`: {e}")
            continue
        if not item:
            problems.append(f"`{name}`: 허가 정보를 찾지 못했습니다. `search_nutri_product`로 정확한 제품명을 확인해 주세요.")
            continue
        official = item.get("PRDLST_NM", name)
        try:
            detail = pick_detail(official)
        except ApiError as e:
            problems.append(f"`{official}`: 기준규격 조회 오류 {e}")
            continue
        if not detail:
            problems.append(f"`{official}`: 품목제조 신고사항(I0030)에서 정확히 일치하는 제품이 없어 함량을 계산하지 못했습니다.")
            continue
        stdr = detail.get("STDR_STND", "")
        amounts = extract_amounts(stdr)
        if not amounts:
            problems.append(f"`{official}`: 기준규격에서 mg·μg 단위 함량을 찾지 못했습니다(CFU 등은 제외).")
            continue
        ntk = detail.get("NTK_MTHD") or item.get("NTK_MTHD", "")
        if name in units:
            mult, how = units[name], "직접 지정"
        else:
            mult, parsed = servings_per_day(ntk, first_denominator_mg(stdr))
            how = "섭취방법 문구 기준" if parsed else "섭취량 문구 해석 실패로 1회분 기준"

        out_rows, nobasis = [], []
        for key, (disp, label_mg, max_mg) in amounts.items():
            day_mg = label_mg * mult
            try:
                lim = _find_effective_limit(key)
            except ApiError as e:
                problems.append(f"`{official}` {disp}: 기준 조회 오류 {e}")
                continue
            if lim is None or "error" in lim:
                nobasis.append(disp if lim is None else f"{disp}({lim['error']})")
                continue
            st = _effective_status(day_mg, lim)
            pct_low = round(day_mg / lim["low"] * 100, 1) if lim["low"] else None
            out_rows.append({"name": disp, "day_mg": round(day_mg, 4), "low_mg": lim["low"], "up_mg": lim["up"], "status": st,
                             "pct_of_low": pct_low, "source": lim["source"], "match": lim["kind"], "basis_name": lim["name"], "memo": lim["memo"]})
        order = {"under": 0, "over": 1, "ok": 2, "ref_under": 3, "ref_over": 4, "ref_ok": 5}
        out_rows.sort(key=lambda r: order[r["status"]])
        effective.append({"product": official, "units_per_day": mult, "rows": out_rows, "no_basis": nobasis})

        report += f"### 🎯 유효량 체크: **{official}** (1일 ×{mult:g}, {how})\n\n"
        func = (detail.get("PRIMARY_FNCLTY") or item.get("PRIMARY_FNCLTY") or "").strip().replace("\r", "").split("\n")[0]
        if func:
            report += f"- 인정 기능성: {short(func, 90)}\n"
        if ntk:
            report += f"- 섭취방법: {short(ntk, 100)}\n"
        report += "\n"
        if out_rows:
            report += "| 성분 | 1일 함량 | 인정 1일 섭취량 | 하한 대비 | 판정 | 출처 |\n| :--- | ---: | ---: | :---: | :---: | :---: |\n"
            for r in out_rows:
                rng = (f"{fmt_auto(r['low_mg'])}~{fmt_auto(r['up_mg'])}" if r["low_mg"] is not None and r["up_mg"] is not None
                       else (f"최대 {fmt_auto(r['up_mg'])}" if r["up_mg"] is not None else f"최소 {fmt_auto(r['low_mg'])}"))
                bar = f"{_bar(r['pct_of_low'])} {r['pct_of_low']:.0f}%" if r["pct_of_low"] is not None else "-"
                report += f"| {r['name']} | {fmt_auto(r['day_mg'])} | {rng} | {bar} | {_EFF_ICON[r['status']]} | {r['source']} |\n"
            report += "\n"
            notes = [f"{r['name']}: {short(r['memo'], 80)}" for r in out_rows if r["memo"]]
            if any(r["match"] == "partial" for r in out_rows):
                notes.append("🔶 표시는 성분명이 복합 원료 기준의 일부와 일치한 경우라 참고용입니다(예: 지아잔틴은 루테인지아잔틴 기준).")
            if notes:
                report += "**기준 메모:**\n" + "".join(f"- {n}\n" for n in notes) + "\n"
        else:
            report += "비교할 수 있는 인정 기준을 찾지 못했습니다.\n\n"
        if nobasis:
            report += f"**기준을 찾지 못한 성분:** {', '.join(nobasis)}\n\n"

    if problems:
        report += "**❗ 확인하지 못한 항목**\n" + "".join(f"- {p}\n" for p in problems) + "\n"
    if not effective:
        return report or "점검 결과가 없습니다."
    report += ("> 💡 출처: 제품 함량 = 식약처 품목제조 신고사항(I0030) 기준규격의 표시량, 인정 1일 섭취량 = 품목분류정보(I2710)·개별인정형(I-0050). "
               "'미달'은 인정 기준보다 적다는 뜻이며 효과가 없다는 판정이 아닙니다. 과다 섭취 여부는 `full_safety_check`의 UL 비교를 함께 확인하세요.")
    return report + "\n\n```json\n" + json.dumps({"effective": effective}, ensure_ascii=False) + "\n```"


# ---------------------------------------------------------------------
# 9) 해외직구 안전 스캐너: 식약처 '해외직구 위해식품 차단정보'(I2715)와 대조
#    제품명·바코드는 제품 단위 일치, 성분은 '차단된 제품에 쓰인 성분인지'(참고)만 알려 줍니다.
# ---------------------------------------------------------------------
OVERSEAS_SERVICE_ID = os.environ.get("OVERSEAS_SERVICE_ID", "I2715")
ADMIN_SERVICE_ID = os.environ.get("ADMIN_SERVICE_ID", "I0470")

# 한글 성분명 -> 차단 목록에 자주 나오는 영문 표기 (일부 예시이며 영문으로 직접 입력해도 됩니다)
KR_EN_INGREDIENTS = {
    "요힘빈": ["yohimbine", "yohimbe"], "실데나필": ["sildenafil"], "타다라필": ["tadalafil"], "바데나필": ["vardenafil"],
    "시부트라민": ["sibutramine"], "멜라토닌": ["melatonin"], "센노사이드": ["sennoside"], "센나": ["senna", "sennoside"],
    "카스카라사그라다": ["cascara sagrada"], "시네프린": ["synephrine"], "에페드린": ["ephedrine", "ephedra"], "마황": ["ephedra"],
    "5-htp": ["5-htp", "5-hydroxytryptophan"], "카바": ["kava"], "크라톰": ["kratom", "mitragynine"],
    "음양곽": ["horny goat weed", "epimedium", "icarine", "icariin"], "이카린": ["icarine", "icariin"],
    "블랙코호시": ["black cohosh"], "바코파": ["bacopa"], "무이라푸아마": ["muira puama"], "구글": ["guggul"],
    "에키네시아": ["echinacea"], "시트룰린": ["l-citrulline", "citrulline"], "dhea": ["dhea"], "cbd": ["cbd", "cannabidiol"],
    "대마": ["hemp", "cannabis"], "빈포세틴": ["vinpocetine"], "페놀프탈레인": ["phenolphthalein", "phenolphthaleine"],
    "테오브로민": ["theobromine"], "리튬": ["lithium"], "프로게스테론": ["progesterone"], "프레그네놀론": ["pregnenolone"],
    "골든씰": ["goldenseal"], "카타우바": ["catuaba"], "짐네마": ["gymnema"], "은": ["은(silver)", "silver"],
    "젤라틴": ["우피 유래 원료"], "우피": ["우피 유래 원료"],
}


def _norm_brand(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣ぁ-んァ-ン一-龥]+", "", str(s or "").lower())


def _parse_ymd(s: str):
    import datetime
    m = re.match(r"(\d{4})(\d{2})(\d{2})", str(s or "").strip())
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _overseas_rows() -> tuple:
    return fetch_service_rows(OVERSEAS_SERVICE_ID, "", 10000)


@lru_cache(maxsize=1)
def _admin_rows() -> tuple:
    return fetch_service_rows(ADMIN_SERVICE_ID, "", 10000)


def _scan_overseas(name: str, ingredients: list, barcode: str) -> dict:
    import datetime
    today = datetime.date.today()
    rows = _overseas_rows()
    q = _norm_brand(name)
    bc = re.sub(r"\D", "", barcode or "")

    def active(r):
        s, e = _parse_ymd(r.get("STT_YMD")), _parse_ymd(r.get("END_YMD"))
        return (s is None or s <= today) and (e is None or today <= e)

    def brief(r):
        return {"product": str(r.get("PRDT_NM") or "").strip(), "maker": str(r.get("MUFC_NM") or "").strip(),
                "country": str(r.get("MUFC_CNTRY_NM") or "").strip(), "ingredients": str(r.get("INGR_NM_LST") or "").strip(),
                "start": str(r.get("STT_YMD") or ""), "end": str(r.get("END_YMD") or ""), "active": active(r),
                "barcode": str(r.get("BARCD_CTN") or "").strip(), "image": str(r.get("IMAGE_URL") or "").split(",")[0].strip()}

    product_hits, maker_hits = [], []
    for r in rows:
        kind = None
        if bc and re.sub(r"\D", "", str(r.get("BARCD_CTN") or "")) == bc:
            kind = "barcode"
        elif len(q) >= 4:
            n = _norm_brand(r.get("PRDT_NM"))
            if n and (q in n or (len(n) >= 6 and n in q)):
                kind = "name"
            elif len(q) >= 5 and q in _norm_brand(r.get("MUFC_NM")):
                maker_hits.append(brief(r))
        if kind:
            b = brief(r)
            b["match"] = kind
            product_hits.append(b)

    ingredient_hits = []
    for term in ingredients:
        t = term.strip().lower()
        if not t:
            continue
        variants = list(dict.fromkeys(KR_EN_INGREDIENTS.get(t, []) + [t]))
        matched = [r for r in rows if any(v and v in str(r.get("INGR_NM_LST") or "").lower() for v in variants if len(v) >= 3)]
        if matched:
            ingredient_hits.append({"term": term.strip(), "variants": variants, "count": len(matched),
                                    "active_count": sum(1 for r in matched if active(r)),
                                    "samples": [str(r.get("PRDT_NM") or "").strip() for r in matched[:3]]})
    level = "green"
    if any(h["active"] for h in product_hits):
        level = "red"
    elif product_hits or ingredient_hits or maker_hits:
        level = "yellow"
    return {"query": name, "barcode": bc, "level": level, "product_hits": product_hits[:5], "maker_hits": maker_hits[:3],
            "ingredient_hits": ingredient_hits, "total_rows": len(rows)}


@_nutri_tool
def check_overseas_product(product_name: str = "", ingredients_str: str = "", barcode: str = "") -> str:
    """해외직구(예: 아이허브)로 산 제품이 식약처 '해외직구 위해식품 차단정보' 목록에 있는지 확인합니다.
    product_name: 제품명(영문 가능, 4자 이상), ingredients_str: 성분(쉼표 구분, 한글 또는 영문 예: '요힘빈, sildenafil'), barcode: 바코드 숫자(선택).
    제품명·바코드 일치는 제품 단위 결과이고, 성분 결과는 '차단된 제품에 쓰인 성분인지' 참고 정보이며 성분 자체의 금지 여부는 아닙니다.
    ⚠️ 차단 목록에 없다고 안전하다는 뜻은 아닙니다. 결과 끝의 ```json 블록은 차트·카드용 데이터입니다.
    ※ 결과를 전달할 때 도구 결과에 없는 일반 지식·추정을 덧붙이지 말고, '해야 합니다' 같은 단정 대신 도구가 낸 판정 범위 안에서만 말하세요."""
    ingredients = _split_terms(ingredients_str)
    if len(_norm_brand(product_name)) < 4 and not ingredients and not re.sub(r"\D", "", barcode or ""):
        return "제품명(4자 이상), 성분, 바코드 중 하나 이상을 입력해 주세요. (예: product_name='Vital Elements Kratom')"
    try:
        d = _scan_overseas(product_name, ingredients, barcode)
    except ApiError as e:
        return f"식약처 API 오류: {e}"

    head = {"red": "🔴 차단 목록에서 일치하는 제품이 있습니다", "yellow": "🟡 확인이 필요한 일치 항목이 있습니다",
            "green": "🟢 차단 목록에서 일치하는 항목을 찾지 못했습니다 (안전 보장은 아님)"}[d["level"]]
    report = f"### ✈️ 해외직구 안전 스캐너\n\n**{head}**\n\n조회 기준: 제품명 `{product_name or '-'}` / 성분 `{', '.join(ingredients) or '-'}` / 바코드 `{d['barcode'] or '-'}` (차단 목록 {d['total_rows']:,}건 대조)\n\n"
    if d["product_hits"]:
        report += "**제품 일치**\n"
        for h in d["product_hits"]:
            state = "차단 중" if h["active"] else "차단 기간 종료"
            report += (f"- {'🔴' if h['active'] else '⚪'} **{short(h['product'], 60)}** ({h['country'] or '-'}, {h['maker'] or '-'}) · "
                       f"{'바코드' if h['match'] == 'barcode' else '제품명'} 일치 · {state}({h['start']}~{h['end']})\n")
            report += f"  - 차단 목록의 성분: {short(h['ingredients'], 80)}\n"
        report += "\n"
    if d["maker_hits"]:
        report += "**같은 제조·유통사의 다른 차단 제품 (참고)**\n" + "".join(f"- {short(h['product'], 50)} ({h['maker']})\n" for h in d["maker_hits"]) + "\n"
    if d["ingredient_hits"]:
        report += "**성분 참고 (차단된 제품에 쓰인 성분)**\n"
        for h in d["ingredient_hits"]:
            report += f"- `{h['term']}`: 차단 제품 {h['count']}건에 사용 (현재 차단 중 {h['active_count']}건) · 예: {', '.join(short(x, 30) for x in h['samples'])}\n"
        report += "\n"
    report += ("> 💡 출처: 식약처 해외직구 위해식품 차단정보(I2715). 제품명 일치는 표기가 다르면 놓칠 수 있고, 성분 결과는 성분 자체가 금지라는 뜻이 아니라 "
               "그 성분이 든 제품이 차단된 적이 있다는 뜻입니다. 의약품 성분(실데나필·타다라필 등)이나 시부트라민이 의심되면 섭취를 피하고 전문가와 상담하세요.")
    return report + "\n\n```json\n" + json.dumps({"overseas": d}, ensure_ascii=False) + "\n```"


# ---------------------------------------------------------------------
# 10) 행정처분 이력 (I0470) - 업체 신뢰 카드 확장
# ---------------------------------------------------------------------
_SEVERE_TYPES = ("취소", "폐쇄", "정지")


def _admin_actions(company: str, lcns: str) -> list:
    """제조·판매업체의 행정처분. 인허가번호가 같으면 'lcns'(확실), 업체명이 같으면 'name'(동명 업체 가능)"""
    rows = _admin_rows()
    nc = _norm_text(company)
    out = []
    for r in rows:
        kind = None
        if lcns and str(r.get("LCNS_NO") or "").strip() == lcns:
            kind = "lcns"
        elif len(nc) >= 3 and "건강기능식품" in str(r.get("INDUTY_CD_NM") or ""):
            n = _norm_text(r.get("PRCSCITYPOINT_BSSHNM"))
            if n and (n == nc or (len(n) >= 4 and (nc in n or n in nc))):
                kind = "name"
        if kind:
            out.append({"match": kind, "company": str(r.get("PRCSCITYPOINT_BSSHNM") or "").strip(),
                        "industry": str(r.get("INDUTY_CD_NM") or "").strip(), "type": str(r.get("DSPS_TYPECD_NM") or "").strip(),
                        "date": str(r.get("DSPS_DCSNDT") or "").strip(), "begin": str(r.get("DSPS_BGNDT") or "").strip(),
                        "end": str(r.get("DSPS_ENDDT") or "").strip(),
                        "violation": re.sub(r"\s+", " ", str(r.get("VILTCN") or "")).strip(),
                        "law": str(r.get("LAWORD_CD_NM") or "").strip(), "agency": str(r.get("DSPS_INSTTCD_NM") or "").strip()})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def _admin_level(actions: list) -> str:
    sure = [a for a in actions if a["match"] == "lcns"]
    if any(any(k in a["type"] for k in _SEVERE_TYPES) for a in sure):
        return "red"
    return "yellow" if actions else "green"


# ---------------------------------------------------------------------
# 11) 광고 팩트체크: 광고 문구를 공식 인정 기능성(I0030 주된기능성)과 대조
#     법 위반 판정은 하지 않고 '공식 인정 내용과 다르게 읽힐 수 있다'까지만 표시합니다.
# ---------------------------------------------------------------------
_DISEASE_PATTERNS = [
    ("치료·치유·완치 표현", r"치료|치유|완치|처방|약효|의약품\s*(수준|급|처럼)"),
    ("질병 예방·치료·개선 표현", r"(암|당뇨(병)?|고혈압|치매|우울증|불면증|관절염|아토피|탈모|빈혈|통풍|골다공증|지방간|간염|염증)\s*[^.!\n]{0,10}(예방|치료|개선|완화|치유|해결|억제|차단)"),
    ("혈압·혈당·콜레스테롤을 '낮춘다'는 단정 표현", r"(혈압|혈당|콜레스테롤|중성지방)\s*(을|를)?\s*(확실히\s*)?(낮춰|낮추|떨어뜨|내려)"),
    ("체중·지방 감량 단정 표현", r"(\d+\s*(kg|킬로)\s*(감량|빠|뺀)|지방\s*(분해|제거|녹)|살\s*(빠|뺀))"),
]
_EXAGGERATION_PATTERNS = [
    ("100%·완벽·최고 등 절대적 표현", r"100\s*%|완벽|최고|최상|기적|만병|무조건|확실히|보장|보증"),
    ("즉시·단기간 효과 단정 표현", r"즉시|당장|하루만에|일주일\s*(만에|내)|\d+\s*일\s*만에"),
    ("부작용 없음 표현", r"부작용\s*(이|은)?\s*(전혀\s*)?(없|제로)"),
    ("승인·인증 표현(근거 확인 필요)", r"(FDA|식약처|식품의약품안전처)\s*(승인|인증|공인|허가)|의사\s*(추천|처방)|임상\s*(입증|증명|완료)"),
]
_AD_STOP = {"도움", "도움을", "필요", "유지", "기능", "건강", "작용", "개선", "대사", "있음", "도움이", "수", "에", "을", "를", "및", "또는",
            "효과", "제품", "함유", "원료", "섭취", "건강에", "건강한", "좋은", "관리", "지원", "케어"}


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[가-힣A-Za-z0-9]{3,}", text or "") if t not in _AD_STOP}


def _function_keys(text: str) -> set:
    return {k for k, syns in FUNCTION_SYNONYMS.items() if k in text or any(s in text for s in syns)}


def _split_ad(text: str) -> list:
    parts = re.split(r"[\r\n]+|(?<=[.!?。])\s+|(?<=다)\.\s*", text or "")
    return [p.strip(" -·•*✔✅▶●■") for p in parts if len(p.strip(" -·•*✔✅▶●■")) >= 4]


_AD_AMOUNT_RE = re.compile(r"([가-힣A-Za-z][가-힣A-Za-z0-9\- ]{0,14}?)\s*(\d[\d,]*(?:\.\d+)?)\s*(㎍|μg|µg|mcg|㎎|mg|g)(?![a-zA-Z])", re.I)


@_nutri_tool
def check_ad_claims(ad_text: str, product_name: str = "") -> str:
    """상세페이지·광고 문구를 붙여 넣으면 식약처 공식 인정 기능성과 대조해 문장별로
    ✅ 인정 범위 / ⚠️ 인정 범위 밖·과장 가능 / ❌ 질병 치료·예방처럼 읽히는 표현 으로 표시합니다.
    product_name 을 주면 그 제품의 인정 기능성·함량(기준규격)과 비교하고, 비우면 질병·과장 표현만 점검합니다.
    ⚠️ 법 위반 판정이 아니라 '공식 인정 내용과 다르게 읽힐 수 있는 표현'을 알려 주는 참고 기능입니다. 결과 끝의 ```json 블록은 카드용 데이터입니다.
    ※ 결과를 전달할 때 도구 결과에 없는 일반 지식·추정을 덧붙이지 말고, '해야 합니다' 같은 단정 대신 도구가 낸 판정 범위 안에서만 말하세요."""
    claims = _split_ad(ad_text)
    if not claims:
        return "광고 문구를 붙여 넣어 주세요. (예: '루테인 20mg 함유, 눈 건강에 도움, 시력 회복 보장')"
    claims = claims[:30]

    official_text, official_lines, amounts, official_name, problems = "", [], {}, "", []
    mult = 1
    if (product_name or "").strip():
        try:
            item, _ = pick_product(product_name)
            if not item:
                problems.append(f"`{product_name}` 허가 정보를 찾지 못해 공식 기능성 대조는 생략합니다.")
            else:
                official_name = item.get("PRDLST_NM", product_name)
                detail = pick_detail(official_name) or {}
                official_text = str(detail.get("PRIMARY_FNCLTY") or item.get("PRIMARY_FNCLTY") or "")
                official_lines = [ln.strip() for ln in re.split(r"[\r\n]+", official_text) if ln.strip()]
                stdr = detail.get("STDR_STND", "")
                amounts = extract_amounts(stdr)
                mult, _ok = servings_per_day(detail.get("NTK_MTHD") or item.get("NTK_MTHD", ""), first_denominator_mg(stdr))
        except ApiError as e:
            problems.append(f"허가 정보 조회 오류: {e}")
    off_keys, off_tokens = _function_keys(official_text), _tokens(official_text)

    results = []
    for c in claims:
        level, reasons = "info", []
        for label, pat in _DISEASE_PATTERNS:
            if re.search(pat, c):
                level = "bad"
                reasons.append(f"❌ {label}: 질병의 예방·치료로 읽힐 수 있음")
        for label, pat in _EXAGGERATION_PATTERNS:
            if re.search(pat, c):
                if level != "bad":
                    level = "warn"
                reasons.append(f"⚠️ {label}")
        if level != "bad" and official_text:
            keys = _function_keys(c)
            toks = _tokens(c)
            if keys & off_keys or (toks & off_tokens):
                shared = sorted((keys & off_keys) or (toks & off_tokens))[:3]
                if level == "info":
                    level = "ok"
                reasons.append(f"✅ 공식 인정 기능성과 겹침({', '.join(shared)})")
            elif keys:
                if level == "info":
                    level = "warn"
                reasons.append(f"⚠️ 인정 기능성에 없는 기능 표현({', '.join(sorted(keys))})")
        if official_text and amounts:
            for m in _AD_AMOUNT_RE.finditer(c):
                key = canon(m.group(1).strip())
                if key in amounts:
                    ad_mg = (_to_float(m.group(2)) or 0) * UNIT_TO_MG.get(m.group(3).lower(), 1.0)
                    label_mg = amounts[key][1]
                    ok = any(abs(ad_mg - ref) <= ref * 0.2 for ref in (label_mg, label_mg * mult) if ref)
                    reasons.append(("✅" if ok else "⚠️") + f" 함량 표기 {m.group(2)}{m.group(3)}: 기준규격 1회 {fmt_auto(label_mg)} / 1일 {fmt_auto(label_mg * mult)}"
                                   + ("와 비슷함" if ok else "와 다름"))
                    if not ok and level in ("info", "ok"):
                        level = "warn"
        if not reasons:
            ks = _function_keys(c)
            reasons.append(f"ℹ️ 기능성 표현 감지({', '.join(sorted(ks))}) — 제품명을 함께 주면 공식 인정 기능성과 대조합니다" if (ks and not official_text)
                           else "ℹ️ 기능성·질병·과장 표현이 감지되지 않음(판단 대상 아님)")
        results.append({"text": c, "level": level, "reasons": reasons})

    icon = {"ok": "✅", "warn": "⚠️", "bad": "❌", "info": "ℹ️"}
    cnt = {k: sum(1 for r in results if r["level"] == k) for k in icon}
    report = "### 📢 광고 팩트체크\n\n"
    if official_name:
        report += f"**대조 제품:** {official_name}\n**공식 인정 기능성(I0030 주된기능성):**\n" + "".join(f"- {short(ln, 100)}\n" for ln in official_lines[:6]) + "\n"
    elif not problems:
        report += "제품명을 주지 않아 인정 기능성 대조는 하지 않고, 질병·과장 표현만 점검했습니다.\n\n"
    report += f"**요약:** ❌ {cnt['bad']}건 · ⚠️ {cnt['warn']}건 · ✅ {cnt['ok']}건 · ℹ️ {cnt['info']}건\n\n"
    for r in results:
        mark = icon[r["level"]] + ("(일부 ✅)" if r["level"] in ("warn", "bad") and any(x.startswith("✅") for x in r["reasons"]) else "")
        report += f"- {mark} “{short(r['text'], 70)}”\n" + "".join(f"  - {x}\n" for x in r["reasons"])
    if problems:
        report += "\n" + "".join(f"> ❗ {p}\n" for p in problems)
    report += ("\n> 💡 법 위반 여부를 판정하는 기능이 아닙니다. 공식 인정 내용과 다르게 읽힐 수 있는 표현을 찾아 주는 참고 도구이며, 키워드 기반이라 놓치거나 "
               "과하게 잡을 수 있습니다. 광고 표현의 적법성은 식약처·전문가에게 확인하세요.")
    data = {"ad": {"product": official_name, "official": official_lines[:8], "claims": results, "counts": cnt}}
    return report + "\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```"


# ---------------------------------------------------------------------
# 12) 중복 지출 계산: 여러 제품의 성분이 겹치는 비율로 '겹치는 만큼의 지출'을 추정
#     입력 예) '센트룸 x1=25000/60, 고려은단 비타민C1000=18000'  (제품명=가격 또는 제품명=가격/일수분, 일수 기본 30)
# ---------------------------------------------------------------------
def _parse_price_items(s: str) -> list:
    out = []
    for term in re.split(r"[,\n]", s or ""):
        term = term.strip()
        if not term:
            continue
        if "=" not in term:
            out.append({"raw": term, "price": None, "days": 30})
            continue
        name, price = term.rsplit("=", 1)
        m = re.match(r"\s*([\d,]+)(?:\s*/\s*(\d+))?", price)
        if not m:
            out.append({"raw": name.strip(), "price": None, "days": 30})
            continue
        out.append({"raw": name.strip(), "price": int(m.group(1).replace(",", "")), "days": int(m.group(2) or 30)})
    return out


@_nutri_tool
def check_duplicate_spending(products_with_price_str: str) -> str:
    """여러 영양제를 같이 먹을 때 성분이 겹치는 정도와 '겹치는 만큼 쓴 돈'(추정)을 계산합니다.
    입력: '제품명=가격' 을 쉼표로 구분. 한 통이 며칠분이면 '제품명=가격/일수'(기본 30일), 하루 섭취 단위 수는 제품명 뒤 'x2'.
    예: '센트룸 데일리 뉴트리션 팩 30 여성 프로=25000/30, 속편한 고려은단 비타민C1000 x2=18000/60'
    ⚠️ 겹침 판정은 기준규격의 mg·μg 성분만 대상이며, 겹치는 성분 중 함량이 가장 큰 제품 하나를 '유지'로 보고 나머지를 중복으로 셉니다.
    금액은 성분 개수 비율로 나눈 단순 추정이라 참고용입니다. 결과 끝의 ```json 블록은 차트용 데이터입니다.
    ※ 결과를 전달할 때 도구 결과에 없는 일반 지식·추정을 덧붙이지 말고, '해야 합니다' 같은 단정 대신 도구가 낸 판정 범위 안에서만 말하세요."""
    items = _parse_price_items(products_with_price_str)
    if len(items) < 2:
        return "제품을 2개 이상 '제품명=가격' 형식으로 입력해 주세요. (예: '제품A=25000/30, 제품B=18000/60')"
    prods, problems = [], []
    for it in items:
        names, units = _parse_product_names(it["raw"])
        if not names:
            continue
        c = _collect_intake(names[:1], units)
        if not c["matched"] or not c["totals"]:
            problems.append(f"`{it['raw']}`: " + ("허가 정보를 찾지 못했습니다." if not c["matched"] else "성분 함량을 계산하지 못했습니다.") + " " + "; ".join(c["errors"]))
            continue
        monthly = round(it["price"] * 30 / it["days"]) if it["price"] else None
        prods.append({"name": c["matched"][0]["official"], "price": it["price"], "days": it["days"], "monthly": monthly,
                      "totals": c["totals"], "display": c["display"]})
    if len(prods) < 2:
        return "비교할 수 있는 제품이 2개 미만입니다.\n" + "\n".join(f"- {p}" for p in problems)
    keys = {}
    for p in prods:
        for k, v in p["totals"].items():
            keys.setdefault(k, []).append((v, p["name"]))
    overlaps = {k: sorted(v, reverse=True) for k, v in keys.items() if len(v) >= 2}
    out = []
    for p in prods:
        n = len(p["totals"])
        red = [k for k, v in overlaps.items() if p["name"] in [x[1] for x in v] and v[0][1] != p["name"]]
        shared = [k for k, v in overlaps.items() if p["name"] in [x[1] for x in v]]
        ratio = len(red) / n if n else 0
        waste = round(p["monthly"] * ratio) if p["monthly"] is not None else None
        out.append({"name": p["name"], "monthly": p["monthly"], "nutrients": n, "shared": [p["display"][k] for k in shared],
                    "redundant": [p["display"][k] for k in red], "ratio": round(ratio * 100), "waste": waste})
    tot = sum(o["monthly"] for o in out if o["monthly"] is not None)
    waste_tot = sum(o["waste"] for o in out if o["waste"] is not None)
    report = "### 💸 중복 지출 계산\n\n| 제품 | 월 비용(30일) | 성분 수 | 겹치는 성분 | 중복으로 본 성분 | 중복 비율 | 중복 지출(추정) |\n| :--- | ---: | ---: | :--- | :--- | ---: | ---: |\n"
    for o in out:
        report += (f"| {short(o['name'], 28)} | {o['monthly']:,}원 | " if o["monthly"] is not None else f"| {short(o['name'], 28)} | - | ") + \
                  f"{o['nutrients']} | {', '.join(o['shared']) or '-'} | {', '.join(o['redundant']) or '-'} | {_bar(o['ratio'])} {o['ratio']}% | " + \
                  (f"{o['waste']:,}원 |\n" if o["waste"] is not None else "- |\n")
    report += f"\n**합계:** 월 {tot:,}원 중 중복 추정 약 {waste_tot:,}원 ({round(waste_tot / tot * 100) if tot else 0}%)\n\n"
    if overlaps:
        report += "**겹치는 성분 상세**\n" + "".join(
            f"- {prods[0]['display'].get(k) or next(p['display'][k] for p in prods if k in p['display'])}: " +
            " / ".join(f"{short(n, 20)} {fmt_auto(v)}" for v, n in v_) + "\n" for k, v_ in overlaps.items()) + "\n"
    else:
        report += "겹치는 mg·μg 성분이 없습니다.\n\n"
    if problems:
        report += "**❗ 제외된 항목**\n" + "".join(f"- {p}\n" for p in problems) + "\n"
    report += ("> 💡 겹침이 곧 낭비는 아닙니다(용량을 나눠 먹거나 목적이 다를 수 있음). 겹치는 성분의 합산 섭취량이 상한을 넘는지는 `full_safety_check`로 함께 확인하세요. "
               "금액은 성분 개수 비율 기준의 단순 추정입니다.")
    return report + "\n\n```json\n" + json.dumps({"spend": {"items": out, "total": tot, "waste": waste_tot}}, ensure_ascii=False) + "\n```"


# ---------------------------------------------------------------------
# 13) 복용 약 효능군 중복 (DUR 효능군중복): 같은 효능군의 약을 겹쳐 먹고 있는지
#     응답 필드 이름을 실제 데이터로 확인하지 못해, 여러 후보 필드를 순서대로 읽습니다.
# ---------------------------------------------------------------------
_EFCY_FIELDS = ("EFFECT_NAME", "EFCY_NAME", "SERS_NAME", "EFFECT_CLS_NAME", "CLASS_NAME", "EFCY_DPLCT_NAME")


@_nutri_tool
def check_drug_efficacy_duplicate(medications_str: str) -> str:
    """복용 중인 약(쉼표 구분, 2개 이상)이 같은 효능군에 속해 중복 복용 위험이 있는지 식약처 DUR '효능군중복' 정보로 확인합니다.
    ⚠️ 등록된 성분만 확인되며 없다고 안전하다는 뜻이 아닙니다. 결과가 비어 있으면 `debug_datagokr`로 응답 필드를 확인해 주세요.
    ※ 결과를 전달할 때 도구 결과에 없는 일반 지식·추정을 덧붙이지 말고, '해야 합니다' 같은 단정 대신 도구가 낸 판정 범위 안에서만 말하세요."""
    meds = _split_terms(medications_str)
    if len(meds) < 2:
        return "약 이름을 2개 이상 쉼표로 구분해 입력해 주세요."
    info, groups, problems = [], {}, []
    for m in meds:
        try:
            matched, ings, _cls = _dur_product_ingredients(m)
        except ApiError as e:
            problems.append(f"`{m}`: {e}")
            continue
        ings = ings or [m]
        for ing in ings:
            try:
                rows, used = _dur_rows_fallback("dur_ingr_efcy", "ingrName", ing)
            except ApiError as e:
                problems.append(f"`{ing}`: {e}")
                continue
            vals = set()
            for r in rows:
                if str(r.get("INGR_NAME") or "").strip() != used:
                    continue
                for f in _EFCY_FIELDS:
                    v = str(r.get(f) or "").strip()
                    if v:
                        vals.add(v)
                        break
            info.append({"med": m, "ingredient": ing, "groups": sorted(vals)})
            for v in vals:
                groups.setdefault(v, set()).add(m)
    dups = {g: sorted(ms) for g, ms in groups.items() if len(ms) >= 2}
    report = "### 💊 약 효능군 중복 점검\n\n"
    if dups:
        report += "**🔴 같은 효능군으로 등록된 조합**\n" + "".join(f"- {g}: {', '.join(ms)}\n" for g, ms in dups.items()) + "\n"
    else:
        report += "**🟢 같은 효능군으로 등록된 조합을 찾지 못했습니다** (안전 보장은 아님)\n\n"
    report += "**약별 조회 결과**\n" + "".join(f"- {i['med']} ({i['ingredient']}): {', '.join(i['groups']) or '등록된 효능군 없음'}\n" for i in info)
    if problems:
        report += "\n**❗ 조회 오류**\n" + "".join(f"- {p}\n" for p in problems)
    report += "\n> 💡 출처: DUR 성분정보 효능군중복(데이터 포털). 중복 여부의 판단과 조정은 의사·약사와 상의하세요."
    return report


# ---------------------------------------------------------------------
# 5) 기능성 키워드 기반 성분 추천 (제품 순위가 아니라 성분 중심)
# ---------------------------------------------------------------------
FUNCTION_SYNONYMS = {
    "관절": ["관절", "연골"],
    "눈": ["시력", "시각", "황반", "안구", "눈"],
    "피로": ["피로", "에너지"],
    "면역": ["면역"],
    "장": ["배변", "유익균", "장내", "장 건강"],
    "혈압": ["혈압"],
    "혈당": ["혈당"],
    "콜레스테롤": ["콜레스테롤", "중성지질"],
    "뼈": ["뼈", "골다공증", "골밀도", "골 건강"],
    "항산화": ["항산화", "유해산소"],
    "수면": ["수면"],
    "피부": ["피부", "자외선", "보습"],
    "기억력": ["기억력", "인지"],
    "간": ["간 건강", "간 기능"],
    "체지방": ["체지방", "체중"],
    "갱년기": ["갱년기"],
    "혈행": ["혈행", "혈액흐름"],
    "전립선": ["전립선"],
    "근육": ["근육", "근력", "운동수행"],
}


def expand_function_keywords(keyword: str) -> list:
    kw = (keyword or "").strip()
    out = []
    for key, syns in FUNCTION_SYNONYMS.items():
        if key in kw or any(s in kw for s in syns):
            out.extend(syns)
    return list(dict.fromkeys(out or [kw]))


def _range_text(low, high, unit: str) -> str:
    if low is not None and high is not None:
        return f"{low:g}~{high:g}{unit}" if low != high else f"{low:g}{unit}"
    if high is not None:
        return f"최대 {high:g}{unit}"
    if low is not None:
        return f"최소 {low:g}{unit}"
    return "정보 없음"


@mcp.tool()
def recommend_by_function(keyword: str, sex: str = "", age: int = 0, current_intake_str: str = "", max_results: int = 5) -> str:
    """'관절', '눈 피로', '면역'처럼 원하는 기능성을 말하면, 식약처가 인정한 기능성 성분을 찾아 하루 섭취 기준과 주의사항을 보여줍니다.
    제품 순위가 아니라 성분 중심이며, 예시 제품은 순위가 아닌 검색 결과입니다.
    sex('남'/'여')·age(만 19세 이상)를 주면 상한섭취량(UL)을, current_intake_str(예: '비타민C 1000mg, 아연 8.5mg')까지 주면 남은 여유량을 계산합니다."""
    kws = expand_function_keywords(keyword)
    if not kws or not kws[0]:
        return "원하는 기능(예: 관절, 눈 건강, 면역, 장 건강)을 입력해 주세요."
    max_results = max(1, min(int(max_results or 5), 10))

    try:
        cls_rows = fetch_service_rows(LIMIT_SERVICE_ID)
    except ApiError as e:
        return f"식약처 API 오류: {e}"
    try:
        ind_rows = fetch_service_rows(INDIV_SERVICE_ID)
    except ApiError:
        ind_rows = ()

    s = norm_sex(sex) if sex else None
    use_ul = bool(s) and isinstance(age, int) and age >= 19
    current = {}
    if current_intake_str.strip():
        for name, mg, _ in _parse_intake(current_intake_str):
            if mg is not None:
                current[canon(name)] = current.get(canon(name), 0.0) + mg

    def score(row):
        text = str(row.get("PRIMARY_FNCLTY") or "")
        return sum(1 for k in kws if k in text)

    cls_hits = sorted((r for r in cls_rows if score(r) > 0), key=lambda r: (-score(r), str(r.get("PRDCT_NM") or "")))[:max_results]
    ind_hits = sorted((r for r in ind_rows if score(r) > 0), key=lambda r: (-score(r), str(r.get("RAWMTRL_NM") or "")))[:3]

    report = f"### 🔎 '{keyword}' 관련 기능성 성분 (검색어: {', '.join(kws)})\n\n"
    if not cls_hits and not ind_hits:
        return report + "식약처 기능성 정보에서 관련 성분을 찾지 못했습니다. 다른 표현(예: '눈' 대신 '시력')으로 시도해 보세요."

    for r in cls_hits:
        name = str(r.get("PRDCT_NM") or "").strip() or "이름 없음"
        unit = str(r.get("INTK_UNIT") or "").strip()
        low, high = _to_float(r.get("DAY_INTK_LOWLIMIT")), _to_float(r.get("DAY_INTK_HIGHLIMIT"))
        report += f"#### {name}\n"
        report += f"- 기능성: {short((r.get('PRIMARY_FNCLTY') or '').replace(chr(10), ' '), 120)}\n"
        report += f"- 식약처 하루 섭취 기준(제품 허가 기준): {_range_text(low, high, unit)}\n"
        memo = (r.get("INTK_MEMO") or "").strip()
        if memo:
            report += f"- 참고: {short(memo, 100)}\n"
        caution = (r.get("IFTKN_ATNT_MATR_CN") or "").strip().replace("\n", " ")
        if caution:
            report += f"- 섭취 시 주의사항: {short(caution, 120)}\n"
        key = canon(name)
        if use_ul:
            status, ul_mg, note = ul_lookup(key, s, age)
            if status == "ok":
                report += f"- 영양소 섭취기준 상한섭취량(UL, {'남' if s == 'M' else '여'} {age}세): {fmt_auto(ul_mg)}\n"
                if key in current:
                    left = ul_mg - current[key]
                    report += (f"- 현재 보충제 합산 {fmt_auto(current[key])} → UL까지 여유 {fmt_auto(left)}\n" if left >= 0
                               else f"- ⚠️ 현재 보충제 합산 {fmt_auto(current[key])} 로 이미 UL을 초과했습니다\n")
            elif status == "unset":
                report += "- 영양소 섭취기준에서 상한섭취량이 설정되지 않은 성분입니다.\n"
        if key in current and not use_ul:
            report += f"- 현재 보충제 합산: {fmt_auto(current[key])} (성별·나이를 입력하면 UL 대비 여유를 계산합니다)\n"
        try:
            examples = fetch_products(name, 3)
        except ApiError:
            examples = ()
        if examples:
            report += "- 예시 제품(검색 결과, 순위 아님): " + ", ".join(e.get("PRDLST_NM", "") for e in examples) + "\n"
        report += "\n"

    if ind_hits:
        report += "#### 개별인정형 원료 (참고)\n"
        for r in ind_hits:
            report += f"- **{short(str(r.get('RAWMTRL_NM') or '').strip(), 50)}**: {short((r.get('PRIMARY_FNCLTY') or '').replace(chr(10), ' '), 80)} "
            lo, hi = str(r.get("DAY_INTK_LOWLIMIT") or "-").strip(), str(r.get("DAY_INTK_HIGHLIMIT") or "-").strip()
            report += f"(하루 {short(lo, 20) if lo == hi else short(lo, 20) + ' ~ ' + short(hi, 20)})\n"
        report += "\n"

    report += ("> 💡 성분을 기능성 문구로 찾은 결과이며 제품이나 성분의 우열을 매긴 것이 아닙니다. "
               "복용 중인 약이나 질환이 있다면 `check_drug_supplement_cautions`, `check_condition_cautions`로 주의 문구를 함께 확인하고 전문가와 상담하세요.")
    return report


if __name__ == "__main__":
    if os.environ.get("MCP_TRANSPORT", "stdio") == "http":      # 서버 배포용 (실제 배포는 server_http.py 사용 권장)
        mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
    else:
        mcp.run()
