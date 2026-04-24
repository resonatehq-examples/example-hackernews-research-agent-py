"""
Hacker News Research Agent using Resonate

Monitors Hacker News for content matching your keywords. Uses an LLM to rank
relevance and runs continuously — surviving restarts without losing progress.

Every step is a durable checkpoint. On crash, Resonate re-runs the generator
but returns cached results for completed steps, so accumulated state rebuilds
in the same order. The promise store IS the state store — no external DB.
"""

import json
import os
import urllib.parse
import urllib.request
from threading import Event
from typing import Optional, TypedDict

from dotenv import load_dotenv
from openai import OpenAI
from resonate import Context, Resonate

load_dotenv()

resonate = Resonate()


class AgentConfig(TypedDict):
    keywords: list[str]
    slack_webhook: Optional[str]
    scan_interval_secs: float
    relevance_threshold: int


# ============================================================================
# Hacker News API
# ============================================================================


def search_hackernews(_: Context, keyword: str, max_results: int = 30) -> list[dict]:
    params = urllib.parse.urlencode({
        "query": keyword,
        "hitsPerPage": max_results,
        "tags": "story",
    })
    url = f"https://hn.algolia.com/api/v1/search?{params}"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.loads(response.read())
        return data.get("hits", [])


# ============================================================================
# LLM Analysis
# ============================================================================


def analyze_story(ctx: Context, story: dict, keyword: str) -> dict:
    client: OpenAI = ctx.get_dependency("openai")

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


def notify_findings(ctx: Context, findings: list[dict], keyword: str) -> None:
    config: AgentConfig = ctx.get_dependency("config")
    slack_webhook = config.get("slack_webhook")

    if not findings:
        print(f"📭 No interesting findings for '{keyword}'")
        return

    count = len(findings)
    word = "story" if count == 1 else "stories"
    print(f"\n🎯 Found {count} interesting {word} about '{keyword}':\n")

    for f in findings:
        print(f"  📰 {f['title']}")
        print(f"     Relevance: {f['relevance_score']}/10")
        print(f"     {f['summary']}")
        print(f"     🔗 {f['hn_url']}")
        if f["url"]:
            print(f"     📎 {f['url']}")
        print()

    if slack_webhook:
        message = {
            "text": f"🔍 Found {count} interesting HN {word} about *{keyword}*",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*<{f['hn_url']}|{f['title']}>* ({f['relevance_score']}/10)\n"
                            f"{f['summary']}\n"
                            + (f"📎 <{f['url']}|Original Article>" if f["url"] else "")
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
                print("✅ Notified via Slack")


# ============================================================================
# Durable Workflows
# ============================================================================


@resonate.register
def scan_keyword(
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

    Each internal `yield ctx.run()` is a checkpoint: a crash mid-scan resumes
    from the last completed step.
    """
    config: AgentConfig = ctx.get_dependency("config")
    relevance_threshold = config["relevance_threshold"]

    stories = yield ctx.run(search_hackernews, keyword)
    seen = set(seen_ids or [])
    new_stories = [s for s in stories if s["objectID"] not in seen]

    print(f"🔍 Scanning HN for: '{keyword}'")
    print(f"📚 {len(stories)} stories found, {len(new_stories)} new")

    newly_analyzed = []
    for story in new_stories:
        analysis = yield ctx.run(analyze_story, story, keyword)
        newly_analyzed.append(analysis)

    interesting = [
        a for a in newly_analyzed
        if a["is_interesting"] and a["relevance_score"] >= relevance_threshold
    ]

    yield ctx.run(notify_findings, interesting, keyword)

    print(f"   Analyzed: {len(new_stories)}  Interesting: {len(interesting)}\n")

    return {
        "keyword": keyword,
        "stories_found": len(stories),
        "newly_analyzed": newly_analyzed,
    }


@resonate.register
def monitor_hackernews(ctx: Context):
    """
    Continuous monitoring loop.

    Owns the `seen_ids` set. On crash-recovery Resonate replays this generator
    and returns cached results for completed `scan_keyword` calls, so `seen_ids`
    rebuilds deterministically — the promise store IS the state store.

    `yield ctx.sleep(...)` between rounds is a durable timer: a restart during
    sleep resumes the sleep rather than triggering an immediate redundant scan.
    """
    config: AgentConfig = ctx.get_dependency("config")
    keywords = config["keywords"]
    scan_interval_secs = config["scan_interval_secs"]
    relevance_threshold = config["relevance_threshold"]

    print("🤖 Hacker News Monitor Started")
    print(f"📡 Keywords: {', '.join(keywords)}")
    print(f"⏰ Scan interval: {scan_interval_secs / 60:.0f} minutes")
    print(f"🎯 Relevance threshold: {relevance_threshold}/10\n")

    seen_ids: set[str] = set()

    while True:
        for keyword in keywords:
            try:
                result = yield ctx.run(scan_keyword, keyword, list(seen_ids))
                for a in result["newly_analyzed"]:
                    seen_ids.add(a["story_id"])
            except Exception as e:
                print(f"❌ Error scanning '{keyword}': {e}")

        yield ctx.sleep(scan_interval_secs)


# ============================================================================
# Entry Point
# ============================================================================


def main() -> None:
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise SystemExit("❌ OPENAI_API_KEY environment variable is required")

    config: AgentConfig = {
        "keywords": [k.strip() for k in os.getenv("HN_KEYWORDS", "AI").split(",")],
        "slack_webhook": os.getenv("SLACK_WEBHOOK"),
        "scan_interval_secs": float(os.getenv("SCAN_INTERVAL_SECS", "3600")),
        "relevance_threshold": int(os.getenv("RELEVANCE_THRESHOLD", "7")),
    }

    resonate.set_dependency("openai", OpenAI(api_key=openai_api_key))
    resonate.set_dependency("config", config)

    print("\n🤖 Hacker News Agent Worker Started")
    print(f"⚙️  Keywords: {', '.join(config['keywords'])}")
    print(f"⏰ Scan interval: {config['scan_interval_secs'] / 60:.0f} minutes\n")
    print("📝 Run a one-time scan for a keyword:")
    print(f"   resonate invoke scan-1 --func scan_keyword --arg \"{config['keywords'][0]}\"")
    print("\n📝 Start continuous monitoring of configured keywords:")
    print("   resonate invoke monitor-1 --func monitor_hackernews\n")

    resonate.start()

    try:
        Event().wait()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        resonate.stop()


if __name__ == "__main__":
    main()
