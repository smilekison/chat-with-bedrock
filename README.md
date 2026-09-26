# Chat with Bedrock

FastAPI chat application using Amazon Bedrock and PostgreSQL/RDS.

The application uses Amazon Bedrock tool use (function calling) so the model can request controlled database operations. FastAPI executes those operations against PostgreSQL and sends the results back to Bedrock. Bedrock never receives the PostgreSQL password and never executes arbitrary SQL.

## Architecture

```
Browser
  ↓
FastAPI / EC2
  ├── PostgreSQL/RDS
  │     ├── conversations
  │     ├── messages
  │     └── expenses
  │
  └── Amazon Bedrock
        ├── query_expenses tool
        └── create_expense tool
```

For an expense question such as "What did I spend on food this week?", Bedrock requests `query_expenses`, FastAPI runs a parameterized SQL query, and the database result is returned to Bedrock for the final answer.

## Required AutoDeploy environment variables

- `AWS_REGION`
- `MODEL_ID`
- `RDS_HOST`
- `RDS_PORT`
- `RDS_DATABASE`
- `RDS_USER`
- `RDS_PASSWORD`

## Optional AutoDeploy environment variables

- `APP_HOST` (default: `0.0.0.0`)
- `APP_PORT` (default: `8000`)
- `APP_TITLE` (default: `Bedrock + RDS Chat`)
- `MAX_CHAT_TURNS` (default: `30`)
- `MAX_MESSAGE_LENGTH` (default: `10000`)
- `MAX_TOOL_ROUNDS` (default: `5`)

No new environment variables are required for PostgreSQL tool use.

## AWS authentication

The application does not require AWS access keys. Boto3 uses the AWS credential provider chain. On EC2, attach an IAM role that can invoke the selected Bedrock model.

Example permission:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": "*"
    }
  ]
}
```

## Database

On startup the application automatically creates these tables if they do not already exist:

- `conversations`
- `messages`
- `expenses`

The `expenses` table contains:

- `id`
- `description`
- `amount`
- `currency`
- `category`
- `expense_date`
- `created_at`

Existing `conversations` and `messages` data is not deleted or replaced by the update.

## Bedrock database tools

### `query_expenses`

Used for questions about actual stored expenses. It accepts:

- start date
- end date
- optional category
- optional currency

The application performs parameterized SQL and returns the matching rows and totals to Bedrock.

### `create_expense`

Used when the user clearly states a real expense, for example:

> I spent 500 NPR on dinner yesterday.

Bedrock extracts the structured fields and the application inserts the record into PostgreSQL.

## Endpoints

- `GET /`
- `GET /health`
- `GET /api/config`
- `GET /api/conversations`
- `GET /api/conversations/{conversation_id}/messages`
- `POST /api/chat`
- `POST /api/expenses` — controlled manual/test expense insertion

## Testing after deployment

You do not need to manually create the `expenses` table. Restart/redeploy the application once; startup migration creates it automatically.

You can insert a test expense through the chat:

> I spent 500 NPR on lunch yesterday.

Then ask:

> What did I spend on food this week?

You can also insert a controlled test record with:

```bash
curl -X POST http://YOUR_APP_HOST:8000/api/expenses \
  -H 'Content-Type: application/json' \
  -d '{
    "description": "Test lunch",
    "amount": 500,
    "currency": "NPR",
    "category": "Food",
    "expense_date": "2026-09-25"
  }'
```

Then ask the chat:

> How much did I spend on food this week?

For production use, protect `POST /api/expenses` with authentication before exposing it publicly.

## AutoDeploy

Build as a Docker application and expose container port `8000`. Inject the required environment variables through AutoDeploy. Keep `RDS_PASSWORD` secret.

The deployment target must be allowed to connect to RDS on TCP 5432. Its IAM role must be allowed to invoke the Bedrock model.

No change to the RDS security-group model is required if the deployed container remains on the EC2 instance already allowed to access RDS.
