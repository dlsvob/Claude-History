#!/usr/bin/env bash
# Deploy Claude History Browser to Google Cloud Run
#
# The app runs without pre-loaded data — users upload their own
# ~/.claude/ archives via the /welcome page.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - A GCP project selected (gcloud config set project PROJECT_ID)
#   - Cloud Run API enabled
#
# Usage:
#   ./deploy.sh                          # uses defaults
#   PROJECT_ID=my-proj ./deploy.sh

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-east1}"
SERVICE="${SERVICE:-claude-history}"
REPO="${REPO:-claude-history-repo}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:latest"

echo "==> Project:  ${PROJECT_ID}"
echo "==> Region:   ${REGION}"
echo "==> Service:  ${SERVICE}"
echo "==> Image:    ${IMAGE}"
echo ""

# --- Step 1: Create Artifact Registry repo (if it doesn't exist) ---
echo "--- Step 1: Ensure Artifact Registry repo exists ---"
gcloud artifacts repositories describe "${REPO}" --location="${REGION}" 2>/dev/null || \
  gcloud artifacts repositories create "${REPO}" --repository-format=docker --location="${REGION}"
echo ""

# --- Step 2: Build container image with Cloud Build ---
echo "--- Step 2: Build container image ---"
gcloud builds submit --no-cache --tag "${IMAGE}" --timeout=20m
echo ""

# --- Step 3: Deploy to Cloud Run (upload-only, no pre-loaded data) ---
echo "--- Step 3: Deploy to Cloud Run ---"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --allow-unauthenticated \
  --port 8080 \
  --session-affinity \
  --memory 2Gi \
  --cpu 2 \
  --timeout 600 \
  --remove-volume claude-data \
  --remove-env-vars "CLAUDE_DATA_DIR,CLAUDE_ARCHIVE_DIR"

echo ""
echo "==> Deployed! Service URL:"
gcloud run services describe "${SERVICE}" --region "${REGION}" --format 'value(status.url)'
