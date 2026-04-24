"""Pytest configuration: default env so `import app.main` succeeds during collection."""

from __future__ import annotations

import os


def pytest_configure(config) -> None:  # noqa: ARG001
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")
    os.environ.setdefault("TANDOOR_URL", "http://tandoor.test")
    os.environ.setdefault("TANDOOR_API_TOKEN", "tandoor-token-placeholder")
    os.environ.setdefault("INGEST_API_KEY", "ingest-test-key")
