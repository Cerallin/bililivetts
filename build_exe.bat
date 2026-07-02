@echo off
REM 使用 pyinstaller 将程序打包为单文件可执行文件
REM pyinstaller 会自动追踪 import 并包含 bili_client / tts_engine / ui_popup 等模块
python -m PyInstaller --noconsole --onefile live_tts.py
if %ERRORLEVEL% neq 0 (
  echo 打包失败
  exit /b %ERRORLEVEL%
)
echo 打包成功，生成文件位于 dist\live_tts.exe
