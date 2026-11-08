---
name: project-k8s-deploy-and-otel
description: "Where the worker runs (k3s evilflowers-dev), how CI deploys it (develop auto-deploy works since c37f894), and the OTel contract"
metadata:
  node_type: memory
  type: project
  verified: 2026-11-08
  originSessionId: 0c064b23-8064-4a5f-9bbb-5e6b4a4211ed
  modified: 2026-11-08T13:38:58.119Z
---

- Dev = k3s ns `evilflowers-dev`, Deployment and container `lcpencrypt`, selector
  `app.kubernetes.io/name=lcpencrypt,app.kubernetes.io/instance=dev`, pods run as root. Manifest
  owned by the Ansible role `evilflowers_k8s` in private EvilFlowersCatalog/knowledge. Prod still
  Docker Compose on gateway (2026-11-08).
- 2026-11-08: CI in `.github/workflows/docker.yml` + `.github/workflows/deploy.yml` (runner
  `elvira-k3s`, rollout restart + digest check). develop → dev automatic; master → prod only with
  `vars.PRODUCTION_DEPLOY_ENABLED=true`.
- 2026-11-08: the `develop` pushes of `efb2a2c` and `fb8821e` failed CI; `c37f894` (issues #1/#2)
  was the first green run: tests, build, and `deploy-development / rollout` on `elvira-k3s` with
  the digest check passing. The knowledge#1 blockers (items 1, 6) no longer stop a dev deploy.
- Telemetry = OTel SDK 1.45.2 / contrib 0.66b2 (catalog IP-019 train), endpoint `HOST_IP:4318`;
  Logfire removed. JSON logs via Celery's `setup_logging` signal; Celery passes the level as an int.
- Filed 2026-11-08: knowledge#1, EvilFlowersCatalog#95, this repo #2.

**How to apply:** a `develop` push deploys to dev; check the run's rollout job, not just the
workflow conclusion. Never guess cluster addresses; read
the `Deploy-a-Service` note (evilflowers-knowledge MCP). Related: [[project-lcpencrypt-worker]].
