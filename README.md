# Slack AI Agent

Slack AI Agent is a Python-based Slack automation service that analyzes newly joined members, stores the analysis in PostgreSQL, and posts the result to Slack. The project combines FastAPI, Slack Bolt, OpenAI, and PostgreSQL into a single event-driven workflow.

The current implementation is working end to end:

- a Slack membership event is received
- member data is enriched
- an AI-generated analysis is produced
- the analysis is saved to PostgreSQL
- the result is posted to the configured Slack reporting channel

## Features

- Listens for `team_join` and `member_joined_channel` Slack events
- Fetches member details from Slack
- Enriches the member profile with lightweight company and GitHub research
- Uses an OpenAI model to generate:
  - a fit score
  - key insights
  - engagement recommendations
- Stores analysis results in PostgreSQL
- Posts the final result into Slack
- Exposes FastAPI endpoints for health checks and local development testing

## Architecture Overview

The application follows this workflow:

1. Slack emits a membership event.
2. The application receives the event through Slack Socket Mode.
3. Member profile details are fetched from Slack.
4. Optional external research is performed using the member's email domain and name.
5. The enriched context is sent to the OpenAI model.
6. The structured analysis is saved in PostgreSQL.
7. A formatted summary is posted to the configured Slack channel.

The main application entry point is [index.py](/Users/reiner/Documents/GitHub/slack-ai-agent/index.py). Database initialization and persistence logic live in [db.py](/Users/reiner/Documents/GitHub/slack-ai-agent/db.py).

## Verified Slack Result

The project is now wired to post analysis results directly into Slack when the bot is configured correctly and invited to the reporting channel.

Screenshot placeholder:

![Slack Analysis Result Placeholder](docs/images/slack-analysis-result.png)

## Tech Stack

- Python 3.12+
- FastAPI
- Slack Bolt for Python
- Slack SDK
- LangChain OpenAI
- PostgreSQL with `psycopg`
- `aiohttp` for async Slack Socket Mode support
- `uv` for dependency management

## Prerequisites

Before running the project, ensure you have:

- Python 3.12 or newer
- PostgreSQL running locally or remotely
- A Slack app configured for Socket Mode
- An OpenAI API key
- `uv` installed

Install `uv` if needed:

```bash
pip install uv
```

## Environment Variables

Create a `.env` file in the project root based on `.env_example`.

```env
PORT=3000
NODE_ENV=development

DATABASE_URL=postgresql://username:password@localhost:5432/slack_ai_agent

SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
SLACK_SIGNING_SECRET=your-slack-signing-secret
SLACK_PRIVATE_CHANNEL_ID=C0123456789

OPENAI_API_KEY=sk-your-openai-api-key
OPENAI_MODEL=gpt-4o-mini

COMPANY_NAME=Rei Technologies Inc.
COMPANY_PRODUCT=Smart agentic AI products
```

Notes:

- For hosted PostgreSQL providers such as Render, use the provider's external connection string.
- If your provider requires TLS, include `?sslmode=require` in `DATABASE_URL`.
- `SLACK_PRIVATE_CHANNEL_ID` must be a real Slack channel ID, not a channel name.

## Slack App Requirements

Your Slack app should be configured with:

- Socket Mode enabled
- A bot token
- An app-level token for Socket Mode
- Event subscriptions for:
  - `team_join`
  - `member_joined_channel`

Depending on your workspace configuration, you will also need the OAuth scopes required to:

- read user profile information
- receive workspace and channel membership events
- post messages to the reporting channel

Operational notes:

- The bot must be invited to the reporting channel.
- If the reporting channel is private, the bot must be explicitly added to it.
- `team_join` triggers when a member joins the workspace.
- `member_joined_channel` triggers when a member joins a public channel.

## Installation

Install project dependencies:

```bash
uv sync
```

If you are starting from a clean environment, this installs the dependencies declared in [pyproject.toml](/Users/reiner/Documents/GitHub/slack-ai-agent/pyproject.toml), including the async Slack transport requirements.

## Running the Project

This application exposes a FastAPI app from `index.py`. Run it locally with `uvicorn`:

```bash
uv run --with uvicorn uvicorn index:app --reload --host 0.0.0.0 --port 8000
```

Once started:

- FastAPI is available at `http://127.0.0.1:8000`
- the PostgreSQL schema is initialized automatically
- the Slack Socket Mode handler starts automatically when `SLACK_APP_TOKEN` is configured

## Available Endpoints

### `GET /health`

Returns a basic health response with a timestamp.

Example:

```bash
curl http://127.0.0.1:8000/health
```

### `POST /test/analyze-member`

Available only when `NODE_ENV=development`.

This endpoint allows you to test the analysis flow without waiting for a real Slack event.

Example:

```bash
curl -X POST http://127.0.0.1:8000/test/analyze-member \
  -H "Content-Type: application/json" \
  -d '{
    "memberInfo": {
      "id": "U123456",
      "name": "Jane Doe",
      "username": "jane",
      "email": "jane@example.com",
      "title": "Engineering Manager",
      "timezone": "Asia/Makassar",
      "profile": {
        "first_name": "Jane",
        "last_name": "Doe",
        "status_text": "Building"
      }
    }
  }'
```

## Database Behavior

On startup, the app creates the `member_analyses` table if it does not already exist. Each record stores:

- member identity fields
- fit score
- insights
- recommendations
- research data
- Slack delivery state
- timestamps

If the same member is analyzed again, the latest row for that member is updated instead of creating an unnecessary duplicate.

## Project Structure

```text
slack-ai-agent
├── index.py       # Main FastAPI + Slack agent implementation
├── db.py          # Main PostgreSQL persistence layer
├── new_index.py   # Alternate or in-progress implementation
├── new_db.py      # Alternate or in-progress database layer
├── .env_example   # Example environment configuration
├── README.md      # Project documentation
└── pyproject.toml # Project metadata and dependencies
```

## Notes

- `PORT` is currently not used directly by the application.
- The current AI prompt is designed for commercial-fit analysis of new members.
- The same architecture can be adapted for other Slack workflows such as onboarding, moderation, or community analytics.

## Reference

- Original video inspiration: [Build Your Own AI Agent – Full Course with OpenAI, Langchain, Render Deployment by freeCodeCamp.org and Code with Ania Kubów](https://www.youtube.com/watch?v=MnG0ugK2JAI)
- Original repository inspiration: https://github.com/kubowania/slack-ai-agent
