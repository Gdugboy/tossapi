"""합성 OAuth·SSE 검증. 실제 사용자 자격/네트워크를 사용하지 않는다."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from chatgpt_auth import AuthError, ChatGPTAuth, CredentialStore, PLAN_SCOPE, prepare_attempt, validate_callback
from openai_engine import OpenAIEngine
from trader_config import Settings


class AuthTests(unittest.TestCase):
    def test_pkce_and_new_registration(self):
        a = prepare_attempt("urn:uuid:fixture", None, "http://127.0.0.1:1234/auth/callback")
        b = prepare_attempt("urn:uuid:fixture", None, "http://127.0.0.1:1234/auth/callback")
        q = parse_qs(urlsplit(a["authorize_url"]).query)
        self.assertEqual(q["client_id"], ["dynamic_agent_client"])
        self.assertEqual(q["agent_name_hint"], ["FOMO Paper Trader"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertNotIn(a["verifier"], a["authorize_url"])
        self.assertNotEqual(a["state"], b["state"])
        self.assertNotEqual(a["nonce"], b["nonce"])

    def test_returning_registration_bound_to_account(self):
        registration = {"client_id": "oaiapp_test", "id_token": "DUMMY_ID_TOKEN", "email": "fixture@example.test"}
        a = prepare_attempt("urn:uuid:fixture", registration, "http://127.0.0.1:1234/auth/callback")
        q = parse_qs(urlsplit(a["authorize_url"]).query)
        self.assertNotIn("agent_name_hint", q)
        self.assertEqual(q["client_id"], ["oaiapp_test"])
        with self.assertRaises(AuthError):
            validate_callback({"state": [a["state"]], "code": ["dummy"], "client_id": ["wrong-client"]}, a)

    def test_state_denial_missing_client_and_duplicate_rejected(self):
        a = prepare_attempt("urn:uuid:fixture", None, "http://127.0.0.1:1234/auth/callback")
        bad = [{"state": ["wrong"], "code": ["dummy"], "client_id": ["oaiapp_test"]},
               {"state": [a["state"]], "error": ["access_denied"]},
               {"state": [a["state"]], "code": ["dummy"]},
               {"state": [a["state"], a["state"]], "code": ["dummy"], "client_id": ["oaiapp_test"]}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(AuthError):
                validate_callback(value, a)
        self.assertEqual(validate_callback({"state": [a["state"]], "code": ["dummy"], "client_id": ["oaiapp_test"]}, a), ("oaiapp_test", "dummy"))

    def test_identity_only_is_not_plan_permission(self):
        with self.assertRaises(AuthError):
            ChatGPTAuth.token_fields({"scope": "openid profile email", "expires_in": 3600, "access_token": "DUMMY", "refresh_token": "DUMMY_REFRESH"})

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_credential_file_encrypted_and_status_redacted(self):
        with TemporaryDirectory() as folder:
            store = CredentialStore(Path(folder))
            with store.locked():
                data = store.read()
                data["active"] = "fixture"
                data["profiles"]["fixture"] = {"label": "test", "access_token": "DUMMY_ACCESS_SECRET", "refresh_token": "DUMMY_REFRESH_SECRET", "scopes": [PLAN_SCOPE]}
                store.write(data)
            self.assertNotIn(b"DUMMY_ACCESS_SECRET", store.file.read_bytes())
            self.assertNotIn(b"DUMMY_REFRESH_SECRET", store.file.read_bytes())
            with store.locked():
                self.assertEqual(store.read()["profiles"]["fixture"]["access_token"], "DUMMY_ACCESS_SECRET")
            status = ChatGPTAuth(store=store).status()
            self.assertTrue(status["plan_enabled"])
            self.assertNotIn("DUMMY_ACCESS_SECRET", json.dumps(status))
            self.assertNotIn("refresh_token", status)

    def test_id_token_signature_and_nonce_checked(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {"iss": "https://auth.openai.com", "aud": "oaiapp_test", "sub": "fixture", "iat": int(time.time()), "exp": int(time.time()) + 60, "nonce": "expected"}
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture"})
        auth = ChatGPTAuth(session=Mock())
        auth.session.get.return_value.json.return_value = {"issuer": "https://auth.openai.com", "jwks_uri": "https://auth.openai.com/.well-known/jwks.json"}
        with patch("jwt.PyJWKClient") as loader:
            loader.return_value.get_signing_key_from_jwt.return_value.key = key.public_key()
            self.assertEqual(auth._validate_id(token, "oaiapp_test", "expected")["sub"], "fixture")
            slightly_ahead = jwt.encode({**claims, "iat": int(time.time()) + 3}, key, algorithm="RS256")
            self.assertEqual(auth._validate_id(slightly_ahead, "oaiapp_test", "expected")["sub"], "fixture")
            with self.assertRaises(AuthError):
                auth._validate_id(token, "oaiapp_test", "wrong")
            with self.assertRaises(AuthError):
                auth._validate_id(token, "wrong-audience", "expected")
            expired = jwt.encode({**claims, "exp": 1}, key, algorithm="RS256")
            with self.assertRaises(AuthError):
                auth._validate_id(expired, "oaiapp_test", "expected")


class PlanTransportTests(unittest.TestCase):
    @staticmethod
    def streamed(events):
        response = Mock(status_code=200)
        lines = []
        for event in events:
            lines.extend(["data: " + json.dumps(event), ""])
        response.iter_lines.return_value = iter(lines)
        return response

    def test_plan_transport_omits_unsupported_fields(self):
        http, auth = Mock(), Mock()
        auth.status.return_value = {"plan_enabled": True, "active_profile": "fixture"}
        auth.select_model.return_value = "account-model"
        auth.access_token.return_value = "DUMMY_PLAN_TOKEN"
        http.post.return_value = self.streamed([{"type": "response.completed", "response": {"status": "completed", "id": "fixture", "model": "account-model", "output": [{"content": [{"type": "output_text", "text": "ok"}]}]}}])
        ai = OpenAIEngine(Settings(), session=http, auth=auth)
        result = ai._request("fixture", {"a": 1}, search=True)
        payload = http.post.call_args.kwargs["json"]
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["store"])
        self.assertIsInstance(payload["input"], list)
        self.assertNotIn("max_output_tokens", payload)
        self.assertEqual(payload["model"], "account-model")
        self.assertEqual(ai.text(result), "ok")
        self.assertEqual(ai.usage[0]["auth_mode"], "chatgpt")
        http.post.return_value.close.assert_called_once()

    def test_truncated_or_failed_stream_not_success(self):
        cases = [[{"type": "response.output_text.delta", "delta": "partial"}],
                 [{"type": "response.incomplete", "response": {"status": "incomplete"}}],
                 [{"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}]]
        for events in cases:
            with self.subTest(events=events), self.assertRaises(RuntimeError):
                OpenAIEngine._read_stream(self.streamed(events))

    def test_plan_failure_never_falls_back_to_paid_api(self):
        http, auth = Mock(), Mock()
        auth.status.return_value = {"plan_enabled": True, "active_profile": "fixture"}
        auth.select_model.return_value = "account-model"
        auth.access_token.return_value = "DUMMY_PLAN_TOKEN"
        http.post.return_value = Mock(status_code=429)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "DUMMY_PAID_API_KEY"}):
            ai = OpenAIEngine(Settings(), session=http, auth=auth)
            with self.assertRaises(RuntimeError):
                ai._request("fixture", {})
            self.assertEqual(http.post.call_count, 1)
            self.assertEqual(http.post.call_args.kwargs["headers"]["Authorization"], "Bearer DUMMY_PLAN_TOKEN")

    def test_missing_login_never_requests_even_if_api_key_exists(self):
        http, auth = Mock(), Mock()
        auth.status.return_value = {"plan_enabled": False}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "DUMMY_PAID_API_KEY"}):
            ai = OpenAIEngine(Settings(), session=http, auth=auth)
            with self.assertRaises(RuntimeError):
                ai._request("fixture", {})
            http.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
