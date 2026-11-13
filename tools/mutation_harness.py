#!/usr/bin/env python3
"""Small AST mutation harness.

mut.py --root TREE --file REL --test "CMD" [--cwd REL] [--funcs a,b] [--workers N] [--timeout S] [--out JSON] [--list]

SAFETY RULE (incident 2026-11-09): mutation testing is only ever done on a COPY of the tree. A worktree's
`hams_shared` entry is a symlink to the main checkout, and a mutant written through it deleted tools/lint_stamp_gate.py in
the main checkout for about half a minute. The harness therefore refuses to write any path that does not resolve (symlinks
followed) to a location inside its own throwaway copy, and verifies at the end that the original file is byte-identical.

KNOWN LIMIT: the copy is made of hardlinks, so editing a test file in place while a run is in progress changes the tests the
run sees. Do not edit the tests or the target during a run (or run it on `cp -r` of the tree).

Each mutant: hardlink-clone of TREE (rsync --link-dest), the target file replaced by an ast.unparse of the mutated
tree, CMD run in the clone from --cwd (relative to root) with nice 10 and an address-space cap
(--mem-limit-mb, default 4096). KILLED = nonzero exit (including death by the memory cap) or timeout; SURVIVED = exit 0.

PROCESS TREE (incident 2026-11-12): CMD runs in its own process group; on timeout or any exception (KeyboardInterrupt too) the whole
group is terminated (TERM, then KILL after a short grace). subprocess.run(shell=True, timeout=) killed only the wrapper shell and left
infinite-loop mutants (one at 48 GB) running as orphans.
"""
import argparse, ast, copy, json, os, queue, shutil, signal, subprocess, sys, time, concurrent.futures as cf

CMP_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE,
            ast.GtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
CMP_SWAP2 = {ast.Lt: ast.GtE, ast.LtE: ast.Gt, ast.Gt: ast.LtE, ast.GtE: ast.Lt}


def enumerate_mutants(tree, wanted):
    muts = []
    nodes = list(ast.walk(tree))
    idx = {id(n): i for i, n in enumerate(nodes)}
    inside = set()
    for f in nodes:
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and (wanted is None or f.name in wanted):
            for n in ast.walk(f):
                inside.add(id(n))
    doc_ids = set()
    for n in nodes:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], 'value', None), ast.Constant) \
                    and isinstance(b[0].value.value, str):
                doc_ids.add(id(b[0].value))
    for n in nodes:
        if id(n) not in inside:
            continue
        i = idx[id(n)]
        ln = getattr(n, 'lineno', 0)
        if isinstance(n, ast.Compare):
            for k, op in enumerate(n.ops):
                t = type(op)
                if t in CMP_SWAP:
                    muts.append((f"cmp {t.__name__}->{CMP_SWAP[t].__name__}", ln, ('cmp', i, k, CMP_SWAP[t])))
                if t in CMP_SWAP2:
                    muts.append((f"cmp {t.__name__}->{CMP_SWAP2[t].__name__}", ln, ('cmp', i, k, CMP_SWAP2[t])))
        elif isinstance(n, ast.BoolOp):
            muts.append((f"boolop {type(n.op).__name__} swap", ln, ('bool', i)))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            muts.append(("remove not", ln, ('unnot', i)))
        elif isinstance(n, (ast.If, ast.While, ast.IfExp)):
            muts.append(("negate condition", ln, ('negate', i)))
            muts.append(("condition -> True", ln, ('const_test', i, True)))
            muts.append(("condition -> False", ln, ('const_test', i, False)))
        elif isinstance(n, ast.Raise):
            muts.append(("delete raise", ln, ('stmt_pass', i)))
        elif isinstance(n, ast.Return):
            if n.value is not None and not (isinstance(n.value, ast.Constant) and n.value.value is None):
                muts.append(("return -> None", ln, ('ret_none', i)))
            if isinstance(n.value, ast.Constant) and isinstance(n.value.value, bool):
                muts.append(("return bool flip", ln, ('boolflip', i)))
            if n.value is None or isinstance(n.value, ast.Constant):
                muts.append(("delete return", ln, ('stmt_pass', i)))
        elif isinstance(n, ast.Constant) and id(n) not in doc_ids:
            v = n.value
            if isinstance(v, bool):
                muts.append((f"const {v}->{not v}", ln, ('const', i, not v)))
            elif isinstance(v, int):
                muts.append((f"const {v}->{v+1}", ln, ('const', i, v + 1)))
                if v > 0:
                    muts.append((f"const {v}->{v-1}", ln, ('const', i, v - 1)))
            elif isinstance(v, str) and v and len(v) < 60:
                muts.append((f"str {v[:20]!r}->+XX", ln, ('const', i, v + 'XX')))
        elif isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub)):
            sw = ast.Sub if isinstance(n.op, ast.Add) else ast.Add
            muts.append((f"binop {type(n.op).__name__}->{sw.__name__}", ln, ('binop', i, sw)))
        elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            muts.append(("call result -> None", ln, ('assign_none', i)))
        elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and not isinstance(n.value.func, ast.Name):
            muts.append(("delete call statement", ln, ('stmt_pass', i)))
    return muts


