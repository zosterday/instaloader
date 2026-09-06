"""Unit tests for profile resolution fallbacks."""

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from instaloader.exceptions import (ConnectionException, QueryReturnedBadRequestException,
                                    QueryReturnedUnauthorizedException)
from instaloader.instaloadercontext import InstaloaderContext
from instaloader.nodeiterator import NodeIterator, resumable_iteration
from instaloader.structures import (Profile, _AnonymousPostIterator, _FeedPostIterator,
                                    load_structure_from_file, save_structure_to_file)


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


def web_profile_response(username="web_profile", items=None, has_next_page=False, end_cursor=None):
    return {
        "status": "ok",
        "data": {
            "user": {
                "id": "5678",
                "username": username,
                "is_private": False,
                "full_name": "Web Profile",
                "profile_pic_url_hd": "https://example.com/profile.jpg",
                "edge_owner_to_timeline_media": {
                    "count": len(items or []),
                    "edges": [{"node": item} for item in (items or [])],
                    "page_info": {
                        "has_next_page": has_next_page,
                        "end_cursor": end_cursor,
                    },
                },
            },
        },
    }


class TestProfileResolution(unittest.TestCase):

    def test_anonymous_resolution_prefers_web_profile_info(self):
        context = Mock(is_logged_in=False, max_connection_attempts=3)
        context.get_json.return_value = web_profile_response()

        profile = Profile.from_username(context, "Web_Profile")

        self.assertEqual(profile.userid, 5678)
        self.assertEqual(profile.username, "web_profile")
        context.get_json.assert_called_once_with(
            "api/v1/users/web_profile_info/",
            params={"username": "web_profile"},
            _attempt=3,
        )
        context.get_iphone_json.assert_not_called()

    def test_anonymous_resolution_uses_mobile_only_after_web_401(self):
        context = Mock(is_logged_in=False, max_connection_attempts=3)
        context.get_json.side_effect = QueryReturnedUnauthorizedException("web profile info failed")
        context.get_iphone_json.return_value = feed_response()

        profile = Profile.from_username(context, "Business_Profile")

        self.assertEqual(profile.userid, 1234)
        self.assertEqual(profile.username, "business_profile")
        self.assertFalse(profile.has_blocked_viewer)
        self.assertIsNone(profile.mediacount)
        context.get_iphone_json.assert_called_once_with(
            "api/v1/feed/user/business_profile/username/", params={"count": 12}
        )
        context.get_json.assert_called_once()

    def test_get_posts_keeps_mobile_unused_when_bundled_web_page_is_complete(self):
        context = Mock(is_logged_in=False, max_connection_attempts=3)
        context.get_json.return_value = web_profile_response(items=[{
            "id": "1",
            "shortcode": "web-post",
            "taken_at_timestamp": 1767225600,
            "__typename": "GraphImage",
            "is_video": False,
        }])
        profile = Profile.from_username(context, "Web_Profile")

        posts = profile.get_posts()

        self.assertIsInstance(posts, _AnonymousPostIterator)
        self.assertEqual(next(posts).shortcode, "web-post")
        with self.assertRaises(StopIteration):
            next(posts)
        context.get_iphone_json.assert_not_called()

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
        context.get_iphone_json.assert_not_called()

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
        context.get_json.side_effect = QueryReturnedUnauthorizedException("web profile info failed")
        context.get_iphone_json.return_value = feed_response(items=[{"pk": "post-1"}])
        profile = Profile.from_username(context, "Business_Profile")
        post = Mock(date_local=datetime(2026, 1, 1))

        with patch("instaloader.structures.Post.from_iphone_struct", return_value=post) as from_struct:
            posts = profile.get_posts()
            self.assertIs(next(posts), post)
            with self.assertRaises(StopIteration):
                next(posts)

        context.get_json.assert_called_once()
        self.assertEqual(context.get_iphone_json.call_count, 1)
        from_struct.assert_called_once_with(context, {"pk": "post-1"})


