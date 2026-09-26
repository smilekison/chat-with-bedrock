import os
import uuid

import boto3
import psycopg2
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

AWS_REGION = os.environ["AWS_REGION"]
MODEL_ID = os.environ["MODEL_ID"]

RDS_HOST = os.environ["RDS_HOST"]
RDS_PORT = int(os.environ["RDS_PORT"])
RDS_DATABASE = os.environ["RDS_DATABASE"]
RDS_USER = os.environ["RDS_USER"]
RDS_PASSWORD = os.environ["RDS_PASSWORD"]

APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
APP_TITLE = os.getenv("APP_TITLE", "Bedrock + RDS Chat")
MAX_CHAT_TURNS = int(os.getenv("MAX_CHAT_TURNS", "30"))
MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", "10000"))

app = FastAPI(title=APP_TITLE)
bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)


def db():
    return psycopg2.connect(
        host=RDS_HOST,
        port=RDS_PORT,
        dbname=RDS_DATABASE,
        user=RDS_USER,
        password=RDS_PASSWORD,
        connect_timeout=10,
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
                ON messages(conversation_id, created_at, id)
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


def normalize_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid conversation ID.") from exc


@app.get("/")
def home():
    return FileResponse("static/index.html")


@app.get("/health")
def health():
    conn = None
    try:
        conn = db()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {type(exc).__name__}") from exc
    finally:
        if conn:
            conn.close()


@app.get("/api/config")
def public_config():
    return {
        "app_title": APP_TITLE,
        "max_message_length": MAX_MESSAGE_LENGTH,
    }


@app.get("/api/conversations")
def list_conversations():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id::text, title, created_at, updated_at
                FROM conversations
                ORDER BY updated_at DESC
            """)
            return [
                {
                    "id": row[0],
                    "title": row[1],
                    "created_at": row[2],
                    "updated_at": row[3],
                }
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


@app.get("/api/conversations/{conversation_id}/messages")
def get_messages(conversation_id: str):
    conversation_id = normalize_uuid(conversation_id)
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, role, content, created_at
                FROM messages
                WHERE conversation_id = %s
                ORDER BY created_at ASC, id ASC
            """, (conversation_id,))
            return [
                {
                    "id": row[0],
                    "role": row[1],
                    "content": row[2],
                    "created_at": row[3],
                }
                for row in cur.fetchall()
            ]
    finally:
        conn.close()


@app.post("/api/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    message = request.message.strip()

    if not message:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    if len(message) > MAX_MESSAGE_LENGTH:
        raise HTTPException(
            status_code=413,
            detail=f"Message is too long. Maximum length is {MAX_MESSAGE_LENGTH}.",
        )

    conversation_id = request.conversation_id
    conn = db()

    try:
        with conn.cursor() as cur:
            if conversation_id:
                conversation_id = normalize_uuid(conversation_id)
                cur.execute("SELECT 1 FROM conversations WHERE id = %s", (conversation_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail="Conversation not found.")
            else:
                conversation_id = str(uuid.uuid4())
                title = message[:80] + ("..." if len(message) > 80 else "")
                cur.execute(
                    "INSERT INTO conversations (id, title) VALUES (%s, %s)",
                    (conversation_id, title),
                )

            cur.execute("""
                INSERT INTO messages (conversation_id, role, content)
                VALUES (%s, 'user', %s)
            """, (conversation_id, message))

            cur.execute("""
                SELECT role, content
                FROM messages
                WHERE conversation_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
            """, (conversation_id, MAX_CHAT_TURNS * 2))
            history_rows = list(reversed(cur.fetchall()))

        messages = [
            {"role": role, "content": [{"text": content}]}
            for role, content in history_rows
        ]

        response = bedrock.converse(
            modelId=MODEL_ID,
            messages=messages,
            inferenceConfig={"maxTokens": 800, "temperature": 0.4},
        )

        reply = response["output"]["message"]["content"][0]["text"]

        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO messages (conversation_id, role, content)
                VALUES (%s, 'assistant', %s)
            """, (conversation_id, reply))
            cur.execute("""
                UPDATE conversations
                SET updated_at = NOW()
                WHERE id = %s
            """, (conversation_id,))

        conn.commit()
        return {"conversation_id": conversation_id, "reply": reply}

    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise HTTPException(
            status_code=502,
            detail=f"Unable to complete request: {type(exc).__name__}",
        ) from exc
    finally:
        conn.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=APP_HOST, port=APP_PORT)
