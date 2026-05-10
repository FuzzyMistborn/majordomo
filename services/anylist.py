import asyncio
import logging
from datetime import date, datetime

from config import Config

logger = logging.getLogger(__name__)

_client = None


def _enabled() -> bool:
    return bool(Config.ANYLIST_EMAIL and Config.ANYLIST_PASSWORD)


def _get_client():
    global _client
    if _client is not None:
        return _client
    from pyanylist import AnyListClient
    logger.info("Logging in to AnyList...")
    _client = AnyListClient.login(Config.ANYLIST_EMAIL, Config.ANYLIST_PASSWORD)
    return _client


def _get_fresh_client():
    global _client
    from pyanylist import AnyListClient
    logger.info("Re-logging in to AnyList (refreshing session)...")
    _client = AnyListClient.login(Config.ANYLIST_EMAIL, Config.ANYLIST_PASSWORD)
    return _client


def _with_retry(fn):
    try:
        return fn(_get_client())
    except RuntimeError as e:
        logger.warning(f"AnyList call failed ({e}), retrying with fresh login...")
        return fn(_get_fresh_client())


def _item_checked(item) -> bool:
    for attr in ("checked", "crossed_off", "is_checked", "complete", "completed"):
        val = getattr(item, attr, None)
        if val is not None:
            return bool(val)
    return False


def _item_to_dict(item, list_name: str | None = None) -> dict:
    d = {
        "name": str(item.name),
        "quantity": str(getattr(item, "quantity", "") or ""),
        "details": str(getattr(item, "details", "") or ""),
        "category": str(getattr(item, "category", "") or ""),
        "checked": _item_checked(item),
    }
    if list_name is not None:
        d["list"] = list_name
    return d


def _fetch_lists_sync() -> list[dict]:
    def fn(client):
        return [{"name": lst.name, "id": str(lst.id)} for lst in client.get_lists()]
    return _with_retry(fn)


def _best_list_match(query: str, all_lists):
    """Return the best-matching list for query, or None."""
    import difflib
    lower = query.lower().strip()
    names_lower = [l.name.lower() for l in all_lists]
    # 1. Exact match
    for l in all_lists:
        if l.name.lower() == lower:
            return l
    # 2. Substring match (query inside name or name inside query)
    for l in all_lists:
        n = l.name.lower()
        if lower in n or n in lower:
            return l
    # 3. Fuzzy similarity (handles grocery/groceries, walmart/Walmart, etc.)
    close = difflib.get_close_matches(lower, names_lower, n=1, cutoff=0.7)
    if close:
        return next(l for l in all_lists if l.name.lower() == close[0])
    return None


def _fetch_list_items_sync(list_name: str, include_checked: bool = False) -> list[dict]:
    def fn(client):
        all_lists = list(client.get_lists())
        match = _best_list_match(list_name, all_lists)
        if match is None:
            available = ", ".join(l.name for l in all_lists)
            raise RuntimeError(f"List '{list_name}' not found. Available lists: {available}")
        lst = client.get_list_by_name(match.name)
        items = [_item_to_dict(item) for item in (lst.items or [])]
        if not include_checked:
            items = [i for i in items if not i["checked"]]
        return items
    return _with_retry(fn)


def _get_ical_url_sync() -> str:
    if Config.ANYLIST_ICAL_URL:
        return Config.ANYLIST_ICAL_URL

    def fn(client):
        try:
            url = client.get_icalendar_url()
        except RuntimeError:
            url = None
        if not url:
            logger.info("iCalendar not enabled; calling enable_icalendar()")
            info = client.enable_icalendar()
            url = getattr(info, "url", None) or str(info)
        return url

    try:
        return fn(_get_client())
    except RuntimeError as e:
        logger.warning(f"AnyList iCal setup failed ({e}), retrying with fresh login...")
        return fn(_get_fresh_client())


def _fetch_meal_plan_sync(start_str: str, end_str: str) -> list[dict]:
    url = _get_ical_url_sync()
    if not url:
        raise ValueError("Could not retrieve AnyList meal plan calendar URL.")

    logger.info("Fetching AnyList iCal feed.")
    import httpx
    with httpx.Client(follow_redirects=True, timeout=15) as http:
        resp = http.get(url)
        if resp.status_code in (403, 404):
            raise ValueError(
                "AnyList meal plan calendar is not accessible (the iCal feed returned "
                f"{resp.status_code}). Make sure you have meals planned in the AnyList "
                "meal planner, then try again."
            )
        resp.raise_for_status()
        ical_data = resp.content

    import icalendar
    cal = icalendar.Calendar.from_ical(ical_data)
    start_dt = date.fromisoformat(start_str[:10])
    end_dt = date.fromisoformat(end_str[:10])
    logger.info(f"Parsing iCal feed, looking for events between {start_dt} and {end_dt}")

    meals = []
    for component in cal.walk("VEVENT"):
        dtstart = component.get("DTSTART")
        if not dtstart:
            continue
        event_date = dtstart.dt
        if isinstance(event_date, datetime):
            event_date = event_date.date()
        if start_dt <= event_date <= end_dt:
            notes = str(component.get("DESCRIPTION", "") or "").strip()
            meals.append({
                "date": event_date.isoformat(),
                "meal": str(component.get("SUMMARY", "(no title)")),
                "notes": notes,
            })

    meals.sort(key=lambda m: m["date"])
    return meals


async def get_lists() -> list[dict]:
    if not _enabled():
        raise RuntimeError("AnyList is not configured.")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_lists_sync)


async def get_list_items(list_name: str, include_checked: bool = False) -> list[dict]:
    if not _enabled():
        raise RuntimeError("AnyList is not configured.")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_list_items_sync, list_name, include_checked)


async def get_meal_plan(start: str, end: str) -> list[dict]:
    if not _enabled():
        raise RuntimeError("AnyList is not configured.")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_meal_plan_sync, start, end)
