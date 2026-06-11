import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from db import close_database, initDatabase, markAsSentToSlack, saveMemberAnalysis

load_dotenv()
logging.basicConfig(level=logging.INFO)


class AnalyzeMemberRequest(BaseModel):
    """Request body for the local development analysis endpoint."""

    memberInfo: dict


class SlackAIAgent:
    """Owns the Slack listeners, FastAPI app, AI workflow, and persistence flow."""

    def __init__(self):
        """Configure Slack clients, OpenAI access, and HTTP routes."""
        # Create the async Slack app used to receive workspace events.
        self.slack = AsyncApp(
            token=os.getenv("SLACK_BOT_TOKEN"),
            signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
        )

        # Create the async Slack Web API client used for user lookups and posting results.
        self.web_client = AsyncWebClient(token=os.getenv("SLACK_BOT_TOKEN"))

        # Configure the LLM used to transform raw profile data into structured analysis.
        self.openai = ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0.3,
            api_key=os.getenv("OPENAI_API_KEY"),
        )

        # The Socket Mode handler is created at startup because it requires a running event loop.
        self.socket_mode_handler = None

        # FastAPI is used for health checks and local development testing.
        self.app = FastAPI(lifespan=self.lifespan)
        self.setupFastAPIRoutes()

        @self.slack.event("team_join")
        async def handle_team_join(event, logger):
            """Analyze a user when they join the Slack workspace."""
            try:
                # Read the joining user from the event payload and guard against malformed events.
                user = event.get("user") or {}
                user_id = user.get("id")
                if not user_id:
                    logging.error("team_join event missing user id: %s", event)
                    return

                # Fetch the latest Slack profile and run the full analysis workflow.
                logging.info("New member joined: %s", user.get("name") or user.get("real_name") or user_id)
                user_info = await self.get_user_info(user_id)
                await self.analyzeAndPostMember(user_info)
            except Exception as error:
                logging.error("Error handling team_join: %s", error)

        @self.slack.event("member_joined_channel")
        async def handle_member_joined_channel(event, logger):
            """Analyze a user when they join a public channel."""
            try:
                # Ignore channel types outside normal public channels for this workflow.
                if event.get("channel_type") != "C":
                    return

                # Extract the user and channel identifiers from Slack's event payload.
                user_id = event.get("user")
                channel_id = event.get("channel")
                if not user_id:
                    logging.error("member_joined_channel event missing user id: %s", event)
                    return

                # Fetch the joining user's profile and run the same downstream analysis flow.
                logging.info("Member %s joined channel %s", user_id, channel_id)
                user_info = await self.get_user_info(user_id)
                await self.analyzeAndPostMember(user_info)
            except Exception as error:
                logging.error("Error handling member_joined_channel: %s", error)

    async def start(self):
        """Initialize the database and start Slack Socket Mode."""
        # Open the database pool and create the required schema if needed.
        await initDatabase()

        # Build the async Socket Mode handler only after the event loop is running.
        app_token = os.getenv("SLACK_APP_TOKEN")
        if app_token and self.socket_mode_handler is None:
            self.socket_mode_handler = AsyncSocketModeHandler(self.slack, app_token)

        # Start the Slack listener in the background so FastAPI startup can complete.
        if self.socket_mode_handler:
            asyncio.create_task(self.socket_mode_handler.start_async())
            logging.info("Slack Socket Mode Handler Started")

    async def stop(self):
        """Shut down Slack connections and close the database pool."""
        # Close the Slack Socket Mode handler if it was created.
        if self.socket_mode_handler:
            close_async = getattr(self.socket_mode_handler, "close_async", None)
            if callable(close_async):
                await close_async()

        # Always close the database pool during application shutdown.
        await close_database()

    @asynccontextmanager
    async def lifespan(self, app: FastAPI):
        """Attach startup and shutdown hooks to the FastAPI application."""
        # Start external resources before serving any requests.
        await self.start()
        try:
            yield
        finally:
            # Ensure resources are released when the process stops.
            await self.stop()

    async def get_user_info(self, user_id: str) -> dict:
        """Fetch a normalized Slack user profile from the Slack Web API."""
        # Request the full Slack profile and reduce it to the fields used by this app.
        result = await self.web_client.users_info(user=user_id)
        user = result["user"]

        return {
            "id": user["id"],
            "name": user.get("real_name") or user.get("name") or user["id"],
            "username": user.get("name"),
            "email": user.get("profile", {}).get("email"),
            "title": user.get("profile", {}).get("title"),
            "timezone": user.get("tz"),
            "profile": {
                "first_name": user.get("profile", {}).get("first_name"),
                "last_name": user.get("profile", {}).get("last_name"),
                "status_text": user.get("profile", {}).get("status_text"),
            },
        }

    async def analyzeAndPostMember(self, member_info: dict):
        """Run enrichment, AI analysis, persistence, and Slack delivery for one member."""
        analysis_id = None

        try:
            # Record progress so the flow is visible in local logs.
            logging.info("Processing member: %s", member_info["name"])

            # Enrich the member with lightweight external context before asking the model.
            research_data = await self.doBasicResearch(member_info)
            analysis = await self.analyzeWithAI(member_info, research_data)

            # Persist the latest analysis before attempting Slack delivery.
            logging.info("Saving analysis to database for %s", member_info["name"])
            analysis_id = await saveMemberAnalysis(member_info, analysis, research_data)

            # Post the final summary into Slack and mark the row as delivered.
            await self.postAnalysisToChannel(member_info, analysis, research_data)
            if analysis_id:
                await markAsSentToSlack(analysis_id)

            return analysis
        except Exception as error:
            # Keep the error visible while preserving the already-saved database row if present.
            logging.error("Error processing %s: %s", member_info.get("name", "unknown member"), error)
            if analysis_id:
                logging.info("Analysis %s saved to database but not sent to Slack due to error", analysis_id)
            raise

    async def doBasicResearch(self, user_info: dict):
        """Collect lightweight external research based on the user's identity data."""
        result = []

        try:
            # Only run company-domain research when the email is present and looks non-personal.
            email = user_info.get("email")
            if email and not self.isPersonalEmail(email):
                domain = email.rsplit("@", 1)[-1]

                # Fetch a minimal company signal from the organization's website.
                company_info = await self.getCompanyInfo(domain)
                if company_info:
                    result.append(company_info)

                # Attempt a simple GitHub profile lookup using the member's display name.
                if user_info.get("name"):
                    github_info = await self.getGithubInfo(user_info["name"])
                    if github_info:
                        result.append(github_info)
        except Exception as error:
            # Research failures are logged but do not block the analysis flow.
            logging.error("Research error: %s", error)

        return result

    @staticmethod
    def isPersonalEmail(email: str) -> bool:
        """Return True when the email appears to come from a personal mailbox provider."""
        # Maintain a small list of common personal-email domains to skip company enrichment.
        personal_domains = {"yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}

        # Extract and normalize the domain for comparison.
        domain = email.rsplit("@", 1)[-1].lower() if "@" in email else None
        return domain in personal_domains

    async def getCompanyInfo(self, domain: str):
        """Fetch a minimal company profile by loading the organization's website."""

        def _fetch():
            """Perform the blocking HTTP request used for company lookup."""
            response = requests.get(
                f"https://www.{domain}",
                timeout=5,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            return response.text

        try:
            # Run the blocking `requests` call in a worker thread so the event loop stays free.
            html = await asyncio.to_thread(_fetch)

            # Extract the page title as a simple description of the organization.
            title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            title = title_match.group(1).strip() if title_match else f"Company: {domain}"

            return {
                "url": f"https://www.{domain}",
                "title": title,
                "content": f"Company website for {domain}",
                "type": "company",
            }
        except Exception as error:
            # A company website miss should not stop the rest of the pipeline.
            logging.error("Could not fetch %s: %s", domain, error)
            return None

    async def getGithubInfo(self, name: str):
        """Search GitHub users by name and return the first useful public profile signal."""

        def _fetch():
            """Perform the blocking GitHub API request used for profile lookup."""
            response = requests.get(
                "https://api.github.com/search/users",
                params={"q": f'"{name}"'},
                timeout=5,
                headers={"Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            return response.json()

        try:
            # Run the blocking GitHub request in a worker thread to avoid blocking async tasks.
            data = await asyncio.to_thread(_fetch)
            items = data.get("items", [])

            # Use the first search hit as a lightweight signal instead of a full profile crawl.
            if items:
                user = items[0]
                return {
                    "url": user["html_url"],
                    "title": f"GitHub: {user['login']}",
                    "content": "Public GitHub profile discovered",
                    "type": "github",
                }
        except Exception as error:
            # GitHub lookup is optional, so failures stay at debug level.
            logging.debug("GitHub search error: %s", error)

        return None

    async def analyzeWithAI(self, member_info, research_data):
        """Generate structured member analysis using the configured OpenAI model."""
        # Define the structured prompt used for every member analysis request.
        prompt = ChatPromptTemplate.from_template(
            """
            Analyze this new community member for fit with our commercial product.

            Company: {company_name}
            Product: {company_product}

            Member:
            - Name: {name}
            - Email: {email}
            - Title: {title}

            Research Data:
            {research}

            Provide a JSON response with:
            - fitScore (0-100): likelihood they'd be interested in our product
            - insights: array of 3-5 key observations
            - recommendations: array of 2-4 engagement suggestions

            Consider job title, company size, technical background, and budget authority.
            """
        )

        try:
            # Flatten research into plain text so it can be injected into the LLM prompt.
            if research_data:
                research_summary = "\n".join(
                    f"{item.get('title', 'Untitled')}: {item.get('content', '')}" for item in research_data
                )
            else:
                research_summary = "Limited research data available"

            # Execute the prompt through the configured chat model.
            chain = prompt | self.openai
            result = await chain.ainvoke(
                {
                    "company_name": os.getenv("COMPANY_NAME", "Reiner Technologies Inc."),
                    "company_product": os.getenv("COMPANY_PRODUCT", "Smart agentic AI products"),
                    "name": member_info["name"],
                    "email": member_info.get("email") or "Not provided",
                    "title": member_info.get("title") or "Not provided",
                    "research": research_summary,
                }
            )

            # Strip Markdown code fences in case the model wraps JSON in a fenced block.
            response_text = result.content if hasattr(result, "content") else str(result)
            cleaned_response = re.sub(r"```json\s*|\s*```", "", response_text).strip()
            analysis = json.loads(cleaned_response)

            # Normalize the model output into a predictable application response.
            return {
                "fitScore": max(0, min(100, analysis.get("fitScore", 50))),
                "insights": analysis.get("insights")
                if isinstance(analysis.get("insights"), list)
                else ["Analysis completed"],
                "recommendations": analysis.get("recommendations")
                if isinstance(analysis.get("recommendations"), list)
                else ["Follow-up recommended"],
            }
        except Exception as error:
            # Fall back to a safe default response if parsing or model execution fails.
            logging.error("AI analysis error: %s", error)
            return {
                "fitScore": 50,
                "insights": ["Unable to complete full analysis"],
                "recommendations": ["Manual review recommended"],
            }

    async def postAnalysisToChannel(self, member, analysis, research_data):
        """Post the finished analysis into the configured Slack reporting channel."""
        # Compute a message color based on the fit score to make the summary scannable.
        fit_score = analysis["fitScore"]
        if fit_score >= 80:
            color = "#36a64f"
        elif fit_score >= 60:
            color = "#ffb84d"
        elif fit_score >= 40:
            color = "#ff9500"
        else:
            color = "#ff4444"

        # Build the base Slack message blocks with the member identity and score.
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"New Member: {member['name']}"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Fit Score:* {fit_score}"},
                    {"type": "mrkdwn", "text": f"*Email:* {member.get('email') or 'Not provided'}"},
                    {"type": "mrkdwn", "text": f"*Title:* {member.get('title') or 'Not provided'}"},
                ],
            },
        ]

        # Append model-generated insights when present.
        insights = analysis.get("insights", [])
        if insights:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Insights:*\n" + "\n".join(f"- {item}" for item in insights),
                    },
                }
            )

        # Append recommended next steps when present.
        recommendations = analysis.get("recommendations", [])
        if recommendations:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Recommendations:*\n" + "\n".join(f"- {item}" for item in recommendations),
                    },
                }
            )

        # Add a minimal research summary so the message shows which enrichments were used.
        if research_data:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "Research sources: " + ", ".join(item["type"] for item in research_data),
                        }
                    ],
                }
            )

        # Add the analysis timestamp to the Slack message footer.
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Analyzed: {datetime.now(timezone.utc).isoformat()}",
                    }
                ],
            }
        )

        # Send the formatted analysis to the configured reporting channel.
        await self.web_client.chat_postMessage(
            channel=os.getenv("SLACK_PRIVATE_CHANNEL_ID"),
            text=f"New Member Analysis: {member['name']} ({fit_score}/100)",
            attachments=[{"color": color, "blocks": blocks}],
        )
        logging.info("Analysis posted to channel for %s", member["name"])

    def setupFastAPIRoutes(self):
        """Register health, test, and exception routes on the FastAPI app."""

        @self.app.get("/health")
        async def health():
            """Return a minimal health status for local or hosted checks."""
            # Keep the health check small and dependency-light.
            return {
                "status": "healthy",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        if os.getenv("NODE_ENV") == "development":

            @self.app.post("/test/analyze-member")
            async def test_analyze_member(payload: AnalyzeMemberRequest):
                """Trigger the analysis pipeline manually during local development."""
                try:
                    # Accept the posted test member and require it to be present.
                    member_info = payload.memberInfo
                    if not member_info:
                        raise HTTPException(status_code=400, detail="memberInfo is required")

                    # Reuse the same production analysis flow used by Slack events.
                    analysis = await self.analyzeAndPostMember(member_info)
                    return {
                        "success": True,
                        "analysis": analysis,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                except HTTPException:
                    # Preserve explicit HTTP exceptions raised above.
                    raise
                except Exception as error:
                    # Convert unexpected failures into a structured API response for debugging.
                    logging.error("Test analysis error: %s", error)
                    raise HTTPException(
                        status_code=500,
                        detail={"error": "Analysis failed", "message": str(error)},
                    )

        @self.app.exception_handler(Exception)
        async def global_exception_handler(request: Request, error: Exception):
            """Convert uncaught application errors into a generic JSON response."""
            # Avoid leaking internals to clients while still logging the underlying failure.
            logging.error("Unhandled application error: %s", error)
            return JSONResponse(status_code=500, content={"error": "Internal server error"})


# Instantiate the agent once so both FastAPI and Slack handlers share the same resources.
agent = SlackAIAgent()
app = agent.app
