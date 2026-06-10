import logging
import os

from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

load_dotenv()

pool = AsyncConnectionPool(
    conninfo=os.getenv("DATABASE_URL"),
    min_size=1,
    max_size=20,
    timeout=2.0,
    max_idle=30,
    kwargs={
        "row_factory": dict_row,
    },
    open=False,
)

async def initDatabase():
    try:
        await pool.open()
        logging.info("Database connection pool opened")
        
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS member_analyses (
                        id SERIAL PRIMARY KEY,
                        member_id VARCHAR(255),
                        member_name VARCHAR(255) NOT NULL,
                        member_email VARCHAR(255),
                        member_title VARCHAR(255),
                        member_timezone VARCHAR(100),
                        fit_score INTEGER NOT NULL,
                        insights JSONB,
                        recommendations JSONB,
                        research_data JSONB,
                        analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        sent_to_slack BOOLEAN DEFAULT FALSE,
                        sent_to_slack_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                
                await cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_member_id ON member_analyses(member_id);
                    """
                )
                
                await cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_analyzed_at ON member_analyses(analyzed_at);
                    """
                )
                
            await conn.commit()
            
        logging.info("Database schema initialized")
        
    except Exception as error:
        logging.error(f'Failed to initialize database: {error}')
        raise
    
    
async def saveMemberAnalysis(member_info, analysis, research_data):
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id
                    FROM member_analyses
                    WHERE member_id = %s
                    ORDER BY analyzed_at DESC, id DESC
                    LIMIT 1
                    """,
                    (member_info.get("id"),),
                )
                existing = await cur.fetchone()

                if existing:
                    await cur.execute(
                        """
                        UPDATE member_analyses
                        SET
                            member_name = %s,
                            member_email = %s,
                            member_title = %s,
                            member_timezone = %s,
                            fit_score = %s,
                            insights = %s,
                            recommendations = %s,
                            research_data = %s,
                            analyzed_at = CURRENT_TIMESTAMP,
                            sent_to_slack = FALSE,
                            sent_to_slack_at = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        RETURNING id
                        """,
                        (
                            member_info["name"],
                            member_info.get("email"),
                            member_info.get("title"),
                            member_info.get("timezone"),
                            analysis["fitScore"],
                            Jsonb(analysis["insights"]),
                            Jsonb(analysis["recommendations"]),
                            Jsonb(research_data),
                            existing["id"],
                        ),
                    )
                else:
                    await cur.execute(
                        """
                        INSERT INTO member_analyses (
                            member_id,
                            member_name,
                            member_email,
                            member_title,
                            member_timezone,
                            fit_score,
                            insights,
                            recommendations,
                            research_data
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            member_info.get("id"),
                            member_info["name"],
                            member_info.get("email"),
                            member_info.get("title"),
                            member_info.get("timezone"),
                            analysis["fit_score"],
                            Jsonb(analysis["insights"]),
                            Jsonb(analysis["recommendations"]),
                            Jsonb(research_data),
                        ),
                    )
                
                result = await cur.fetchone()
                
            await conn.commit()
            
        analysis_id = result["id"]
        
        logging.info(f'Saved analysis to database with ID: {analysis_id}')
        
        return analysis_id
        
    except Exception as error:
        logging.error(f'Failed to initialize database: {error}')
        raise
    
async def markAsSentToSlack(analysis_id):
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE member_analyses
                    SET
                        sent_to_slack = TRUE,
                        sent_to_slack_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (analysis_id,),
                )

            await conn.commit()

    except Exception as error:
        logging.error(f"Failed to mark as sent to Slack: {error}")
        raise


async def close_database():
    await pool.close()
    logging.info("Database connection pool closed")
    