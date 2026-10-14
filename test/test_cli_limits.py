"""CLI profile count limits and lazy iterator request amplification."""

import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from instaloader import Instaloader, Profile
from instaloader.__main__ import _main
from instaloader.nodeiterator import NodeIterator


class TestCLILimits(unittest.TestCase):
    def test_profile_count_limit_reaches_real_download_loop(self):
        loader = Instaloader(sleep=False, quiet=True, resume_prefix=None, save_metadata=False)
        self.addCleanup(loader.close)
        profile = Profile(loader.context, {'id': '1234', 'username': 'public', 'is_private': False,
                                          'edge_owner_to_timeline_media': {'count': 3}})
        seen = []
        def posts():
            for identity in range(3):
                seen.append(identity)
                yield Mock(shortcode=str(identity))
        with patch.object(loader, 'check_profile_id', return_value=profile), \
                patch.object(Profile, 'get_posts', side_effect=posts), \
                patch.object(loader, 'download_post', return_value=True) as download:
            _main(loader, ['public'], download_profile_pic=False, max_count=1)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(seen, [0])

    def test_first_item_comparison_does_not_repeat_metadata_requests(self):
        wrapped = []
        def wrap(node):
            wrapped.append(node['id'])
            return (node['id'], datetime(2026, 1, int(node['id'])))
        iterator = NodeIterator(Mock(username=None), 'fixture', lambda data: data, wrap,
                                first_data={'edges': [{'node': {'id': str(i)}} for i in [1, 2, 3]],
                                            'page_info': {'has_next_page': False}},
                                is_first=lambda item, first: first is None or item[1] > first[1])
        self.assertEqual([item[0] for item in iterator], ['1', '2', '3'])
        self.assertEqual(iterator.first_item[0], '3')
        self.assertEqual(wrapped, ['1', '2', '3'])


if __name__ == '__main__':
    unittest.main()
