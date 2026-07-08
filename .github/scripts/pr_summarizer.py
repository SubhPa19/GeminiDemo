import os
import sys
import subprocess

# --- Dynamic Dependency Bootstrapping ---
try:
    import requests
    import tree_sitter
    import tree_sitter_languages
    if getattr(tree_sitter, "__version__", "") != "0.21.3":
        raise ImportError("Mismatch")
except ImportError:
    import sys
    import subprocess
    print("📦 Bootstrapping specific versions of tree-sitter to avoid breaking API changes...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "tree-sitter==0.21.3", "tree-sitter-languages==1.10.2", "--break-system-packages"])
    except subprocess.CalledProcessError:
        print("Fallback: Retrying install without --break-system-packages for older pip versions...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "tree-sitter==0.21.3", "tree-sitter-languages==1.10.2"])

import json
import requests
import time
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional, Dict, Set, List

# ==============================================================================
# SCRIPT METADATA & CONSTANTS
# ==============================================================================
SCRIPT_VERSION = "2.4.21"
BOT_MARKER = f"<!-- gemini-bot-review-v{SCRIPT_VERSION} -->"

# ==============================================================================
# 1. INTERFACES & ABSTRACT-LIKE BASE CLASSES (Open/Closed Principle)
# ==============================================================================
class LLMClient:
    """
    Abstract interface defining the requirements for an LLM Client.
    Allows easy swapping/extension to other LLM providers (e.g., OpenAI, Vertex AI)
    without modifying the orchestration logic.
    """
    def get_completion(self, prompt: str, is_json: bool = False) -> Any:
        raise NotImplementedError("LLMClient subclasses must implement get_completion.")

# ==============================================================================
# 2. CONCRETE IMPLEMENTATIONS (Single Responsibility Principle - LLM Interface)
# ==============================================================================
class GeminiClient(LLMClient):
    """
    Handles all communication and JSON sanitization for the Gemini Developer API.
    """
    def __init__(self, model_name: str, api_key: str):
        self.model_name = model_name
        self.api_key = api_key
        self.gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent?key={self.api_key}"
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def _execute_tool(self, name: str, args: dict) -> dict:
        try:
            if name == "grep_search":
                query = args.get("query", "")
                print(f"🔧 Tool Call: grep_search('{query}')")
                result = subprocess.run(["git", "grep", "-n", query], capture_output=True, text=True, cwd=".")
                output = result.stdout
                if len(output) > 5000:
                    output = output[:5000] + "\n...[truncated]"
                return {"result": output if output else "No matches found."}
            elif name == "view_file":
                filepath = args.get("filepath", "")
                print(f"🔧 Tool Call: view_file('{filepath}')")
                if not os.path.exists(filepath):
                    return {"error": f"File {filepath} not found."}
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                if len(content) > 10000:
                    content = content[:10000] + "\n...[truncated]"
                return {"result": content}
            else:
                return {"error": f"Unknown tool: {name}"}
        except Exception as e:
            return {"error": str(e)}

    def get_completion(self, prompt: str, is_json: bool = False, enable_tools: bool = False) -> Any:
        """
        Sends content generation request to Gemini API and handles tool calling loops.
        """
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        payload = {"contents": contents}
        
        if is_json:
            payload["generationConfig"] = {"response_mime_type": "application/json"}
            
        if enable_tools:
            payload["tools"] = [{
                "functionDeclarations": [
                    {
                        "name": "grep_search",
                        "description": "Searches the codebase using git grep to find references, function calls, or variable definitions across the entire repository. This allows you to verify if a function is called elsewhere.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "query": {"type": "STRING", "description": "The regex or literal string query to search for"}
                            },
                            "required": ["query"]
                        }
                    },
                    {
                        "name": "view_file",
                        "description": "Reads the complete contents of a specific file in the repository.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "filepath": {"type": "STRING", "description": "The exact relative path to the file"}
                            },
                            "required": ["filepath"]
                        }
                    }
                ]
            }]
        
        headers = {"Content-Type": "application/json"}
        
        max_turns = 15 if enable_tools else 1
        turn = 0
        
        while turn < max_turns:
            # Force the model to output text/JSON on the final turn by disabling tools
            if turn == max_turns - 1 and "tools" in payload:
                del payload["tools"]
                
            turn += 1
            for attempt in range(3):
                try:
                    res = requests.post(self.gemini_url, json=payload, headers=headers, timeout=180)
                    res.raise_for_status()
                    response_json = res.json()
                    
                    usage = response_json.get('usageMetadata', {})
                    self.total_input_tokens += usage.get('promptTokenCount', 0)
                    self.total_output_tokens += usage.get('candidatesTokenCount', 0)
                    
                    candidate = response_json['candidates'][0]
                    message = candidate['content']
                    parts = message.get('parts', [])
                    
                    has_function_call = False
                    function_responses = []
                    
                    for part in parts:
                        if "functionCall" in part:
                            has_function_call = True
                            call = part["functionCall"]
                            name = call["name"]
                            args = call.get("args", {})
                            
                            result = self._execute_tool(name, args)
                            
                            function_responses.append({
                                "functionResponse": {
                                    "name": name,
                                    "response": result
                                }
                            })
                    
                    if has_function_call:
                        contents.append({"role": "model", "parts": parts})
                        contents.append({"role": "function", "parts": function_responses})
                        payload["contents"] = contents
                        break # Break retry loop, continue tool loop
                    else:
                        text = ""
                        for part in parts:
                            if "text" in part:
                                text += part["text"]
                        if is_json:
                            return self._parse_gemini_json(text)
                        return text
                except Exception as e:
                    print(f"⚠️ Gemini request attempt {attempt+1} failed: {e}")
                    if 'res' in locals() and res is not None:
                        print(f"   API Response: {res.text}")
                    time.sleep(2 ** attempt + 1)
                    if attempt == 2:
                        return None
        return None

    def calculate_cost(self) -> float:
        """
        Calculates estimated cost based on standard Google AI Studio pricing.
        """
        model_lower = self.model_name.lower()
        if "pro" in model_lower:
            input_rate = 1.25 / 1000000  # $1.25 per 1M tokens
            output_rate = 5.00 / 1000000 # $5.00 per 1M tokens
        else: # Default to Flash
            input_rate = 0.075 / 1000000 # $0.075 per 1M tokens
            output_rate = 0.30 / 1000000 # $0.30 per 1M tokens
            
        return (self.total_input_tokens * input_rate) + (self.total_output_tokens * output_rate)

    def _parse_gemini_json(self, text: str) -> Any:
        """
        Robustly extracts and parses JSON from Gemini's response.
        Handles nested markdown formatting and backticks.
        """
        if not text:
            return None
        
        clean_text = text.strip()
        if clean_text.startswith("```"):
            # Remove markdown code blocks if present (json block or generic)
            clean_text = re.sub(r'^```(?:json)?\s*', '', clean_text)
            clean_text = re.sub(r'\s*```$', '', clean_text)
        
        try:
            return json.loads(clean_text, strict=False)
        except json.JSONDecodeError as e:
            print(f"⚠️ Initial JSON parse failed: {e}. Attempting extraction...")
            # Fallback: Extract the first { ... } or [ ... ] block
            match = re.search(r'(\{.*\}|\[.*\])', clean_text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1), strict=False)
                except json.JSONDecodeError as e2:
                    print(f"❌ Extraction fallback also failed: {e2}")
            
            # Final attempt: handle potential unescaped control characters
            try:
                sanitized = re.sub(r'(?<!\\)\n', '\\n', clean_text)
                return json.loads(sanitized, strict=False)
            except Exception as e3:
                print(f"❌ Final fallback sanitization also failed: {e3}")
                return None

