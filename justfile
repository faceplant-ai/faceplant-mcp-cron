default: dev

build:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Stopping existing container..."
    docker rm -f faceplant-mcp-cron 2>/dev/null || true
    echo "Building image..."
    docker build -f config/Dockerfile -t faceplant-mcp-cron .
    echo "Done. Run 'just dev' to start."

dev:
    #!/usr/bin/env bash
    set -euo pipefail
    [ -f .env ] || cp .env.example .env
    docker rm -f faceplant-mcp-cron 2>/dev/null || true
    docker run -d --name faceplant-mcp-cron --env-file .env \
        -v faceplant-mcp-cron-data:/data \
        -p 5191:8000 faceplant-mcp-cron
    echo "-> http://localhost:5191"

shutdown:
    docker rm -f faceplant-mcp-cron 2>/dev/null || true
    echo "Stopped."

# --- Terraform Bootstrap (runs in CI via GitHub Actions) ---

init:
    #!/usr/bin/env bash
    set -euo pipefail
    REGION="us-west-2"
    BUCKET="faceplant-mcp-cron-tfstate"
    TABLE="faceplant-mcp-cron-tflock"

    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
        --create-bucket-configuration LocationConstraint="$REGION" 2>/dev/null || true
    aws s3api put-bucket-versioning --bucket "$BUCKET" \
        --versioning-configuration Status=Enabled 2>/dev/null || true

    aws dynamodb create-table --table-name "$TABLE" \
        --attribute-definitions AttributeName=LockID,AttributeType=S \
        --key-schema AttributeName=LockID,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST --region "$REGION" 2>/dev/null || true
    aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"

    cd terraform && terraform init

init-destroy:
    #!/usr/bin/env bash
    set -euo pipefail
    REGION="us-west-2"
    BUCKET="faceplant-mcp-cron-tfstate"
    TABLE="faceplant-mcp-cron-tflock"

    state_size=$(aws s3api head-object --bucket "$BUCKET" --key "terraform.tfstate" --query "ContentLength" --output text 2>/dev/null || echo "0")
    if [ "$state_size" -gt 200 ]; then
        echo "ERROR: Terraform state file exists in S3 ($state_size bytes)."
        echo "Run 'cd terraform && terraform destroy' first, then run 'just init-destroy'."
        exit 1
    fi

    echo "Terraform state is empty — safe to delete the state backend."
    read -p "Type 'destroy' to confirm: " confirm
    [ "$confirm" = "destroy" ] || { echo "Aborted."; exit 1; }

    aws s3 rb "s3://$BUCKET" --force 2>/dev/null || true
    aws dynamodb delete-table --table-name "$TABLE" --region "$REGION" 2>/dev/null || true
    echo "Done. Bootstrap resources destroyed."
