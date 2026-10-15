"""Exercise public HTML, WWW requests, page boundaries and saved cursors together."""

import copy
import json
import unittest
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

import requests

from instaloader import InstaloaderContext, Profile
from instaloader.exceptions import BadResponseException, InvalidArgumentException, ProfileIdentityMismatchException
from instaloader.structures import get_json_structure, load_structure


def preview(index):
    return {'__typename': 'XIGPolarisImageMedia', '__isXIGPolarisMedia': 'XIGPolarisImageMedia',
            'pk': str(1000 + index), 'id': 'scoped-media-' + str(index), 'code': 'post' + str(index),
            'media_type': 1, 'product_type': 'feed', 'accessibility_caption': None,
            'caption': {'text': 'public fixture'}, 'display_uri': 'https://example.com/preview.jpg',
            'seo_canonical_url': None, 'user': {'pk': '9999', 'id': 'coauthor', 'username': 'coauthor'}}


def timeline(start, end, cursor):
    return {'edges': [{'node': preview(i), 'cursor': 'edge-' + str(i)} for i in range(start, end)],
            'page_info': {'has_next_page': cursor is not None, 'end_cursor': cursor}}


def html(data, bootstrap=False):
    scripts = [{'__bbox': {'result': {'data': data}}}]
    if bootstrap:
        scripts.insert(0, {'define': [['LSD', [], {'token': 'synthetic-lsd'}, 323],
                                     ['InstagramSecurityConfig', [], {'csrf_token': 'synthetic-csrf'}, 91]]})
    return ''.join("<script data-sjs type='application/json'>" + json.dumps(s) + '</script>' for s in scripts)


class PublicServer:
    """Replace network I/O only; reject the obsolete query and mismatched cursors."""

    def __init__(self):
        self.requests = []
        self.api_requests = []
        self.reply_node_id = 'scoped-profile'
        self.api_error = None
        self.profile_csrf_cookie = None
        self.rotate_csrf = False
        self.next_page_override = None
        self.total = 25
        self.fail_post_once = None

    def send(self, session, request, **kwargs):
        self.requests.append(request)
        path = urlsplit(request.url).path
        reply = requests.Response()
        reply.status_code = 200
        reply.url = request.url
        reply.request = request
        if path == '/example/':
            if self.profile_csrf_cookie:
                session.cookies.set('csrftoken', self.profile_csrf_cookie, domain='.instagram.com')
            data = {'xig_user_by_username': {'pk': '1234', 'id': 'scoped-profile', 'username': 'example',
                    'is_private': False, 'all_media_count': self.total, 'profile_pic_url': 'https://example.com/pic.jpg',
                    'polaris_ordered_timeline_connection': timeline(1, 13, 'public-cursor-1')}}
            text = html(data, bootstrap=True)
        elif path.startswith('/p/post'):
            i = int(path.split('/')[2][4:])
            if i == self.fail_post_once:
                self.fail_post_once = None
                raise requests.exceptions.ConnectionError('Synthetic dropped connection')
            media = {'pk': str(1000 + i), 'id': 'scoped-media-' + str(i), 'code': 'post' + str(i),
                     'media_type': 1, 'taken_at': 1791933082 - i, 'caption': {'text': 'public fixture'},
                     'user': {'pk': '9999', 'username': 'coauthor'}, 'like_count': 1, 'comment_count': 0,
                     'image_versions2': {'candidates': [{'url': 'https://example.com/image.jpg'}]}}
            text = html({'xig_polaris_media': {'code': media['code'], 'if_not_gated_logged_out': media}})
        elif path == '/api/graphql':
            form = parse_qs(request.body)
            variables = json.loads(form['variables'][0])
            self.api_requests.append((request, form, variables))
            expected = {'first': 12, 'id': 'scoped-profile', 'after': variables.get('after')}
            valid = (form.get('doc_id') == ['27389614800735091'] and variables == expected
                     and request.headers.get('Sec-Fetch-Site') == 'same-origin'
                     and request.headers.get('Sec-Fetch-Mode') == 'cors'
                     and request.headers.get('Sec-Fetch-Dest') == 'empty'
                     and request.headers.get('X-FB-LSD') == 'synthetic-lsd')
            if self.rotate_csrf and len(self.api_requests) > 1:
                valid = valid and request.headers.get('X-CSRFToken') == 'synthetic-api-csrf'
            if self.profile_csrf_cookie:
                cookie = SimpleCookie(request.headers.get('Cookie', '')).get('csrftoken')
                valid = valid and cookie is not None and cookie.value == request.headers.get('X-CSRFToken')
            if not valid:
                text = 'for (;;);' + json.dumps({'error': 1357054})
            elif self.api_error:
                text = 'for (;;);' + json.dumps(self.api_error)
            else:
                page = int(variables['after'].rsplit('-', 1)[1])
                start = page * 12 + 1
                end = min(start + 12, self.total + 1)
                cursor = 'public-cursor-' + str(page + 1) if end <= self.total else None
                text = 'for (;;);' + json.dumps({'data': {'node': {'id': self.reply_node_id,
                        'polaris_ordered_timeline_connection': self.next_page_override or timeline(start, end, cursor)}},
                        'extensions': {}})
                if self.rotate_csrf:
                    session.cookies.set('csrftoken', 'synthetic-api-csrf', domain='.instagram.com')
        else:
            reply.status_code = 401
            text = json.dumps({'status': 'fail', 'message': 'Please wait a few minutes before you try again.'})
        reply._content = text.encode()
        return reply

    def patch(self):
        return patch.object(requests.Session, 'send',
                            lambda session, request, **kwargs: self.send(session, request, **kwargs))


