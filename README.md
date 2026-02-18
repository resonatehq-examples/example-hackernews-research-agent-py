# Hacker News Research Agent (Python + Resonate)

A durable Hacker News monitoring agent built with [Resonate](https://resonatehq.io) and Python. Continuously scans for content matching your keywords, uses AI to evaluate relevance, and never processes the same story twice — even across restarts.

## The distributed systems problem this solves

Scanning an external API, calling an AI model for each result, and writing to a database are all steps that can fail independently. In a naive implementation, a crash mid-scan means you lose track of where you were: you might re-analyze stories, skip stories, or send duplicate notifications.

Resonate turns the entire scan into a **durable workflow**. Every step is checkpointed — the HN fetch, each deduplication check, each AI analysis, each database write. If the process crashes, it resumes from the last successful checkpoint rather than starting over.

## Features

- **Durable execution** — survives crashes and restarts, resumes mid-scan
- **Deduplication** — stories are tracked in SQLite; never analyzed twice
- **Durable sleep** — the interval between scans is a checkpoint, not a `time.sleep()`; a restart during a sleep period resumes the sleep rather than triggering an immediate scan
- **AI-powered analysis** — GPT-4o-mini evaluates relevance and summarizes findings
- **Multi-keyword support** — monitor multiple topics in a single agent
- **Slack notifications** — optional webhook for interesting findings

## Quick Start

### 1. Prerequisites

- Python 3.13+
- Resonate Server running locally (`resonate serve`)
- OpenAI API key

### 2. Install

```bash
uv sync
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your settings
```

**Required:**
- `OPENAI_API_KEY` — your OpenAI API key

**Optional:**
- `HN_KEYWORDS` — comma-separated topics to monitor (default: `AI`)
- `SLACK_WEBHOOK` — Slack incoming webhook URL
- `DB_PATH` — path to the SQLite database file (default: `hackernews_agent.db`)
- `SCAN_INTERVAL_SECS` — seconds between scan rounds (default: `3600`)
- `RELEVANCE_THRESHOLD` — minimum AI score (1–10) to count as interesting (default: `7`)

### 4. Start Resonate Server

```bash
resonate serve
```

### 5. Run the agent worker

```bash
uv run agent
```

The worker registers its functions with Resonate and waits for invocations.

### 6. Invoke a scan

In a separate terminal, trigger a one-time scan:

```bash
resonate invoke scan-1 --func scan_keyword --arg "distributed systems"
```

Or start the continuous monitoring loop:

```bash
resonate invoke monitor-1 --func monitor_hackernews
```

## How It Works

```
monitor_hackernews()          ← durable loop, sleeps between rounds
  └─ scan_keyword()           ← one durable scan per keyword
       ├─ search_hackernews() ← fetch stories from HN Algolia API  (checkpoint)
       ├─ has_processed_story() × N  ← dedup check per story      (checkpoint)
       ├─ analyze_story() × N        ← AI relevance scoring        (checkpoint)
       ├─ mark_story_processed() × N ← write to SQLite             (checkpoint)
       └─ notify_findings()          ← console + optional Slack    (checkpoint)
```

Each arrow is a `yield ctx.run()` call — a durable checkpoint. The workflow can be interrupted at any point and will resume from the last completed step.

## Code Structure

```
src/
└── agent.py    # All workflows and step functions (~230 LOC)
```

## Example Output

```
🤖 Hacker News Monitor Started
📡 Monitoring keywords: distributed systems, durable execution
⏰ Scan interval: 60 minutes
🎯 Relevance threshold: 7/10

🔍 Scanning HN for: 'distributed systems'
📚 Found 30 stories
📊 12 new stories to analyze

🎯 Found 2 interesting stories about 'distributed systems':

  📰 Building Reliable Distributed Systems at Scale
     Relevance: 9/10
     A deep dive into how Stripe handles distributed coordination...
     🔗 https://news.ycombinator.com/item?id=39847819
     📎 https://stripe.com/blog/...

✅ Round complete (4.2s)
```

## License

MIT
