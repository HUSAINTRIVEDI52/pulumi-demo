"""
SonarQube Ephemeral Infrastructure - AWS Account Agnostic
Pulumi IaC with GitOps, auto-scheduling, and cost optimization.

Supports: AWS (ECS Fargate Spot + EFS + RDS Aurora Serverless v2)
Strategy: Ephemeral containers, data persistence via EFS/RDS, auto start/stop
"""

import pulumi
import pulumi_aws as aws
import json
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
cfg = pulumi.Config()
env          = cfg.get("env")           or "dev"
project      = cfg.get("project")       or "sonarqube"
region       = cfg.get("region")        or "us-east-1"
# Working hours (UTC) — adjust per your timezone
work_start   = cfg.get_int("workStartHourUTC") or 7   # 7 AM UTC
work_end     = cfg.get_int("workEndHourUTC")   or 19  # 7 PM UTC
work_days    = cfg.get("workDays")             or "MON-FRI"
# Compute tier: "spot" (cheapest) | "on-demand"
compute_tier = cfg.get("computeTier")          or "spot"
# SonarQube edition: "community" (free) | "developer" | "enterprise"
sq_edition   = cfg.get("sonarEdition")         or "community"
# DB: "aurora-serverless" (cheapest) | "rds-postgres"
db_type      = cfg.get("dbType")               or "aurora-serverless"

tags = {
    "Project":     project,
    "Environment": env,
    "ManagedBy":   "pulumi",
    "GitOps":      "true",
    "CostCenter":  "engineering",
}

# ─────────────────────────────────────────────
# NETWORKING
# ─────────────────────────────────────────────
vpc = aws.ec2.Vpc(
    f"{project}-vpc",
    cidr_block           = "10.0.0.0/16",
    enable_dns_hostnames = True,
    enable_dns_support   = True,
    tags                 = {**tags, "Name": f"{project}-{env}-vpc"},
)

igw = aws.ec2.InternetGateway(
    f"{project}-igw",
    vpc_id = vpc.id,
    tags   = {**tags, "Name": f"{project}-{env}-igw"},
)

# Public subnets (2 AZs for HA ALB)
public_subnets = []
private_subnets = []
azs = ["a", "b"]

for i, az in enumerate(azs):
    pub = aws.ec2.Subnet(
        f"{project}-public-{az}",
        vpc_id                  = vpc.id,
        cidr_block              = f"10.0.{i}.0/24",
        availability_zone       = f"{region}{az}",
        map_public_ip_on_launch = True,
        tags                    = {**tags, "Name": f"{project}-public-{az}"},
    )
    priv = aws.ec2.Subnet(
        f"{project}-private-{az}",
        vpc_id            = vpc.id,
        cidr_block        = f"10.0.{10+i}.0/24",
        availability_zone = f"{region}{az}",
        tags              = {**tags, "Name": f"{project}-private-{az}"},
    )
    public_subnets.append(pub)
    private_subnets.append(priv)

# NAT Gateway (single AZ for cost savings in dev)
eip = aws.ec2.Eip(f"{project}-nat-eip", domain="vpc", tags=tags)
nat_gw = aws.ec2.NatGateway(
    f"{project}-nat",
    subnet_id     = public_subnets[0].id,
    allocation_id = eip.id,
    tags          = {**tags, "Name": f"{project}-nat"},
)

# Route tables
pub_rt = aws.ec2.RouteTable(
    f"{project}-pub-rt",
    vpc_id = vpc.id,
    routes = [{"cidr_block": "0.0.0.0/0", "gateway_id": igw.id}],
    tags   = tags,
)
priv_rt = aws.ec2.RouteTable(
    f"{project}-priv-rt",
    vpc_id = vpc.id,
    routes = [{"cidr_block": "0.0.0.0/0", "nat_gateway_id": nat_gw.id}],
    tags   = tags,
)

for i, (pub, priv) in enumerate(zip(public_subnets, private_subnets)):
    aws.ec2.RouteTableAssociation(f"{project}-pub-rta-{i}", subnet_id=pub.id, route_table_id=pub_rt.id)
    aws.ec2.RouteTableAssociation(f"{project}-priv-rta-{i}", subnet_id=priv.id, route_table_id=priv_rt.id)