# ==============================================================================
# 3. GITHUB REST CLIENT (Single Responsibility Principle - GitHub API Integration)
# ==============================================================================
class GitHubClient:
    """
    Handles all interactions with the GitHub REST API (fetching metadata/diffs,
    retrying failed requests, and submitting reviews/comments).
    """
    def __init__(self, repo: str, pr_number: str, token: str):
        self.repo = repo
        self.pr_number = pr_number
        self.token = token
        self.last_api_error: Optional[str] = None
        
        self.api_headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json"
        }
        github_api_base = os.getenv("GITHUB_API_URL") or "https://api.github.com"
        self.base_url = f"{github_api_base.rstrip('/')}/repos/{self.repo}"

    def _safe_request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """
        Generic request wrapper with optimized 429 (Rate Limit) and 422 handling.
        """
        kwargs.setdefault("timeout", 30)
        for attempt in range(5):
            try:
                res = requests.request(method, url, **kwargs)
                if res.status_code == 429:
                    print(f"🛑 Rate limit hit (429). Attempt {attempt+1}. Waiting 10 seconds...")
                    time.sleep(10)
                    continue
                
                # Special handling for 422 to see validation errors (useful for PR reviews)
                if res.status_code == 422:
                    self.last_api_error = res.text
                    print(f"❌ Validation Error (422) for {url}: {res.text}")
                    return res # Return immediately, retrying a 422 will never succeed
                
                res.raise_for_status()
                return res
            except Exception as e:
                if not self.last_api_error:
                    self.last_api_error = str(e)
                print(f"⚠️ Attempt {attempt+1} failed for {url}: {e}")
                time.sleep(2 ** attempt + 1)
        return None

    def fetch_pr_data(self) -> Optional[Dict[str, Any]]:
        """
        Fetches basic pull request metadata.
        """
        print("🔍 Fetching PR metadata...")
        url = f"{self.base_url}/pulls/{self.pr_number}"
        res = self._safe_request("GET", url, headers=self.api_headers)
        if not res: 
            return None
        
        data = res.json()
        return {
            "author": data.get("user", {}).get("login", "Developer"),
            "base_branch": data.get("base", {}).get("ref", "main"),
            "title": data.get("title", "this PR"),
            "description": data.get("body", ""),
            "diff_url": url,
            "head_sha": data.get("head", {}).get("sha")
        }

    def fetch_diff(self, url: str) -> str:
        """
        Fetches the PR's unified diff text. Truncates if it exceeds 60,000 characters.
        """
        print("💾 Fetching PR diff...")
        headers = self.api_headers.copy()
        headers["Accept"] = "application/vnd.github.v3.diff"
        res = self._safe_request("GET", url, headers=headers)
        if not res: 
            return ""
        
        diff = res.text
        if len(diff) > 60000:
            print("⚠️ Diff too large, truncating to 60,000 characters...")
            diff = diff[:60000] + "\n\n[Diff truncated for size]"
        return diff

    def fetch_checklist(self, branch: str, checklist_path: str) -> Optional[str]:
        """
        Attempts to read the repository checklist file from the target base branch.
        """
        url = f"{self.base_url}/contents/{checklist_path}?ref={branch}"
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github.v3.raw"}
        res = self._safe_request("GET", url, headers=headers)
        if res and res.status_code == 200:
            return res.text
        return None

    def submit_bundled_review(self, body: str, event: str, comments: list) -> Optional[requests.Response]:
        """
        Submits all findings in a single Bundled Review to minimize developer notification noise.
        """
        print(f"📦 Submitting bundled review ({len(comments)} inline findings) as {event}...")
        url = f"{self.base_url}/pulls/{self.pr_number}/reviews"
        payload = {
            "body": body + f"\n\n<br>\n\n<sup>[**Grace-{SCRIPT_VERSION}**](https://docs.google.com/presentation/d/1mN6SBazQFwcmtuBgiGhm184HVhY_CvxWq9t4nZt6mzQ/edit?usp=sharing \"Gemini-powered Review And Code Evaluator\") | [Share Feedback](https://forms.gle/bpRX129ku5YMi9JLA) | [Walkthrough & Guide](https://drive.google.com/file/d/1paA9hswGG1MazQF_0WBvrZD9sKBUscOy/view?usp=drive_link)</sup>\n{BOT_MARKER}",
            "event": event,
            "comments": comments
        }
        return self._safe_request("POST", url, headers=self.api_headers, json=payload)

    def post_failure_comment(self, error_msg: str) -> Optional[requests.Response]:
        """
        Posts a fallback issue comment if the main review fails to submit.
        """
        url = f"{self.base_url}/issues/{self.pr_number}/comments"
        body = f"❌ **Review Failure Report**\n\nThe PR summarizer script failed to submit a formal review.\n\n**Error Details**:\n```\n{error_msg}\n```\n\n<br>\n\n<sup>[**Grace-{SCRIPT_VERSION}**](https://docs.google.com/presentation/d/1mN6SBazQFwcmtuBgiGhm184HVhY_CvxWq9t4nZt6mzQ/edit?usp=sharing \"Gemini-powered Review And Code Evaluator\") | [Share Feedback](https://forms.gle/bpRX129ku5YMi9JLA) | [Walkthrough & Guide](https://drive.google.com/file/d/1paA9hswGG1MazQF_0WBvrZD9sKBUscOy/view?usp=drive_link)</sup>\n{BOT_MARKER}"
        print("🚀 Posting failure comment to PR...")
        return self._safe_request("POST", url, headers=self.api_headers, json={"body": body})

