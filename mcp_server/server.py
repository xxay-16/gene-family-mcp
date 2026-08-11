from mcp.server.fastmcp import FastMCP

from .backend_client import BackendClient


mcp = FastMCP('gene-family-mcp')
backend = BackendClient()


@mcp.tool()
def backend_health() -> dict:
    """Check whether the Gene Family backend API is available."""
    return backend.health()


@mcp.tool()
def submit_cis_element_analysis(sequence: str) -> dict:
    """Submit a DNA promoter sequence for PlantCARE cis-element analysis.

    The backend accepts A, C, G, T and N characters. The operation is
    asynchronous and returns a task_id for later status queries.
    """
    return backend.submit_cis_element_analysis(sequence)


@mcp.tool()
def get_cis_element_task(task_id: str) -> dict:
    """Get the current state or result of a cis-element analysis task."""
    return backend.get_task_status(task_id)


def main():
    mcp.run(transport='stdio')


if __name__ == '__main__':
    main()