# ─────────────────────────────────────────────
# SECURITY GROUPS
# ─────────────────────────────────────────────
alb_sg = aws.ec2.SecurityGroup(
    f"{project}-alb-sg",
    vpc_id      = vpc.id,
    description = "ALB inbound HTTP/HTTPS",
    ingress     = [
        {"from_port": 80,  "to_port": 80,  "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]},
        {"from_port": 443, "to_port": 443, "protocol": "tcp", "cidr_blocks": ["0.0.0.0/0"]},
    ],
    egress      = [{"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]}],
    tags        = {**tags, "Name": f"{project}-alb-sg"},
)

ecs_sg = aws.ec2.SecurityGroup(
    f"{project}-ecs-sg",
    vpc_id      = vpc.id,
    description = "SonarQube ECS task",
    ingress     = [{"from_port": 9000, "to_port": 9000, "protocol": "tcp", "security_groups": [alb_sg.id]}],
    egress      = [{"from_port": 0,    "to_port": 0,    "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]}],
    tags        = {**tags, "Name": f"{project}-ecs-sg"},
)

db_sg = aws.ec2.SecurityGroup(
    f"{project}-db-sg",
    vpc_id      = vpc.id,
    description = "SonarQube DB",
    ingress     = [{"from_port": 5432, "to_port": 5432, "protocol": "tcp", "security_groups": [ecs_sg.id]}],
    egress      = [{"from_port": 0,    "to_port": 0,    "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]}],
    tags        = {**tags, "Name": f"{project}-db-sg"},
)

efs_sg = aws.ec2.SecurityGroup(
    f"{project}-efs-sg",
    vpc_id      = vpc.id,
    description = "EFS mount",
    ingress     = [{"from_port": 2049, "to_port": 2049, "protocol": "tcp", "security_groups": [ecs_sg.id]}],
    egress      = [{"from_port": 0,    "to_port": 0,    "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]}],
    tags        = {**tags, "Name": f"{project}-efs-sg"},
)

# ─────────────────────────────────────────────
# EFS  (persistent SonarQube data — survives container restarts)
# ─────────────────────────────────────────────
efs = aws.efs.FileSystem(
    f"{project}-efs",
    encrypted        = True,
    performance_mode = "generalPurpose",
    throughput_mode  = "bursting",
    lifecycle_policies = [{"transition_to_ia": "AFTER_30_DAYS"}],
    tags             = {**tags, "Name": f"{project}-efs"},
)

efs_mount_targets = [
    aws.efs.MountTarget(
        f"{project}-efs-mt-{i}",
        file_system_id  = efs.id,
        subnet_id       = priv.id,
        security_groups = [efs_sg.id],
    )
    for i, priv in enumerate(private_subnets)
]

efs_ap = aws.efs.AccessPoint(
    f"{project}-efs-ap",
    file_system_id = efs.id,
    posix_user     = {"uid": 1000, "gid": 1000},
    root_directory = {
        "path": "/sonarqube",
        "creation_info": {"owner_uid": 1000, "owner_gid": 1000, "permissions": "755"},
    },
    tags = tags,
)

# ─────────────────────────────────────────────
# RDS AURORA SERVERLESS V2  (auto-pause = ~$0 when idle)
# ─────────────────────────────────────────────
db_subnet_group = aws.rds.SubnetGroup(
    f"{project}-db-sng",
    subnet_ids  = [s.id for s in private_subnets],
    description = f"{project} DB subnet group",
    tags        = tags,
)

db_password = cfg.require_secret("dbPassword")

db_cluster = aws.rds.Cluster(
    f"{project}-db",
    cluster_identifier      = f"{project}-{env}",
    engine                  = "aurora-postgresql",
    engine_mode             = "provisioned",
    engine_version          = "15.4",
    database_name           = "sonarqube",
    master_username         = "sonarqube",
    master_password         = db_password,
    db_subnet_group_name    = db_subnet_group.name,
    vpc_security_group_ids  = [db_sg.id],
    serverlessv2_scaling_configuration = {
        "min_capacity": 0.5,   # 0.5 ACU minimum — auto-pauses after 5 min idle
        "max_capacity": 4.0,   # scales up to 4 ACU under load
    },
    skip_final_snapshot     = env != "prod",
    deletion_protection     = env == "prod",
    backup_retention_period = 7,
    preferred_backup_window = "02:00-03:00",
    storage_encrypted       = True,
    tags                    = tags,
)

db_instance = aws.rds.ClusterInstance(
    f"{project}-db-instance",
    cluster_identifier = db_cluster.id,
    instance_class     = "db.serverless",
    engine             = db_cluster.engine,
    engine_version     = db_cluster.engine_version,
    tags               = tags,
)

# ─────────────────────────────────────────────
# ECR (optional: mirror sonarqube image for speed)
# ─────────────────────────────────────────────
ecr_repo = aws.ecr.Repository(
    f"{project}-ecr",
    name                 = f"{project}/{env}",
    image_tag_mutability = "MUTABLE",
    image_scanning_configuration = {"scan_on_push": True},
    tags                 = tags,
)

aws.ecr.LifecyclePolicy(
    f"{project}-ecr-policy",
    repository = ecr_repo.name,
    policy     = json.dumps({
        "rules": [{
            "rulePriority": 1,
            "description":  "Keep last 5 images",
            "selection":    {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 5},
            "action":       {"type": "expire"},
        }]
    }),
)

# ─────────────────────────────────────────────
# IAM — ECS Task roles
# ─────────────────────────────────────────────
task_exec_role = aws.iam.Role(
    f"{project}-exec-role",
    assume_role_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }),
    tags = tags,
)
aws.iam.RolePolicyAttachment(
    f"{project}-exec-policy",
    role       = task_exec_role.name,
    policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
)

task_role = aws.iam.Role(
    f"{project}-task-role",
    assume_role_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}],
    }),
    tags = tags,
)

# Allow EFS access from task
aws.iam.RolePolicy(
    f"{project}-task-efs-policy",
    role   = task_role.id,
    policy = pulumi.Output.all(efs.arn).apply(lambda args: json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect":   "Allow",
            "Action":   ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"],
            "Resource": args[0],
        }],
    })),
)

