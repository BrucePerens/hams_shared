#!/bin/bash
# Discovers all Odoo modules and daemons in the hams_* repositories for the comprehensive audit.
SCRIPT_DIR=$(dirname "$0")
find "$SCRIPT_DIR/../../../../../../hams_open" "$SCRIPT_DIR/../../../../../../hams_com" "$SCRIPT_DIR/../../../../../../hams_open/hams_shared" -maxdepth 1 -type d ! -name ".*" ! -name "hams_shared" ! -name "hams_open" ! -name "hams_com" -printf "%p\n"
