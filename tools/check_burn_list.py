#!/usr/bin/env python3
"""
Odoo DevSecOps AST Linter (ADR-0083, ADR-0022)

This script parses all source code (Python, JS, XML, CSV) into Abstract Syntax Trees (AST)
to physically block security vulnerabilities, linter evasion, and framework anti-patterns.

Future AI sessions MUST read this docstring. If this linter fails your code:
1. DO NOT attempt to obfuscate or bypass the check using string concatenation or `getattr`. The AST parser will trace it.
2. DO NOT wrap the offending logic in `try/except` to hide it.
3. FIX the underlying architectural violation described in the diagnostic message.
4. If the pattern is a false positive or an explicitly authorized exception, use the appropriate `# burn-ignore-...` or `audit-ignore-...` tag, AND append a tracing anchor.
"""
import os
import re
import sys
import ast
import argparse
import xml.parsers.expat
import html.parser
import glob


class XMLNode:
    def __init__(self, tag, attrs, lineno):
        self.tag = tag
        self.attrs = attrs
        self.lineno = max(1, lineno)
        self.end_lineno = self.lineno
        self.children = []
        self.parent = None
        self.text = ""

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def get_ancestors(self):
        anc = []
        p = self.parent
        while p:
            anc.append(p)
            p = p.parent
        return anc


class OdooHTMLParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = XMLNode("root_wrapper", {}, 1)
        self.stack = [self.root]
        self.void_elements = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}

    def handle_starttag(self, tag, attrs):
        node = XMLNode(tag, dict(attrs), self.getpos()[0])
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in self.void_elements:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag in self.void_elements:
            return
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack[i].end_lineno = self.getpos()[0]
                self.stack = self.stack[:i]
                break

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].text += data

    def handle_comment(self, data):
        node = XMLNode("#comment", {"text": data}, self.getpos()[0])
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)

def parse_odoo_html(content):
    parser = OdooHTMLParser()
    parser.feed(content)
    return parser.root

def parse_odoo_xml(content):
    """Safely parses Odoo XML handling edge cases like bare ampersands and inline logic."""
    def preserve_lines(match):
        return re.sub(r"[^\n]", " ", match.group(0))

    content_clean = re.sub(
        r"^\s*\x3c\?xml[^\x3e]*\?\x3e",
        preserve_lines,
        content,
        flags=re.IGNORECASE,
    )
    content_clean = re.sub(
        r"\x3c!DOCTYPE[^\x3e]*\x3e",
        preserve_lines,
        content_clean,
        flags=re.IGNORECASE,
    )
    content_clean = content_clean.replace("&", "&amp;")

    wrapped = f"\x3croot_wrapper\x3e{content_clean}\x3c/root_wrapper\x3e"
    parser = xml.parsers.expat.ParserCreate()
    stack = []
    root = None

    def start_element(name, attrs):
        node = XMLNode(name, attrs, parser.CurrentLineNumber)
        if stack:
            node.parent = stack[-1]
            stack[-1].children.append(node)
        else:
            nonlocal root
            root = node
        stack.append(node)

    def end_element(name):
        if stack:
            node = stack.pop()
            node.end_lineno = parser.CurrentLineNumber

    def char_data(data):
        if stack:
            stack[-1].text += data

    def comment(data):
        node = XMLNode("#comment", {"text": data}, parser.CurrentLineNumber)
        if stack:
            node.parent = stack[-1]
            stack[-1].children.append(node)

    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    parser.CharacterDataHandler = char_data
    parser.CommentHandler = comment

    parser.Parse(wrapped, True)
    return root


# -------------------------------------------------------------------------
# REGEX-BASED GLOBAL SCANNERS
# -------------------------------------------------------------------------

GENERAL_ERROR_RULES = [
    (
        r"hooks\.py$",
        re.compile(r"\bin\s+[a-zA-Z0-9_]+\._fields\b"),
        "CRITICAL ANTI-DEFENSIVE PATTERN: Do not check for field existence in hooks. Assume schema is correct and fail fast.",
    ),
    (
        r"\.(py|js|xml)$",
        re.compile(r"(?i)#\s*(TODO|FIXME|\.\.\.|insert logic|implement later)"),
        "CRITICAL AI LAZINESS: Placeholders, TODOs, and elisions (...) are strictly forbidden. You must write complete, functional code.",
    ),
    (
        r"\.py$",
        re.compile(r"sys\.path\.append\(os\.path\.abspath\(os\.path\.join\(os\.path\.dirname\(__file__\),\s*['\"]\.\.['\"]"),
        "CRITICAL HALLUCINATION: Unnecessary sys.path.append with '..'. Local modules and daemons should resolve sibling imports natively.",
    ),
    (
        r"^(?!.*daemons?/|.*parcel_extract\.py).*\.py$",
        re.compile(r"sys\.path\.(append|insert)\([^)]*__file__[^)]*\)"),
        "CRITICAL HALLUCINATION: Redundant sys.path manipulation. Python automatically resolves imports from the script's directory.",
    ),
    (
        r"(?:/|^)daemons?/(?!.*parcel_extract\.py).*\.py$",
        re.compile(r"^\s*(?:import\s+odoo\b|from\s+odoo\b)"),
        "CRITICAL DAEMON DECOUPLING: Standalone daemons and their tests MUST NOT import Odoo modules or testing decorators. They must run entirely isolated.",
    ),
    (
        r"\.js$",
        re.compile(r"\.bindPopup\(\s*`|\.innerHTML\s*=\s*`"),
        "JS DOM XSS: Template literal passed to bindPopup or innerHTML.",
    ),
    (
        r"\.py$",
        re.compile(r"except\s+ImportError\s*:"),
        "CRITICAL AI FAILURE: Wrapping imports in try/except ImportError is forbidden. Use manifest external_dependencies.",
    ),
    (
        r"test_.*\.py$",
        re.compile(r"(?<![\'\"])(?:urllib\.request\.urlretrieve|requests\.(?:get|post|put|delete))\s*\("),
        "CRITICAL TEST ISOLATION: Tests must not make real external HTTP requests. Mock the network call (e.g., via unittest.mock.patch).",
    ),
    (
        r"^(?!.*(?:/|^)tools/).*\.py$",
        re.compile(r"(127\.0\.0\.1|localhost)"),
        "CRITICAL NETWORK HARDCODING: 'localhost' and '127.0.0.1' are prohibited. In containerized environments, these resolve to the container's internal loopback, NOT the target service. Use Docker DNS names (e.g., 'odoo', 'redis').",
    ),
    (
        r"\.py$",
        re.compile(r"\bdatetime\.datetime\.now\(\)|\bdatetime\.date\.today\(\)"),
        "CRITICAL TIMEZONE BUG: Native datetime fetching bypasses Odoo's timezone context. Use 'odoo.fields.Datetime.now()' or 'odoo.fields.Date.context_today(self)'.",
    ),
    (
        r"test_.*\.py$",
        re.compile(r"['\"]/tmp(?:/|['\"])"),
        "CRITICAL TEST REALISM / PATHING: Hardcoding '/tmp' is forbidden. Tests must use the exact same paths as the production environment per AGENTS.md.",
    ),
    (
        r".*tour\.js$",
        re.compile(r"run:\s*['\"]text['\"]"),
        "CRITICAL TOUR ARCHITECTURE: 'text' is banned in JS tours. Use 'edit' instead to ensure proper mounting and event firing.",
    ),
    (
        r".*tour\.js$",
        re.compile(r"trigger:\s*['\"][^'\"]*:contains\("),
        "CRITICAL TOUR ARCHITECTURE: The ':contains' pseudo-selector is banned in Odoo 19 UI tours as it causes DOMExceptions on un-mounted elements.",
    ),
    (
        r".*tour\.js$",
        re.compile(r"trigger:\s*['\"]\.(modal|o_technical_modal)['\"],\s*run:\s*function"),
        "CRITICAL TOUR RACE CONDITION: Do not poll the DOM natively for modals. You MUST use TourUtils.waitForAbsence or TourUtils.waitForElement to ensure they are mounted properly.",
    ),
    (
        r"test_.*\.py$",
        re.compile(r"class\s+[A-Za-z0-9_]+\s*\(\s*(?:TransactionCase|HttpCase)\s*\)\s*:"),
        "CRITICAL ARCHITECTURE: Tests must inherit from HamsTransactionCase or HamsHttpCase. Direct Odoo base class inheritance is forbidden.",
    ),
    (
        r"test_.*\.py$",
        re.compile(r"(?:@patch\b|with\s+patch\b|with\s+patch\.object\b)"),
        "CRITICAL ARCHITECTURE: Native patch decorators and context managers are forbidden. Use self.safe_patch() or self.safe_patch_object().",
    ),
    (
        r"\.py$",
        re.compile(r"os\.(?:environ\.get|getenv)\s*\(\s*['\"][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASS|API|CRED)[A-Za-z0-9_]*['\"]", re.IGNORECASE),
        "CRITICAL TENANT LEAK: Do not use environment variables as fallbacks for credentials in multi-tenant systems. This breaks isolation. Use configuration models or secure daemon key registries instead.",
    ),
    (
        r"\.(py|js|xml|csv)$",
        re.compile(r"\[[^\]\n]+\]\(https?://[^)\n]+\)"),
        "CRITICAL MARKDOWN BLEED: Found a Markdown-formatted URL `[text](url)` in a non-Markdown file. The Web UI occasionally corrupts raw URLs into markdown links. You MUST output URLs as raw strings.",
    ),
    (
        r"test_.*\.py$",
        re.compile(r"(?:@unittest\.skip\b|@skip\b|@skipIf\b|@skipUnless\b|\bskipTest\s*\()"),
        "CRITICAL AI LAZINESS: The use of skipTest or @skip decorators is strictly forbidden. You must repair the test instead of skipping it.",
    ),
    (
        r"\.py$",
        re.compile(r"tools\.mute_logger\(['\"]odoo\.sql_db['\"]\)", re.IGNORECASE),
        "CRITICAL AI LAZINESS: Muting odoo.sql_db is strictly forbidden. It hides critical PostgreSQL RAISE EXCEPTION tracebacks from the test runner. Fail fast.",
    ),
    (
        r"\.py$",
        re.compile(r"except\s+AttributeError\b"),
        "CRITICAL AI LAZINESS: Catch-all AttributeError is forbidden. Let missing dependencies or broken schema contracts fail loudly.",
    ),
    (
        r"\.py$",
        re.compile(r"except\s+KeyError\b"),
        "CRITICAL AI LAZINESS: Catch-all KeyError is forbidden. Let missing models in self.env fail fast.",
    ),
    (
        r"\.py$",
        re.compile(r"in\s+dir\("),
        "CRITICAL AI LAZINESS: 'in dir()' is forbidden. It hides values that should be present. Fix the architecture.",
    ),
    (
        r"\.py$",
        re.compile(r"registry\.models"),
        "CRITICAL AI LAZINESS: Checking 'registry.models' to avoid soft-dependency failures is forbidden. Declare your dependencies.",
    ),
]

