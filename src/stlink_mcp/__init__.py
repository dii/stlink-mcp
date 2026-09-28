"""An MCP server for programming and inspecting STM32 targets over ST-LINK."""

from importlib.metadata import PackageNotFoundError, version

# Read from the installed metadata, which versioningit derives from the git tag,
# so the tag stays the only copy. This used to be a literal "0.1.0", a second
# copy that would have been wrong from the first commit after the next tag.
try:
    __version__ = version("stlink-mcp")
except PackageNotFoundError:
    # Imported from a source tree that was never installed, as tools/dwctl.py
    # does. Nothing there says which version it is.
    __version__ = "0.0.0+unknown"