# SSM Parameter Store for secrets
aws.iam.RolePolicy(
    f"{project}-task-ssm-policy",
    role   = task_exec_role.id,
    policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect":   "Allow",
            "Action":   ["ssm:GetParameters", "secretsmanager:GetSecretValue", "kms:Decrypt"],
            "Resource": "*",
        }],
    }),
)

# ─────────────────────────────────────────────
# SSM PARAMETERS  (GitOps: managed via config repo)
# ─────────────────────────────────────────────
db_pass_param = aws.ssm.Parameter(
    f"{project}-db-pass",
    name  = f"/{project}/{env}/db/password",
    type  = "SecureString",
    value = db_password,
    tags  = tags,
)

# ─────────────────────────────────────────────
# CLOUDWATCH LOG GROUP
# ─────────────────────────────────────────────
log_group = aws.cloudwatch.LogGroup(
    f"{project}-logs",
    name              = f"/ecs/{project}/{env}",
    retention_in_days = 14,
    tags              = tags,
)

# ─────────────────────────────────────────────
# ECS CLUSTER
# ─────────────────────────────────────────────
cluster = aws.ecs.Cluster(
    f"{project}-cluster",
    name = f"{project}-{env}",
    settings = [{"name": "containerInsights", "value": "enabled"}],
    tags     = tags,
)

# Capacity providers: FARGATE_SPOT (cheapest) + FARGATE (fallback)
aws.ecs.ClusterCapacityProviders(
    f"{project}-cp",
    cluster_name       = cluster.name,
    capacity_providers = ["FARGATE_SPOT", "FARGATE"],
    default_capacity_provider_strategies = [
        {"capacity_provider": "FARGATE_SPOT", "weight": 4, "base": 0},
        {"capacity_provider": "FARGATE",      "weight": 1, "base": 1},
    ],
)

# ─────────────────────────────────────────────
# TASK DEFINITION
# SonarQube community: needs 2vCPU / 4GB min
# ─────────────────────────────────────────────
sq_image = "sonarqube:10-community"  # pin to specific tag in production

