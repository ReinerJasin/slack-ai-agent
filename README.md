# Slack AI Agent

Slack AI Agent is a Python implementation of an event-driven Slack workflow that analyzes new members as they join a workspace or public channel. The application enriches basic Slack profile data with lightweight external research, asks an OpenAI model to generate structured insights, stores the analysis in PostgreSQL, and posts a summary back into Slack.

This repository is designed as a practical reference for building Slack automation with Python, FastAPI, Slack Bolt, PostgreSQL, and OpenAI.

## Features

- Listens for `team_join` and `member_joined_channel` Slack events
- Fetches Slack profile details for the joined member
- Performs basic enrichment using company website and GitHub profile lookups
- Uses an OpenAI model to generate:
  - a fit score
  - key insights
  - engagement recommendations
- Persists analysis results in PostgreSQL
- Posts formatted analysis results to a private Slack channel
- Exposes a health endpoint and a development test endpoint through FastAPI

## Architecture Overview

The current implementation follows this flow:

1. Slack emits a membership event.
2. The application fetches member details from Slack.
3. Optional research is performed using the member's email domain and name.
4. The enriched profile is sent to the LLM for structured analysis.
5. The result is stored in PostgreSQL.
6. A summary is posted to the configured Slack channel.

The main application entry point is [index.py](/index.py). Database initialization and persistence logic live in [db.py](/db.py).

## Tech Stack

- Python 3.12+
- FastAPI
- Slack Bolt for Python
- Slack SDK
- LangChain OpenAI
- PostgreSQL with `psycopg`
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

## Slack App Requirements

Your Slack app should be configured with:

- Socket Mode enabled
- A bot token
- An app-level token for Socket Mode
- The event subscriptions required by this project:
  - `team_join`
  - `member_joined_channel`

Depending on your workspace configuration, you will also need the appropriate OAuth scopes to:

- read user profile information
- listen to membership events
- post messages to the target channel

## Installation

Install project dependencies:

```bash
uv sync
```

If you prefer `pip`, you can install from `pyproject.toml`, but `uv` is the intended workflow for this repository.

## Running the Project

This application exposes a FastAPI app from `index.py`. A simple way to run it locally is with `uvicorn`.

Run the server:

```bash
uv run --with uvicorn uvicorn index:app --reload --host 0.0.0.0 --port 8000
```

Once started:

- FastAPI will serve the HTTP application on `http://127.0.0.1:8000`
- The Slack Socket Mode handler will start automatically if `SLACK_APP_TOKEN` is set
- The database schema will be initialized on startup

## Available Endpoints

### `GET /health`

Returns a basic health response with a timestamp.

Example:

```bash
curl http://127.0.0.1:8000/health
```

### `POST /test/analyze-member`

Available only when `NODE_ENV=development`.

This endpoint allows you to manually test the member analysis flow without waiting for a real Slack event.

Example request:

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

On startup, the app creates the `member_analyses` table if it does not already exist. Analysis records include:

- member identity fields
- fit score
- insights
- recommendations
- research data
- Slack delivery status
- timestamps

If the same member is analyzed again, the latest record is updated rather than inserting an entirely separate duplicate row.

## Project Structure

```text
slack-ai-agent
├── index.py       # Main FastAPI + Slack agent implementation
├── db.py          # Main PostgreSQL persistence layer
├── new_index.py   # Alternate or in-progress implementation
├── new_db.py      # Alternate or in-progress database layer
├── .env_example   # Example environment configuration
└── pyproject.toml # Project metadata and dependencies
```

## Notes

- `PORT` is currently not used directly by the application.
- The current AI prompt is geared toward commercial-fit analysis for newly joined members.
- The codebase is a useful foundation if you want to adapt the same workflow pattern for moderation, onboarding, community analytics, or other event-driven Slack automations.

## Reference

- Original video inspiration: https://www.youtube.com/watch?v=MnG0ugK2JAI
- Original repository inspiration: https://github.com/kubowania/slack-ai-agent
