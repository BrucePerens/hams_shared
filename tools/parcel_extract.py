#!/usr/bin/env python3
import os
import sys
import re
import subprocess
import xml.etree.ElementTree as ET
import json
import ast
import difflib
import tempfile
import importlib
import urllib.parse
import shutil
import check_burn_list

def _cluster_indices(indices, max_gap):
    if not indices:
        return []
    clusters = []
    for idx in sorted(indices):
        if not clusters:
            clusters.append([idx])
        elif idx - clusters[-1][-1] <= max_gap:
            clusters[-1].append(idx)
        else:
            clusters.append([idx])
    return clusters

def lint_file_content(filepath, content):
    post_errors = []
    warnings = []
    ext = os.path.splitext(filepath)[1].lower()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_filepath = os.path.join(tmpdir, filepath)
        os.makedirs(os.path.dirname(tmp_filepath), exist_ok=True)
        with open(tmp_filepath, "w", encoding="utf-8") as f:
            f.write(content)

        if ext == ".py":
            cmd = ["flake8", "--select=E9,F,F541"]
            if os.path.basename(filepath) == "__init__.py":
                cmd.append("--extend-ignore=F401")
            cmd.append(tmp_filepath)
            try:
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    out = res.stdout.strip().replace(tmp_filepath, filepath)
                    post_errors.append(f"[ERROR] flake8 found issues:\n{out}")
            except FileNotFoundError:
                warnings.append("[WARN] flake8 is not installed. Skipping verification.")
        elif ext == ".xml":
            try:
                ET.fromstring(content)
            except ET.ParseError as e:
                post_errors.append(f"[ERROR] XML Parsing Error: {e}")
        elif ext == ".md":
            try:
                mask_markdown_and_check_balance(content)
            except ValueError as e:
                post_errors.append(f"[ERROR] {e}")
        elif ext == ".json":
            try:
                json.loads(content)
            except ValueError as e:
                post_errors.append(f"[ERROR] Invalid JSON: {e}")

        if ext in (".py", ".xml", ".js", ".csv"):
            if check_burn_list:
                try:
                    importlib.reload(check_burn_list)
                    check_burn_list.FOUND_TEST_CONTENTS = {}
                    check_burn_list.REQUIRE_TEST_VERIFICATION = []
                    errs, warns = check_burn_list.scan_file(tmp_filepath)
                    if errs:
                        err_str = "\n".join(errs).replace(tmp_filepath, filepath)
                        post_errors.append(f"[ERROR] check_burn_list.py rejected:\n{err_str}")
                    for w in warns:
                        warnings.append(f"[WARN] check_burn_list.py warning: {w.replace(tmp_filepath, filepath)}")
                except (ImportError, AttributeError, SyntaxError, TypeError, ValueError, KeyError, OSError) as e:
                    warnings.append(f"[WARN] Failed to execute custom linter: {e}")

    return post_errors, warnings

def mask_markdown_and_check_balance(payload):
    in_fenced = False
    fence_char = ""
    fence_len = 0
    in_inline = False
    inline_len = 0
    lines = payload.split("\n")

    for line in lines:
        stripped = line.lstrip(" \t")
        if not in_fenced and not in_inline:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                char = stripped[0]
                count = 0
                for c in stripped:
                    if c == char: count += 1
                    else: break
                if count >= 3:
                    in_fenced = True
                    fence_char = char
                    fence_len = count
                    continue
        if in_fenced:
            if stripped.startswith(fence_char):
                count = 0
                for c in stripped:
                    if c == fence_char: count += 1
                    else: break
                if count >= fence_len:
                    in_fenced = False
            continue

        i = 0
        n = len(line)
        while i < n:
            if not in_inline and line[i] == "\\":
                i += 2
                continue
            if line[i] == "`":
                start_idx = i
                while i < n and line[i] == "`":
                    i += 1
                count = i - start_idx
                if not in_inline:
                    in_inline = True
                    inline_len = count
                elif count == inline_len:
                    in_inline = False
            else:
                i += 1

    if in_fenced:
        raise ValueError("Markdown Error: Unclosed fenced code block.")
    if in_inline:
        raise ValueError("Markdown Error: Unclosed inline code snippet.")

    return payload