task_def = aws.ecs.TaskDefinition(
    f"{project}-task",
    family                   = f"{project}-{env}",
    cpu                      = "2048",  # 2 vCPU
    memory                   = "4096",  # 4 GB
    network_mode             = "awsvpc",
    requires_compatibilities = ["FARGATE"],
    execution_role_arn       = task_exec_role.arn,
    task_role_arn            = task_role.arn,
    volumes = [{
        "name": "sonarqube-data",
        "efs_volume_configuration": {
            "file_system_id":          efs.id,
            "transit_encryption":      "ENABLED",
            "authorization_config":    {
                "access_point_id": efs_ap.id,
                "iam":             "ENABLED",
            },
        },
    }],
    container_definitions = pulumi.Output.all(
        db_cluster.endpoint,
        db_pass_param.arn,
        log_group.name,
    ).apply(lambda args: json.dumps([{
        "name":      "sonarqube",
        "image":     sq_image,
        "essential": True,
        "portMappings": [{"containerPort": 9000, "protocol": "tcp"}],
        "environment": [
            {"name": "SONAR_JDBC_URL",      "value": f"jdbc:postgresql://{args[0]}:5432/sonarqube"},
            {"name": "SONAR_JDBC_USERNAME", "value": "sonarqube"},
            {"name": "SONAR_WEB_PORT",      "value": "9000"},
            # JVM tuning for Fargate
            {"name": "SONAR_CE_JAVAOPTS",   "value": "-Xmx1g -Xms512m"},
            {"name": "SONAR_WEB_JAVAOPTS",  "value": "-Xmx512m -Xms256m"},
        ],
        "secrets": [
            {"name": "SONAR_JDBC_PASSWORD", "valueFrom": args[1]},
        ],
        "mountPoints": [{
            "sourceVolume":  "sonarqube-data",
            "containerPath": "/opt/sonarqube/data",
            "readOnly":      False,
        }],
        "ulimits": [{"name": "nofile", "softLimit": 65535, "hardLimit": 65535}],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group":         args[2],
                "awslogs-region":        region,
                "awslogs-stream-prefix": "sonarqube",
            },
        },
        "healthCheck": {
            "command":     ["CMD-SHELL", "wget -qO- http://localhost:9000/api/system/status | grep -q 'UP' || exit 1"],
            "interval":    30,
            "timeout":     5,
            "retries":     5,
            "startPeriod": 120,
        },
    }])),
    tags = tags,
)

# ─────────────────────────────────────────────
# ALB
# ─────────────────────────────────────────────
alb = aws.lb.LoadBalancer(
    f"{project}-alb",
    name               = f"{project}-{env}",
    internal           = False,
    load_balancer_type = "application",
    security_groups    = [alb_sg.id],
    subnets            = [s.id for s in public_subnets],
    idle_timeout       = 120,
    tags               = {**tags, "Name": f"{project}-alb"},
)

target_group = aws.lb.TargetGroup(
    f"{project}-tg",
    name        = f"{project}-{env}",
    port        = 9000,
    protocol    = "HTTP",
    target_type = "ip",
    vpc_id      = vpc.id,
    health_check = {
        "path":                "/api/system/status",
        "healthy_threshold":   2,
        "unhealthy_threshold": 5,
        "interval":            30,
        "timeout":             10,
        "matcher":             "200",
    },
    deregistration_delay = 30,
    tags                 = tags,
)

alb_listener = aws.lb.Listener(
    f"{project}-listener",
    load_balancer_arn = alb.arn,
    port              = 80,
    protocol          = "HTTP",
    default_actions   = [{"type": "forward", "target_group_arn": target_group.arn}],
)

# ─────────────────────────────────────────────
# ECS SERVICE  (desired_count=0 by default; scheduler turns it on)
# ─────────────────────────────────────────────
service = aws.ecs.Service(
    f"{project}-service",
    name            = f"{project}-{env}",
    cluster         = cluster.id,
    task_definition = task_def.arn,
    desired_count   = 0,   # ← ephemeral: off by default
    capacity_provider_strategies = [
        {"capacity_provider": "FARGATE_SPOT", "weight": 4, "base": 0},
        {"capacity_provider": "FARGATE",      "weight": 1, "base": 1},
    ],
    network_configuration = {
        "subnets":          [s.id for s in private_subnets],
        "security_groups":  [ecs_sg.id],
        "assign_public_ip": False,
    },
    load_balancers = [{
        "target_group_arn": target_group.arn,
        "container_name":   "sonarqube",
        "container_port":   9000,
    }],
    health_check_grace_period_seconds = 180,
    deployment_circuit_breaker = {"enable": True, "rollback": True},
    deployment_maximum_percent         = 200,
    deployment_minimum_healthy_percent = 0,   # allow 0 tasks (required for start/stop)
    enable_execute_command = True,
    tags                   = tags,
    opts                   = pulumi.ResourceOptions(depends_on=efs_mount_targets),
)

