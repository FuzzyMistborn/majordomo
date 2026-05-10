"""
Home Assistant integration via REST API with long-lived access token.
"""

import re

import httpx
from config import Config

_HA_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_HA_ENTITY_ID_RE = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")

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
        requested = {d for d in domains if isinstance(d, str) and _HA_NAME_RE.fullmatch(d)}
        allowed = allowed & requested
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
    if not _HA_NAME_RE.fullmatch(domain) or not _HA_NAME_RE.fullmatch(service):
        raise ValueError("Invalid Home Assistant domain or service.")
    if not _HA_ENTITY_ID_RE.fullmatch(entity_id):
        raise ValueError("Invalid Home Assistant entity_id.")
    if domain not in Config.HA_ALLOWED_DOMAINS:
        raise PermissionError(f"Domain '{domain}' is not in the allowed domains list.")
    entity_domain = entity_id.split(".")[0]
    if entity_domain != domain:
        raise PermissionError("Service domain must match the target entity domain.")
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
    if not _HA_ENTITY_ID_RE.fullmatch(entity_id):
        raise ValueError("Invalid Home Assistant entity_id.")
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

async def get_weather() -> dict:
    """Fetch current weather and today's forecast from the configured weather entity."""
    if not _enabled():
        raise RuntimeError("Home Assistant is not configured.")
    if not Config.HA_WEATHER_ENTITY:
        raise RuntimeError("HA_WEATHER_ENTITY is not configured.")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_base()}/api/states/{Config.HA_WEATHER_ENTITY}",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
    attrs = data.get("attributes", {})
    result = {
        "condition": data.get("state", "unknown"),
        "temperature": attrs.get("temperature"),
        "temperature_unit": attrs.get("temperature_unit", ""),
        "humidity": attrs.get("humidity"),
        "wind_speed": attrs.get("wind_speed"),
        "wind_speed_unit": attrs.get("wind_speed_unit", ""),
        "friendly_name": attrs.get("friendly_name", Config.HA_WEATHER_ENTITY),
    }
    # Include today's forecast entries if available
    forecast = attrs.get("forecast", [])
    if forecast:
        result["forecast"] = forecast[:5]
    return result


# Stub to satisfy main.py import
async def fetch_ha_tools() -> list:
    return []

def get_ha_tools() -> list:
    return []
