"""
Home Assistant integration via REST API with long-lived access token.
"""

import httpx
from config import Config

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {Config.HA_TOKEN}",
        "Content-Type": "application/json",
    }

def _base() -> str:
    return Config.HA_URL.rstrip("/")

def _enabled() -> bool:
    return bool(Config.HA_URL and Config.HA_TOKEN)

async def get_states(domains: list[str] | None = None) -> list[dict]:
    """Fetch entity states, filtered to allowed domains, excluding unavailable."""
    if not _enabled():
        return []
    allowed = set(Config.HA_ALLOWED_DOMAINS)
    if domains:
        allowed = allowed & set(domains)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{_base()}/api/states", headers=_headers())
        resp.raise_for_status()
    results = []
    for entity in resp.json():
        entity_id: str = entity.get("entity_id", "")
        domain = entity_id.split(".")[0]
        if domain not in allowed:
            continue
        state = entity.get("state")
        if state in ("unavailable", "unknown"):
            continue
        results.append({
            "entity_id": entity_id,
            "domain": domain,
            "state": state,
            "friendly_name": entity.get("attributes", {}).get("friendly_name", entity_id),
        })
    return results

async def call_service(domain: str, service: str, entity_id: str, extra: dict | None = None) -> dict:
    """Call any HA service."""
    if not _enabled():
        raise RuntimeError("Home Assistant is not configured.")
    if domain not in Config.HA_ALLOWED_DOMAINS:
        raise PermissionError(f"Domain '{domain}' is not in the allowed domains list.")
    payload = {"entity_id": entity_id}
    if extra:
        payload.update(extra)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_base()}/api/services/{domain}/{service}",
            headers=_headers(),
            json=payload,
        )
        resp.raise_for_status()
    return {"success": True, "entity_id": entity_id, "service": f"{domain}.{service}"}

async def get_entity_state(entity_id: str) -> dict:
    """Get state of a single entity."""
    if not _enabled():
        raise RuntimeError("Home Assistant is not configured.")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{_base()}/api/states/{entity_id}", headers=_headers())
        resp.raise_for_status()
        data = resp.json()
    return {
        "entity_id": entity_id,
        "state": data.get("state"),
        "friendly_name": data.get("attributes", {}).get("friendly_name", entity_id),
        "attributes": data.get("attributes", {}),
    }

async def _ensure_datetime(dt: str, end_of_day: bool = False) -> str:
    """Ensure a date string has a time component for the HA calendar API."""
    if "T" not in dt:
        suffix = "T23:59:59" if end_of_day else "T00:00:00"
        return dt + suffix
    return dt


async def get_calendar_events(start: str, end: str) -> list[dict]:
    """
    Query all configured calendars for events in the given ISO 8601 date range.
    Returns merged list of events across all calendars.
    """
    if not _enabled():
        raise RuntimeError("Home Assistant is not configured.")
    if not Config.HA_CALENDARS:
        return []

    start = await _ensure_datetime(start, end_of_day=False)
    end = await _ensure_datetime(end, end_of_day=True)

    all_events = []
    async with httpx.AsyncClient(timeout=15) as client:
        for calendar_id in Config.HA_CALENDARS:
            try:
                resp = await client.get(
                    f"{_base()}/api/calendars/{calendar_id}",
                    headers=_headers(),
                    params={"start": start, "end": end},
                )
                resp.raise_for_status()
                for event in resp.json():
                    all_events.append({
                        "calendar": calendar_id,
                        "summary": event.get("summary", "(no title)"),
                        "start": event.get("start", {}).get("dateTime") or event.get("start", {}).get("date", ""),
                        "end": event.get("end", {}).get("dateTime") or event.get("end", {}).get("date", ""),
                        "description": event.get("description", ""),
                        "location": event.get("location", ""),
                        "all_day": "dateTime" not in event.get("start", {}),
                    })
            except Exception as e:
                all_events.append({"calendar": calendar_id, "error": str(e)})

    # Sort by start time
    all_events.sort(key=lambda e: e.get("start", ""))
    return all_events


# Stub to satisfy main.py import
async def fetch_ha_tools() -> list:
    return []

def get_ha_tools() -> list:
    return []
