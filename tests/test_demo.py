from __future__ import annotations

import importlib
import re

from fastapi.testclient import TestClient


def test_demo_seed_is_fictional_and_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'demo.db'}")
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_USERNAME", "demo_admin")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_PASSWORD", "strong-demo-password")

    from app.demo_seed import seed
    from app.config import load_settings
    from app.db import Database
    from app.repository import Repository

    assert seed() == (2, 0)
    assert seed() == (0, 2)
    repository = Repository(Database(load_settings()), load_settings())
    jobs = repository.list_jobs()
    assert len(jobs) == 2
    assert all(job.status == "completed" for job in jobs)
    assert all("demo" in job.normalized_username for job in jobs)
    assert len(repository.list_global_leads()) >= 3
    assert repository.list_discovery_candidates()["stats"]["total"] == 2


def test_demo_web_is_authenticated_and_blocks_external_jobs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'demo-web.db'}")
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_USERNAME", "demo_admin")
    monkeypatch.setenv("PLATFORM_BOOTSTRAP_PASSWORD", "strong-demo-password")
    import app.web as web

    web = importlib.reload(web)
    with TestClient(web.app) as client:
        login_page = client.get("/login")
        assert "轩制作 · Made by XUAN" in login_page.text
        assert "Article" in login_page.text
        client.post("/login", data={"username": "demo_admin", "password": "strong-demo-password"}, follow_redirects=False)
        dashboard = client.get("/")
        assert "演示模式" in dashboard.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', dashboard.text).group(1)
        response = client.post("/scans", data={"csrf": csrf, "target": "@external_target"}, follow_redirects=False)
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        api_response = client.post(
            "/api/scans",
            headers={"x-csrf-token": csrf},
            json={"target": "@external_target", "force": False, "tags": []},
        )
        assert api_response.status_code == 409
        assert api_response.json()["detail"] == "demo_mode_read_only"
