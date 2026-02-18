"""
Hacker News Research Agent using Resonate

Monitors Hacker News for content matching your keywords. Uses AI to evaluate
relevance, tracks what's been processed so scans are never duplicated, and
runs continuously across restarts without losing progress.

Every operation is a durable checkpoint — if the process crashes mid-scan,
it resumes exactly where it left off.
"""

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI
from resonate import Context, Resonate

load_dotenv()

# ============================================================================
# Resonate
# ============================================================================

resonate = Resonate()

# ============================================================================
# Database (SQLite — no external dependencies)
# ============================================================================


def init_database(_, db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_stories (
                story_id   TEXT PRIMARY KEY,
                title      TEXT NOT NULL,
                url        TEXT,
                processed_at TEXT DEFAULT (datetime('now')),
                relevance_score INTEGER,
                was_interesting INTEGER
            )
        """)
        conn.commit()
    finally:
        conn.close()


def has_processed_story(_, db_path: str, story_id: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM processed_stories WHERE story_id = ?", (story_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_story_processed(
    _,
    db_path: str,
    story_id: str,
    title: str,
    url: str,
    relevance_score: int,
    is_interesting: bool,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO processed_stories
               (story_id, title, url, relevance_score, was_interesting)
               VALUES (?, ?, ?, ?, ?)""",
            (story_id, title, url, relevance_score, int(is_interesting)),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================================
# Hacker News API
# ============================================================================


def search_hackernews(_, keyword: str, max_results: int = 30) -> list[dict]:
    params = urllib.parse.urlencode({
        "query": keyword,
        "hitsPerPage": max_results,
        "tags": "story",
    })
    url = f"https://hn.algolia.com/api/v1/search?{params}"

    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read())
        return data.get("hits", [])


# ============================================================================
# AI Analysis
# ============================================================================


def analyze_story(_, openai_api_key: str, story: dict, keyword: str) -> dict:
    client = OpenAI(api_key=openai_api_key)

    prompt = f"""Analyze this Hacker News story for relevance to "{keyword}":

Title: {story.get("title", "")}
URL: {story.get("url") or "No URL"}
Points: {story.get("points", 0)}
Comments: {story.get("num_comments", 0)}

Rate the relevance (1-10) and determine if it's interesting enough to notify someone.
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


def notify_findings(
    _, findings: list[dict], keyword: str, slack_webhook: Optional[str] = None
) -> None:
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
    db_path: str,
    openai_api_key: str,
    keyword: str,
    relevance_threshold: int,
    slack_webhook: Optional[str],
) -> None:
    """
    Single durable scan of Hacker News for one keyword.

    Each sub-step (fetch, dedup check, AI analysis, DB write, notify) is
    checkpointed. A crash mid-scan resumes from the last successful step
    rather than re-running everything from scratch.
    """
    print(f"🔍 Scanning HN for: '{keyword}'")

    stories = yield ctx.run(search_hackernews, keyword)
    print(f"📚 Found {len(stories)} stories")

    new_stories = []
    for story in stories:
        already_seen = yield ctx.run(has_processed_story, db_path, story["objectID"])
        if not already_seen:
            new_stories.append(story)

    print(f"📊 {len(new_stories)} new stories to analyze")

    interesting = []
    for story in new_stories:
        analysis = yield ctx.run(analyze_story, openai_api_key, story, keyword)

        yield ctx.run(
            mark_story_processed,
            db_path,
            analysis["story_id"],
            analysis["title"],
            analysis["url"],
            analysis["relevance_score"],
            analysis["is_interesting"],
        )

        if analysis["is_interesting"] and analysis["relevance_score"] >= relevance_threshold:
            interesting.append(analysis)

    yield ctx.run(notify_findings, interesting, keyword, slack_webhook)

    print(f"📊 Scan Results for '{keyword}':")
    print(f"   Stories found:      {len(stories)}")
    print(f"   New stories:        {len(new_stories)}")
    print(f"   Interesting:        {len(interesting)}\n")


@resonate.register
def monitor_hackernews(
    ctx: Context,
    db_path: str,
    openai_api_key: str,
    keywords: list[str],
    relevance_threshold: int,
    scan_interval_secs: float,
    slack_webhook: Optional[str],
) -> None:
    """
    Continuous monitoring workflow. Scans all keywords on a configurable
    interval, sleeping durably between rounds.

    Because sleep is durable, a restart during a sleep period resumes the
    sleep rather than triggering an immediate redundant scan.
    """
    yield ctx.run(init_database, db_path)

    print("🤖 Hacker News Monitor Started")
    print(f"📡 Monitoring keywords: {', '.join(keywords)}")
    print(f"⏰ Scan interval: {scan_interval_secs / 60:.0f} minutes")
    print(f"🎯 Relevance threshold: {relevance_threshold}/10\n")

    while True:
        scan_start = time.monotonic()

        for keyword in keywords:
            try:
                yield ctx.run(
                    scan_keyword,
                    db_path,
                    openai_api_key,
                    keyword,
                    relevance_threshold,
                    slack_webhook,
                )
            except Exception as e:
                print(f"❌ Error scanning '{keyword}': {e}")

        elapsed = time.monotonic() - scan_start
        print(f"✅ Round complete ({elapsed:.1f}s)\n")

        yield ctx.sleep(scan_interval_secs)


# ============================================================================
# Entry Point
# ============================================================================


def main() -> None:
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise SystemExit("❌ OPENAI_API_KEY environment variable is required")

    keywords = [k.strip() for k in os.getenv("HN_KEYWORDS", "AI").split(",")]
    db_path = os.getenv("DB_PATH", "hackernews_agent.db")
    slack_webhook = os.getenv("SLACK_WEBHOOK")
    scan_interval_secs = float(os.getenv("SCAN_INTERVAL_SECS", "3600"))
    relevance_threshold = int(os.getenv("RELEVANCE_THRESHOLD", "7"))

    print("\n🤖 Hacker News Agent Worker Started")
    print(f"⚙️  Keywords: {', '.join(keywords)}")
    print(f"⏰ Scan interval: {scan_interval_secs / 60:.0f} minutes\n")
    print("📝 To run a one-time scan:")
    print(f'   resonate invoke scan-1 --func scan_keyword --arg "{keywords[0]}"')
    print("\n📝 To start continuous monitoring:")
    print("   resonate invoke monitor-1 --func monitor_hackernews\n")

    handle = monitor_hackernews.begin_run(
        "hackernews-monitor",
        db_path,
        openai_api_key,
        keywords,
        relevance_threshold,
        scan_interval_secs,
        slack_webhook,
    )

    try:
        handle.result()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        resonate.stop()


if __name__ == "__main__":
    main()
