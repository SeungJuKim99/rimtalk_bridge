"""RimTalk -> Claude Agent SDK bridge.

OpenAI Chat Completions 호환 로컬 서버. 구독 로그인 상태의 Claude Agent SDK를 호출한다.
API 키는 사용하지 않는다 (ANTHROPIC_API_KEY가 있으면 기동을 거부한다).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator

# 이 변수가 있으면 CLI가 구독 대신 API 과금 경로를 쓰므로 기동 시 거부한다.
_FORBIDDEN_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultError,
    ResultMessage,
    StreamEvent,
    TextBlock,
    query,
)

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("bridge")

# --- 설정 ------------------------------------------------------------------
DEFAULTS: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8765,
    "model": "haiku",           # haiku / sonnet / opus 별칭 또는 전체 모델 ID
    "concurrency": 2,
    "timeout": 120,             # 초
    "workdir": str(BASE_DIR / "workspace"),
    "debug": False,
}
# /v1/models 에 노출할 이름. 요청의 model 값이 이 중 하나면 그대로 사용,
# 그 외(예: gpt-4o, 빈 값)는 기본 모델로 대체.
MODEL_ALIASES = ["haiku", "sonnet", "opus"]


def load_config(argv: list[str] | None = None) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    cfg_path = BASE_DIR / "config.json"
    if cfg_path.exists():
        cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
    env_map = {
        "BRIDGE_PORT": ("port", int),
        "BRIDGE_MODEL": ("model", str),
        "BRIDGE_CONCURRENCY": ("concurrency", int),
        "BRIDGE_TIMEOUT": ("timeout", float),
        "BRIDGE_WORKDIR": ("workdir", str),
    }
    for env, (key, cast) in env_map.items():
        if os.environ.get(env):
            cfg[key] = cast(os.environ[env])

    p = argparse.ArgumentParser(description="RimTalk Claude bridge")
    p.add_argument("--port", type=int)
    p.add_argument("--model")
    p.add_argument("--concurrency", type=int)
    p.add_argument("--timeout", type=float)
    p.add_argument("--debug", action="store_true", default=None)
    args = p.parse_args(argv)
    for k, v in vars(args).items():
        if v is not None:
            cfg[k] = v
    cfg["host"] = "127.0.0.1"  # 외부 노출 금지: 설정으로도 변경 불가
    return cfg


CFG: dict[str, Any] = {}
SEM: asyncio.Semaphore | None = None


class UTF8JSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"  # 일부 클라이언트의 Latin-1 오해석 방지


app = FastAPI(title="RimTalk Claude Bridge", default_response_class=UTF8JSONResponse)


# --- 유틸 -------------------------------------------------------------------
def openai_error(status: int, message: str, etype: str = "server_error", code: str | None = None):
    return UTF8JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": etype, "param": None, "code": code}},
    )


def content_to_text(content: Any) -> str:
    """OpenAI content(문자열 또는 파트 배열)를 텍스트로. 이미지는 생략 표시."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") == "text":
                parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                parts.append("[image omitted]")
        elif isinstance(part, str):
            parts.append(part)
    return "\n".join(parts)


def build_prompt(messages: list[dict]) -> tuple[str, str]:
    """system 메시지는 시스템 프롬프트로, 나머지는 역할 표시를 붙여 직렬화."""
    system_parts, convo = [], []
    for m in messages:
        role = m.get("role", "user")
        text = content_to_text(m.get("content"))
        if role in ("system", "developer"):
            system_parts.append(text)
        else:
            convo.append((role, text))

    if len(convo) == 1 and convo[0][0] == "user":
        prompt = convo[0][1]
    else:
        lines = []
        for role, text in convo:
            label = {"user": "User", "assistant": "Assistant"}.get(role, role.capitalize())
            lines.append(f"[{label}]\n{text}")
        lines.append("[Assistant]")
        prompt = (
            "Below is the conversation so far. Continue it by writing only the next "
            "Assistant reply (without the [Assistant] label).\n\n" + "\n\n".join(lines)
        )
    system = "\n\n".join(p for p in system_parts if p) or "You are a helpful assistant."
    return system, prompt


