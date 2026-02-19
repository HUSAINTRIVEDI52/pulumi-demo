"""
sonarqube_toggle.py — Lambda function for on-demand SonarQube start/stop.
Deployed separately; invoked by:
  - EventBridge Scheduler (automated schedule)
  - API Gateway (manual toggle button / Slack slash command)
  - SNS (cost alerts triggering auto-stop)

Environment variables:
  ECS_CLUSTER   — cluster name
  ECS_SERVICE   — service name
  SNS_TOPIC_ARN — notification topic
"""

import boto3
import json
import os
import logging

logger    = logging.getLogger()
logger.setLevel(logging.INFO)

ecs   = boto3.client("ecs")
sns   = boto3.client("sns")

CLUSTER   = os.environ["ECS_CLUSTER"]
SERVICE   = os.environ["ECS_SERVICE"]
SNS_TOPIC = os.environ.get("SNS_TOPIC_ARN", "")


def handler(event, context):
    action = event.get("action", "status")  # start | stop | status | toggle

    current = _get_desired_count()

    if action == "status":
        running = _get_running_count()
        return {"status": "ok", "desired": current, "running": running}

    if action == "toggle":
        action = "stop" if current > 0 else "start"

    if action == "start":
        new_count = 1
    elif action == "stop":
        new_count = 0
    else:
        return {"status": "error", "message": f"Unknown action: {action}"}

    response = ecs.update_service(
        cluster      = CLUSTER,
        service      = SERVICE,
        desiredCount = new_count,
    )

    msg = f"SonarQube {CLUSTER}/{SERVICE} → desired={new_count} (action={action})"
    logger.info(msg)

    if SNS_TOPIC:
        sns.publish(
            TopicArn = SNS_TOPIC,
            Subject  = f"SonarQube {'Started ▶️' if new_count else 'Stopped ⏹️'}",
            Message  = msg,
        )

    return {
        "status":    "ok",
        "action":    action,
        "desired":   new_count,
        "service":   response["service"]["serviceName"],
    }


def _get_desired_count():
    r = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])
    return r["services"][0]["desiredCount"]


def _get_running_count():
    r = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])
    return r["services"][0]["runningCount"]