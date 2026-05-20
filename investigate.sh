#!/bin/bash
# Deep investigation — runs Claude as a FULL agent (not --print)
# Claude can read files, search code, and suggest fixes
# Run after the dashboard is generated

CURSOR_CLI="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
REPO_DIR="$HOME/Documents/GitHub/prow-nightly-monitor"

cd "$REPO_DIR" || exit 1

# Clone the commatrix repo for Claude to investigate
if [ ! -d "/tmp/commatrix-investigate" ]; then
  git clone --depth=1 https://github.com/openshift-kni/commatrix.git /tmp/commatrix-investigate 2>/dev/null
else
  cd /tmp/commatrix-investigate && git pull --quiet 2>/dev/null
fi

cd /tmp/commatrix-investigate

# Run Claude as a full agent on the commatrix repo
"$CURSOR_CLI" agent --trust --print --output-format text \
"You have the commatrix repo checked out. Read these files to understand the project:
- test/e2e/validation_test.go (the failing test)
- samples/custom-entries/ (static entries)
- pkg/ (the library code)

The CI nightly test keeps failing because:
- Port in ephemeral range (32768-60999) owned by a system daemon (like crio)
- The test finds it via ss command but there's no EndpointSlice for it
- The port number changes every reboot so you can't add a fixed static entry

Investigate:
1. Read validation_test.go — find where it checks EndpointSlices vs ss ports
2. Read the static entries format
3. Think: what's the right fix? Should the test skip ephemeral ports owned by system daemons?
4. If you can write a fix, describe the exact code change needed

Write your findings to /tmp/commatrix-investigate/investigation-report.md" \
2>&1

if [ -f "/tmp/commatrix-investigate/investigation-report.md" ]; then
  cp /tmp/commatrix-investigate/investigation-report.md "$REPO_DIR/public/investigation-report.md"
  echo "Investigation report saved"
fi