class TestFeedPostIterator(unittest.TestCase):

    def test_paginates_with_max_id_and_tracks_newest_post(self):
        context = Mock()
        context.get_iphone_json.return_value = {
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
            iterator = _FeedPostIterator(context, 1234, first_page=first_page)
            self.assertEqual(list(iterator), [posts_by_id["post-1"], posts_by_id["post-2"]])

        context.get_iphone_json.assert_called_once_with(
            "api/v1/feed/user/1234/",
            params={"count": 12, "max_id": "cursor-1"},
        )
        self.assertIs(iterator.first_item, posts_by_id["post-2"])

    def test_failure_saves_checkpoint_and_next_run_retries_only_last_post(self):
        context = Mock(username=None)
        first_page = feed_response(items=[{"pk": "post-1"}, {"pk": "post-2"}])
        posts_by_id = {
            "post-1": Mock(
                date_local=datetime(2026, 1, 2, tzinfo=timezone.utc),
                _node={"id": "1", "shortcode": "one", "taken_at_timestamp": 1767312000},
            ),
            "post-2": Mock(
                date_local=datetime(2026, 1, 1, tzinfo=timezone.utc),
                _node={"id": "2", "shortcode": "two", "taken_at_timestamp": 1767225600},
            ),
        }

        with TemporaryDirectory() as temp_dir, patch(
            "instaloader.structures.Post.from_iphone_struct",
            side_effect=lambda _context, item: posts_by_id[item["pk"]],
        ):
            resume_path = str(Path(temp_dir) / "resume.json.xz")
            iterator = _FeedPostIterator(context, 1234, first_page=first_page)
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                with resumable_iteration(
                    context,
                    iterator,
                    load_structure_from_file,
                    save_structure_to_file,
                    lambda _magic: resume_path,
                ):
                    self.assertIs(next(iterator), posts_by_id["post-1"])
                    raise RuntimeError("download failed")

            self.assertTrue(Path(resume_path).is_file())
            resumed_iterator = _FeedPostIterator(context, 1234, first_page=first_page)
            with resumable_iteration(
                context,
                resumed_iterator,
                load_structure_from_file,
                save_structure_to_file,
                lambda _magic: resume_path,
            ) as (is_resuming, start_index):
                self.assertTrue(is_resuming)
                self.assertEqual(start_index, 0)
                self.assertEqual(
                    list(resumed_iterator),
                    [posts_by_id["post-1"], posts_by_id["post-2"]],
                )

            self.assertFalse(Path(resume_path).exists())

    def test_failed_later_page_resumes_at_failed_cursor_without_replaying_earlier_pages(self):
        context = Mock(username=None)
        first_page = feed_response(
            items=[{"pk": "post-1"}, {"pk": "post-2"}],
            more_available=True,
            next_max_id="cursor-1",
        )
        second_page = {
            "items": [{"pk": "post-3"}, {"pk": "post-4"}],
            "more_available": True,
            "next_max_id": "cursor-2",
        }
        final_page = {"items": [{"pk": "post-5"}], "more_available": False}
        posts_by_id = {
            "post-{}".format(number): Mock(
                date_local=datetime(2026, 1, number, tzinfo=timezone.utc),
                _node={
                    "id": str(number),
                    "shortcode": "post-{}".format(number),
                    "taken_at_timestamp": 1767225600 + number,
                },
            )
            for number in range(1, 6)
        }
        context.get_iphone_json.side_effect = [second_page, ConnectionException("page refused")]

        with TemporaryDirectory() as temp_dir, patch(
            "instaloader.structures.Post.from_iphone_struct",
            side_effect=lambda _context, item: posts_by_id[item["pk"]],
        ):
            resume_path = str(Path(temp_dir) / "resume.json.xz")
            iterator = _FeedPostIterator(context, 1234, first_page=first_page)
            with self.assertRaisesRegex(ConnectionException, "page refused"):
                with resumable_iteration(
                    context,
                    iterator,
                    load_structure_from_file,
                    save_structure_to_file,
                    lambda _magic: resume_path,
                ):
                    list(iterator)

            self.assertEqual(context.get_iphone_json.call_count, 2)
            context.get_iphone_json.reset_mock(side_effect=True)
            context.get_iphone_json.return_value = final_page
            resumed_iterator = _FeedPostIterator(context, 1234, first_page=first_page)
            with resumable_iteration(
                context,
                resumed_iterator,
                load_structure_from_file,
                save_structure_to_file,
                lambda _magic: resume_path,
            ) as (is_resuming, start_index):
                self.assertTrue(is_resuming)
                self.assertEqual(start_index, 3)
                self.assertEqual(
                    list(resumed_iterator),
                    [posts_by_id["post-4"], posts_by_id["post-5"]],
                )

            context.get_iphone_json.assert_called_once_with(
                "api/v1/feed/user/1234/", params={"count": 12, "max_id": "cursor-2"}
            )


class TestAnonymousPostIterator(unittest.TestCase):

    def test_web_run_checkpoint_resumes_master_iterator(self):
        context = Mock(username=None)
        posts = {
            "1": Mock(
                mediaid=1,
                date_local=datetime(2026, 1, 2, tzinfo=timezone.utc),
                _node={"id": "1", "shortcode": "one", "taken_at_timestamp": 1767312000},
            ),
            "2": Mock(
                mediaid=2,
                date_local=datetime(2026, 1, 1, tzinfo=timezone.utc),
                _node={"id": "2", "shortcode": "two", "taken_at_timestamp": 1767225600},
            ),
        }
        first_data = {
            "count": 2,
            "edges": [{"node": {"id": "1"}}, {"node": {"id": "2"}}],
            "page_info": {"has_next_page": False, "end_cursor": None},
        }

        def new_hybrid():
            primary = NodeIterator(
                context,
                query_hash="web-query",
                edge_extractor=lambda data: data,
                node_wrapper=lambda node: posts[node["id"]],
                query_variables={"id": "1234"},
                query_referer="https://www.instagram.com/example/",
                first_data=first_data,
            )
            return _AnonymousPostIterator(context, primary, 1234)

        with TemporaryDirectory() as temp_dir:
            resume_path = str(Path(temp_dir) / "resume.json.xz")
            hybrid = new_hybrid()
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                with resumable_iteration(
                    context,
                    hybrid,
                    load_structure_from_file,
                    save_structure_to_file,
                    lambda _magic: resume_path,
                ):
                    self.assertIs(next(hybrid), posts["1"])
                    raise RuntimeError("download failed")

            resumed_hybrid = new_hybrid()
            with resumable_iteration(
                context,
                resumed_hybrid,
                load_structure_from_file,
                save_structure_to_file,
                lambda _magic: resume_path,
            ) as (is_resuming, start_index):
                self.assertTrue(is_resuming)
                self.assertEqual(start_index, 0)
                self.assertEqual(list(resumed_hybrid), [posts["1"], posts["2"]])

    def test_uses_same_resume_identity_as_mobile_iterator(self):
        context = Mock(username=None)
        feed = _FeedPostIterator(context, 1234, first_page=feed_response())
        hybrid = _AnonymousPostIterator(context, iter(()), 1234)

        self.assertEqual(feed.magic, hybrid.magic)
        self.assertNotIn('/', feed.magic)
        self.assertNotIn('+', feed.magic)

    def test_keeps_mobile_unused_when_web_iterator_succeeds(self):
        context = Mock()
        posts = [
            Mock(mediaid=1, date_local=datetime(2026, 1, 2)),
            Mock(mediaid=2, date_local=datetime(2026, 1, 1)),
        ]

        iterator = _AnonymousPostIterator(context, iter(posts), 1234)

        self.assertEqual(list(iterator), posts)
        context.get_iphone_json.assert_not_called()
        self.assertIs(iterator.first_item, posts[0])

    def test_switches_after_web_failure_and_skips_duplicate_posts(self):
        context = Mock()
        web_post = Mock(mediaid=1, date_local=datetime(2026, 1, 2))
        duplicate = Mock(mediaid=1, date_local=datetime(2026, 1, 2))
        fallback_post = Mock(mediaid=2, date_local=datetime(2026, 1, 1))
        context.get_iphone_json.return_value = {
            "items": [{"pk": "post-1"}, {"pk": "post-2"}],
            "more_available": False,
        }

        def failing_web_iterator():
            yield web_post
            raise ConnectionException("web timeline refused")

        posts_by_id = {"post-1": duplicate, "post-2": fallback_post}
        with patch(
            "instaloader.structures.Post.from_iphone_struct",
            side_effect=lambda _context, item: posts_by_id[item["pk"]],
        ):
            iterator = _AnonymousPostIterator(context, failing_web_iterator(), 1234)
            self.assertEqual(list(iterator), [web_post, fallback_post])

        context.get_iphone_json.assert_called_once_with(
            "api/v1/feed/user/1234/", params={"count": 12}
        )
        context.log.assert_called_once()
        self.assertIs(iterator.first_item, web_post)

    def test_mobile_run_can_resume_checkpoint_created_after_web_fallback(self):
        context = Mock(username=None)
        web_post = Mock(
            mediaid=1,
            date_local=datetime(2026, 1, 2, tzinfo=timezone.utc),
            _node={"id": "1", "shortcode": "one", "taken_at_timestamp": 1767312000},
        )
        duplicate = Mock(
            mediaid=1,
            date_local=datetime(2026, 1, 2, tzinfo=timezone.utc),
            _node={"id": "1", "shortcode": "one", "taken_at_timestamp": 1767312000},
        )
        fallback_post = Mock(
            mediaid=2,
            date_local=datetime(2026, 1, 1, tzinfo=timezone.utc),
            _node={"id": "2", "shortcode": "two", "taken_at_timestamp": 1767225600},
        )
        fallback_page = {
            "items": [{"pk": "post-1"}, {"pk": "post-2"}],
            "more_available": False,
        }
        context.get_iphone_json.return_value = fallback_page

        def failing_web_iterator():
            yield web_post
            raise ConnectionException("web timeline refused")

        posts_by_id = {"post-1": duplicate, "post-2": fallback_post}
        with TemporaryDirectory() as temp_dir, patch(
            "instaloader.structures.Post.from_iphone_struct",
            side_effect=lambda _context, item: posts_by_id[item["pk"]],
        ):
            resume_path = str(Path(temp_dir) / "resume.json.xz")
            hybrid = _AnonymousPostIterator(context, failing_web_iterator(), 1234)
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                with resumable_iteration(
                    context,
                    hybrid,
                    load_structure_from_file,
                    save_structure_to_file,
                    lambda _magic: resume_path,
                ):
                    self.assertIs(next(hybrid), web_post)
                    self.assertIs(next(hybrid), fallback_post)
                    raise RuntimeError("download failed")

            # Simulate the next invocation resolving through mobile immediately.
            mobile = _FeedPostIterator(context, 1234, first_page=fallback_page)
            with resumable_iteration(
                context,
                mobile,
                load_structure_from_file,
                save_structure_to_file,
                lambda _magic: resume_path,
            ) as (is_resuming, start_index):
                self.assertTrue(is_resuming)
                self.assertEqual(start_index, 1)
                self.assertEqual(list(mobile), [fallback_post])


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
