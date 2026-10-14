"""Offline checkpoints: schema compatibility, cursor continuity and recoverability."""

import copy
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from instaloader.exceptions import InvalidArgumentException
from instaloader.nodeiterator import FrozenNodeIterator, NodeIterator, resumable_iteration
from instaloader.structures import (FrozenAnonymousIterator, _AnonymousPostIterator, _profile_posts_query_variables,
                                    get_json_structure, load_structure, load_structure_from_file,
                                    save_structure_to_file)


def node_checkpoint():
    return {
        "query_hash": "fixture-query", "doc_id": None,
        "query_variables": {"id": "1234"},
        "query_referer": "https://www.instagram.com/example/", "context_username": None,
        "total_index": 67, "best_before": (datetime.now() + timedelta(days=2)).timestamp(),
        "remaining_data": {
            "edges": [{"node": {"id": "68"}}],
            "page_info": {"has_next_page": True, "end_cursor": "saved-cursor"},
        },
        "first_node": None,
    }


def anonymous_checkpoint():
    return {
        "user_id": "1234", "context_username": None, "total_index": 67,
        "best_before": node_checkpoint()["best_before"], "mode": "primary",
        "primary": node_checkpoint(), "fallback": None, "seen_mediaids": [], "first_node": None,
    }


class TestCheckpointCompatibility(unittest.TestCase):
    def test_malformed_first_post_is_rejected_before_iterator_mutation(self):
        for first_node in [{'shortcode': 'fixture'}, {'shortcode': 'fixture', 'date': 'invalid'}]:
            with self.subTest(first_node=first_node):
                context = Mock(username=None)
                state = anonymous_checkpoint()
                state['first_node'] = first_node
                primary = NodeIterator(context, 'fixture-query', lambda data: data, lambda node: node,
                                       query_variables={'id': '1234'},
                                       query_referer='https://www.instagram.com/example/',
                                       first_data=node_checkpoint()['remaining_data'])
                iterator = _AnonymousPostIterator(context, primary, 1234)
                with self.assertRaises(InvalidArgumentException):
                    iterator.thaw(load_structure(context, state))
                self.assertEqual(iterator.total_index, 0)
                self.assertEqual(primary.total_index, 0)

    def test_frozen_only_loader_rejects_post_documents_without_network(self):
        context = Mock()
        for document in [{'shortcode': 'unrelated'},
                         {'node': {'shortcode': 'unrelated'}, 'instaloader': {'node_type': 'Post'}}]:
            with self.subTest(document=document), self.assertRaises(InvalidArgumentException):
                load_structure(context, document, frozen_only=True)
        context.get_page_data.assert_not_called()
        context.doc_id_graphql_query.assert_not_called()

    def test_known_old_web_query_migrates_without_losing_cursor_or_source_identity(self):
        context = Mock(username=None)
        current_variables = _profile_posts_query_variables('example')
        old_variables = copy.deepcopy(current_variables)
        del old_variables['first']
        del old_variables['include_multi_captions']
        del old_variables['data']['include_reel_media_seen_timestamp']
        old_variables['__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider'] = False
        state = anonymous_checkpoint()
        state['primary'].update(query_hash=None, doc_id='38154989454116081', query_variables=old_variables)
        primary = NodeIterator(context, None, lambda data: data, lambda node: node,
                               query_variables=current_variables,
                               query_referer='https://www.instagram.com/example/', doc_id='28975909992013618',
                               first_data=node_checkpoint()['remaining_data'])
        iterator = _AnonymousPostIterator(context, primary, 1234)
        iterator.thaw(load_structure(context, state))
        saved = get_json_structure(iterator.freeze())['node']['primary']
        self.assertEqual(saved['doc_id'], '28975909992013618')
        self.assertEqual(saved['remaining_data'], state['primary']['remaining_data'])
        self.assertEqual(saved['migrated_from_query']['doc_id'], '38154989454116081')
        self.assertEqual(saved['migrated_from_query']['query_variables'], old_variables)

    def test_raw_anonymous_schema_resumes_saved_cursor(self):
        context = Mock(username=None)
        context.graphql_query.return_value = {
            "edges": [{"node": {"id": "69"}}],
            "page_info": {"has_next_page": False, "end_cursor": None},
        }
        primary = NodeIterator(context, "fixture-query", lambda data: data,
                               lambda node: Mock(mediaid=int(node["id"]), date_local=datetime(2026, 1, 1)),
                               query_variables={"id": "1234"},
                               query_referer="https://www.instagram.com/example/",
                               first_data=node_checkpoint()["remaining_data"])
        iterator = _AnonymousPostIterator(context, primary, 1234)
        frozen = load_structure(context, anonymous_checkpoint())
        self.assertIsInstance(frozen, FrozenAnonymousIterator)
        iterator.thaw(frozen)
        self.assertEqual(iterator.total_index, 67)
        self.assertEqual([post.mediaid for post in iterator], [68, 69])
        self.assertEqual(context.graphql_query.call_args.args[1]["after"], "saved-cursor")

    def test_legacy_node_defaults_do_not_mutate_input(self):
        old = node_checkpoint()
        del old["doc_id"]
        del old["first_node"]
        document = {"node": old, "instaloader": {"node_type": "FrozenNodeIterator"}}
        before = copy.deepcopy(document)
        frozen = load_structure(Mock(), document)
        self.assertIsInstance(frozen, FrozenNodeIterator)
        self.assertIsNone(frozen.doc_id)
        self.assertIsNone(frozen.first_node)
        self.assertEqual(document, before)

    def test_unknown_state_survives_load_thaw_freeze_save(self):
        context = Mock(username=None)
        state = anonymous_checkpoint()
        state["future_state"] = {"opaque": [1, "two"]}
        state["primary"]["future_cursor_metadata"] = {"revision": 5}
        primary = NodeIterator(context, "fixture-query", lambda data: data, lambda node: node,
                               query_variables={"id": "1234"},
                               query_referer="https://www.instagram.com/example/",
                               first_data=node_checkpoint()["remaining_data"])
        iterator = _AnonymousPostIterator(context, primary, 1234)
        iterator.thaw(load_structure(context, state))
        saved = get_json_structure(iterator.freeze())["node"]
        self.assertEqual(saved["future_state"], {"opaque": [1, "two"]})
        self.assertEqual(saved["primary"]["future_cursor_metadata"], {"revision": 5})
        self.assertEqual(saved["primary"]["remaining_data"]["page_info"]["end_cursor"], "saved-cursor")

    def test_malformed_state_is_rejected_before_thaw(self):
        for mutation in [
            {"total_index": -1}, {"total_index": True}, {"best_before": float("nan")},
            {"query_variables": []}, {"remaining_data": {"edges": "bad"}},
            {"remaining_data": {"edges": [], "page_info": {"has_next_page": True, "end_cursor": None}}},
        ]:
            with self.subTest(mutation=mutation):
                state = {**node_checkpoint(), **mutation}
                with self.assertRaises(InvalidArgumentException):
                    load_structure(Mock(), {"node": state, "instaloader": {"node_type": "FrozenNodeIterator"}})

    def test_rejected_checkpoint_is_preserved_with_recovery_record(self):
        context = Mock(username=None)
        state = node_checkpoint()
        state["query_variables"] = {"id": "9999"}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "resume.json"
            raw = json.dumps(state).encode()
            path.write_bytes(raw)
            iterator = NodeIterator(context, "fixture-query", lambda data: data, lambda node: node,
                                    query_variables={"id": "1234"},
                                    query_referer="https://www.instagram.com/example/",
                                    first_data={"edges": [], "page_info": {"has_next_page": False}})
            with resumable_iteration(context, iterator, load_structure_from_file,
                                     save_structure_to_file, lambda _: str(path)) as (resuming, _):
                self.assertFalse(resuming)
                self.assertEqual(list(iterator), [])
            recovered = list(Path(directory).glob("resume.json.quarantine-*"))
            artifacts = [p for p in recovered if not p.name.endswith(".diagnostic.json")]
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0].read_bytes(), raw)
            diagnostic = json.loads(Path(str(artifacts[0]) + ".diagnostic.json").read_text())
            self.assertEqual(diagnostic["original_path"], str(path))
            self.assertIn("Mismatching resume", diagnostic["reason"])


if __name__ == "__main__":
    unittest.main()
