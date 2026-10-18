# -*- coding: utf-8 -*-
"""Unit tests for API key authentication on the FastAPI surface.

Covers the no-key pass-through (backwards compatibility), 401 for missing and
invalid credentials, both accepted headers, the ``FLOW_API_KEY`` master key,
revocation, the public endpoints, and the failure modes that must never
regress: a non-ASCII credential or stored key, an unreadable or malformed key
store, and credentials offered in the query string.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from flow_agent import api, auth
from flow_agent.auth import (
    create_api_key,
    delete_api_key,
    get_active_keys,
    is_auth_enforced,
    list_api_keys,
    revoke_api_key,
    verify_api_key,
)

# A credential outside ASCII. ``secrets.compare_digest`` refuses such ``str``
# arguments, so this has to be handled rather than crash the request.
NON_ASCII_KEY = "sk-flow-\uD55C\uAE00-\uD0A4"


class FakeEngine:
    """Stand-in for FlowEngine so no upstream account is touched."""

    binary = Path("/tmp/fake/flow")

    def available(self) -> bool:
        return True

    async def generate_image(self, **kwargs):
        return {
            "job_id": "test-job-1",
            "account_id": "test-acct-1",
            "status": "succeeded",
            "files": [{"path": "/tmp/fake_output.jpg"}],
        }

    async def generate_video(self, **kwargs):
        return {"job_id": "test-job-2", "status": "submitted", "files": []}

    async def get_balance(self, refresh=False):
        return {"accounts": [], "total_credits": 100, "unread": 0}

    async def get_stats(self):
        return {"total_generations": 1}

    async def get_projects(self, cookies=None):
        return []


class TestApiKeyAuthentication(unittest.TestCase):
    """Authentication behaviour of the protected and public routes."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.key_file = Path(self.temp_dir) / "api_keys.json"

        self.fake_engine = FakeEngine()
        api.engine = self.fake_engine

        self.auth_file_patcher = patch.object(auth, "DEFAULT_KEY_FILE", self.key_file)
        self.auth_file_patcher.start()

        self.orig_env_key = os.environ.get("FLOW_API_KEY")
        if "FLOW_API_KEY" in os.environ:
            del os.environ["FLOW_API_KEY"]

        self.client = TestClient(api.app)

    def tearDown(self):
        self.auth_file_patcher.stop()
        if self.orig_env_key is not None:
            os.environ["FLOW_API_KEY"] = self.orig_env_key
        elif "FLOW_API_KEY" in os.environ:
            del os.environ["FLOW_API_KEY"]

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_unauthenticated_mode_when_no_keys_configured(self):
        """With no keys anywhere, requests pass for backwards compatibility."""
        self.assertFalse(is_auth_enforced(self.key_file))

        self.assertEqual(self.client.get("/health").status_code, 200)
        res = self.client.post("/api/v1/image", json={"prompt": "p", "aspect": "1:1"})
        self.assertEqual(res.status_code, 200)

    def test_auth_enforced_blocks_missing_key(self):
        """A stored key turns enforcement on and a bare request gets 401."""
        create_api_key(name="test-device", key_file=self.key_file)
        self.assertTrue(is_auth_enforced(self.key_file))

        res = self.client.post("/api/v1/image", json={"prompt": "p", "aspect": "1:1"})
        self.assertEqual(res.status_code, 401)
        self.assertIn("Missing API key", res.json()["detail"])

    def test_auth_blocks_invalid_key(self):
        """An unrecognised credential is refused."""
        create_api_key(name="test-device", key_file=self.key_file)

        res = self.client.post(
            "/api/v1/image",
            json={"prompt": "p", "aspect": "1:1"},
            headers={"Authorization": "Bearer sk-flow-invalid-token-12345"},
        )
        self.assertEqual(res.status_code, 401)
        self.assertIn("invalid or has been revoked", res.json()["detail"])

    def test_auth_succeeds_with_valid_bearer_token(self):
        """The OpenAI SDK's Authorization header authenticates."""
        entry = create_api_key(name="test-device", key_file=self.key_file)

        res = self.client.post(
            "/api/v1/image",
            json={"prompt": "p", "aspect": "1:1"},
            headers={"Authorization": f"Bearer {entry['key']}"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["job_id"], "test-job-1")

    def test_auth_succeeds_with_x_api_key_header(self):
        """The X-API-Key header authenticates too."""
        entry = create_api_key(name="test-device", key_file=self.key_file)

        res = self.client.get("/v1/balance", headers={"X-API-Key": entry["key"]})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total_credits"], 100)

    def test_env_var_flow_api_key_acts_as_master_key(self):
        """FLOW_API_KEY authenticates on its own, with no stored key."""
        os.environ["FLOW_API_KEY"] = "sk-flow-master-secret-key"
        self.assertTrue(is_auth_enforced(self.key_file))

        res = self.client.get(
            "/v1/balance", headers={"Authorization": "Bearer sk-flow-master-secret-key"}
        )
        self.assertEqual(res.status_code, 200)

    def test_revoked_key_is_rejected(self):
        """A revoked key stops working once another active key exists."""
        entry = create_api_key(name="temp-key", key_file=self.key_file)
        revoked = entry["key"]
        revoke_api_key("temp-key", key_file=self.key_file)

        # The only key is gone, so nothing is enforced and the request passes.
        self.assertEqual(
            self.client.get(
                "/v1/balance", headers={"Authorization": f"Bearer {revoked}"}
            ).status_code,
            200,
        )

        create_api_key(name="another-active-key", key_file=self.key_file)
        res = self.client.get("/v1/balance", headers={"Authorization": f"Bearer {revoked}"})
        self.assertEqual(res.status_code, 401)

    def test_public_endpoints_remain_accessible(self):
        """Health stays reachable while enforcement is on."""
        create_api_key(name="test-device", key_file=self.key_file)

        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "ok")

    def test_non_ascii_credential_is_rejected_not_crashed(self):
        """A credential outside ASCII yields 401, never an unhandled TypeError."""
        create_api_key(name="test-device", key_file=self.key_file)
        bearer = HTTPAuthorizationCredentials(scheme="Bearer", credentials=NON_ASCII_KEY)

        with self.assertRaises(HTTPException) as caught:
            verify_api_key(bearer=bearer, x_api_key=None)

        self.assertEqual(caught.exception.status_code, 401)

    def test_non_ascii_stored_key_does_not_break_verification(self):
        """A stored non-ASCII master key still refuses a wrong key with 401."""
        os.environ["FLOW_API_KEY"] = NON_ASCII_KEY

        res = self.client.get(
            "/v1/balance", headers={"Authorization": "Bearer sk-flow-plain-ascii-key"}
        )
        self.assertEqual(res.status_code, 401)

    def test_corrupt_store_fails_closed(self):
        """A store that is not JSON must never switch enforcement off."""
        create_api_key(name="test-device", key_file=self.key_file)
        self.key_file.write_text("{ not json at all", encoding="utf-8")

        self.assertTrue(is_auth_enforced(self.key_file))
        res = self.client.post("/api/v1/image", json={"prompt": "p", "aspect": "1:1"})
        self.assertEqual(res.status_code, 503)

    def test_store_without_keys_list_fails_closed(self):
        """A store whose shape is unrecognised fails closed as well."""
        self.key_file.write_text('{"version": 1}', encoding="utf-8")

        self.assertTrue(is_auth_enforced(self.key_file))
        self.assertEqual(self.client.get("/v1/balance").status_code, 503)

    def test_unreadable_store_fails_closed(self):
        """A store path that cannot be read as a file fails closed."""
        self.key_file.mkdir()  # the path exists, but read_text() cannot read it

        self.assertTrue(is_auth_enforced(self.key_file))
        self.assertEqual(self.client.get("/v1/balance").status_code, 503)

    def test_query_parameter_credential_is_rejected(self):
        """Credentials are read from headers only, never the query string."""
        entry = create_api_key(name="test-device", key_file=self.key_file)

        res = self.client.get(f"/v1/balance?api_key={entry['key']}")
        self.assertEqual(res.status_code, 401)

    def test_duplicate_alias_is_rejected(self):
        """Aliases identify a key, so a duplicate is refused."""
        create_api_key(name="laptop", key_file=self.key_file)

        with self.assertRaises(ValueError):
            create_api_key(name="laptop", key_file=self.key_file)

    def test_ambiguous_alias_target_is_rejected(self):
        """An alias shared by two hand-written entries names no single key."""
        self.key_file.write_text(
            json.dumps(
                {
                    "keys": [
                        {"key": "sk-flow-one", "name": "dup", "active": True},
                        {"key": "sk-flow-two", "name": "dup", "active": True},
                    ]
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            delete_api_key("dup", key_file=self.key_file)

        self.assertEqual(len(list_api_keys(self.key_file)), 2)
        self.assertTrue(delete_api_key("sk-flow-one", key_file=self.key_file))
        self.assertEqual(len(list_api_keys(self.key_file)), 1)

    def test_active_keys_lists_env_and_stored_keys(self):
        """Both credential sources are reported as active."""
        entry = create_api_key(name="test-device", key_file=self.key_file)
        os.environ["FLOW_API_KEY"] = "sk-flow-master-secret-key"

        active = get_active_keys(self.key_file)
        self.assertIn(entry["key"], active)
        self.assertIn("sk-flow-master-secret-key", active)


if __name__ == "__main__":
    unittest.main()
