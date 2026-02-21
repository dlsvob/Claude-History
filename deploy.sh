#!/usr/bin/env bash
# Deploy Claude History Browser to Google Cloud Run with GCS FUSE
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - A GCP project selected (gcloud config set project PROJECT_ID)
#   - Cloud Run API enabled
#
# Usage:
#   ./deploy.sh                          # uses defaults
#   PROJECT_ID=my-proj BUCKET=my-bucket ./deploy.sh

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-east1}"
BUCKET="${BUCKET:-claude-history-data}"
SERVICE="${SERVICE:-claude-history}"
REPO="${REPO:-claude-history-repo}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:latest"

echo "==> Project:  ${PROJECT_ID}"
echo "==> Region:   ${REGION}"
echo "==> Bucket:   ${BUCKET}"
echo "==> Service:  ${SERVICE}"
echo "==> Image:    ${IMAGE}"
echo ""

# --- Step 1: Create GCS bucket (if it doesn't exist) and sync data ---
echo "--- Step 1: Sync data to gs://${BUCKET}/ ---"
gcloud storage buckets describe "gs://${BUCKET}" 2>/dev/null || gcloud storage buckets create "gs://${BUCKET}" --location="${REGION}"
gcloud storage rsync "${HOME}/.claude" "gs://${BUCKET}/claude" --recursive
gcloud storage rsync "${HOME}/claude-archive" "gs://${BUCKET}/archive" --recursive
echo ""

# --- Step 2: Create Artifact Registry repo (if it doesn't exist) ---
echo "--- Step 2: Ensure Artifact Registry repo exists ---"
gcloud artifacts repositories describe "${REPO}" --location="${REGION}" 2>/dev/null || \
  gcloud artifacts repositories create "${REPO}" --repository-format=docker --location="${REGION}"
echo ""

# --- Step 3: Build container image with Cloud Build ---
echo "--- Step 3: Build container image ---"
gcloud builds submit --no-cache --tag "${IMAGE}" --timeout=20m
echo ""

# --- Step 4: Deploy to Cloud Run with GCS FUSE volume mount ---
echo "--- Step 4: Deploy to Cloud Run ---"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --allow-unauthenticated \
  --port 8080 \
  --session-affinity \
  --memory 2Gi \
  --cpu 2 \
  --execution-environment gen2 \
  --add-volume name=claude-data,type=cloud-storage,bucket="${BUCKET}" \
  --add-volume-mount volume=claude-data,mount-path=/data \
  --set-env-vars "CLAUDE_DATA_DIR=/data/claude,CLAUDE_ARCHIVE_DIR=/data/archive"

echo ""
echo "==> Deployed! Service URL:"
gcloud run services describe "${SERVICE}" --region "${REGION}" --format 'value(status.url)'