def format_instruction(body: dict) -> str:
    """response_format을 시스템 프롬프트 지시문으로 변환.

    SDK의 output_format은 루트가 object인 스키마만 받는데, RimTalk 샘플은 루트가
    array라 400 오류가 난다. 그래서 강제 대신 지시문으로 전달한다 (스트리밍도 유지됨).
    """
    rf = body.get("response_format")
    if not isinstance(rf, dict):
        return ""
    if rf.get("type") == "json_schema":
        schema = (rf.get("json_schema") or {}).get("schema")
        if schema:
            return ("\n\nOutput format: respond with ONLY valid JSON matching this JSON Schema. "
                    "No markdown code fences, no extra text.\n" + json.dumps(schema, ensure_ascii=False))
    if rf.get("type") == "json_object":
        return "\n\nOutput format: respond with ONLY a valid JSON object. No markdown code fences, no extra text."
    return ""


def resolve_model(requested: str | None) -> str:
    if requested:
        r = requested.strip()
        if r in MODEL_ALIASES or r.startswith("claude-"):
            return r
    return CFG["model"]


def make_options(system: str, model: str, body: dict, stream: bool) -> ClaudeAgentOptions:
    opts: dict[str, Any] = dict(
        system_prompt=system,              # 문자열 = Claude Code 기본 프롬프트 대체
        model=model,
        tools=[],                          # 내장 도구 전부 비활성화
        allowed_tools=[],
        mcp_servers={},
        strict_mcp_config=True,
        setting_sources=[],                # CLAUDE.md / settings.json 로드 안 함
        max_turns=1,
        cwd=CFG["workdir"],
        include_partial_messages=stream,
        thinking={"type": "disabled"},
        extra_args={"no-session-persistence": None, "disable-slash-commands": None},
        stderr=lambda line: log.debug("[cli] %s", line.rstrip()),
    )
    return ClaudeAgentOptions(**opts)


