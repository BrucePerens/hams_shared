#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
import subprocess
import sys


def get_repo_name():
    try:
        url = subprocess.check_output(
            ['git', 'config', '--get', 'remote.origin.url'],
            stderr=subprocess.DEVNULL
        ).decode().strip()
    except subprocess.CalledProcessError:
        print("Error: Could not determine git repository url.", file=sys.stderr)
        sys.exit(1)

    if url.startswith('git@github.com:'):
        repo = url.split(':')[1]
    elif url.startswith('https://github.com/'):
        repo = url.replace('https://github.com/', '')
    else:
        repo = url

    if repo.endswith('.git'):
        repo = repo[:-4]

    return repo


def get_odoo_modules(root_dir='.'):
    modules = []
    for entry in os.listdir(root_dir):
        full_path = os.path.join(root_dir, entry)
        if os.path.isdir(full_path):
            manifest_path = os.path.join(full_path, '__manifest__.py')
            if os.path.isfile(manifest_path):
                modules.append(entry)
    return sorted(modules)


def main():
    parser = argparse.ArgumentParser(description="Run jules jobs for all detected Odoo modules.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('prompt', nargs='?', help="The prompt text to use.")
    group.add_argument('-f', '--file', help="A file containing the prompt text.")

    args = parser.parse_args()

    if args.file:
        try:
            with open(args.file, 'r', encoding='utf-8') as f:
                base_prompt = f.read().strip()
        except Exception as e:
            print(f"Error reading file {args.file}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        base_prompt = args.prompt.strip()

    repo = get_repo_name()
    modules = get_odoo_modules()

    if not modules:
        print("No modules found.")
        sys.exit(0)

    print(f"Detected repository: {repo}")
    print(f"Detected {len(modules)} modules.")

    processes = []
    for module in modules:
        prompt = f"Process the module {module} according to these instructions: {base_prompt}"
        cmd = ['jules', 'new', '--repo', repo, prompt]
        print(f"Starting job for module: {module}")
        try:
            p = subprocess.Popen(cmd)
            processes.append((module, p))
        except FileNotFoundError:
            print("Error: 'jules' command not found. Cannot start job.", file=sys.stderr)
            sys.exit(1)

    print("All jobs started.")


if __name__ == '__main__':
    main()
