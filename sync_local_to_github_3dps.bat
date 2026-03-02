@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion

REM ============================================================
REM 3Dps — One-way sync: local -> GitHub (commit + push)
REM Auto-detect repo root by searching for .git up the tree.
REM If not found: git init in parent of bat location and set origin.
REM Local-only ignores via .git\info\exclude (NOT pushed).
REM ============================================================

REM ── НАСТРОЙКИ (заполните один раз) ──────────────────────────
REM Полный URL вашего репозитория на GitHub:
set "EXPECTED_ORIGIN_URL=https://github.com/romolego/3Dps"
REM Строка для проверки origin через findstr (например: user/repo):
set "EXPECTED_ORIGIN_MATCH=romolego/3Dps"
REM ─────────────────────────────────────────────────────────────

REM ── Список локальных путей, которые НЕ должны попадать в Git ─
REM Exclude-паттерны (для .git\info\exclude):
set "EXCL_01=/.venv/"
set "EXCL_02=/projects/"
set "EXCL_03=/downloads/"
set "EXCL_04=/tools/"
set "EXCL_05=/.cursor/"
set "EXCL_06=/!материалы курсору/"
set "EXCL_07=/structure.txt"
REM Пути для git rm --cached:
set "UNTRACK_DIRS=.venv projects downloads tools .cursor"
set "UNTRACK_BANG=!материалы курсору"
set "UNTRACK_FILES=structure.txt"
REM ─────────────────────────────────────────────────────────────

echo ========================================
echo 3Dps - Sync local to GitHub
echo ========================================
echo.

REM ── Проверка настроек ────────────────────────────────────────
if "%EXPECTED_ORIGIN_URL%"=="" (
  echo [ERROR] Переменная EXPECTED_ORIGIN_URL не задана.
  echo         Откройте этот bat-файл и впишите URL репозитория GitHub.
  goto :END_FAIL
)
if "%EXPECTED_ORIGIN_MATCH%"=="" (
  echo [ERROR] Переменная EXPECTED_ORIGIN_MATCH не задана.
  echo         Откройте этот bat-файл и впишите строку для проверки origin
  echo         ^(например: username/reponame^).
  goto :END_FAIL
)

REM ── Проверка git ─────────────────────────────────────────────
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

where git >nul 2>&1
if errorlevel 1 (
  echo [ERROR] git не найден в PATH.
  echo         Установите Git и убедитесь, что он доступен из cmd.exe.
  goto :END_FAIL
)

REM ── Поиск корня репозитория (.git) ───────────────────────────
set "CAND=%SCRIPT_DIR%"
:SEARCH_UP
if exist "%CAND%\.git" (
  set "REPO=%CAND%"
  goto :FOUND_REPO
)
for %%P in ("%CAND%\..") do set "PARENT=%%~fP"
if /i "%PARENT%"=="%CAND%" goto :NO_REPO
set "CAND=%PARENT%"
goto :SEARCH_UP

:NO_REPO
REM ── Репозиторий не найден — инициализация ────────────────────
for %%P in ("%SCRIPT_DIR%") do set "REPO=%%~fP"

echo [WARN] .git не найден выше: %SCRIPT_DIR%
echo        Инициализация git-репозитория в: %REPO%
echo.

git -C "%REPO%" init
if errorlevel 1 (
  echo [ERROR] git init не удался.
  goto :END_FAIL
)

REM Создание ветки main
git -C "%REPO%" checkout -B main >nul 2>&1

REM Добавление origin
git -C "%REPO%" remote get-url origin >nul 2>&1
if errorlevel 1 (
  git -C "%REPO%" remote add origin "%EXPECTED_ORIGIN_URL%"
  if errorlevel 1 (
    echo [ERROR] Не удалось добавить remote origin.
    goto :END_FAIL
  )
  echo [OK] origin добавлен: %EXPECTED_ORIGIN_URL%
)

:FOUND_REPO

REM ── Определение git-dir и путей ─────────────────────────────
set "GITDIR="
for /f "delims=" %%G in ('git -C "%REPO%" rev-parse --git-dir 2^>nul') do set "GITDIR=%%G"
if "%GITDIR%"=="" (
  echo [ERROR] Невозможно определить git-dir.
  goto :END_FAIL
)

set "GITDIR_ABS="
if "%GITDIR:~1,1%"==":" (
  set "GITDIR_ABS=%GITDIR%"
) else (
  set "GITDIR_ABS=%REPO%\%GITDIR%"
)

set "LOCKPATH=%GITDIR_ABS%\index.lock"
set "EXCLUDE=%GITDIR_ABS%\info\exclude"

REM ── Проверка index.lock ──────────────────────────────────────
if exist "%LOCKPATH%" (
  echo [ERROR] Файл index.lock существует. Другой git-процесс может быть запущен.
  echo         Закройте все git-приложения и удалите файл:
  echo         "%LOCKPATH%"
  goto :END_FAIL
)

