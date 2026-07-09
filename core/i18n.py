"""Minimal dictionary-based localization for visible JARVIS UI text."""

from __future__ import annotations

import os


def _normalise_lang(value: str | None) -> str:
    code = (value or "ru").strip().lower().replace("_", "-")
    return code.split("-", 1)[0].split(".", 1)[0] or "ru"


LANG = _normalise_lang(
    os.environ.get("JARVIS_UI_LANG") or os.environ.get("JARVIS_LANG") or "ru"
)


_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "status.initialising": "INITIALISING",
        "status.muted": "MUTED",
        "status.speaking": "SPEAKING",
        "status.thinking": "THINKING",
        "status.processing": "PROCESSING",
        "status.listening": "LISTENING",
        "status.connecting": "CONNECTING",
        "status.connected": "CONNECTED",
        "status.reconnecting": "RECONNECTING",
        "status.sleeping": "SLEEPING",
        "status.error": "ERROR",
        "dialog.select_file": "Select a file for JARVIS",
        "dialog.filter.all": "All Files",
        "dialog.filter.images": "Images",
        "dialog.filter.documents": "Documents",
        "dialog.filter.data": "Data",
        "dialog.filter.code": "Code",
        "dialog.filter.audio": "Audio",
        "dialog.filter.video": "Video",
        "dialog.filter.archives": "Archives",
        "drop.idle": "Drop file here  or  Click to Browse",
        "drop.types": "Images · Video · Audio · PDF · Docs · Code · Data",
        "drop.release": "Release to load",
        "file.generic": "FILE",
        "camera.visual_input": "VISUAL INPUT",
        "camera.feed": "CAMERA FEED",
        "button.close": "CLOSE",
        "setup.required": "INITIALISATION REQUIRED",
        "setup.configure": "Configure J.A.R.V.I.S. before first boot.",
        "setup.api_key": "GEMINI API KEY",
        "setup.os": "OPERATING SYSTEM",
        "setup.auto_detected": "Auto-detected: {os}",
        "setup.initialise": "INITIALISE SYSTEMS",
        "remote.access": "REMOTE ACCESS",
        "remote.scan": "Scan with phone camera to connect instantly",
        "remote.manual": "Or enter manually:",
        "remote.new_key": "NEW KEY",
        "button.dismiss": "DISMISS",
        "remote.expires": "Key expires in  {minutes:02d}:{seconds:02d}",
        "remote.connected": "CONNECTED",
        "remote.ready": "Phone connected — JARVIS ready",
        "monitor.header": "SYS MONITOR",
        "monitor.uptime": "UP",
        "monitor.processes": "PROC",
        "monitor.os": "OS",
        "badge.ai_active": "AI CORE\nACTIVE",
        "badge.security": "SEC\nCLEARED",
        "badge.protocol": "PROTOCOL\nXLVIII",
        "panel.activity_log": "ACTIVITY LOG",
        "panel.file_upload": "FILE UPLOAD",
        "panel.command_input": "COMMAND INPUT",
        "file.no_file": "No file loaded — drop or click above to upload",
        "file.tell_jarvis": "Tell JARVIS what to do with it",
        "button.interrupt": "INTERRUPT",
        "button.microphone_active": "MICROPHONE ACTIVE",
        "button.microphone_muted": "MICROPHONE MUTED",
        "button.remote_control": "REMOTE CONTROL",
        "button.fullscreen": "FULLSCREEN",
        "button.desktop_shortcut": "CREATE DESKTOP SHORTCUT",
        "input.placeholder": "Type a command or question...",
        "content.briefing": "BRIEFING",
        "footer.shortcuts": "[F4] Mute  ·  [F11] Fullscreen",
        "footer.brand": "FatihMakes Industries  ·  MARK XLVIII  ·  CLASSIFIED",
        "footer.copyright": "© STARK INDUSTRIES",
        "header.subtitle": "Just A Rather Very Intelligent System",
        "setup.install_requirements": "Installing requirements...",
        "setup.install_playwright": "Installing Playwright browsers...",
        "setup.complete": "Setup complete! Run 'python main.py' to start MARK XXV.",
        "log.you": "You",
        "log.system": "SYS",
        "log.error": "ERR",
        "log.file": "FILE",
        "log.web": "Web",
        "log.desktop_shortcut_created": "SYS: Desktop shortcut created.",
        "log.shortcut_failed": "ERR: Shortcut failed — {error}",
        "log.file_loaded": "FILE: {name} ({size}) loaded",
        "log.dashboard_unavailable": "SYS: Dashboard not running — remote unavailable.",
        "log.remote_key_failed": "SYS: Could not generate remote key.",
        "log.remote_key_generated": "SYS: Remote key generated — manual: {url}",
        "log.mic_muted": "SYS: Microphone muted.",
        "log.mic_active": "SYS: Microphone active.",
        "log.initialised": "SYS: Initialised. OS={os}. JARVIS online.",
        "result.no_results": "No results found for: {query}",
        "result.search_results_for": "Search results for: {query}",
        "result.source": "Source",
        "result.no_news": "No news found for: {query}",
        "result.latest_news": "Latest news: {query}",
        "result.comparison": "Comparison — {aspect}",
        "result.search_query_required": "Please provide a search query.",
        "result.search_failed": "Search failed: {error}",
        "weather.city_missing": "Sir, the city is missing for the weather report.",
        "weather.browser_failed": "Sir, I couldn't open the browser for the weather report: {error}",
        "weather.showing": "Showing the weather for {city}, {when}, sir.",
        "date.today": "today",
    },
    "ru": {
        "status.initialising": "ЗАПУСК",
        "status.muted": "МИКРОФОН ВЫКЛЮЧЕН",
        "status.speaking": "ГОВОРЮ",
        "status.thinking": "ДУМАЮ",
        "status.processing": "ОБРАБОТКА",
        "status.listening": "СЛУШАЮ...",
        "status.connecting": "ПОДКЛЮЧЕНИЕ...",
        "status.connected": "ПОДКЛЮЧЕНО",
        "status.reconnecting": "ПЕРЕПОДКЛЮЧЕНИЕ...",
        "status.sleeping": "ОЖИДАНИЕ",
        "status.error": "ОШИБКА",
        "dialog.select_file": "Выберите файл для JARVIS",
        "dialog.filter.all": "Все файлы",
        "dialog.filter.images": "Изображения",
        "dialog.filter.documents": "Документы",
        "dialog.filter.data": "Данные",
        "dialog.filter.code": "Код",
        "dialog.filter.audio": "Аудио",
        "dialog.filter.video": "Видео",
        "dialog.filter.archives": "Архивы",
        "drop.idle": "Перетащите файл или нажмите для выбора",
        "drop.types": "Фото · Видео · Аудио · PDF · Док · Код · Данные",
        "drop.release": "Отпустите, чтобы загрузить",
        "file.generic": "ФАЙЛ",
        "camera.visual_input": "ВИЗУАЛЬНЫЙ ВВОД",
        "camera.feed": "КАМЕРА",
        "button.close": "ЗАКРЫТЬ",
        "setup.required": "ТРЕБУЕТСЯ НАСТРОЙКА",
        "setup.configure": "Настройте J.A.R.V.I.S. перед первым запуском.",
        "setup.api_key": "КЛЮЧ GEMINI API",
        "setup.os": "ОПЕРАЦИОННАЯ СИСТЕМА",
        "setup.auto_detected": "Автоопределено: {os}",
        "setup.initialise": "ЗАПУСТИТЬ СИСТЕМЫ",
        "remote.access": "УДАЛЕННЫЙ ДОСТУП",
        "remote.scan": "Отсканируйте камерой телефона для быстрого подключения",
        "remote.manual": "Или введите вручную:",
        "remote.new_key": "НОВЫЙ КЛЮЧ",
        "button.dismiss": "СКРЫТЬ",
        "remote.expires": "Ключ истекает через  {minutes:02d}:{seconds:02d}",
        "remote.connected": "ПОДКЛЮЧЕНО",
        "remote.ready": "Телефон подключен — JARVIS готов",
        "monitor.header": "МОНИТОР СИСТЕМЫ",
        "monitor.uptime": "РАБ",
        "monitor.processes": "ПРОЦ",
        "monitor.os": "ОС",
        "badge.ai_active": "ИИ\nАКТИВЕН",
        "badge.security": "ДОСТУП\nРАЗРЕШЕН",
        "badge.protocol": "ПРОТОКОЛ\nXLVIII",
        "panel.activity_log": "ЖУРНАЛ АКТИВНОСТИ",
        "panel.file_upload": "ЗАГРУЗКА ФАЙЛА",
        "panel.command_input": "ВВОД КОМАНДЫ",
        "file.no_file": "Файл не загружен — перетащите или нажмите выше",
        "file.tell_jarvis": "Скажите JARVIS, что сделать с ним",
        "button.interrupt": "ПРЕРВАТЬ",
        "button.microphone_active": "МИКРОФОН АКТИВЕН",
        "button.microphone_muted": "МИКРОФОН ВЫКЛЮЧЕН",
        "button.remote_control": "УДАЛЕННОЕ УПРАВЛЕНИЕ",
        "button.fullscreen": "ПОЛНЫЙ ЭКРАН",
        "button.desktop_shortcut": "СОЗДАТЬ ЯРЛЫК",
        "input.placeholder": "Введите команду или вопрос...",
        "content.briefing": "СВОДКА",
        "footer.shortcuts": "[F4] Микрофон  ·  [F11] Полный экран",
        "footer.brand": "FatihMakes Industries  ·  MARK XLVIII  ·  СЕКРЕТНО",
        "footer.copyright": "© STARK INDUSTRIES",
        "header.subtitle": "Просто очень интеллектуальная система",
        "setup.install_requirements": "Установка зависимостей...",
        "setup.install_playwright": "Установка браузеров Playwright...",
        "setup.complete": "Настройка завершена! Запустите MARK XXV командой 'python main.py'.",
        "log.you": "Вы",
        "log.system": "СИСТЕМА",
        "log.error": "ОШИБКА",
        "log.file": "ФАЙЛ",
        "log.web": "Веб",
        "log.desktop_shortcut_created": "СИСТЕМА: Ярлык на рабочем столе создан.",
        "log.shortcut_failed": "ОШИБКА: Не удалось создать ярлык — {error}",
        "log.file_loaded": "ФАЙЛ: {name} ({size}) загружен",
        "log.dashboard_unavailable": "СИСТЕМА: Панель не запущена — удаленный доступ недоступен.",
        "log.remote_key_failed": "СИСТЕМА: Не удалось создать ключ удаленного доступа.",
        "log.remote_key_generated": "СИСТЕМА: Ключ удаленного доступа создан — вручную: {url}",
        "log.mic_muted": "СИСТЕМА: Микрофон выключен.",
        "log.mic_active": "СИСТЕМА: Микрофон активен.",
        "log.initialised": "СИСТЕМА: Настройка завершена. ОС={os}. JARVIS онлайн.",
        "result.no_results": "Результаты не найдены для: {query}",
        "result.search_results_for": "Результаты поиска: {query}",
        "result.source": "Источник",
        "result.no_news": "Новости не найдены для: {query}",
        "result.latest_news": "Последние новости: {query}",
        "result.comparison": "Сравнение — {aspect}",
        "result.search_query_required": "Укажите поисковый запрос.",
        "result.search_failed": "Поиск не выполнен: {error}",
        "weather.city_missing": "Сэр, не указан город для прогноза погоды.",
        "weather.browser_failed": "Сэр, не удалось открыть браузер для прогноза погоды: {error}",
        "weather.showing": "Показываю погоду для {city}, {when}, сэр.",
        "date.today": "сегодня",
    },
}


