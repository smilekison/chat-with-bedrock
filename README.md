# Chat with Bedrock

FastAPI chat application using Amazon Bedrock and PostgreSQL/RDS.

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

The app automatically creates `conversations` and `messages` tables on startup.

## Endpoints

- `GET /`
- `GET /health`
- `GET /api/config`
- `GET /api/conversations`
- `GET /api/conversations/{conversation_id}/messages`
- `POST /api/chat`

## AutoDeploy

Build as a Docker application and expose container port `8000`. Inject the required environment variables through AutoDeploy. Keep `RDS_PASSWORD` secret.

The deployment target must be allowed to connect to RDS on TCP 5432. Its IAM role must be allowed to invoke the Bedrock model.
