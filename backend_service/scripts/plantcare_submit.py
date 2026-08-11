import argparse
import mimetypes
import os
import sys
import uuid
from pathlib import Path
from urllib import request
from urllib.error import HTTPError, URLError


PLANTCARE_URL = "https://bioinformatics.psb.ugent.be/webtools/plantcare/cgi-bin/CallMat_onCluster.htpl"


def build_multipart(fields, file_field=None):
    boundary = f"----PlantCAREBoundary{uuid.uuid4().hex}"
    chunks = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")

    if file_field is not None:
        field_name, file_path = file_field
        filename = os.path.basename(file_path)
        mime_type = mimetypes.guess_type(filename)[0] or "text/plain"
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        chunks.append(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
        chunks.append(file_bytes)
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--ref", default="")
    parser.add_argument("--sequence", default="")
    parser.add_argument("--file", dest="file_path", default="")
    parser.add_argument("--url", default=PLANTCARE_URL)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()

    sequence = args.sequence.strip()
    file_path = args.file_path.strip()

    if not sequence and not file_path:
        print("错误: --sequence 和 --file 至少提供一个。", file=sys.stderr)
        return 2

    if file_path:
        p = Path(file_path)
        if not p.exists() or not p.is_file():
            print(f"错误: 文件不存在 -> {file_path}", file=sys.stderr)
            return 2

    fields = {
        "Field_UserEmail": args.email.strip(),
        "Field_REF": args.ref,
        "Field_Sequence": sequence,
    }

    file_field = ("Field_File", file_path) if file_path else None
    body, content_type = build_multipart(fields, file_field=file_field)

    if args.dry_run:
        print("Dry run 模式：")
        print(f"POST URL: {args.url}")
        print(f"Email: {fields['Field_UserEmail']}")
        print(f"REF: {fields['Field_REF']}")
        print(f"Sequence length: {len(fields['Field_Sequence'])}")
        print(f"File: {file_path if file_path else '(none)'}")
        print(f"Body bytes: {len(body)}")
        return 0

    req = request.Request(args.url, method="POST", data=body)
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(body)))
    req.add_header("User-Agent", "plantcare-submit-script/1.0")

    try:
        with request.urlopen(req, timeout=args.timeout) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            print(f"HTTP {resp.status}")
            print(f"Final URL: {resp.geturl()}")
            print(content[:4000])
            if "something is wrong with your email address" in content.lower():
                print("服务端提示邮箱地址异常，请检查 --email。", file=sys.stderr)
                return 3
            return 0
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"HTTPError: {e.code}", file=sys.stderr)
        print(body_text[:2000], file=sys.stderr)
        return 1
    except URLError as e:
        print(f"URLError: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
