import email
import imaplib
import mimetypes
import random
import re
import string
import time
import uuid
from pathlib import Path
from urllib import request
from urllib.error import HTTPError, URLError

from django.conf import settings


class PlantCareProviderError(RuntimeError):
    pass


def _build_multipart(fields, file_field=None):
    boundary = f'----PlantCAREBoundary{uuid.uuid4().hex}'
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\n'.encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode('utf-8'))
        chunks.append(b'\r\n')
    if file_field is not None:
        field_name, file_path = file_field
        filename = Path(file_path).name
        mime_type = mimetypes.guess_type(filename)[0] or 'text/plain'
        file_bytes = Path(file_path).read_bytes()
        chunks.append(f'--{boundary}\r\n'.encode())
        chunks.append(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode())
        chunks.append(f'Content-Type: {mime_type}\r\n\r\n'.encode())
        chunks.append(file_bytes)
        chunks.append(b'\r\n')
    chunks.append(f'--{boundary}--\r\n'.encode())
    return b''.join(chunks), f'multipart/form-data; boundary={boundary}'


def _decode_text(value):
    if not value:
        return ''
    parts = email.header.decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or 'utf-8', errors='replace'))
        else:
            out.append(text)
    return ''.join(out)


def _extract_text(message):
    if message.is_multipart():
        chunks = []
        for part in message.walk():
            ctype = part.get_content_type()
            disp = str(part.get('Content-Disposition') or '').lower()
            if 'attachment' in disp:
                continue
            if ctype in ('text/plain', 'text/html'):
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or 'utf-8'
                text = payload.decode(charset, errors='replace')
                if ctype == 'text/html':
                    text = re.sub(r'<[^>]+>', ' ', text)
                chunks.append(text.strip())
        return '\n'.join([x for x in chunks if x]).strip()
    payload = message.get_payload(decode=True)
    if not payload:
        return ''
    charset = message.get_content_charset() or 'utf-8'
    text = payload.decode(charset, errors='replace')
    if message.get_content_type() == 'text/html':
        text = re.sub(r'<[^>]+>', ' ', text)
    return text.strip()


def _save_attachments(message, output_dir):
    saved = []
    for part in message.walk():
        disp = str(part.get('Content-Disposition') or '').lower()
        if 'attachment' not in disp:
            continue
        filename = Path(_decode_text(part.get_filename() or 'attachment.bin')).name
        data = part.get_payload(decode=True)
        if data is None:
            continue
        out_path = output_dir / filename
        stem = out_path.stem
        suffix = out_path.suffix
        index = 1
        while out_path.exists():
            out_path = output_dir / f'{stem}_{index}{suffix}'
            index += 1
        out_path.write_bytes(data)
        saved.append(str(out_path))
    return saved


def _make_random_ref(size=12):
    alphabet = string.ascii_lowercase + string.digits
    return ''.join(random.choice(alphabet) for _ in range(size))


def submit_prediction(sequence: str, ref: str | None = None) -> str:
    email_address = settings.PLANTCARE_EMAIL.strip()
    auth_code = settings.PLANTCARE_AUTH_CODE.strip()
    if not email_address:
        raise ValueError('PLANTCARE_EMAIL 未配置')
    if not auth_code:
        raise ValueError('PLANTCARE_AUTH_CODE 未配置')
    sequence = sequence.strip()
    if not sequence:
        raise ValueError('序列不能为空')

    ref = ref or _make_random_ref()
    fields = {
        'Field_UserEmail': email_address,
        'Field_REF': ref,
        'Field_Sequence': sequence,
    }
    body, content_type = _build_multipart(fields, file_field=None)
    req = request.Request(settings.PLANTCARE_SUBMIT_URL, method='POST', data=body)
    req.add_header('Content-Type', content_type)
    req.add_header('Content-Length', str(len(body)))
    req.add_header('User-Agent', 'ninja-service-plantcare/1.0')

    try:
        with request.urlopen(req, timeout=120) as resp:
            content = resp.read().decode('utf-8', errors='replace')
            if 'something is wrong with your email address' in content.lower():
                raise PlantCareProviderError('PlantCARE 提示邮箱地址异常')
            if 'has been submitted' not in content.lower():
                raise PlantCareProviderError('PlantCARE 提交未确认成功')
    except HTTPError as e:
        body_text = e.read().decode('utf-8', errors='replace')
        raise PlantCareProviderError(
            f'提交失败 HTTP {e.code}: {body_text[:500]}'
        ) from e
    except URLError as e:
        raise PlantCareProviderError(f'提交失败: {e}') from e
    return ref


def collect_results(ref_to_output_dir: dict[str, Path]):
    if not ref_to_output_dir:
        return {}
    email_address = settings.PLANTCARE_EMAIL.strip()
    auth_code = settings.PLANTCARE_AUTH_CODE.strip()
    if not email_address:
        raise ValueError('PLANTCARE_EMAIL 未配置')
    if not auth_code:
        raise ValueError('PLANTCARE_AUTH_CODE 未配置')

    pending = {ref.lower(): (ref, Path(output_dir)) for ref, output_dir in ref_to_output_dir.items()}
    results = {}
    with imaplib.IMAP4_SSL(settings.PLANTCARE_IMAP_HOST, settings.PLANTCARE_IMAP_PORT) as imap:
        imap.login(email_address, auth_code)
        status, _ = imap.select(settings.PLANTCARE_IMAP_FOLDER)
        if status != 'OK':
            raise RuntimeError(f'无法打开邮箱文件夹: {settings.PLANTCARE_IMAP_FOLDER}')
        for ref_lower, (ref, save_dir) in pending.items():
            status, data = imap.uid('search', None, 'TEXT', f'"{ref}"')
            if status != 'OK':
                continue
            matched_uids = data[0].split() if data and data[0] else []
            if not matched_uids:
                continue
            for uid in matched_uids[::-1]:
                status, msg_data = imap.uid('fetch', uid, '(RFC822)')
                if status != 'OK' or not msg_data or msg_data[0] is None:
                    continue
                raw_email = msg_data[0][1]
                message = email.message_from_bytes(raw_email)
                subject = _decode_text(message.get('Subject', ''))
                date = _decode_text(message.get('Date', ''))
                body_text = _extract_text(message)
                ref_lower = ref.lower()
                if ref_lower not in subject.lower() and ref_lower not in body_text.lower():
                    continue
                save_dir.mkdir(parents=True, exist_ok=True)
                attachments = _save_attachments(message, save_dir)
                results[ref] = {
                    'ref': ref,
                    'subject': subject,
                    'date': date,
                    'attachments': attachments,
                }
                break
    return results


def collect_result(ref: str, output_dir) -> dict | None:
    return collect_results({ref: Path(output_dir)}).get(ref)


def run_prediction(
    sequence: str,
    output_dir=None,
    on_submitted=None,
    on_result_received=None,
):
    """Compatibility helper for manual scripts; production uses submit + scheduler."""
    ref = submit_prediction(sequence)
    if on_submitted is not None:
        on_submitted(ref)
    save_dir = (
        Path(output_dir)
        if output_dir is not None
        else Path(settings.ARTIFACT_ROOT) / ref
    )
    for _ in range(settings.PLANTCARE_MAX_POLLS):
        time.sleep(settings.PLANTCARE_POLL_INTERVAL)
        result = collect_result(ref, save_dir)
        if result is not None:
            if on_result_received is not None:
                on_result_received()
            return result
    raise TimeoutError('等待结果超时，请稍后重试')


def run_prediction_task(sequence: str):
    """Legacy django-q2 entry point retained for compatibility."""
    return run_prediction(sequence)
