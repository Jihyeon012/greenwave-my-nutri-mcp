# My-Nutri 배포 가이드 (Render)

구성: **my-nutri-mcp**(MCP 서버, 주소 `/mcp`) + **my-nutri-web**(웹사이트). PlayMCP에는 MCP 서버 주소만 등록합니다.

## 1. 코드 올리기
1. 이 폴더 전체를 GitHub 새 저장소에 올립니다 (`.gitignore`가 `.mcp.json`·`.env`를 제외합니다).
2. 키는 절대 코드에 적지 않습니다. 이미 채팅 등에 노출된 적 있는 식약처 키는 재발급을 권장합니다.

## 2. Render 설정 (순서대로)
1. Render → New → **Blueprint** → 저장소 선택 → `render.yaml`이 서비스 2개를 만듭니다.
2. **my-nutri-mcp** 환경변수 입력: `MFDS_API_KEY`, `DATA_GO_KR_KEY`(Decoding 키), `MCP_AUTH_TOKEN`(긴 임의 문자열, 직접 만들기).
3. 배포 후 `https://<my-nutri-mcp 주소>/health` 가 `{"status":"ok"}`면 정상입니다.
4. **my-nutri-web** 환경변수 입력: `ANTHROPIC_API_KEY`, `MCP_URL` = `https://<my-nutri-mcp 주소>/mcp`, `MCP_AUTH_TOKEN`(2번과 같은 값).
5. `https://<my-nutri-web 주소>` 접속 → 동의 체크 → 예시 버튼으로 시험.

## 3. PlayMCP 등록 (공식 가이드는 직접 확인하세요)
- 서버 주소: `https://<my-nutri-mcp 주소>/mcp` (Streamable HTTP, HTTPS 도메인 필요)
- 인증: `Authorization: Bearer <MCP_AUTH_TOKEN>` 또는 `X-API-Key` 둘 다 받습니다.
- 식별자는 영문·숫자만, "kakao" 문자열 금지(후기 기준). 도구 설명을 수정하면 "정보 불러오기"를 다시 눌러야 합니다.
- 순서: 정보 불러오기 → 임시 등록 → PlayMCP AI채팅에서 시험 → 등록 및 심사 요청.

## 4. 운영 주의
- 무료(free) 플랜은 15분 무요청 시 잠들어 첫 응답이 느립니다. 시연·심사 기간에는 starter 권장.
- 공공데이터포털 개발계정은 하루 10,000건입니다. 이용자가 늘면 운영계정 전환이 필요합니다.
- 웹은 IP당 10분 8회, 하루 전체 300회로 제한합니다(`IP_LIMIT`, `IP_WINDOW_SEC`, `DAILY_LIMIT`로 조정).
- 복용 약·질환 정보는 서버에 저장·로그하지 않지만 Claude API(Anthropic)로 전송됩니다. 화면의 동의 문구를 유지하세요.
