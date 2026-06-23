import os, ast
def get_deps(path):
    with open(path) as f:
        data = ast.literal_eval(f.read())
        return data.get("depends", [])

deps = {}
for root, dirs, files in os.walk("."):
    if "__manifest__.py" in files:
        mod = os.path.basename(root)
        deps[mod] = get_deps(os.path.join(root, "__manifest__.py"))

def find_cycle(start, path=None):
    if path is None: path = []
    path = path + [start]
    for d in deps.get(start, []):
        if d in path:
            print("CYCLE:", path + [d])
            return True
        if d in deps:
            if find_cycle(d, path): return True
    return False

for mod in deps:
    find_cycle(mod)