ODOO_ERROR_RULES = [
    (
        r"\.py$",
        re.compile(r"['\"]test_enable['\"]"),
        "CRITICAL TEST EVASION: The use of 'test_enable' to bypass code execution during tests is strictly forbidden. Use RealTransactionCase for commit handling.",
    ),
    (
        r"\.py$",
        re.compile(r"\bEnvironment\s*\(\s*[^,]+,\s*(?:uid\s*=\s*)?(?:1|odoo\.SUPERUSER_ID|SUPERUSER_ID)\b"),
        "CRITICAL ZERO-SUDO VIOLATION: Instantiating Environment with SUPERUSER_ID or 1 is a sudo bypass cheat. Query for a service account ID instead.",
    ),
    (
        r"\.py$",
        re.compile(r"['\"]groups_id['\"]\s*:"),
        "CRITICAL BIAS TRAP: Odoo 18+ normalized the res.users groups relation to 'group_ids'.",
    ),
    (
        r"\.py$",
        re.compile(r"^\s*_sql_constraints\s*="),
        "CRITICAL DEPRECATION: Use 'models.Constraint' class attributes instead.",
    ),
    (
        r"\.py$",
        re.compile(r"\bget_module_resource\b"),
        "CRITICAL DEPRECATION: 'get_module_resource' was removed in Odoo 19. Use 'odoo.tools.file_open'.",
    ),
    (
        r"\.py$",
        re.compile(r"\bget_resource_path\b"),
        "CRITICAL DEPRECATION: 'get_resource_path' was removed in Odoo 19. Use 'odoo.tools.misc.file_path'.",
    ),
    (
        r"controllers/.*\.py$",
        re.compile(r"@(?:tools\.)?ormcache"),
        "CRITICAL ARCHITECTURE: Cannot use @ormcache on Controller methods.",
    ),
    (
        r"^(?!.*daemon_key_manager/).*\.(py|js|xml|csv)$",
        re.compile(r"res\.users\.apikeys"),
        "CRITICAL SECURITY: Odoo native RPC bearer token allocation (res.users.apikeys) is forbidden. Use 'daemon_key_manager' instead.",
    ),
    (
        r"\.js$",
        re.compile(r"registry\.category\(\s*['\"](tours|web_tours)['\"]\s*\)"),
        "CRITICAL JS TOUR REGISTRATION: Odoo 19 UI tours MUST be registered under 'web_tour.tours', not 'tours' or 'web_tours'.",
    ),
    (
        r"\.js$",
        re.compile(r"isCheck\s*:\s*true"),
        "CRITICAL JS TOUR DEPRECATION: 'isCheck: true' is removed in Odoo 19. Use 'run: () => {}' instead.",
    ),
    (
        r"\.js$",
        re.compile(r"test\s*:\s*true"),
        "CRITICAL JS TOUR DEPRECATION: 'test: true' is removed in Odoo 19. Tours are discovered automatically.",
    ),
    (
        r"\.js$",
        re.compile(r"\bwillUnmount\b"),
        "CRITICAL OWL 2 DEPRECATION: 'willUnmount' is removed in OWL 2. Use 'onWillUnmount' from '@odoo/owl'.",
    ),
    (
        r"\.js$",
        re.compile(r"publicWidget\.registry|['\"]web\.public\.widget['\"]|@web/legacy/js/public/public_widget"),
        "CRITICAL OWL ARCHITECTURE: Legacy publicWidget is deprecated in Odoo 19. You MUST use the modern Interaction pattern (@web/public/interaction) and mountComponent.",
    ),
    (
        r"tour.*\.js$|.*_tour\.js$",
        re.compile(r"\.o_form_readonly"),
        "FRAGILE TOUR TRIGGER: '.o_form_readonly' is frequently unreliable in Odoo 19 tours. Use standard buttons, '.o_form_button_create', or 'body' instead.",
    ),
    (
        r"tour.*\.js$|.*_tour\.js$",
        re.compile(r"\.o_notification_success"),
        "FRAGILE TOUR TRIGGER: '.o_notification_success' changed in Odoo 19. Use standard notification classes or avoid waiting on toasts.",
    ),
    (
        r"\.js$",
        re.compile(r"\.o_form_saved_indicator"),
        "CRITICAL JS TOUR DEPRECATION: '.o_form_saved_indicator' was removed in Odoo 19. Do not use it.",
    ),
    (
        r"\.js$",
        re.compile(r"[^\s/]\s*;\s*(?:import|export)\b"),
        "CRITICAL JS ASSET PARSER: 'import' and 'export' keywords MUST be at the start of a new line or preceded only by spaces. Inline imports after a semicolon (e.g., `let a=1; import X`) crash the Odoo 19 regex transpiler."
    ),
    (
        r"\.js$",
        re.compile(r"export\s*\{[^\}]*(?://|/\*)[^\}]*\}", re.DOTALL),
        "CRITICAL JS ASSET PARSER: Inline comments inside an export object block (e.g., `export { a, // comment \\n b }`) break Odoo's module loader. Place comments outside the export block."
    ),
    (
        r"\.js$",
        re.compile(r"import\s+.*?\s+from\s+['\"](?!\.|@)[^'\"]+/[^'\"]+['\"]"),
        "CRITICAL JS PATH AMBIGUITY: Imports containing a '/' that do not start with '.' (relative) or '@' (Odoo alias) confuse the asset parser. Odoo will attempt to resolve it as a physical file path and throw an 'Unmet Dependencies' error."
    ),
    (
        r"\.js$",
        re.compile(r"^(?!\s*//|\s*/\*\*|\s*\*|\s*(?:import|export)\b)[^\n]*?(?:import|export)\s+.*?from\s+['\"][^'\"]+['\"]", re.MULTILINE),
        "CRITICAL JS ASSET PARSER: Do not embed `import ... from ...` strings inside inline multi-line variable strings. Odoo's regex heuristic will falsely trigger on it and corrupt the transpilation."
    ),
    (
        r"\.js$",
        re.compile(r"\$\("),
        "jQuery ($) is forbidden. Use Vanilla JS or modern OWL components.",
    ),
    (
        r"\.js$",
        re.compile(r'useService\s*\(\s*["\']company["\']\s*\)'),
        "useService('company') is deprecated in modern Odoo frontends.",
    ),
    (
        r"\.js$",
        re.compile(r'useService\s*\(\s*["\']rpc["\']\s*\)'),
        "CRITICAL OWL DEPRECATION: The raw 'rpc' service is deprecated. Use 'useService(\"orm\")' instead, unless explicitly burning this rule for a non-ORM controller.",
    ),
    (
        r"\.(py|js)$",
        re.compile(
            r"['\"]state['\"]\s*,\s*['\"](=|in)['\"]\s*,\s*(?:\[\s*)?['\"](open|closed)['\"]"
        ),
        "CRITICAL DEPRECATION: survey.survey 'state' field was removed in Odoo 19. Use 'active' (Boolean).",
    ),
    (
        r"\.py$",
        re.compile(r"['\"]detailed_type['\"]\s*:"),
        "CRITICAL DEPRECATION: 'detailed_type' on product.template was reverted to 'type' in Odoo 19.",
    ),
    (
        r"\.py$",
        re.compile(r"\bwith_context\s*\(\s*allowed_company_ids\s*="),
        "CRITICAL MULTI-TENANT BYPASS: Manually injecting 'allowed_company_ids' via with_context is forbidden per ADR-0083. You MUST use the architecturally mandated '.with_company(company_id)' method.",
    ),
    (
        r"\.(py|js|xml)$",
        re.compile(
            r"['\"](account\.move|account\.payment|res\.partner\.bank|payment\.token|payment\.transaction)['\"]|\.bank_ids|\.payment_token_ids"
        ),
        "CRITICAL FINANCIAL EXPOSURE: Access to financial models or relational fields is forbidden without 'burn-ignore-financial' and an anchor.",
    ),
    (
        r"\.xml$",
        re.compile(r"\x3ctree\b"),
        "CRITICAL DEPRECATION: The \x3ctree\x3e tag is banned in Odoo 19. Use \x3clist\x3e instead.",
    ),
    (
        r"\.xml$",
        re.compile(
            r"hasclass\(['\"]card['\"]\)|hasclass\(['\"]field-.*['\"]\)|//label\[@for='.*'\]|//button\[@string='.*'\]"
        ),
        "FRAGILE XPATH: Targeting 'hasclass(\"card\")', 'hasclass(\"field-*\")', labels by 'for', or buttons by 'string' is banned. Target robust structural elements.",
    ),
    (
        r"tour.*\.js$|.*_tour\.js$",
        re.compile(r"trigger:\s*['\"`].*?(?:\.o_app|\.nav-link|\.o_menu_brand|h[1-6]:contains).*?['\"`]"),
        "FRAGILE TOUR TRIGGER: Odoo 19 UI shifted. Do not use '.o_app', '.nav-link', '.o_menu_brand', or 'h1:contains' in tour triggers. Use structure-agnostic selectors like '[data-menu-xmlid=...]' or '*:contains'.",
    ),
    (
        r"tour.*\.js$|.*_tour\.js$",
        re.compile(r"trigger:\s*['\"`](?:.*[\s,>])?(?:select|option)(?:[\[:#.\s].*?)?['\"`]"),
        "CRITICAL JS TOUR DEPRECATION: Native \x3cselect\x3e and \x3coption\x3e tags are removed from backend form views in Odoo 19. Target '.o_select_menu' and '.o_select_menu_item' instead.",
    ),
    (
        r"\.js$",
        re.compile(r"window\.location"),
        "CRITICAL TOUR NAVIGATION: 'window.location' is banned. Use 'document.location'.",
    ),
    (
        r"tour.*\.js$|.*_tour\.js$",
        re.compile(r"trigger:\s*['\"`].*?\bselect\b.*?['\"`]"),
        "FRAGILE TOUR TRIGGER: Native \x3cselect\x3e elements are deprecated in Odoo 19 form views. Use '.o_select_menu' and '.o_select_menu_item' instead.",
    ),
    (
        r"tour.*\.js$|.*_tour\.js$",
        re.compile(r"trigger:\s*['\"`].*?:contains.*?['\"`]"),
        "CRITICAL JS TOUR SYNTAX: Odoo 19 native triggers use querySelectorAll, which crashes instantly on jQuery's ':contains' pseudo-selector. Target elements by name, id, or structural classes instead.",
    ),
    (
        r"tour.*\.js$|.*_tour\.js$",
        re.compile(r"run:\s*['\"`]text\b"),
        "CRITICAL JS TOUR ACTION: 'text' is not a valid action in Odoo 19. Use 'edit' to simulate text input.",
    ),
    (
        r"tour.*\.js$|.*_tour\.js$",
        re.compile(r"base\.menu_custom"),
        "CRITICAL TOUR DEPRECATION: The Technical menu (base.menu_custom) is strictly hidden/removed in this environment. Do not target it in UI tours.",
    ),
    (
        r"\.py$",
        re.compile(r"env\[['\"]ir\.module\.module['\"]\]\.search\("),
        "CRITICAL FRAMEWORK ACL: Searching 'ir.module.module' directly fails without 'base.group_user'. You MUST inject the 'zero_sudo.odoo_facility_service_internal' context via .with_user().",
    ),
    (
        r"\.(py|js)$",
        re.compile(r"['\"]/web#.*?['\"]"),
        "CRITICAL ROUTING DEPRECATION: Hash-based routing (/web#...) is deprecated and forcefully redirected in Odoo 19. Use query parameters (/odoo?...) instead.",
    ),
    (
        r"\.(py|js)$",
        re.compile(r"['\"]/web/(?!login\b|signup\b|assets\b|static\b)[^'\"]*['\"]"),
        "CRITICAL ROUTING DEPRECATION: /web is deprecated and forcefully redirected to /odoo in Odoo 19, losing the query parameters! Use /odoo instead.",
    ),
    (
        r"tour.*\.js$|.*_tour\.js$",
        re.compile(r"\.\.\.TourUtils\.safeSave\("),
        "CRITICAL JS TOUR MINIFIER CRASH: Do not use the ES6 spread operator (...) inside tour step arrays. Odoo's rjsmin minifier corrupts it and throws 'Unexpected token :'. Use .concat(TourUtils.safeSave()) instead.",
    ),
    (
        r"tour.*\.js$|.*_tour\.js$",
        re.compile(r"\.ui-menu-item\b"),
        "CRITICAL JS TOUR DEPRECATION: jQuery autocomplete (.ui-menu-item) is removed in Odoo 19. Target '.o-autocomplete--dropdown-item' or '.dropdown-item' instead, and ensure you use TourUtils.deterministicInput() instead of 'edit' to trigger the dropdown.",
    ),
    (
        r"tour.*\.js$|.*_tour\.js$",
        re.compile(r"trigger:\s*['\"`]\.o_form_button_save['\"`]"),
        "CRITICAL JS TOUR LATENCY: Raw triggers on the save button are banned. You MUST use '.concat(TourUtils.safeSave())' to ensure the DOM blur and RPC resolution complete safely before the test runner tears down the environment.",
    ),
    (
        r"test_.*\.py$",
        re.compile(r"class\s+[a-zA-Z0-9_]+\s*\((?:HttpCase|TransactionCase)\):"),
        "CRITICAL TEST ARCHITECTURE: Do not inherit directly from Odoo's native HttpCase or TransactionCase. You MUST inherit from HamsHttpCase or HamsTransactionCase to ensure the Process Reaper and latency safeguards are active.",
    ),
]

WARNING_RULES = [
    (
        r"\.py$",
        re.compile(r"zipfile\.ZipFile|tarfile\.open"),
        "[%AUDIT] PATH TRAVERSAL / ARCHIVE EXTRACTION: Ensure zip/tar extraction includes a check for symlink bits (e.g., `external_attr`) to prevent slip attacks.",
    ),
]
MULTILINE_WARNING_RULES = []
EXEMPTIONS = {}
REQUIRE_TEST_VERIFICATION = []
FOUND_TEST_CONTENTS = {}
FOUND_TOURS = []
FOUND_MANIFESTS = {}


# -------------------------------------------------------------------------
# AST VULNERABILITY VISITOR (PYTHON)
# -------------------------------------------------------------------------

