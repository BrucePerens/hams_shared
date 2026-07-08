import os
import ast


def get_deps(path):
    with open(path) as f:
        data = ast.literal_eval(f.read())
        return data.get("depends", [])


deps = {}
for root, dirs, files in os.walk("."):
    if "radae" in dirs:
        dirs.remove("radae")
    if "__manifest__.py" in files:
        mod = os.path.basename(root)
        deps[mod] = get_deps(os.path.join(root, "__manifest__.py"))


def find_cycle(start, path=None, path_set=None, visited=None):
    if path is None:
        path = []
        path_set = set()
    if visited is None:
        visited = set()

    if start in visited:
        return False

    path.append(start)
    path_set.add(start)

    for d in deps.get(start, []):
        if d in path_set:
            print("CYCLE:", path + [d])
            return True
        if d in deps:
            if find_cycle(d, path, path_set, visited):
                return True

    path.pop()
    path_set.remove(start)
    visited.add(start)
    return False

visited_global = set()
for mod in deps:
    find_cycle(mod, visited=visited_global)
