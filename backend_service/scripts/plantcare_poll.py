import argparse
import email
import imaplib
import os
import random
import re
import string
import sys
import time
from email.header import decode_header
from pathlib import Path
from urllib import request
from urllib.error import HTTPError, URLError

from plantcare_submit import PLANTCARE_URL, build_multipart

HOST_MAP = {
    "gmail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "qq.com": "imap.qq.com",
    "163.com": "imap.163.com",
    "126.com": "imap.126.com",
}


def decode_text(value):
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def infer_host(email_address):
    if "@" not in email_address:
        return ""
    domain = email_address.split("@", 1)[1].lower()
    return HOST_MAP.get(domain, "")


def extract_text(message):
    if message.is_multipart():
        chunks = []
        for part in message.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                if ctype == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                chunks.append(text.strip())
        return "\n".join([x for x in chunks if x]).strip()
    payload = message.get_payload(decode=True)
    if not payload:
        return ""
    charset = message.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="replace")
    if message.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    return text.strip()


def save_attachments(message, output_dir):
    saved = []
    for part in message.walk():
        disp = str(part.get("Content-Disposition") or "").lower()
        if "attachment" not in disp:
            continue
        filename = decode_text(part.get_filename() or "attachment.bin")
        data = part.get_payload(decode=True)
        if data is None:
            continue
        out_path = output_dir / filename
        stem = out_path.stem
        suffix = out_path.suffix
        index = 1
        while out_path.exists():
            out_path = output_dir / f"{stem}_{index}{suffix}"
            index += 1
        out_path.write_bytes(data)
        saved.append(str(out_path))
    return saved


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", default=os.environ.get("MAIL_PASSWORD", ""))
    parser.add_argument("--imap-host", default="")
    parser.add_argument("--port", type=int, default=993)
    parser.add_argument("--folder", default="INBOX")
    parser.add_argument("--search", default="UNSEEN")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--subject-contains", default="")
    parser.add_argument("--save-attachments-dir", default="")
    parser.add_argument("--submit-url", default=PLANTCARE_URL)
    parser.add_argument("--ref", default="")
    parser.add_argument("--sequence", default="")
    default_fasta = Path(__file__).resolve().parent.parent / 'tests' / 'fixtures' / 'test.fa'
    parser.add_argument("--file", dest="file_path", default=str(default_fasta))
    parser.add_argument("--submit-timeout", type=int, default=120)
    parser.add_argument("--poll-interval", type=int, default=10)
    parser.add_argument("--max-polls", type=int, default=60)
    return parser.parse_args()


def make_random_ref(size=12):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(size))


def submit_job(args):
    sequence = args.sequence.strip()
    file_path = args.file_path.strip()

    if not sequence and not file_path:
        print("错误: --sequence 和 --file 至少提供一个。", file=sys.stderr)
        return False, ""
    if file_path:
        p = Path(file_path)
        if not p.exists() or not p.is_file():
            print(f"错误: 文件不存在 -> {file_path}", file=sys.stderr)
            return False, ""

    fields = {
        "Field_UserEmail": args.email.strip(),
        "Field_REF": args.ref,
        "Field_Sequence": sequence,
    }
    file_field = ("Field_File", file_path) if file_path else None
    body, content_type = build_multipart(fields, file_field=file_field)

    req = request.Request(args.submit_url, method="POST", data=body)
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(body)))
    req.add_header("User-Agent", "plantcare-submit-and-mail-poll/1.0")

    try:
        with request.urlopen(req, timeout=args.submit_timeout) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            print(f"提交HTTP状态码: {resp.status}")
            print(f"提交返回URL: {resp.geturl()}")
            print("提交响应(前2000字符):")
            print(content[:2000])
            ok = "has been submitted" in content.lower()
            if "something is wrong with your email address" in content.lower():
                print("服务端提示邮箱地址异常。", file=sys.stderr)
                return False, content
            return ok, content
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"提交HTTPError: {e.code}", file=sys.stderr)
        print(body_text[:2000], file=sys.stderr)
        return False, ""
    except URLError as e:
        print(f"提交URLError: {e}", file=sys.stderr)
        return False, ""


def get_all_uids(imap):
    status, data = imap.uid("search", None, "ALL")
    if status != "OK":
        return []
    raw = data[0].strip()
    if not raw:
        return []
    return raw.split()


def main():
    args = parse_args()

    if not args.password:
        print("错误: 请通过 --password 或环境变量 MAIL_PASSWORD 提供邮箱密码/授权码。", file=sys.stderr)
        return 2

    host = args.imap_host.strip() or infer_host(args.email.strip())
    if not host:
        print("错误: 无法自动推断 IMAP 主机，请通过 --imap-host 指定。", file=sys.stderr)
        return 2

    if not args.ref.strip():
        args.ref = make_random_ref()
    else:
        args.ref = args.ref.strip()
    print(f"本次提交随机标识(ref): {args.ref}")

    if args.save_attachments_dir:
        save_dir = Path(args.save_attachments_dir)
    else:
        save_dir = Path.cwd() / f"plantcare_attachments_{args.ref}"
    save_dir.mkdir(parents=True, exist_ok=True)
    print(f"附件下载目录: {save_dir}")

    submitted, _ = submit_job(args)
    if not submitted:
        print("提交未确认成功，停止轮询。", file=sys.stderr)
        return 1

    try:
        with imaplib.IMAP4_SSL(host, args.port) as imap:
            imap.login(args.email, args.password)
            status, _ = imap.select(args.folder)
            if status != "OK":
                print(f"错误: 无法打开邮箱文件夹 {args.folder}", file=sys.stderr)
                return 1

            print(f"开始轮询邮件，间隔{args.poll_interval}s，最多{args.max_polls}轮，按ref搜索: {args.ref}")

            for idx in range(1, args.max_polls + 1):
                time.sleep(args.poll_interval)
                status, data = imap.uid("search", None, "TEXT", f'"{args.ref}"')
                if status != "OK":
                    print(f"第{idx}轮: 搜索失败")
                    continue
                matched_uids = data[0].split() if data and data[0] else []
                if not matched_uids:
                    print(f"第{idx}轮: 未搜索到包含ref的新邮件")
                    continue

                matched = 0
                for uid in matched_uids[::-1]:
                    status, msg_data = imap.uid("fetch", uid, "(RFC822)")
                    if status != "OK" or not msg_data or msg_data[0] is None:
                        continue
                    raw_email = msg_data[0][1]
                    message = email.message_from_bytes(raw_email)
                    subject = decode_text(message.get("Subject", ""))
                    sender = decode_text(message.get("From", ""))
                    date = decode_text(message.get("Date", ""))
                    body_text = extract_text(message)
                    ref_lower = args.ref.lower()
                    if ref_lower not in subject.lower() and ref_lower not in body_text.lower():
                        continue

                    matched += 1
                    print("=" * 70)
                    print(f"UID: {uid.decode()}")
                    print(f"From: {sender}")
                    print(f"Date: {date}")
                    print(f"Subject: {subject}")
                    print("-" * 70)
                    print(body_text[:3000] if body_text else "(无正文或正文为空)")

                    saved = save_attachments(message, save_dir)
                    print("-" * 70)
                    if saved:
                        print("附件已保存:")
                        for path in saved:
                            print(path)
                    else:
                        print("该邮件无附件。")

                if matched > 0:
                    return 0
                print(f"第{idx}轮: 未发现正文或标题包含ref的邮件")

            print("轮询结束，未等到匹配邮件。")
            return 0
    except imaplib.IMAP4.error as e:
        print(f"IMAP错误: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"网络错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
