"""
Hacker News Research Agent using Resonate

Monitors Hacker News for content matching your keywords. Uses an LLM to rank
relevance and runs continuously — surviving restarts without losing progress.

Every step is a durable checkpoint. On crash, Resonate resumes from the last
completed step; results already computed are returned from cache, so
accumulated state rebuilds in the same order. The promise store IS the state
store — no external DB.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Optional, TypedDict

from dotenv import load_dotenv
from openai import OpenAI
from resonate.resonate import Resonate

if TYPE_CHECKING:
    from resonate.context import Context

load_dotenv()


class AgentConfig(TypedDict):
    keywords: list[str]
    slack_webhook: Optional[str]
    scan_interval_secs: float
    relevance_threshold: int


# Thin wrapper so AgentConfig can be stored as a type-keyed dependency.
class AgentConfigDep:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config


# ============================================================================
# Hacker News API
# ============================================================================


async def search_hackernews(_: Context, keyword: str, max_results: int = 30) -> list[dict]:
    params = urllib.parse.urlencode({
        "query": keyword,
        "hitsPerPage": max_results,
        "tags": "story",
    })
    hn_url = f"https://hn.algolia.com/api/v1/search?{params}"
    with urllib.request.urlopen(hn_url, timeout=30) as response:
        data = json.loads(response.read())
        return data.get("hits", [])


# ============================================================================
# LLM Analysis
# ============================================================================


async def analyze_story(ctx: Context, story: dict, keyword: str) -> dict:
    client: OpenAI = ctx.get_dependency(OpenAI)

    prompt = f"""Analyze this Hacker News story for relevance to "{keyword}":

Title: {story.get("title", "")}
URL: {story.get("url") or "No URL"}
Points: {story.get("points", 0)}
Comments: {story.get("num_comments", 0)}

Rate the relevance (1-10) and decide if it is interesting enough to notify someone.
A story is interesting if it:
- Provides actionable insights or news
- Discusses significant developments or trends
- Contains technical depth or novel approaches
- Has strong community engagement (high points/comments)

Respond with JSON:
{{
  "relevanceScore": <1-10>,
  "summary": "<2-3 sentence summary>",
  "keyPoints": ["<point 1>", "<point 2>", ...],
  "isInteresting": <true/false>
}}"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "You are a research analyst. Analyze content and provide structured assessments in JSON format.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=500,
        response_format={"type": "json_object"},
    )

    content = response.choices[0].message.content or "{}"
    analysis = json.loads(content)

    return {
        "story_id": story["objectID"],
        "title": story.get("title", ""),
        "url": story.get("url", ""),
        "hn_url": f"https://news.ycombinator.com/item?id={story['objectID']}",
        "relevance_score": analysis.get("relevanceScore", 5),
        "summary": analysis.get("summary", ""),
        "key_points": analysis.get("keyPoints", []),
        "is_interesting": analysis.get("isInteresting", False),
    }


# ============================================================================
# Notifications
# ============================================================================


async def notify_findings(ctx: Context, findings: list[dict], keyword: str) -> None:
    config_dep: AgentConfigDep = ctx.get_dependency(AgentConfigDep)
    config = config_dep.config
    slack_webhook = config.get("slack_webhook")

    if not findings:
        print(f"No interesting findings for '{keyword}'", flush=True)
        return

    count = len(findings)
    word = "story" if count == 1 else "stories"
    print(f"\nFound {count} interesting {word} about '{keyword}':\n", flush=True)

    for f in findings:
        print(f"  {f['title']}")
        print(f"     Relevance: {f['relevance_score']}/10")
        print(f"     {f['summary']}")
        print(f"     {f['hn_url']}")
        if f["url"]:
            print(f"     {f['url']}")
        print()

    if slack_webhook:
        message = {
            "text": f"Found {count} interesting HN {word} about *{keyword}*",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*<{f['hn_url']}|{f['title']}>* ({f['relevance_score']}/10)\n"
                            f"{f['summary']}\n"
                            + (f"<{f['url']}|Original Article>" if f["url"] else "")
                        ),
                    },
                }
                for f in findings
            ],
        }
        data = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            slack_webhook,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            if resp.status == 200:
                print("Notified via Slack", flush=True)


# ============================================================================
# Durable Workflows
# ============================================================================


