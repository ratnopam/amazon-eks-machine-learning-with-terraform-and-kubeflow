#!/bin/bash
# Build and push slurmd container to ECR
#
# Usage:
#   ./build.sh
#   AWS_REGION=us-east-1 ./build.sh

set -e

REGION="${AWS_REGION:-us-west-2}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REPO_NAME="slurmd-pytorch"
TAG="25.05.0-cu126-py312"
ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}"

echo "Building slurmd container..."
echo "  Region: ${REGION}"
echo "  Account: ${ACCOUNT_ID}"
echo "  Repository: ${ECR_REPO}"
echo "  Tag: ${TAG}"
echo ""

# Login to DLC ECR (source image)
echo "Logging in to AWS DLC ECR..."
aws ecr get-login-password --region us-west-2 | \
    docker login --username AWS --password-stdin 763104351884.dkr.ecr.us-west-2.amazonaws.com

# Create ECR repo if not exists
echo "Creating ECR repository (if not exists)..."
aws ecr describe-repositories --repository-names ${REPO_NAME} --region ${REGION} 2>/dev/null || \
    aws ecr create-repository --repository-name ${REPO_NAME} --region ${REGION}

# Login to target ECR
echo "Logging in to target ECR..."
aws ecr get-login-password --region ${REGION} | \
    docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com

# Build
echo "Building Docker image..."
docker build -t ${ECR_REPO}:${TAG} .

# Push
echo "Pushing to ECR..."
docker push ${ECR_REPO}:${TAG}

echo ""
echo "=========================================="
echo "SUCCESS!"
echo ""
echo "Image: ${ECR_REPO}:${TAG}"
echo ""
echo "Add to terraform.tfvars:"
echo "  slurmd_image_repository = \"${ECR_REPO}\""
echo "  slurmd_image_tag = \"${TAG}\""
echo "=========================================="
