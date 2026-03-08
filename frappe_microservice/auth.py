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
                    'Accept': 'application/json'
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

    def _validate_session(self):
        """
        Determine the current user from this request. If Authorization: Bearer
        is present, validate via _validate_oauth_token. Otherwise look for sid
        cookie and call Central Site get_logged_user; on success set frappe
        user and sid and return (username, None). On any failure clear session
        and return (None, 401 response). Used by secure_route before running the view.
        """
        try:
            auth_header = request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                access_token = auth_header[7:]
                return self._validate_oauth_token(access_token)

            session_cookies = request.cookies
            self.logger.debug(
                f"Session validation - cookies: {dict(session_cookies)}")

            sid = session_cookies.get('sid')

            if not sid or sid == 'Guest':
                self.logger.info(
                    "Session validation - no valid sid or token, rejecting")
                return None, self._json_error_response({
                    "status": "error",
                    "message": "Authentication required. Please provide a Bearer token or login at Central Site.",
                    "type": "Unauthorized",
                    "code": 401
                }, 401)

            try:
                response = http_requests.get(
                    f'{self.central_site_url}/api/method/frappe.auth.get_logged_user',
                    cookies=session_cookies,
                    timeout=5,
                    headers={'Accept': 'application/json'}
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
                            "Session validation - user is Guest or invalid")
                else:
                    self.logger.info(
                        f"Session validation - Central Site returned {response.status_code}")

            except http_requests.exceptions.RequestException as api_error:
                self.logger.error(
                    f"Session validation - API call failed: {api_error}")
            except Exception as api_error:
                self.logger.error(
                    f"Session validation - API response error: {api_error}")

            if hasattr(frappe, 'session'):
                frappe.session.user = 'Guest'
                frappe.session.sid = None
                self.logger.debug("Cleared invalid session context")

            return None, self._json_error_response({
                "status": "error",
                "message": f"Authentication required. Please login at Central Site: {self.central_site_url}/api/method/login",
                "type": "Unauthorized",
                "code": 401
            }, 401)

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
