#!/bin/sh
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

find . -name "__pycache__" -type d -exec rm -rf {} +
