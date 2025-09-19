"""Type stubs for dj-database-url."""

def config(
    env: str = "DATABASE_URL",
    default: str = None,
    conn_max_age: int = 0,
    conn_health_checks: bool = False,
    ssl_require: bool = False,
) -> dict:
    """Parse database URL from environment."""
    pass

def parse(url: str, conn_max_age: int = 0) -> dict:
    """Parse database URL."""
    pass