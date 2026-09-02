"""Unit tests for profile resolution fallbacks."""

import unittest
from unittest.mock import Mock

from instaloader.exceptions import (QueryReturnedBadRequestException,
                                    QueryReturnedUnauthorizedException)
from instaloader.instaloadercontext import InstaloaderContext
from instaloader.structures import Profile


class TestProfileResolution(unittest.TestCase):

    @staticmethod
    def _working_graphql_responses():
        return [
            {
                "data": {
                    "xdt_api__v1__feed__user_timeline_graphql_connection": {
                        "edges": [{
                            "node": {
                                "user": {
                                    "id": "1234",
                                    "username": "business_profile",
                                }
                            }
                        }]
                    }
                }
            },
            {
                "data": {
                    "user": {
                        "id": "1234",
                        "username": "business_profile",
                        "full_name": "Business Profile",
                        "is_private": False,
                        "follower_count": 42,
                        "following_count": 7,
                        "media_count": 3,
                    }
                }
            },
        ]

    def test_falls_back_to_current_graphql_queries_after_web_profile_info_400(self):
        context = Mock()
        context.get_json.side_effect = QueryReturnedBadRequestException("web profile info failed")
        context.doc_id_graphql_query.side_effect = self._working_graphql_responses()

        profile = Profile.from_username(context, "Business_Profile")

        self.assertEqual(profile.userid, 1234)
        self.assertEqual(profile.username, "business_profile")
        self.assertEqual(profile.followers, 42)
        self.assertEqual(profile.mediacount, 3)
        self.assertEqual(
            [call.args[0] for call in context.doc_id_graphql_query.call_args_list],
            ["27774912572190533", "38611279431804694"],
        )

    def test_falls_back_to_current_graphql_queries_after_web_profile_info_401(self):
        context = Mock()
        context.get_json.side_effect = QueryReturnedUnauthorizedException("web profile info failed")
        context.doc_id_graphql_query.side_effect = self._working_graphql_responses()

        profile = Profile.from_username(context, "Business_Profile")

        self.assertEqual(profile.userid, 1234)
        self.assertEqual(profile.username, "business_profile")
        self.assertEqual(profile.followers, 42)
        self.assertEqual(profile.mediacount, 3)
        self.assertEqual(
            [call.args[0] for call in context.doc_id_graphql_query.call_args_list],
            ["27774912572190533", "38611279431804694"],
        )

    def test_reraises_web_profile_info_error_when_fallback_cannot_resolve_user(self):
        context = Mock()
        context.get_json.side_effect = QueryReturnedBadRequestException("original failure")
        context.doc_id_graphql_query.return_value = {
            "data": {
                "xdt_api__v1__feed__user_timeline_graphql_connection": {
                    "edges": []
                }
            }
        }

        with self.assertRaisesRegex(QueryReturnedBadRequestException, "original failure"):
            Profile.from_username(context, "empty_profile")


class TestUnauthorizedResponse(unittest.TestCase):

    def test_401_is_not_retried(self):
        response = Mock(
            status_code=401,
            reason="Unauthorized",
            url="https://www.instagram.com/api/v1/users/web_profile_info/?username=test",
            is_redirect=False,
            headers={},
        )
        response.json.return_value = {
            "status": "fail",
            "message": "Please wait a few minutes before you try again.",
        }
        session = Mock()
        session.get.return_value = response
        context = InstaloaderContext(sleep=False, max_connection_attempts=3)
        context._rate_controller = Mock()

        with self.assertRaises(QueryReturnedUnauthorizedException):
            context.get_json(
                "api/v1/users/web_profile_info/",
                params={"username": "test"},
                session=session,
            )

        session.get.assert_called_once()


if __name__ == '__main__':
    unittest.main()
