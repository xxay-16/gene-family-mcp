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