def check_ast_vulnerabilities(filepath, content, lines, is_odoo_module=False):
    errors = []
    warnings = []
    filename = os.path.basename(filepath)
    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError as e:
        errors.append((e.lineno or 1, f"CRITICAL SYNTAX/INDENTATION ERROR: {e.msg}"))
        return errors, warnings

    class TaintVisitor(ast.NodeVisitor):
        def __init__(self, filename, lines, is_odoo_module, filepath=""):
            self.errors = []
            self.warnings = []
            self.assignments = {}
            self.loop_depth = 0
            self.in_http_controller = False
            self.in_real_transaction_case = False
            self.filename = filename
            self.filepath = filepath
            self.lines = lines
            self.is_odoo_module = is_odoo_module
            self._assignment_stack = set()
            self.current_method = None
            self.current_decorators = []
            self.current_kwarg_name = None
            self.defined_functions = {
                n.name
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }

            self.has_ham_base = False
            if self.filepath:
                current = os.path.abspath(self.filepath)
                if os.path.isfile(current):
                    current = os.path.dirname(current)
                while current and current != os.path.dirname(current):
                    if os.path.exists(os.path.join(current, "ham_base", "__manifest__.py")):
                        self.has_ham_base = True
                        break
                    current = os.path.dirname(current)

        def add_error(self, lineno, msg):
            if lineno <= len(self.lines) and "burn-ignore" in self.lines[lineno - 1]:
                return
            self.errors.append((lineno, msg))

        def add_warning(self, lineno, msg):
            if lineno <= len(self.lines):
                line_content = self.lines[lineno - 1]
                if (
                    "burn-ignore" in line_content
                    or ("audit-ignore-mail" in line_content and "Mail Templates" in msg)
                    or (
                        "audit-ignore-search" in line_content
                        and "Data Integrity" in msg
                    )
                    or ("audit-ignore-i18n" in line_content and "I18N" in msg)
                    or ("audit-ignore-path" in line_content and "PATH TRAVERSAL" in msg)
                ):
                    return
            self.warnings.append((lineno, msg))

        def is_tainted_sql(self, node):
            """Recursively traces variables to detect if they contain f-strings or concatenation before being passed to cr.execute()"""
            if isinstance(node, ast.JoinedStr):
                return "f-string"
            if isinstance(node, ast.BinOp):
                if isinstance(node.op, ast.Mod):
                    return "percent interpolation"
                if isinstance(node.op, ast.Add):
                    return "string concatenation"
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
            ):
                is_safe_sql = (
                    isinstance(node.func.value, ast.Call)
                    and getattr(node.func.value.func, "attr", "") == "SQL"
                )
                if not is_safe_sql:
                    return ".format()"
            if isinstance(node, ast.Name):
                if node.id in self._assignment_stack:
                    return False
                if node.id in self.assignments:
                    self._assignment_stack.add(node.id)
                    res = self.is_tainted_sql(self.assignments[node.id])
                    self._assignment_stack.remove(node.id)
                    if res:
                        return f"variable '{node.id}' assigned via {res}"
            return False

        def is_untranslated_string(self, node):
            """Detects raw strings that are not wrapped in Odoo's _() translation function."""
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value.strip()
                if (
                    len(val) < 5
                    or " " not in val
                    or val.upper().startswith(
                        ("SELECT ", "UPDATE ", "INSERT ", "DELETE ")
                    )
                ):
                    return False
                return True
            elif isinstance(node, ast.JoinedStr):
                return True
            elif isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Mod, ast.Add)
            ):
                return self.is_untranslated_string(node.left)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                    return self.is_untranslated_string(node.func.value)
                if isinstance(node.func, ast.Name) and node.func.id == "_":
                    return False
            return False

        def visit_Compare(self, node):
            if self.is_odoo_module:
                if len(node.ops) == 1 and isinstance(node.ops[0], ast.In):
                    comp = node.comparators[0]
                    if isinstance(comp, ast.Attribute) and comp.attr == "env":
                        if getattr(comp.value, "id", "") == "self" or getattr(comp.value, "id", "") == "request":
                            self.add_error(
                                node.lineno,
                                "CRITICAL ARCHITECTURE: Soft-dependency checking (`'model' in self.env`) is forbidden. You MUST explicitly declare dependencies in __manifest__.py.",
                            )
                    if isinstance(comp, ast.Attribute) and comp.attr == "modules" and getattr(comp.value, "id", "") == "sys":
                        self.add_error(
                            node.lineno,
                            "CRITICAL ARCHITECTURE: Probing `sys.modules` for Odoo addons is forbidden (test evasion). Declare dependencies in __manifest__.py.",
                        )
            self.generic_visit(node)

        def visit_BinOp(self, node):
            if isinstance(node.op, ast.Add):
                def is_string_node(n):
                    if isinstance(n, ast.Constant) and isinstance(n.value, str):
                        return True
                    if isinstance(n, ast.JoinedStr):
                        return True
                    return False

                if is_string_node(node.left) and is_string_node(node.right):
                    self.add_error(
                        getattr(node, "lineno", 1),
                        "CRITICAL STRING CONCATENATION: Using '+' to concatenate two string literals is forbidden to prevent linter evasion.",
                    )
            self.generic_visit(node)

        def visit_With(self, node):
            if self.is_odoo_module:
                for item in node.items:
                    if isinstance(item.context_expr, ast.Call):
                        func_name = getattr(item.context_expr.func, "id", getattr(item.context_expr.func, "attr", ""))
                        if func_name == "suppress":
                            self.add_error(node.lineno, "CRITICAL AI LAZINESS: contextlib.suppress() is strictly forbidden as it acts as a silent black hole for errors. Explicitly catch and log exceptions.")
                is_cursor = any(
                    isinstance(item.context_expr, ast.Call)
                    and isinstance(item.context_expr.func, ast.Attribute)
                    and item.context_expr.func.attr == "cursor"
                    for item in node.items
                )
                if is_cursor:
                    for child in ast.walk(node):
                        if (
                            isinstance(child, ast.Call)
                            and isinstance(child.func, ast.Attribute)
                            and child.func.attr in ("commit", "rollback")
                        ):
                            self.add_error(
                                child.lineno,
                                "CURSOR MISMANAGEMENT: Do not manually call commit() or rollback() inside a `with registry.cursor():` block.",
                            )

                for item in node.items:
                    if isinstance(item.context_expr, ast.Call) and getattr(item.context_expr.func, "attr", "") in ("assertRaises", "assertRaisesRegex"):
                        has_create_write = False
                        has_flush = False
                        for child in ast.walk(node):
                            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                                if child.func.attr in ("create", "write"):
                                    has_create_write = True
                                elif child.func.attr == "flush_all":
                                    has_flush = True
                        if has_create_write and not has_flush:
                            self.add_error(
                                node.lineno,
                                "[!] DIAGNOSTIC FOR AI: ORM create/write inside assertRaises requires self.env.flush_all() before the context manager exits to trigger @api.constrains."
                            )

            self.generic_visit(node)

        def visit_Dict(self, node):
            keys_found = set()
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys_found.add(k.value)
                    if self.is_odoo_module:
                        if k.value in (
                            "error",
                            "success",
                            "warning",
                            "message",
                        ) and self.is_untranslated_string(v):
                            if not re.search(r".*_?api\.py$", self.filename):
                                self.add_warning(
                                    node.lineno,
                                    f"[%AUDIT] I18N: Untranslated string assigned to UI feedback dict key '{k.value}'.",
                                )
                        if k.value == "groups_id":
                            self.add_error(
                                node.lineno, "[!] DIAGNOSTIC FOR AI: Odoo 18+ normalized the res.users groups relation to 'group_ids'."
                            )
                        if k.value == "group_ids" and not self.filename.startswith("test_"):
                            self.add_error(
                                node.lineno,
                                "[!] DIAGNOSTIC FOR AI: Mutating 'group_ids' in Python is forbidden. Define privileges statically in XML/CSV."
                            )
                    if self.filename == "__manifest__.py" and k.value == "assets":
                        if isinstance(v, ast.Dict):
                            for b_val in v.values:
                                if isinstance(b_val, ast.List):
                                    for elt in b_val.elts:
                                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str) and "*" in elt.value:
                                            self.add_error(elt.lineno, "CRITICAL ASSET COMPILER CRASH: Glob patterns (*) are strictly forbidden in __manifest__.py asset lists. Odoo's asset compiler fails silently when matching directories. You MUST enumerate every file explicitly.")
            if self.is_odoo_module and "owner_user_id" in keys_found and "user_websites_group_id" in keys_found:
                self.add_error(
                    node.lineno,
                    "MUTUAL EXCLUSIVITY TRAP: Cannot assign both 'owner_user_id' and 'user_websites_group_id'.",
                )
            self.generic_visit(node)

        def visit_For(self, node):
            is_chunking_loop = False
            if (
                isinstance(node.iter, ast.Call)
                and getattr(node.iter.func, "id", "") == "range"
                and len(node.iter.args) == 3
            ):
                step_arg = node.iter.args[2]
                if isinstance(step_arg, ast.Name) and step_arg.id in (
                    "chunk_size",
                    "batch_size",
                ):
                    is_chunking_loop = True
            if not is_chunking_loop:
                self.loop_depth += 1
            self.generic_visit(node)
            if not is_chunking_loop:
                self.loop_depth -= 1

        def visit_ClassDef(self, node):
            is_real_txn = any(
                getattr(base, "id", "") == "RealTransactionCase"
                or getattr(base, "attr", "") == "RealTransactionCase"
                or getattr(base, "id", "") in ("HamsHttpCase", "HttpCase")
                or getattr(base, "attr", "") in ("HamsHttpCase", "HttpCase")
                for base in node.bases
            )
            old_val = self.in_real_transaction_case
            self.in_real_transaction_case = old_val or is_real_txn
            self.generic_visit(node)
            self.in_real_transaction_case = old_val

        def _check_test_empty(self, node):
            """Blocks dead-code testing evasion tactics."""
            if self.filename.startswith("test_") and node.name.startswith("test_"):
                calls_external = any(
                    isinstance(child, ast.Call)
                    and getattr(child.func, "id", getattr(child.func, "attr", None))
                    not in self.defined_functions
                    for child in ast.walk(node)
                )
                if not calls_external:
                    self.add_error(
                        node.lineno,
                        f"[!] DIAGNOSTIC FOR AI: Empty test detected. Test '{node.name}' must actually execute target logic.",
                    )

                for i, stmt in enumerate(node.body):
                    if (
                        isinstance(
                            stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)
                        )
                        and i < len(node.body) - 1
                    ):
                        self.add_error(
                            stmt.lineno,
                            f"[!] DIAGNOSTIC FOR AI: AST Evasion Detected. Unreachable code detected after return/raise/break/continue in '{node.name}'. Tests must execute fully.",
                        )

        def visit_FunctionDef(self, node):
            if node.name.startswith("_patched_") or node.name.startswith("patched_"):
                if not node.args.vararg or not node.args.kwarg:
                    self.add_error(
                        node.lineno,
                        "[!] DIAGNOSTIC FOR AI: Monkey-patch wrapper functions MUST include *args and **kwargs to absorb unexpected framework arguments."
                    )

            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                if not self.filename.startswith("test_"):
                    self.add_error(node.lineno, "[!] DIAGNOSTIC FOR AI: Empty functions using 'pass' are forbidden. Implement the logic or remove the method.")

            is_controller = any(
                (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "route"
                )
                or (isinstance(dec, ast.Attribute) and dec.attr == "route")
                for dec in node.decorator_list
            )
            if self.is_odoo_module:
                for dec in node.decorator_list:
                    if (
                        isinstance(dec, ast.Attribute)
                        and dec.attr == "returns"
                        and getattr(dec.value, "id", "") == "api"
                    ) or (
                        isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "returns"
                        and getattr(dec.func.value, "id", "") == "api"
                    ):
                        self.add_error(node.lineno, "@api.returns is deprecated.")

            self._check_test_empty(node)

            old_assignments, old_http, old_method, old_decorators, old_kwarg = (
                self.assignments.copy(),
                self.in_http_controller,
                self.current_method,
                self.current_decorators,
                self.current_kwarg_name,
            )
            self.assignments, self.in_http_controller, self.current_method = (
                {},
                is_controller,
                node.name,
            )
            self.current_decorators = [
                (
                    dec.attr
                    if isinstance(dec, ast.Attribute)
                    else (
                        getattr(dec.func, "attr", "")
                        if hasattr(dec, "func")
                        else getattr(dec, "id", "")
                    )
                )
                for dec in node.decorator_list
            ]
            self.current_kwarg_name = node.args.kwarg.arg if node.args.kwarg else None

            self.generic_visit(node)

            (
                self.assignments,
                self.in_http_controller,
                self.current_method,
                self.current_decorators,
                self.current_kwarg_name,
            ) = (old_assignments, old_http, old_method, old_decorators, old_kwarg)

        def visit_Assign(self, node):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.assignments[target.id] = node.value
                elif self.is_odoo_module:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "context"
                        and isinstance(target.value, ast.Attribute)
                        and target.value.attr == "env"
                        and getattr(target.value.value, "id", "") == "self"
                    ):
                        self.add_error(
                            node.lineno,
                            "Never modify `self.env.context` directly. Use `self.with_context()`.",
                        )
                    elif isinstance(target, ast.Name) and target.id == "_sql_constraints":
                        self.add_error(
                            node.lineno,
                            "[!] DIAGNOSTIC FOR AI: Use 'models.Constraint' instead of '_sql_constraints'.",
                        )
                    elif isinstance(target, ast.Attribute) and target.attr == "group_ids" and not self.filename.startswith("test_"):
                        self.add_error(
                            node.lineno,
                            "[!] DIAGNOSTIC FOR AI: Mutating 'group_ids' in Python is forbidden. Define privileges statically in XML/CSV.",
                        )
                    elif isinstance(target, ast.Subscript) and getattr(target, "slice", None):
                        slice_val = getattr(target.slice, "value", None)
                        if slice_val == "group_ids" and not self.filename.startswith("test_"):
                            self.add_error(
                                node.lineno,
                                "[!] DIAGNOSTIC FOR AI: Mutating 'group_ids' in Python is forbidden. Define privileges statically in XML/CSV.",
                            )
                        elif slice_val in ("error", "success", "warning", "message") and self.is_untranslated_string(node.value):
                            self.add_warning(
                                node.lineno,
                                "[%AUDIT] I18N: Untranslated string assigned to dict key.",
                            )
            self.generic_visit(node)

        def visit_Import(self, node):

            if getattr(self, 'current_method', None):
                self.add_error(
                    node.lineno,
                    "LOCAL IMPORT: Imports inside functions/methods are strictly forbidden."
                )

            for alias in node.names:
                if alias.name == "pickle":
                    self.add_error(
                        node.lineno, "CRITICAL RCE: The pickle module is vulnerable."
                    )
                elif alias.name == "random":
                    self.add_error(node.lineno, "WEAK CRYPTO: Do not use 'random'.")
            self.generic_visit(node)

        def visit_Expr(self, node):
            if isinstance(node.value, ast.Constant) and node.value.value is Ellipsis:
                self.add_error(node.lineno, "CRITICAL AI LAZINESS: Elision (...) is strictly forbidden. Write complete code.")
            self.generic_visit(node)

        def visit_Tuple(self, node):
            if len(node.elts) == 3:
                if isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "id":
                    if isinstance(node.elts[1], ast.Constant) and node.elts[1].value in ("=", "in"):
                        if isinstance(node.elts[2], ast.Constant) and type(node.elts[2].value) is int:
                            self.add_error(node.lineno, "CRITICAL AI LAZINESS: Hardcoded ID lookup ('id', '=', int). Use self.env.ref() or immutable string keys.")
                        elif isinstance(node.elts[2], ast.List):
                            if all(isinstance(elt, ast.Constant) and type(elt.value) is int for elt in node.elts[2].elts):
                                self.add_error(node.lineno, "CRITICAL AI LAZINESS: Hardcoded ID lookup ('id', 'in', [int, ...]). Use self.env.ref() or immutable string keys.")
            self.generic_visit(node)

        def visit_ExceptHandler(self, node):
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                self.add_error(
                    getattr(node, 'lineno', 1),
                    "[!] DIAGNOSTIC FOR AI: Empty exception handlers using 'pass' are forbidden. Log the error or handle it."
                )
            self.generic_visit(node)

        def visit_Try(self, node):
            if getattr(self, "has_ham_base", False):
                for child in ast.walk(node):
                    if isinstance(child, ast.Call) and isinstance(getattr(child, "func", None), ast.Attribute) and child.func.attr == "_get_service_uid":
                        self.add_error(
                            getattr(child, "lineno", node.lineno),
                            "CRITICAL FAST FAIL: _get_service_uid MUST NOT be wrapped in a try/except block. It must fail fast if the service account is missing."
                        )
            for handler in node.handlers:
                if (
                    isinstance(handler.type, ast.Name)
                    and handler.type.id == "ImportError"
                ):
                    self.add_error(
                        node.lineno,
                        "CRITICAL AI FAILURE: Wrapping imports in try/except ImportError is forbidden. Use manifest external_dependencies.",
                    )
                is_catch_all = handler.type is None or (isinstance(handler.type, ast.Name) and handler.type.id == "Exception")
                if is_catch_all:
                    handler_line = getattr(handler, "lineno", node.lineno)
                    line_content = self.lines[handler_line - 1] if handler_line <= len(self.lines) else ""
                    if "audit-ignore-catch-all" not in line_content:
                        self.add_error(
                            handler_line,
                            "[!] DIAGNOSTIC FOR AI: Catch-all exceptions (bare or Exception) are forbidden. Target specific exceptions (e.g., KeyError, ValueError). Use # audit-ignore-catch-all ONLY where an operation must continue past failure.",
                        )
                    else:
                        has_logging = any(
                            isinstance(child, ast.Call) and getattr(child.func, "attr", "") in ("warning", "error", "critical", "exception", "info")
                            for child in ast.walk(handler)
                        )
                        if not has_logging:
                            self.add_error(
                                handler_line,
                                "CRITICAL SILENT FAILURE: Even with audit-ignore-catch-all, the exception block must contain a logging call to prevent swallowed tracebacks.",
                            )
            self.generic_visit(node)

        def visit_ImportFrom(self, node):

            if getattr(self, 'current_method', None):
                self.add_error(
                    node.lineno,
                    "LOCAL IMPORT: Imports inside functions/methods are strictly forbidden."
                )

            if node.module == "pickle":
                self.add_error(
                    node.lineno, "CRITICAL RCE: The pickle module is vulnerable."
                )
            elif node.module == "random":
                self.add_error(node.lineno, "WEAK CRYPTO: Do not use 'random'.")
            elif self.is_odoo_module and getattr(node, "module", "") == "odoo.modules" and any(
                alias.name == "get_module_resource" for alias in node.names
            ):
                self.add_error(
                    node.lineno,
                    "CRITICAL DEPRECATION: 'get_module_resource' is removed.",
                )
                
            if any(alias.name == "SUPERUSER_ID" for alias in node.names):
                self.add_error(
                    node.lineno,
                    "[!] DIAGNOSTIC FOR AI: `.sudo()` and `SUPERUSER_ID` are completely forbidden on this platform to prevent privilege escalation. Use the service account architecture (`with_user()`) instead."
                )
                
            self.generic_visit(node)

        def visit_Constant(self, node):
            if isinstance(node.value, str):
                if self.is_odoo_module:
                    if re.search(r" numbercall ", node.value):
                        self.add_error(
                            node.lineno,
                            "Remove 'numbercall'. Odoo 18+ crons run indefinitely.",
                        )
                    if (
                        node.value == "res.users.apikeys"
                        and "key_registry.py" not in self.filename
                    ):
                        self.add_error(
                            node.lineno,
                            "CRITICAL SECURITY: Odoo native RPC bearer token allocation (res.users.apikeys) is forbidden. Use 'daemon_key_manager'.",
                        )
            self.generic_visit(node)

        def visit_Name(self, node):
            if self.is_odoo_module:
                if node.id == "numbercall":
                    self.add_error(node.lineno, "Remove 'numbercall'.")
                elif node.id == "_sql_constraints":
                    self.add_error(node.lineno, "[!] DIAGNOSTIC FOR AI: Use 'models.Constraint' instead of '_sql_constraints'.")
                elif node.id == "SUPERUSER_ID":
                    self.add_error(node.lineno, "[!] DIAGNOSTIC FOR AI: `.sudo()` and `SUPERUSER_ID` are completely forbidden on this platform to prevent privilege escalation. Use the service account architecture (`with_user()`) instead.")
            self.generic_visit(node)

        def visit_keyword(self, node):
            if node.arg == "shell" and getattr(node.value, "value", None) is True:
                self.add_error(
                    getattr(node, "lineno", 1),
                    "CRITICAL SHELL INJECTION: Avoid shell=True.",
                )
            if self.is_odoo_module:
                if node.arg == "numbercall":
                    self.add_error(getattr(node, "lineno", 1), "Remove 'numbercall'.")
                elif node.arg == "groups_id":
                    self.add_error(
                        getattr(node, "lineno", 1),
                        "[!] DIAGNOSTIC FOR AI: Do not use 'groups_id'. Odoo 19 requires 'group_ids'.",
                    )
                elif node.arg == "group_ids" and not self.filename.startswith("test_"):
                    self.add_error(
                        getattr(node, "lineno", 1),
                        "[!] DIAGNOSTIC FOR AI: Mutating 'group_ids' in Python is forbidden. Define privileges statically in XML/CSV.",
                    )
                elif node.arg in ("oldname", "select"):
                    self.add_error(
                        getattr(node, "lineno", 1),
                        f"CRITICAL DEPRECATION: '{node.arg}' is a legacy attribute.",
                    )
                elif node.arg == "type" and getattr(node.value, "value", None) == "json":
                    self.add_error(getattr(node, "lineno", 1), "Use type='jsonrpc'.")
                elif node.arg == "index" and getattr(node.value, "value", None) == "trgm":
                    self.add_error(getattr(node, "lineno", 1), "Use index='trigram'.")
                elif (
                    node.arg == "csrf"
                    and getattr(node.value, "value", None) in (False, 0)
                    and not re.search(r".*_?api\.py$", self.filename)
                ):
                    self.add_error(
                        getattr(node, "lineno", 1),
                        "SECURITY ALERT: csrf=False found outside an API.",
                    )
                elif node.arg == "related" and getattr(node.value, "value", "").endswith(
                    ".users"
                ):
                    self.add_error(
                        getattr(node, "lineno", 1),
                        "Legacy security relation: Use 'user_ids'.",
                    )
            self.generic_visit(node)

        def visit_Attribute(self, node):
            if self.is_odoo_module:
                if node.attr == "sudo":
                    line_content = (
                        self.lines[node.lineno - 1]
                        if node.lineno <= len(self.lines)
                        else ""
                    )
                    if not (
                        "# burn-ignore-sudo" in line_content  # fmt: skip
                        and (
                            "sudo()._generate(" in line_content
                            or
                            ".sudo().unlink()" in line_content
                        )
                    ):
                        self.add_error(
                            node.lineno,
                            "[!] DIAGNOSTIC FOR AI: `.sudo()` is completely forbidden on this platform to prevent privilege escalation. Use the service account architecture (`with_user()`) instead.",
                        )
                if node.attr == "testing":
                    if isinstance(node.value, ast.Call) and getattr(node.value.func, "attr", "") == "current_thread":
                        self.add_error(
                            node.lineno,
                            "CRITICAL ARCHITECTURE: Probing `threading.current_thread().testing` is forbidden test evasion.",
                        )
                if getattr(node.value, "id", "") == "self":
                    if node.attr in ("_context", "_uid"):
                        self.add_error(
                            node.lineno, f"Use 'self.env.{node.attr.strip('_')}'."
                        )
                elif node.attr == "users" and getattr(
                    node.value, "id", getattr(node.value, "attr", "")
                ) in ("group", "groups", "_group_id"):
                    self.add_error(node.lineno, "Legacy security relation: Use 'user_ids'.")
            self.generic_visit(node)

        def _check_forbidden_functions(self, node):
            if not isinstance(node.func, ast.Name):
                return
            fid = node.func.id
            if fid == "hasattr":
                # Allow hasattr(super(), ...) for cooperative mixin architecture
                is_super = False
                if node.args and isinstance(node.args[0], ast.Call) and isinstance(node.args[0].func, ast.Name) and node.args[0].func.id == "super":
                    is_super = True
                if not is_super:
                    self.add_error(
                        node.lineno,
                        "CRITICAL AI LAZINESS: The use of hasattr() is strictly forbidden to prevent masking architectural type uncertainties.",
                    )
            elif fid == "hash":
                self.add_error(
                    node.lineno,
                    "CRITICAL NON-DETERMINISM: Python's native `hash()` is salted...",
                )
            elif fid == "eval":
                self.add_error(node.lineno, "CRITICAL RCE: Never use native eval()...")
            elif fid == "exec":
                self.add_error(
                    node.lineno,
                    "CRITICAL RCE: The use of exec() is strictly forbidden.",
                )
            elif self.is_odoo_module:
                if fid == "get_module_resource":
                    self.add_error(
                        node.lineno,
                        "CRITICAL DEPRECATION: 'get_module_resource' was removed in Odoo 19. Use 'odoo.tools.file_open'.",
                    )
                elif fid == "_sign_token":
                    self.add_error(
                        node.lineno,
                        "Verify '_sign_token' is not called on models lacking an 'access_token' field...",
                    )
                elif fid == "clear_caches":
                    self.add_error(
                        node.lineno,
                        "ORM cache invalidation in Odoo 19+ MUST use targeted `.clear_cache(self)` or `self.env.registry.clear_cache()`.",
                    )
                elif fid == "_check_recursion":
                    self.add_error(node.lineno, "Odoo 18+ Hierarchy: Use '_has_cycle()'...")
                elif fid == "getattr":
                    if len(node.args) >= 2 and getattr(node.args[1], "value", None) == "sudo":
                        self.add_error(
                            node.lineno,
                            "[!] DIAGNOSTIC FOR AI: Obfuscated use of sudo via getattr() detected and blocked.",
                        )
                    if len(node.args) >= 3 or node.keywords:
                        self.add_error(
                            node.lineno,
                            "CRITICAL AI LAZINESS: 3-argument getattr() is forbidden to prevent silently defaulting on missing schema attributes. Access fields directly to ensure the schema contract is enforced.",
                        )
                elif (
                    fid == "setattr"
                    and len(node.args) >= 2
                    and getattr(node.args[1], "value", None) == "group_ids"
                    and not self.filename.startswith("test_")
                ):
                    self.add_error(
                        node.lineno,
                        "[!] DIAGNOSTIC FOR AI: Mutating 'group_ids' via setattr is forbidden. Define privileges statically.",
                    )

        def _check_i18n_messages(self, node, func_name):
            if re.search(r".*_?api\.py$", self.filename) or not self.is_odoo_module:
                return
            if func_name in ("UserError", "AccessError", "ValidationError"):
                if node.args and self.is_untranslated_string(node.args[0]):
                    self.add_warning(
                        node.lineno,
                        f"[%AUDIT] I18N: User-facing exception message in '{func_name}' should be wrapped in _() for translation.",
                    )
            elif func_name in ("message_post", "message_subscribe"):
                for kw in node.keywords:
                    if kw.arg in ("body", "subject") and self.is_untranslated_string(
                        kw.value
                    ):
                        self.add_warning(
                            node.lineno,
                            f"[%AUDIT] I18N: User-facing chatter '{kw.arg}' in {func_name} should be wrapped in _() for translation.",
                        )

        def _check_forbidden_attributes(self, node, attr):
            is_cr_execute = False
            if (
                attr == "execute"
                and getattr(node.func.value, "attr", getattr(node.func.value, "id", ""))
                == "cr"
            ):
                is_cr_execute = True

            if attr == "system" and getattr(node.func.value, "id", "") == "os":
                self.add_error(
                    node.lineno, "CRITICAL SECURITY: 'os.system' is banned due to shell injection vulnerabilities. Use 'subprocess.run' with array arguments."
                )
            if attr in ("loads", "dumps") and getattr(node.func.value, "id", "") == "pickle":
                self.add_error(
                    node.lineno, "CRITICAL RCE: The pickle module is vulnerable."
                )
            elif attr in ("md5", "sha1") and getattr(node.func.value, "id", "") == "hashlib":
                self.add_error(node.lineno, "WEAK CRYPTO: MD5/SHA1 broken.")
            elif attr in ("choice", "randint", "random") and getattr(node.func.value, "id", "") == "random":
                self.add_error(node.lineno, "WEAK CRYPTO: Do not use 'random'.")

            if self.is_odoo_module:
                if attr == "send_mail":
                    self.add_warning(
                        node.lineno, "[%AUDIT] Mail Templates: Verify model_id matches."
                    )
                elif attr == "_sign_token":
                    self.add_error(node.lineno, "Verify '_sign_token' context...")
                elif attr == "clear_caches":
                    self.add_error(
                        node.lineno,
                        "CRITICAL DEPRECATION: `clear_caches()` is removed. Use `self.method.clear_cache(self)` or `self.env.registry.clear_cache()`.",
                    )
                elif (
                    attr in ("search", "create", "browse")
                    and getattr(node.func.value, "id", "") == "self"
                ):
                    self.add_error(
                        node.lineno, "Ambiguous ORM call: Use `self.env['your.model']...`"
                    )
                elif attr in ("with_user", "with_context"):
                    if attr == "with_user" and getattr(node, "args", []):
                        arg0 = node.args[0]
                        if isinstance(arg0, ast.Constant) and arg0.value == 1:
                            self.add_error(node.lineno, "CRITICAL ZERO-SUDO VIOLATION: Using .with_user(1) is a sudo bypass cheat. Query for a designated service account ID instead.")
                        elif getattr(arg0, "id", "") == "SUPERUSER_ID" or getattr(arg0, "attr", "") == "SUPERUSER_ID":
                            self.add_error(node.lineno, "CRITICAL ZERO-SUDO VIOLATION: Using .with_user(SUPERUSER_ID) is a sudo bypass cheat. Query for a designated service account ID instead.")
                    caller = node.func.value
                    if isinstance(caller, ast.Name) and caller.id == "env":
                        self.add_error(
                            node.lineno,
                            f"CRITICAL ORM ERROR: Cannot call `.{attr}()` directly on the Environment object. Call it on a RecordSet (e.g., `env['model'].{attr}(...)`)."
                        )
                    elif isinstance(caller, ast.Attribute) and caller.attr == "env" and getattr(caller.value, "id", "") == "self":
                        self.add_error(
                            node.lineno,
                            f"CRITICAL ORM ERROR: Cannot call `.{attr}()` directly on the Environment object. Call it on a RecordSet (e.g., `self.env['model'].{attr}(...)`)."
                        )
                elif attr == "_check_recursion":
                    self.add_error(node.lineno, "Odoo 18+ Hierarchy: Use '_has_cycle()'...")
                elif attr in ("message_post", "message_subscribe") and (
                    "res.users"
                    in (
                        ast.unparse(node.func.value).strip()
                        if hasattr(ast, "unparse")
                        else ""
                    )
                    or str(getattr(node.func.value, "attr", "")) in ("user_id", "user")
                ):
                    self.add_error(
                        node.lineno,
                        "Messaging & Followers: Do not call on res.users directly.",
                    )
                elif (
                    attr == "ref"
                    and len(node.args) == 1
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "base.group_user"
                ):
                    if not ("odoo_facility_service" in self.filename):
                        self.add_warning(
                            node.lineno,
                            "[%AUDIT] DOMAIN SANDBOX: Do not grant base.group_user (Internal User) in tests or logic. Only odoo_facility_service_internal may hold this.",
                        )
                elif (
                    attr == "sleep"
                    and getattr(node.func.value, "id", "") == "time"
                    and "audit-ignore-sleep"
                    not in (
                        self.lines[node.lineno - 1]
                        if node.lineno <= len(self.lines)
                        else ""
                    )
                ):
                    if not any(x in getattr(self, "filepath", self.filename).replace("\\", "/") for x in ("tools/", "daemon/", "daemons/")):
                        self.add_warning(
                            node.lineno,
                            "[%AUDIT] THREAD BLOCKING: 'time.sleep()' halts the worker...",
                        )
                elif attr == "Thread" and getattr(node.func.value, "id", "") == "threading":
                    self.add_error(node.lineno, "CRITICAL DOS VECTOR: Unbounded Thread.")

            if self.in_http_controller and self.is_odoo_module:
                if attr == "website" and getattr(node.func.value, "id", "") == "request":
                     self.add_warning(
                         node.lineno,
                         "[%AUDIT] MULTI-TENANT ISOLATION: When extracting 'request.website', ensure you immediately extract its '.id' or fallback to 0 for distributed cache keys (ADR-0083)."
                     )

                if (
                    attr == "get"
                    and getattr(node.func.value, "id", "") == self.current_kwarg_name
                ):
                    self.add_warning(
                        node.lineno,
                        "[%AUDIT] CONTROLLER BINDING: Ensure explicit inputs.",
                    )
                elif attr in ("create", "write") and (
                    any(
                        getattr(arg, "id", "") in ("kwargs", "kw", "post")
                        for arg in node.args
                    )
                    or any(
                        getattr(kw.value, "id", "") in ("kwargs", "kw", "post")
                        for kw in node.keywords
                        if kw.arg is None
                    )
                ):
                    self.add_warning(
                        node.lineno,
                        "[%AUDIT] RPC MASS ASSIGNMENT: Never pass raw request payloads directly to create/write.",
                    )
            return is_cr_execute

        def _check_search_methods(self, node, attr):
            if getattr(node.func.value, "id", "") == "re":
                return
            if attr == "search":
                if not any(
                    kw.arg == "limit" for kw in node.keywords
                ) and not self.filename.startswith("test_"):
                    self.add_warning(
                        node.lineno,
                        "[%AUDIT] UNBOUNDED SEARCH: '.search()' called without 'limit'. If this model is multi-tenant, also ensure explicitly filtering by 'company_id' or False.",
                    )
                if any(kw.arg == "count" for kw in node.keywords):
                    self.add_error(
                        node.lineno, "[!] DIAGNOSTIC FOR AI: Use `search_count(...)` instead of search with count=True."
                    )

            val = node.func.value
            is_env_subscript = (
                isinstance(val, ast.Subscript)
                and getattr(val.value, "attr", getattr(val.value, "id", "")) == "env"
            )
            if is_env_subscript and (
                self.current_method in ("create", "write")
                or (self.current_method or "").startswith(("_check_", "_validate_"))
                or any(d in self.current_decorators for d in ("constrains", "onchange"))
            ):
                self.add_warning(
                    node.lineno,
                    f"[%AUDIT] Data Integrity: Direct `{attr}()` on env without `.with_user()` micro-privilege context.",
                )

        def _check_cr_execute(self, node, is_cr_execute):
            """Blocks dynamic SQL construction."""
            if is_cr_execute and node.args:
                taint_reason = self.is_tainted_sql(node.args[0])
                if taint_reason:
                    self.add_error(
                        node.lineno,
                        f"[!] DIAGNOSTIC FOR AI: SQLi Prevention. Query constructed via {taint_reason} passed to cr.execute(). Use parameterized queries or psycopg2.sql.",
                    )

        def visit_Subscript(self, node):
            if self.is_odoo_module:
                val = getattr(node.value, "id", getattr(node.value, "attr", ""))
                if val == "config" and isinstance(node.slice, ast.Constant) and node.slice.value in ("test_enable", "test_file"):
                    self.add_error(node.lineno, "CRITICAL ARCHITECTURE: Probing `config['test_enable']` to evade execution is strictly forbidden.")
            self.generic_visit(node)

        def visit_Call(self, node):
            self._check_forbidden_functions(node)
            func_name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            self._check_i18n_messages(node, func_name)

            if func_name == "getattr" and len(node.args) >= 2:
                arg1, arg2 = node.args[0], node.args[1]
                if isinstance(arg1, ast.Call) and getattr(arg1.func, "attr", "") == "current_thread":
                    if isinstance(arg2, ast.Constant) and arg2.value == "testing":
                        self.add_error(node.lineno, "CRITICAL ARCHITECTURE: Probing `threading.current_thread().testing` via getattr is forbidden test evasion.")
            
            if func_name == "get" and isinstance(node.func, ast.Attribute):
                parent = getattr(node.func.value, "attr", getattr(node.func.value, "id", ""))
                if parent in ("registry", "models") or (isinstance(node.func.value, ast.Attribute) and getattr(node.func.value.value, "attr", "") in ("registry", "models")):
                    self.add_error(node.lineno, "CRITICAL ARCHITECTURE: Soft-dependency checking via `registry.get()` is forbidden. Declare dependencies in __manifest__.py.")

            if func_name in ("search", "search_count"):
                val = getattr(node.func, "value", None)
                if isinstance(val, ast.Subscript) and getattr(val.value, "attr", getattr(val.value, "id", "")) == "env":
                    if isinstance(val.slice, ast.Constant) and val.slice.value == "ir.module.module":
                        self.add_error(node.lineno, "CRITICAL ARCHITECTURE: Dynamic database querying for 'ir.module.module' is forbidden. Declare dependencies in __manifest__.py.")

            if func_name == "get" and isinstance(node.func, ast.Attribute):
                parent = getattr(node.func.value, "id", getattr(node.func.value, "attr", ""))
                if parent == "config":
                    if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in ("test_enable", "test_file"):
                        self.add_error(node.lineno, "CRITICAL ARCHITECTURE: Probing `config.get('test_enable')` to evade execution is strictly forbidden.")

            if func_name == "Environment" or (isinstance(node.func, ast.Attribute) and node.func.attr == "Environment"):
                uid_arg = None
                if len(node.args) >= 2:
                    uid_arg = node.args[1]
                else:
                    for kw in node.keywords:
                        if kw.arg == "uid":
                            uid_arg = kw.value
                            break
                if uid_arg:
                    is_su = False
                    if isinstance(uid_arg, ast.Constant) and uid_arg.value == 1:
                        is_su = True
                    elif isinstance(uid_arg, ast.Name) and uid_arg.id == "SUPERUSER_ID":
                        is_su = True
                    elif isinstance(uid_arg, ast.Attribute) and uid_arg.attr == "SUPERUSER_ID":
                        is_su = True
                    if is_su:
                        self.add_error(
                            node.lineno,
                            "CRITICAL ZERO-SUDO VIOLATION: Instantiating an Environment with SUPERUSER_ID or uid=1 is strictly forbidden (sudo cheat). Query for a service account ID instead."
                        )

            if isinstance(node.func, ast.Name) and node.func.id == "print":
                if not ("tools/" in getattr(self, "filepath", self.filename).replace("\\", "/") or self.filename == "check_burn_list.py"):
                    self.add_error(node.lineno, "CRITICAL AI LAZINESS: Native print() is banned. Use logging (_logger.info, etc.) for centralized log aggregation.")

            if func_name == "open" or (isinstance(node.func, ast.Attribute) and getattr(node.func, "attr", "") in ("open", "remove", "unlink", "symlink") and getattr(node.func.value, "id", "") == "os"):
                if self.in_http_controller or "model" in self.current_decorators:
                    self.add_warning(
                        node.lineno,
                        "[%AUDIT] PATH TRAVERSAL: Ensure paths passed to filesystem operations in RPC/controller methods are strictly sanitized against directory traversal (e.g., checking for '..')."
                    )

            if func_name in ("assertTrue", "assertFalse"):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, bool):
                    if (func_name == "assertTrue" and node.args[0].value is True) or (func_name == "assertFalse" and node.args[0].value is False):
                        self.add_error(node.lineno, f"CRITICAL AI LAZINESS: Hollow assertion {func_name}({node.args[0].value}) is banned. Assert against actual variables.")
                if node.args and isinstance(node.args[0], ast.BoolOp) and isinstance(node.args[0].op, ast.And):
                    self.add_error(node.lineno, f"CRITICAL TEST ANTI-PATTERN: {func_name} with multiple conditions (and). Split into individual assertions for precise diagnostics.")
            elif func_name == "Markup":
                if node.args:
                    arg0 = node.args[0]
                    if isinstance(arg0, ast.JoinedStr) or (isinstance(arg0, ast.Call) and getattr(arg0.func, "attr", "") == "format") or (isinstance(arg0, ast.BinOp) and isinstance(arg0.op, ast.Mod)):
                        self.add_error(node.lineno, "CRITICAL XSS VULNERABILITY: Do not interpolate variables directly into Markup(). Use odoo.tools.html_escape on user inputs first or use standard QWeb rendering.")
            elif func_name == "assertEqual" and len(node.args) == 2:
                arg1, arg2 = node.args[0], node.args[1]
                if type(arg1) == type(arg2):
                    if isinstance(arg1, ast.Constant) and arg1.value == arg2.value:
                        self.add_error(node.lineno, "CRITICAL AI LAZINESS: Hollow assertion (comparing identical literals) is banned.")
                    elif isinstance(arg1, ast.Name) and arg1.id == arg2.id:
                        self.add_error(node.lineno, "CRITICAL AI LAZINESS: Hollow assertion (comparing a variable to itself) is banned.")

            if getattr(node.func, "attr", getattr(node.func, "id", "")) == "env":
                for kw in node.keywords:
                    if kw.arg == "su" and self.is_odoo_module:
                        self.add_error(
                            node.lineno,
                            "CRITICAL PRIVILEGE ESCALATION: Environment modification with 'su=True' is strictly forbidden (Zero-Sudo evasion).",
                        )

            is_cr_execute = False
            attr = (
                getattr(node.func, "attr", "")
                if isinstance(node.func, ast.Attribute)
                else ""
            )

            if attr in ("commit", "rollback") and self.filename.startswith("test_") and self.is_odoo_module:
                val = getattr(node.func, "value", None)
                if isinstance(val, ast.Attribute) and val.attr == "cr":
                    if (
                        getattr(val.value, "id", "") == "env"
                        or getattr(val.value, "attr", "") == "env"
                    ):
                        if not self.in_real_transaction_case:
                            self.add_error(
                                node.lineno,
                                "TEST CURSOR CORRUPTION: Calling commit() or rollback() inside tests breaks the test cursor. Use RealTransactionCase.",
                            )

            if attr:
                is_cr_execute = self._check_forbidden_attributes(node, attr)

            if self.loop_depth > 0 and attr in ("search", "search_count", "read_group") and self.is_odoo_module:
                caller_id = (
                    getattr(node.func.value, "id", "")
                    if hasattr(node.func, "value")
                    else ""
                )
                if caller_id != "re" and "regex" not in caller_id:
                    self.add_error(
                        node.lineno,
                        f"[!] DIAGNOSTIC FOR AI: ORM '.{attr}()' inside a loop causes N+1 locking. Pre-fetch data outside the loop.",
                    )

            if attr in ("search", "search_count") and self.is_odoo_module:
                self._check_search_methods(node, attr)

            if func_name == "start_tour":
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    if "debug=" not in node.args[0].value:
                        self.add_error(
                            node.lineno,
                            "[!] DIAGNOSTIC FOR AI: start_tour() URLs MUST explicitly include 'debug=1' to prevent Owl 'dev' mode crashes per ADR-0081."
                        )

            self._check_cr_execute(node, is_cr_execute)

            if attr == "execute" and not is_cr_execute and self.is_odoo_module:
                if len(node.args) >= 2 and getattr(node.args[1], "value", None) in (
                    "search",
                    "search_read",
                    "read",
                ):
                    for arg in node.args[2:]:
                        if isinstance(arg, ast.Dict):
                            keys = [getattr(k, "value", None) for k in arg.keys]
                            if any(
                                k in ("fields", "limit", "offset", "order")
                                for k in keys
                            ):
                                self.add_error(
                                    node.lineno,
                                    "CRITICAL JSON-RPC KWARGS: Do not pass a dictionary of kwargs as a positional argument to search/search_read. Use explicit keyword arguments (e.g., fields=...).",
                                )

            if func_name == "symlink":
                is_os_symlink = False
                if isinstance(node.func, ast.Attribute) and getattr(node.func.value, "id", "") == "os":
                    is_os_symlink = True
                elif isinstance(node.func, ast.Name):
                    is_os_symlink = True

                if is_os_symlink and node.args:
                    def _resolve_str(n):
                        if isinstance(n, ast.Constant) and isinstance(n.value, str):
                            return n.value
                        if isinstance(n, ast.Name) and n.id in self.assignments:
                            return _resolve_str(self.assignments[n.id])
                        return None

                    src_val = _resolve_str(node.args[0])
                    if src_val:
                        check_paths = [
                            src_val,
                            os.path.join(os.path.dirname(self.filepath), src_val),
                            os.path.join(os.getcwd(), src_val)
                        ]
                        for p in check_paths:
                            try:
                                if os.path.isdir(p) and os.path.isfile(os.path.join(p, '__manifest__.py')):
                                    self.add_error(
                                        node.lineno,
                                        "CRITICAL ARCHITECTURE: Creating symbolic links to resolve modules (like zero_sudo or distributed_redis_cache) is strictly forbidden. You MUST configure the Odoo --addons-path correctly instead."
                                    )
                                    break
                            except Exception:
                                pass

            self.generic_visit(node)

    visitor = TaintVisitor(filename, lines, is_odoo_module, filepath=filepath)
    visitor.visit(tree)
    return visitor.errors, visitor.warnings


