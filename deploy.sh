#!/usr/bin/env bash
# Build and deploy the agent to Google Cloud Run.
#
# Prerequisites (see README.md "Deploy" section for details):
#   - gcloud CLI installed and authenticated: gcloud auth login
#   - A GCP project created, with billing enabled against your education credit
#   - APIs enabled: gcloud services enable run.googleapis.com cloudbuild.googleapis.com
#   - A .env file in this directory (copy .env.example and fill in real values)
#
# Usage: ./deploy.sh

set -euo pipefail

if [ ! -f .env ]; then
  echo "Error: .env not found. Copy .env.example to .env and fill in your credentials first." >&2
  exit 1
fi

set -a
source .env
set +a

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID must be set in .env}"
: "${OPENAI_API_KEY:?OPENAI_API_KEY must be set in .env}"

SERVICE_NAME="csci599-a1"
REGION="us-west1"
IMAGE="gcr.io/${GCP_PROJECT_ID}/${SERVICE_NAME}"

echo "==> Building and pushing container image: ${IMAGE}"
gcloud config set project "${GCP_PROJECT_ID}" >/dev/null
gcloud builds submit --tag "${IMAGE}"

# Build the --set-env-vars value. TAVILY_API_KEY and OPENAI_BASE_URL are
# included only if set, so you can deploy with just OpenAI configured, or
# omit Tavily if you haven't gotten a key yet (that server will simply show
# as not-connected on /health rather than failing the whole deployment).
ENV_VARS="OPENAI_API_KEY=${OPENAI_API_KEY},OPENAI_MODEL=${OPENAI_MODEL:-gpt-5},MCP_SERVERS_CONFIG=/app/mcp_config.json,MCP_FILESYSTEM_ROOT=/app/mcp_workspace"
if [ -n "${TAVILY_API_KEY:-}" ]; then
  ENV_VARS="${ENV_VARS},TAVILY_API_KEY=${TAVILY_API_KEY}"
fi
if [ -n "${OPENAI_BASE_URL:-}" ]; then
  ENV_VARS="${ENV_VARS},OPENAI_BASE_URL=${OPENAI_BASE_URL}"
fi

echo "==> Deploying to Cloud Run (region: ${REGION})"
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}" \
  --platform managed \
  --region "${REGION}" \
  --allow-unauthenticated \
  --memory 512Mi \
  --min-instances 0 \
  --max-instances 1 \
  --set-env-vars "${ENV_VARS}"

URL=$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(status.url)')
echo ""
echo "==> Live URL: ${URL}"
echo "    Submit this URL for grading. Verify with:"
echo "    curl ${URL}/health"
echo "    curl -X POST ${URL}/chat -H 'Content-Type: application/json' -d '{\"query\":\"hello\",\"session_id\":\"test\"}'"
