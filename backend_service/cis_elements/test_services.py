import email
import io
import tempfile
from email.message import EmailMessage
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from django.test import override_settings

from .services import (
    PlantCareProviderError,
    _build_multipart,
    _decode_text,
    _extract_text,
    _save_attachments,
    collect_results,
    submit_prediction,
)


class _HTTPResponse:
    def __init__(self, body: str, status: int = 200):
        self.body = body.encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body


class _FakeIMAP:
    def __init__(self, messages=None, select_status='OK'):
        self.messages = messages or {}
        self.select_status = select_status
        self.login_args = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def login(self, email_address, auth_code):
        self.login_args = (email_address, auth_code)

    def select(self, folder):
        return self.select_status, [b'']

    def uid(self, operation, *args):
        if operation == 'search':
            ref = args[-1].strip('"').lower()
            uids = [
                uid
                for uid, item in self.messages.items()
                if ref in item['ref'].lower()
            ]
            return 'OK', [b' '.join(uids)]
        if operation == 'fetch':
            uid = args[0]
            return 'OK', [(b'RFC822', self.messages[uid]['raw'])]
        raise AssertionError(operation)


def _result_email(ref: str, filename: str = 'result.tab') -> bytes:
    message = EmailMessage()
    message['Subject'] = f'PlantCARE result {ref}'
    message['From'] = 'plantcare@example.org'
    message['Date'] = 'Tue, 11 Aug 2026 12:00:00 +0000'
    message.set_content(f'Your PlantCARE reference is {ref}.')
    message.add_attachment(
        b'gene1\tTATA-box\tTATA\t10\t4\t+\tArabidopsis\tcore\n',
        maintype='text',
        subtype='tab-separated-values',
        filename=filename,
    )
    return message.as_bytes()


class PlantCareServiceTests(TestCase):
    def test_multipart_supports_fields_and_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / 'input.fa'
            file_path.write_text('>gene\nACGT\n', encoding='utf-8')
            body, content_type = _build_multipart(
                {'Field_REF': 'ref-1'},
                ('Field_File', file_path),
            )

        self.assertIn(b'Field_REF', body)
        self.assertIn(b'input.fa', body)
        self.assertIn(b'>gene', body)
        self.assertIn(b'ACGT', body)
        self.assertTrue(content_type.startswith('multipart/form-data; boundary='))

    def test_mail_helpers_decode_extract_and_deduplicate_attachments(self):
        message = email.message_from_bytes(_result_email('ref-1', '../result.tab'))
        self.assertIn('ref-1', _extract_text(message))
        self.assertEqual(_decode_text('plain'), 'plain')

        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / 'result.tab').write_text('existing', encoding='utf-8')
            saved = _save_attachments(message, output_dir)

            self.assertEqual(Path(saved[0]).name, 'result_1.tab')
            self.assertTrue(Path(saved[0]).is_file())
            self.assertEqual(Path(saved[0]).parent, output_dir)

    @override_settings(
        PLANTCARE_EMAIL='test@example.com',
        PLANTCARE_AUTH_CODE='auth-code',
        PLANTCARE_SUBMIT_URL='https://plantcare.test/submit',
    )
    @patch('cis_elements.services.request.urlopen')
    def test_submit_prediction_success(self, urlopen_mock):
        urlopen_mock.return_value = _HTTPResponse('Your job has been submitted')

        ref = submit_prediction('ACGT', ref='fixed-ref')

        self.assertEqual(ref, 'fixed-ref')
        submitted_request = urlopen_mock.call_args.args[0]
        self.assertEqual(submitted_request.full_url, 'https://plantcare.test/submit')
        self.assertIn(b'fixed-ref', submitted_request.data)

    @override_settings(PLANTCARE_EMAIL='', PLANTCARE_AUTH_CODE='auth-code')
    def test_submit_requires_email_configuration(self):
        with self.assertRaisesRegex(ValueError, 'PLANTCARE_EMAIL'):
            submit_prediction('ACGT')

    @override_settings(
        PLANTCARE_EMAIL='test@example.com',
        PLANTCARE_AUTH_CODE='auth-code',
    )
    @patch('cis_elements.services.request.urlopen')
    def test_submit_maps_provider_and_network_errors(self, urlopen_mock):
        urlopen_mock.return_value = _HTTPResponse(
            'Something is wrong with your email address'
        )
        with self.assertRaises(PlantCareProviderError):
            submit_prediction('ACGT')

        urlopen_mock.side_effect = HTTPError(
            'https://plantcare.test',
            500,
            'error',
            None,
            io.BytesIO(b'provider error'),
        )
        with self.assertRaisesRegex(PlantCareProviderError, 'HTTP 500'):
            submit_prediction('ACGT')

        urlopen_mock.side_effect = URLError('offline')
        with self.assertRaisesRegex(PlantCareProviderError, 'offline'):
            submit_prediction('ACGT')

    @override_settings(
        PLANTCARE_EMAIL='test@example.com',
        PLANTCARE_AUTH_CODE='auth-code',
        PLANTCARE_IMAP_HOST='imap.test',
        PLANTCARE_IMAP_PORT=993,
        PLANTCARE_IMAP_FOLDER='INBOX',
    )
    @patch('cis_elements.services.imaplib.IMAP4_SSL')
    def test_collect_results_downloads_matching_mail(self, imap_mock):
        fake_imap = _FakeIMAP(
            {b'7': {'ref': 'ref-7', 'raw': _result_email('ref-7')}}
        )
        imap_mock.return_value = fake_imap

        with tempfile.TemporaryDirectory() as temp_dir:
            results = collect_results({'ref-7': Path(temp_dir)})

            self.assertEqual(results['ref-7']['ref'], 'ref-7')
            self.assertTrue(Path(results['ref-7']['attachments'][0]).is_file())
        self.assertEqual(fake_imap.login_args, ('test@example.com', 'auth-code'))

    @override_settings(
        PLANTCARE_EMAIL='test@example.com',
        PLANTCARE_AUTH_CODE='auth-code',
    )
    @patch('cis_elements.services.imaplib.IMAP4_SSL')
    def test_collect_results_handles_empty_and_invalid_folder(self, imap_mock):
        self.assertEqual(collect_results({}), {})
        imap_mock.return_value = _FakeIMAP(select_status='NO')
        with self.assertRaisesRegex(RuntimeError, '无法打开邮箱文件夹'):
            collect_results({'ref': Path('unused')})