def map_usage(usage: dict | None) -> dict:
    u = usage or {}
    prompt = (
        int(u.get("input_tokens") or 0)
        + int(u.get("cache_read_input_tokens") or 0)
        + int(u.get("cache_creation_input_tokens") or 0)
    )
    completion = int(u.get("output_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


class BridgeError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def raise_error(detail: str, status: int | None, code: str | None, assistant_error: str | None):
    """CLI 오류 결과를 OpenAI 형식으로 매핑할 BridgeError로 변환."""
    text = detail.lower()
    if status == 429 or assistant_error == "rate_limit" or "usage limit" in text or "limit reached" in text:
        log.error("사용량 한도/레이트 리밋: %s", detail)
        raise BridgeError(429, f"Claude usage limit / rate limit: {detail}", "rate_limit")
    if assistant_error == "authentication_failed" or "not logged in" in text or status == 401:
        log.error("인증 실패 - start_bridge.bat 을 다시 실행해 로그인하세요: %s", detail)
        raise BridgeError(401, f"Claude authentication failed: {detail}", "authentication_failed")
    log.error("Claude 오류 (%s): %s", code, detail)
    raise BridgeError(status or 502, f"Claude error ({code}): {detail}", code)


def check_result(res: ResultMessage | None, assistant_error: str | None):
    if res is None:
        raise BridgeError(502, "Claude CLI가 결과 없이 종료되었습니다.")
    if res.is_error:
        detail = "; ".join(res.errors or []) or (res.result or res.subtype)
        raise_error(detail, res.api_error_status, assistant_error or res.subtype, assistant_error)


def from_result_error(e: ResultError, assistant_error: str | None):
    detail = "; ".join(e.errors) or e.result or str(e)
    raise_error(detail, e.api_error_status, assistant_error or e.subtype or e.terminal_reason, assistant_error)


def final_text(res: ResultMessage, collected: list[str]) -> str:
    if res.result:
        return res.result
    return "".join(collected)


# --- 실행 -------------------------------------------------------------------
async def run_once(system: str, prompt: str, model: str, body: dict):
    """비스트리밍 1회 호출. (text, usage, model) 반환."""
    collected: list[str] = []
    result: ResultMessage | None = None
    a_err: str | None = None
    used_model = model
    try:
        async for msg in query(prompt=prompt, options=make_options(system, model, body, False)):
            if isinstance(msg, AssistantMessage):
                used_model = msg.model or used_model
                a_err = msg.error or a_err
                collected += [b.text for b in msg.content if isinstance(b, TextBlock)]
            elif isinstance(msg, ResultMessage):
                result = msg
    except ResultError as e:
        from_result_error(e, a_err)
    check_result(result, a_err)
    return final_text(result, collected), map_usage(result.usage), used_model


async def run_stream(system: str, prompt: str, model: str, body: dict) -> AsyncIterator[tuple[str, Any]]:
    """('delta', text) ... ('done', (full_text, usage, model)) 이벤트를 낸다."""
    collected: list[str] = []
    streamed = False
    result: ResultMessage | None = None
    a_err: str | None = None
    used_model = model
    opts = make_options(system, model, body, True)
    try:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, StreamEvent):
                ev = msg.event
                if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    streamed = True
                    yield "delta", ev["delta"]["text"]
            elif isinstance(msg, AssistantMessage):
                used_model = msg.model or used_model
                a_err = msg.error or a_err
                collected += [b.text for b in msg.content if isinstance(b, TextBlock)]
            elif isinstance(msg, ResultMessage):
                result = msg
    except ResultError as e:
        from_result_error(e, a_err)
    check_result(result, a_err)
    text = final_text(result, collected)
    if not streamed and text:
        yield "delta", text
    yield "done", (text, map_usage(result.usage), used_model)


# --- 엔드포인트 -------------------------------------------------------------
@app.get("/v1/models")
@app.get("/models")
async def list_models():
    now = int(time.time())
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": now, "owned_by": "anthropic"} for m in MODEL_ALIASES],
    }


