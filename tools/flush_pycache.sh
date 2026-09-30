#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later

find . -name "__pycache__" -type d -exec rm -rf {} +