def check_ai_foibles(payload, filepath=""):
    if payload.lstrip().startswith("```"):
        lines = payload.strip("\r\n").split("\n")
        if (
            len(lines) >= 2
            and lines[0].strip().startswith("```")
            and lines[-1].strip().startswith("```")
        ):
            payload = "\n".join(lines[1:-1]) + "\n"

    text_to_check = payload
    if filepath.endswith(".md"):
        try:
            mask_markdown_and_check_balance(payload)
        except ValueError:
            pass

    foibles = [
        r"#\s*\.\.\.\s*rest of",
        r"//\s*\.\.\.\s*rest of",
        r"<!--\s*\.\.\.\s*rest of",
        r"#\s*Code unchanged",
        r"//\s*Code unchanged",
        r"#\s*\.\.\.\s*existing code\s*\.\.\.",
    ]
    for f in foibles:
        if re.search(f, text_to_check, re.IGNORECASE):
            raise ValueError(
                f"AI Laziness Foible: Found placeholder matching {f}. "
                "Payload rejected to prevent file corruption."
            )

    if filepath.endswith(".md") and re.search(r"(?<!`)``(?!`)", text_to_check):
        raise ValueError(
            "UI Data Loss Detected: Found empty inline code block (``). "
            "The conversational UI likely stripped an HTML/XML comment before reaching the extractor. "
            "You MUST percent-encode the tags (<, >) to bypass the UI."
        )
    return payload

def validate_syntax_in_memory(filepath, content):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".py":
        try:
            ast.parse(content)
        except SyntaxError as e:
            raise ValueError(f"Python Syntax/Indentation Error: {e}")
    elif ext == ".json":
        try:
            json.loads(content)
        except ValueError as e:
            raise ValueError(f"JSON Syntax Error: {e}")
    elif ext == ".xml":
        try:
            ET.fromstring(content)
        except ET.ParseError as e:
            raise ValueError(f"XML Syntax Error: {e}")
    elif ext == ".md":
        mask_markdown_and_check_balance(content)

def get_semantic_tokens(source_text):
    tokens = []
    pattern = re.compile(r"([a-zA-Z_]\w*|\d+|[^\w\s])")
    for match in pattern.finditer(source_text):
        tokens.append({
            "type": "REGEX_TOKEN",
            "val": match.group(1),
            "start": match.start(),
            "end": match.end(),
        })
    return tokens if tokens else None

def smart_replace(original_text, start_idx, end_idx, replace_text, filepath=""):
    orig_match = original_text[start_idx:end_idx]
    if "\n" not in orig_match and "\n" not in replace_text:
        return original_text[:start_idx] + replace_text + original_text[end_idx:]

    line_start = original_text.rfind("\n", 0, start_idx) + 1
    first_line_indent_str = original_text[line_start:start_idx]
    orig_lines = orig_match.split("\n")
    replace_lines = replace_text.split("\n")

    if replace_lines and not replace_lines[-1]:
        replace_lines.pop()

    abs_orig_lines = []
    for i, line in enumerate(orig_lines):
        if i == 0:
            abs_orig_lines.append(first_line_indent_str + line)
        else:
            abs_orig_lines.append(line)

    orig_min_indent = min((len(line) - len(line.lstrip(" \t")) for line in abs_orig_lines if line.strip()), default=0)
    replace_min_indent = min((len(line) - len(line.lstrip(" \t")) for line in replace_lines if line.strip()), default=0)

    base_shift = orig_min_indent - replace_min_indent
    shifts_to_try = [base_shift]
    if filepath.endswith(".py"):
        shifts_to_try.extend([0, base_shift + 4, base_shift - 4, base_shift + 8, base_shift - 8, base_shift + 12, base_shift - 12, 4, 8, 12, 16, -4, -8])

    best_new_text = None
    for shift in shifts_to_try:
        indented_replace_lines = []
        for i, line in enumerate(replace_lines):
            if not line.strip():
                indented_replace_lines.append("")
                continue
            curr_indent = len(line) - len(line.lstrip(" \t"))
            new_indent = max(0, curr_indent + shift)
            new_line = (" " * new_indent) + line.lstrip(" \t")
            if i == 0:
                if new_line.startswith(first_line_indent_str):
                    new_line = new_line[len(first_line_indent_str) :]
                else:
                    new_line = new_line.lstrip(" \t")
            indented_replace_lines.append(new_line)

        indented_replace_text = "\n".join(indented_replace_lines)
        if orig_match.endswith("\n") and not indented_replace_text.endswith("\n"):
            indented_replace_text += "\n"

        new_text = original_text[:start_idx] + indented_replace_text + original_text[end_idx:]

        if filepath.endswith(".py"):
            try:
                ast.parse(new_text)
                return new_text
            except SyntaxError:
                if best_new_text is None:
                    best_new_text = new_text
                continue
        else:
            return new_text
    return best_new_text or new_text