async def scan_keyword(
    ctx: Context,
    keyword: str,
    seen_ids: Optional[list[str]] = None,
):
    """
    One durable scan for a single keyword.

    Fetches stories, analyzes any not already in `seen_ids` with the LLM, and
    returns the full analysis set. `seen_ids` is a plain list so this function
    is CLI/RPC-invokable — callers without prior state pass `[]` (or omit the
    arg via the CLI).

    Each internal `await ctx.run()` is a checkpoint: a crash mid-scan resumes
    from the last completed step.
    """
    config_dep: AgentConfigDep = ctx.get_dependency(AgentConfigDep)
    config = config_dep.config
    relevance_threshold = config["relevance_threshold"]

    stories = await ctx.run(search_hackernews, keyword)
    seen = set(seen_ids or [])
    new_stories = [s for s in stories if s["objectID"] not in seen]

    print(f"Scanning HN for: '{keyword}'", flush=True)
    print(f"{len(stories)} stories found, {len(new_stories)} new", flush=True)

    newly_analyzed = []
    for story in new_stories:
        analysis = await ctx.run(analyze_story, story, keyword)
        newly_analyzed.append(analysis)

    interesting = [
        a for a in newly_analyzed
        if a["is_interesting"] and a["relevance_score"] >= relevance_threshold
    ]

    await ctx.run(notify_findings, interesting, keyword)

    print(f"   Analyzed: {len(new_stories)}  Interesting: {len(interesting)}\n", flush=True)

    return {
        "keyword": keyword,
        "stories_found": len(stories),
        "newly_analyzed": newly_analyzed,
    }


async def monitor_hackernews(ctx: Context):
    """
    Continuous monitoring loop.

    Owns the `seen_ids` set. On crash-recovery Resonate replays the async
    function and returns cached results for completed `scan_keyword` calls,
    so `seen_ids` rebuilds deterministically — the promise store IS the state
    store.

    `await ctx.sleep(...)` between rounds is a durable timer: a restart during
    sleep resumes the sleep rather than triggering an immediate redundant scan.
    """
    config_dep: AgentConfigDep = ctx.get_dependency(AgentConfigDep)
    config = config_dep.config
    keywords = config["keywords"]
    scan_interval_secs = config["scan_interval_secs"]
    relevance_threshold = config["relevance_threshold"]

    print("Hacker News Monitor Started", flush=True)
    print(f"Keywords: {', '.join(keywords)}", flush=True)
    print(f"Scan interval: {scan_interval_secs / 60:.0f} minutes", flush=True)
    print(f"Relevance threshold: {relevance_threshold}/10\n", flush=True)

    seen_ids: set[str] = set()

    while True:
        for keyword in keywords:
            try:
                result = await ctx.run(scan_keyword, keyword, list(seen_ids))
                for a in result["newly_analyzed"]:
                    seen_ids.add(a["story_id"])
            except Exception as e:
                print(f"Error scanning '{keyword}': {e}", flush=True)

        await ctx.sleep(scan_interval_secs)


# ============================================================================
# Entry Point
# ============================================================================


async def _async_main() -> None:
    config: AgentConfig = {
        "keywords": [k.strip() for k in os.getenv("HN_KEYWORDS", "AI").split(",")],
        "slack_webhook": os.getenv("SLACK_WEBHOOK"),
        "scan_interval_secs": float(os.getenv("SCAN_INTERVAL_SECS", "3600")),
        "relevance_threshold": int(os.getenv("RELEVANCE_THRESHOLD", "7")),
    }

    url = os.environ.get("RESONATE_URL", "http://localhost:8001")
    resonate = Resonate(url=url)

    resonate.with_dependency(OpenAI())
    resonate.with_dependency(AgentConfigDep(config))
    resonate.register(scan_keyword)
    resonate.register(monitor_hackernews)

    print("\nHacker News Agent Worker Started", flush=True)
    print(f"Keywords: {', '.join(config['keywords'])}", flush=True)
    print(f"Scan interval: {config['scan_interval_secs'] / 60:.0f} minutes\n", flush=True)
    print("Run a one-time scan for a keyword:", flush=True)
    print(f"   resonate invoke scan-1 --func scan_keyword --arg \"{config['keywords'][0]}\"", flush=True)
    print("\nStart continuous monitoring of configured keywords:", flush=True)
    print("   resonate invoke monitor-1 --func monitor_hackernews\n", flush=True)

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nShutting down...", flush=True)
    finally:
        await resonate.stop()


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
