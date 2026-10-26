ARG BASE_IMAGE=vmanager:1.0
FROM ${BASE_IMAGE}
COPY runtime/ /opt/runtime/
# Add one share to the inherited script; retain all existing startup/network behavior.
RUN python3 - <<'PY'
from pathlib import Path
p = Path('/run/samba.sh')
s = p.read_text()
anchor = 'if ! smbd; then'
assert s.count(anchor) == 1 and '[Runtime]' not in s
s = s.replace(anchor, 'addShare "/opt/runtime" "Runtime" "Bundled runtime (read only)" "no" || error "Failed to create share Runtime!"\n\n' + anchor)
p.write_text(s)
PY
LABEL org.opencontainers.image.version="1.1"