def apply(tree, m):
    t = copy.deepcopy(tree)
    nodes = list(ast.walk(t))
    kind = m[0]
    n = nodes[m[1]]
    if kind == 'cmp':
        n.ops[m[2]] = m[3]()
    elif kind == 'bool':
        n.op = ast.Or() if isinstance(n.op, ast.And) else ast.And()
    elif kind == 'unnot':
        _replace(t, n, n.operand)
    elif kind == 'negate':
        n.test = ast.UnaryOp(op=ast.Not(), operand=n.test)
    elif kind == 'const_test':
        n.test = ast.Constant(value=m[2])
    elif kind == 'stmt_pass':
        _replace(t, n, ast.Pass())
    elif kind == 'ret_none':
        n.value = ast.Constant(value=None)
    elif kind == 'boolflip':
        n.value = ast.Constant(value=not n.value.value)
    elif kind == 'const':
        n.value = m[2]
    elif kind == 'binop':
        n.op = m[2]()
    elif kind == 'assign_none':
        n.value = ast.Constant(value=None)
    ast.fix_missing_locations(t)
    return t


def _replace(tree, old, new):
    for p in ast.walk(tree):
        for field, val in ast.iter_fields(p):
            if isinstance(val, list):
                for j, x in enumerate(val):
                    if x is old:
                        val[j] = new
                        return
            elif val is old:
                setattr(p, field, new)
                return
    raise RuntimeError("replace failed")


def clone(root, dst):
    if os.path.exists(dst):
        shutil.rmtree(dst)
    subprocess.run(["rsync", "-a", f"--link-dest={root}", "--exclude=node_modules", "--exclude=__pycache__",
                    "--exclude=/tmp", root.rstrip('/') + '/', dst + '/'], check=True)


def require_inside_copy(path, work):
    """Raise SystemExit unless `path` resolves, symlinks followed, to a file inside the throwaway copy `work`."""
    real_work = os.path.realpath(work)
    real = os.path.realpath(path)
    if not real.startswith(real_work + os.sep):
        raise SystemExit('SAFETY: refusing to mutate %s: it resolves to %s, outside the throwaway copy %s'
                         % (path, real, real_work))
    return real


