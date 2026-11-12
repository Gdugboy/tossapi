"""공식 Sign in with ChatGPT: PKCE, 검증된 OIDC 신원, Windows DPAPI 저장.

다른 앱/Codex의 로그인 파일을 읽거나 가져오지 않는다. 이 앱의 승인을 별도로 받는다.
"""
from __future__ import annotations
import base64
from contextlib import contextmanager
import ctypes
from hashlib import sha256
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
import webbrowser
import requests

from trader_config import ROOT

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
USAGE_URL = "https://chatgpt.com/settings/usage"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE


class AuthError(RuntimeError):
    pass


def _dpapi(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise AuthError("이 버전의 인증정보 저장은 Windows 사용자 암호화(DPAPI)를 사용합니다.")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypto = ctypes.WinDLL("crypt32", use_last_error=True)
    free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    function = crypto.CryptUnprotectData if decrypt else crypto.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise AuthError("Windows 사용자 암호화 저장소를 사용할 수 없습니다.")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free(ctypes.cast(output.data, ctypes.c_void_p))


class CredentialStore:
    def __init__(self, folder: Path = ROOT / "data" / "chatgpt"):
        self.folder = Path(folder)
        self.file = self.folder / "profiles.dpapi"

    @contextmanager
    def locked(self):
        import msvcrt
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / "refresh.lock").open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + 40
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AuthError("다른 프로세스가 ChatGPT 로그인을 갱신 중입니다. 잠시 후 다시 시도하세요.")
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def read(self) -> dict:
        if not self.file.exists():
            return {"host_id": "urn:uuid:" + str(uuid4()), "active": None, "profiles": {}}
        try:
            return json.loads(_dpapi(self.file.read_bytes(), decrypt=True))
        except (ValueError, OSError) as exc:
            raise AuthError("ChatGPT 인증 저장소를 읽을 수 없습니다. 인증 값을 출력하지 않습니다.") from exc

    def write(self, data: dict):
        self.folder.mkdir(parents=True, exist_ok=True)
        encrypted = _dpapi(json.dumps(data, ensure_ascii=False).encode())
        temp = self.folder / ("credential-" + str(uuid4()) + ".tmp")
        try:
            with temp.open("xb") as file:
                os.chmod(temp, 0o600)
                file.write(encrypted)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, self.file)
        finally:
            if temp.exists():
                temp.unlink()


def prepare_attempt(host_id: str, registration: dict | None, redirect_uri: str) -> dict:
    verifier = secrets.token_urlsafe(48)
    nonce, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    client_id = registration["client_id"] if registration else "dynamic_agent_client"
    query = {"client_id": client_id, "ext_agent_host_id": host_id, "response_type": "code",
             "redirect_uri": redirect_uri, "scope": SCOPES, "resource": RESOURCE,
             "state": state, "nonce": nonce, "code_challenge_method": "S256", "code_challenge": challenge}
    if registration:
        if registration.get("id_token"):
            query["id_token_hint"] = registration["id_token"]
        if registration.get("email"):
            query["login_hint"] = registration["email"]
    else:
        query["agent_name_hint"] = "FOMO Paper Trader"
    return {"state": state, "nonce": nonce, "verifier": verifier, "redirect_uri": redirect_uri,
            "client_id": client_id, "registration": registration, "authorize_url": AUTHORIZE + "?" + urlencode(query)}


def validate_callback(params: dict, attempt: dict) -> tuple[str, str]:
    if any(len(v) != 1 for v in params.values()):
        raise AuthError("로그인 응답에 중복된 항목이 있습니다.")
    state = params.get("state", [""])[0]
    if not hmac.compare_digest(state, attempt["state"]):
        raise AuthError("로그인 요청과 응답이 일치하지 않습니다.")
    if params.get("error"):
        raise AuthError("ChatGPT 로그인이 취소되었거나 권한이 승인되지 않았습니다.")
    client_id = params.get("client_id", [attempt["client_id"]])[0]
    if attempt["registration"] and client_id != attempt["client_id"]:
        raise AuthError("선택한 계정과 다른 앱 등록 정보입니다.")
    if not client_id or client_id == "dynamic_agent_client":
        raise AuthError("발급된 앱 client_id가 없어 로그인을 완료할 수 없습니다.")
    code = params.get("code", [""])[0]
    if not code:
        raise AuthError("로그인 승인 코드가 없습니다.")
    return client_id, code


