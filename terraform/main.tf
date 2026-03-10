terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "faceplant-mcp-cron-tfstate"
    key            = "terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "faceplant-mcp-cron-tflock"
    encrypt        = true
  }
}

provider "aws" {
  region = "us-west-2"

  default_tags {
    tags = {
      Project   = "faceplant"
      Service   = "cron"
      ManagedBy = "terraform"
    }
  }
}

# --- Remote state: read shared infra outputs ---

data "terraform_remote_state" "infra" {
  backend = "s3"
  config = {
    bucket = "faceplant-infra-tfstate"
    key    = "terraform.tfstate"
    region = "us-west-2"
  }
}

variable "slack_bot_token" {
  type      = string
  sensitive = true
  default   = ""
}

variable "notion_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "anthropic_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

locals {
  infra = data.terraform_remote_state.infra.outputs
  name  = "mcp-cron"
  port  = 8000
  environment = {
    BASE_URL         = "/api/mcp-cron"
    ALLOWED_ORIGINS  = "https://faceplant.ai"
    BROKER_URL       = "http://broker.faceplant.local:8000"
    GATEWAY_UPSTREAM = "http://mcp-cron.faceplant.local:8000"
    SLACK_BOT_TOKEN  = var.slack_bot_token
    NOTION_API_KEY   = var.notion_api_key
    ANTHROPIC_API_KEY = var.anthropic_api_key
  }
}

# --- ECR ---

resource "aws_ecr_repository" "this" {
  name                 = "faceplant-${local.name}"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "faceplant-${local.name}" }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# --- CloudWatch ---

resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/faceplant-${local.name}"
  retention_in_days = 30

  tags = { Name = "faceplant-${local.name}" }
}

# --- Cloud Map ---

resource "aws_service_discovery_service" "this" {
  name = local.name

  dns_config {
    namespace_id = local.infra.cloudmap_namespace_id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }

  tags = { Name = "faceplant-${local.name}" }
}

# --- EFS for persistent /data volume ---

resource "aws_efs_file_system" "data" {
  creation_token = "faceplant-${local.name}-data"
  encrypted      = true

  tags = { Name = "faceplant-${local.name}-data" }
}

resource "aws_efs_mount_target" "data" {
  for_each = toset(local.infra.private_subnet_ids)

  file_system_id  = aws_efs_file_system.data.id
  subnet_id       = each.value
  security_groups = [local.infra.ecs_security_group_id]
}

# --- ECS Task Definition ---

resource "aws_ecs_task_definition" "this" {
  family                   = "faceplant-${local.name}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = local.infra.ecs_execution_role_arn
  task_role_arn            = local.infra.ecs_task_role_arn

  volume {
    name = "data"

    efs_volume_configuration {
      file_system_id = aws_efs_file_system.data.id
      root_directory = "/"
    }
  }

  container_definitions = jsonencode([{
    name      = local.name
    image     = "${aws_ecr_repository.this.repository_url}:latest"
    essential = true

    portMappings = [{ containerPort = local.port, protocol = "tcp" }]

    mountPoints = [{
      sourceVolume  = "data"
      containerPath = "/data"
      readOnly      = false
    }]

    environment = [
      for k, v in local.environment : { name = k, value = v }
    ]

    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:${local.port}/health')\""]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = "us-west-2"
        "awslogs-stream-prefix" = "ecs"
      }
    }
  }])

  tags = { Name = "faceplant-${local.name}" }
}

# --- ECS Service ---

resource "aws_ecs_service" "this" {
  name            = local.name
  cluster         = local.infra.ecs_cluster_id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  enable_execute_command = true

  network_configuration {
    subnets          = local.infra.private_subnet_ids
    security_groups  = [local.infra.ecs_security_group_id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.this.arn
  }

  tags = { Name = "faceplant-${local.name}" }
}

# --- Outputs ---

output "ecr_url" {
  value = aws_ecr_repository.this.repository_url
}

output "efs_id" {
  value = aws_efs_file_system.data.id
}
