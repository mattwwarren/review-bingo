"""Cross-site scripting (XSS) security tests.

Verifies that user input is properly sanitized/escaped to prevent
XSS attacks in API responses and stored data.
"""

from http import HTTPStatus

import pytest
from httpx import AsyncClient


class TestXSSUserFields:
    """XSS injection via user name/email fields."""

    @pytest.mark.asyncio
    async def test_xss_script_tag_in_user_name(self, client: AsyncClient) -> None:
        """Verify script tags in user names are properly handled."""
        script_payload = "<script>alert('XSS')</script>"
        response = await client.post(
            "/users",
            json={
                "name": script_payload,
                "email": "xss-test@example.com",
            },
        )

        # Verify Content-Type is JSON (not HTML)
        assert response.headers.get("content-type") == "application/json"

        # Should either reject or store as literal string
        if response.status_code == HTTPStatus.CREATED:
            user = response.json()
            # Payload stored as literal, not executed
            assert user["name"] == script_payload
            # JSON response is safe (browsers won't execute <script> in JSON)
            # The key is that Content-Type is application/json, not text/html
        else:
            # Validation rejected it
            assert response.status_code in (
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )

    @pytest.mark.asyncio
    async def test_xss_event_handler_in_user_name(self, client: AsyncClient) -> None:
        """Verify event handlers in user names are properly handled."""
        event_handler_payload = '<img src=x onerror="alert(1)">'
        response = await client.post(
            "/users",
            json={
                "name": event_handler_payload,
                "email": "event-test@example.com",
            },
        )

        # Verify Content-Type is JSON
        assert response.headers.get("content-type") == "application/json"

        if response.status_code == HTTPStatus.CREATED:
            user = response.json()
            assert user["name"] == event_handler_payload
        else:
            assert response.status_code in (
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )

    @pytest.mark.asyncio
    async def test_xss_html_entity_encoding(self, client: AsyncClient) -> None:
        """Verify HTML entity encoded XSS is properly handled."""
        entity_payload = "&#60;script&#62;alert('XSS')&#60;/script&#62;"
        response = await client.post(
            "/users",
            json={
                "name": entity_payload,
                "email": "entity-test@example.com",
            },
        )

        if response.status_code == HTTPStatus.CREATED:
            user = response.json()
            # Stored as literal string (entities not decoded)
            assert user["name"] == entity_payload
            # Verify response is JSON, not HTML that would decode entities
            assert response.headers.get("content-type") == "application/json"
        else:
            assert response.status_code in (
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )

    @pytest.mark.asyncio
    async def test_xss_iframe_injection(self, client: AsyncClient) -> None:
        """Verify iframe injection in user names is properly handled."""
        iframe_payload = "<iframe src='javascript:alert(1)'>"
        response = await client.post(
            "/users",
            json={
                "name": iframe_payload,
                "email": "iframe-test@example.com",
            },
        )

        if response.status_code == HTTPStatus.CREATED:
            user = response.json()
            assert user["name"] == iframe_payload
            # Verify JSON response (iframes only execute in HTML context)
            assert response.headers.get("content-type") == "application/json"
        else:
            assert response.status_code in (
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )


class TestXSSAdvanced:
    """Advanced XSS attack vectors."""

    @pytest.mark.asyncio
    async def test_xss_unicode_bypass_attempt(self, client: AsyncClient) -> None:
        """Verify unicode-based XSS bypass attempts are prevented."""
        # Unicode variations of <script>
        unicode_payloads = [
            "\u003cscript\u003ealert(1)\u003c/script\u003e",
            "＜script＞alert(1)＜/script＞",  # Full-width characters  # noqa: RUF001
        ]

        for payload in unicode_payloads:
            response = await client.post(
                "/users",
                json={
                    "name": payload,
                    "email": f"unicode{hash(payload)}@example.com",
                },
            )

            if response.status_code == HTTPStatus.CREATED:
                user = response.json()
                # Stored as literal (not normalized to <script>)
                assert user["name"] == payload
                # Response is JSON
                assert response.headers.get("content-type") == "application/json"
            else:
                assert response.status_code in (
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )

    @pytest.mark.asyncio
    async def test_xss_double_encoding(self, client: AsyncClient) -> None:
        """Verify double-encoded XSS is prevented."""
        # Double URL encoding: %253Cscript%253E = %3Cscript%3E = <script>
        double_encoded = "%253Cscript%253Ealert(1)%253C/script%253E"
        response = await client.post(
            "/users",
            json={
                "name": double_encoded,
                "email": "doubleenc@example.com",
            },
        )

        if response.status_code == HTTPStatus.CREATED:
            user = response.json()
            # Should NOT be decoded to <script>
            assert user["name"] == double_encoded
            assert "<script>" not in user["name"]
            # Verify JSON response
            assert response.headers.get("content-type") == "application/json"
        else:
            assert response.status_code in (
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.UNPROCESSABLE_ENTITY,
            )