# ==============================================================================
# 4. DIFF LINE PARSER UTILITY (Single Responsibility Principle - Diff Analysis)
# ==============================================================================
class DiffParser:
    """
    Stateless utility dedicated entirely to parsing git unified diffs
    and identifying correct line alignment.
    """
    @staticmethod
    def parse_valid_lines(diff_text: str) -> Dict[str, Set[int]]:
        """
        Parses a unified diff to extract valid 'RIGHT' side (new/added) line numbers 
        for inline review comments. Prevents GitHub API 422 error submissions.
        """
        valid_lines: Dict[str, Set[int]] = {}
        current_file: Optional[str] = None
        current_line_new: Optional[int] = None

        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                current_line_new = None
            elif line.startswith("+++ "):
                filepath = line[4:].strip()
                if filepath.startswith("b/"):
                    filepath = filepath[2:]
                current_file = filepath.lstrip('./').lstrip('/')
                if current_file not in valid_lines:
                    valid_lines[current_file] = set()
                current_line_new = None
            elif line.startswith("@@ ") and current_file is not None:
                try:
                    plus_part = line.split("+")[1].split(" ")[0]
                    start_line_str = plus_part.split(",")[0]
                    current_line_new = int(start_line_str)
                except Exception:
                    current_line_new = None
            elif current_line_new is not None:
                if line.startswith("\\"): 
                    continue
                if not line or line[0] == ' ':
                    current_line_new += 1
                elif line[0] == '+':
                    valid_lines[current_file].add(current_line_new)
                    current_line_new += 1
                elif line.startswith("-"):
                    pass

        return valid_lines

# ==============================================================================
# 4.1 UNIVERSAL CONTEXT GRABBER (Single Responsibility Principle - Workspace Scanning)
# ==============================================================================
class UniversalContextGrabber:
    """
    AST-based Codebase Context & Type Resolution Grabber using tree-sitter-languages.
    """
    EXT_TO_LANG = {
        ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
        ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
        ".cs": "c_sharp", ".py": "python", ".sh": "bash", ".bash": "bash",
        ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
        ".swift": "swift", ".go": "go", ".rb": "ruby"
    }

    @classmethod
    def extract_ast_context(cls, filepath: str) -> str:
        if not os.path.exists(filepath): return ""
        _, ext = os.path.splitext(filepath)
        lang = cls.EXT_TO_LANG.get(ext)
        if not lang: return ""
        
        try:
            from tree_sitter_languages import get_parser
            parser = get_parser(lang)
            with open(filepath, "r", encoding="utf-8") as f:
                code_bytes = f.read().encode("utf-8")
            
            tree = parser.parse(code_bytes)
            
            context_lines = []
            def traverse(node):
                if node.type in ['class_declaration', 'function_declaration', 'method_declaration', 'struct_specifier', 'interface_declaration', 'function_definition']:
                    start_byte = node.start_byte
                    # Extract the signature (limit to 200 chars to avoid grabbing huge blocks)
                    code_snippet = code_bytes[start_byte:start_byte+200].decode('utf-8', errors='ignore')
                    signature = code_snippet.split('{')[0].split('\n')[0].strip()
                    if signature:
                        context_lines.append(f"  * Declared in `{os.path.basename(filepath)}`: {signature}")
                for child in node.children:
                    traverse(child)
                    
            traverse(tree.root_node)
            return "\n".join(context_lines)
        except Exception as e:
            print(f"⚠️ AST extraction failed for {filepath}: {e}")
            return ""

    @classmethod
    def resolve_context(cls, diff_text: str, workspace_root: str = ".") -> str:
        valid_files = set()
        for line in diff_text.splitlines():
            if line.startswith("+++ "):
                filepath = line[4:].strip().lstrip('b/').lstrip('./').lstrip('/')
                valid_files.add(filepath)

        ast_results = []
        for filepath in valid_files:
            full_path = os.path.join(workspace_root, filepath)
            ctx = cls.extract_ast_context(full_path)
            if ctx:
                ast_results.append(ctx)

        if not ast_results:
            return ""

        markdown_lines = [
            "\n### 🔍 AST CODEBASE ARCHITECTURE CONTEXT:",
            "Here are the structural signatures (classes/methods/interfaces) of the modified files extracted via Abstract Syntax Tree:"
        ]
        markdown_lines.extend(ast_results)
        markdown_lines.append("\n*Use this precise structural context to identify the programming language, variable types, class architectures, and safety parameters dynamically.*")
        
        return "\n".join(markdown_lines)

    @classmethod
    def resolve_full_files_context(cls, diff_text: str, workspace_root: str = ".") -> str:
        valid_files = set()
        for line in diff_text.splitlines():
            if line.startswith("+++ "):
                filepath = line[4:].strip().lstrip('b/').lstrip('./').lstrip('/')
                if filepath != "dev/null":
                    valid_files.add(filepath)

        # SAFETY VALVE: Skip full file context for massive PRs to maintain LLM focus
        if len(valid_files) > 25:
            print(f"⚠️ Massive PR detected ({len(valid_files)} files). Skipping Full File Context to preserve LLM focus; relying on Diff + AST only.")
            return ""

        file_contents = []
        for filepath in valid_files:
            full_path = os.path.join(workspace_root, filepath)
            if not os.path.exists(full_path): continue
            
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    content = f.read()
                file_contents.append(f"### File: {filepath}\n```\n{content}\n```")
            except Exception as e:
                print(f"⚠️ Failed to read full file context for {filepath}: {e}")

        if not file_contents:
            return ""

        markdown_lines = [
            "\n### 📄 FULL MODIFIED FILE CONTENTS:",
            "Below are the complete contents of all files modified in this PR. Use these to understand logic and state outside the immediate 3-line diff window:"
        ]
        markdown_lines.extend(file_contents)
        
        full_text = "\n\n".join(markdown_lines)
        if len(full_text) > 400000:
            print("⚠️ Full file context too large, truncating...")
            full_text = full_text[:400000] + "\n\n[Full file context truncated for size]"
            
        return full_text