def fuzzy_line_replace(original_text, search_text, replace_text, filepath=""):
    orig_lines = original_text.split("\n")
    search_lines = search_text.strip("\n").split("\n")
    if not orig_lines or not search_lines: return None
    search_len = len(search_lines)
    target_len = len(orig_lines)
    if search_len == 0 or search_len > target_len: return None

    norm_search = [line_item.strip() for line_item in search_lines]
    best_ratio = 0
    best_indices = []

    for i in range(target_len - search_len + 1):
        window = [line_item.strip() for line_item in orig_lines[i : i + search_len]]
        ratio = difflib.SequenceMatcher(None, window, norm_search).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_indices = [i]
        elif ratio == best_ratio and ratio > 0:
            best_indices.append(i)

    if best_ratio >= 0.85:
        clusters = _cluster_indices(best_indices, search_len)
        if len(clusters) > 1:
            raise ValueError("Fuzzy line match is not unique (found multiple blocks with similar logic). Please provide more context lines.")
        best_idx = clusters[0][0]
        start_idx = sum(len(line) + 1 for line in orig_lines[:best_idx])
        end_idx = start_idx + sum(len(line) + 1 for line in orig_lines[best_idx : best_idx + search_len])
        end_idx = min(end_idx, len(original_text))
        return smart_replace(original_text, start_idx, end_idx, replace_text, filepath)
    return None

def semantic_token_replace(original_text, search_text, replace_text, filepath=""):
    target_tokens = get_semantic_tokens(original_text)
    search_tokens = get_semantic_tokens(search_text)
    if not target_tokens or not search_tokens: return None
    search_len = len(search_tokens)
    target_len = len(target_tokens)
    if search_len == 0 or search_len > target_len: return None

    matches = []
    for i in range(target_len - search_len + 1):
        match = True
        for j in range(search_len):
            if target_tokens[i + j]["val"] != search_tokens[j]["val"]:
                match = False
                break
        if match: matches.append(i)

    if len(matches) > 1:
        clusters = _cluster_indices(matches, search_len)
        if len(clusters) > 1:
            raise ValueError("Semantic token match is not unique. Please provide more context lines in the SEARCH block.")
        matches = [clusters[0][0]]

    if len(matches) == 1:
        start_idx = target_tokens[matches[0]]["start"]
        end_idx = target_tokens[matches[0] + search_len - 1]["end"]
        return smart_replace(original_text, start_idx, end_idx, replace_text, filepath)
    return None

def fuzzy_token_replace(original_text, search_text, replace_text, filepath=""):
    target_tokens = get_semantic_tokens(original_text)
    search_tokens = get_semantic_tokens(search_text)
    if not target_tokens or not search_tokens: return None
    search_len = len(search_tokens)
    target_len = len(target_tokens)
    if search_len == 0 or search_len > target_len: return None

    search_vals = [t["val"] for t in search_tokens]
    target_vals = [t["val"] for t in target_tokens]
    best_ratio = 0
    best_indices = []

    for i in range(target_len - search_len + 1):
        window = target_vals[i : i + search_len]
        ratio = difflib.SequenceMatcher(None, window, search_vals).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_indices = [i]
        elif ratio == best_ratio and ratio > 0:
            best_indices.append(i)

    if best_ratio >= 0.90:
        clusters = _cluster_indices(best_indices, search_len)
        if len(clusters) > 1:
            raise ValueError("Fuzzy token match is not unique. Please provide more context lines.")
        best_idx = clusters[0][0]
        start_idx = target_tokens[best_idx]["start"]
        end_idx = target_tokens[best_idx + search_len - 1]["end"]
        return smart_replace(original_text, start_idx, end_idx, replace_text, filepath)
    return None

def get_markdown_tokens(text):
    tokens = []
    for match in re.finditer(r"([a-zA-Z0-9]+|[^\w\s])", text):
        tokens.append({"raw": match.group(), "norm": match.group().lower(), "start": match.start(), "end": match.end()})
    return tokens

def semantic_markdown_replace(original_text, search_text, replace_text, filepath=""):
    target_tokens = get_markdown_tokens(original_text)
    search_tokens = get_markdown_tokens(search_text)
    if not target_tokens or not search_tokens: return None
    search_len = len(search_tokens)
    target_len = len(target_tokens)
    if search_len == 0 or search_len > target_len: return None

    matches = []
    for i in range(target_len - search_len + 1):
        match = True
        for j in range(search_len):
            if target_tokens[i + j]["norm"] != search_tokens[j]["norm"]:
                match = False
                break
        if match: matches.append(i)

    if len(matches) > 1:
        clusters = _cluster_indices(matches, search_len)
        if len(clusters) > 1:
            raise ValueError("Semantic Markdown match is not unique. Please provide more context lines.")
        matches = [clusters[0][0]]

    if len(matches) == 1:
        start_idx = target_tokens[matches[0]]["start"]
        end_idx = target_tokens[matches[0] + search_len - 1]["end"]
        return smart_replace(original_text, start_idx, end_idx, replace_text, filepath)
    return None

