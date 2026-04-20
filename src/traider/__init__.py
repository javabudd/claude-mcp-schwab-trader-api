"""traider — unified read-only MCP server hub for trading insight.

Tool groups are gated by the ``TRAIDER_PROFILES`` env var; only the
modules for enabled profiles are imported at startup. See
``traider.server`` for the profile→module map.
"""

__version__ = "0.4.0"
