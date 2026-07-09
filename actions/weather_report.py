import webbrowser
from urllib.parse import quote_plus

from core.i18n import t


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    city     = parameters.get("city")
    when     = parameters.get("time", "today")

    if not city or not isinstance(city, str) or not city.strip():
        msg = t("weather.city_missing")
        _log(msg, player)
        return msg

    city = city.strip()
    when = (when or "today").strip()
    display_when = t("date.today") if when.lower() == "today" else when

    search_query  = f"weather in {city} {when}"
    url           = f"https://www.google.com/search?q={quote_plus(search_query)}"

    try:
        opened = webbrowser.open(url)
        if not opened:
            raise RuntimeError("webbrowser.open returned False")
    except Exception as e:
        msg = t("weather.browser_failed", error=e)
        _log(msg, player)
        return msg

    msg = t("weather.showing", city=city, when=display_when)
    _log(msg, player)

    if session_memory:
        try:
            session_memory.set_last_search(query=search_query, response=msg)
        except Exception:
            pass

    return msg


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass
