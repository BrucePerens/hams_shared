#! /bin/sh
# This software is distributed under the terms of the Affero General Public License (AGPL-3).

# gh pr list --state open --json number,mergeable | jq -r '.[] | select(.mergeable == "MERGEABLE") | .number' | xargs -I {} gh pr merge {} --merge --admin
gh pr list --state open --search "draft:true" --json number --jq '.[].number' | xargs -I {} sh -c 'gh pr ready {} && gh pr merge {} --merge'

