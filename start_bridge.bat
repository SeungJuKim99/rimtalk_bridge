@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem API 키 과금 경로 차단: 이 창의 프로세스에서만 제거 (시스템 설정은 건드리지 않음)
set "ANTHROPIC_API_KEY="
set "ANTHROPIC_AUTH_TOKEN="

if not exist ".venv\Scripts\python.exe" (
  echo [설치] 가상환경을 만듭니다...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 ( echo Python 3.10 이상이 필요합니다. & pause & exit /b 1 )
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

".venv\Lib\site-packages\claude_agent_sdk\_bundled\claude.exe" auth status | findstr /c:"\"loggedIn\": true" >nul
if errorlevel 1 (
  echo [경고] Claude 구독 로그인이 되어 있지 않습니다. 로그인 창을 엽니다...
  ".venv\Lib\site-packages\claude_agent_sdk\_bundled\claude.exe" auth login --claudeai
)

".venv\Scripts\python.exe" bridge.py %*
pause
