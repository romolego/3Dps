@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion

REM ============================================================
REM 3Dps — One-way sync: GitHub -> local (FORCED)
REM Принудительно приводит локальную копию к состоянию origin/main.
REM Локальные папки из списка ниже НЕ удаляются.
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
REM ─────────────────────────────────────────────────────────────

echo ========================================
echo 3Dps - Sync GitHub to local (FORCED)
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
echo [ERROR] Репозиторий git не найден выше: %SCRIPT_DIR%
echo         Этот bat-файл должен находиться внутри git-рабочей копии.
echo         Сначала выполните sync_local_to_github_3dps.bat для инициализации.
goto :END_FAIL

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

echo === SCRIPT : %SCRIPT_DIR%
echo === REPO   : %REPO%
echo === ORIGIN : %ORIGIN_URL%
echo.

REM ── Добавление локальных ignore-правил в .git\info\exclude ──
call :ENSURE_EXCLUDE_LINE "%EXCL_01%"
call :ENSURE_EXCLUDE_LINE "%EXCL_02%"
call :ENSURE_EXCLUDE_LINE "%EXCL_03%"
call :ENSURE_EXCLUDE_LINE "%EXCL_04%"
call :ENSURE_EXCLUDE_LINE "%EXCL_05%"
call :ENSURE_EXCLUDE_LINE "%EXCL_06%"
call :ENSURE_EXCLUDE_LINE "%EXCL_07%"

REM ── Проверка наличия локальных изменений ─────────────────────
set "DIRTY="
for /f "delims=" %%S in ('git -C "%REPO%" status --porcelain 2^>nul') do set "DIRTY=1"

if defined DIRTY (
  echo [WARN] Есть локальные изменения. Они будут УДАЛЕНЫ ^(reset --hard + clean^).
  echo.
  git -C "%REPO%" status -sb
  echo.
  setlocal EnableDelayedExpansion
  set "CONFIRM="
  set /p "CONFIRM=Продолжить и перетереть локалку из GitHub? (y/N): "
  set "CONFIRM=!CONFIRM: =!"
  if /i not "!CONFIRM!"=="y" (
    echo [CANCELLED] Операция отменена пользователем.
    endlocal
    goto :END_FAIL
  )
  endlocal
)

REM ── git fetch origin ─────────────────────────────────────────
echo === git fetch origin
git -C "%REPO%" fetch origin
if errorlevel 1 goto :END_FAIL

REM ── Переключение на main ─────────────────────────────────────
echo.
echo === git checkout main
git -C "%REPO%" checkout main >nul 2>&1
if errorlevel 1 (
  echo        main не найдена локально, создаём из origin/main...
  git -C "%REPO%" checkout -B main origin/main
  if errorlevel 1 goto :END_FAIL
)

REM ── git reset --hard origin/main ─────────────────────────────
echo.
echo === git reset --hard origin/main
git -C "%REPO%" reset --hard origin/main
if errorlevel 1 goto :END_FAIL

REM ── git clean -fd (с сохранением локальных путей) ────────────
echo.
echo === git clean -fd (keep local-only paths/files)
REM ВАЖНО: -e использует паттерны gitignore:
REM  - для папок достаточно имени
REM  - для файлов — просто имя файла
REM  - для "!" в имени папки: экранируем через ^!
setlocal DisableDelayedExpansion
git -C "%REPO%" clean -fd ^
  -e ".venv" ^
  -e "projects" ^
  -e "downloads" ^
  -e "tools" ^
  -e ".cursor" ^
  -e "structure.txt" ^
  -e "^!материалы курсору"
endlocal
if errorlevel 1 goto :END_FAIL

echo.
echo === git status -sb (after)
git -C "%REPO%" status -sb
echo.
echo ========================================
echo [OK] Локальная main принудительно
echo      синхронизирована с origin/main.
echo ========================================
echo.
echo Локальные папки (.venv, projects, downloads,
echo tools, .cursor, !материалы курсору, structure.txt)
echo сохранены и не удалены.
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
