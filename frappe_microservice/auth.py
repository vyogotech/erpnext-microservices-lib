"""
Authentication mixin -- OAuth2 and SID session validation.

This mixin provides session validation for MicroserviceApp. All validation is
delegated to the Central Site so the microservice does not store passwords.
- _validate_oauth_token(access_token): Calls Central Site OIDC profile endpoint
  with Bearer token; on success sets frappe user and returns (username, None).
  On failure returns (None, error_response_tuple).
- _validate_session(): First checks for Authorization: Bearer and validates via
  OAuth; otherwise looks for sid cookie and validates via Central Site
  get_logged_user. On success sets frappe session and returns (username, None);
  on failure clears session and returns (None, 401 response tuple).
"""

import frappe
import requests as http_requests
from flask import request


class AuthMixin:
    """
    Mixin for MicroserviceApp that provides authentication.

    Expects the host class to have:
        self.logger: logging.Logger
        self.central_site_url: str
        self._json_error_response(payload, status_code): method
    """

    def _validate_oauth_token(self, access_token: str):
        """
        Validate the Bearer token by calling the Central Site's OIDC profile
        endpoint. On 200, extract username from message.email or message.sub,
        set frappe user/session, and return (username, None). Otherwise return
        (None, _json_error_response(...)) for 401. Any exception (network, etc.)
        also returns (None, 401 error response).
        """
        try:
            response = http_requests.get(
                f'{self.central_site_url}/api/method/frappe.integrations.oauth2.openid_profile',
                headers={
                    'Authorization': f'Bearer {access_token}',
                    'Accept': 'application/json',
                    'Host': getattr(self, 'frappe_site', '') or 'dev.localhost',
                },
                timeout=5
            )

            if response.status_code == 200:
                user_info = response.json().get('message', {})
                username = user_info.get('email') or user_info.get('sub')

                if username:
                    self.logger.info(f"OAuth2 validation successful for user: {username}")

                    frappe.set_user(username)
                    if hasattr(frappe, 'session'):
                        frappe.session.user = username

                    return username, None

            self.logger.warning(f"OAuth2 validation failed with status: {response.status_code}")
            return None, self._json_error_response({
                "status": "error",
                "message": "Invalid or expired OAuth2 token",
                "type": "Unauthorized",
                "code": 401
            }, 401)

        except Exception as e:
            self.logger.error(f"OAuth2 validation error: {e}")
            return None, self._json_error_response({
                "status": "error",
                "message": "Authentication service error",
                "type": "AuthenticationError",
                "code": 401
            }, 401)

    def _validate_session_via_db(self, sid):
        """
        Fallback: validate SID directly against the shared tabSessions table.

        Used when the Central Site call fails (different Redis, Host header issue,
        or network error). Since all microservices share the same MariaDB, the
        session created by auth-service is always visible here.

        Returns (username, None) on success or (None, error_response) on failure.
        """
        try:
            from frappe.utils import now, time_diff_in_seconds
            from frappe.utils.data import add_to_date

            # Default session expiry: 240 hours (Frappe default)
            session_expiry = "240:00:00"
            try:
                session_expiry = frappe.db.get_single_value("System Settings", "session_expiry") or session_expiry
            except Exception:
                pass

            parts = session_expiry.split(":")
            expiry_seconds = (int(parts[0]) * 3600) + (int(parts[1]) * 60) + int(parts[2] if len(parts) > 2 else 0)
            expired_threshold = add_to_date(now(), seconds=-expiry_seconds, as_string=True)

            Sessions = frappe.qb.DocType("Sessions")
            result = (
                frappe.qb.from_(Sessions)
                .select(Sessions.user, Sessions.sessiondata)
                .where(Sessions.sid == sid)
                .where(Sessions.lastupdate > expired_threshold)
            ).run(as_dict=True)

            if not result:
                self.logger.info(f"DB session fallback - SID not found or expired: {sid[:8]}...")
                return None, self._json_error_response({
                    "status": "error",
                    "message": "Session expired or invalid.",
                    "type": "Unauthorized",
                    "code": 401
                }, 401)

            username = result[0].get("user")
            if not username or username == "Guest":
                return None, self._json_error_response({
                    "status": "error",
                    "message": "Session expired or invalid.",
                    "type": "Unauthorized",
                    "code": 401
                }, 401)

            self.logger.info(f"DB session fallback - validated user: {username}")
            frappe.set_user(username)
            frappe.session.sid = sid
            if hasattr(frappe, 'local') and hasattr(frappe.local, 'session'):
                frappe.local.session.data = frappe._dict()

            return username, None

        except Exception as e:
            self.logger.error(f"DB session fallback failed: {e}", exc_info=True)
            return None, self._json_error_response({
                "status": "error",
                "message": "Authentication service error. Please try again later.",
                "type": "AuthenticationError",
                "code": 401
            }, 401)

    def _validate_session(self):
        """
        Determine the current user from this request.

        Auth priority:
          1. X-Internal-Token header matching INTERNAL_SERVICE_TOKEN env var
             → treats caller as Administrator (service-to-service, no user session).
          2. Authorization: Bearer <token> → validated via Central Site OIDC.
          3. sid cookie → validated via Central Site get_logged_user.
          4. sid cookie → fallback: direct DB lookup in shared tabSessions.

        Returns (username, None) on success or (None, error_response) on failure.
        """
        import os

        try:
            # ── 1. Internal service token (service-to-service bypass) ────────────
            internal_token = os.getenv('INTERNAL_SERVICE_TOKEN')
            if internal_token:
                req_token = request.headers.get('X-Internal-Token', '')
                if req_token and req_token == internal_token:
                    username = 'Administrator'
                    self.logger.info(
                        "Internal service token validated — treating caller as Administrator"
                    )
                    frappe.set_user(username)
                    if hasattr(frappe, 'session'):
                        frappe.session.user = username
                        frappe.session.sid = 'internal'
                    return username, None

            # ── 2. OAuth2 Bearer token ────────────────────────────────────────────
            auth_header = request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                access_token = auth_header[7:]
                return self._validate_oauth_token(access_token)

            session_cookies = request.cookies
            self.logger.debug(
                f"Session validation - cookies: {dict(session_cookies)}")

            # React Native fetch often cannot send Cookie; clients may send X-Frappe-SID instead.
            sid = session_cookies.get('sid') or (
                request.headers.get('X-Frappe-SID') or ''
            ).strip() or None

            if not sid or sid == 'Guest':
                self.logger.info(
                    "Session validation - no valid sid or token, rejecting")
                return None, self._json_error_response({
                    "status": "error",
                    "message": "Authentication required. Please provide a Bearer token or login at Central Site.",
                    "type": "Unauthorized",
                    "code": 401
                }, 401)

            # ── 3. SID cookie → Central Site validation ───────────────────────────
            try:
                response = http_requests.get(
                    f'{self.central_site_url}/api/method/frappe.auth.get_logged_user',
                    cookies=session_cookies,
                    timeout=5,
                    headers={
                        'Accept': 'application/json',
                        'Host': getattr(self, 'frappe_site', '') or 'dev.localhost',
                    }
                )

                self.logger.debug(
                    f"Session validation - Central Site response: {response.status_code}")

                if response.status_code == 200:
                    user_info = response.json()
                    username = user_info.get('message')

                    if username and username != 'Guest':
                        self.logger.info(
                            f"Session validation - valid user: {username}")

                        frappe.set_user(username)
                        frappe.session.sid = sid

                        if hasattr(frappe, 'local') and hasattr(frappe.local, 'session'):
                            frappe.local.session.data = frappe._dict()

                        self.logger.debug(
                            f"Session context set: user={username}, sid={sid}")
                        return username, None
                    else:
                        self.logger.info(
                            "Session validation - Central Site returned Guest, trying DB fallback")
                else:
                    self.logger.info(
                        f"Session validation - Central Site returned {response.status_code}, trying DB fallback")

            except http_requests.exceptions.RequestException as api_error:
                self.logger.warning(
                    f"Session validation - Central Site unreachable: {api_error}, trying DB fallback")
            except Exception as api_error:
                self.logger.warning(
                    f"Session validation - Central Site error: {api_error}, trying DB fallback")

            # ── 4. Fallback: direct DB session lookup ─────────────────────────────
            self.logger.info("Session validation - attempting direct DB fallback")
            return self._validate_session_via_db(sid)

        except Exception as e:
            self.logger.error(f"Session validation error: {e}", exc_info=True)

            if hasattr(frappe, 'session'):
                frappe.session.user = 'Guest'
                frappe.session.sid = None
                self.logger.debug(
                    "Cleared session context after validation error")

            return None, self._json_error_response({
                "status": "error",
                "message": "Authentication service error. Please try again later.",
                "type": "AuthenticationError",
                "code": 401
            }, 401)
