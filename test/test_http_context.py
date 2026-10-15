"""Offline HTTP boundary checks; fixtures contain only synthetic public data."""

import json
import unittest
from unittest.mock import Mock, patch

import requests

from instaloader.exceptions import (AbortDownloadException, BadResponseException, ConnectionException,
                                    LoginRequiredException, QueryReturnedForbiddenException,
                                    QueryReturnedNotFoundException, QueryReturnedUnauthorizedException,
                                    TooManyRequestsException)
from instaloader.instaloadercontext import InstaloaderContext


def response(status=200, body='{"status":"ok"}', headers=None):
    result = requests.Response()
    result.status_code = status
    result.reason = {200: "OK", 302: "Found", 401: "Unauthorized", 403: "Forbidden",
                     404: "Not Found", 429: "Too Many Requests", 503: "Service Unavailable"}.get(status, "Error")
    result._content = body.encode("utf-8")
    result.headers.update(headers or {})
    return result


class Transport:
    """Replace only the network send; keep requests' session/preparation behavior."""

    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def send(self, session, request, **kwargs):
        self.requests.append(request)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        reply.url = request.url
        reply.request = request
        return reply

    def patch(self):
        return patch.object(requests.Session, "send", lambda session, request, **kwargs: self.send(session, request, **kwargs))


class TestJSONHTTPContext(unittest.TestCase):
    def test_challenge_redirect_retains_explicit_block_reason_for_callers(self):
        transport = Transport(response(302, headers={'location': '/challenge/fixture/'}))
        with transport.patch(), self.assertRaises(AbortDownloadException) as caught:
            self.context.get_json('api/public/', {})
        self.assertEqual(getattr(caught.exception, 'block_reason', None), 'challenge_required')
        self.assertIn('challenge', str(caught.exception))
        self.assertEqual(len(transport.requests), 1)


    def setUp(self):
        self.context = InstaloaderContext(sleep=False, quiet=True, max_connection_attempts=3)
        self.context._rate_controller = Mock()
        self.context.error = Mock()
        self.addCleanup(self.context.close)

    def test_forbidden_and_missing_are_typed_terminal_errors_with_status(self):
        for status, error in ((403, QueryReturnedForbiddenException), (404, QueryReturnedNotFoundException)):
            with self.subTest(status=status):
                transport = Transport(*(response(status) for _ in range(3)))
                with transport.patch(), self.assertRaises(error) as caught:
                    self.context.get_json("api/public/", {"token": "fixture-secret"})
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(caught.exception.status_code, status)
                self.assertNotIn("fixture-secret", str(caught.exception))

    def test_non_object_unauthorized_body_is_still_terminal_unauthorized(self):
        transport = Transport(response(401, "[]"))
        with transport.patch(), self.assertRaises(QueryReturnedUnauthorizedException) as caught:
            self.context.get_json("api/public/", {})
        self.assertEqual(caught.exception.status_code, 401)
        self.assertEqual(len(transport.requests), 1)

    def test_invalid_json_and_non_object_json_are_terminal_bad_responses(self):
        for body in ("<html>restricted</html>", "[]", "null", '"unexpected"'):
            with self.subTest(body=body):
                transport = Transport(*(response(body=body) for _ in range(3)))
                with transport.patch(), self.assertRaises(BadResponseException):
                    self.context.get_json("api/public/", {})
                self.assertEqual(len(transport.requests), 1)

    def test_challenge_response_stops_without_retry(self):
        transport = Transport(*(response(body='{"status":"fail","message":"challenge_required"}')
                                for _ in range(3)))
        with transport.patch(), self.assertRaises(AbortDownloadException):
            self.context.get_json("api/public/", {})
        self.assertEqual(len(transport.requests), 1)

    def test_rate_limit_honors_retry_after_and_preserves_last_status(self):
        transport = Transport(*(response(429, headers={"Retry-After": "7"}) for _ in range(3)))
        with transport.patch(), self.assertRaises(TooManyRequestsException) as caught:
            self.context.get_json("api/public/", {"token": "fixture-secret"})
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after, 7.0)
        self.assertEqual(self.context._rate_controller.sleep.call_count, 2)
        self.context._rate_controller.sleep.assert_called_with(7.0)
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_rate_limit_honors_http_date_retry_after(self):
        transport = Transport(response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}), response())
        with patch("instaloader.instaloadercontext.time.time", return_value=1445412470), transport.patch():
            self.assertEqual(self.context.get_json("api/public/", {}), {"status": "ok"})
        self.context._rate_controller.sleep.assert_called_once_with(10.0)

    def test_server_failure_is_bounded_and_preserves_last_status(self):
        transport = Transport(*(response(503) for _ in range(3)))
        with transport.patch(), self.assertRaises(ConnectionException) as caught:
            self.context.get_json("api/public/", {})
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(caught.exception.status_code, 503)

    def test_explicit_unlimited_network_attempts_remains_compatible(self):
        self.context.max_connection_attempts = 0
        transport = Transport(*(response(503) for _ in range(4)), response())
        with transport.patch():
            self.assertEqual(self.context.get_json("api/public/", {}), {"status": "ok"})
        self.assertEqual(len(transport.requests), 5)

    def test_rate_limit_attempts_are_bounded_even_in_unlimited_network_mode(self):
        self.context.max_connection_attempts = 0
        transport = Transport(*(response(429) for _ in range(3)))
        with transport.patch(), self.assertRaises(TooManyRequestsException):
            self.context.get_json("api/public/", {})
        self.assertEqual(len(transport.requests), 3)

    def test_network_failure_is_bounded_and_does_not_log_url_query(self):
        transport = Transport(*(requests.exceptions.Timeout("https://www.instagram.com/api/?token=fixture-secret")
                                for _ in range(3)))
        with transport.patch(), self.assertRaises(ConnectionException) as caught:
            self.context.get_json("api/public/", {"token": "fixture-secret"})
        self.assertEqual(len(transport.requests), 3)
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertNotIn("fixture-secret", str(self.context.error.call_args_list))

    def test_relative_login_redirect_is_not_followed(self):
        transport = Transport(response(302, headers={"Location": "/accounts/login/?next=fixture-secret"}))
        with transport.patch(), self.assertRaises(LoginRequiredException):
            self.context.get_json("api/public/", {})
        self.assertEqual(len(transport.requests), 1)

    def test_relative_challenge_redirect_is_not_followed(self):
        transport = Transport(response(302, headers={"Location": "/challenge/?token=fixture-secret"}))
        with transport.patch(), self.assertRaises(AbortDownloadException) as caught:
            self.context.get_json("api/public/", {})
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn("fixture-secret", str(caught.exception))