def boundary_markdown_replace(original_text, search_text, replace_text, filepath=""):
    target_tokens = get_markdown_tokens(original_text)
    search_tokens = get_markdown_tokens(search_text)
    if not target_tokens or len(search_tokens) < 10: return None

    prefix = [t["norm"] for t in search_tokens[:5]]
    suffix = [t["norm"] for t in search_tokens[-5:]]
    target_words = [t["norm"] for t in target_tokens]

    start_matches = []
    for i in range(len(target_words) - 4):
        if target_words[i : i + 5] == prefix: start_matches.append(i)
    if len(start_matches) > 1:
        clusters = _cluster_indices(start_matches, 5)
        if len(clusters) > 1: raise ValueError("Boundary Markdown Prefix is not unique.")
        start_matches = [clusters[0][0]]
    if not start_matches: return None

    best_start_token_idx = start_matches[0]
    end_matches = []
    for i in range(best_start_token_idx, len(target_words) - 4):
        if target_words[i : i + 5] == suffix: end_matches.append(i + 4)
    if len(end_matches) > 1:
        clusters = _cluster_indices(end_matches, 5)
        if len(clusters) > 1: raise ValueError("Boundary Markdown Suffix is not unique.")
        end_matches = [clusters[0][0]]
    if not end_matches: return None

    best_end_token_idx = end_matches[0]
    start_idx = target_tokens[best_start_token_idx]["start"]
    end_idx = target_tokens[best_end_token_idx]["end"]
    return smart_replace(original_text, start_idx, end_idx, replace_text, filepath)

def fuzzy_markdown_replace(original_text, search_text, replace_text, filepath=""):
    target_tokens = get_markdown_tokens(original_text)
    search_tokens = get_markdown_tokens(search_text)
    if not target_tokens or not search_tokens: return None
    search_len = len(search_tokens)
    target_len = len(target_tokens)
    if search_len == 0 or search_len > target_len: return None

    search_words = [t["norm"] for t in search_tokens]
    target_words = [t["norm"] for t in target_tokens]
    best_ratio = 0
    best_indices = []

    for i in range(target_len - search_len + 1):
        window = target_words[i : i + search_len]
        ratio = difflib.SequenceMatcher(None, window, search_words).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_indices = [i]
        elif ratio == best_ratio and ratio > 0:
             best_indices.append(i)

    if best_ratio > 0.90:
        clusters = _cluster_indices(best_indices, search_len)
        if len(clusters) > 1:
            raise ValueError("Fuzzy Markdown match is not unique. Please provide more context lines.")
        best_idx = clusters[0][0]
        start_idx = target_tokens[best_idx]["start"]
        end_idx = target_tokens[best_idx + search_len - 1]["end"]
        return smart_replace(original_text, start_idx, end_idx, replace_text, filepath)
    return None

def get_xml_tokens(text):
    tokens = []
    pattern = re.compile(r"(<!\[CDATA\[.*?\]\]>|<[^>]+>|[^<]+)", re.DOTALL)
    def normalize_tag(tag_str):
        if tag_str.startswith("</") or tag_str.startswith("<!") or tag_str.startswith("<?"):
            return " ".join(tag_str.split())
        inner = tag_str[1:-1]
        is_self_closing = False
        if inner.endswith("/"):
            is_self_closing = True
            inner = inner[:-1]
        parts = inner.split(None, 1)
        if not parts: return tag_str
        tag_name = parts[0]
        attrs_str = parts[1] if len(parts) > 1 else ""
        attr_pattern = re.compile(r'([\w\-\:]+)\s*=\s*(["\'])(.*?)\2', re.DOTALL)
        attrs = attr_pattern.findall(attrs_str)
        sorted_attrs = sorted(attrs, key=lambda x: x[0])
        norm_attr_str = " ".join([f'{k}="{v}"' for k, q, v in sorted_attrs])
        res = f"<{tag_name}"
        if norm_attr_str: res += f" {norm_attr_str}"
        res += "/>" if is_self_closing else ">"
        return res

    for match in pattern.finditer(text):
        raw = match.group(1)
        norm = normalize_tag(raw) if raw.startswith("<") else raw.strip()
        if not norm: continue
        tokens.append({"raw": raw, "norm": norm, "start": match.start(), "end": match.end()})
    return tokens

