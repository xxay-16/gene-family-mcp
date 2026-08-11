from __future__ import annotations

import csv
import json
import tarfile
from collections import Counter
from pathlib import Path, PurePosixPath


class PlantCareParseError(ValueError):
    pass


def _safe_member_path(name: str) -> PurePosixPath:
    normalized = name.replace('\\', '/')
    member_path = PurePosixPath(normalized)
    if member_path.is_absolute() or '..' in member_path.parts:
        raise PlantCareParseError(f'unsafe archive member path: {name}')
    if not member_path.parts or ':' in member_path.parts[0]:
        raise PlantCareParseError(f'unsafe archive member path: {name}')
    return member_path


def extract_tar_safely(
    archive_path: Path,
    output_dir: Path,
    *,
    max_members: int,
    max_file_size: int,
    max_total_size: int,
) -> list[Path]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total_size = 0

    try:
        archive = tarfile.open(archive_path, mode='r:*')
    except (tarfile.TarError, OSError) as exc:
        raise PlantCareParseError('invalid PlantCARE archive') from exc

    with archive:
        members = archive.getmembers()
        if len(members) > max_members:
            raise PlantCareParseError('archive contains too many members')
        for member in members:
            member_path = _safe_member_path(member.name)
            if member.isdir():
                continue
            if not member.isfile():
                raise PlantCareParseError(
                    f'unsupported archive member type: {member.name}'
                )
            if member.size > max_file_size:
                raise PlantCareParseError(
                    f'archive member exceeds size limit: {member.name}'
                )
            total_size += member.size
            if total_size > max_total_size:
                raise PlantCareParseError('archive exceeds total size limit')

            target = (output_dir / Path(*member_path.parts)).resolve()
            if not target.is_relative_to(output_dir):
                raise PlantCareParseError(
                    f'archive member escapes output directory: {member.name}'
                )
            source = archive.extractfile(member)
            if source is None:
                raise PlantCareParseError(
                    f'cannot read archive member: {member.name}'
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open('wb') as destination:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise PlantCareParseError(
                            f'truncated archive member: {member.name}'
                        )
                    destination.write(chunk)
                    remaining -= len(chunk)
            extracted.append(target)
    return extracted


def parse_plantcare_tab(path: Path) -> dict:
    records = []
    element_counts: Counter[str] = Counter()
    sequence_counts: Counter[str] = Counter()

    with path.open('r', encoding='utf-8', errors='replace', newline='') as stream:
        reader = csv.reader(stream, delimiter='\t')
        for row_number, row in enumerate(reader, start=1):
            if not row or not any(value.strip() for value in row):
                continue
            padded = row[:8] + [''] * max(0, 8 - len(row))
            sequence_id, element, motif, position, length, strand, organism, function = (
                value.strip() for value in padded[:8]
            )
            if not sequence_id or not element:
                continue
            try:
                position_value = int(position)
                length_value = int(length)
            except ValueError:
                continue
            record = {
                'sequence_id': sequence_id,
                'element': element,
                'motif_sequence': motif,
                'position': position_value,
                'length': length_value,
                'strand': strand,
                'organism': organism,
                'function': function,
                'source_row': row_number,
            }
            records.append(record)
            element_counts[element] += 1
            sequence_counts[sequence_id] += 1

    return {
        'source_file': path.name,
        'record_count': len(records),
        'sequence_count': len(sequence_counts),
        'element_type_count': len(element_counts),
        'element_counts': dict(element_counts.most_common()),
        'sequence_counts': dict(sequence_counts.most_common()),
        'records': records,
    }


def process_plantcare_attachments(
    attachments: list[Path],
    output_dir: Path,
    *,
    max_members: int,
    max_file_size: int,
    max_total_size: int,
) -> dict:
    derived_files: list[Path] = []
    tables = []
    extraction_dir = output_dir / 'extracted'

    for attachment in attachments:
        if tarfile.is_tarfile(attachment):
            derived_files.extend(
                extract_tar_safely(
                    attachment,
                    extraction_dir / attachment.stem.replace('.tar', ''),
                    max_members=max_members,
                    max_file_size=max_file_size,
                    max_total_size=max_total_size,
                )
            )
        elif attachment.suffix.lower() == '.tab':
            derived_files.append(attachment)

    for derived_file in derived_files:
        if derived_file.suffix.lower() == '.tab':
            tables.append(parse_plantcare_tab(derived_file))

    combined_elements: Counter[str] = Counter()
    combined_sequences: Counter[str] = Counter()
    for table in tables:
        combined_elements.update(table['element_counts'])
        combined_sequences.update(table['sequence_counts'])

    structured_result = {
        'table_count': len(tables),
        'record_count': sum(table['record_count'] for table in tables),
        'sequence_count': len(combined_sequences),
        'element_type_count': len(combined_elements),
        'element_counts': dict(combined_elements.most_common()),
        'sequence_counts': dict(combined_sequences.most_common()),
        'tables': tables,
    }
    structured_path = output_dir / 'plantcare_result.json'
    structured_path.write_text(
        json.dumps(structured_result, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return {
        'summary': {
            key: value
            for key, value in structured_result.items()
            if key != 'tables'
        },
        'structured_path': structured_path,
        'derived_files': derived_files,
    }
