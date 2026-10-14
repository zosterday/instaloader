"""Public page shapes observed in upstream 4.15.4 and anonymous CLI requests."""

import unittest
from unittest.mock import Mock

from instaloader.exceptions import BadResponseException, QueryReturnedUnauthorizedException
from instaloader.structures import Post, Profile


def public_media(code='example', owner='collaborator', media_type=1):
    return {
        'pk': '4000000000000000001', 'id': 'POLARIS_4000000000000000001',
        'code': code, 'media_type': media_type, 'taken_at': 1791933082,
        'user': {'pk': '9999', 'id': 'scoped-owner-id', 'username': owner},
        'caption': {'text': 'fixture caption'}, 'like_count': 12, 'comment_count': 2,
        'image_versions2': {'candidates': [{'url': 'https://example.com/image.jpg'}]},
        'video_versions': [{'url': 'https://example.com/video.mp4'}] if media_type == 2 else None,
    }


def public_profile(username='requested', pk='1234'):
    return [
        {'xig_user_by_username': {'pk': pk, 'id': 'scoped-profile-id', 'username': username,
                                 'is_private': False, 'all_media_count': 24,
                                 'profile_pic_url': 'https://example.com/profile.jpg'}},
        {'xig_user_by_username': {'pk': pk, 'id': 'scoped-profile-id',
            'polaris_ordered_timeline_connection': {
                'edges': [{'node': {'pk': '4000000000000000001', 'code': 'example',
                                   '__typename': 'XIGPolarisImageMedia',
                                   'id': 'POLARIS_4000000000000000001', 'media_type': 1,
                                   'user': {'pk': '9999', 'username': 'collaborator'}}}],
                'page_info': {'has_next_page': True, 'end_cursor': 'page-cursor'},
            }}},
    ]


class TestPublicPageResolution(unittest.TestCase):
    def test_logged_in_page_does_not_invent_relationship_metadata(self):
        context = Mock(is_logged_in=True, max_connection_attempts=1)
        page = public_profile()
        page[0]['xig_user_by_username']['is_private'] = True
        page.pop()
        context.get_page_data.return_value = page
        context.doc_id_graphql_query.return_value = {'data': {'user': {
            'id': '1234', 'username': 'requested', 'is_private': True,
            'friendship_status': {'following': True},
        }}}
        profile = Profile.from_username(context, 'requested')
        self.assertTrue(profile.followed_by_viewer)

    def test_page_profile_reuses_timeline_and_keeps_collaborator_identity_separate(self):
        context = Mock(username=None, is_logged_in=False, max_connection_attempts=1)
        context.get_json.side_effect = AssertionError('Deprecated profile endpoint must not be needed.')
        context.doc_id_graphql_query.side_effect = AssertionError('Embedded first page must be reused.')
        context.get_page_data.side_effect = [public_profile(),
                                            [{'xig_polaris_media': {'code': 'example',
                                                'if_not_gated_logged_out': public_media()}}]]
        profile = Profile.from_username(context, 'requested')
        post = next(profile.get_posts())
        self.assertEqual(profile.userid, 1234)
        self.assertEqual(profile.mediacount, 24)
        self.assertEqual(post.mediaid, 4000000000000000001)
        self.assertEqual(int(post.owner_id), 9999)
        self.assertEqual(post.owner_username, 'collaborator')
        self.assertEqual(post.url, 'https://example.com/image.jpg')
        self.assertEqual(post.caption, 'fixture caption')

    def test_cached_id_mismatch_is_rejected(self):
        context = Mock(is_logged_in=False, max_connection_attempts=1)
        context.get_page_data.return_value = public_profile(pk='9999')
        context.get_json.return_value = {'data': {'user': {'id': '9999', 'username': 'requested'}}}
        with self.assertRaises(BadResponseException):
            Profile.from_username(context, 'requested', profile_id=1234)

    def test_same_id_rename_is_accepted(self):
        context = Mock(is_logged_in=False, max_connection_attempts=1)
        context.get_page_data.return_value = public_profile(username='renamed')
        context.get_json.return_value = {'data': {'user': {'id': '1234', 'username': 'renamed'}}}
        profile = Profile.from_username(context, 'oldname', profile_id=1234)
        self.assertEqual((profile.userid, profile.username), (1234, 'renamed'))

    def test_post_owner_cannot_resolve_requested_profile(self):
        context = Mock(is_logged_in=False, max_connection_attempts=1)
        context.get_page_data.return_value = []
        context.get_json.side_effect = QueryReturnedUnauthorizedException('profile endpoint refused')
        context.doc_id_graphql_query.return_value = {'data': {
            'xdt_api__v1__feed__user_timeline_graphql_connection': {
                'edges': [{'node': public_media()}],
                'page_info': {'has_next_page': False, 'end_cursor': None}}}}
        with self.assertRaises(BadResponseException):
            Profile.from_username(context, 'requested')

    def test_public_video_missing_view_counts_does_not_query_clips(self):
        context = Mock(is_logged_in=False)
        context.get_page_data.return_value = [{'xig_polaris_media': {'code': 'video',
            'if_not_gated_logged_out': public_media(code='video', media_type=2)}}]
        context.doc_id_graphql_query.side_effect = AssertionError('Optional counts must not add requests.')
        post = Post.from_shortcode(context, 'video')
        self.assertEqual(post.video_url, 'https://example.com/video.mp4')
        self.assertIsNone(post.video_view_count)


if __name__ == '__main__':
    unittest.main()
