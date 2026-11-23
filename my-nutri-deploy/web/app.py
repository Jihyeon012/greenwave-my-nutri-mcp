"""My-Nutri 웹사이트 백엔드 (FastAPI). Render 시작 명령: uvicorn web.app:app --host 0.0.0.0 --port $PORT
브라우저 -> /api/chat -> Claude API(MCP 커넥터) -> My-Nutri-MCP 서버(공개 HTTPS 주소) 순서로 호출합니다.
개인정보: 대화·복용 약·질환 정보는 서버에 저장하지 않고 로그에도 남기지 않습니다(대화 기록은 브라우저 화면에만 있음).
"""
import json
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

MCP_URL = os.environ.get("MCP_URL", "")                  # 예: https://my-nutri-mcp.onrender.com/mcp
MCP_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
MODEL = os.environ.get("WEB_MODEL", "claude-sonnet-5-5")
IP_LIMIT = int(os.environ.get("IP_LIMIT", "8"))          # IP당 허용 요청 수
IP_WINDOW = int(os.environ.get("IP_WINDOW_SEC", "600"))  # 위 횟수를 세는 시간(초)
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "300"))  # 하루 전체 요청 상한(비용 보호)
BETA = "mcp-client-2025-11-20"

SYSTEM = """당신은 My-Nutri-MCP 도구로 영양제를 점검해 주는 도우미입니다.
- 영양제·복용 약·몸 상태에 관한 질문은 먼저 My-Nutri-MCP 도구를 호출해 확인하세요. 제품명은 도구로 확인하고 추측하지 마세요.
- 도구 결과에 있는 내용만 전달하세요. 도구 결과에 없는 일반 지식, 추정, 건강 조언, 다른 출처의 수치를 덧붙이지 마세요.
- 결론을 단정하지 마세요('안전합니다', '삭제해야 합니다' 금지). 도구가 낸 판정 범위 안에서만 말하고, '검색되지 않음'을 '안전함'으로 바꾸지 마세요.
- 표·막대·이모지 판정은 도구 결과 화면에 따로 표시되므로 다시 옮겨 적지 말고, 핵심 3~5줄로만 한국어로 요약하세요. 합계·비율은 직접 계산하지 마세요.
- 영양제와 무관한 질문에는 이 서비스의 범위를 안내하세요."""

app = FastAPI(title="My-Nutri", docs_url=None, redoc_url=None)
_hits = defaultdict(deque)
_day = {"key": "", "n": 0}


class Msg(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=2500)


class ChatIn(BaseModel):
    messages: list[Msg] = Field(min_length=1, max_length=12)
    consent: bool = False


def client_ip(req: Request) -> str:
    fwd = req.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "?")) or "?"