def semantic_xml_replace(original_text, search_text, replace_text, filepath=""):
    target_tokens = get_xml_tokens(original_text)
    search_tokens = get_xml_tokens(search_text)
    if not target_tokens or not search_tokens: return None
    search_len = len(search_tokens)
    target_len = len(target_tokens)
    if search_len == 0 or search_len > target_len: return None

    matches = []
    for i in range(target_len - search_len + 1):
        match = True
        for j in range(search_len):
            if target_tokens[i + j]["norm"] != search_tokens[j]["norm"]:
                match = False
                break
        if match: matches.append(i)

    if len(matches) > 1:
        clusters = _cluster_indices(matches, search_len)
        if len(clusters) > 1: raise ValueError("Semantic XML match is not unique. Please provide more context lines.")
        matches = [clusters[0][0]]

    if len(matches) == 1:
        start_idx = target_tokens[matches[0]]["start"]
        end_idx = target_tokens[matches[0] + search_len - 1]["end"]
        return smart_replace(original_text, start_idx, end_idx, replace_text, filepath)
    return None

def whitespace_agnostic_replace(original_text, search_text, replace_text, filepath=""):
    search_stripped = "".join(search_text.split())
    if not search_stripped: return original_text
    chars, indices = [], []
    for i, c in enumerate(original_text):
        if not c.isspace():
            chars.append(c)
            indices.append(i)
    orig_stripped = "".join(chars)
    matches = []
    idx = orig_stripped.find(search_stripped)
    while idx != -1:
        matches.append(idx)
        idx = orig_stripped.find(search_stripped, idx + 1)
    if len(matches) > 1:
        clusters = _cluster_indices(matches, len(search_stripped))
        if len(clusters) > 1: raise ValueError("Whitespace-agnostic match is not unique.")
        matches = [clusters[0][0]]
    if len(matches) == 1:
        idx = matches[0]
        start_idx = indices[idx]
        end_idx = indices[idx + len(search_stripped) - 1]
        return smart_replace(original_text, start_idx, end_idx + 1, replace_text, filepath)
    return None

def parse_search_replace_blocks(payload):
    def _strip_empty_bounding_lines(lines):
        start = 0
        end = len(lines)
        while start < end and not lines[start].strip(): start += 1
        while end > start and not lines[end - 1].strip(): end -= 1
        return lines[start:end]

    blocks = []
    lines = payload.split("\n")
    state = "TEXT"
    current_search, current_replace = [], []

    for line in lines:
        stripped = line.strip()
        stripped_no_space = stripped.replace(" ", "")
        if stripped_no_space.startswith("::::SEARCH"):
            if state != "TEXT": raise ValueError("Malformed search block: ':::: SEARCH' found inside another block.")
            state = "SEARCH"
            current_search, current_replace = [], []
        elif stripped.startswith("====") and len(stripped) >= 4 and all(c == "=" for c in stripped):
            if state != "SEARCH": raise ValueError("Malformed search block: '====' found without preceding ':::: SEARCH'.")
            state = "REPLACE"
        elif stripped_no_space.startswith("::::REPLACE"):
            if state != "REPLACE": raise ValueError("Malformed search block: ':::: REPLACE' found without preceding '===='.")
            search_str = "\n".join(_strip_empty_bounding_lines(current_search)) + "\n"
            replace_str = "\n".join(current_replace) + "\n"
            if search_str == replace_str:
                raise ValueError("UI Data Loss Prevention: Search and replace blocks are identical. This indicates elided contents, usually due to a failure to URL-encode \"<\" and \">\".")
            blocks.append({
                "search": search_str,
                "replace": replace_str,
            })
            state = "TEXT"
        else:
            if state == "SEARCH": current_search.append(line)
            elif state == "REPLACE": current_replace.append(line)

    if state != "TEXT": raise ValueError("Malformed search block: Unclosed ':::: SEARCH' or '====' block.")
    return blocks

