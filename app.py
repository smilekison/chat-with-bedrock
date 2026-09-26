import json
import os
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

import boto3
from botocore.config import Config
import psycopg2
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

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
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "5"))

app = FastAPI(title=APP_TITLE)

# The EC2 instance role supplies AWS credentials. No AWS access keys are
# required in the application environment.
bedrock = boto3.client(
    "bedrock-runtime",
    region_name=AWS_REGION,
    config=Config(connect_timeout=10, read_timeout=300, retries={"max_attempts": 3}),
)

SYSTEM_PROMPT = """
You are an AI expense assistant connected to the user's PostgreSQL expense database.

Rules:
- When a question depends on the user's actual expenses, ALWAYS use the database tools.
- Never invent, estimate, or assume expense amounts that are not returned by the database.
- Use query_expenses for questions about spending, totals, categories, dates, or expense history.
- Use create_expense when the user clearly tells you about a real expense they want recorded.
- If the user asks a general question unrelated to their stored expenses, answer normally.
- When reporting database results, distinguish actual stored data from general advice.
- The current server date is {current_date}. Use it when resolving relative dates such as today, yesterday, this week, or last week.
""".strip()


TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "query_expenses",
                "description": (
                    "Query the user's actual expense records in PostgreSQL. "
                    "Use this whenever the user asks how much they spent, "
                    "what they spent money on, totals, categories, or expense history. "
                    "The application executes the database query; do not write SQL."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "start_date": {
                                "type": "string",
                                "description": "Start date inclusive in YYYY-MM-DD format.",
                            },
                            "end_date": {
                                "type": "string",
                                "description": "End date inclusive in YYYY-MM-DD format.",
                            },
                            "category": {
                                "type": "string",
                                "description": (
                                    "Optional expense category, for example Food, "
                                    "Transport, Shopping, Bills."
                                ),
                            },
                            "currency": {
                                "type": "string",
                                "description": "Optional currency filter, for example NPR or USD.",
                            },
                        },
                        "required": ["start_date", "end_date"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "create_expense",
                "description": (
                    "Record a real expense in PostgreSQL when the user clearly "
                    "states that they spent money. Do not use this for hypothetical "
                    "examples or general questions."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "description": {
                                "type": "string",
                                "description": "Short description of the expense.",
                            },
                            "amount": {
                                "type": "number",
                                "description": "Positive expense amount.",
                            },
                            "currency": {
                                "type": "string",
                                "description": "Currency code such as NPR or USD.",
                            },
                            "category": {
                                "type": "string",
                                "description": "Expense category such as Food or Transport.",
                            },
                            "expense_date": {
                                "type": "string",
                                "description": "Expense date in YYYY-MM-DD format.",
                            },
                        },
                        "required": [
                            "description",
                            "amount",
                            "currency",
                            "category",
                            "expense_date",
                        ],
                    }
                },
            }
        },
    ]
}


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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS expenses (
                    id BIGSERIAL PRIMARY KEY,
                    description TEXT NOT NULL,
                    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
                    currency VARCHAR(10) NOT NULL DEFAULT 'NPR',
                    category VARCHAR(100) NOT NULL,
                    expense_date DATE NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_expenses_date
                ON expenses(expense_date)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_expenses_category_date
                ON expenses(category, expense_date)
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


class ExpenseCreateRequest(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default="NPR", min_length=1, max_length=10)
    category: str = Field(min_length=1, max_length=100)
    expense_date: date


def normalize_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid conversation ID.") from exc


def parse_date(value, field_name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be YYYY-MM-DD.") from exc


def query_expenses(
    start_date: date,
    end_date: date,
    category: str | None = None,
    currency: str | None = None,
):
    if start_date > end_date:
        raise ValueError("start_date cannot be after end_date.")

    conn = db()
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT id, description, amount, currency, category, expense_date
                FROM expenses
                WHERE expense_date BETWEEN %s AND %s
            """
            params = [start_date, end_date]

            if category:
                sql += " AND LOWER(category) = LOWER(%s)"
                params.append(category.strip())

            if currency:
                sql += " AND LOWER(currency) = LOWER(%s)"
                params.append(currency.strip())

            sql += " ORDER BY expense_date ASC, id ASC"

            cur.execute(sql, params)
            rows = cur.fetchall()

            expenses = [
                {
                    "id": row[0],
                    "description": row[1],
                    "amount": str(row[2]),
                    "currency": row[3],
                    "category": row[4],
                    "expense_date": row[5].isoformat(),
                }
                for row in rows
            ]

            totals = {}
            for row in rows:
                key = row[3]
                totals[key] = totals.get(key, Decimal("0")) + row[2]

            return {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "category": category,
                "currency": currency,
                "count": len(expenses),
                "totals": {key: str(value) for key, value in totals.items()},
                "expenses": expenses,
            }
    finally:
        conn.close()


def create_expense(
    description: str,
    amount,
    currency: str,
    category: str,
    expense_date: date,
):
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("amount must be a valid positive number.") from exc

    if amount <= 0:
        raise ValueError("amount must be greater than zero.")

    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO expenses
                    (description, amount, currency, category, expense_date)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, description, amount, currency, category, expense_date, created_at
                """,
                (
                    description.strip(),
                    amount,
                    currency.strip().upper(),
                    category.strip(),
                    expense_date,
                ),
            )
            row = cur.fetchone()

        conn.commit()

        return {
            "id": row[0],
            "description": row[1],
            "amount": str(row[2]),
            "currency": row[3],
            "category": row[4],
            "expense_date": row[5].isoformat(),
            "created_at": row[6].isoformat(),
        }
    finally:
        conn.close()


def execute_tool(tool_name: str, tool_input: dict):
    if tool_name == "query_expenses":
        return query_expenses(
            start_date=parse_date(tool_input.get("start_date"), "start_date"),
            end_date=parse_date(tool_input.get("end_date"), "end_date"),
            category=tool_input.get("category"),
            currency=tool_input.get("currency"),
        )

    if tool_name == "create_expense":
        return create_expense(
            description=str(tool_input.get("description", "")).strip(),
            amount=tool_input.get("amount"),
            currency=str(tool_input.get("currency", "NPR")).strip(),
            category=str(tool_input.get("category", "")).strip(),
            expense_date=parse_date(tool_input.get("expense_date"), "expense_date"),
        )

    raise ValueError(f"Unknown tool: {tool_name}")


def run_bedrock(messages):
    system = [
        {
            "text": SYSTEM_PROMPT.format(current_date=date.today().isoformat())
        }
    ]

    for _ in range(MAX_TOOL_ROUNDS):
        response = bedrock.converse(
            modelId=MODEL_ID,
            system=system,
            messages=messages,
            toolConfig=TOOL_CONFIG,
            inferenceConfig={
                "maxTokens": 800,
                "temperature": 0,
            },
        )

        output_message = response["output"]["message"]
        messages.append(output_message)

        if response.get("stopReason") != "tool_use":
            text_blocks = [
                block["text"]
                for block in output_message.get("content", [])
                if "text" in block
            ]
            if text_blocks:
                return "\n".join(text_blocks).strip()
            return "I could not generate a text response."

        tool_results = []

        for block in output_message.get("content", []):
            if "toolUse" not in block:
                continue

            tool_use = block["toolUse"]
            tool_result = {
                "toolUseId": tool_use["toolUseId"],
                "content": [],
            }

            try:
                result = execute_tool(tool_use["name"], tool_use.get("input", {}))
                tool_result["content"] = [{"json": result}]
            except Exception as exc:
                tool_result["status"] = "error"
                tool_result["content"] = [
                    {
                        "text": (
                            "The database operation could not be completed. "
                            f"Reason: {type(exc).__name__}."
                        )
                    }
                ]

            tool_results.append({"toolResult": tool_result})

        if not tool_results:
            return "I could not complete the requested database operation."

        messages.append(
            {
                "role": "user",
                "content": tool_results,
            }
        )

    return "I could not complete the request because the tool-call limit was reached."


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
        raise HTTPException(
            status_code=503,
            detail=f"Database unavailable: {type(exc).__name__}",
        ) from exc
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


@app.post("/api/expenses")
def add_expense(expense: ExpenseCreateRequest):
    # Useful for controlled testing/manual entry. Production applications
    # should add authentication before exposing expense-management endpoints.
    return create_expense(
        description=expense.description,
        amount=expense.amount,
        currency=expense.currency,
        category=expense.category,
        expense_date=expense.expense_date,
    )


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
                cur.execute(
                    "SELECT 1 FROM conversations WHERE id = %s",
                    (conversation_id,),
                )
                if not cur.fetchone():
                    raise HTTPException(
                        status_code=404,
                        detail="Conversation not found.",
                    )
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

        reply = run_bedrock(messages)

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
