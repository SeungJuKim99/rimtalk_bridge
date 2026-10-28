# RimTalk ↔ Claude 로컬 브리지

```
RimTalk ──(OpenAI 호환 HTTP)──▶ 127.0.0.1:8765 브리지 ──(Claude Agent SDK, 구독 로그인)──▶ Claude
```

RimTalk의 **Custom** 공급자 설정을 이 PC의 로컬 서버로 향하게 해서, Claude 구독(Team 프리미엄 시트) 사용량으로 정착민 대화를 생성합니다. API 키는 쓰지 않습니다.

## 구성 파일

| 파일 | 설명 |
|---|---|
| `bridge.py` | 서버 본체 (FastAPI + uvicorn + claude-agent-sdk) |
| `config.json` | 포트 / 모델 / 동시성 / 타임아웃 |
| `start_bridge.bat` | 원클릭 실행 (venv 생성, 로그인 확인, 서버 실행) |
| `requirements.txt` | 의존성 고정 버전 |
| `workspace\` | SDK 작업 디렉터리 (비어 있는 전용 폴더) |

## 설치

1. Python 3.10 이상이 필요합니다 (`winget install Python.Python.3.12 --source winget --scope user`).
2. `start_bridge.bat`를 처음 실행하면 `.venv` 생성과 패키지 설치가 자동으로 진행됩니다.
3. **Claude 구독 로그인 (최초 1회)**: 로그인이 안 되어 있으면 bat가 브라우저 로그인을 띄웁니다. 수동으로 하려면:
   ```
   .venv\Lib\site-packages\claude_agent_sdk\_bundled\claude.exe auth login --claudeai
   ```
   반드시 `--claudeai`(구독)로 로그인하세요. `--console`은 API 과금 경로입니다.
   확인: `... claude.exe auth status`를 실행해 `"loggedIn": true`가 나오면 됩니다.
   > Claude 데스크톱 앱의 로그인은 앱 내부에만 있어서 SDK와 공유되지 않습니다. 별도 로그인이 필요합니다.

## 실행

`start_bridge.bat`를 더블클릭하면 콘솔 창에 요청 로그가 표시됩니다. 창을 닫으면 서버가 종료됩니다.

```
start_bridge.bat                 # 기본 (config.json 사용)
start_bridge.bat --debug         # RimTalk가 보낸 원본 요청/응답 본문까지 출력
start_bridge.bat --model sonnet  # 기본 모델 일시 변경
```

### 설정 (우선순위: 명령줄 > 환경변수 > config.json > 기본값)

| 항목 | config.json | 환경변수 | 기본값 |
|---|---|---|---|
| 포트 | `port` | `BRIDGE_PORT` | 8765 |
| 기본 모델 | `model` | `BRIDGE_MODEL` | `haiku` |
| 동시 요청 수 | `concurrency` | `BRIDGE_CONCURRENCY` | 2 |
| 타임아웃(초) | `timeout` | `BRIDGE_TIMEOUT` | 120 |

호스트는 항상 `127.0.0.1`로 고정되어 외부에서 접속할 수 없습니다.

## RimTalk 설정

1. RimTalk 모드 설정에서 **Custom** (고급 / Cloud provider 중 Custom)을 선택합니다.
2. **URL**: `http://127.0.0.1:8765`
   - RimTalk가 `/v1/chat/completions`를 자동으로 붙입니다. `http://127.0.0.1:8765/v1`로 넣어도 동작합니다.
3. **API Key**: 아무 문자열 (예: `local`). 비워 두면 RimTalk가 "API key missing"으로 동작하지 않습니다.
4. **Model**: `haiku` 또는 `sonnet` (`opus`도 가능하지만 한도 소모가 큼)
   - 전체 모델 ID(`claude-...`)를 입력해도 그대로 전달됩니다.
   - 그 외 이름(예: `gpt-4o`)이나 빈 값은 `config.json`의 기본 모델로 대체됩니다.
