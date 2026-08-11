from mcp.server.fastmcp import FastMCP

from .backend_client import BackendClient

mcp = FastMCP('gene-family-mcp')
backend = BackendClient()


@mcp.tool()
def backend_health() -> dict:
    """Check whether the Gene Family backend API is available."""
    return backend.health()


@mcp.tool()
def get_capabilities() -> dict:
    """List analysis types and execution backends available on the service."""
    return backend.capabilities()


@mcp.tool()
def submit_cis_element_analysis(
    sequence: str,
    idempotency_key: str = '',
) -> dict:
    """Submit a DNA promoter sequence for PlantCARE cis-element analysis.

    The backend accepts A, C, G, T and N characters. The operation is
    asynchronous and returns a stable job_id for later status queries.
    """
    return backend.submit_cis_element_analysis(sequence, idempotency_key)


@mcp.tool()
def validate_fasta(
    fasta: str,
    alphabet: str = 'auto',
    filename: str = 'input.fasta',
    idempotency_key: str = '',
) -> dict:
    """Validate and normalize a multi-record FASTA input asynchronously.

    Alphabet may be auto, dna or protein. The backend stores the input as a
    content-addressed artifact, validates identifiers and residue symbols, and
    emits normalized FASTA plus a JSON summary.
    """
    return backend.submit_fasta_validation(
        fasta,
        alphabet,
        filename,
        idempotency_key,
    )


@mcp.tool()
def align_sequences(
    artifact_id: str,
    strategy: str = 'auto',
    threads: int = 2,
    idempotency_key: str = '',
) -> dict:
    """Align a validated FASTA Artifact asynchronously with MAFFT.

    artifact_id must refer to the normalized_fasta output of a successful
    validate_fasta job. Strategies are auto, linsi, ginsi and einsi. Check
    get_capabilities first because MAFFT is an optional backend runtime.
    """
    return backend.submit_multiple_sequence_alignment(
        artifact_id,
        strategy,
        threads,
        idempotency_key,
    )


@mcp.tool()
def build_phylogenetic_tree(
    artifact_id: str,
    model: str = 'auto',
    threads: int = 2,
    idempotency_key: str = '',
) -> dict:
    """Build a Newick phylogenetic tree from an aligned FASTA Artifact.

    artifact_id must refer to aligned_fasta from align_sequences. For DNA,
    models are auto, gtr or jc. For proteins, models are auto, jtt, lg or wag.
    """
    return backend.submit_phylogenetic_tree(
        artifact_id,
        model,
        threads,
        idempotency_key,
    )


@mcp.tool()
def run_sequence_phylogeny(
    fasta: str,
    alphabet: str = 'auto',
    alignment_strategy: str = 'auto',
    tree_model: str = 'auto',
    threads: int = 2,
    filename: str = 'input.fasta',
    idempotency_key: str = '',
) -> dict:
    """Run FASTA validation, MAFFT alignment and FastTree as one workflow.

    The workflow is persisted in the backend and can survive API or worker
    restarts. It returns one stable parent job_id; get_job_status and
    get_job_result expose aggregate progress, child jobs and the final Newick.
    """
    return backend.submit_sequence_phylogeny(
        fasta,
        alphabet,
        alignment_strategy,
        tree_model,
        threads,
        filename,
        idempotency_key,
    )


@mcp.tool()
def get_job_status(job_id: str) -> dict:
    """Get the current state and progress of an analysis job."""
    return backend.get_job(job_id)


@mcp.tool()
def get_job_result(job_id: str) -> dict:
    """Get the structured result and artifact manifest for a completed job."""
    return backend.get_job_result(job_id)


@mcp.tool()
def cancel_job(job_id: str) -> dict:
    """Cancel an analysis job that has not reached a terminal state."""
    return backend.cancel_job(job_id)


def main():
    mcp.run(transport='stdio')


if __name__ == '__main__':
    main()
