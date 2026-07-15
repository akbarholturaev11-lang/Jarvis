"""Minimal dictionary-based localization for visible JARVIS UI text."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from core.app_paths import resolve_app_paths


SUPPORTED_UI_LANGUAGES = {"en", "ru"}
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = (
    resolve_app_paths().config_dir
    if getattr(sys, "frozen", False)
    else BASE_DIR / "config"
)
SETTINGS_FILE = CONFIG_DIR / "settings.json"


def _normalise_lang(value: str | None) -> str:
    code = (value or "").strip().lower().replace("_", "-")
    code = code.split("-", 1)[0].split(".", 1)[0]
    if code in SUPPORTED_UI_LANGUAGES:
        return code
    return ""


def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_ui_language() -> str:
    settings_lang = _normalise_lang(str(_load_settings().get("ui_language", "")))
    if settings_lang in SUPPORTED_UI_LANGUAGES:
        return settings_lang

    env_lang = _normalise_lang(os.environ.get("JARVIS_UI_LANG") or os.environ.get("JARVIS_LANG"))
    if env_lang in SUPPORTED_UI_LANGUAGES:
        return env_lang

    return "ru"


LANG = load_ui_language()


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
        "camera.opencv_missing": "OpenCV (cv2) is not installed. Run: python -m pip install opencv-python-headless",
        "button.close": "CLOSE",
        "setup.required": "INITIALISATION REQUIRED",
        "setup.configure": "Configure J.A.R.V.I.S. before first boot.",
        "setup.api_key": "GEMINI API KEY",
        "setup.secure_note": "Stored in your operating system secure credential store.",
        "setup.permissions_note": "Microphone access is required for voice. Camera, screen, accessibility, and notifications are requested only when you use those features.",
        "setup.secure_store_failed": "Secure credential storage is unavailable. The key was not saved.",
        "setup.validating": "Validating Gemini key…",
        "setup.key_invalid": "Gemini rejected this key. Check it and try again.",
        "setup.validation_unavailable": "The key could not be validated now. Check your connection and try again.",
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
        "keepawake.on": "Keep-awake on — phone connected.",
        "keepawake.off": "Keep-awake off — phone disconnected.",
        "keepawake.unsupported": "Keep-awake not available on this system",
        "tunnel.starting": "Remote tunnel starting…",
        "tunnel.active": "Remote access active — reachable from anywhere.",
        "tunnel.stopped": "Remote tunnel stopped — local network only.",
        "tunnel.not_installed": "cloudflared not installed — remote access unavailable.",
        "tunnel.failed": "Remote tunnel failed to start.",
        "tunnel.lan_only": "Local network only",
        "settings.title": "SETTINGS",
        "settings.remote_access": "Remote access (anywhere)",
        "settings.keep_awake": "Keep computer awake for remote",
        "settings.language": "Interface language",
        "settings.devices": "Paired devices",
        "settings.revoke": "REVOKE ALL",
        "settings.status": "Connection",
        "settings.qr_pair": "SHOW QR / PIN",
        "settings.on": "ON",
        "settings.off": "OFF",
        "settings.phones_connected": "{n} phone(s) connected",
        "settings.no_devices": "No paired devices",
        "settings.restart_note": "Restart to fully apply the language.",
        "settings.automation": "Command automation",
        "settings.add_automation": "+ NEW COMMAND",
        "settings.macro_name": "Command name",
        "settings.pick_actions": "Pick actions JARVIS will run:",
        "settings.save": "SAVE",
        "settings.cancel": "CANCEL",
        "settings.no_macros": "No commands yet — add one below.",
        "settings.gear": "Settings",
        "settings.product": "Product and updates",
        "settings.product_version": "Version {version} · build {build}",
        "settings.product_not_configured": "Product service is not configured.",
        "settings.product_not_activated": "This exact version is not activated.",
        "settings.product_entitled": "This exact version is activated for offline use.",
        "settings.product_unavailable": "Secure product services are unavailable.",
        "settings.product_invalid": "Product entitlement state is invalid.",
        "settings.activate_version": "ACTIVATE THIS VERSION",
        "settings.device_id": "Device ID: {device_id}",
        "settings.copy_device_id": "COPY DEVICE ID",
        "settings.device_id_copied": "Device ID copied.",
        "settings.activation_title": "Activate JARVIS",
        "settings.activation_prompt": "Enter the activation key for this exact version:",
        "settings.check_updates": "CHECK FOR UPDATES",
        "settings.download_update": "DOWNLOAD VERIFIED UPDATE",
        "settings.install_update": "INSTALL VERIFIED UPDATE",
        "settings.submit_payment": "SUBMIT PAYMENT SCREENSHOT",
        "settings.check_payment": "CHECK PAYMENT STATUS",
        "settings.payment_file_title": "Select payment screenshot",
        "settings.payment_file_filter": "Images (*.png *.jpg *.jpeg *.webp)",
        "settings.payment_submitted": "Payment screenshot submitted for manual review.",
        "settings.payment_pending": "Payment is pending manual review.",
        "settings.payment_review": "Payment is under manual review.",
        "settings.payment_rejected": "Payment was rejected; no entitlement was granted.",
        "settings.payment_rejected_reason": "Payment was rejected: {reason}",
        "settings.payment_invalid": "Payment screenshot could not be submitted.",
        "settings.release_offer": "Available version {version} · {amount} {currency} minor units\nPlatforms: {platforms}",
        "settings.release_features": "What is new: {text}",
        "settings.release_fixes": "Fixes: {text}",
        "settings.release_not_provided": "Not provided",
        "settings.payment_destination": "Payment: {method}\nRecipient: {recipient}",
        "settings.payment_steps": "Instructions: {text}",
        "settings.payment_not_configured": "Payment destination is not configured by the operator. Screenshot submission is disabled.",
        "settings.product_working": "Working…",
        "settings.activation_success": "This exact version was activated.",
        "settings.activation_failed": "Activation could not be completed.",
        "settings.update_current": "This build is current.",
        "settings.update_purchase_required": "A newer version is available and requires a separate purchase.",
        "settings.update_entitled": "A purchased update is available for verified download.",
        "settings.update_downloaded": "Update downloaded and cryptographically verified. Select Install verified update to continue.",
        "settings.update_download_failed": "The verified update could not be downloaded.",
        "settings.update_failed": "The update check could not be completed.",
        "settings.update_installed": "The update was installed and health-checked. Restart JARVIS to use the new version.",
        "settings.update_preserved": "Installation did not complete; the verified previous version remains intact.",
        "settings.update_rolled_back": "The update failed its health check. The verified previous version was restored.",
        "settings.update_rollback_required": "Rollback could not be verified. JARVIS must not continue until recovery succeeds.",
        "settings.update_install_not_available": "Installation is unavailable because exact-version authority or trusted updater support could not be verified.",
        "settings.update_install_invalid": "The staged update or its exact-version entitlement is no longer valid. Download it again.",
        "settings.update_install_failed": "The update could not be installed safely; no success is claimed.",
        "product.update_recovery_preserved": "Interrupted update recovered: the previous version was verified and preserved.",
        "product.update_recovery_rolled_back": "Interrupted update recovered: the previous version was restored and verified.",
        "product.update_recovery_blocked": "An interrupted update could not be recovered safely. License onboarding and JARVIS runtime are blocked.",
        "product.activation_required": "Activate this exact version in Settings before JARVIS starts.",
        "product.gate.title": "PRODUCT LICENSE REQUIRED",
        "product.gate.subtitle": "Verify the paid entitlement for this exact version before JARVIS starts.",
        "product.gate.version": "Version {version} · build {build}",
        "product.gate.activation_required": "This exact version is not activated. Enter an activation key or open purchase options.",
        "product.gate.device_mismatch": "This license may be bound to another device. Ask the administrator to approve a device replacement, then retry.",
        "product.gate.not_configured": "Product service configuration is missing. JARVIS is blocked in fail-closed mode.",
        "product.gate.not_available": "Secure license or device storage is not available on this system.",
        "product.gate.invalid": "The local entitlement or packaged product identity is invalid.",
        "product.gate.offline": "Activation needs a network connection. A previously verified exact-version entitlement still works offline.",
        "product.gate.rejected": "The activation key was rejected or is no longer valid.",
        "product.gate.server_unavailable": "The activation service is temporarily unavailable. No entitlement was granted.",
        "product.gate.failed": "The license status could not be verified. JARVIS remains locked.",
        "product.gate.device": "Device ID: {device_id}",
        "product.gate.unavailable": "not available",
        "product.gate.activation_placeholder": "Activation key for this exact version",
        "product.gate.activate": "ACTIVATE",
        "product.gate.purchase": "PURCHASE THIS VERSION",
        "product.gate.refresh": "REFRESH STATUS",
        "product.gate.working": "Verifying license authority…",
        "product.gate.key_required": "Enter an activation key before continuing.",
        "product.gate.purchase_not_available": "Initial purchase is not available in this build yet. No payment or entitlement was created.",
        "product.gate.offer": "Version {version} · {amount} {currency} · {platforms}",
        "product.gate.features": "Features: {text}",
        "product.gate.fixes": "Fixes: {text}",
        "product.gate.payment_destination": "Payment: {method} · {recipient}",
        "product.gate.payment_steps": "Instructions: {text}",
        "product.gate.offer_ready": "Review the server-controlled price and instructions, then upload a receipt.",
        "product.gate.release_not_provided": "not provided",
        "product.gate.upload_payment": "UPLOAD RECEIPT",
        "product.gate.check_payment": "CHECK PAYMENT",
        "product.gate.resubmit": "PREPARE RESUBMISSION",
        "product.gate.payment_submitted": "Receipt submitted privately. Waiting for administrator review.",
        "product.gate.payment_pending": "Payment is pending administrator review.",
        "product.gate.payment_review": "An administrator is reviewing the payment.",
        "product.gate.payment_rejected": "Payment was rejected: {reason}",
        "product.gate.payment_approved": "Payment approved. The signed exact-version entitlement was verified.",
        "product.gate.payment_not_configured": "Payment instructions are not configured on the server. No upload was made.",
        "product.gate.payment_offline": "Payment status is offline. Retry when the network is available.",
        "product.gate.payment_server_unavailable": "Payment service is temporarily unavailable. No success is claimed.",
        "product.gate.payment_invalid": "The payment response or receipt is invalid. Nothing was activated.",
        "product.gate.evidence_invalid": "Choose a valid, non-animated PNG, JPEG, or WebP receipt.",
        "product.gate.evidence_too_large": "The receipt exceeds the 10 MiB limit.",
        "product.gate.evidence_not_available": "Secure receipt processing is not available on this system.",
        "product.gate.evidence_failed": "The receipt could not be read safely. Try another file.",
        "product.gate.polling_stopped": "Automatic checks stopped after bounded retries. Use Check payment to continue.",
        "product.gate.security_note": "An older purchased version remains usable offline; every new semantic version requires its own entitlement.",
        "product.gate.dev_override_log": "SYS: Development license bypass active (source build only).",
        "monitor.header": "SYS MONITOR",
        "monitor.uptime": "UP",
        "monitor.processes": "PROC",
        "monitor.os": "OS",
        "badge.ai_active": "AI CORE\nACTIVE",
        "badge.security": "SEC\nCLEARED",
        "badge.protocol": "PROTOCOL\nJARVIS",
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
        "content.personal_briefing": "PERSONAL BRIEFING",
        "footer.shortcuts": "[F4] Mute  ·  [F11] Fullscreen",
        "footer.brand": "AkbarCustom  ·  JARVIS  ·  CLASSIFIED",
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
        "ui_language.changed": "UI language changed to English. Restart the app to apply.",
        "ui_language.unsupported": "Unsupported UI language. Use English or Russian.",
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
        "camera.opencv_missing": "OpenCV (cv2) не установлен. Выполните: python -m pip install opencv-python-headless",
        "button.close": "ЗАКРЫТЬ",
        "setup.required": "ТРЕБУЕТСЯ НАСТРОЙКА",
        "setup.configure": "Настройте J.A.R.V.I.S. перед первым запуском.",
        "setup.api_key": "КЛЮЧ GEMINI API",
        "setup.secure_note": "Ключ хранится в защищённом хранилище учётных данных системы.",
        "setup.permissions_note": "Для голоса нужен доступ к микрофону. Доступ к камере, экрану, универсальному доступу и уведомлениям запрашивается только при использовании этих функций.",
        "setup.secure_store_failed": "Защищённое хранилище недоступно. Ключ не сохранён.",
        "setup.validating": "Проверка ключа Gemini…",
        "setup.key_invalid": "Gemini отклонил этот ключ. Проверьте его и повторите.",
        "setup.validation_unavailable": "Сейчас ключ проверить нельзя. Проверьте соединение и повторите.",
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
        "keepawake.on": "Режим бодрствования включён — телефон подключён.",
        "keepawake.off": "Режим бодрствования выключен — телефон отключён.",
        "keepawake.unsupported": "Режим бодрствования недоступен в этой системе",
        "tunnel.starting": "Удалённый туннель запускается…",
        "tunnel.active": "Удалённый доступ активен — доступно откуда угодно.",
        "tunnel.stopped": "Удалённый туннель остановлен — только локальная сеть.",
        "tunnel.not_installed": "cloudflared не установлен — удалённый доступ недоступен.",
        "tunnel.failed": "Не удалось запустить удалённый туннель.",
        "tunnel.lan_only": "Только локальная сеть",
        "settings.title": "НАСТРОЙКИ",
        "settings.remote_access": "Удалённый доступ (откуда угодно)",
        "settings.keep_awake": "Не давать компьютеру засыпать",
        "settings.language": "Язык интерфейса",
        "settings.devices": "Привязанные устройства",
        "settings.revoke": "ОТВЯЗАТЬ ВСЕ",
        "settings.status": "Соединение",
        "settings.qr_pair": "ПОКАЗАТЬ QR / PIN",
        "settings.on": "ВКЛ",
        "settings.off": "ВЫКЛ",
        "settings.phones_connected": "Подключено телефонов: {n}",
        "settings.no_devices": "Нет привязанных устройств",
        "settings.restart_note": "Перезапустите, чтобы полностью применить язык.",
        "settings.automation": "Автоматизация команд",
        "settings.add_automation": "+ НОВАЯ КОМАНДА",
        "settings.macro_name": "Название команды",
        "settings.pick_actions": "Выберите действия JARVIS:",
        "settings.save": "СОХРАНИТЬ",
        "settings.cancel": "ОТМЕНА",
        "settings.no_macros": "Пока нет команд — добавьте ниже.",
        "settings.gear": "Настройки",
        "settings.product": "Продукт и обновления",
        "settings.product_version": "Версия {version} · сборка {build}",
        "settings.product_not_configured": "Сервис продукта не настроен.",
        "settings.product_not_activated": "Эта точная версия не активирована.",
        "settings.product_entitled": "Эта точная версия активирована для офлайн-работы.",
        "settings.product_unavailable": "Защищённые сервисы продукта недоступны.",
        "settings.product_invalid": "Состояние лицензии продукта недействительно.",
        "settings.activate_version": "АКТИВИРОВАТЬ ЭТУ ВЕРСИЮ",
        "settings.device_id": "ID устройства: {device_id}",
        "settings.copy_device_id": "КОПИРОВАТЬ ID УСТРОЙСТВА",
        "settings.device_id_copied": "ID устройства скопирован.",
        "settings.activation_title": "Активация JARVIS",
        "settings.activation_prompt": "Введите ключ активации для этой точной версии:",
        "settings.check_updates": "ПРОВЕРИТЬ ОБНОВЛЕНИЯ",
        "settings.download_update": "СКАЧАТЬ ПРОВЕРЕННОЕ ОБНОВЛЕНИЕ",
        "settings.install_update": "УСТАНОВИТЬ ПРОВЕРЕННОЕ ОБНОВЛЕНИЕ",
        "settings.submit_payment": "ОТПРАВИТЬ СКРИНШОТ ОПЛАТЫ",
        "settings.check_payment": "ПРОВЕРИТЬ СТАТУС ОПЛАТЫ",
        "settings.payment_file_title": "Выберите скриншот оплаты",
        "settings.payment_file_filter": "Изображения (*.png *.jpg *.jpeg *.webp)",
        "settings.payment_submitted": "Скриншот оплаты отправлен на ручную проверку.",
        "settings.payment_pending": "Оплата ожидает ручной проверки.",
        "settings.payment_review": "Оплата находится на ручной проверке.",
        "settings.payment_rejected": "Оплата отклонена; право на версию не предоставлено.",
        "settings.payment_rejected_reason": "Оплата отклонена: {reason}",
        "settings.payment_invalid": "Не удалось отправить скриншот оплаты.",
        "settings.release_offer": "Доступна версия {version} · {amount} {currency} в минимальных единицах\nПлатформы: {platforms}",
        "settings.release_features": "Что нового: {text}",
        "settings.release_fixes": "Исправления: {text}",
        "settings.release_not_provided": "Не указано",
        "settings.payment_destination": "Оплата: {method}\nПолучатель: {recipient}",
        "settings.payment_steps": "Инструкция: {text}",
        "settings.payment_not_configured": "Оператор не настроил реквизиты оплаты. Отправка скриншота отключена.",
        "settings.product_working": "Выполняется…",
        "settings.activation_success": "Эта точная версия активирована.",
        "settings.activation_failed": "Не удалось завершить активацию.",
        "settings.update_current": "Установлена актуальная сборка.",
        "settings.update_purchase_required": "Доступна новая версия; её нужно приобрести отдельно.",
        "settings.update_entitled": "Приобретённое обновление доступно для проверенной загрузки.",
        "settings.update_downloaded": "Обновление скачано и криптографически проверено. Нажмите «Установить проверенное обновление».",
        "settings.update_download_failed": "Не удалось скачать проверенное обновление.",
        "settings.update_failed": "Не удалось проверить обновления.",
        "settings.update_installed": "Обновление установлено и прошло проверку работоспособности. Перезапустите JARVIS для новой версии.",
        "settings.update_preserved": "Установка не завершилась; проверенная предыдущая версия осталась без изменений.",
        "settings.update_rolled_back": "Обновление не прошло проверку работоспособности. Проверенная предыдущая версия восстановлена.",
        "settings.update_rollback_required": "Откат не удалось проверить. JARVIS не должен продолжать работу до успешного восстановления.",
        "settings.update_install_not_available": "Установка недоступна: не удалось подтвердить право на точную версию или поддержку доверенного обновления.",
        "settings.update_install_invalid": "Подготовленное обновление или право на точную версию больше недействительно. Скачайте обновление заново.",
        "settings.update_install_failed": "Не удалось безопасно установить обновление; успех не заявляется.",
        "product.update_recovery_preserved": "Прерванное обновление восстановлено: предыдущая версия проверена и сохранена.",
        "product.update_recovery_rolled_back": "Прерванное обновление восстановлено: предыдущая версия восстановлена и проверена.",
        "product.update_recovery_blocked": "Не удалось безопасно восстановиться после прерванного обновления. Активация лицензии и запуск JARVIS заблокированы.",
        "product.activation_required": "Активируйте эту точную версию в настройках перед запуском JARVIS.",
        "product.gate.title": "ТРЕБУЕТСЯ ЛИЦЕНЗИЯ ПРОДУКТА",
        "product.gate.subtitle": "Перед запуском JARVIS подтвердите оплаченное право на эту точную версию.",
        "product.gate.version": "Версия {version} · сборка {build}",
        "product.gate.activation_required": "Эта точная версия не активирована. Введите ключ активации или откройте варианты покупки.",
        "product.gate.device_mismatch": "Возможно, лицензия привязана к другому устройству. Попросите администратора подтвердить замену устройства и повторите.",
        "product.gate.not_configured": "Конфигурация сервиса продукта отсутствует. JARVIS заблокирован в безопасном режиме.",
        "product.gate.not_available": "Защищённое хранилище лицензии или устройства недоступно в этой системе.",
        "product.gate.invalid": "Локальное право или идентификатор сборки продукта недействительны.",
        "product.gate.offline": "Для активации требуется сеть. Ранее проверенное право на точную версию продолжает работать офлайн.",
        "product.gate.rejected": "Ключ активации отклонён или больше недействителен.",
        "product.gate.server_unavailable": "Сервис активации временно недоступен. Право не предоставлено.",
        "product.gate.failed": "Не удалось проверить лицензию. JARVIS остаётся заблокированным.",
        "product.gate.device": "ID устройства: {device_id}",
        "product.gate.unavailable": "недоступно",
        "product.gate.activation_placeholder": "Ключ активации для этой точной версии",
        "product.gate.activate": "АКТИВИРОВАТЬ",
        "product.gate.purchase": "КУПИТЬ ЭТУ ВЕРСИЮ",
        "product.gate.refresh": "ОБНОВИТЬ СТАТУС",
        "product.gate.working": "Проверка лицензии…",
        "product.gate.key_required": "Введите ключ активации, чтобы продолжить.",
        "product.gate.purchase_not_available": "Первичная покупка пока недоступна в этой сборке. Оплата и право не создавались.",
        "product.gate.offer": "Версия {version} · {amount} {currency} · {platforms}",
        "product.gate.features": "Функции: {text}",
        "product.gate.fixes": "Исправления: {text}",
        "product.gate.payment_destination": "Оплата: {method} · {recipient}",
        "product.gate.payment_steps": "Инструкции: {text}",
        "product.gate.offer_ready": "Проверьте заданные сервером цену и инструкции, затем загрузите чек.",
        "product.gate.release_not_provided": "не указано",
        "product.gate.upload_payment": "ЗАГРУЗИТЬ ЧЕК",
        "product.gate.check_payment": "ПРОВЕРИТЬ ОПЛАТУ",
        "product.gate.resubmit": "ПОДГОТОВИТЬ ПОВТОРНУЮ ОТПРАВКУ",
        "product.gate.payment_submitted": "Чек отправлен в закрытое хранилище. Ожидается проверка администратора.",
        "product.gate.payment_pending": "Платёж ожидает проверки администратора.",
        "product.gate.payment_review": "Администратор проверяет платёж.",
        "product.gate.payment_rejected": "Платёж отклонён: {reason}",
        "product.gate.payment_approved": "Платёж подтверждён. Подписанное право на точную версию проверено.",
        "product.gate.payment_not_configured": "Платёжные инструкции не настроены на сервере. Загрузка не выполнялась.",
        "product.gate.payment_offline": "Статус оплаты недоступен без сети. Повторите после подключения.",
        "product.gate.payment_server_unavailable": "Сервис оплаты временно недоступен. Успех не заявляется.",
        "product.gate.payment_invalid": "Ответ оплаты или чек недействителен. Активация не выполнена.",
        "product.gate.evidence_invalid": "Выберите действительный неанимированный чек PNG, JPEG или WebP.",
        "product.gate.evidence_too_large": "Размер чека превышает 10 МиБ.",
        "product.gate.evidence_not_available": "Безопасная обработка чека недоступна в этой системе.",
        "product.gate.evidence_failed": "Не удалось безопасно прочитать чек. Выберите другой файл.",
        "product.gate.polling_stopped": "Автопроверка остановлена после ограниченного числа попыток. Нажмите «Проверить оплату».",
        "product.gate.security_note": "Ранее купленная версия продолжает работать офлайн; для каждой новой семантической версии требуется отдельное право.",
        "product.gate.dev_override_log": "SYS: Обход лицензии для разработки активен только в исходной сборке.",
        "monitor.header": "МОНИТОР СИСТЕМЫ",
        "monitor.uptime": "РАБ",
        "monitor.processes": "ПРОЦ",
        "monitor.os": "ОС",
        "badge.ai_active": "ИИ\nАКТИВЕН",
        "badge.security": "ДОСТУП\nРАЗРЕШЕН",
        "badge.protocol": "ПРОТОКОЛ\nJARVIS",
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
        "content.personal_briefing": "ЛИЧНАЯ СВОДКА",
        "footer.shortcuts": "[F4] Микрофон  ·  [F11] Полный экран",
        "footer.brand": "AkbarCustom  ·  JARVIS  ·  СЕКРЕТНО",
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
        "ui_language.changed": "Язык интерфейса изменён на русский. Перезапустите приложение.",
        "ui_language.unsupported": "Неподдерживаемый язык интерфейса. Используйте английский или русский.",
    },
}


def active_lang() -> str:
    return LANG if LANG in _MESSAGES else "ru"


def set_ui_language(language: str) -> str:
    lang = _normalise_lang(language)
    if lang not in SUPPORTED_UI_LANGUAGES:
        raise ValueError(t("ui_language.unsupported"))

    settings = _load_settings()
    settings["ui_language"] = lang
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    global LANG
    LANG = lang
    return lang


def ui_language_change_message(language: str) -> str:
    lang = _normalise_lang(language)
    if lang not in SUPPORTED_UI_LANGUAGES:
        raise ValueError(t("ui_language.unsupported"))
    return _MESSAGES[lang]["ui_language.changed"]


def change_ui_language(language: str) -> str:
    lang = set_ui_language(language)
    return ui_language_change_message(lang)


def detect_ui_language_command(text: str) -> str | None:
    raw = re.sub(r"\s+", " ", str(text).casefold()).strip()
    if not raw:
        return None

    if re.search(r"\b(inglis|ingliz)(cha)?\s+qil\b", raw):
        return "en"
    if re.search(r"\brus(cha)?\s+qil\b", raw):
        return "ru"

    wants_en = any(term in raw for term in (
        "english", "inglis", "ingliz", "английск", "английский"
    ))
    wants_ru = any(term in raw for term in (
        "russian", "русск", "русский", "rus", "ruscha"
    ))
    has_switch = any(term in raw for term in (
        "switch", "change", "set", "enable", "make",
        "переключ", "включ", "измени", "поменяй", "сделай",
        "qil", "almashtir", "o'zgartir", "ozgartir",
    ))
    has_ui = any(term in raw for term in (
        "ui", "interface", "interfeys", "интерфейс", "язык интерфейса",
    ))

    if has_switch and has_ui:
        if wants_en:
            return "en"
        if wants_ru:
            return "ru"
    return None


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
        "PERSONAL BRIEFING": t("content.personal_briefing"),
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
        "SYS: Personal briefing phase sent.": "СИСТЕМА: Личная сводка отправлена.",
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
    if raw.startswith("SYS: Personal briefing phase failed: "):
        return "СИСТЕМА: Не удалось подготовить личную сводку: " + raw.rsplit(": ", 1)[1]
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
