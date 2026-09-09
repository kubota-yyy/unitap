import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from unitap_pkg import cli, discovery, editor_lock


class DiscoveryTests(unittest.TestCase):
    def invoke(self, argv, tool_response=None, unicli_response=None):
        parser, dispatch = cli.build_parser()
        args = parser.parse_args(['--project', '/chosen/project', '--json', *argv])
        with patch.object(discovery, 'send_request_to_current_transport', return_value=tool_response) as native, \
             patch.object(discovery, 'run_unicli', return_value=unicli_response) as unicli, \
             redirect_stdout(io.StringIO()):
            try:
                dispatch[args.command](args, None)
                code = 0
            except SystemExit as error:
                code = error.code
        return args._last_unitap_response, native, unicli, code

    def test_offline_catalog_uses_registered_parser_without_backend_calls(self):
        payload, native, unicli, code = self.invoke(['commands'])
        self.assertEqual(0, code)
        names = {c['name'] for c in payload['result']['commands']}
        self.assertTrue({'exec', 'eval', 'commands', 'describe', 'compile_check'} <= names)
        native.assert_not_called()
        unicli.assert_not_called()
        self.assertIsNone(payload['result']['sources']['tool']['available'])

    def test_extension_registration_is_discoverable(self):
        class Extension:
            @staticmethod
            def register(subparsers, dispatch):
                p = subparsers.add_parser('project_verify', help='Verify this project')
                p.add_argument('--suite', required=True, choices=['smoke', 'all'])
        with patch.object(cli, '_ext_module', Extension):
            payload, _, _, code = self.invoke(['describe', 'cli:project_verify'])
        self.assertEqual(0, code)
        parameter = payload['result']['command']['parameters'][0]
        self.assertTrue(parameter['required'])
        self.assertEqual(['smoke', 'all'], parameter['choices'])

    def test_live_catalog_namespaces_searches_and_stays_compact(self):
        native_response = {'ok': True, 'result': {'tools': [{'name': 'Compile', 'description': 'Inspect compile', 'parameters': []}]}}
        uni_response = {'ok': True, 'result': {'response': {'data': [{'name': 'Compile', 'module': 'Editor', 'requestFields': []}]}}}
        payload, native, unicli, code = self.invoke(['commands', '--live', '--search', 'COMPILE'], native_response, uni_response)
        self.assertEqual(0, code)
        ids = {c['id'] for c in payload['result']['commands']}
        self.assertTrue({'tool:Compile', 'unicli:Compile', 'cli:compile_check'} <= ids)
        self.assertTrue(all('parameters' not in c for c in payload['result']['commands']))
        self.assertFalse(payload['result']['partial'])
        self.assertEqual('/chosen/project', native.call_args.args[0])
        self.assertFalse(native.call_args.args[1]['retryable'])
        unicli.assert_called_once_with('/chosen/project', 'commands', timeout_ms=5000)

    def test_missing_backend_is_reported_without_losing_cli_catalog(self):
        error = {'ok': False, 'error': {'code': 'unavailable', 'message': 'offline'}}
        payload, _, _, code = self.invoke(['commands', '--live'], error, error)
        self.assertEqual(0, code)
        self.assertTrue(payload['result']['partial'])
        self.assertGreater(payload['result']['count'], 0)
        self.assertEqual('unavailable', payload['result']['sources']['unicli']['error']['code'])

    def test_describe_only_queries_requested_backend_and_keeps_nested_schema(self):
        schema = {'name': 'GameObject.Find', 'requestFields': [{'name': 'name', 'type': 'string'}],
                  'responseTypeDetails': [{'typeName': 'FoundObject', 'fields': []}]}
        payload, native, _, code = self.invoke(['describe', 'unicli:GameObject.Find'],
            unicli_response={'ok': True, 'result': {'response': {'data': [schema]}}})
        self.assertEqual(0, code)
        native.assert_not_called()
        self.assertEqual(schema['requestFields'], payload['result']['command']['parameters'])
        self.assertEqual(schema['responseTypeDetails'], payload['result']['command']['responseTypeDetails'])

    def test_unknown_command_and_unavailable_backend_are_distinct(self):
        payload, _, _, code = self.invoke(['describe', 'does_not_exist'])
        self.assertEqual((1, 'command_not_found'), (code, payload['error']['code']))
        payload, _, _, code = self.invoke(['describe', 'tool:find_assets'], {'ok': False, 'error': {'code': 'offline'}})
        self.assertEqual((1, 'backend_unavailable'), (code, payload['error']['code']))

    def test_malformed_backend_catalog_is_partial_not_successful_empty_list(self):
        payload, _, _, _ = self.invoke(['commands', '--live', '--backend', 'unicli'],
            unicli_response={'ok': True, 'result': {'response': {'data': {'wrong': 'shape'}}}})
        self.assertTrue(payload['result']['partial'])
        self.assertEqual('invalid_catalog', payload['result']['sources']['unicli']['error']['code'])

    def test_invalid_timeout_does_not_touch_backends(self):
        payload, native, unicli, code = self.invoke(['commands', '--live', '--timeout-ms', '0'])
        self.assertEqual((1, 'invalid_timeout'), (code, payload['error']['code']))
        native.assert_not_called()
        unicli.assert_not_called()

    def test_malformed_envelope_does_not_crash_discovery(self):
        for response in [[], {'ok': True, 'result': None}, {'ok': True, 'result': {'tools': [None]}}]:
            with self.subTest(response=response):
                payload, _, _, code = self.invoke(['commands', '--live', '--backend', 'tool'], response)
                self.assertEqual(0, code)
                self.assertTrue(payload['result']['partial'])
                self.assertEqual('invalid_catalog', payload['result']['sources']['tool']['error']['code'])

    def test_ambiguous_tool_description_is_not_arbitrarily_selected(self):
        payload, _, _, code = self.invoke(['describe', 'tool:duplicate'], {'ok': True, 'result': {'tools': [
            {'name': 'duplicate', 'className': 'First'}, {'name': 'duplicate', 'className': 'Second'}]}})
        self.assertEqual((1, 'ambiguous_command'), (code, payload['error']['code']))

    def test_transport_failure_is_reported_without_retry(self):
        with patch.object(discovery, 'send_request_to_current_transport', side_effect=TimeoutError('timed out')) as send:
            entries, state = discovery._live_entries('/chosen/project', 'tool', 200)
        self.assertEqual([], entries)
        self.assertFalse(state['available'])
        send.assert_called_once()

    def test_catalog_works_in_empty_directory_without_project_or_unicli(self):
        entrypoint = Path(__file__).resolve().parents[1] / 'unitap.py'
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run([sys.executable, str(entrypoint), '--json', 'commands'], cwd=temp,
                env=dict(os.environ, UNITAP_UNICLI_BIN='/does/not/exist'), capture_output=True, text=True, timeout=10)
            self.assertEqual([], list(Path(temp).iterdir()))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIsNone(json.loads(result.stdout)['result']['projectPath'])

    def test_discovery_does_not_wait_for_project_lock(self):
        entrypoint = Path(__file__).resolve().parents[1] / 'unitap.py'
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / 'Assets').mkdir()
            (project / 'ProjectSettings').mkdir()
            with editor_lock.editor_operation_lock(project, {'command': 'compile_check'}, wait=False, timeout_s=0):
                result = subprocess.run([sys.executable, str(entrypoint), '--project', temp,
                    '--json', 'commands'], capture_output=True, text=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse((project / 'Library/Unitap/execution-history.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
