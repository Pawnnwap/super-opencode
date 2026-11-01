@echo off
rem Codex launcher for super-opencode supervised runs: dedicated CODEX_HOME
rem (clean tool surface) + the local Responses-compat proxy as model provider.
rem --dangerously-bypass-hook-trust: the run-side PostToolUse Python edit gate
rem is vetted by this repo (it is our own script), so it needs no interactive
rem trust entry; the flag is per-invocation and never disables approvals.
set "CODEX_HOME=E:\pyprojects\super-opencode\.codex_home_soprun"
set "NO_PROXY=127.0.0.1,localhost"
"C:\Users\a\AppData\Roaming\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe" --dangerously-bypass-hook-trust %*
