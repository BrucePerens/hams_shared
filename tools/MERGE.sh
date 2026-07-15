# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

# List draft PRs and mark each as ready
gh pr list --json number,isDraft --jq '.[] | select(.isDraft == true) | .number' | \
xargs -I {} gh pr ready {}

# List all non-draft PRs and merge them using admin privileges
gh pr list --json number,isDraft --jq '.[] | select(.isDraft == false) | .number' | \
xargs -I {} gh pr merge {} --admin --merge --delete-branch

