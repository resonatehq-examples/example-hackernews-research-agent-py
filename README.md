# Hacker News Research Agent (Python + Resonate)

A durable Hacker News monitoring agent built with [Resonate](https://resonatehq.io) and Python. Continuously scans for content matching your keywords and uses AI to evaluate relevance — surviving restarts without losing progress or re-processing stories.

## The distributed systems problem this solves

Scanning an external API, calling an AI model for each result, and notifying on findings are all steps that can fail independently. In a naive implementation, a crash mid-scan means you lose track of where you were: stories might be re-analyzed, notifications might be sent twice, or results might be lost entirely.

Resonate turns the entire scan into a **durable workflow**. Every step is checkpointed — the HN fetch, each AI analysis, the notification. If the process crashes, it resumes from the last successful checkpoint.

Deduplication across scan rounds works through Resonate's replay mechanism: when the workflow recovers, it replays the generator and returns cached results for completed steps. The `seen_ids` set rebuilds itself correctly in order — no external database needed.

## Features

- **Durable execution** — survives crashes and restarts, resumes mid-scan
- **Deduplication** — stories analyzed in previous rounds are skipped, rebuilt through Resonate replay
- **Durable sleep** — the interval between rounds is a checkpoint; a restart during sleep resumes the sleep rather than triggering an immediate scan
- **AI-powered analysis** — GPT-4o-mini evaluates relevance and summarizes findings
- **Multi-keyword support** — monitor multiple topics in a single agent
- **Slack notifications** — optional webhook for interesting findings
- **No external database** — Resonate's promise store is the only state you need

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
monitor_hackernews()          ← owns seen_ids, sleeps durably between rounds
  └─ scan_keyword() × keywords
       ├─ search_hackernews()  ← fetch from HN Algolia API        (checkpoint)
       ├─ analyze_story() × N  ← AI relevance scoring per story   (checkpoint)
       └─ notify_findings()    ← console + optional Slack         (checkpoint)
```

`seen_ids` is a plain Python set. Resonate makes it durable: on replay, each
`yield ctx.run()` returns its cached result, so the set rebuilds in the same
order — correctly excluding already-processed stories.

## Code Structure

```
src/
└── agent.py    # All workflows and step functions (~180 LOC)
```

## License

MIT
