from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

DNA_ALPHABET = frozenset('ACGTRYSWKMBDHVN')
PROTEIN_ALPHABET = frozenset('ABCDEFGHIKLMNPQRSTVWXYZJUO*')
SUPPORTED_ALPHABETS = {'auto', 'dna', 'protein'}


@dataclass(frozen=True)
class FastaRecord:
    identifier: str
    description: str
    sequence: str


def parse_and_normalize_fasta(
    text: str,
    *,
    alphabet: str,
    max_records: int,
    max_sequence_length: int,
    max_total_residues: int,
    max_header_length: int,
) -> tuple[str, dict]:
    requested_alphabet = alphabet.strip().lower()
    if requested_alphabet not in SUPPORTED_ALPHABETS:
        raise ValueError('alphabet must be one of: auto, dna, protein')

    records: list[FastaRecord] = []
    current_header: str | None = None
    sequence_lines: list[str] = []

    def finish_record() -> None:
        if current_header is None:
            return
        sequence = ''.join(sequence_lines).replace(' ', '').replace('\t', '').upper()
        if not sequence:
            raise ValueError(f'sequence {current_header.split()[0]!r} is empty')
        identifier, _, description = current_header.partition(' ')
        records.append(FastaRecord(identifier, description.strip(), sequence))

    normalized_text = text.replace('\r\n', '\n').replace('\r', '\n')
    for line_number, raw_line in enumerate(normalized_text.split('\n'), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith('>'):
            finish_record()
            current_header = line[1:].strip()
            sequence_lines = []
            if not current_header:
                raise ValueError(f'header on line {line_number} is empty')
            if len(current_header) > max_header_length:
                raise ValueError(
                    f'header on line {line_number} exceeds {max_header_length} characters'
                )
            if any(ord(char) < 32 for char in current_header):
                raise ValueError(f'header on line {line_number} contains control characters')
            if len(records) >= max_records:
                raise ValueError(f'FASTA exceeds maximum of {max_records} records')
        else:
            if current_header is None:
                raise ValueError(
                    f'sequence data on line {line_number} appears before the first header'
                )
            sequence_lines.append(line)
    finish_record()

    if not records:
        raise ValueError('FASTA contains no records')

    identifiers = [record.identifier for record in records]
    duplicate_ids = sorted(
        identifier for identifier, count in Counter(identifiers).items() if count > 1
    )
    if duplicate_ids:
        preview = ', '.join(duplicate_ids[:5])
        raise ValueError(f'FASTA identifiers must be unique; duplicates: {preview}')

    total_residues = sum(len(record.sequence) for record in records)
    if total_residues > max_total_residues:
        raise ValueError(
            f'FASTA exceeds maximum of {max_total_residues} total residues'
        )
    longest = max(len(record.sequence) for record in records)
    if longest > max_sequence_length:
        raise ValueError(
            f'a sequence exceeds maximum length of {max_sequence_length} residues'
        )

    observed = set().union(*(set(record.sequence) for record in records))
    detected_alphabet = requested_alphabet
    if requested_alphabet == 'auto':
        if observed <= DNA_ALPHABET:
            detected_alphabet = 'dna'
        elif observed <= PROTEIN_ALPHABET:
            detected_alphabet = 'protein'
        else:
            invalid = ''.join(sorted(observed - PROTEIN_ALPHABET))
            raise ValueError(f'FASTA contains unsupported residue symbols: {invalid}')
    allowed = DNA_ALPHABET if detected_alphabet == 'dna' else PROTEIN_ALPHABET
    invalid = observed - allowed
    if invalid:
        raise ValueError(
            f'FASTA contains invalid {detected_alphabet} symbols: {"".join(sorted(invalid))}'
        )

    output_lines: list[str] = []
    lengths = []
    for record in records:
        header = record.identifier
        if record.description:
            header += f' {record.description}'
        output_lines.append(f'>{header}')
        output_lines.extend(
            record.sequence[index : index + 80]
            for index in range(0, len(record.sequence), 80)
        )
        lengths.append(len(record.sequence))

    summary = {
        'record_count': len(records),
        'total_residues': total_residues,
        'minimum_length': min(lengths),
        'maximum_length': max(lengths),
        'mean_length': round(total_residues / len(records), 2),
        'alphabet': detected_alphabet,
        'identifier_preview': identifiers[:20],
    }
    if detected_alphabet == 'dna':
        gc_count = sum(
            record.sequence.count('G') + record.sequence.count('C')
            for record in records
        )
        summary['gc_percent'] = round(gc_count * 100 / total_residues, 2)

    return '\n'.join(output_lines) + '\n', summary
