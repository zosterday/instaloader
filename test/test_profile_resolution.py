"""Unit tests for profile resolution fallbacks."""

import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from instaloader.exceptions import (QueryReturnedBadRequestException,
                                    QueryReturnedUnauthorizedException)
from instaloader.instaloadercontext import InstaloaderContext
from instaloader.structures import Profile, _FeedPostIterator


def feed_response(username="business_profile", items=None, more_available=False, next_max_id=None):
    return {
        "status": "ok",
        "user": {
            "pk": "1234",
            "username": username,
            "is_private": False,
            "full_name": "Business Profile",
            "profile_pic_url": "https://example.com/profile.jpg",
        },
        "items": [] if items is None else items,
        "more_available": more_available,
        "next_max_id": next_max_id,
    }


class TestProfileResolution(unittest.TestCase):

    def test_anonymous_resolution_uses_feed_without_web_profile_info(self):
        context = Mock(is_logged_in=False, max_connection_attempts=3)
        context.get_json.return_value = feed_response()

        profile = Profile.from_username(context, "Business_Profile")

        self.assertEqual(profile.userid, 1234)
        self.assertEqual(profile.username, "business_profile")
        self.assertFalse(profile.has_blocked_viewer)
        self.assertIsNone(profile.mediacount)
        context.get_json.assert_called_once_with(
            "api/v1/feed/user/business_profile/username/", params={"count": 12}
        )

    def test_logged_in_resolution_falls_back_to_feed_after_401(self):
        context = Mock(is_logged_in=True, max_connection_attempts=3)
        context.get_json.side_effect = [
            QueryReturnedUnauthorizedException("web profile info failed"),
            feed_response(),
        ]

        profile = Profile.from_username(context, "Business_Profile")

        self.assertEqual(profile.userid, 1234)
        self.assertEqual(profile.username, "business_profile")
        self.assertFalse(profile.has_blocked_viewer)
        self.assertEqual(context.get_json.call_count, 2)
        self.assertEqual(
            context.get_json.call_args_list[0].args[0],
            "api/v1/users/web_profile_info/",
        )
        self.assertEqual(
            context.get_json.call_args_list[1].args[0],
            "api/v1/feed/user/business_profile/username/",
        )

    def test_reraises_401_when_feed_fallback_also_fails(self):
        context = Mock(is_logged_in=True, max_connection_attempts=3)
        context.get_json.side_effect = [
            QueryReturnedUnauthorizedException("original failure"),
            QueryReturnedBadRequestException("feed failure"),
        ]

        with self.assertRaisesRegex(QueryReturnedUnauthorizedException, "original failure"):
            Profile.from_username(context, "empty_profile")

    def test_get_posts_reuses_feed_first_page(self):
        context = Mock(is_logged_in=False, max_connection_attempts=3)
        context.get_json.return_value = feed_response(items=[{"pk": "post-1"}])
        profile = Profile.from_username(context, "Business_Profile")
        post = Mock(date_local=datetime(2026, 1, 1))

        with patch("instaloader.structures.Post.from_iphone_struct", return_value=post) as from_struct:
            posts = profile.get_posts()
            self.assertIs(next(posts), post)
            with self.assertRaises(StopIteration):
                next(posts)

        self.assertEqual(context.get_json.call_count, 1)
        from_struct.assert_called_once_with(context, {"pk": "post-1"})


class TestFeedPostIterator(unittest.TestCase):

    def test_paginates_with_max_id_and_tracks_newest_post(self):
        context = Mock()
        context.get_json.return_value = {
            "items": [{"pk": "post-2"}],
            "more_available": False,
        }
        first_page = feed_response(
            items=[{"pk": "post-1"}], more_available=True, next_max_id="cursor-1"
        )
        posts_by_id = {
            "post-1": Mock(date_local=datetime(2026, 1, 1)),
            "post-2": Mock(date_local=datetime(2026, 1, 2)),
        }

        with patch(
            "instaloader.structures.Post.from_iphone_struct",
            side_effect=lambda _context, item: posts_by_id[item["pk"]],
        ):
            iterator = _FeedPostIterator(context, "business_profile", first_page=first_page)
            self.assertEqual(list(iterator), [posts_by_id["post-1"], posts_by_id["post-2"]])

        context.get_json.assert_called_once_with(
            "api/v1/feed/user/business_profile/username/",
            params={"count": 12, "max_id": "cursor-1"},
        )
        self.assertIs(iterator.first_item, posts_by_id["post-2"])


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
