"""Runtime support for plugin hooks.

This package contains the hook context and the operation recorder used by
plugin install/uninstall/configure hooks. The recorder turns side effects
into a manifest of operations that can be rolled back on failure.
"""
