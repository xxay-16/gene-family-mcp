import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from .local_tools.capabilities import _probe, resolve_executable
from .local_tools.mafft import ToolExecutionError, run_mafft


class ToolCapabilityTests(SimpleTestCase):
    def tearDown(self):
        _probe.cache_clear()

    def test_resolve_absolute_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / 'mafft'
            executable.write_text('placeholder', encoding='utf-8')

            self.assertEqual(resolve_executable(str(executable)), str(executable))
            self.assertIsNone(resolve_executable(str(executable) + '.missing'))

    @override_settings(TOOL_PROBE_TIMEOUT=2)
    @patch('jobs.local_tools.capabilities.resolve_executable', return_value='/bin/mafft')
    @patch('jobs.local_tools.capabilities.subprocess.run')
    def test_probe_reports_version(self, run_mock, _resolve_mock):
        run_mock.return_value = Mock(
            returncode=0,
            stdout='',
            stderr='v7.525\n',
        )

        result = _probe('mafft-test', '--version')

        self.assertTrue(result['available'])
        self.assertEqual(result['version'], 'v7.525')
        self.assertNotIn('resolved_executable', result)
        self.assertNotIn('configured_executable', result)


class MafftAdapterTests(SimpleTestCase):
    @override_settings(
        MAFFT_EXECUTABLE='mafft',
        MAX_TOOL_THREADS=4,
        MAX_ALIGNMENT_OUTPUT_BYTES=1024,
    )
    @patch('jobs.local_tools.mafft.mafft_capability', return_value={'version': 'v-test'})
    @patch('jobs.local_tools.mafft.resolve_executable', return_value='/bin/mafft')
    @patch('jobs.local_tools.mafft.subprocess.run')
    def test_run_mafft_builds_argument_list_and_writes_output(
        self,
        run_mock,
        _resolve_mock,
        _capability_mock,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / 'input.fasta'
            output_path = Path(temp_dir) / 'aligned.fasta'
            input_path.write_text('>a\nACGT\n>b\nACGT\n', encoding='utf-8')

            def runner(command, **kwargs):
                kwargs['stdout'].write(b'>a\nACGT\n>b\nACGT\n')
                return Mock(returncode=0, stderr=b'progress')

            run_mock.side_effect = runner
            result = run_mafft(
                input_path,
                output_path,
                strategy='linsi',
                threads=2,
                timeout=30,
            )

            command = run_mock.call_args.args[0]
            self.assertEqual(command[0], '/bin/mafft')
            self.assertIn('--localpair', command)
            self.assertEqual(command[-1], str(input_path))
            self.assertEqual(output_path.read_bytes(), b'>a\nACGT\n>b\nACGT\n')
            self.assertEqual(result['version'], 'v-test')

    @override_settings(
        MAFFT_EXECUTABLE='mafft',
        MAX_TOOL_THREADS=4,
        MAX_ALIGNMENT_OUTPUT_BYTES=1024,
    )
    @patch('jobs.local_tools.mafft.resolve_executable', return_value='/bin/mafft')
    @patch('jobs.local_tools.mafft.subprocess.run')
    def test_run_mafft_timeout_removes_partial_output(
        self,
        run_mock,
        _resolve_mock,
    ):
        run_mock.side_effect = subprocess.TimeoutExpired(['/bin/mafft'], 1)
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / 'input.fasta'
            output_path = Path(temp_dir) / 'aligned.fasta'
            input_path.write_text('>a\nACGT\n>b\nACGT\n', encoding='utf-8')

            with self.assertRaisesRegex(ToolExecutionError, 'execution timeout'):
                run_mafft(
                    input_path,
                    output_path,
                    strategy='auto',
                    threads=1,
                    timeout=1,
                )

            self.assertFalse(output_path.exists())

    @override_settings(
        MAFFT_EXECUTABLE='mafft',
        MAX_TOOL_THREADS=4,
        MAX_ALIGNMENT_OUTPUT_BYTES=1024,
    )
    @patch('jobs.local_tools.mafft.resolve_executable', return_value='/bin/mafft')
    @patch('jobs.local_tools.mafft.subprocess.run')
    def test_run_mafft_failure_does_not_expose_stderr(
        self,
        run_mock,
        _resolve_mock,
    ):
        run_mock.return_value = Mock(
            returncode=2,
            stderr=b'secret path C:/private/input.fasta',
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / 'input.fasta'
            output_path = Path(temp_dir) / 'aligned.fasta'
            input_path.write_text('>a\nACGT\n>b\nACGT\n', encoding='utf-8')

            with self.assertRaises(ToolExecutionError) as raised:
                run_mafft(
                    input_path,
                    output_path,
                    strategy='auto',
                    threads=1,
                    timeout=30,
                )

        self.assertEqual(str(raised.exception), 'MAFFT exited with code 2')
