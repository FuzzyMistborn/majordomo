import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import caldav

from config import Config

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(Config.CALDAV_URL and Config.CALDAV_USERNAME and Config.CALDAV_PASSWORD)


def _parse_dt(dt_str: str, end_of_day: bool = False) -> datetime:
    if "T" in dt_str:
        dt = datetime.fromisoformat(dt_str)
    else:
        d = date.fromisoformat(dt_str)
        time = "23:59:59" if end_of_day else "00:00:00"
        dt = datetime.fromisoformat(f"{d.isoformat()}T{time}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(Config.TIMEZONE))
    return dt


def _fetch_events_sync(start_str: str, end_str: str) -> list[dict]:
    client = caldav.DAVClient(
        url=Config.CALDAV_URL,
        username=Config.CALDAV_USERNAME,
        password=Config.CALDAV_PASSWORD,
    )
    principal = client.principal()
    calendars = principal.calendars()

    if Config.CALDAV_CALENDARS:
        allowed = {n.lower() for n in Config.CALDAV_CALENDARS}
        calendars = [c for c in calendars if (c.name or "").lower() in allowed]

    start_dt = _parse_dt(start_str, end_of_day=False)
    end_dt = _parse_dt(end_str, end_of_day=True)

    all_events = []
    for cal in calendars:
        cal_name = cal.name or "unknown"
        try:
            events = cal.date_search(start=start_dt, end=end_dt, expand=True)
            for event in events:
                comp = event.icalendar_component
                summary = str(comp.get("SUMMARY", "(no title)"))
                dtstart = comp.get("DTSTART")
                location = str(comp.get("LOCATION", ""))
                description = str(comp.get("DESCRIPTION", ""))

                if dtstart:
                    start_val = dtstart.dt
                    all_day = isinstance(start_val, date) and not isinstance(start_val, datetime)
                    start_iso = start_val.isoformat() if all_day else start_val.isoformat()
                else:
                    start_iso = ""
                    all_day = False

                all_events.append({
                    "calendar": cal_name,
                    "summary": summary,
                    "start": start_iso,
                    "all_day": all_day,
                    "location": location,
                    "description": description,
                })
        except Exception as e:
            logger.error(f"CalDAV error on calendar '{cal_name}': {e}")
            all_events.append({"calendar": cal_name, "error": str(e)})

    all_events.sort(key=lambda e: e.get("start", ""))
    return all_events


def _create_event_sync(summary: str, start_str: str, end_str: str | None, description: str, location: str, calendar_name: str | None) -> str:
    from datetime import timedelta
    calendars = _get_client_calendars(calendar_name)
    if not calendars:
        raise ValueError("No matching calendar found.")
    target = calendars[0]

    start_dt = _parse_dt(start_str)
    end_dt = _parse_dt(end_str) if end_str else start_dt + timedelta(hours=1)

    target.save_event(
        dtstart=start_dt,
        dtend=end_dt,
        summary=summary,
        description=description,
        location=location,
    )
    return target.name or "calendar"


def _get_client_calendars(calendar_name: str | None = None):
    client = caldav.DAVClient(
        url=Config.CALDAV_URL,
        username=Config.CALDAV_USERNAME,
        password=Config.CALDAV_PASSWORD,
    )
    principal = client.principal()
    calendars = principal.calendars()
    if calendar_name:
        matched = [c for c in calendars if (c.name or "").lower() == calendar_name.lower()]
        if matched:
            return matched
        logger.warning(f"Calendar '{calendar_name}' not found; falling back to all configured calendars")
    if Config.CALDAV_CALENDARS:
        allowed = {n.lower() for n in Config.CALDAV_CALENDARS}
        return [c for c in calendars if (c.name or "").lower() in allowed]
    return calendars


def _find_event_sync(summary: str, start_str: str, calendar_name: str | None):
    """Find a single event by summary + date across configured calendars."""
    from datetime import timedelta
    search_dt = _parse_dt(start_str)
    search_start = search_dt.replace(hour=0, minute=0, second=0)
    search_end = search_start + timedelta(days=1)

    needle = summary.lower().strip()
    exact_match = None
    contains_match = None

    for cal in _get_client_calendars(calendar_name):
        for event in cal.date_search(start=search_start, end=search_end, expand=True):
            comp = event.icalendar_component
            haystack = str(comp.get("SUMMARY", "")).lower()
            if haystack == needle:
                exact_match = (event, cal)
                break
            if needle in haystack or haystack in needle:
                contains_match = (event, cal)
        if exact_match:
            break

    result = exact_match or contains_match
    if result:
        return result
    raise ValueError(f"No event named '{summary}' found on {start_str[:10]}.")


def _delete_event_sync(summary: str, start_str: str, calendar_name: str | None) -> str:
    event, cal = _find_event_sync(summary, start_str, calendar_name)
    event.delete()
    return cal.name or "calendar"


def _update_event_sync(summary: str, start_str: str, calendar_name: str | None,
                       new_summary: str | None, new_start: str | None, new_end: str | None,
                       new_description: str | None, new_location: str | None) -> str:
    import icalendar as ical
    from datetime import timedelta

    event, cal = _find_event_sync(summary, start_str, calendar_name)
    full_cal = ical.Calendar.from_ical(event.data)

    for component in full_cal.walk("VEVENT"):
        if new_summary:
            component["SUMMARY"] = ical.vText(new_summary)
        if new_description is not None:
            component["DESCRIPTION"] = ical.vText(new_description)
        if new_location is not None:
            component["LOCATION"] = ical.vText(new_location)
        if new_start:
            start_dt = _parse_dt(new_start)
            component["DTSTART"] = ical.vDatetime(start_dt)
            if new_end:
                component["DTEND"] = ical.vDatetime(_parse_dt(new_end))
            elif "DTEND" in component:
                old_duration = component["DTEND"].dt - component["DTSTART"].dt
                component["DTEND"] = ical.vDatetime(start_dt + old_duration)
        elif new_end:
            component["DTEND"] = ical.vDatetime(_parse_dt(new_end))

    event.data = full_cal.to_ical().decode("utf-8")
    event.save()
    return cal.name or "calendar"


async def get_calendar_events(start: str, end: str) -> list[dict]:
    if not _enabled():
        raise RuntimeError("CalDAV is not configured.")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_events_sync, start, end)


async def create_calendar_event(summary: str, start: str, end: str | None = None, description: str = "", location: str = "", calendar_name: str | None = None) -> str:
    if not _enabled():
        raise RuntimeError("CalDAV is not configured.")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _create_event_sync, summary, start, end, description, location, calendar_name)


async def delete_calendar_event(summary: str, start: str, calendar_name: str | None = None) -> str:
    if not _enabled():
        raise RuntimeError("CalDAV is not configured.")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _delete_event_sync, summary, start, calendar_name)


async def update_calendar_event(summary: str, start: str, calendar_name: str | None = None,
                                new_summary: str | None = None, new_start: str | None = None,
                                new_end: str | None = None, new_description: str | None = None,
                                new_location: str | None = None) -> str:
    if not _enabled():
        raise RuntimeError("CalDAV is not configured.")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _update_event_sync, summary, start, calendar_name,
        new_summary, new_start, new_end, new_description, new_location,
    )
