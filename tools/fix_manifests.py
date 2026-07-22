import os
import ast

def fix_manifest(path):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Simple line-by-line removal is usually safer than AST rewrite for manifests to preserve formatting.
    lines = content.split('\n')
    in_data_array = False
    new_lines = []
    modified = False
    
    for line in lines:
        if '"data": [' in line or "'data': [" in line:
            in_data_array = True
            new_lines.append(line)
        elif in_data_array and ']' in line and not ('"' in line or "'" in line):
            # rudimentary end of array detection
            in_data_array = False
            new_lines.append(line)
        elif in_data_array and ('"data/documentation.html"' in line or "'data/documentation.html'" in line or '"data/testing_documentation.html"' in line or "'data/testing_documentation.html'" in line):
            modified = True
            continue # skip this line
        else:
            new_lines.append(line)
            
    if modified:
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(new_lines))
        print(f"Fixed {path}")

for root, dirs, files in os.walk('.'):
    if '__manifest__.py' in files:
        fix_manifest(os.path.join(root, '__manifest__.py'))
