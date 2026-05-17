# Архитектура проекта 3Dps

## Архитектурная модель

Текущее состояние проекта — локальное монолитное приложение:

- бэкенд: `backend/main.py`;
- фронтенд: `frontend/index.html`;
- данные: файловая система в `projects/`;
- запуск: Windows-скрипты `start_3dps.bat` и `stop_3dps.bat`.

## Компоненты

| Компонент | Файл/папка | Роль |
| --- | --- | --- |
| Backend | `backend/main.py` | FastAPI, FFmpeg/OpenCV/Pillow-логика, файловое хранение, API |
| Frontend | `frontend/index.html` | HTML/CSS/JS SPA, экраны, роутинг, viewer, работа с аннотациями |
| Runtime scripts | `start_3dps.bat`, `stop_3dps.bat` | Запуск/остановка сервиса, PID, health-check |
| Persistent storage | `projects/` | Все проекты, метаданные, кадры, аннотации, настройки |
| Shared assets | `assets/fonts/DejaVuSans.ttf` | Шрифт для кириллицы при рендеринге/экспорте |
| Runtime artifacts | `.runtime/` | PID, логи сервера, временные служебные файлы |

## Поток работы системы

1. `start_3dps.bat` проверяет Python, `.venv`, зависимости и `ffmpeg`.
2. Скрипт запускает `python main.py --service`.
3. Бэкенд пишет PID в `.runtime/server.pid`, логирует в `.runtime/server.log` и открывает сервис на `127.0.0.1:8000`.
4. Браузер открывает SPA.
5. SPA работает как трёхэкранное приложение:
   - `upload`
   - `settings`
   - `viewer`
6. Пользователь создаёт или открывает проект, а данные сохраняются в файловую структуру внутри `projects/<id>/`.

## Пользовательские маршруты

Подтверждённые браузерные маршруты:

- `/` — главная/экран загрузки;
- `/project/<id>` — восстановление проекта и переход в `settings` или `viewer`.

Фронтенд использует History API и собственный роутер в `frontend/index.html`:

- `parseRoute()`
- `pushRoute()`
- `navigateHome()`
- `navigateToProject()`
- `popstate`-обработчик
- startup restore при загрузке страницы

Это важно: канонический route проекта уже существует и должен считаться частью внешнего контракта.

## Группы API

Основные группы API в `backend/main.py`:

- сервис и SPA:
  - `/health`
  - `/`
  - `/project/{pid}`
- жизненный цикл проекта:
  - `/api/upload`
  - `/api/projects`
  - `/api/projects/{pid}`
  - `/api/project/{pid}/save`
  - `/api/project/{pid}` `DELETE`
  - `/api/import-zip`
- генерация и просмотр кадров:
  - preview endpoints
  - `/api/projects/{pid}/estimate`
  - `/api/projects/{pid}/generate`
  - `/api/projects/{pid}/progress`
  - `/api/projects/{pid}/frames/stop`
  - `/api/projects/{pid}/frames`
  - `/api/projects/{pid}/frames/{idx}`
  - `/api/projects/{pid}/thumbs/{idx}`
- аннотации:
  - `/api/projects/{pid}/markers`
  - `/api/projects/{pid}/marker_types`
  - `/api/projects/{pid}/zones`
  - `/api/projects/{pid}/zone_types`
  - `/api/projects/{pid}/annotations`
- экспорт:
  - `/api/projects/{pid}/export`
  - `/api/projects/{pid}/export_advanced`
- центрирование:
  - `/api/projects/{pid}/roi`
  - `/api/projects/{pid}/generate_centered`
  - `/api/projects/{pid}/centered_status`
  - `/api/projects/{pid}/centered_frames`
- глобальные настройки:
  - `/api/app-settings`

## Хранение данных

Типовая структура проекта:

```text
projects/
  app_settings.json
  <project_id>/
    project.json
    metadata.json
    original_<video>.mp4
    frames/
    thumbs/
    preview_cache/
    quality_preview_cache/
    markers.json
    marker_types.json
    zones.json
    zone_types.json
    roi.json
    index.json
    centered_frames/
    centered_index.json
```

В проекте уже поддерживаются и legacy-слои совместимости, когда `metadata.json` восстанавливается из `project.json` или `index.json`.

## Технические свойства текущего решения

- Бэкенд и фронтенд остаются монолитными файлами.
- Прогресс генерации и флаги отмены частично держатся в памяти процесса.
- Каноническое хранилище — файловая система, не БД.
- Важные внешние локальные зависимости:
  - `ffmpeg`/`ffprobe`
  - `opencv-contrib-python`
  - `numpy`
  - `Pillow`
- Для кириллицы используется bundled-шрифт `DejaVuSans.ttf`.

## Текущие ограничения и технический долг

- `backend/main.py` и `frontend/index.html` уже велики и совмещают много обязанностей.
- В коде присутствуют временные отладочные вставки `agent log` и `FIX H*`.
- Есть обращения к `.cursor/debug.log`.
- Во фронтенде есть отправка диагностических событий на `http://127.0.0.1:7248/...`; это следует считать временной инструментальной логикой, а не целевым контуром системы.
- Скрипты запуска ориентированы на Windows и порт `8000`.

## Практический вывод

Перед крупной разработкой нельзя исходить из предположения, что проект уже разделён по слоям или подготовлен к серверному масштабу. Фактическая точка входа — монолитная локальная система с сильной привязкой к файловому состоянию проекта.
