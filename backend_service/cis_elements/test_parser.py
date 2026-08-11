import io
import json
import tarfile
import tempfile
from pathlib import Path
from unittest import TestCase

from .parser import (
    PlantCareParseError,
    extract_tar_safely,
    parse_plantcare_tab,
    process_plantcare_attachments,
)


class PlantCareParserTests(TestCase):
    def test_parse_tab_returns_structured_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            table = Path(temp_dir) / 'result.tab'
            table.write_text(
                'gene1\tTATA-box\tTATA\t10\t4\t+\tArabidopsis\tcore promoter\n'
                'gene1\tMBS\tCAACTG\t50\t6\t-\tArabidopsis\tdrought\n'
                'gene2\tTATA-box\tTATAA\t20\t5\t+\tBrassica\tcore promoter\n',
                encoding='utf-8',
            )

            result = parse_plantcare_tab(table)

        self.assertEqual(result['record_count'], 3)
        self.assertEqual(result['sequence_count'], 2)
        self.assertEqual(result['element_counts']['TATA-box'], 2)
        self.assertEqual(result['records'][0]['position'], 10)

    def test_extract_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / 'unsafe.tar.gz'
            with tarfile.open(archive_path, 'w:gz') as archive:
                info = tarfile.TarInfo('../escape.txt')
                content = b'unsafe'
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

            with self.assertRaises(PlantCareParseError):
                extract_tar_safely(
                    archive_path,
                    Path(temp_dir) / 'output',
                    max_members=10,
                    max_file_size=1024,
                    max_total_size=2048,
                )

    def test_extract_rejects_oversized_member(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / 'large.tar.gz'
            with tarfile.open(archive_path, 'w:gz') as archive:
                info = tarfile.TarInfo('large.tab')
                content = b'x' * 32
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

            with self.assertRaises(PlantCareParseError):
                extract_tar_safely(
                    archive_path,
                    Path(temp_dir) / 'output',
                    max_members=10,
                    max_file_size=16,
                    max_total_size=1024,
                )

    def test_process_archive_writes_structured_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_path = root / 'plantcare.tar.gz'
            table_content = (
                b'gene1\tTATA-box\tTATA\t10\t4\t+\tArabidopsis\tcore promoter\n'
            )
            with tarfile.open(archive_path, 'w:gz') as archive:
                info = tarfile.TarInfo('plantcare_result.tab')
                info.size = len(table_content)
                archive.addfile(info, io.BytesIO(table_content))

            result = process_plantcare_attachments(
                [archive_path],
                root,
                max_members=10,
                max_file_size=1024,
                max_total_size=2048,
            )

            structured = json.loads(
                result['structured_path'].read_text(encoding='utf-8')
            )

        self.assertEqual(result['summary']['record_count'], 1)
        self.assertEqual(structured['element_counts']['TATA-box'], 1)
        self.assertTrue(
            any(path.name == 'plantcare_result.tab' for path in result['derived_files'])
        )