REM ── Проверка origin ──────────────────────────────────────────
set "ORIGIN_URL="
for /f "delims=" %%U in ('git -C "%REPO%" remote get-url origin 2^>nul') do set "ORIGIN_URL=%%U"
if "%ORIGIN_URL%"=="" (
  echo [ERROR] remote "origin" не задан в репозитории.
  echo         Ожидается: %EXPECTED_ORIGIN_URL%
  goto :END_FAIL
)

echo %ORIGIN_URL% | findstr /i "%EXPECTED_ORIGIN_MATCH%" >nul
if errorlevel 1 (
  echo [ERROR] origin указывает на другой репозиторий:
  echo         %ORIGIN_URL%
  echo         Ожидается: %EXPECTED_ORIGIN_URL%
  goto :END_FAIL
)

REM ── Определение текущей ветки ────────────────────────────────
set "BRANCH="
for /f "delims=" %%B in ('git -C "%REPO%" symbolic-ref --quiet --short HEAD 2^>nul') do set "BRANCH=%%B"
if "%BRANCH%"=="" set "BRANCH=main"

echo === SCRIPT : %SCRIPT_DIR%
echo === REPO   : %REPO%
echo === ORIGIN : %ORIGIN_URL%
echo === BRANCH : %BRANCH%
echo.

REM ── Добавление локальных ignore-правил в .git\info\exclude ──
call :ENSURE_EXCLUDE_LINE "%EXCL_01%"
call :ENSURE_EXCLUDE_LINE "%EXCL_02%"
call :ENSURE_EXCLUDE_LINE "%EXCL_03%"
call :ENSURE_EXCLUDE_LINE "%EXCL_04%"
call :ENSURE_EXCLUDE_LINE "%EXCL_05%"
call :ENSURE_EXCLUDE_LINE "%EXCL_06%"
call :ENSURE_EXCLUDE_LINE "%EXCL_07%"

REM ── Убрать локальные пути из индекса (файлы остаются на диске)
echo === untrack ignored paths (keep files locally)
git -C "%REPO%" rm -r --cached --ignore-unmatch -- %UNTRACK_DIRS% >nul 2>&1
setlocal EnableDelayedExpansion
git -C "%REPO%" rm -r --cached --ignore-unmatch -- "!UNTRACK_BANG!" >nul 2>&1
endlocal
git -C "%REPO%" rm --cached --ignore-unmatch -- %UNTRACK_FILES% >nul 2>&1
echo.

REM ── git status (до) ──────────────────────────────────────────
echo === git status -sb (before)
git -C "%REPO%" status -sb
if errorlevel 1 goto :END_FAIL
echo.

REM ── git add -A ───────────────────────────────────────────────
echo === git add -A
git -C "%REPO%" add -A
if errorlevel 1 goto :END_FAIL

REM ── Проверка: есть ли что коммитить? ─────────────────────────
git -C "%REPO%" diff --cached --quiet
if errorlevel 1 goto :DO_COMMIT
echo === Нечего коммитить (staged пуст), переходим к push...
goto :PUSH_ONLY

:DO_COMMIT
echo.
set "MSG="
set /p "MSG=Сообщение коммита (Enter = update): "
if "%MSG%"=="" set "MSG=update"

echo === git commit -m "%MSG%"
git -C "%REPO%" commit -m "%MSG%"
if errorlevel 1 goto :END_FAIL

:PUSH_ONLY
echo.
echo === git fetch origin
git -C "%REPO%" fetch origin
if errorlevel 1 goto :END_FAIL

echo === git push origin %BRANCH%
git -C "%REPO%" push origin "%BRANCH%"
if errorlevel 1 (
  echo.
  echo [WARN] Обычный push не прошёл. Пробуем --force-with-lease...
  git -C "%REPO%" push --force-with-lease origin "%BRANCH%"
  if errorlevel 1 (
    echo.
    echo [ERROR] git push не удался (даже с --force-with-lease).
    echo         Проверьте состояние репозитория вручную.
    goto :END_FAIL
  )
)

echo.
echo === git status -sb (after)
git -C "%REPO%" status -sb
echo.
echo === HEAD:
git -C "%REPO%" log --oneline --decorate -n 1
echo.
echo [OK] Готово. Локальные изменения отправлены в GitHub.
goto :END_OK

REM ================================================================
REM Вспомогательная функция: добавить строку в exclude, если её нет
REM ================================================================
:ENSURE_EXCLUDE_LINE
set "LINE=%~1"
if not exist "%EXCLUDE%" (
  if not exist "%GITDIR_ABS%\info" mkdir "%GITDIR_ABS%\info"
  type nul > "%EXCLUDE%"
)
findstr /l /x /c:"%LINE%" "%EXCLUDE%" >nul 2>&1
if errorlevel 1 (
  >>"%EXCLUDE%" echo %LINE%
)
exit /b 0

:END_FAIL
echo.
pause
exit /b 1

:END_OK
echo.
pause
exit /b 0
