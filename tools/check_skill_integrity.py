#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Skill Integrity Checker
-----------------------
Validates that SKILL.md files retain their mandatory sections and formatting.
"""

import os
import sys
import glob
import subprocess

def check_skill_file(filepath, workspace_root):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    errors = []
    
    if not content.startswith("---"):
        errors.append("Missing or invalid YAML frontmatter (must start with '---').")
    else:
        # Check for name and description in frontmatter
        header_end = content.find("---", 3)
        if header_end == -1:
            errors.append("Unclosed YAML frontmatter.")
        else:
            frontmatter = content[3:header_end]
            if "name:" not in frontmatter:
                errors.append("Missing 'name:' in YAML frontmatter.")
            if "description:" not in frontmatter:
                errors.append("Missing 'description:' in YAML frontmatter.")
            if "ignore_structure: true" in frontmatter:
                return []  # Bypass structural checks for non-agent skills

    # Check against HEAD to see if we deleted important sections
    rel_path = os.path.relpath(filepath, workspace_root)
    try:
        res = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"],
            capture_output=True,
            text=True,
            check=True,
            cwd=workspace_root,
        )
        head_content = res.stdout
    except subprocess.CalledProcessError:
        head_content = ""

    if "## Workflow" in head_content and "## Workflow" not in content:
        errors.append("Removed mandatory section: '## Workflow' (present in HEAD).")
        
    if "## Common Pitfalls & Strict Rules" in head_content and "## Common Pitfalls & Strict Rules" not in content:
        errors.append("Removed mandatory section: '## Common Pitfalls & Strict Rules' (present in HEAD).")

    return errors

def main():
    workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    skills_dir = os.path.join(workspace_root, "agents", "skills")
    
    if not os.path.isdir(skills_dir):
        # Skills dir doesn't exist, maybe not initialized yet
        sys.exit(0)
        
    skill_files = glob.glob(os.path.join(skills_dir, "**", "SKILL.md"), recursive=True)
    
    violation = False
    
    for filepath in skill_files:
        errors = check_skill_file(filepath, workspace_root)
        if errors:
            print(f"❌ SKILL INTEGRITY VIOLATION in {os.path.relpath(filepath, workspace_root)}")
            for err in errors:
                print(f"   - {err}")
            violation = True
            
    if violation:
        sys.exit(1)
        
if __name__ == "__main__":
    main()
