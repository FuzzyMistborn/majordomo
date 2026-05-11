import asyncio

from tavily import AsyncTavilyClient
from config import Config


async def search(query: str, limit: int = 5) -> dict:
    """
    Search the web via Tavily and return a dict with:
      - results: list of {title, url, snippet}
      - query: the original query
    """
    query = query.strip()
    if not query:
        raise ValueError("Search query cannot be empty.")
    if len(query) > Config.MAX_SEARCH_QUERY_CHARS:
        raise ValueError(f"Search query is too long (max {Config.MAX_SEARCH_QUERY_CHARS} characters).")
    client = AsyncTavilyClient(api_key=Config.TAVILY_API_KEY)
    response = await asyncio.wait_for(
        client.search(query, max_results=limit),
        timeout=Config.INTEGRATION_TIMEOUT_SECONDS,
    )

    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in response.get("results", [])
    ]

    return {"query": query, "results": results}