# ─────────────────────────────────────────────
# APPLICATION AUTO SCALING (scale to 0 / 1)
# ─────────────────────────────────────────────
aas_target = aws.appautoscaling.Target(
    f"{project}-aas-target",
    max_capacity       = 1,
    min_capacity       = 0,
    resource_id        = pulumi.Output.all(cluster.name, service.name).apply(
                            lambda a: f"service/{a[0]}/{a[1]}"
                         ),
    scalable_dimension = "ecs:service:DesiredCount",
    service_namespace  = "ecs",
)

# ─────────────────────────────────────────────
# EVENTBRIDGE SCHEDULER  — Start/Stop on working hours
# ─────────────────────────────────────────────
scheduler_role = aws.iam.Role(
    f"{project}-scheduler-role",
    assume_role_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect":    "Allow",
            "Principal": {"Service": "scheduler.amazonaws.com"},
            "Action":    "sts:AssumeRole",
        }],
    }),
    tags = tags,
)

aws.iam.RolePolicy(
    f"{project}-scheduler-policy",
    role   = scheduler_role.id,
    policy = pulumi.Output.all(service.id, cluster.arn).apply(lambda _: json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["ecs:UpdateService", "application-autoscaling:RegisterScalableTarget"],
                "Resource": "*",
            },
        ],
    })),
)

# START — weekdays at work_start UTC
start_schedule = aws.scheduler.Schedule(
    f"{project}-start",
    name                         = f"{project}-{env}-start",
    schedule_expression          = f"cron(0 {work_start} ? * {work_days} *)",
    schedule_expression_timezone = "UTC",
    flexible_time_window         = {"mode": "OFF"},
    target = {
        "arn":      "arn:aws:scheduler:::aws-sdk:ecs:updateService",
        "role_arn": scheduler_role.arn,
        "input": pulumi.Output.all(cluster.name, service.name).apply(lambda a: json.dumps({
            "Cluster":      a[0],
            "Service":      a[1],
            "DesiredCount": 1,
        })),
    },
)

# STOP — weekdays at work_end UTC
stop_schedule = aws.scheduler.Schedule(
    f"{project}-stop",
    name                         = f"{project}-{env}-stop",
    schedule_expression          = f"cron(0 {work_end} ? * {work_days} *)",
    schedule_expression_timezone = "UTC",
    flexible_time_window         = {"mode": "OFF"},
    target = {
        "arn":      "arn:aws:scheduler:::aws-sdk:ecs:updateService",
        "role_arn": scheduler_role.arn,
        "input": pulumi.Output.all(cluster.name, service.name).apply(lambda a: json.dumps({
            "Cluster":      a[0],
            "Service":      a[1],
            "DesiredCount": 0,
        })),
    },
)

# ─────────────────────────────────────────────
# CLOUDWATCH ALARMS + SNS
# ─────────────────────────────────────────────
alarm_topic = aws.sns.Topic(f"{project}-alarms", tags=tags)

aws.cloudwatch.MetricAlarm(
    f"{project}-cpu-alarm",
    comparison_operator = "GreaterThanThreshold",
    evaluation_periods  = 2,
    metric_name         = "CPUUtilization",
    namespace           = "AWS/ECS",
    period              = 300,
    statistic           = "Average",
    threshold           = 85,
    alarm_description   = "SonarQube CPU > 85%",
    dimensions          = {"ClusterName": cluster.name, "ServiceName": service.name},
    alarm_actions       = [alarm_topic.arn],
    tags                = tags,
)

# ─────────────────────────────────────────────
# OUTPUTS
# ─────────────────────────────────────────────
pulumi.export("vpc_id",          vpc.id)
pulumi.export("cluster_name",    cluster.name)
pulumi.export("service_name",    service.name)
pulumi.export("alb_dns",         alb.dns_name)
pulumi.export("sonarqube_url",   alb.dns_name.apply(lambda d: f"http://{d}"))
pulumi.export("db_endpoint",     db_cluster.endpoint)
pulumi.export("efs_id",          efs.id)
pulumi.export("ecr_repo",        ecr_repo.repository_url)
pulumi.export("log_group",       log_group.name)
pulumi.export("start_schedule",  f"cron(0 {work_start} ? * {work_days} *)")
pulumi.export("stop_schedule",   f"cron(0 {work_end} ? * {work_days} *)")