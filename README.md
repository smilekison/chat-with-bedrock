# Bedrock + RDS Chat App

Small FastAPI web application that:

- sends chat messages to Amazon Bedrock
- saves every user/assistant message in PostgreSQL/RDS
- restores conversation history from PostgreSQL
- exposes GET/POST APIs
- uses the EC2 instance IAM role for Bedrock authentication

## Run on EC2

```bash
cd ~/bedrock-rds-chat-app

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Set environment variables:

```bash
export AWS_REGION="us-east-1"
export MODEL_ID="amazon.nova-lite-v1:0"
export RDS_HOST="YOUR_RDS_ENDPOINT"
export RDS_PORT="5432"
export RDS_DATABASE="expenses"
export RDS_USER="postgres"
export RDS_PASSWORD="YOUR_RDS_PASSWORD"
```

Start:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://YOUR_EC2_PUBLIC_IP:8000
```

## API

### Health

```http
GET /health
```

### List conversations

```http
GET /api/conversations
```

### Get conversation messages

```http
GET /api/conversations/{conversation_id}/messages
```

### Send message

```http
POST /api/chat
Content-Type: application/json

{
  "conversation_id": null,
  "message": "Hello Bedrock"
}
```

The first request creates a conversation. Later requests use the returned `conversation_id`.

## IAM

The EC2 instance role needs permission to invoke the Bedrock model, for example:

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

No AWS access keys are required in the application.