5. 권장: RimTalk의 **대화 빈도를 낮게** 설정하세요.

### 브리지의 요청 처리 방식

- **스트리밍**: RimTalk는 항상 `stream: true`로 요청합니다. 브리지는 SSE로 응답하고, `stream_options.include_usage`가 있으면 토큰 사용량 청크도 보냅니다.
- **system 메시지**: Claude Code 기본 시스템 프롬프트를 **대체**합니다. 그래서 코딩 비서 말투가 나오지 않습니다.
- **user/assistant 메시지**: 역할 라벨을 붙여 하나의 프롬프트로 합칩니다.
- **비활성화 항목**: 도구, MCP, CLAUDE.md·settings, 슬래시 명령, 세션 기록 저장, 확장 사고(thinking)를 모두 끕니다. 최대 1턴입니다.
- **`response_format: json_schema` / `json_object`** (RimTalk 고급 설정의 Structured Output 샘플): 시스템 프롬프트에 "이 스키마의 JSON으로만 답하라"는 지시를 추가하는 방식으로 전달합니다. SDK의 `output_format`은 루트가 object인 스키마만 받는데, RimTalk 샘플은 루트가 array라서 강제하면 400 오류가 나기 때문입니다. 스트리밍은 유지됩니다.
- **이미지 입력**: 지원하지 않습니다 (`[image omitted]`로 대체됩니다).
- `temperature`, `max_tokens`, `reasoning_effort` 등은 Agent SDK에 대응 옵션이 없어 무시됩니다.

## 문제 해결

| 증상 | 원인 / 조치 |
|---|---|
| `HTTP 401 ... Not logged in` | 구독 로그인이 안 됨 → 위의 `auth login --claudeai` 실행 |
| `HTTP 429 ... usage limit` | 구독 사용량 한도 도달. 한도가 리셋될 때까지 대기하거나 RimTalk 빈도를 낮추세요 |
| `HTTP 504 Timed out` | 응답 지연. `timeout`을 늘리거나 동시성을 줄이세요 |
| 서버가 바로 종료되며 `ANTHROPIC_API_KEY ... 기동을 중단` | API 키 환경변수가 설정되어 있음. `start_bridge.bat`는 자체적으로 이 변수를 지우고 실행합니다 |
| 포트 사용 중 오류 | `config.json`의 `port`를 바꾸고, RimTalk URL도 같이 바꾸세요 |
| RimTalk에 대사가 안 뜨거나 JSON 오류 발생 | `--debug`로 실행해 요청/응답을 확인하세요. RimTalk 설정 → 톱니바퀴 → Sample #1 (Structured Output) 적용을 시도해 보세요 |

동작 확인 (PowerShell):

```powershell
Invoke-RestMethod http://127.0.0.1:8765/v1/models
Invoke-RestMethod http://127.0.0.1:8765/v1/chat/completions -Method Post -ContentType 'application/json; charset=utf-8' `
  -Body ([Text.Encoding]::UTF8.GetBytes('{"model":"haiku","messages":[{"role":"system","content":"너는 RimWorld 정착민이다. 한 문장으로 답해."},{"role":"user","content":"오늘 기분 어때?"}]}'))
```

## 주의 사항

- 이 사용량은 Claude 채팅·Claude Code와 **같은 구독 한도를 공유**합니다. RimTalk 대화 빈도는 낮게 두는 것을 권장합니다.
- Agent SDK는 CLI 프로세스를 매번 띄우기 때문에 일반 API보다 응답이 **수 초 더 느립니다**.
- Anthropic이 구독 기반 SDK 사용 정책을 개편하고 있습니다. 앞으로 별도 크레딧 체계로 바뀌는 등 이 방식의 과금이 달라질 수 있습니다 (참고: https://support.claude.com/en/articles/15036540).
- 인증은 공식 Agent SDK에 들어 있는 Claude CLI의 로그인 상태만 사용합니다. 자격증명 파일을 복사하거나 추출하지 않습니다.
