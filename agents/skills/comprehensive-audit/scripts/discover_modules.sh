#!/bin/bash
# Discovers all Odoo modules and daemons in the hams_* repositories for the comprehensive audit.
find /home/bruce/workspace/hams_open /home/bruce/workspace/hams_com /home/bruce/workspace/hams_open/hams_shared -maxdepth 1 -type d ! -name ".*" ! -name "hams_shared" ! -name "hams_open" ! -name "hams_com" -printf "%p\n"