class TestPublicPageHTTPContext(unittest.TestCase):

    def setUp(self):
        self.context = InstaloaderContext(sleep=False, quiet=True)
        self.context._rate_controller = Mock()
        self.context.error = Mock()
        self.addCleanup(self.context.close)

    def test_page_data_handles_attribute_order_quotes_and_nested_embedded_results(self):
        first = {"__bbox": {"result": {"data": {"user": {"id": "101", "username": "public_fixture"}}}}}
        second = [{"ignored": {"__bbox": {"result": {"data": {"timeline": {"edges": []}}}}}}]
        html = ("<html><script data-sjs type='application/json'>" + json.dumps(first) + "</script>"
                "<script id='fixture' TYPE='APPLICATION/JSON' data-sjs>" + json.dumps(second) + "</script>"
                "<script type='application/json'>{broken}</script>"
                "<script type='text/javascript'>alert('ignored')</script></html>")
        transport = Transport(response(body=html))
        with transport.patch():
            self.assertEqual(self.context.get_page_data("public_fixture/"),
                             [{"user": {"id": "101", "username": "public_fixture"}}, {"timeline": {"edges": []}}])
        self.assertEqual(len(transport.requests), 1)

    def test_page_without_embedded_data_returns_empty_list(self):
        transport = Transport(response(body="<html><body>Public page has no embedded data.</body></html>"))
        with transport.patch():
            self.assertEqual(self.context.get_page_data("public_fixture/"), [])

    def test_dropped_page_connection_and_server_error_recover_without_restart(self):
        transport = Transport(requests.exceptions.ConnectionError('https://example.com/?token=fixture-secret'),
                              response(503, headers={'Retry-After': '4'}),
                              response(body='<script type="application/json">'
                                            '{"__bbox":{"result":{"data":{"post":"recovered"}}}}'
                                            '</script>'))
        with transport.patch():
            self.assertEqual(self.context.get_page_data('p/public_fixture/'), [{'post': 'recovered'}])
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual([r.url for r in transport.requests],
                         ['https://www.instagram.com/p/public_fixture/'] * 3)
        self.assertEqual([c.args[0] for c in self.context._rate_controller.sleep.call_args_list], [1, 4])
        self.assertNotIn('fixture-secret', str(self.context.error.call_args_list))

    def test_page_transient_failure_stops_at_configured_attempt_limit(self):
        for failure in (requests.exceptions.Timeout('fixture-secret'), response(503)):
            with self.subTest(failure=type(failure).__name__):
                transport = Transport(failure, failure, failure)
                with transport.patch(), self.assertRaises(ConnectionException) as caught:
                    self.context.get_page_data('p/public_fixture/')
                self.assertEqual(len(transport.requests), 3)
                self.assertNotIn('fixture-secret', str(caught.exception))
                if isinstance(failure, requests.Response):
                    self.assertEqual(caught.exception.status_code, 503)

    def test_public_page_pacing_adds_at_most_quarter_second_per_request(self):
        self.context.sleep = True
        transport = Transport(response(body='<html></html>'))
        delays = []
        with transport.patch(), patch('instaloader.instaloadercontext.random.expovariate', return_value=12), \
                patch('instaloader.instaloadercontext.time.sleep', side_effect=delays.append):
            self.context.get_page_data('public_fixture/')
        self.assertLessEqual(sum(delays), 0.25)

    def test_160_public_pages_do_not_inherit_the_75_request_json_budget(self):
        context = InstaloaderContext(sleep=False, quiet=True)
        self.addCleanup(context.close)
        transport = Transport(*(response(body='<html></html>') for _ in range(160)))
        clock = [1000.0]
        delays = []
        def sleep(seconds):
            delays.append(seconds)
            clock[0] += seconds
        with transport.patch(), patch('instaloader.instaloadercontext.time.monotonic', side_effect=lambda: clock[0]), \
                patch('instaloader.instaloadercontext.time.sleep', side_effect=sleep):
            for _ in range(160):
                self.assertEqual(context.get_page_data('public_fixture/'), [])
            # HTML navigations must not consume the separate aggregate GraphQL budget.
            for _ in range(120):
                context._rate_controller.wait_before_query('fixture_graphql')
        self.assertEqual(len(transport.requests), 160)
        self.assertEqual(delays, [])

    def test_page_request_uses_anonymous_session_and_existing_user_agent(self):
        self.context._session.cookies.set("sessionid", "fixture-owned-session")
        self.context._session.headers["Authorization"] = "Bearer fixture-owned-token"
        self.context.username = "fixture_owner"
        transport = Transport(response(body="<html></html>"))
        with transport.patch():
            self.assertEqual(self.context.get_page_data("public_fixture/"), [])
        outgoing = transport.requests[0]
        self.assertEqual(outgoing.headers["User-Agent"], self.context.user_agent)
        self.assertNotIn("Authorization", outgoing.headers)
        self.assertNotIn("fixture-owned-session", outgoing.headers.get("Cookie", ""))
        self.assertEqual(self.context._session.cookies.get("sessionid"), "fixture-owned-session")

    def test_page_restrictions_are_typed_terminal_errors(self):
        for status, error in ((401, QueryReturnedUnauthorizedException), (403, QueryReturnedForbiddenException),
                              (404, QueryReturnedNotFoundException), (429, TooManyRequestsException)):
            with self.subTest(status=status):
                transport = Transport(response(status, "<html>restricted</html>", {"Retry-After": "11"}))
                with transport.patch(), self.assertRaises(error) as caught:
                    self.context.get_page_data("public_fixture/")
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(caught.exception.status_code, status)
                if status == 429:
                    self.assertEqual(caught.exception.retry_after, 11.0)

    def test_page_login_and_challenge_redirects_are_not_followed(self):
        for location, error in (("/accounts/login/?next=fixture-secret", LoginRequiredException),
                                ("/challenge/?token=fixture-secret", AbortDownloadException)):
            with self.subTest(location=location):
                transport = Transport(response(302, headers={"Location": location}))
                with transport.patch(), self.assertRaises(error) as caught:
                    self.context.get_page_data("public_fixture/")
                self.assertEqual(len(transport.requests), 1)
                self.assertNotIn("fixture-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