# ==============================================================================
# 5. METRICS EXPORTER CLIENT (Single Responsibility Principle - Telemetry Webhook)
# ==============================================================================
class MetricsExporter:
    """
    Manages telemetry logging and sending review statistics to external webhook triggers.
    """
    @staticmethod
    def export_metrics(webhook_url: str, payload: Dict[str, Any]) -> bool:
        """
        Posts review details to Google Sheets webhook endpoint.
        """
        try:
            print("📤 Exporting metrics to Google Sheets webhook...")
            response = requests.post(webhook_url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
            if response.status_code in (200, 302):
                print("✅ Metrics successfully pushed to Google Sheets!")
                return True
            else:
                print(f"⚠️ Webhook returned status code {response.status_code}: {response.text}")
                return False
        except Exception as e:
            print(f"❌ Failed to post metrics to webhook: {e}")
            return False

# ==============================================================================
# 6. ORCHESTRATION PIPELINE (Single Responsibility & Dependency Inversion)
# ==============================================================================
class PRReviewOrchestrator:
    """
    Coordinates and drives the entire PR review pipeline. 
    Accepts client dependencies via its constructor (Dependency Inversion).
    """
    def __init__(self, github_client: GitHubClient, llm_client: LLMClient):
        self.gh = github_client
        self.llm = llm_client
        self.checklist_path = os.getenv("CHECKLIST_PATH", ".github/checklist.md")

    def load_domain_config(self) -> Optional[Dict[str, Any]]:
        """
        Loads local domain configurations from domain_config.json.
        """
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "domain_config.json")
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Failed to load domain_config.json from {config_path}: {e}")
            return None

    def run(self) -> None:
        """
        Drives the multi-stage review pipeline execution.
        """
        start_time = time.time()
        # 1. Load configuration details
        config = self.load_domain_config()
        if not config:
            self.gh.post_failure_comment("Missing or malformed `domain_config.json`. Analysis cancelled.")
            sys.exit(1)

        domain_name = config.get("domain_name", "Generic")
        persona = config.get("persona", "Lead Developer")
        lang_block = config.get("lang_block", "")
        default_checklist = config.get("default_checklist", "General best practices applied.")
        hunter_prompt_extra = config.get("hunter_prompt_extra", "Analyze this Git Diff for bugs.")
        verifier_risks_prompt = config.get("verifier_risks_prompt", "Highlight potential risks.")
        
        # 100% Generic Fallback Grounding Rules
        default_grounding = """Before writing any verified finding to the output JSON, you MUST run each candidate finding through this strict self-correction pass:

1. **The Scope Check:**
   - *Is the candidate issue referencing a line number that was NOT added or modified in the diff (lines starting with '+')?*
   - *If YES:* You MUST delete/reject this finding entirely. You are strictly forbidden from reporting issues on unchanged context lines.

2. **The Certainty Check:**
   - *Are you 100% certain of the variable types, API interfaces, or framework behaviors based on the provided diff and codebase context?*
   - *If NO (i.e. you are guessing or assuming a type without explicit codebase declarations):* Do NOT flag this as a critical bug or logical failure. Demote it to a minor suggestion or delete it entirely.

3. **The Styling & Convention Check:**
   - *Is the candidate issue a minor code-styling preference (e.g. indentation, bracket placement, variable naming styles) rather than a safety, stability, or logic bug?*
   - *If YES:* Do not block the merge. Demote it to a minor warning or delete it entirely."""

        verifier_grounding_rules = config.get("verifier_grounding_rules", default_grounding)
        
        improvement_rule = """
* **The Improvement vs. Issue Check:**
  - *Is the candidate finding actually describing a positive change, refactoring, or robustness improvement introduced by the developer in the PR (e.g. adding guard clauses, extracting duplicate code, adding helper parameters) rather than a bug, security risk, crash risk, or architectural violation?*
  - *If YES:* You MUST delete/reject this finding entirely. You are strictly forbidden from reporting developer improvements as issues/warnings.
"""
        
        # Dynamically inject the rule if not already present
        if "Improvement vs. Issue Check" not in str(verifier_grounding_rules):
            if isinstance(verifier_grounding_rules, list):
                verifier_grounding_rules.append(improvement_rule.strip())
            elif isinstance(verifier_grounding_rules, str):
                certainty_marker = "*Only output findings that survive"
                if certainty_marker in verifier_grounding_rules:
                    parts = verifier_grounding_rules.split(certainty_marker)
                    verifier_grounding_rules = parts[0].rstrip() + "\n\n" + improvement_rule.strip() + "\n\n" + certainty_marker + parts[1]
                else:
                    verifier_grounding_rules = verifier_grounding_rules.rstrip() + "\n\n" + improvement_rule.strip()

        pr_summary_title = "PR Analysis"
        
        try:
            # 2. Fetch critical PR metadata & diff
            meta = self.gh.fetch_pr_data()
            if not meta:
                self.gh.post_failure_comment("Failed to fetch PR metadata.")
                sys.exit(1)
            
            pr_summary_title = meta['title']
            diff = self.gh.fetch_diff(meta['diff_url'])
            if not diff:
                self.gh.post_failure_comment("PR diff is empty or could not be fetched.")
                sys.exit(1)

            # 2.1 Extract Valid Diff Line Numbers
            valid_lines_map = DiffParser.parse_valid_lines(diff)
            valid_lines_str = "\n".join([f"- `{path}`: lines {sorted(list(lines))}" for path, lines in valid_lines_map.items()])

            # Retrieve checklist template
            checklist = self.gh.fetch_checklist(meta['base_branch'], self.checklist_path) or default_checklist

            # Retrieve architectural constraints
            constraints_path = os.getenv("CONSTRAINTS_PATH", ".github/architectural_constraints.md")
            constraints = ""
            try:
                if os.path.exists(constraints_path):
                    print("📖 Loading architectural constraints locally...")
                    with open(constraints_path, "r", encoding="utf-8") as f:
                        constraints = f.read()
            except Exception as e:
                print(f"⚠️ Failed to read local constraints: {e}")

            if not constraints:
                print("🌐 Fetching architectural constraints from base branch...")
                constraints = self.gh.fetch_checklist(meta['base_branch'], constraints_path) or ""

            # 3. Concurrency Stage: Run Summary and Hunter passes in parallel
            print(f"🚀 Starting {domain_name} Parallel Analysis passes...")
            full_file_context = UniversalContextGrabber.resolve_full_files_context(diff, workspace_root=".")
            with ThreadPoolExecutor(max_workers=2) as executor:
                summary_prompt = (
                    f"Summarize in one short sentence what this PR titled '{meta['title']}' "
                    f"is attempting to achieve in the {domain_name} project. "
                    f"Title: {meta['title']}, Description: {meta['description']}"
                )
                summary_future = executor.submit(self.llm.get_completion, summary_prompt)
                
                hunter_prompt = f"{hunter_prompt_extra} Output a JSON array of objects with 'path', 'line', and 'finding'.\n\n{diff}\n{full_file_context}"
                hunter_future = executor.submit(self.llm.get_completion, hunter_prompt, is_json=True)

                pr_summary_text = summary_future.result() or meta['title']
                raw_issues = hunter_future.result() or []

            potential_issues = []
            if isinstance(raw_issues, list):
                potential_issues = raw_issues
            elif isinstance(raw_issues, dict):
                potential_issues = (raw_issues.get('findings') or 
                                    raw_issues.get('issues') or 
                                    raw_issues.get('potential_issues') or [])
            if not isinstance(potential_issues, list):
                potential_issues = []

            # 4. Verifier Pass: Synthesize Hunter Findings and the Code
            print(f"🛡️ Starting {domain_name} verification pass...")
            codebase_context = UniversalContextGrabber.resolve_context(diff, workspace_root=".")
            verifier_prompt = f"""
You are the {persona}. Verify findings and generate a dual JSON report.

### 🛠️ YOU ARE AN AGENT (TOOL ACCESS)
You have access to tools (`grep_search` and `view_file`). 
Before generating the final JSON report, you may call tools to:
- Verify if a variable/function is used elsewhere in the codebase.
- Read the implementation of helper functions used in the PR.
- Investigate the broader architecture to avoid false positives.

Once you have gathered enough information, output the final JSON report.

### 🏗️ PROJECT-WIDE ARCHITECTURAL & CODING CONSTRAINTS:
{constraints}

{codebase_context}

{full_file_context}

### 📝 ONLY ANALYZE AND REPORT ON THESE MODIFIED LINES:
Below is the list of files and the exact line numbers that were added or modified in the PR:
{valid_lines_str}

CRITICAL: You are strictly forbidden from reporting any issues or violations on lines not listed above. Any issue on an unchanged line is invalid.

### 🛡️ STRICT SELF-CORRECTION & GROUNDING RULES (DO THIS FIRST):
{verifier_grounding_rules}

### **STRICT JSON REQUIREMENTS**:
- Output MUST be valid JSON. 
- ESCAPE all newlines as `\n` inside strings. Do NOT use literal newlines.
- ESCAPE all double quotes as `\"` inside strings.
- Avoid using double quotes inside the markdown report if possible (use single quotes instead).
- Double check that the "markdown_report" string is correctly escaped.

**REQUIRED OUTPUT JSON STRUCTURE**:
You MUST output EXACTLY one JSON object matching this structure:
```json
{{
  "scratchpad": "Trace execution flow and thread safety here before writing the report.",
  "markdown_report": "Full Markdown report text formatted EXACTLY as described below.",
  "verified_findings": [
    {{
      "path": "path/to/file.kt",
      "line": 123,
      "chain_of_thought": {{
        "evidence": "Quote the exact lines from the diff or broader context.",
        "broader_context_verification": "If the broader context proves this code is safe, OR if you lack sufficient context to be 100% certain it is a bug, you MUST assume it is intentional.",
        "cross_examination": "Play devil's advocate. What is the strongest argument that this code is intentional? If the defense is stronger than the critique, you MUST set severity to 'invalid'."
      }},
      "severity": "critical|major|minor|invalid",
      "critique": "text",
      "surgical_fix": "code"
    }}
  ],
  "merge_verdict": "🔴 HARD STOP"
}}
```

### 📋 Formatting Guide for "markdown_report":
Construct the "markdown_report" to be extremely concise, visual, and action-oriented. Do not write long paragraphs of text.

1. **Top Alert Block**:
   Wrap the verdict, justification, and summary inside a single GitHub-style alert block:
   - For '🔴 HARD STOP', use '> [!CAUTION]' and the exact title '> ### 🔴 **Merge Verdict: HARD STOP**'
   - For '🟡 Needs Review', use '> [!WARNING]' and the exact title '> ### 🟡 **Merge Verdict: Needs Review**'
   - For '🟢 LGTM', use '> [!NOTE]' and the exact title '> ### 🟢 **Merge Verdict: LGTM**'
   
    **STRICT METRIC BOLDING RULE (For Hard Stop or Needs Review)**:
    If the verdict is '🔴 HARD STOP' or '🟡 Needs Review', inside the 1-sentence professional justification you MUST explicitly highlight the counts of critical, major, and minor issues/warnings in bold. For example: "This PR introduces **2 critical**, **1 major**, and **2 minor** issues." or similar.
    
    **STRICT LGTM PRAISE RULE (For LGTM)**:
    If the verdict is '🟢 LGTM' (0 issues), write a warm, highly encouraging, and professional appreciation message. Highlight in bold that the PR introduces **0 issues** or **no issues**. Then, follow it with a clean, bulleted list of 2-3 specific architectural or code quality highlights of the changes (e.g. Robustness, Clean Refactoring, Performance) summarizing what was done well.
    
    Structure inside the block (exact markdown):
    > [!CAUTION] (or !WARNING / !NOTE)
    > ### [Verdict Emoji] **Merge Verdict: [Verdict Status]**
    > [1-sentence justification/praise with bolded metrics]
    > [If LGTM, include empty line followed by key highlights bullet points]
   
2. **Action Required Punch List**:
   Present all findings grouped by severity and aggregated by category under '### 🛠️ Action Required'. Do not use blockquotes, card boxes, or expandable details blocks.
   
   **STRICT CATEGORY GROUPING & AGGREGATION RULE**:
   - You MUST group findings under the following three subheadings (only output a subheading if there are findings of that severity):
     - `#### 🔴 Critical`
     - `#### 🟠 Major`
     - `#### 🟡 Minor / Warnings`
   - Format each aggregated category as a single bullet point. It must contain the issue category name (and count if multiple) in bold, followed by a pipe separator (` | `), followed by the file(s) and their respective line numbers:
     - If there is only **1 instance** of an issue category under that severity:
       `* **[Issue Category Name]** | [File Name](../blob/{meta['head_sha']}/Relative_Path), Line [Lines]`
     - If there are **multiple instances** (e.g., N instances) of the same issue category under that severity, group them by file name and join the files using standard list formatting (e.g., `[File 1], Line [Lines 1] and [File 2], Line [Lines 2]` or `[File 1], Line [Lines 1], [File 2], Line [Lines 2] and [File 3], Line [Lines 3]`):
       `* **[N] Instance of [Issue Category Name]** | [File Name 1](../blob/{meta['head_sha']}/Relative_Path_1), Line [Lines 1], [File Name 2](../blob/{meta['head_sha']}/Relative_Path_2), Line [Lines 2] and [File Name 3](../blob/{meta['head_sha']}/Relative_Path_3), Line [Lines 3]`
   - DO NOT include descriptions or extra text. Keep it extremely concise.
   - File Name Link format: Standard markdown link `[File Name](../blob/{meta['head_sha']}/Relative_Path)` pointing to the file (no line number anchor in the URL path).
   - Line number format (based on the number of findings in that file):
     - If 1 line: `, Line [Line_1]` (e.g. `, Line 33`)
     - If 2 lines: `, Line [Line_1] and [Line_2]` (e.g. `, Line 20 and 45`)
     - If 3+ lines: `, Line [Line_1], [Line_2] and [Line_3]` (e.g. `, Line 4, 5 and 6`)
   
   **STRICT EMPTY SECTION EXCLUSION RULE**:
   If there are **0 issues**, you MUST completely omit the '### 🛠️ Action Required' section and its following divider line (`---`). Do not print the header, subheadings, or any bullet points.
   
   Structure inside the list (only output if issues > 0):
    ### 🛠️ Action Required
    
    #### 🔴 Critical
    * **Context Memory Leak** | [Library.kt](../blob/{meta['head_sha']}/app/src/main/java/com/example/Library.kt), Line 4
    * **3 Instance of Context Memory Leak** | [Library.kt](../blob/{meta['head_sha']}/app/src/main/java/com/example/Library.kt), Line 4, 5 and 6
    * **6 Instance of Context Memory Leak** | [Library.kt](../blob/{meta['head_sha']}/app/src/main/java/com/example/Library.kt), Line 4, 5 and 6, [MainActivity.kt](../blob/{meta['head_sha']}/app/src/main/java/com/example/MainActivity.kt), Line 12 and [AppService.kt](../blob/{meta['head_sha']}/app/src/main/java/com/example/AppService.kt), Line 20 and 21
    
    #### 🟠 Major
    * **[Issue Category Name]** | [File Name](../blob/{meta['head_sha']}/Relative_Path), Line [Lines]
    
    #### 🟡 Minor / Warnings
    * **[Issue Category Name]** | [File Name](../blob/{meta['head_sha']}/Relative_Path), Line [Lines]
    
    Follow this action items section with a divider line (only if issues > 0), ensuring there is an empty line before the divider:
    
    ---

3. **Definition of Done (DoD) Compliance (Table-Free)**:
    Present the Definition of Done (DoD) compliance checklist as a clean, flat list under '### 🛡️ Definition of Done (DoD)'.
    
    **STRICT NO-TABLE RULE**:
    You MUST NOT use HTML tables, markdown tables, or `<nobr>` tags anywhere in this section.
    
    **STRICT EMPTY ITEM EXCLUSION RULE**:
    - If there are no failed checks, you MUST completely omit the '🔴 FAILED' section.
    
    **STRICT DOD HIDE RULE**:
    If all DoD checks passed successfully (meaning there are 0 FAILED checklist items, and only PASSED checks exist), you MUST **completely omit the entire '### 🛡️ Definition of Done (DoD)' section** (including the heading and its contents) from the report.
    
    **STRICT CATEGORY GROUPING & AGGREGATION RULE FOR FAILED**:
    - Group categories that contain any failed checks under the header `**🔴 FAILED | [Total Failed Count] Checks:**`.
    - Format this line under the header exactly as follows (using `|` as the separator between badges, and no parentheses around the count):
      `* 🔴 **FAILED | [Total Failed Count] Checks:** `[Category 1 Name] [Violations Count 1]` | `[Category 2 Name] [Violations Count 2]` | ...`
      For example: `* 🔴 **FAILED | 10 Checks:** `Code Quality 2` | `Architecture 1` | `Performance 3` | `Memory & Lifecycle 4``
    
    **STRICT PASSED CHECKS CATEGORY AGGREGATION RULE**:
    - To eliminate text clutter and keep comments compact, you MUST NOT list every individual passed requirement by name. Instead, you MUST group all passed requirements by their Category Name (e.g. `Security`, `Documentation`, `PR Quality`) and display them on a single bulleted line using category badges with counts of passed checks.
    - You MUST limit the list of passed categories to a maximum of 5 categories (pick the first 5 completely passed categories if there are more than 5).
    - Format this line under the header exactly as follows (using `|` as the separator between badges, and no parentheses around the count):
      `* 🟢 **PASSED | [Total Passed Count] Checks:** `[Category 1 Name] [Passed Count 1]` | `[Category 2 Name] [Passed Count 2]` | ...`
      For example: `* 🟢 **PASSED | 11 Checks:** `Security 2` | `Documentation 1` | `PR Quality 3``
    
    Ensure you output EXACTLY the following structure under the header, dynamically hiding the empty parts based on the rules above:
    
    ### 🛡️ Definition of Done (DoD)
    
    * 🔴 **FAILED | [Total Failed Count] Checks:** `[Category 1 Name] [Violations Count 1]` | `[Category 2 Name] [Violations Count 2]` | ...
    
    * 🟢 **PASSED | [Total Passed Count] Checks:** `[Category 1 Name] [Passed Count 1]` | `[Category 2 Name] [Passed Count 2]` | ...

---

Diff: {diff}
Findings: {json.dumps(potential_issues, indent=2)}
Checklist: {checklist}
"""
            raw_verified_res = self.llm.get_completion(verifier_prompt, is_json=True, enable_tools=True)
            v_data = {}
            if isinstance(raw_verified_res, dict):
                v_data = raw_verified_res
            elif isinstance(raw_verified_res, list) and len(raw_verified_res) > 0 and isinstance(raw_verified_res[0], dict):
                v_data = raw_verified_res[0]
            
            if not v_data:
                print("⚠️ Verifier returned invalid or empty JSON.")
            else:
                if 'markdown_report' not in v_data:
                    print(f"⚠️ VERIFIER OUTPUT MISSING 'markdown_report'. Keys found: {list(v_data.keys())}")
                    print(f"RAW JSON:\n{json.dumps(v_data, indent=2)}")

            # 5. Filter Verified Findings and Re-synthesize Report if needed
            verified_findings = v_data.get('verified_findings', [])
            filtered_findings = []
            excluded_findings = []
            
            if isinstance(verified_findings, list):
                for f in verified_findings:
                    if not isinstance(f, dict):
                        continue
                    path = f.get('path')
                    line = f.get('line')
                    if path and line:
                        try:
                            line_num = int(line)
                            normalized_path = path.strip().lstrip('./').lstrip('/')
                            severity = str(f.get('severity', '')).lower()
                            if severity == 'invalid':
                                excluded_findings.append(f)
                            elif normalized_path in valid_lines_map and line_num in valid_lines_map[normalized_path]:
                                filtered_findings.append(f)
                            else:
                                excluded_findings.append(f)
                        except Exception:
                            excluded_findings.append(f)
                    else:
                        excluded_findings.append(f)

            if len(excluded_findings) > 0:
                print(f"ℹ️ {len(excluded_findings)} findings fell outside the diff and will be excluded to reduce noise.")
                v_data['verified_findings'] = filtered_findings
                
                # Check if no findings are left
                if not filtered_findings:
                    v_data['merge_verdict'] = "🟢 LGTM"
                    v_data['markdown_report'] = "> [!NOTE]\n> ### 🟢 **Merge Verdict: LGTM**\n> This PR introduces **0 issues** and is compliant with all architectural and code quality rules."
                else:
                    # Re-synthesize the report using the LLM to remove the excluded findings
                    print("🔄 Re-synthesizing review report to exclude out-of-diff findings...")
                    re_synth_prompt = f"""You are a context-aware PR review editor.
We have analyzed a PR, but some of the reported findings were on unchanged lines and have been filtered out.

Original JSON report:
{json.dumps(v_data, indent=2)}

We have kept only these verified findings:
{json.dumps(filtered_findings, indent=2)}

Please regenerate the 'markdown_report' and update the 'merge_verdict' so that:
1. Any mentions of the filtered-out/excluded findings (issues not present in the kept findings list) are completely removed from the report.
2. The alert block header is updated to accurately count the remaining critical, major, and minor issues/warnings.
3. The Definition of Done (DoD) checks are updated to reflect the new counts.
4. If there are 0 findings remaining, update the verdict to '🟢 LGTM' and write a warm appreciation message as required by the formatting guide.

Return ONLY a JSON object matching this structure:
{{
  "markdown_report": "Updated Markdown report text",
  "merge_verdict": "🟢 LGTM | 🟡 Needs Review | 🔴 HARD STOP"
}}
"""
                    cleaned_res = self.llm.get_completion(re_synth_prompt, is_json=True)
                    if cleaned_res and isinstance(cleaned_res, dict):
                        v_data['markdown_report'] = cleaned_res.get('markdown_report', v_data['markdown_report'])
                        v_data['merge_verdict'] = cleaned_res.get('merge_verdict', v_data['merge_verdict'])

            # 6. Bundle Inline Comments
            bundled_comments = []
            fallback_comments = []
            verified_findings = v_data.get('verified_findings', [])
            
            if isinstance(verified_findings, list):
                for f in verified_findings:
                    if not isinstance(f, dict): 
                        continue
                    path, line = f.get('path'), f.get('line')
                    critique = str(f.get('critique') or 'No critique.')
                    fix = str(f.get('surgical_fix') or '// No fix.').strip()
                    if fix.startswith('```') and fix.endswith('```'):
                        lines = fix.split('\n')
                        if len(lines) >= 2:
                            fix = '\n'.join(lines[1:-1]).strip()
                    
                    if path and line:
                        try:
                            line_num = int(line)
                            normalized_path = path.strip().lstrip('./').lstrip('/')
                            body = f"**Critique**: {critique}\n\n**Surgical Fix**:\n```{lang_block}\n{fix}\n```"
                            
                            # Verify if the target line actually falls inside the modified diff range
                            if normalized_path in valid_lines_map and line_num in valid_lines_map[normalized_path]:
                                bundled_comments.append({ "path": normalized_path, "line": line_num, "body": body })
                            else:
                                fallback_comments.append(f"**File**: `{path}` (Line {line_num})\n{body}")
                        except Exception: 
                            continue

            # 7. Determine GitHub Event Type
            verdict = v_data.get('merge_verdict', '🟡 Needs Review')
            
            # Normalize verdict formatting to standard 🟢, 🟡, 🔴 circles
            if any(x in verdict for x in ["✅", "🟢", "\u2705", "\\u2705"]) or "lgtm" in verdict.lower():
                verdict = "🟢 LGTM"
            elif any(x in verdict for x in ["🔴", "stop", "reject"]) or "stop" in verdict.lower():
                verdict = "🔴 HARD STOP"
            else:
                verdict = "🟡 Needs Review"

            github_event = "COMMENT"
            if "🔴" in verdict:
                github_event = "REQUEST_CHANGES"
            elif "🟢" in verdict and not bundled_comments and not fallback_comments:
                github_event = "APPROVE"

            # 8. Submit Review
            author_mention = f"@{meta['author']}"
            header = f"{author_mention}\n\n"
            markdown_report = v_data.get('markdown_report', "⚠️ Analysis report malformed.")
            
            # Post-process to remove DoD section if all checks passed
            if "### 🛡️ Definition of Done (DoD)" in markdown_report:
                parts = markdown_report.split("### 🛡️ Definition of Done (DoD)")
                prefix = parts[0]
                dod_content = parts[1]
                if "🔴" not in dod_content and "🟡" not in dod_content and "FAILED" not in dod_content.upper() and "WARNING" not in dod_content.upper():
                    prefix = prefix.rstrip()
                    if prefix.endswith("---"):
                        prefix = prefix[:-3].rstrip()
                    markdown_report = prefix
            
            full_body = header + markdown_report
            
            if fallback_comments:
                print(f"ℹ️ {len(fallback_comments)} findings fell outside the diff and were excluded from the PR comment to reduce noise.")
                for i, fc in enumerate(fallback_comments, 1):
                    print(f"  📝 Outside-Diff Finding #{i}: {fc[:200]}...")
            
            print(f"Attempting to submit review with {len(bundled_comments)} inline findings and {len(fallback_comments)} general findings...")
            res = self.gh.submit_bundled_review(full_body, github_event, bundled_comments)
            
            # API Fallback Logic: Bundle all comments inside the main review body if inline failed (e.g. 422 errors)
            if not (res and res.status_code < 300):
                print("⚠️ Bundled review failed. Attempting fallback (Appending inline comments to main body)...")
                
                if bundled_comments:
                    full_body += "\n\n### 📝 Converted Inline Findings (API Rejected)\n\n"
                    for bc in bundled_comments:
                        full_body += f"**File**: `{bc['path']}` (Line {bc['line']})\n{bc['body']}\n\n---\n\n"

                error_note = f"\n\n⚠️ **Review Diagnostic Info**:\nSome inline findings were converted to general comments because the GitHub API rejected the inline placement (e.g. diff too large).\n**Error**: `{self.gh.last_api_error}`"
                
                res_fallback = self.gh.submit_bundled_review(full_body + error_note, "COMMENT", [])
                if not (res_fallback and res_fallback.status_code < 300):
                    self.gh.post_failure_comment(self.gh.last_api_error or "Unknown API Error")
                    sys.exit(1)
            
            print(f"✅ {domain_name} Review submitted successfully.")

            # 9. Metrics Export Webhook Step
            webhook_url = os.getenv("METRICS_WEBHOOK_URL")
            if webhook_url:
                from datetime import datetime
                
                # Count critical, major, and minor findings from verified_findings
                critical_count = 0
                major_count = 0
                minor_count = 0
                if isinstance(verified_findings, list):
                    for f in verified_findings:
                        if isinstance(f, dict):
                            severity = f.get("severity", "minor").lower()
                            if "critical" in severity:
                                critical_count += 1
                            elif "major" in severity:
                                major_count += 1
                            else:
                                minor_count += 1
                
                if critical_count == 0 and major_count == 0 and minor_count == 0:
                    quick_summary = "No issues found"
                else:
                    summary_parts = []
                    if critical_count > 0:
                        summary_parts.append(f"{critical_count} critical")
                    if major_count > 0:
                        summary_parts.append(f"{major_count} major")
                    if minor_count > 0:
                        summary_parts.append(f"{minor_count} minor")
                    quick_summary = f"Found {', '.join(summary_parts)} issues"
                
                # Calculate PR Size based on added/modified lines in diff
                loc = sum(len(lines) for lines in valid_lines_map.values())
                if loc < 50:
                    pr_size = f"Small ({loc} LOC)"
                elif loc < 250:
                    pr_size = f"Medium ({loc} LOC)"
                else:
                    pr_size = f"Large ({loc} LOC)"
                
                # Calculate total execution cost
                cost = round(self.llm.calculate_cost(), 6) if hasattr(self.llm, "calculate_cost") else 0.0
                
                # Calculate total review duration
                time_taken_seconds = round(time.time() - start_time, 2)

                metrics_payload = {
                    "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "date": datetime.utcnow().strftime("%Y-%m-%d"),
                    "project": self.gh.repo,
                    "pr_number": self.gh.pr_number,
                    "author": meta.get('author', 'Unknown'),
                    "title": pr_summary_title,
                    "verdict": verdict,
                    "findings_count": len(bundled_comments) + len(fallback_comments),
                    "critical_findings": critical_count,
                    "major_findings": major_count,
                    "minor_findings": minor_count,
                    "quick_summary": quick_summary,
                    "pr_link": f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/{self.gh.repo}/pull/{self.gh.pr_number}",
                    "model_name": getattr(self.llm, "model_name", "Unknown Model"),
                    "pr_size": pr_size,
                    "domain": domain_name,
                    "estimated_cost": cost,
                    "review_time": f"{time_taken_seconds}s",
                    "bot_version": SCRIPT_VERSION
                }
                MetricsExporter.export_metrics(webhook_url, metrics_payload)

        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"💥 Fatal error inside pipeline: {e}")
            self.gh.post_failure_comment(str(e))
            sys.exit(1)

# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    # Gather workspace context and inputs
    repo_env = os.getenv("REPO")
    pr_num_env = os.getenv("PR_NUMBER")
    gh_token = os.getenv("GITHUB_TOKEN") or os.getenv("TOKEN_GH")
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("ANOTHER_API_KEY")
    model = "gemini-3.1-pro-preview"

    if not all([repo_env, pr_num_env, gh_token, gemini_key]):
        print("❌ Missing required environment variables (REPO, PR_NUMBER, GITHUB_TOKEN, GEMINI_API_KEY).")
        sys.exit(1)

    # 1. Instantiate concrete API dependency clients (SOLID DIP)
    github_client = GitHubClient(repo=repo_env, pr_number=pr_num_env, token=gh_token)
    gemini_client = GeminiClient(model_name=model, api_key=gemini_key)

    # 2. Inject clients into orchestrator pipeline and run
    orchestrator = PRReviewOrchestrator(github_client=github_client, llm_client=gemini_client)
    orchestrator.run()