# -------------------------------------------------------------------------
# FILE INGESTION & XML SCANNING
# -------------------------------------------------------------------------

def scan_file(filepath, is_odoo_module=False):
    filename = os.path.basename(filepath)
    if filename == "check_burn_list.py":
        return [], []

    errors_found, warnings_found = [], []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
            lines = content.splitlines()
    except Exception as e:
        return [f"Could not read file: {e}"], []

    if filename == "__manifest__.py":
        try:
            tree = ast.parse(content, filename=filepath)
            for node in tree.body:
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Dict):
                    manifest_dict = ast.literal_eval(node.value)
                    valid_licenses = [
                        "GPL-2", "GPL-2 or any later version", "GPL-3",
                        "GPL-3 or any later version", "AGPL-3", "LGPL-3",
                        "Other OSI approved licence", "OEEL-1", "OPL-1",
                        "Other proprietary"
                    ]
                    if "license" not in manifest_dict:
                        errors_found.append(f"Line {node.lineno}: CRITICAL MANIFEST ERROR: 'license' key is missing. It MUST be present and set to a valid Odoo license (e.g., 'Other proprietary').")
                    elif manifest_dict.get("license") not in valid_licenses:
                        errors_found.append(f"Line {node.lineno}: CRITICAL MANIFEST ERROR: Invalid 'license' value '{manifest_dict.get('license')}'. Valid options are: {', '.join(valid_licenses)}. Note: 'Other proprietary' must use a lowercase 'p'.")
        except Exception:
            pass

    if is_odoo_module and filename.endswith(".csv"):
        financial_models = [
            "model_res_partner_bank",
            "model_account_tax",
            "model_res_company",
            "model_account_move",
            "model_account_payment",
            "model_payment_token",
            "model_payment_transaction",
            "model_account_journal",
        ]
        for i, line in enumerate(lines, 1):
            stripped_line = line.strip()
            if not stripped_line:
                if i < len(lines):
                    errors_found.append(f"Line {i}: CRITICAL CSV FORMAT: Blank lines are forbidden in Odoo CSV files.")
                continue
            if stripped_line.startswith("#"):
                errors_found.append(f"Line {i}: CRITICAL CSV FORMAT: Comments (#) are forbidden in Odoo CSV files.")

            if line.startswith("id,"):
                continue
            parts = line.split(",")
            if len(parts) >= 3:
                model_id = parts[2]
                if any(model_id == f_model for f_model in financial_models):
                    errors_found.append(
                        f"Line {i}: CRITICAL FINANCIAL EXPOSURE: Granting access to '{model_id}' in custom ir.model.access.csv is strictly forbidden."
                    )

    if is_odoo_module and filename.endswith((".xml", ".html")):
        try:
            if filename.endswith(".html"):
                root_node = parse_odoo_html(content)
            else:
                root_node = parse_odoo_xml(content)

            for node in root_node.walk():
                if node.tag != "#comment":
                    if node.text and "[@ANCHOR:" in node.text:
                        errors_found.append(f"Line {node.lineno}: CRITICAL ANCHOR FORMAT: Semantic anchors in XML/HTML MUST be enclosed within comments ().")
                    for attr_name, attr_val in node.attrs.items():
                        if "[@ANCHOR:" in str(attr_val):
                            errors_found.append(f"Line {node.lineno}: CRITICAL ANCHOR FORMAT: Semantic anchors in XML/HTML MUST be enclosed within comments (). Found in attribute.")

                if node.tag == "template" or (
                    node.tag == "record" and node.attrs.get("model") == "ir.ui.view"
                ):
                    has_tour = any(
                        [
                            "[@ANCHOR:" in child.attrs.get("text", "")
                            for child in node.walk()
                            if child.tag == "#comment"
                        ]
                    )
                    if (
                        not has_tour
                        and node.parent
                        and node.parent.children.index(node) > 0
                    ):
                        prev = node.parent.children[
                            node.parent.children.index(node) - 1
                        ]
                        if prev.tag == "#comment" and "[@ANCHOR:" in prev.attrs.get(
                            "text", ""
                        ):
                            has_tour = True
                    if not has_tour:
                        raw_text = "\n".join(
                            lines[
                                max(0, node.lineno - 2) : min(
                                    len(lines), node.end_lineno + 1
                                )
                            ]
                        )
                        if (
                            "[@ANCHOR:" in raw_text
                            or "burn-ignore-tour" in raw_text
                            or "audit-ignore-view" in raw_text
                        ):
                            has_tour = True
                    if not has_tour:
                        errors_found.append(
                            f"Line {node.lineno}: UI TOUR MANDATE VIOLATION: All XML views (\x3crecord model='ir.ui.view'\x3e) and templates must be tested by a UI tour (include an '\x3c!-- [@ANCHOR: ...] --\x3e' comment linking to the tour) or explicitly bypassed using '\x3c!-- audit-ignore-view --\x3e' if a tour is unjustified."
                        )
                    if node.attrs.get("inherit_id") in (
                        "website.snippet_options",
                        "web_editor.snippet_options",
                    ):
                        errors_found.append(
                            f"Line {node.lineno}: CRITICAL DEPRECATION: snippet_options inheritance is highly volatile/removed in Odoo 19. Do not use it."
                        )
                if (
                    node.tag == "record"
                    and node.attrs.get("model") in ("ir.rule", "res.groups")
                    and not any(
                        anc.tag == "data"
                        and anc.attrs.get("noupdate") in ("1", "True", "true")
                        for anc in node.get_ancestors()
                    )
                ):
                    errors_found.append(
                        f"Line {node.lineno}: CRITICAL SECURITY: \x3crecord\x3e must be inside noupdate data block."
                    )
                if node.tag == "record":
                    model_name = node.attrs.get("model")
                    defined_fields = {child.attrs.get("name") for child in node.children if child.tag == "field"}

                    mandatory_model_fields = {
                        "res.users": {"name", "login", "company_id", "company_ids", "notification_type"},
                        "ir.rule": {"name", "model_id"},
                        "ir.model.access": {"name", "model_id", "group_id"},
                        "ir.ui.view": {"name", "model"},
                        "ir.actions.act_window": {"name", "res_model"},
                        "ir.cron": {"name", "model_id", "user_id"},
                        "res.groups": {"name"},
                        "res.company": {"name"},
                        "res.partner": {"name"},
                    }

                    if model_name in mandatory_model_fields:
                        required = mandatory_model_fields[model_name]
                        missing = required - defined_fields
                        if missing:
                            errors_found.append(
                                f"Line {node.lineno}: CRITICAL XML DATA INTEGRITY: \x3crecord model='{model_name}'\x3e is missing mandatory fields required in Odoo 19: {', '.join(missing)}. This causes silent installation failures."
                            )

                    if model_name == "res.users" and any(anc.tag == "data" and anc.attrs.get("noupdate") in ("1", "True", "true") for anc in node.get_ancestors()):
                        warnings_found.append(
                            f"Line {node.lineno}: [%AUDIT] RECORD UPDATE: \x3crecord model='res.users'\x3e is inside a noupdate='1' block. If this service account requires updates in the future, Odoo will ignore them."
                        )

                if node.tag == "record" and node.attrs.get("model") == "ir.rule":
                    has_group = any(
                        child.tag == "field" and child.attrs.get("name") == "groups"
                        for child in node.children
                    )
                    if not has_group:
                        errors_found.append(
                            f"Line {node.lineno}: CRITICAL SECURITY: ir.rule must specify a 'groups' field. Global rules (no group) are deprecated and banned."
                        )
                    for child in node.children:
                        if (
                            child.tag == "field"
                            and child.attrs.get("name") == "model_id"
                        ):
                            ref = child.attrs.get("ref", "")
                            financial_models = [
                                "model_res_partner_bank",
                                "model_account_tax",
                                "model_res_company",
                                "model_account_move",
                                "model_account_payment",
                                "model_payment_token",
                                "model_payment_transaction",
                                "model_account_journal",
                            ]
                            if ref in financial_models:
                                errors_found.append(
                                    f"Line {node.lineno}: CRITICAL FINANCIAL EXPOSURE: Creating ir.rule for '{ref}' is strictly forbidden."
                                )
                if node.tag == "xpath" and node.attrs.get("position") not in (
                    "inside",
                    "replace",
                    "before",
                    "after",
                    "attributes",
                ):
                    errors_found.append(f"Line {node.lineno}: INVALID XPATH position.")
                if node.tag == "field" and "name" not in node.attrs:
                    errors_found.append(
                        f"Line {node.lineno}: CRITICAL XML missing name."
                    )
                if "t-raw" in node.attrs:
                    errors_found.append(f"Line {node.lineno}: CRITICAL XSS: use t-out.")
                if "t-esc" in node.attrs:
                    errors_found.append(f"Line {node.lineno}: CRITICAL DEPRECATION: t-esc is banned. Use t-out.")
                if "attrs" in node.attrs:
                    errors_found.append(f"Line {node.lineno}: CRITICAL DEPRECATION: The 'attrs' attribute was removed in Odoo 17+. Use invisible, readonly, and required directly.")
                if node.attrs.get("t-name") == "kanban-box":
                    errors_found.append(f"Line {node.lineno}: CRITICAL DEPRECATION: t-name=\"kanban-box\" is banned in Odoo 19. Use t-name=\"card\".")
                if node.attrs.get("data-snippet", "").startswith("s_dynamic_snippet"):
                    if "data-filter-id" not in node.attrs or "data-template-key" not in node.attrs:
                        errors_found.append(f"Line {node.lineno}: CRITICAL OWL 2 CRASH: Dynamic snippets ({node.attrs.get('data-snippet')}) must explicitly declare 'data-filter-id' and 'data-template-key' to prevent InteractionService null pointer crashes on empty datasets.")
                if node.tag == "group" and (node.attrs.get("expand") == "0" or "string" in node.attrs):
                    errors_found.append(f"Line {node.lineno}: CRITICAL DEPRECATION: \x3cgroup expand=\"0\"\x3e and \x3cgroup string=\"...\"\x3e are banned in Odoo 19. Odoo 19 requires clean group tags.")
                if node.tag == "xpath" and "expr" in node.attrs:
                    expr = str(node.attrs.get("expr", ""))
                    if ".." in expr or re.search(r"//[a-zA-Z0-9_]+\[\s*[a-zA-Z0-9_]+\[@", expr):
                        errors_found.append(f"Line {node.lineno}: FRAGILE XPATH: Parent axis traversals (..) and complex container predicates are banned.")

                # WCAG Accessibility Enforcement
                if node.tag == "i":
                    cls = str(node.attrs.get("class", ""))
                    if "fa " in cls or "fa-" in cls or "oi " in cls or "oi-" in cls:
                        if not any(
                            k in node.attrs
                            for k in ("title", "aria-label", "aria-hidden")
                        ):
                            errors_found.append(
                                f"Line {node.lineno}: CRITICAL ACCESSIBILITY (WCAG): Icon \x3ci\x3e tags with '{cls}' must have 'title', 'aria-label', or 'aria-hidden=\"true\"'."
                            )

                if node.tag == "img":
                    if "alt" not in node.attrs:
                        errors_found.append(
                            f"Line {node.lineno}: CRITICAL ACCESSIBILITY (WCAG): \x3cimg\x3e tags must have an 'alt' attribute."
                        )

                if node.tag in ("button", "a"):
                    if not any(
                        k in node.attrs for k in ("title", "aria-label", "string")
                    ):
                        # Only warn if it's completely empty of text as well (ignoring deep QWeb evaluation complexity for the warning)
                        if not (node.text and node.text.strip()) and not node.children:
                            warnings_found.append(
                                f"Line {node.lineno}: [%AUDIT] ACCESSIBILITY (WCAG): Empty \x3c{node.tag}\x3e tag lacks 'string', 'title', or 'aria-label'."
                            )

                if node.tag == "field":
                    record_anc = next(
                        (anc for anc in node.get_ancestors() if anc.tag == "record"),
                        None,
                    )
                    model = record_anc.attrs.get("model") if record_anc else None
                    field_name = node.attrs.get("name")
                    ref_val = str(node.attrs.get("ref", ""))
                    eval_val = str(node.attrs.get("eval", ""))

                    if model == "res.groups" and field_name == "users":
                        errors_found.append(
                            f"Line {node.lineno}: CRITICAL BIAS TRAP: use user_ids."
                        )
                    if model == "res.groups" and field_name == "category_id":
                        errors_found.append(
                            f"Line {node.lineno}: CRITICAL SECURITY CATEGORY: category_id is banned for res.groups. Use privilege_id."
                        )
                    if model == "res.groups" and field_name == "privilege_id":
                        if "base.module_category_" in ref_val:
                            errors_found.append(
                                f"Line {node.lineno}: CRITICAL SECURITY PRIVILEGE: 'privilege_id' cannot reference standard Odoo categories like '{ref_val}'. It must point to a 'res.groups.privilege' record."
                            )
                    if model == "res.users" and field_name == "groups_id":
                        errors_found.append(
                            f"Line {node.lineno}: CRITICAL BIAS TRAP: groups_id is banned for res.users. Use group_ids."
                        )

                    # Inappropriate Data Assignment Traps
                    if field_name in ("user_id", "user_ids"):
                        if "base.group_" in ref_val or "base.group_" in eval_val:
                            errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: Assigning a group to a user field '{field_name}'.")
                        if "base.partner_" in ref_val or "base.partner_" in eval_val or "base.main_partner" in ref_val or "base.main_partner" in eval_val:
                            errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: Assigning a partner to a user field '{field_name}'.")

                    if field_name in ("group_id", "group_ids", "groups"):
                        if "base.user_" in ref_val or "base.user_" in eval_val:
                            errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: Assigning a user to a group field '{field_name}'.")
                        if "base.module_category_" in ref_val or "base.module_category_" in eval_val:
                            errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: Assigning a module category to a group field '{field_name}'.")

                    if field_name in ("company_id", "company_ids"):
                        if "base.user_" in ref_val or "base.user_" in eval_val or "base.group_" in ref_val or "base.group_" in eval_val:
                            errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: Assigning a user or group to a company field '{field_name}'.")

                    if field_name in ("partner_id", "partner_ids"):
                        if "base.user_" in ref_val or "base.user_" in eval_val or "base.group_" in ref_val or "base.group_" in eval_val or "base.module_category_" in ref_val or "base.module_category_" in eval_val:
                            errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: Assigning a user, group, or category to a partner field '{field_name}'.")

                    if field_name == "model_id":
                        if "base.group_" in ref_val or "base.user_" in ref_val or "base.module_category_" in ref_val or "base.partner_" in ref_val:
                            errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: Assigning a non-model reference to a 'model_id' field '{field_name}'.")

                    if field_name in ("active", "sequence", "is_published", "color", "priority"):
                        if ref_val:
                            errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: Using 'ref' on primitive/boolean/integer field '{field_name}'. Use 'eval' or node text instead.")

                        if model == "ir.cron" and field_name == "user_id":
                            if "base.user_root" in ref_val or "base.user_admin" in ref_val or "base.user_root" in eval_val or "base.user_admin" in eval_val:
                                errors_found.append(f"Line {node.lineno}: CRITICAL ZERO-SUDO VIOLATION: ir.cron cannot be assigned to base.user_root or base.user_admin. You MUST use a dedicated service account.")

                        if field_name.endswith("_ids") and eval_val:
                            eval_stripped = eval_val.replace(" ", "")
                            if eval_stripped.startswith("[") and not eval_stripped.startswith("[(6,") and not eval_stripped.startswith("[(4,") and not eval_stripped.startswith("[(5,"):
                                errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: Assigning a raw list to an x2many field '{field_name}'. You MUST use Odoo ORM commands (e.g., [(6, 0, [...])]).")

                        if ref_val and ref_val.isdigit():
                            errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: 'ref' attribute must be an XML ID string, not a hardcoded numeric ID '{ref_val}'.")

                        if eval_val and any(bad in eval_val for bad in ("__import__", "exec(", "eval(")):
                            errors_found.append(f"Line {node.lineno}: CRITICAL SECURITY: Dangerous built-in execution detected in 'eval' expression.")

                        if model == "ir.actions.act_window" and field_name == "type":
                            node_text = node.text.strip() if node.text else ""
                            if node_text and node_text != "ir.actions.act_window":
                                errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: 'type' for ir.actions.act_window must be 'ir.actions.act_window'.")

                        if field_name in ("employee_id", "employee_ids"):
                            if "base.user_" in ref_val or "base.user_" in eval_val:
                                errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: Assigning a user to an employee field '{field_name}'.")

                    if node.tag == "record":
                        model_name = node.attrs.get("model", "")
                        if "_" in model_name and "." not in model_name:
                            if model_name.startswith(("res_", "ir_", "account_", "mail_", "website_", "crm_", "sale_")):
                                errors_found.append(f"Line {node.lineno}: CRITICAL TYPE MISMATCH: Odoo models use dots, not underscores. Found '{model_name}'. Did you mean '{model_name.replace('_', '.', 1)}'?")

                for k, v in node.attrs.items():
                    v_str = str(v)
                    if "request.env" in v_str:
                        errors_found.append(
                            f"Line {node.lineno}: CRITICAL SSTI: Using 'request.env' inside QWeb templates exposes the database to Remote Code Execution."
                        )
                    if (
                        ".state" in v_str
                        and ("open" in v_str or "closed" in v_str)
                        and ("==" in v_str or "!=" in v_str)
                    ):
                        errors_found.append(
                            f"Line {node.lineno}: CRITICAL DEPRECATION: survey.survey 'state' field was removed in Odoo 19. Use 'active' (Boolean)."
                        )
                if node.text:
                    if "request.env" in node.text:
                        errors_found.append(
                            f"Line {node.lineno}: CRITICAL SSTI: Using 'request.env' inside QWeb templates exposes the database to Remote Code Execution."
                        )
                    if (
                        ".state" in node.text
                        and ("open" in node.text or "closed" in node.text)
                        and ("==" in node.text or "!=" in node.text)
                    ):
                        errors_found.append(
                            f"Line {node.lineno}: CRITICAL DEPRECATION: survey.survey 'state' field was removed in Odoo 19. Use 'active' (Boolean)."
                        )

                if node.tag == "record" and node.attrs.get("model") == "ir.cron":
                    raw_text = "\n".join(
                        lines[
                            max(0, node.lineno - 2) : min(
                                len(lines), node.end_lineno + 1
                            )
                        ]
                    )
                    if "audit-ignore-cron" not in raw_text:
                        warnings_found.append(
                            f"Line {node.lineno}: [%AUDIT] CRON ARCHITECTURE: Ensure the Python method implements stateless batching via _trigger()."
                        )
                if node.tag == "xpath":
                    raw_text = "\n".join(
                        lines[
                            max(0, node.lineno - 2) : min(
                                len(lines), node.end_lineno + 1
                            )
                        ]
                    )
                    if "audit-ignore-xpath" not in raw_text:
                        warnings_found.append(
                            f"Line {node.lineno}: [%AUDIT] XPATH RENDERING: All \x3cxpath\x3e injections must be proven to render correctly."
                        )
        except Exception as e:
            errors_found.append(f"CRITICAL XML AST ERROR: {e}")

    if filename.endswith(".js") and ("tour" in filename or "tours" in filepath):
        js_tour_rules = [
            (r"trigger:\s*['\"`]button\[name=[^\]]+\]['\"`][^}]+run:\s*['\"`]click['\"`]\s*\}[\s\]\),;]*$", f"CRITICAL JS TOUR DIRTY FORM: The tour '{filename}' appears to end immediately after clicking an action button. It MUST explicitly wait for an RPC resolution (e.g., a verifiable DOM field state change) in a subsequent step to prevent a dirty form crash."),
            (r"window\.(confirm|alert)\s*\(", f"CRITICAL JS TOUR DIALOG: Tour '{filename}' contains an execution of window.confirm or window.alert. This will freeze the headless browser. You MUST override it (e.g., window.confirm = () => true;) in a separate step targeting 'body' before clicking the trigger."),
            (r"trigger:\s*['\"`]\.(modal-dialog|modal-content)['\"`][^}]+run:\s*['\"`][a-zA-Z]+['\"`]", f"CRITICAL JS TOUR MODAL: Tour '{filename}' attempts to perform an action (run: '...') directly on '.modal-dialog' or '.modal-content'. These must be used strictly as empty DOM polling steps (run: function() {{}}) to wait for the modal to render. Use '.modal-body' for neutral clicks."),
            (r"expectUnloadPage:\s*true[^}]+run:\s*(?:function|\(\)\s*=>)|run:\s*(?:function|\(\)\s*=>)[^}]+expectUnloadPage:\s*true", f"CRITICAL JS TOUR UNLOAD: Tour '{filename}' uses 'expectUnloadPage: true' alongside a custom JS closure for 'run'. This breaks Odoo's native unload event binding. You MUST use the native Odoo helper \"run: 'click'\" when expecting a page unload."),
            (r"run:\s*['\"`]edit[^}]*\}\s*\]\s*\.\s*concat\(\s*TourUtils\.safeSave", f"CRITICAL JS TOUR DOM BLUR: Tour '{filename}' calls TourUtils.safeSave() immediately after an 'edit' step. You MUST inject a neutral 'click away' step (e.g., trigger: '.o_form_sheet' or '.modal-body', run: 'click') before saving to ensure DOM blur events fire and prevent dirty form race conditions."),
            (r"run:\s*['\"`]edit[^}]+}\s*,\s*\{\s*(?:content:\s*['\"`][^'\"`]+['\"`]\s*,\s*)?trigger:\s*['\"`]button\[name=", f"CRITICAL JS TOUR DOM BLUR: Tour '{filename}' attempts to click a backend action button immediately after an 'edit' step. You MUST inject a neutral 'click away' step (e.g., trigger: '.o_form_sheet' or '.modal-body', run: 'click') to force the DOM blur event before the RPC fires."),
            (r"trigger:\s*['\"`]button\[name=[^\]]+\]['\"`][^}]+run:\s*['\"`]click['\"`]\s*\}[^\]]+trigger:\s*['\"`]\.o_notification['\"`]", f"CRITICAL JS TOUR RPC RESOLUTION: Tour '{filename}' clicks an action button and immediately waits for '.o_notification'. Backend methods returning 'True' (form reloads) DO NOT spawn notifications. Wait for a verifiable DOM state change (e.g., '.o_field_widget[name=\"...\"]:not(.o_field_empty)') instead."),
            (r"\{(?![^{}]*expectUnloadPage:\s*true)(?=[^{}]*run:\s*['\"`]click['\"`])[^{}]*trigger:\s*['\"`][^'\"`]*type=[\"']submit[\"'][^'\"`]*['\"`][^{}]*\}", f"CRITICAL JS TOUR UNLOAD EXPECTATION: Tour '{filename}' clicks a 'type=\"submit\"' button but lacks 'expectUnloadPage: true'. Form submissions trigger hard browser reloads. You MUST declare this flag to prevent the tour runner from crashing on the beforeUnload event.")
        ]
        for pattern, msg in js_tour_rules:
            for match in re.finditer(pattern, content, re.DOTALL):
                lineno = content[:match.start()].count('\n') + 1
                errors_found.append(f"Line {lineno}: {msg}")

    if filename.startswith("test_") and filename.endswith(".py"):
        FOUND_TEST_CONTENTS[filepath] = content

    if filename.endswith(".js"):
        has_pragma = False
        code_started = False
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped:
                continue
            if "/** @odoo-module **/" in stripped or "/** @odoo-module" in stripped:
                has_pragma = True
                break
            if not stripped.startswith("//") and not stripped.startswith("/*") and not stripped.startswith("*"):
                code_started = True
                break
        if code_started and not has_pragma:
            errors_found.append("Line 1: CRITICAL ASSET BUNDLER: JavaScript file is missing the `/** @odoo-module **/` pragma at the absolute top of the file. Native imports will fail.")

    if filename.endswith(".py"):
        ast_errs, ast_warns = check_ast_vulnerabilities(filepath, content, lines, is_odoo_module)
        for lineno, msg in ast_errs:
            code_snippet = lines[lineno - 1].strip() if lineno <= len(lines) else ""
            errors_found.append(
                f"Line {lineno} (AST): {msg}\n      Code: `{code_snippet}`"
            )
        for lineno, msg in ast_warns:
            code_snippet = lines[lineno - 1].strip() if lineno <= len(lines) else ""
            warnings_found.append(
                f"Line {lineno} (AST): {msg}\n      Code: `{code_snippet}`"
            )

    # Protect against AI meta-editing failures
    if (
        filename == "LLM_LINTER_GUIDE.md"
        and "CRITICAL BIAS TRAP: Odoo 18+ normalized the res.users groups relation to 'group_ids'."
        not in content
    ):
        errors_found.append(
            "AI SUMMARIZATION BIAS TRAP: LLM_LINTER_GUIDE.md was truncated or summarized. All rules must be preserved."
        )

    in_py_multiline = False
    py_multiline_marker = None
    for line_num, line in enumerate(lines, 1):
        stripped = line.strip()
        if filename.endswith(".py"):
            if not in_py_multiline:
                if '"""' in line or "'''" in line:
                    marker = '"""' if '"""' in line else "'''"
                    if line.count(marker) % 2 != 0:
                        in_py_multiline, py_multiline_marker = True, marker
                    continue
            else:
                if py_multiline_marker in line:
                    in_py_multiline, py_multiline_marker = False, None
                continue
            if stripped.startswith("#"):
                continue

        if filename.endswith(".js") and stripped.startswith("//"):
            continue
        if filename.endswith(".xml") and stripped.startswith("\x3c!--"):
            continue

        if "noqa" in line.lower():
            if "noqa: e402" in line.lower():
                pass  # Exception allowed for sys.path injections in isolated daemon tests
            else:
                errors_found.append(
                    f"Line {line_num}: CRITICAL LINTER EVASION: Use of 'noqa' is strictly forbidden.\n      Code: `{stripped}`"
                )

        if "burn-ignore" in line and not any(
            allowed in line
            for allowed in [
                "burn-ignore-financial",
                "burn-ignore-tour",
                "burn-ignore-sudo",
                "burn-ignore-route",
                "burn-ignore-env",
                "burn-ignore-test-tags"
            ]
        ):
            errors_found.append(
                f"Line {line_num}: UNAUTHORIZED BYPASS.\n      Code: `{stripped}`"
            )

        if "audit-ignore" in line:
            valid_audits = [
                "audit-ignore-cron",
                "audit-ignore-mail",
                "audit-ignore-search",
                "audit-ignore-xpath",
                "audit-ignore-sleep",
                "audit-ignore-view",
                "audit-ignore-i18n",
                "audit-ignore-catch-all",
                "audit-ignore-path",
            ]
            if not any(tag in line for tag in valid_audits):
                errors_found.append(
                    f"Line {line_num}: UNAUTHORIZED BYPASS.\n      Code: `{stripped}`"
                )
            else:
                anchor_match = re.search(r"\[@ANCHOR:\s*([a-zA-Z0-9_]+)\s*\]", line)
                if anchor_match:
                    REQUIRE_TEST_VERIFICATION.append(
                        {
                            "anchor": anchor_match.group(1),
                            "type": next(
                                (tag for tag in valid_audits if tag in line), None
                            ),
                            "file": filepath,
                            "line": line_num,
                        }
                    )

        for burn_type in ["burn-ignore-financial"]:
            if burn_type in line:
                anchor_match = re.search(r"\[@ANCHOR:\s*([a-zA-Z0-9_]+)\s*\]", line)
                if anchor_match:
                    REQUIRE_TEST_VERIFICATION.append(
                        {
                            "anchor": anchor_match.group(1),
                            "type": burn_type,
                            "file": filepath,
                            "line": line_num,
                        }
                    )

        for ext_pattern, regex, msg in GENERAL_ERROR_RULES:
            if re.search(ext_pattern, filepath.replace("\\", "/")) and regex.search(
                line
            ):
                if "burn-ignore" not in line:
                    errors_found.append(
                        f"Line {line_num}: {msg}\n      Code: `{stripped}`"
                    )

        if is_odoo_module:
            for ext_pattern, regex, msg in ODOO_ERROR_RULES:
                if re.search(ext_pattern, filepath.replace("\\", "/")) and regex.search(
                    line
                ):
                    if "burn-ignore" not in line:
                        errors_found.append(
                            f"Line {line_num}: {msg}\n      Code: `{stripped}`"
                        )

        for ext_pattern, regex, msg in WARNING_RULES:
            if re.search(ext_pattern, filepath.replace("\\", "/")) and regex.search(
                line
            ):
                if "audit-ignore" not in line:
                    warnings_found.append(
                        f"Line {line_num}: {msg}\n      Code: `{stripped}`"
                    )

    return errors_found, warnings_found