def check_limits(ip: str, now: float | None = None) -> str | None:
    """허용이면 None, 거절이면 사용자에게 보여줄 안내 문구"""
    now = time.time() if now is None else now
    day = time.strftime("%Y-%m-%d", time.gmtime(now + 9 * 3600))
    if _day["key"] != day:
        _day.update(key=day, n=0)
    if _day["n"] >= DAILY_LIMIT:
        return "오늘 이용 가능한 횟수가 모두 사용되었습니다. 내일 다시 이용해 주세요."
    q = _hits[ip]
    while q and now - q[0] > IP_WINDOW:
        q.popleft()
    if len(q) >= IP_LIMIT:
        return f"요청이 너무 많습니다. {IP_WINDOW // 60}분 뒤에 다시 시도해 주세요."
    q.append(now)
    _day["n"] += 1
    return None


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def extract_chart(text: str) -> list:
    """도구 결과 끝의 ```json 블록 -> 막대 차트 목록 [{title, ref, rows:[{name, pct, pct2}]}]. 실패하면 빈 목록."""
    m = re.search(r"```json\s*([\s\S]*?)```\s*$", text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    charts = []
    try:
        if isinstance(data.get("intake"), list):     # full_safety_check: 성분별 합산량 / 상한섭취량(UL)
            rows = [{"name": r.get("name", ""), "pct": _num(r.get("pct_label")), "pct2": _num(r.get("pct_max"))}
                    for r in data["intake"] if _num(r.get("pct_label")) is not None]
            if rows:
                charts.append({"title": "상한섭취량(UL) 대비 합산 섭취량", "ref": "UL 100%", "a": "표시량 기준", "b": "최대 추정", "rows": rows})
        if isinstance(data.get("nutrients"), list):  # check_nutrient_adequacy: 영양제가 채우는 권장섭취량 비율
            rows = [{"name": r.get("name", ""), "pct": _num(r.get("pct_supp")),
                     "pct2": _num(r.get("pct_total")) if data.get("used_diet") else None}
                    for r in data["nutrients"] if _num(r.get("pct_supp")) is not None]
            if rows:
                charts.append({"title": "권장섭취량 대비 영양제 비율", "ref": "기준 100%", "a": "영양제", "b": "영양제+식사", "rows": rows})
        if isinstance(data.get("effective"), list):  # check_effective_dose: 제품별 인정 하한 대비 1일 함량
            for e in data["effective"]:
                rows = [{"name": r.get("name", ""), "pct": _num(r.get("pct_of_low")), "pct2": None}
                        for r in e.get("rows", []) if _num(r.get("pct_of_low")) is not None]
                if rows:
                    charts.append({"title": "인정 1일 섭취량 하한 대비 · " + str(e.get("product", "")), "ref": "하한 100%", "a": "1일 함량", "b": "", "rows": rows})
    except (AttributeError, TypeError):
        return []
    return charts


def split_blocks(content: list) -> dict:
    """Claude 응답 블록 -> {text, tools:[{name, text, error}]}"""
    names, tools, texts = {}, [], []
    for b in content:
        d = b.model_dump() if hasattr(b, "model_dump") else dict(b)
        t = d.get("type")
        if t == "text":
            texts.append(d.get("text", ""))
        elif t == "mcp_tool_use":
            names[d.get("id")] = d.get("name", "")
        elif t == "mcp_tool_result":
            body = d.get("content")
            text = "\n".join(c.get("text", "") for c in body if isinstance(c, dict)) if isinstance(body, list) else str(body or "")
            chart = extract_chart(text)
            text = re.sub(r"\n*```json[\s\S]*?```\s*$", "", text).strip()     # 차트용 JSON 은 글자 화면에서는 제거(막대 차트로 따로 그림)
            tools.append({"name": names.get(d.get("tool_use_id"), ""), "text": text, "error": bool(d.get("is_error")), "charts": chart})
    return {"text": "\n".join(texts).strip(), "tools": tools}


@app.post("/api/chat")
def chat(body: ChatIn, request: Request):
    if not body.consent:
        raise HTTPException(400, "개인정보(복용 약·질환 포함) 처리 안내에 동의해야 이용할 수 있습니다.")
    if body.messages[-1].role != "user":
        raise HTTPException(400, "마지막 메시지는 사용자 메시지여야 합니다.")
    if not (MCP_URL and os.environ.get("ANTHROPIC_API_KEY")):
        raise HTTPException(503, "서버 설정이 완료되지 않았습니다(MCP_URL, ANTHROPIC_API_KEY).")
    limited = check_limits(client_ip(request))
    if limited:
        return JSONResponse({"detail": limited}, status_code=429)
    server = {"type": "url", "url": MCP_URL, "name": "my-nutri"}
    if MCP_TOKEN:
        server["authorization_token"] = MCP_TOKEN
    try:
        resp = anthropic.Anthropic(timeout=90.0).beta.messages.create(
            model=MODEL, max_tokens=1500, system=SYSTEM,
            messages=[{"role": m.role, "content": m.content} for m in body.messages],
            mcp_servers=[server], tools=[{"type": "mcp_toolset", "mcp_server_name": "my-nutri"}], betas=[BETA])
    except anthropic.APIError:
        raise HTTPException(502, "AI 서버 호출에 실패했습니다. 잠시 후 다시 시도해 주세요.")
    return split_blocks(resp.content)


PLAIN_SYSTEM = """당신은 일반적인 AI 도우미입니다. 영양제 질문에 한국어로 6~8줄 안으로 답하세요. 별도의 검색·데이터 도구는 없고, 알고 있는 일반 지식으로만 답합니다."""


class PlainIn(BaseModel):
    question: str = Field(min_length=1, max_length=1500)
    consent: bool = False


@app.post("/api/plain")
def plain(body: PlainIn, request: Request):
    """비교 화면용: 도구 없이 일반 Claude 가 답하는 모습을 보여 준다(My-Nutri-MCP 와 비교하기 위한 것)."""
    if not body.consent:
        raise HTTPException(400, "개인정보 처리 안내에 동의해야 이용할 수 있습니다.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "서버 설정이 완료되지 않았습니다(ANTHROPIC_API_KEY).")
    limited = check_limits("plain:" + client_ip(request))
    if limited:
        return JSONResponse({"detail": limited}, status_code=429)
    try:
        resp = anthropic.Anthropic(timeout=60.0).messages.create(
            model=MODEL, max_tokens=700, system=PLAIN_SYSTEM, messages=[{"role": "user", "content": body.question}])
    except anthropic.APIError:
        raise HTTPException(502, "AI 서버 호출에 실패했습니다. 잠시 후 다시 시도해 주세요.")
    return {"text": "".join(getattr(b, "text", "") for b in resp.content).strip()}


@app.get("/health")
def health():
    return {"status": "ok"}


STATIC = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
