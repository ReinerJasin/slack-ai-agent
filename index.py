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
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from db import (initDatabase, saveMemberAnalysis, markAsSentToSlack, close_database)

load_dotenv()

logging.basicConfig(level=logging.INFO)

# Custom Log
# class Logger:
#     @staticmethod
#     def info(msg, *args):
#         print(f"[INFO] {msg}", *args)

#     @staticmethod
#     def error(msg, *args):
#         print(f"[ERROR] {msg}", *args)
        
#     @staticmethod
#     def debug(msg, *args):
#         if os.getenv("NODE_ENV") == "development":
#             print(f"[DEBUG] {msg}", *args)

# Agent Class
class SlackAIAgent:
    def __init__(self):
        self.slack = App(
            token=os.getenv("SLACK_BOT_TOKEN"),
            signing_secret=os.getenv("SLACK_SIGNING_SECRET")
        )
        
        self.web_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
        
        self.openai = ChatOpenAI(
            model="gpt-4",
            temperature=0.3,
            api_key=os.getenv("OPENAI_API_KEY")
        )
        
        self.socket_mode_handler = None
        
        app_token = os.getenv("SLACK_APP_TOKEN")
        if app_token:
            self.socket_mode_handler = SocketModeHandler(self.slack, app_token)

        self.app = FastAPI(lifespan=self.lifespan)
        self.setupFastAPIRoutes()
        
        @self.slack.event('team_join')
        async def handle_team_join(event, logger):
            try:
                # Log event
                logging.info(f'New member joined: {event["user"]["name"] or event["user"]["real_name"] }')
                
                # Get detailed user information
                user_info = await self.get_user_info(event["user"]["id"])
                
                # Analyze and post member
                await self.analyzeAndPostMember(user_info)

            except Exception as e:
                logging.error(f'Error handling team_join: {e}')
        
        @self.slack.event('member_joined_channel')
        async def handle_member_joined_channel(event, logger):
            try:
                if event["channel_type"] == "C":
                    # Log event
                    logging.info(f'Member {event["user"]} joined channel {event["channel"]}')
                    
                    # Get user info
                    user_info = self.get_user_info(event["user"]["id"])
                    
                    # Analyze and post member
                    await self.analyzeAndPostMember(user_info)

            except Exception as e:
                logging.error(f'Error handling member_joined_channel: {e}')
    
    async def start(self):
        await initDatabase()
        
        if self.socket_mode_handler:
            self.socket_mode_task = asyncio.create_task(
                asyncio.to_thread(self.socket_mode_handler.start)
            )
            logging.info("Slack Socket Mode Handler Started")
            
    async def stop(self):
        if self.socket_mode_handler:
            close = getattr(self.socket_mode_handler, "close", None)
            if callable(close):
                await asyncio.to_thread(close)

        await close_database()

    @asynccontextmanager
    async def lifespan(self, app: FastAPI):
        await self.start()
        try:
            yield
        finally:
            await self.stop()
    
    async def get_user_info(self, user_id: str) -> dict:
        result = await asyncio.to_thread(self.web_client.users_info, user=user_id)
        user = result["user"]

        return {
            "id": user["id"],
            "name": user.get("real_name") or user.get("name"),
            "username": user.get("name"),
            "email": user.get("profile", {}).get("email"),
            "title": user.get("profile", {}).get("title"),
            "timezone": user.get("tz"),
            "profile": {
                "first_name": user.get("profile", {}).get("first_name"),
                "last_name": user.get("profile", {}).get("last_name"),
                "status_text": user.get("profile", {}).get("status_text"),
            }
        }
        
        
    async def analyzeAndPostMember(self, member_info: dict):
        analysisId = None
        
        try:
            # Logging info
            logging.info(f'Processing member: {member_info["name"]}')
            
            # Do research about the member
            research_data = await self.doBasicResearch(member_info)
            analysis = await self.analyzeWithAI(member_info, research_data)
            
            # Logging info
            logging.info(f'Saving analysis to database for {member_info["name"]}')
            
            # Save the analysis result
            analysisId = await saveMemberAnalysis(member_info, analysis, research_data)
            
            # Post analysis to channel
            await self.postAnalysisToChannel(member_info, analysis, research_data)
            
            # If analysis return non empty, then mark as sent
            if analysisId:
                await markAsSentToSlack(analysisId)
        
        except Exception as e:
            logging.error(f"Error processing {member_info.get('name', 'unknown member')}: {e}")
            
            if analysisId:
                logging.info(f'Analysis {analysisId} saved to database but not sent to Slack due to error')

            raise
    
    async def doBasicResearch(self, user_info:dict):
        result = []
        
        try:
            # check if the email exist and only if it's not a personal email
            if user_info["email"] and not self.isPersonalEmail(user_info["email"]):
                
                # Extract the domain part only
                domain = user_info["email"].rsplit('@', 1)[-1]
                
                ### COMPANY INFO ###
                company_info = await self.getCompanyInfo(domain)
                
                if company_info:
                    result.append(company_info)
                
                ### GITHUB INFO ###
                if user_info["name"]:
                    github_info = await self.getGithubInfo(user_info["name"])
                    
                    if github_info:
                        result.append(github_info)
                    
            
        except Exception as e:
            logging.error(f'Research error: {e}')
            
        return result
    
    @staticmethod
    def isPersonalEmail(email: str) -> bool:
        """
        Check whether email is personal or not by checking the domain
        """
        
        # personalDomains = {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com'}
        personalDomains = {'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com'} # gmail removed for testing purposes
        
        # Extract the domain part only
        domain = email.rsplit('@', 1)[-1].lower() if '@' in email else None
        
        return domain in personalDomains
    
    async def getCompanyInfo(self, domain: str):
        """
        Get company info by accessing the domain
        """
        def _fetch():
            response = requests.get(
                f'https://www.{domain}',
                timeout=5,
                headers={
                    "User-Agent": "Mozilla/5.0"
                }
            )
            
            response.raise_for_status()
            return response.text
        
        try:
            html = await asyncio.to_thread(_fetch)
            
            title_match = re.search(
                r"<title>(.*?)</title>",
                html,
                re.IGNORECASE | re.DOTALL
            )
            
            title = title_match.group(1).strip() if title_match else f"Company: {domain}"
            
            return {
                "url": f'https://www.{domain}',
                "title": title,
                "content": f"Company website for {domain}",
                "type": "company",
            }
            
        except Exception as e:
            logging.error(f'Could not fetch {domain}:', str(e))
    
    
    async def getGithubInfo(self, name: str):
        """
        Get Github info by searching the name on github
        """
        
        def _fetch():
            response = requests.get(
                f'https://api.github.com/search/users',
                params={"q": f'"{name}"'},
                timeout=5,
                headers={"Accept": "application/vnd.github+json"},
            )
            
            
            response.raise_for_status()
            return response.json()
        
        try:
            data = await asyncio.to_thread(_fetch)
            items = data.get("items", [])
            
            if items and len(items) > 0:
                user = items[0]
                
                return {
                    "url": user["html_url"],
                    "title": f'Github: {user['login']}',
                    "content": f'{user.get('public_repos', 0)} public repositories',
                    "type": "github"
                }
                
        except Exception as e:
            logging.debug(f'GitHub search error: {e}')
            
        return None
            
    async def analyzeWithAI(self, member_info, research_data):
        prompt = ChatPromptTemplate.from_template(
            '''
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
            
            Consider job title, company size, technical background, and budget athority.
            '''
        )
        
        try:
            if len(research_data) > 0:
                researchSummary = "\n".join(
                    f"{item.get('title', 'Untitled')}: {item.get('content', "")}" for item in research_data
                )
            else:
                researchSummary = "Limited research data available"
            
            chain = prompt | self.openai
            
            result = await chain.ainvoke({
                "company_name": os.getenv("COMPANY_NAME", "Reiner Technologies Inc."),
                "company_product": os.getenv("COMPANY_PRODUCT", "Smart Agentic AI for Humanity Purposes"),
                "name": member_info["name"],
                "email": member_info["email"] or "Not Provided",
                "title": member_info["title"] or "Not provided",
                "research": researchSummary,
            })
            
            response_text = result.content if hasattr(result, "content") else str(result)
            
            cleaned_response = re.sub(r"```json\s*|\s*```", "", response_text).strip()
            
            analysis = json.loads(cleaned_response)
            
            return {
                "fitScore": max(0, min(100, analysis.get("fitScore", 50))),
                "insights": analysis.get("insights") if isinstance(analysis.get("insights"), list) else ["Analysis Completed"],
                "recommendations": analysis.get("recommendations") if isinstance(analysis.get("recommendations"), list) else ["Follow up recommended"]
            }
            
        except Exception as e:
            logging.error(f"AI analysis error: {str(e)}")
            
            return {
                "fitScore": 50,
                "insights": ["Unable to complete full analysis"],
                "recommendations": ["Manual review recommended"]
            }
    
    async def postAnalysisToChannel(self, member, analysis, research_data):
        
        # Set the color
        fitScore = analysis["fitScore"]
        
        if fitScore >= 80:
            color = "#36a64f"
        elif fitScore >= 60:
            color = "#ffb84d"
        elif fitScore >= 40:
            color = "#ff9500"
        else:
            color = "#ff4444"
        
        # Set the block
        blocks = [
            {
                "type": "header",
                "text": { "type": "plain_text", "text": f"🔍 New Member: {member['name']}" }
            },
            {
                "type": "section",
                "fields": [
                    { "type": "mrkdwn", "text": f"*Fit Score:* {analysis['fitScore']}" },
                    { "type": "mrkdwn", "text": f"*Email:* {member['email'] or "Not Provided"}" },
                    { "type": "mrkdwn", "text": f"*Title:* {member['title'] or "Not Provided"}" },
                ]
            }
        ]
        
        ### INSIGHTS ###
        insights = analysis.get("insights", [])
        
        if len(insights) > 0:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Insights:*\n" + "\n".join(
                        f"{item}" for item in insights
                    ),
                }
            })
        
        ### RECOMMENDATIONS ###
        recommendations = analysis.get("recommendations", [])
        
        if len(recommendations) > 0:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Recommendations:*\n" + "\n".join(
                        f"{item}" for item in recommendations
                    ),
                }
            })
            
        ### TIME ###
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"📊 Analyzed: {datetime.now(timezone.utc).isoformat()}"
                }
            ]
        })
        
        await asyncio.to_thread(
            self.web_client.chat_postMessage,
            channel=os.getenv("SLACK_PRIVATE_CHANNEL_ID"),
            text=f"New Member Analysis: {member['name']} ({fitScore}/100)",
            attachments=[
                {
                    "color": color,
                    "blocks": blocks
                }
            ],
        )
        
        logging.info(f"Analysis posted to channel for {member['name']}")
    
    def setupFastAPIRoutes(self):
        @self.app.get("/health")
        async def health():
            return {
                "status": "healthy",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
        if os.getenv("NODE_ENV") == 'development':
            @self.app.post("/test/analyze-member")
            async def test_analyze_member(payload: AnalyzeMemberRequest):
                try:
                    member_info = payload.memberInfo
                    
                    if not member_info:
                        raise HTTPException(
                            status_code=400,
                            detail="memberInfo is required"
                        )
                        
                    analysis = await self.analyzeAndPostMember(member_info)
                    
                    return {
                        "success": True,
                        "analysis": analysis,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }

                except HTTPException:
                    raise
                
                except Exception as e:
                    logging.error(f'Test analysis error: {e}')
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "error": "Analysis failed",
                            "message": str(e)
                        }
                    )
                    
        @self.app.exception_handler(Exception)
        async def global_exception_handler(request: Request, e: Exception):
            logging.error(f'Express error {e}')      
            
            return JSONResponse(
                status_code=500,
                content={"error": "Internal server error"}
            )
            
class AnalyzeMemberRequest(BaseModel):
    memberInfo: dict

agent = SlackAIAgent()
app = agent.app