# -------------------------------------------------------------------------
# CI/CD BYPASS TEST VERIFICATION
# -------------------------------------------------------------------------

def _verify_test_ast(
    req, target_content, target_file, verification_errors, total_errors
):
    anchor, b_type = req["anchor"], req["type"]
    if target_file.endswith(".js"):
        if b_type == "audit-ignore-xpath" and not any(
            k in target_content
            for k in ("get_view", "url_open", "_get_combined_arch", "trigger:")
        ):
            print(
                "  ❌ ERROR: Invalid JS Test Implementation. Must contain 'trigger:' to verify UI rendering in JS."
            )
            return verification_errors + 1, total_errors + 1
        return verification_errors, total_errors

    try:
        tree = ast.parse(target_content, filename=target_file)
    except SyntaxError as e:
        print(f"  ❌ ERROR: Syntax error in test file {target_file}: {e}")
        return verification_errors + 1, total_errors + 1

    anchor_line = -1
    for i, line_text in enumerate(target_content.splitlines(), 1):
        if f"[@ANCHOR: {anchor}]" in line_text:
            anchor_line = i
            break

    target_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            start_line = getattr(node, "lineno", 0)
            end_line = getattr(node, "end_lineno", float("inf"))
            if start_line <= anchor_line <= end_line:
                target_func = node
                break

    if not target_func:
        print(
            f"  ❌ ERROR: Test Anchor '{anchor}' is not inside an AST FunctionDef in {target_file}."
        )
        return verification_errors + 1, total_errors + 1

    found_qcount = False
    found_view = False
    found_trigger = False
    found_mail = False
    found_security_check = False

    for node in ast.walk(target_func):
        if isinstance(node, ast.Call):
            func_attr = getattr(node.func, "attr", "")
            if func_attr in ("assertQueryCount", "assertLess", "assertLessEqual"):
                found_qcount = True
            if func_attr in ("get_view", "url_open", "_get_combined_arch"):
                found_view = True
            if func_attr == "_trigger":
                found_trigger = True
            if func_attr in ("send_mail", "message_post"):
                found_mail = True
            if func_attr in (
                "assertRaises",
                "assertRaisesRegex",
                "assertFalse",
                "assertTrue",
            ):
                found_security_check = True
            if func_attr == "object":
                for arg in getattr(node, "args", []):
                    if isinstance(arg, ast.Constant) and arg.value in (
                        "send_mail",
                        "message_post",
                        "execute",
                    ):
                        if arg.value == "execute":
                            found_qcount = True
                        else:
                            found_mail = True

        if isinstance(node, ast.With):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call):
                    func_attr = getattr(item.context_expr.func, "attr", "")
                    if func_attr == "assertQueryCount":
                        found_qcount = True
                    elif func_attr in ("assertRaises", "assertRaisesRegex"):
                        found_security_check = True

    for node in ast.walk(target_func):
        if isinstance(node, (ast.For, ast.While)):
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and getattr(child.func, "attr", "") in (
                    "get_view",
                    "url_open",
                ):
                    print(
                        f"  ❌ ERROR: AST Evasion Detected. Found loop wrapping view/URL validation in test anchor '{anchor}' in {target_file}."
                    )
                    return verification_errors + 1, total_errors + 1

    is_valid, msg = True, ""
    if b_type in ("audit-ignore-xpath", "audit-ignore-view"):
        is_valid, msg = (
            (True, "")
            if found_view
            else (
                False,
                "AST requires 'get_view', 'url_open', or '_get_combined_arch' call.",
            )
        )
    elif b_type == "audit-ignore-search":
        is_valid, msg = (
            (True, "") if found_qcount else (False, "AST requires 'assertQueryCount'.")
        )
    elif b_type == "audit-ignore-cron":
        is_valid, msg = (
            (True, "") if found_trigger else (False, "AST requires '_trigger()' call.")
        )
    elif b_type == "audit-ignore-mail":
        is_valid, msg = (
            (True, "")
            if found_mail
            else (False, "AST requires 'send_mail' or 'message_post'.")
        )
    elif b_type == "burn-ignore-financial":
        is_valid, msg = (
            (True, "")
            if found_security_check
            else (
                False,
                "AST requires security assertion (e.g., assertRaises, assertTrue) to verify financial data protection limits.",
            )
        )
    elif b_type == "audit-ignore-i18n":
        is_valid, msg = True, ""

    if not is_valid:
        print(
            f"  ❌ ERROR: Invalid Test Implementation (AST) in {target_file}. {b_type} cites anchor '{anchor}'. {msg}"
        )
        return verification_errors + 1, total_errors + 1
    return verification_errors, total_errors

