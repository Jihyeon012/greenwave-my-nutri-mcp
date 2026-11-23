"""My-Nutri-MCP 원격(HTTP) 실행용 진입점. Render 시작 명령: uvicorn server_http:app --host 0.0.0.0 --port $PORT
- MCP 주소: https://<서비스주소>/mcp  (Streamable HTTP)
- 상태 확인: /health
- 인증: 환경변수 MCP_AUTH_TOKEN 이 설정되면 'Authorization: Bearer <토큰>' 또는 'X-API-Key: <토큰>' 이 있어야 /mcp 에 접근할 수 있습니다.
"""
import hmac
import json
import os

from my_nutri_mcp import mcp

TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


class GateMiddleware:
    """순수 ASGI 미들웨어: /health 응답 + /mcp 토큰 확인 (스트리밍 응답을 건드리지 않음)"""

    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def _send_json(self, send, status: int, body: dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(data)).encode())]})
        await send({"type": "http.response.body", "body": data})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/health":
                return await self._send_json(send, 200, {"status": "ok"})
            if self.token and path.startswith("/mcp"):
                headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
                given = headers.get("x-api-key", "")
                auth = headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    given = given or auth[7:].strip()
                if not hmac.compare_digest(given.encode(), self.token.encode()):
                    return await self._send_json(send, 401, {"error": "unauthorized"})
        await self.app(scope, receive, send)


app = GateMiddleware(mcp.http_app(), TOKEN)