class TestPublicPagination(unittest.TestCase):
    def setUp(self):
        self.context = InstaloaderContext(sleep=False, quiet=True, max_connection_attempts=1)
        self.context._rate_controller = Mock()
        self.addCleanup(self.context.close)
        self.server = PublicServer()

    def test_yields_25_posts_across_three_pages_with_scoped_identity(self):
        with self.server.patch():
            profile = Profile.from_username(self.context, 'example')
            posts = list(profile.get_posts())
        self.assertEqual(profile.userid, 1234)
        self.assertEqual([p.mediaid for p in posts], list(range(1001, 1026)))
        self.assertTrue(all(int(p.owner_id) == 9999 for p in posts))
        self.assertEqual([q[2]['after'] for q in self.server.api_requests],
                         ['public-cursor-1', 'public-cursor-2'])

    def test_public_pagination_never_adds_multi_second_random_delays(self):
        self.context.sleep = True
        delays = []
        with self.server.patch(), patch('instaloader.instaloadercontext.random.expovariate', return_value=12), \
                patch('instaloader.instaloadercontext.time.sleep', side_effect=delays.append):
            posts = list(Profile.from_username(self.context, 'example').get_posts())
        self.assertEqual(len(posts), 25)
        self.assertLessEqual(max(delays), 0.25)

    def test_160_posts_survive_a_dropped_connection_at_post_81_without_losing_posts(self):
        self.context.max_connection_attempts = 3
        self.server.total = 160
        self.server.fail_post_once = 81
        with self.server.patch():
            posts = list(Profile.from_username(self.context, 'example').get_posts())
        self.assertEqual([p.mediaid for p in posts], list(range(1001, 1161)))
        self.assertEqual(len(self.server.api_requests), 13)
        self.assertEqual(sum(urlsplit(r.url).path == '/p/post81/' for r in self.server.requests), 2)

    def test_page_two_continues_after_freeze_load_thaw(self):
        with self.server.patch():
            profile = Profile.from_username(self.context, 'example')
            original = profile.get_posts()
            self.assertEqual([next(original).mediaid for _ in range(12)], list(range(1001, 1013)))
            saved = get_json_structure(original.freeze())
            resumed = profile.get_posts()
            resumed.thaw(load_structure(self.context, saved))
            self.assertEqual([p.mediaid for p in resumed], list(range(1012, 1026)))
        self.assertEqual(self.server.api_requests[0][2]['after'], 'public-cursor-1')

    def test_fork_one_public_checkpoint_keeps_saved_edges_and_cursor(self):
        from instaloader.structures import _profile_posts_query_variables
        with self.server.patch():
            profile = Profile.from_username(self.context, 'example')
            original = profile.get_posts()
            for _ in range(12):
                next(original)
            saved = get_json_structure(original.freeze())
            primary = saved['node']['primary']
            primary.update(doc_id='28975909992013618', query_variables=_profile_posts_query_variables('example'))
            before = copy.deepcopy(primary['remaining_data'])
            resumed = profile.get_posts()
            resumed.thaw(load_structure(self.context, saved))
            self.assertEqual(resumed.freeze().primary['remaining_data'], before)
            self.assertEqual([p.mediaid for p in resumed], list(range(1012, 1026)))
        self.assertEqual(self.server.api_requests[0][2]['after'], 'public-cursor-1')

    def test_native_cursor_is_not_relabelled_as_a_public_cursor(self):
        from instaloader.structures import _profile_posts_query_variables
        with self.server.patch():
            profile = Profile.from_username(self.context, 'example')
            saved = get_json_structure(profile.get_posts().freeze())
            primary = saved['node']['primary']
            primary.update(doc_id='28975909992013618', query_variables=_profile_posts_query_variables('example'))
            primary['remaining_data']['edges'][0]['node']['__typename'] = 'XDTMediaDict'
            primary['remaining_data']['page_info']['end_cursor'] = '1001_1234'
            with self.assertRaises(InvalidArgumentException):
                profile.get_posts().thaw(load_structure(self.context, saved))
        self.assertEqual(self.server.api_requests, [])

    def test_mismatching_scoped_response_does_not_continue_another_profile(self):
        self.server.reply_node_id = 'another-scoped-profile'
        with self.server.patch():
            iterator = Profile.from_username(self.context, 'example').get_posts()
            for _ in range(12):
                next(iterator)
            with self.assertRaises(ProfileIdentityMismatchException):
                next(iterator)

    def test_controller_errors_are_terminal_and_keep_the_saved_cursor(self):
        self.context.max_connection_attempts = 3
        self.server.api_error = {'error': 1357054, 'errorDescription': 'synthetic-lsd'}
        with self.server.patch():
            iterator = Profile.from_username(self.context, 'example').get_posts()
            for _ in range(12):
                next(iterator)
            with self.assertRaises(BadResponseException) as caught:
                next(iterator)
            self.assertNotIn('synthetic-lsd', str(caught.exception))
            self.assertEqual(iterator.freeze().primary['remaining_data']['page_info']['end_cursor'], 'public-cursor-1')
        self.assertEqual(len(self.server.api_requests), 1)

    def test_public_transport_never_imports_a_loaded_account_session(self):
        self.context._session.cookies.set('sessionid', 'synthetic-owned-session')
        self.context._session.headers['Authorization'] = 'synthetic-owned-authorization'
        with self.server.patch():
            posts = list(Profile.from_username(self.context, 'example').get_posts())
        self.assertEqual(len(posts), 25)
        for request in self.server.requests:
            self.assertNotIn('synthetic-owned', str(request.headers))
        request, form, _ = self.server.api_requests[0]
        self.assertEqual(form['lsd'], ['synthetic-lsd'])
        self.assertEqual(request.headers['X-CSRFToken'], 'synthetic-csrf')

    def test_rotated_public_csrf_cookie_is_retained_across_pages(self):
        self.server.rotate_csrf = True
        self.server.profile_csrf_cookie = 'synthetic-page-csrf'
        with self.server.patch():
            self.assertEqual(len(list(Profile.from_username(self.context, 'example').get_posts())), 25)
        self.assertEqual(self.server.api_requests[0][0].headers['X-CSRFToken'], 'synthetic-page-csrf')
        self.assertEqual(self.server.api_requests[1][0].headers['X-CSRFToken'], 'synthetic-api-csrf')

    def test_incomplete_pagination_does_not_silently_mark_feed_finished(self):
        for next_page in (timeline(13, 25, 'public-cursor-1'), timeline(1, 1, 'public-cursor-2'),
                          timeline(1, 13, 'public-cursor-2')):
            with self.subTest(next_page=next_page['page_info']):
                self.server.next_page_override = next_page
                with self.server.patch():
                    iterator = Profile.from_username(self.context, 'example').get_posts()
                    for _ in range(12):
                        next(iterator)
                    with self.assertRaises(BadResponseException):
                        next(iterator)


if __name__ == '__main__':
    unittest.main()
