from tavily import AsyncTavilyClient
from config import Config


async def search(query: str, limit: int = 5) -> dict:
    """
    Search the web via Tavily and return a dict with:
      - results: list of {title, url, snippet}
      - query: the original query
    """
    client = AsyncTavilyClient(api_key=Config.TAVILY_API_KEY)
    response = await client.search(query, max_results=limit)

    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in response.get("results", [])
    ]

    return {"query": query, "results": results}