# [@ANCHOR: mutation_harness:run_group]
def run_group(test, cwd, env, timeout=None, mem_limit_mb=4096, grace=3.0):
    """Runs shell command `test` (nice 10, `ulimit -v` of mem_limit_mb) in its own process group. Returns (returncode, output, timed_out).
    On timeout or any exception the whole group is terminated (TERM, then KILL after `grace` seconds) and reaped."""
    cap = f"ulimit -v {int(mem_limit_mb) * 1024}; " if mem_limit_mb else ""
    proc = subprocess.Popen(f"{cap}exec nice -n 10 {test}", shell=True, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out, False
    except subprocess.TimeoutExpired:
        _stop_group(proc, grace)
        return None, b"", True
    except BaseException:
        _stop_group(proc, grace)
        raise


def _stop_group(proc, grace):
    """Terminate the group led by proc (its pid is the group id: start_new_session), wait, then force-end stragglers."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=grace)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.communicate()


def run_one(args, wid, job):
    desc, ln, key = job
    work = os.path.join(args.work, f"w{wid}")
    path = os.path.join(work, args.file)
    require_inside_copy(path, work)
    os.unlink(path)
    with open(path, 'w') as f:
        f.write(args.mutated_src[key])
    t0 = time.time()
    env = dict(os.environ)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    code, _, timed_out = run_group(args.test, os.path.join(work, args.cwd), env, args.timeout, args.mem_limit_mb)
    status = 'KILLED' if timed_out or code != 0 else 'SURVIVED'
    os.unlink(path)
    os.link(os.path.join(args.root, args.file), path)
    return desc, ln, status, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--file', required=True)
    ap.add_argument('--test', required=True)
    ap.add_argument('--cwd', default='.')
    ap.add_argument('--funcs')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--timeout', type=int, default=120)
    ap.add_argument('--mem-limit-mb', type=int, default=4096, help='per-mutant address-space cap, 0 = none; dying by it is KILLED')
    ap.add_argument('--work', default=os.path.join(os.path.expanduser('~'), 'workspace', 'tmp', 'mutwork'))
    ap.add_argument('--out')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--budget', type=float, default=0, help='seconds after which no NEW mutant is started (0 = no limit); the run is then PARTIAL')
    args = ap.parse_args()
    deadline = time.time() + args.budget if args.budget else None
    src = open(os.path.join(args.root, args.file)).read()
    import hashlib
    original_digest = hashlib.sha256(src.encode()).hexdigest()
    tree = ast.parse(src)
    wanted = set(args.funcs.split(',')) if args.funcs else None
    muts = enumerate_mutants(tree, wanted)
    if args.limit:
        muts = muts[:args.limit]
    print(f"{len(muts)} mutants", flush=True)
    if args.list:
        for d, l, _ in muts:
            print(l, d)
        return
    args.mutated_src = {}
    jobs = []
    for k, (d, l, m) in enumerate(muts):
        try:
            args.mutated_src[k] = ast.unparse(apply(tree, m))
        except Exception:
            continue
        jobs.append((d, l, k))
    tag = os.path.basename(args.file).replace('.py', '')
    args.work = os.path.join(args.work, tag)
    os.makedirs(args.work, exist_ok=True)
    for w in range(args.workers):
        clone(args.root, os.path.join(args.work, f"w{w}"))
    rc, out, _ = run_group(args.test, os.path.join(args.work, 'w0', args.cwd), {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                           None, args.mem_limit_mb)
    if rc != 0:
        print("BASELINE FAILS\n" + out.decode()[-3000:])
        sys.exit(2)
    q = queue.Queue()
    for j in jobs:
        q.put(j)

    def worker(wid):
        out = []
        while True:
            if deadline is not None and time.time() > deadline:
                return out
            try:
                j = q.get_nowait()
            except queue.Empty:
                return out
            out.append(run_one(args, wid, j))

    results = []
    with cf.ThreadPoolExecutor(args.workers) as ex:
        for f in [ex.submit(worker, w) for w in range(args.workers)]:
            results += f.result()
    results.sort(key=lambda r: r[1])
    killed = sum(1 for r in results if r[2] == 'KILLED')
    print(f"score {killed}/{len(results)} = {100*killed/max(1,len(results)):.1f}%")
    if len(results) < len(jobs):
        print(f"PARTIAL: {len(results)} of {len(jobs)} mutants run before the budget of {args.budget:g} s")
    for d, l, s, t in results:
        if s == 'SURVIVED':
            print(f"SURVIVED line {l}: {d}")
    if hashlib.sha256(open(os.path.join(args.root, args.file), 'rb').read()).hexdigest() != original_digest:
        raise SystemExit('SAFETY: the original file changed during the run')
    if args.out:
        json.dump(results, open(args.out, 'w'))
    shutil.rmtree(args.work, ignore_errors=True)


if __name__ == '__main__':
    main()
