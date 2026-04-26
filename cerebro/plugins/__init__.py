"""Plugin discovery, loading, and dependency resolution.

This package also serves as the default in-tree location where first-party
plugins ship as subdirectories. ``loader.discover_plugins`` walks this
directory for plugin subdirs (each containing ``plugin.yaml``) and ignores
the support modules ``loader``, ``resolver``, and ``__init__``.
"""