@app.get("/health")
@app.get("/v1/health")
async def health():
    return {"status": "ok", "default_model": CFG["model"]}


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    t0 = time.perf_counter()
    raw = await request.body()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return openai_error(400, "Invalid JSON body", "invalid_request_error")
    if CFG["debug"]:
        log.info("[debug] 원본 요청:\n%s", json.dumps(body, ensure_ascii=False, indent=2))

    messages = body.get("messages") or []
    if not messages:
        return openai_error(400, "messages is required", "invalid_request_error")
    model = resolve_model(body.get("model"))
    system, prompt = build_prompt(messages)
    system += format_instruction(body)
    stream = bool(body.get("stream"))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    tag = f"{cid[-6:]} model={model} stream={stream}"
    log.info("요청 %s (요청 model=%r)", tag, body.get("model"))

    def done_log(ok: bool, extra: str = ""):
        log.info("%s %s %.1fs %s", "성공" if ok else "실패", tag, time.perf_counter() - t0, extra)

    if not stream:
        try:
            async with SEM:
                text, usage, used_model = await asyncio.wait_for(
                    run_once(system, prompt, model, body), CFG["timeout"]
                )
        except asyncio.TimeoutError:
            done_log(False, "timeout")
            return openai_error(504, f"Timed out after {CFG['timeout']}s", "timeout")
        except BridgeError as e:
            done_log(False, e.message)
            return openai_error(e.status, e.message, "api_error", e.code)
        except ClaudeSDKError as e:
            log.exception("SDK 오류")
            done_log(False, str(e))
            return openai_error(502, f"Claude Agent SDK error: {e}")
        done_log(True, f"tokens={usage['total_tokens']}")
        if CFG["debug"]:
            log.info("[debug] 응답: %s", text)
        return UTF8JSONResponse({
            "id": cid, "object": "chat.completion", "created": created, "model": used_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": usage,
        })

    # 첫 이벤트를 미리 받아 둔다: 인증/한도 오류는 SSE가 아닌 HTTP 오류 코드로 돌려주기 위함.
    deadline = time.monotonic() + CFG["timeout"]
    await SEM.acquire()
    gen = run_stream(system, prompt, model, body)

    async def next_event():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return await asyncio.wait_for(gen.__anext__(), remaining)

    try:
        first = await next_event()
    except StopAsyncIteration:
        first = None
    except Exception as e:  # noqa: BLE001
        SEM.release()
        await gen.aclose()
        if isinstance(e, asyncio.TimeoutError):
            done_log(False, "timeout")
            return openai_error(504, f"Timed out after {CFG['timeout']}s", "timeout")
        if isinstance(e, BridgeError):
            done_log(False, e.message)
            return openai_error(e.status, e.message, "api_error", e.code)
        log.exception("SDK 오류")
        done_log(False, str(e))
        return openai_error(502, f"Claude Agent SDK error: {e}")

    async def sse() -> AsyncIterator[bytes]:
        def chunk(delta: dict, finish: str | None = None, usage: dict | None = None, choices: bool = True):
            obj: dict[str, Any] = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if choices else []}
            if usage is not None:
                obj["usage"] = usage
            return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")

        usage = None
        event = first
        try:
            yield chunk({"role": "assistant", "content": ""})
            while event is not None:
                kind, payload = event
                if kind == "delta":
                    yield chunk({"content": payload})
                else:
                    text, usage, _ = payload
                    if CFG["debug"]:
                        log.info("[debug] 응답: %s", text)
                try:
                    event = await next_event()
                except StopAsyncIteration:
                    event = None
            yield chunk({}, "stop")
            if include_usage and usage:
                yield chunk({}, usage=usage, choices=False)
            yield b"data: [DONE]\n\n"
            done_log(True, f"tokens={(usage or {}).get('total_tokens')}")
        except asyncio.TimeoutError:
            done_log(False, "timeout")
            yield _sse_error(f"Timed out after {CFG['timeout']}s", "timeout")
        except BridgeError as e:
            done_log(False, e.message)
            yield _sse_error(e.message, e.code)
        except ClaudeSDKError as e:
            log.exception("SDK 오류")
            done_log(False, str(e))
            yield _sse_error(f"Claude Agent SDK error: {e}")
        finally:
            await gen.aclose()
            SEM.release()

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse_error(message: str, code: str | None = None) -> bytes:
    err = {"error": {"message": message, "type": "api_error", "code": code}}
    return f"data: {json.dumps(err, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode("utf-8")


# --- 진입점 -----------------------------------------------------------------
def main():
    global CFG, SEM
    CFG = load_config()
    logging.basicConfig(
        level=logging.DEBUG if CFG["debug"] else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    )
    for noisy in ("asyncio", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    present = [k for k in _FORBIDDEN_ENV if os.environ.get(k)]
    if present:
        log.error("%s 환경변수가 설정되어 있습니다. API 과금 경로를 막기 위해 기동을 중단합니다. "
                  "start_bridge.bat 로 실행하거나 변수를 제거하세요.", ", ".join(present))
        sys.exit(1)
    for k in _FORBIDDEN_ENV:
        os.environ.pop(k, None)

    Path(CFG["workdir"]).mkdir(parents=True, exist_ok=True)
    SEM = asyncio.Semaphore(int(CFG["concurrency"]))
    log.info("RimTalk Claude 브리지 시작: http://%s:%s/v1  기본 모델=%s 동시성=%s 타임아웃=%ss 디버그=%s",
             CFG["host"], CFG["port"], CFG["model"], CFG["concurrency"], CFG["timeout"], CFG["debug"])
    uvicorn.run(app, host=CFG["host"], port=int(CFG["port"]), log_level="warning")


if __name__ == "__main__":
    main()