def active_lang() -> str:
    return LANG if LANG in _MESSAGES else "en"


def t(key: str, **kwargs: object) -> str:
    lang = active_lang()
    template = _MESSAGES.get(lang, {}).get(key) or _MESSAGES["en"].get(key, key)
    if kwargs:
        return template.format(**kwargs)
    return template


def state_label(state: str) -> str:
    if not state:
        return t("status.initialising")
    key = f"status.{state.lower()}"
    lang = active_lang()
    return _MESSAGES.get(lang, {}).get(key) or _MESSAGES["en"].get(key) or state


def file_dialog_filter() -> str:
    return ";;".join([
        f"{t('dialog.filter.all')} (*.*)",
        f"{t('dialog.filter.images')} (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg)",
        f"{t('dialog.filter.documents')} (*.pdf *.docx *.txt *.md *.pptx)",
        f"{t('dialog.filter.data')} (*.csv *.xlsx *.json *.xml)",
        f"{t('dialog.filter.code')} (*.py *.js *.ts *.html *.css *.java *.cpp *.go)",
        f"{t('dialog.filter.audio')} (*.mp3 *.wav *.ogg *.m4a *.aac *.flac)",
        f"{t('dialog.filter.video')} (*.mp4 *.avi *.mov *.mkv *.wmv *.webm)",
        f"{t('dialog.filter.archives')} (*.zip *.rar *.tar *.gz *.7z)",
    ])


