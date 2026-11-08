---
name: project-k8s-deploy-and-otel
description: "Where the worker runs (k3s evilflowers-dev), how CI deploys it, what blocks the first develop push, and the OTel contract"
metadata:
  type: project
  verified: 2026-11-08
---

- Dev = k3s ns `evilflowers-dev`, Deployment and container `lcpencrypt`, selector
  `app.kubernetes.io/name=lcpencrypt,app.kubernetes.io/instance=dev`, pods run as root. Manifest
  owned by the Ansible role `evilflowers_k8s` in private EvilFlowersCatalog/knowledge. Prod still
  Docker Compose on gateway (2026-11-08).
- 2026-11-08: CI in `.github/workflows/docker.yml` + `.github/workflows/deploy.yml` (runner
  `elvira-k3s`, rollout restart + digest check). develop → dev automatic; master → prod only with
  `vars.PRODUCTION_DEPLOY_ENABLED=true`. Commit `efb2a2c` on local `develop`, not pushed.
- Blocker: knowledge#1 items 1 (dev image `:develop`) and 6 (repo in the ARC runner group). The
  first `develop` push before that queues forever or fails its digest check.
- Telemetry = OTel SDK 1.45.2 / contrib 0.66b2 (catalog IP-019 train), endpoint `HOST_IP:4318`;
  Logfire removed. JSON logs via Celery's `setup_logging` signal; Celery passes the level as an int.
- Filed 2026-11-08: knowledge#1, EvilFlowersCatalog#95, this repo #2.

**How to apply:** check knowledge#1 before suggesting a push. Never guess cluster addresses; read
the `Deploy-a-Service` note (evilflowers-knowledge MCP). Related: [[project-lcpencrypt-worker]].
