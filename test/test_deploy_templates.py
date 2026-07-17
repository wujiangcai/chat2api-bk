from __future__ import annotations

import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = PROJECT_ROOT / "deploy" / "production"


class DeployTemplateTests(unittest.TestCase):
    def test_production_template_files_exist(self):
        for name in ["docker-compose.yml", "Caddyfile", ".env.production.example", "README.md"]:
            self.assertTrue((DEPLOY_DIR / name).exists(), name)

    def test_compose_includes_production_services_and_env(self):
        compose = (DEPLOY_DIR / "docker-compose.yml").read_text(encoding="utf-8")
        for service in ["postgres:", "redis:", "minio:", "api:", "caddy:"]:
            self.assertIn(service, compose)
        for required in [
            "STORAGE_BACKEND: postgres",
            "DATABASE_URL:",
            "IMAGE_JOB_QUEUE_BACKEND: redis",
            "REDIS_URL:",
            "OBJECT_STORAGE_BACKEND:",
            "OBJECT_STORAGE_PUBLIC_BASE_URL:",
            "SECURITY_HEADERS_ENABLED:",
        ]:
            self.assertIn(required, compose)

    def test_env_example_contains_required_production_variables(self):
        env = (DEPLOY_DIR / ".env.production.example").read_text(encoding="utf-8")
        for required in [
            "APP_ENV=production",
            "CHATGPT2API_AUTH_KEY=",
            "CHATGPT2API_BASE_URL=",
            "WEB_ALLOWED_ORIGINS=",
            "STORAGE_BACKEND",
            "DATABASE_URL=postgresql://",
            "IMAGE_JOB_QUEUE_BACKEND=redis",
            "REDIS_URL=redis://redis:6379/0",
            "OBJECT_STORAGE_BACKEND=minio",
            "OBJECT_STORAGE_BUCKET=",
            "OBJECT_STORAGE_PUBLIC_BASE_URL=",
            "SECURITY_HEADERS_ENABLED=true",
            "ENABLE_HSTS=true",
            "BACKUP_OUTPUT_DIR=",
            "ALERT_JOB_QUEUE_BACKLOG_THRESHOLD=",
        ]:
            self.assertIn(required, env)

    def test_caddyfile_contains_reverse_proxy_and_security_headers(self):
        caddy = (DEPLOY_DIR / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("reverse_proxy api:80", caddy)
        self.assertIn("reverse_proxy minio:9000", caddy)
        self.assertIn("Strict-Transport-Security", caddy)
        self.assertIn("X-Frame-Options", caddy)

    def test_readme_documents_production_preflight(self):
        readme = (DEPLOY_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("scripts/check_production_ready.py", readme)
        self.assertIn("/api/admin/production-readiness", readme)
        self.assertIn("生产上线预检", readme)
        self.assertIn("launch_evidence", readme)
        self.assertIn("scripts/verify_production_deployment.py", readme)
        self.assertIn("--image-job", readme)
        self.assertIn("--upload-evidence", readme)


if __name__ == "__main__":
    unittest.main()