def localize_content_title(title: str) -> str:
    if active_lang() != "ru":
        return title

    labels = {
        "BRIEFING": t("content.briefing"),
        "SEARCH": "ПОИСК",
        "NEWS": "НОВОСТИ",
        "RESEARCH": "ИССЛЕДОВАНИЕ",
        "PRICE": "ЦЕНА",
        "COMPARE": "СРАВНЕНИЕ",
    }
    raw = title.strip()
    prefix, sep, rest = raw.partition("—")
    key = prefix.strip().upper()
    if key in labels:
        return f"{labels[key]} {sep} {rest.strip()}".strip() if sep else labels[key]
    return title


def localize_log_message(text: str) -> str:
    if active_lang() != "ru":
        return text

    raw = str(text)
    exact = {
        "SYS: Interrupted — listening...": "СИСТЕМА: Прервано — слушаю...",
        "SYS: Shutdown requested.": "СИСТЕМА: Запрошено завершение.",
        "SYS: Briefing phase 1 (greeting) sent.": "СИСТЕМА: Сводка, этап 1 (приветствие) отправлен.",
        "SYS: Briefing phase 2 (news) sent.": "СИСТЕМА: Сводка, этап 2 (новости) отправлен.",
        "SYS: Proactive check-in.": "СИСТЕМА: Проактивная проверка.",
        "SYS: Phone connected via Remote Dashboard.": "СИСТЕМА: Телефон подключен через удаленную панель.",
        "SYS: JARVIS online.": "СИСТЕМА: JARVIS онлайн.",
        "ERR: API key invalid — please re-enter your key.": "ОШИБКА: Ключ API недействителен — введите ключ заново.",
        "Could not open Instagram in browser.": "Не удалось открыть Instagram в браузере.",
        "Could not open Messenger in browser.": "Не удалось открыть Messenger в браузере.",
    }
    if raw in exact:
        return exact[raw]

    if raw.startswith("You: "):
        return f"{t('log.you')}: {raw[5:]}"
    if raw.startswith("Jarvis: "):
        return f"JARVIS: {raw[8:]}"
    if raw.startswith("JARVIS: "):
        return raw
    if raw.startswith("FILE: ") and raw.endswith(" loaded"):
        body = raw[6:-7]
        return f"{t('log.file')}: {body} загружен"
    if raw.startswith("[Web]: "):
        return f"[{t('log.web')}]: {raw[7:]}"
    if raw.startswith("SYS: Dashboard unavailable. Run: "):
        cmd = raw.removeprefix("SYS: Dashboard unavailable. Run: ")
        return f"СИСТЕМА: Панель недоступна. Выполните: {cmd}"
    if raw.startswith("SYS: Briefing news phase failed: "):
        return "СИСТЕМА: Этап новостной сводки не выполнен: " + raw.rsplit(": ", 1)[1]
    if raw.startswith("NET: Connection lost - reconnecting in "):
        secs = raw.removeprefix("NET: Connection lost - reconnecting in ")
        return f"СЕТЬ: Соединение потеряно — переподключение через {secs}"
    if raw.startswith("ERR: Runtime recovered - reconnecting in "):
        secs = raw.removeprefix("ERR: Runtime recovered - reconnecting in ")
        return f"ОШИБКА: Runtime восстановлен — переподключение через {secs}"
    if raw.startswith("ERR: "):
        return f"{t('log.error')}: {raw[5:]}"
    if raw.startswith("SYS: "):
        return f"{t('log.system')}: {raw[5:]}"

    replacements = [
        ("[Search:", "[Поиск:"),
        ("[Settings]", "[Настройки]"),
        ("[Desktop]", "[Рабочий стол]"),
        ("[desktop]", "[рабочий стол]"),
        ("[Code] Build started...", "[Код] Сборка запущена..."),
        ("[Code] Writing code...", "[Код] Написание кода..."),
        ("[Code] Editing file...", "[Код] Редактирование файла..."),
        ("[Code] Analyzing code...", "[Код] Анализ кода..."),
        ("[Code] Optimizing code...", "[Код] Оптимизация кода..."),
        ("[Code] Taking screenshot for analysis...", "[Код] Снимок экрана для анализа..."),
        ("[Code] Attempt ", "[Код] Попытка "),
        ("[Code] Fixing ", "[Код] Исправление "),
        ("[Code] Running ", "[Код] Запуск "),
        ("[DevAgent]", "[Разработка]"),
        ("[FileProcessor]", "[Файлы]"),
        ("[browser]", "[браузер]"),
        ("[open_app]", "[приложение]"),
        ("[file]", "[файлы]"),
        ("[msg]", "[сообщение]"),
        ("[Computer]", "[Компьютер]"),
        ("[Reminder]", "[Напоминание]"),
        ("[FlightFinder]", "[Авиабилеты]"),
        ("[GameUpdater]", "[Игры]"),
        ("[YouTube] Searching:", "[YouTube] Поиск:"),
        ("[YouTube] Summarizing:", "[YouTube] Сводка:"),
        ("[YouTube] Getting info:", "[YouTube] Информация:"),
        ("[YouTube] Trending:", "[YouTube] Тренды:"),
        ("[YouTube] Action:", "[YouTube] Действие:"),
    ]
    for old, new in replacements:
        if raw.startswith(old):
            return raw.replace(old, new, 1)

    return raw
