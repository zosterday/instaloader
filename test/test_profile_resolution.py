"""Unit tests for profile resolution fallbacks."""

import unittest
from unittest.mock import Mock

from instaloader.exceptions import QueryReturnedBadRequestException
from instaloader.structures import Profile


class TestProfileResolution(unittest.TestCase):

    def test_falls_back_to_current_graphql_queries_after_web_profile_info_400(self):
        context = Mock()
        context.get_json.side_effect = QueryReturnedBadRequestException("web profile info failed")
        context.doc_id_graphql_query.side_effect = [
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


if __name__ == '__main__':
    unittest.main()
