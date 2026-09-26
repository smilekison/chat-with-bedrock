import os
import uuid
from datetime import datetime, timezone

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
MODEL_ID = os.getenv("MODEL_ID", "amazon.nova-lite-v1:0")

RDS_HOST = os.environ["RDS_HOST"]
RDS_PORT = int(os.getenv("RDS_PORT", "5432"))
RDS_DATABASE = os.getenv("RDS_DATABASE", "expenses")
RDS_USER = os.environ["RDS_USER"]
RDS_PASSWORD = os.environ["RDS_PASSWORD"]

app = FastAPI(title="Bedrock + RDS Chat")

bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)


def db():
    return psycopg2.connect(
        host=RDS_HOST,
        port=RDS_PORT,
        dbname=RDS_DATABASE,
        user=RDS_USER,
        password=RDS_PASSWORD,
    )


def init_db():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id UUID PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT 'New chat',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id BIGSERIAL PRIMARY KEY,
                    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role VARCHAR(20) NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id, created_at)
            """)
        conn.commit()
    finally:
        conn.close()


@app.on_event("startup")
def startup():
    init_db()


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str


@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.get("/health")
def health():
    conn = db()
    conn.close()
    return {"status": "ok"}


@app.get("/api/conversations")
def list_conversations():
    conn = db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, title, created_at, updated_at
                FROM conversations
                ORDER BY updated_at DESC
            """)
            rows = cur.fetchall()
            return rows
    finally:
        conn.close()


@app.get("/api/conversations/{conversation_id}/messages")
def get_messages(conversation_id: str):
    conn = db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, role, content, created_at
                FROM messages
                WHERE conversation_id = %s
                ORDER BY created_at ASC, id ASC
            """, (conversation_id,))
            rows = cur.fetchall()
            return rows
    finally:
        conn.close()


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    conversation_id = request.conversation_id

    conn = db()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if conversation_id:
                try:
                    uuid.UUID(conversation_id)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid conversation ID.")

                cur.execute(
                    "SELECT id FROM conversations WHERE id = %s",
                    (conversation_id,),
                )
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Conversation not found.")
            else:
                conversation_id = str(uuid.uuid4())
                title = message[:80] + ("..." if len(message) > 80 else "")
                cur.execute(
                    """
                    INSERT INTO conversations (id, title)
                    VALUES (%s, %s)
                    """,
                    (conversation_id, title),
                )

            # Save the user's message first.
            cur.execute(
                """
                INSERT INTO messages (conversation_id, role, content)
                VALUES (%s, 'user', %s)
                """,
                (conversation_id, message),
            )

            # Get conversation history for Bedrock.
            cur.execute(
                """
                SELECT role, content
                FROM messages
                WHERE conversation_id = %s
                ORDER BY created_at ASC, id ASC
                """,
                (conversation_id,),
            )
            history = cur.fetchall()

        # Convert DB history to Bedrock Converse format.
        messages = [
            {
                "role": row["role"],
                "content": [{"text": row["content"]}],
            }
            for row in history
        ]

        response = bedrock.converse(
            modelId=MODEL_ID,
            messages=messages,
            inferenceConfig={
                "maxTokens": 800,
                "temperature": 0.4,
            },
        )

        reply = response["output"]["message"]["content"][0]["text"]

        # Save assistant response.
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages (conversation_id, role, content)
                VALUES (%s, 'assistant', %s)
                """,
                (conversation_id, reply),
            )
            cur.execute(
                """
                UPDATE conversations
                SET updated_at = NOW()
                WHERE id = %s
                """,
                (conversation_id,),
            )
        conn.commit()

        return {
            "conversation_id": conversation_id,
            "reply": reply,
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        conn.close()
