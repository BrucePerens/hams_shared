import os
import json

import sys

repos = [
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../../hams_com")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../../hams_open")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../../../hams_open/hams_shared"))
]

target_dirs = []

def has_code(directory):
    for root, _, files in os.walk(directory):
        if 'node_modules' in root or '.git' in root or '__pycache__' in root or '.agents' in root:
            continue
        for f in files:
            if f.endswith('.py') or f.endswith('.rs') or f.endswith('.js') or f.endswith('.xml'):
                return True
    return False

for repo in repos:
    if not os.path.exists(repo):
        continue
    for item in os.listdir(repo):
        path = os.path.join(repo, item)
        if os.path.isdir(path) and item not in ['.git', '.agents', 'hams_shared']:
            if has_code(path):
                target_dirs.append(path)
                
sys.stdout.write(json.dumps(target_dirs) + '\n')