def extract_parcel(raw_text):
    raw_text = raw_text.replace("\r\n", "\n")
    lines = raw_text.splitlines()
    boundary = None

    for line_item in lines:
        stripped = line_item.strip()
        if stripped.startswith("@@BOUNDARY_"):
            if stripped.endswith("@@"):
                boundary = stripped
                break
            elif stripped.endswith("@@--"):
                boundary = stripped[:-2]
                break

    if not boundary:
        print("❌ Error: Invalid Parcel format. Missing boundary string.\n")
        return

    terminator = boundary + "--"
    if not any(line_item.strip() == terminator for line_item in lines):
        print(f"❌ Error: Parcel terminator ({terminator}) missing. Rejecting payload.\n")
        return

    pattern = rf"^{re.escape(boundary)}$"
    parts = re.split(pattern, raw_text, flags=re.MULTILINE)
    tasks_by_file = {}
    VALID_HEADERS = ("Path:", "Operation:", "New-Path:", "Mode:", "Encoding:", "Repository:")

    for part in parts:
        if not part.strip() or part.strip().startswith("--"): continue
        part = part.lstrip()
        lines = part.splitlines()
        header_lines, payload_lines = [], []
        in_header = True

        for line in lines:
            if in_header:
                if not line.strip():
                    in_header = False
                    continue
                if line.startswith(VALID_HEADERS):
                    header_lines.append(line)
                elif re.match(r"^[A-Za-z0-9\-]+:", line):
                    print(f"❌ Error: Unknown header '{line.strip()}' in Parcel block.\nRejecting payload.")
                    return
                else:
                    in_header = False
                    payload_lines.append(line)
            else:
                payload_lines.append(line)

        if not header_lines:
            print("❌ Error: Parcel block missing headers (e.g., 'Path:').\nRejecting payload.")
            return

        header = "\n".join(header_lines)
        payload = "\n".join(payload_lines)

        path_lines = [l for l in header.splitlines() if l.startswith("Path:")]
        if not path_lines:
            print("❌ Error: Parcel block missing 'Path:' header.\nRejecting payload.")
            return
        filepath = path_lines[0].split(":", 1)[1].strip()

        operation_lines = [l for l in header.splitlines() if l.startswith("Operation:")]
        operation = operation_lines[0].split(":", 1)[1].strip().lower() if operation_lines else "overwrite"

        new_path_lines = [l for l in header.splitlines() if l.startswith("New-Path:")]
        new_filepath = new_path_lines[0].split(":", 1)[1].strip() if new_path_lines else None

        mode_lines = [l for l in header.splitlines() if l.startswith("Mode:")]
        mode_str = mode_lines[0].split(":", 1)[1].strip() if mode_lines else None

        repo_lines = [l for l in header.splitlines() if l.startswith("Repository:")]
        expected_repo = repo_lines[0].split(":", 1)[1].strip() if repo_lines else None

        if expected_repo:
            try:
                top_level = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], stderr=subprocess.STDOUT, text=True).strip()
                current_repo = os.path.basename(top_level)
            except (subprocess.CalledProcessError, OSError):
                current_repo = os.path.basename(os.getcwd())
            exp_base = expected_repo.split('/')[-1].lower()
            curr_base = current_repo.lower()
            if not (curr_base.startswith(exp_base) or exp_base.startswith(curr_base)):
                tasks_by_file.setdefault(filepath, []).append({"error": "Repository mismatch."})
                continue

        if terminator in payload:
            payload = payload.split(terminator)[0]

        if filepath.endswith((".xml", ".html")):
            if "<" in payload or ">" in payload:
                tasks_by_file.setdefault(filepath, []).append({"error": "UI Data Loss Prevention: Raw '<' or '>' detected in XML/HTML payload. You MUST URL-encode all angle brackets (<, >) in the Parcel output to prevent the UI from stripping tags."})
                continue

        payload = urllib.parse.unquote(payload)
        if filepath.endswith((".py", ".sh", ".conf", ".yaml", ".json", ".xml", ".csv", ".md")):
            payload = re.sub(r'\\?\[\s*(["\']?)\s*(https?://[^\]"\'\s]+)\s*\1\s*\\?\]\s*\\?\(\s*(https?://[^)\s]+)\s*\\?\)', r'\1\2\1', payload)
            payload = re.sub(r"(https?://)?\[([^\]]+)\]\([^)]*https?://[^)]+\)", lambda m: (m.group(1) or "") + m.group(2), payload)

        try:
            payload = check_ai_foibles(payload, filepath)
        except ValueError as e:
            tasks_by_file.setdefault(filepath, []).append({"error": str(e)})
            continue

        payload = payload.rstrip() + "\n"
        tasks_by_file.setdefault(filepath, []).append({
            "operation": operation, "filepath": filepath, "new_filepath": new_filepath,
            "mode_str": mode_str, "payload": payload, "error": None
        })

    def _print_summary(fp, errs, warns, aborted, count):
        if aborted:
            print(f"❌ Extracted with errors: {fp} ({count} operations)")
            for err in errs: print(f"  {err}")
            print(f"  [!] Aborted all modifications for {fp} due to errors.\n")
        elif warns:
            print(f"⚠️  Extracted with warnings: {fp} ({count} operations)")
            for w in warns: print(f"  {w}")
            print()
        else:
            op_text = "operation" if count == 1 else "operations"
            print(f"✅ Extracted: {fp} ({count} {op_text})")

    failed_files, shortened_files = [], []
    python_files_changed = False

    for filepath, tasks in tasks_by_file.items():
        errors, warnings = [], []
        for t in tasks:
            if t.get("error"): errors.append(t["error"])
        if errors:
            _print_summary(filepath, errors, warnings, aborted=True, count=len(tasks))
            failed_files.append(filepath)
            continue

        target_dir = os.path.dirname(filepath)
        if target_dir: os.makedirs(target_dir, exist_ok=True)

        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    original_text = f.read()
                    current_text = original_text
            except OSError as e:
                errors.append(f"Failed to read existing file {filepath}: {e}")
                _print_summary(filepath, errors, warnings, aborted=True, count=len(tasks))
                failed_files.append(filepath)
                continue
        else:
            original_text = None
            current_text = ""

        file_mutated, mode_int, file_deleted, renamed_to, copied_to = False, None, False, None, None

        try:
            for task in tasks:
                op = task["operation"]
                payload = task["payload"].replace("\r\n", "\n")
                if current_text: current_text = current_text.replace("\r\n", "\n")
                mode_str = task["mode_str"]

                if mode_str:
                    try: mode_int = int(mode_str, 8)
                    except ValueError: raise ValueError(f"Invalid mode format: {mode_str}. Must be octal.")

                if op in ("delete", "remove"):
                    file_deleted = True
                    break
                elif op == "rename":
                    if not task["new_filepath"]: raise ValueError("Rename requires 'New-Path: <target>'")
                    if not os.path.exists(filepath): raise FileNotFoundError(f"Cannot rename missing file: {filepath}")
                    renamed_to = task["new_filepath"]
                elif op == "copy":
                    if not task["new_filepath"]: raise ValueError("Copy requires 'New-Path: <target>'")
                    if not os.path.exists(filepath): raise FileNotFoundError(f"Cannot copy missing file: {filepath}")
                    copied_to = task["new_filepath"]
                elif op == "chmod":
                    if not mode_str: raise ValueError("Chmod requires 'Mode: <octal_string>'")
                    if not os.path.exists(filepath) and not file_mutated: raise FileNotFoundError(f"Cannot chmod missing file: {filepath}")
                elif op == "overwrite":
                    current_text = payload
                    if not payload.strip(): warnings.append("[WARN] Extracted payload is empty.")
                    file_mutated = True
                elif op == "append":
                    if current_text and not current_text.endswith("\n"): current_text += "\n"
                    current_text += payload
                    if not payload.strip(): warnings.append("[WARN] Appended payload is empty.")
                    file_mutated = True
                elif op == "search-and-replace":
                    if not os.path.exists(filepath) and not file_mutated:
                        raise FileNotFoundError(f"Cannot search-and-replace missing file: {filepath}")
                    search_blocks = parse_search_replace_blocks(payload)
                    if not search_blocks: raise ValueError("Malformed search-and-replace block. Missing markers.")

                    for block in search_blocks:
                        search_text, replace_text = block["search"], block["replace"]
                        new_text = None
                        if filepath.endswith(".py"):
                            new_text = fuzzy_line_replace(current_text, search_text, replace_text, filepath)
                            if new_text is None: new_text = semantic_token_replace(current_text, search_text, replace_text, filepath)
                            if new_text is None: new_text = fuzzy_token_replace(current_text, search_text, replace_text, filepath)
                            if new_text is None: new_text = whitespace_agnostic_replace(current_text, search_text, replace_text, filepath)
                        elif filepath.endswith(".md"):
                            new_text = fuzzy_line_replace(current_text, search_text, replace_text, filepath)
                            if new_text is None: new_text = semantic_markdown_replace(current_text, search_text, replace_text, filepath)
                            if new_text is None: new_text = boundary_markdown_replace(current_text, search_text, replace_text, filepath)
                            if new_text is None: new_text = fuzzy_markdown_replace(current_text, search_text, replace_text, filepath)
                            if new_text is None: new_text = whitespace_agnostic_replace(current_text, search_text, replace_text, filepath)
                        elif filepath.endswith(".xml"):
                            new_text = fuzzy_line_replace(current_text, search_text, replace_text, filepath)
                            if new_text is None: new_text = semantic_xml_replace(current_text, search_text, replace_text, filepath)
                            if new_text is None: new_text = whitespace_agnostic_replace(current_text, search_text, replace_text, filepath)
                        else:
                            new_text = fuzzy_line_replace(current_text, search_text, replace_text, filepath)
                            if new_text is None: new_text = whitespace_agnostic_replace(current_text, search_text, replace_text, filepath)

                        if new_text is None: raise ValueError("Semantic token, fuzzy line, and whitespace fallback all failed for search block.")
                        current_text = new_text
                    file_mutated = True

            if file_mutated:
                if filepath.endswith((".py", ".xml", ".md", ".js", ".html")):
                    current_text = re.sub(r"[ \t]+$", "", current_text, flags=re.MULTILINE)

                if original_text is not None and current_text == original_text:
                    raise ValueError("UI Data Loss Prevention: The generated file is exactly identical to the original file. This indicates elided contents, usually due to a failure to URL-encode \"<\" and \">\".")

                validate_syntax_in_memory(filepath, current_text)
                lint_errs, lint_warns = lint_file_content(filepath, current_text)
                if lint_errs: errors.extend(lint_errs)
                if lint_warns: warnings.extend(lint_warns)

        except (ValueError, FileNotFoundError, OSError, SyntaxError, TypeError, AttributeError) as e:
            errors.append(str(e))

        if errors:
            _print_summary(filepath, errors, warnings, aborted=True, count=len(tasks))
            failed_files.append(filepath)
            continue

        try:
            if file_deleted:
                if os.path.exists(filepath): os.remove(filepath)
                print(f"✅ Deleted: {filepath}")
                if filepath.endswith(".py"): python_files_changed = True
                continue
            if renamed_to:
                os.makedirs(os.path.dirname(renamed_to), exist_ok=True)
                os.rename(filepath, renamed_to)
                print(f"✅ Renamed: {filepath} -> {renamed_to}")
                if filepath.endswith(".py") or renamed_to.endswith(".py"): python_files_changed = True
                continue
            if copied_to:
                os.makedirs(os.path.dirname(copied_to), exist_ok=True)
                shutil.copy2(filepath, copied_to)
                print(f"✅ Copied: {filepath} -> {copied_to}")
                if filepath.endswith(".py") or copied_to.endswith(".py"): python_files_changed = True
                continue

            is_shortened = False
            shortened_str = ""
            if file_mutated:
                if original_text is not None:
                    orig_lines = len(original_text.splitlines())
                    new_lines = len(current_text.splitlines())
                    if new_lines < orig_lines:
                        is_shortened = True
                        shortened_str = f"{filepath} ({orig_lines} -> {new_lines} lines)"
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(current_text)

            if mode_int is not None: os.chmod(filepath, mode_int)

            if file_mutated:
                if is_shortened: shortened_files.append(shortened_str)
                if filepath.endswith(".py"): python_files_changed = True

            _print_summary(filepath, errors, warnings, aborted=False, count=len(tasks))

        except OSError as e:
            errors.append(f"Commit failed: {e}")
            _print_summary(filepath, errors, warnings, aborted=True, count=len(tasks))
            failed_files.append(filepath)

    if failed_files:
        print(f"\n❌ Failed to extract: {', '.join(failed_files)}")
    if shortened_files:
        print("\n" + "!" * 80)
        print("WARNING: THE FOLLOWING FILES BECAME SHORTER!")
        for sf in shortened_files: print(f"  - {sf}")
        print("!" * 80 + "\n")

    if python_files_changed:
        generate_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generate_pot.py")
        if os.path.exists(generate_script):
            print("\n[*] Python files modified. Synchronizing i18n/hams_master.pot...")
            try:
                res = subprocess.run([sys.executable, generate_script], capture_output=True, text=True)
                if res.returncode == 0: print("✅ i18n/hams_master.pot synchronized successfully.")
                else: print(f"⚠️  Failed to synchronize POT file:\n{res.stderr or res.stdout}")
            except OSError as e:
                print(f"⚠️  Failed to execute generate_pot.py: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        try:
            with open(sys.argv[1], "r", encoding="utf-8") as f:
                input_data = f.read()
        except FileNotFoundError:
            print(f"❌ Error: File '{sys.argv[1]}' not found.\n")
            sys.exit(1)
    else:
        input_data = sys.stdin.read()
    extract_parcel(input_data)
