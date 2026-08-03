import os
import sys

_SERVER = os.path.join(os.path.dirname(__file__), "..", "server")
# `server/` for the plugin's own modules; `server/vendor/` for its vendored
# dependencies (google-auth, googleapiclient, mcp). Without vendor the suite
# only imports when PYTHONPATH happens to be set externally.
sys.path.insert(0, _SERVER)
sys.path.insert(1, os.path.join(_SERVER, "vendor"))
