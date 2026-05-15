import logging

import httpx

from config import Config

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 4096


async def send_message(recipient: str, text: str) -> None:
    url = f"{Config.SIGNAL_API_URL.rstrip('/')}/v2/send"
    chunks = [text[i:i + _CHUNK_SIZE] for i in range(0, max(len(text), 1), _CHUNK_SIZE)]
    async with httpx.AsyncClient() as client:
        for chunk in chunks:
            try:
                resp = await client.post(
                    url,
                    json={
                        "recipients": [recipient],
                        "message": chunk,
                        "number": Config.SIGNAL_SENDER_NUMBER,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Failed to send Signal message to {recipient}: {e}")
                return


async def receive_messages() -> list[dict]:
    url = f"{Config.SIGNAL_API_URL.rstrip('/')}/v1/receive/{Config.SIGNAL_SENDER_NUMBER}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Failed to receive Signal messages: {e}")
    return []
