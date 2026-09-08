"""Unit tests for anonymous web profile resolution and pagination."""

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from instaloader.exceptions import (ConnectionException, QueryReturnedBadRequestException,
                                    QueryReturnedUnauthorizedException)
from instaloader.instaloader import Instaloader
from instaloader.instaloadercontext import InstaloaderContext
from instaloader.nodeiterator import NodeIterator, resumable_iteration
from instaloader.structures import (FrozenFeedIterator, Profile, _AnonymousPostIterator,
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


def timeline_response(username="business_profile", items=None, has_next_page=False, end_cursor=None):
    if items is None:
        items = [{
            "pk": "post-1",
            "code": "web-post",
            "media_type": 1,
            "taken_at": 1767225600,
            "caption": None,
            "has_liked": False,
            "like_count": 0,
            "comment_count": 0,
            "image_versions2": {"candidates": [{"url": "https://example.com/post.jpg"}]},
            "user": {
                "pk": "1234",
                "username": username,
                "is_private": False,
                "full_name": "Business Profile",
                "profile_pic_url": "https://example.com/profile.jpg",
            },
        }]
    return {
        "status": "ok",
        "data": {
            "xdt_api__v1__feed__user_timeline_graphql_connection": {
                "edges": [{"node": item} for item in items],
                "page_info": {
                    "has_next_page": has_next_page,
                    "end_cursor": end_cursor,
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

    def test_anonymous_resolution_uses_web_timeline_after_profile_info_401(self):
        context = Mock(is_logged_in=False, max_connection_attempts=3)
        context.get_json.side_effect = QueryReturnedUnauthorizedException("web profile info failed")
        context.doc_id_graphql_query.return_value = timeline_response()

        profile = Profile.from_username(context, "Business_Profile")

        self.assertEqual(profile.userid, 1234)
        self.assertEqual(profile.username, "business_profile")
        self.assertFalse(profile.has_blocked_viewer)
        self.assertIsNone(profile.mediacount)
        context.get_json.assert_called_once()
        self.assertEqual(context.doc_id_graphql_query.call_args.args[0], "38154989454116081")
        context.get_iphone_json.assert_not_called()

    def test_cached_id_is_used_without_native_profile_resolution(self):
        context = Mock(is_logged_in=False, max_connection_attempts=3)
        context.get_json.side_effect = QueryReturnedUnauthorizedException("web profile info failed")

        profile = Profile.from_username(context, "Cached_Profile", profile_id=1234)

        self.assertEqual(profile.userid, 1234)
        self.assertEqual(profile.username, "cached_profile")
        self.assertTrue(profile.is_id_only)
        context.get_iphone_json.assert_not_called()

    def test_cached_id_web_posts_failure_does_not_fall_back_to_mobile(self):
        context = Mock(is_logged_in=False, max_connection_attempts=3)
        context.get_json.side_effect = QueryReturnedUnauthorizedException("web profile info failed")
        context.doc_id_graphql_query.side_effect = QueryReturnedUnauthorizedException("web posts failed")

        profile = Profile.from_username(context, "Cached_Profile", profile_id=1234)
        with self.assertRaisesRegex(QueryReturnedUnauthorizedException, "web posts failed"):
            profile.get_posts()

        context.doc_id_graphql_query.assert_called_once()
        context.get_iphone_json.assert_not_called()

    def test_get_posts_uses_current_web_timeline_shape(self):
        context = Mock(is_logged_in=False, max_connection_attempts=3)
        context.get_json.return_value = web_profile_response()
        context.doc_id_graphql_query.return_value = timeline_response(username="web_profile")
        profile = Profile.from_username(context, "Web_Profile")

        posts = profile.get_posts()

        self.assertIsInstance(posts, _AnonymousPostIterator)
        self.assertEqual(next(posts).shortcode, "web-post")
        with self.assertRaises(StopIteration):
            next(posts)
        self.assertEqual(context.doc_id_graphql_query.call_args.args[0], "38154989454116081")
        context.get_iphone_json.assert_not_called()

    def test_anonymous_web_timeline_paginates_with_relay_cursor(self):
        context = Mock(is_logged_in=False, max_connection_attempts=3)
        context.get_json.return_value = web_profile_response()
        first_node = {"pk": "post-1"}
        second_node = {"pk": "post-2"}
        context.doc_id_graphql_query.side_effect = [
            timeline_response(items=[first_node], has_next_page=True, end_cursor="cursor-1"),
            timeline_response(items=[second_node]),
        ]
        posts = {
            "post-1": Mock(date_local=datetime(2026, 1, 2)),
            "post-2": Mock(date_local=datetime(2026, 1, 1)),
        }
        profile = Profile.from_username(context, "Web_Profile")

        with patch(
            "instaloader.structures.Post.from_iphone_struct",
            side_effect=lambda _context, node: posts[node["pk"]],
        ):
            self.assertEqual(list(profile.get_posts()), [posts["post-1"], posts["post-2"]])

        first_variables = context.doc_id_graphql_query.call_args_list[0].args[1]
        second_variables = context.doc_id_graphql_query.call_args_list[1].args[1]
        self.assertNotIn("after", first_variables)
        self.assertEqual(second_variables["after"], "cursor-1")
        self.assertEqual(second_variables["first"], 12)
        self.assertEqual(first_variables["data"]["count"], 12)
        self.assertEqual(first_variables["username"], "web_profile")
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

    def test_get_posts_reuses_web_timeline_resolution_page(self):
        context = Mock(is_logged_in=False, max_connection_attempts=3)
        context.get_json.side_effect = QueryReturnedUnauthorizedException("web profile info failed")
        context.doc_id_graphql_query.return_value = timeline_response()
        profile = Profile.from_username(context, "Business_Profile")

        posts = profile.get_posts()
        self.assertEqual(next(posts).shortcode, "web-post")
        with self.assertRaises(StopIteration):
            next(posts)

        context.doc_id_graphql_query.assert_called_once()
        context.get_iphone_json.assert_not_called()


class TestCachedProfileID(unittest.TestCase):

    def test_cli_passes_stored_id_into_anonymous_web_resolution(self):
        loader = Instaloader(sleep=False)
        profile = Mock(userid=1234)

        with patch.object(loader, "load_profile_id", return_value=1234), patch(
            "instaloader.instaloader.Profile.from_username", return_value=profile
        ) as from_username:
            result = loader.check_profile_id("cached_profile")

        self.assertIs(result, profile)
        from_username.assert_called_once_with(
            loader.context, "cached_profile", profile_id=1234
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

    def test_resume_identity_is_stable_and_filename_safe(self):
        context = Mock(username=None)
        first = _AnonymousPostIterator(context, iter(()), 1234)
        second = _AnonymousPostIterator(context, iter(()), 1234)

        self.assertEqual(first.magic, second.magic)
        self.assertNotIn('/', first.magic)
        self.assertNotIn('+', first.magic)

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

    def test_does_not_switch_to_mobile_after_web_pagination_started(self):
        context = Mock()
        web_post = Mock(mediaid=1, date_local=datetime(2026, 1, 2))

        def failing_web_iterator():
            yield web_post
            raise ConnectionException("web timeline refused")

        iterator = _AnonymousPostIterator(context, failing_web_iterator(), 1234)
        self.assertIs(next(iterator), web_post)
        with self.assertRaisesRegex(ConnectionException, "web timeline refused"):
            next(iterator)

        context.get_iphone_json.assert_not_called()
        self.assertIs(iterator.first_item, web_post)

    def test_web_run_ignores_untranslatable_mobile_checkpoint(self):
        context = Mock(username=None)
        web_post = Mock(
            mediaid=1,
            date_local=datetime(2026, 1, 2, tzinfo=timezone.utc),
            _node={"id": "1", "shortcode": "one", "taken_at_timestamp": 1767312000},
        )
        with TemporaryDirectory() as temp_dir:
            resume_path = str(Path(temp_dir) / "resume.json.xz")
            save_structure_to_file(FrozenFeedIterator(
                user_id="1234",
                context_username=None,
                total_index=0,
                best_before=datetime(2026, 12, 1).timestamp(),
                data={"items": [{"pk": "post-1"}], "more_available": False},
                page_index=0,
                seen_cursors=[],
                first_node=None,
            ), resume_path)

            primary = NodeIterator(
                context,
                query_hash="web-query",
                edge_extractor=lambda data: data,
                node_wrapper=lambda _node: web_post,
                query_variables={"id": "1234"},
                query_referer="https://www.instagram.com/example/",
                first_data={
                    "count": 1,
                    "edges": [{"node": {"id": "1"}}],
                    "page_info": {"has_next_page": False, "end_cursor": None},
                },
            )
            hybrid = _AnonymousPostIterator(context, primary, 1234)
            with resumable_iteration(
                context,
                hybrid,
                load_structure_from_file,
                save_structure_to_file,
                lambda _magic: resume_path,
            ) as (is_resuming, start_index):
                self.assertFalse(is_resuming)
                self.assertEqual(start_index, 0)
                self.assertEqual(list(hybrid), [web_post])

            self.assertFalse(Path(resume_path).exists())


class TestUnauthorizedResponse(unittest.TestCase):

    def test_non_rate_limit_401_is_not_retried(self):
        response = Mock(
            status_code=401,
            reason="Unauthorized",
            url="https://www.instagram.com/api/v1/users/web_profile_info/?username=test",
            is_redirect=False,
            headers={},
        )
        response.json.return_value = {
            "status": "fail",
            "message": "login_required",
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

    def test_anonymous_web_please_wait_401_is_treated_as_endpoint_failure(self):
        throttled = Mock(
            status_code=401,
            reason="Unauthorized",
            url="https://www.instagram.com/graphql/query",
            is_redirect=False,
            headers={},
        )
        throttled.json.return_value = {
            "status": "fail",
            "message": "Please wait a few minutes before you try again.",
        }
        session = Mock()
        session.post.return_value = throttled
        context = InstaloaderContext(sleep=False, max_connection_attempts=3)
        context._rate_controller = Mock()

        with self.assertRaises(QueryReturnedUnauthorizedException):
            context.get_json(
                "graphql/query",
                params={"doc_id": "web-query", "variables": "{}"},
                session=session,
                use_post=True,
            )

        session.post.assert_called_once()
        context._rate_controller.handle_429.assert_not_called()

    def test_anonymous_mobile_please_wait_401_uses_iphone_cooldown(self):
        throttled = Mock(
            status_code=401,
            reason="Unauthorized",
            url="https://i.instagram.com/api/v1/feed/user/1234/",
            is_redirect=False,
            headers={},
        )
        throttled.json.return_value = {
            "status": "fail",
            "message": "Please wait a few minutes before you try again.",
        }
        successful = Mock(status_code=200, is_redirect=False, headers={})
        successful.json.return_value = {"status": "ok", "items": []}
        session = Mock()
        session.get.side_effect = [throttled, successful]
        context = InstaloaderContext(sleep=False, max_connection_attempts=3)
        context._rate_controller = Mock()

        result = context.get_json(
            "api/v1/feed/user/1234/",
            params={"count": 12},
            host="i.instagram.com",
            session=session,
        )

        self.assertEqual(result, {"status": "ok", "items": []})
        self.assertEqual(session.get.call_count, 2)
        context._rate_controller.handle_429.assert_called_once_with("iphone")


if __name__ == '__main__':
    unittest.main()