class ChatGPTAuth:
    def __init__(self, store=None, session=None):
        self.store = store or CredentialStore()
        self.session = session or requests.Session()

    def status(self) -> dict:
        if not self.store.file.exists():
            return {"connected": False, "plan_enabled": False, "profiles": [], "usage_url": USAGE_URL}
        with self.store.locked():
            data = self.store.read()
        active = data["profiles"].get(data.get("active"), {})
        return {"connected": bool(active.get("access_token")), "plan_enabled": bool(active.get("access_token") and PLAN_SCOPE in active.get("scopes", [])),
                "active_label": active.get("label"), "active_profile": data.get("active"), "model": active.get("selected_model"),
                "profiles": [{"profile": key, "label": p["label"], "active": key == data.get("active")} for key, p in data["profiles"].items()],
                "welcome_pending": bool(active and not active.get("welcome_acknowledged", False)), "usage_url": USAGE_URL}

    def _validate_id(self, id_token: str, client_id: str, nonce: str | None = None) -> dict:
        stage = "discovery"
        try:
            import jwt
            discovery = self.session.get(ISSUER + "/.well-known/openid-configuration", timeout=15)
            discovery.raise_for_status()
            metadata = discovery.json()
            jwks_uri = metadata["jwks_uri"]
            if metadata["issuer"] != ISSUER or urlsplit(jwks_uri).scheme != "https" or urlsplit(jwks_uri).netloc != "auth.openai.com":
                raise AuthError("OpenAI 신원 검증 서버가 올바르지 않습니다.")
            stage = "public_key"
            key = jwt.PyJWKClient(jwks_uri, timeout=15).get_signing_key_from_jwt(id_token)
            stage = "claims"
            claims = jwt.decode(id_token, key.key, algorithms=["RS256", "ES256"], audience=client_id,
                                issuer=ISSUER, leeway=5, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
            if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id:
                raise AuthError("여러 대상이 있는 ID 토큰의 승인 대상이 일치하지 않습니다.")
            if nonce is not None and not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
                raise AuthError("로그인 응답의 nonce가 일치하지 않습니다.")
            return claims
        except AuthError:
            raise
        except ImportError as exc:
            raise AuthError("로그인 검증 라이브러리가 없습니다. .venv의 Python으로 실행하세요.") from exc
        except Exception as exc:
            raise AuthError(f"OpenAI 로그인 신원 검증 실패 ({stage}: {type(exc).__name__}). 토큰 값은 기록하지 않습니다.") from exc

    def _token_request(self, body: dict) -> dict:
        try:
            response = self.session.post(TOKEN, data=body, timeout=(10, 30))
            if response.status_code != 200:
                raise AuthError(f"ChatGPT 인증 요청이 거절되었습니다 (HTTP {response.status_code}). 다시 로그인하세요.")
            result = response.json()
            if not result.get("access_token") or result.get("token_type", "").lower() != "bearer":
                raise AuthError("올바른 ChatGPT 접근 토큰을 받지 못했습니다.")
            return result
        except requests.RequestException as exc:
            raise AuthError("ChatGPT 인증 서버에 연결하지 못했습니다.") from exc

    @staticmethod
    def token_fields(result: dict, previous: dict | None = None) -> dict:
        previous = previous or {}
        scopes = result.get("scope", "").split()
        if PLAN_SCOPE not in scopes:
            raise AuthError("로그인은 되었지만 ChatGPT 요금제 사용 권한이 없습니다. 사용 권한을 승인해 주세요.")
        expires = int(result.get("expires_in", 0))
        if expires <= 0 or not result.get("refresh_token"):
            raise AuthError("토큰 유효기간이나 갱신 토큰이 없습니다. 다시 로그인하세요.")
        return {"access_token": result["access_token"], "refresh_token": result["refresh_token"],
                "id_token": result.get("id_token", previous.get("id_token")), "scopes": scopes,
                "expires_at": time.time() + expires, "saved_at": time.time(), "earliest_refresh_at": result.get("earliest_refresh_at", 0)}

    def finish_sign_in(self, attempt: dict, params: dict):
        client_id, code = validate_callback(params, attempt)
        # 발급된 client_id는 코드 교환 실패에도 다음 로그인에 재사용한다.
        with self.store.locked():
            data = self.store.read()
            data["pending_registration"] = {"client_id": client_id, "host_id": data["host_id"]}
            self.store.write(data)
        result = self._token_request({"grant_type": "authorization_code", "client_id": client_id,
                                      "code": code, "code_verifier": attempt["verifier"],
                                      "redirect_uri": attempt["redirect_uri"], "resource": RESOURCE})
        claims = self._validate_id(result.get("id_token", ""), client_id, attempt["nonce"])
        prior = attempt["registration"] or {}
        if prior.get("subject") and prior["subject"] != claims["sub"]:
            raise AuthError("선택한 계정과 다른 사용자입니다. 새 계정 추가로 로그인하세요.")
        fields = self.token_fields(result, prior)
        profile_id = sha256((client_id + "|" + claims["sub"]).encode()).hexdigest()[:16]
        with self.store.locked():
            data = self.store.read()
            old = data["profiles"].get(profile_id, {})
            data["profiles"][profile_id] = {**old, **fields, "client_id": client_id, "subject": claims["sub"],
                                            "email": claims.get("email"), "issuer": ISSUER,
                                            "label": old.get("label", "ChatGPT 계정 " + str(len(data["profiles"]) + 1))}
            data["active"] = profile_id
            data.pop("pending_registration", None)
            self.store.write(data)

    def sign_in(self, *, timeout=600, new_account=False, profile=None, on_ready=None):
        with self.store.locked():
            data = self.store.read()
            self.store.write(data)  # 최초 host_id를 브라우저를 열기 전에 영구 저장한다.
        registration = None if new_account else data["profiles"].get(profile or data.get("active"))
        if not registration and not new_account:
            registration = data.get("pending_registration")
        result = {"done": False, "error": None}
        auth = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # callback URL의 코드/힌트를 로그에 쓰지 않는다.

            def page(self, text, status=200):
                body = ("<!doctype html><html lang='ko'><meta charset='utf-8'><meta name='referrer' content='no-referrer'>"
                        "<title>FOMO · ChatGPT 연결</title><body style='font:18px sans-serif;max-width:680px;margin:80px auto'>" + text + "</body></html>").encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                target = urlsplit(self.path)
                if target.path == "/":
                    return self.page("<h1>FOMO Paper Trader</h1><p>ChatGPT 요금제로 AI 분석을 연결합니다. 실제 매매 주문 기능은 없습니다.</p>"
                                     "<p><a href='/login' style='display:inline-block;background:#000;color:#fff;padding:14px 22px;border-radius:8px;text-decoration:none'>Continue with ChatGPT</a></p>")
                if target.path == "/login":
                    self.send_response(302)
                    self.send_header("Location", attempt["authorize_url"])
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                if target.path != "/auth/callback" or result["done"]:
                    return self.page("페이지가 없습니다.", 404)
                params = parse_qs(target.query, keep_blank_values=True)
                try:
                    # 잘못된 state 요청은 승인 코드를 교환하지 않고 계속 대기한다.
                    validate_callback(params, attempt)
                except AuthError as exc:
                    if hmac.compare_digest(params.get("state", [""])[0], attempt["state"]):
                        result.update(done=True, error=str(exc))
                    return self.page("로그인 요청을 완료하지 못했습니다. 앱에서 다시 시도하세요.", 400)
                result["done"] = True
                try:
                    auth.finish_sign_in(attempt, params)
                    self.page("<h1>You're using your ChatGPT plan</h1><p>연결되었습니다. 이 창을 닫고 앱으로 돌아가세요.</p>"
                              "<p>AI 요청은 ChatGPT 구독의 사용 한도를 이용합니다.</p><a href='https://chatgpt.com/settings/usage'>Manage usage</a>")
                except Exception as exc:
                    result["error"] = str(exc) if isinstance(exc, AuthError) else "로그인 처리 중 오류가 발생했습니다."
                    self.page("연결을 완료하지 못했습니다. 앱의 상태를 확인하세요.", 400)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        server.timeout = 1
        redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
        attempt = prepare_attempt(data["host_id"], registration, redirect)
        landing = f"http://127.0.0.1:{server.server_port}/"
        try:
            print("Continue with ChatGPT: " + landing, flush=True)
            if on_ready:
                on_ready(landing)
            else:
                webbrowser.open(landing)
            deadline = time.monotonic() + timeout
            while not result["done"] and time.monotonic() < deadline:
                server.handle_request()
            if not result["done"]:
                raise AuthError("로그인 대기 시간이 만료되었습니다. 다시 연결하세요.")
            if result["error"]:
                raise AuthError(result["error"])
        finally:
            server.server_close()
        return self.status()

    def access_token(self) -> str:
        with self.store.locked():
            data = self.store.read()
            profile = data["profiles"].get(data.get("active"), {})
            if not profile.get("access_token") or PLAN_SCOPE not in profile.get("scopes", []):
                raise AuthError("Continue with ChatGPT로 먼저 연결하고 요금제 사용 권한을 승인하세요.")
            if profile["expires_at"] > time.time() + 60:
                return profile["access_token"]
            result = self._token_request({"grant_type": "refresh_token", "client_id": profile["client_id"],
                                          "refresh_token": profile["refresh_token"], "resource": RESOURCE})
            if result.get("id_token"):
                claims = self._validate_id(result["id_token"], profile["client_id"])
                if claims["sub"] != profile["subject"]:
                    raise AuthError("갱신된 로그인 계정이 기존 계정과 다릅니다.")
            profile.update(self.token_fields(result, profile))
            self.store.write(data)
            return profile["access_token"]

    def models(self) -> list[dict]:
        response = self.session.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + self.access_token()}, timeout=30)
        if response.status_code != 200:
            raise AuthError(f"ChatGPT 모델 목록 조회 실패 (HTTP {response.status_code}).")
        return [{"slug": x["slug"], "display_name": x.get("display_name", x["slug"])}
                for x in response.json().get("models", []) if x.get("visibility") == "list" and x.get("slug")]

    def select_model(self, requested="auto") -> str:
        models = self.models()
        slugs = [m["slug"] for m in models]
        if not slugs:
            raise AuthError("이 ChatGPT 계정에서 사용 가능한 모델이 없습니다.")
        if requested != "auto" and requested not in slugs:
            raise AuthError("설정한 모델은 이 ChatGPT 계정에 없습니다. auth-models로 확인하세요.")
        selected = requested if requested != "auto" else "gpt-6.1-sol" if "gpt-6.1-sol" in slugs else slugs[0]
        with self.store.locked():
            data = self.store.read()
            data["profiles"][data["active"]]["selected_model"] = selected
            self.store.write(data)
        return selected

    def acknowledge(self):
        with self.store.locked():
            data = self.store.read()
            if data.get("active"):
                data["profiles"][data["active"]]["welcome_acknowledged"] = True
                self.store.write(data)

    def sign_out(self):
        confirmed = False
        with self.store.locked():
            data = self.store.read()
            profile = data["profiles"].get(data.get("active"), {})
            if profile.get("refresh_token"):
                try:
                    metadata = self.session.get(ISSUER + "/.well-known/openid-configuration", timeout=10).json()
                    endpoint = metadata["revocation_endpoint"]
                    if urlsplit(endpoint).scheme != "https" or urlsplit(endpoint).netloc != "auth.openai.com":
                        raise AuthError("해제 서버가 올바르지 않습니다.")
                    for delay in (0, 1, 2):
                        if delay:
                            time.sleep(delay)
                        response = self.session.post(endpoint, data={"token": profile["refresh_token"], "token_type_hint": "refresh_token", "client_id": profile["client_id"]}, timeout=10)
                        if response.status_code == 200:
                            confirmed = True
                            break
                        if response.status_code < 500:
                            break
                except (requests.RequestException, ValueError, KeyError, AuthError):
                    pass
            for key in ("access_token", "refresh_token", "id_token", "scopes", "expires_at"):
                profile.pop(key, None)
            self.store.write(data)
        return {"local_signed_out": True, "remote_revocation_confirmed": confirmed, "usage_url": USAGE_URL}
