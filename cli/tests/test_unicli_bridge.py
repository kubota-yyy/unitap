import argparse
import io
import json
import subprocess
import unittest
import tempfile
import sys
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from unitap_pkg import unicli_bridge, editor_lock


class UniCliBridgeTests(unittest.TestCase):
    def invoke(self, operation='eval', forwarded=None, response=None, returncode=0, error=None):
        args = argparse.Namespace(command='unicli', project='/chosen/project', json=True,
                                  timeout_ms=1200, operation=operation,
                                  unicli_args=forwarded if forwarded is not None else ['return 1;'])
        result = subprocess.CompletedProcess([], returncode,
                    json.dumps(response if response is not None else {'success': True, 'data': 1}), '')
        with patch.object(unicli_bridge.shutil, 'which', return_value='/bin/unicli'), \
             patch.object(unicli_bridge.subprocess, 'run', return_value=result, side_effect=error) as run, \
             patch.dict('os.environ', {'UNICLI_PROJECT': '/wrong/project'}), \
             redirect_stdout(io.StringIO()):
            try:
                unicli_bridge.do_unicli(args)
                exit_code = 0
            except SystemExit as ex:
                exit_code = ex.code
        return args._last_unitap_response, run, exit_code

    def test_code_stays_single_argument_and_project_matches_lock(self):
        code = 'return "$HOME `hello` $(id)\\n";'
        payload, run, code_out = self.invoke(forwarded=[code])
        self.assertEqual(0, code_out)
        self.assertEqual(['/bin/unicli', 'eval', code, '--json', '--timeout', '1200', '--no-focus'], run.call_args.args[0])
        self.assertEqual('/chosen/project', run.call_args.kwargs['env']['UNICLI_PROJECT'])
        self.assertEqual('/chosen/project', run.call_args.kwargs['cwd'])
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual('unicli', payload['result']['backend'])

    def test_semantic_failure_and_nonzero_exit_are_failures(self):
        for response, exit_code in [({'success': False, 'message': 'compile failed'}, 0), ({'success': True}, 3)]:
            with self.subTest(response=response, exit_code=exit_code):
                payload, run, code = self.invoke(response=response, returncode=exit_code)
                self.assertEqual(1, code)
                self.assertEqual('unicli_failed', payload['error']['code'])
                run.assert_called_once()

    def test_timeout_does_not_retry_or_fallback(self):
        payload, run, code = self.invoke(error=subprocess.TimeoutExpired('unicli', 1.2))
        self.assertEqual(1, code)
        self.assertEqual('unicli_timeout', payload['error']['code'])
        self.assertIn('may still be executing', payload['error']['message'])
        run.assert_called_once()

    def test_wrong_project_or_timeout_cannot_bypass_wrapper(self):
        for option in ['--project=/other', '--project', '--timeout=9', '--timeout']:
            with self.subTest(option=option):
                payload, run, code = self.invoke(forwarded=['Command', option])
                self.assertEqual(1, code)
                self.assertEqual('conflicting_option', payload['error']['code'])
                run.assert_not_called()

    def test_non_object_json_is_rejected(self):
        payload, _, code = self.invoke(response=[1, 2])
        self.assertEqual(1, code)
        self.assertEqual('unicli_invalid_response', payload['error']['code'])

    def test_cli_does_not_launch_unicli_while_project_is_locked(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / 'Assets').mkdir()
            (project / 'ProjectSettings').mkdir()
            (project / 'ProjectSettings/ProjectVersion.txt').write_text('m_EditorVersion: 6000.6.0f1')
            cli = Path(__file__).resolve().parents[1] / 'unitap.py'
            with editor_lock.editor_operation_lock(project, {'command': 'compile_check'}, wait=False, timeout_s=0):
                result = subprocess.run([sys.executable, str(cli), '--project', str(project),
                    '--no-wait-lock', '--json', 'unicli', 'exec', 'PlayMode.Status'], capture_output=True, text=True, timeout=10)
            self.assertEqual(1, result.returncode)
            self.assertEqual('editor_busy', json.loads(result.stdout)['error']['code'])

    def test_parser_and_lock_registration(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest='command')
        dispatch = {}
        unicli_bridge.register(subparsers, dispatch)
        args = parser.parse_args(['unicli', '--timeout-ms', '3000', 'exec', 'TestRunner.RunEditMode', '--assemblies', 'RabbitPunch.Tests'])
        self.assertEqual(3000, args.timeout_ms)
        self.assertEqual(['TestRunner.RunEditMode', '--assemblies', 'RabbitPunch.Tests'], args.unicli_args)
        self.assertTrue(args._skip_heartbeat)
        self.assertTrue(editor_lock.command_requires_editor_lock(args))
        self.assertIs(unicli_bridge.do_unicli, dispatch['unicli'])


if __name__ == '__main__':
    unittest.main()