def _is_odoo_module(filepath, target_dir):
    filepath_forward = filepath.replace("\\", "/")
    if "/daemons/" in filepath_forward or "/daemon/" in filepath_forward or "/tools/" in filepath_forward:
        return False

    current = os.path.dirname(os.path.abspath(filepath))
    target_abs = os.path.abspath(target_dir)
    while current:
        if os.path.exists(os.path.join(current, '__manifest__.py')):
            return True
        if current == target_abs or current == os.path.dirname(current):
            break
        current = os.path.dirname(current)
    return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", default=".")
    parser.add_argument(
        "--ignore-file", default="ignore_list.txt", help="Path to ignore config file"
    )
    args = parser.parse_args()
    target_dir = os.path.abspath(args.directory)
    total_errors, total_warnings, scanned_files = 0, 0, 0

    ignore_patterns = []
    ignore_path = (
        args.ignore_file
        if os.path.isabs(args.ignore_file)
        else os.path.join(target_dir, args.ignore_file)
    )
    if os.path.exists(ignore_path):
        with open(ignore_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    ignore_patterns.append(re.compile(stripped))

    def is_ignored(path_str):
        for pat in ignore_patterns:
            if pat.search(path_str):
                return True
        return False

    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and d
            not in (
                "__pycache__",
                "node_modules",
                "tools",
                "daemons",
                "hams_local_relay",
                "hams_community",
                "hams_com",
            )
            and not is_ignored(os.path.relpath(os.path.join(root, d), target_dir))
        ]
        for file in files:
            if file in ("check_burn_list.py", "LLM_LINTER_GUIDE.md"):
                continue
            filepath = os.path.join(root, file)
            if is_ignored(os.path.relpath(filepath, target_dir)):
                continue

            if file == "__manifest__.py":
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        manifest_content = f.read()
                    tree = ast.parse(manifest_content, filename=filepath)
                    for node in tree.body:
                        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Dict):
                            manifest_dict = ast.literal_eval(node.value)
                            FOUND_MANIFESTS[os.path.abspath(root)] = manifest_dict
                except Exception:
                    pass

            if file.endswith((".py", ".xml", ".js", ".csv", ".html")):
                scanned_files += 1
                is_odoo = _is_odoo_module(filepath, target_dir)
                errors, warnings = scan_file(filepath, is_odoo_module=is_odoo)
                if file.endswith(".py"):
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            first_line = f.readline()
                            filepath_forward = filepath.replace("\\", "/")
                            if first_line.startswith("#!") and not "daemons/" in filepath_forward and not "daemon/" in filepath_forward and not "tools/" in filepath_forward and not filepath.endswith("setup.py") and not filepath.endswith("__init__.py"):
                                errors.append("Line 1 (Shebang): Shebangs are strictly prohibited in standard Odoo module files as they can interfere with packaging and execution expectations.")
                            if file == "__manifest__.py":
                                f.seek(0)
                                if first_line.startswith("#!"):
                                    errors.append("Line 1 (__manifest__.py format): __manifest__.py must not contain a shebang. It should ideally start with the dictionary '{' or standard -*- coding -*- comment.")
                    except Exception:
                        pass
                if errors or warnings:
                    print(f" 📄 {os.path.relpath(filepath, target_dir)}")
                    for w in warnings:
                        print(f"  ⚠️  WARNING: {w}")
                    for e in errors:
                        print(f"  ❌ ERROR: {e}")
                    total_warnings += len(warnings)
                    total_errors += len(errors)

    verification_errors = 0
    for req in REQUIRE_TEST_VERIFICATION:
        anchor = req["anchor"]

        def get_mod_dir(p):
            d = os.path.dirname(os.path.abspath(p))
            while d and d != os.path.dirname(d):
                if os.path.exists(os.path.join(d, "__manifest__.py")): return d
                d = os.path.dirname(d)
            return None

        req_mod = get_mod_dir(req["file"])
        best_match = None
        for f, c in FOUND_TEST_CONTENTS.items():
            if f"[@ANCHOR: {anchor}]" in c:
                if req_mod and get_mod_dir(f) == req_mod:
                    best_match = (c, f)
                    break
                elif not best_match:
                    best_match = (c, f)

        target_content, target_file = best_match if best_match else (None, None)

        if not target_content:
            print(
                f"  ❌ ERROR: Orphaned Bypass. {req['type']} in {req['file']}:{req['line']} cites anchor '{anchor}' not found in any test file."
            )
            verification_errors += 1
            total_errors += 1
            continue
        verification_errors, total_errors = _verify_test_ast(
            req, target_content, target_file, verification_errors, total_errors
        )

    for tour_path in FOUND_TOURS:
        abs_tour = os.path.abspath(tour_path)
        mod_dir = os.path.dirname(abs_tour)
        found_mod = None
        while mod_dir and mod_dir != os.path.dirname(mod_dir):
            if mod_dir in FOUND_MANIFESTS:
                found_mod = mod_dir
                break
            mod_dir = os.path.dirname(mod_dir)

        if not found_mod:
            continue

        manifest = FOUND_MANIFESTS[found_mod]
        assets = manifest.get("assets", {})

        matched = False
        parent_dir = os.path.dirname(found_mod)
        for bundle_name, patterns in assets.items():
            for pattern in patterns:
                abs_glob_pattern = os.path.join(parent_dir, pattern)
                matched_files = [os.path.abspath(p) for p in glob.glob(abs_glob_pattern, recursive=True)]
                if abs_tour in matched_files:
                    matched = True
                    break
            if matched:
                break

        if not matched:
            print(f"  ❌ ERROR: Tour Asset Registration Trap. Tour file '{os.path.relpath(tour_path, target_dir)}' is not matched by any glob pattern in 'assets' of its __manifest__.py.")
            total_errors += 1

    # Audit Orphaned o_tour_ classes and Dangling Tour Targets (Bidirectional Audit)
    xml_tour_classes = set()
    js_tour_targets = set()
    xml_content_all = ""
    js_content_all = ""
    for root, dirs, files in os.walk(target_dir):
        if "node_modules" in root: continue
        for file in files:
            filepath = os.path.join(root, file)
            if file.endswith('.xml'):
                try:
                    content = open(filepath, 'r', encoding='utf-8').read()
                    xml_tour_classes.update(re.findall(r'o_tour_[a-zA-Z0-9_-]+', content))
                    xml_content_all += content
                except Exception: pass
            elif file.endswith('.js'):
                try:
                    content = open(filepath, 'r', encoding='utf-8').read()
                    js_tour_targets.update(re.findall(r'o_tour_[a-zA-Z0-9_-]+', content))
                    js_content_all += content
                except Exception: pass

    for cls in xml_tour_classes:
        if cls not in js_content_all:
            print(f"  ❌ ERROR: Orphaned Tour Class: '{cls}' found in XML but never targeted in any JS tour. Remove dead code.")
            total_errors += 1

    for target in js_tour_targets:
        if target not in xml_content_all:
            print(f"  ❌ ERROR: Dangling Tour Target: '{target}' found in a JS tour but missing from all backend XML views. Tour will fatally timeout.")
            total_errors += 1

    if total_errors > 0 or total_warnings > 0:
        print(f"\nScan Complete: Checked {scanned_files} files.")
        print(
            f"Total Errors: {total_errors} | Total Warnings (Audits): {total_warnings}"
        )

    if total_errors > 0:
        sys.exit(1)
    if total_warnings > 0:
        print("✅ Passed with warnings. Audits require manual verification.")
    sys.exit(0)


if __name__ == "__main__":
    main()
