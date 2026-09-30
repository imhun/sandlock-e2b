"""Deployment assets: manifests, scripts and node-side helpers.

The one Python package that still lives here is the NFS quota agent
(``deploy.quota_agent``), kept importable for tests. C3's per-node agent moved
to the top-level ``c3_agent/`` on 2026-09-30: it is a service with its own
image, and ``deploy/`` holds deployment configuration and scripts.
"""
