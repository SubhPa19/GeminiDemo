import os
import json
import requests
import sys
import time
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional, Dict, Set

# ==============================================================================
# SCRIPT METADATA & CONSTANTS
# ==============================================================================
SCRIPT_VERSION = "1.3.1"
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

    def get_completion(self, prompt: str, is_json: bool = False) -> Any:
        """
        Sends content generation request to Gemini API and parses response.
        """
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        if is_json:
            payload["generationConfig"] = {
                "response_mime_type": "application/json"
            }
        
        headers = {"Content-Type": "application/json"}
        
        # Simple retry loop for standard network/transient failures
        for attempt in range(3):
            try:
                res = requests.post(self.gemini_url, json=payload, headers=headers, timeout=60)
                res.raise_for_status()
                response_json = res.json()
                
                # Track token usage from API metadata
                usage = response_json.get('usageMetadata', {})
                self.total_input_tokens += usage.get('promptTokenCount', 0)
                self.total_output_tokens += usage.get('candidatesTokenCount', 0)
                
                text = response_json['candidates'][0]['content']['parts'][0]['text']
                if is_json:
                    return self._parse_gemini_json(text)
                return text
            except Exception as e:
                print(f"⚠️ Gemini request attempt {attempt+1} failed: {e}")
                time.sleep(2 ** attempt + 1)
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
            return json.loads(clean_text)
        except json.JSONDecodeError as e:
            print(f"⚠️ Initial JSON parse failed: {e}. Attempting extraction...")
            # Fallback: Extract the first { ... } or [ ... ] block
            match = re.search(r'(\{.*\}|\[.*\])', clean_text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError as e2:
                    print(f"❌ Extraction fallback also failed: {e2}")
            
            # Final attempt: handle potential unescaped control characters
            try:
                sanitized = re.sub(r'(?<!\\)\n', '\\n', clean_text)
                return json.loads(sanitized)
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
            "body": body + f"\n\n---\n🤖 **Grace-{SCRIPT_VERSION}** | [Share Feedback](https://forms.gle/bpRX129ku5YMi9JLA) | [Walkthrough & Guide](https://drive.google.com/file/d/1paA9hswGG1MazQF_0WBvrZD9sKBUscOy/view?usp=drive_link)\n{BOT_MARKER}",
            "event": event,
            "comments": comments
        }
        return self._safe_request("POST", url, headers=self.api_headers, json=payload)

    def post_failure_comment(self, error_msg: str) -> Optional[requests.Response]:
        """
        Posts a fallback issue comment if the main review fails to submit.
        """
        url = f"{self.base_url}/issues/{self.pr_number}/comments"
        body = f"❌ **Review Failure Report**\n\nThe PR summarizer script failed to submit a formal review.\n\n**Error Details**:\n```\n{error_msg}\n```\n\n---\n🤖 **Grace-{SCRIPT_VERSION}** | [Share Feedback](https://forms.gle/bpRX129ku5YMi9JLA) | [Walkthrough & Guide](https://drive.google.com/file/d/1paA9hswGG1MazQF_0WBvrZD9sKBUscOy/view?usp=drive_link)\n{BOT_MARKER}"
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
                if not line or line[0] in ('+', ' '):
                    valid_lines[current_file].add(current_line_new)
                    current_line_new += 1
                elif line.startswith("-"):
                    pass

        return valid_lines

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

            # Retrieve checklist template
            checklist = self.gh.fetch_checklist(meta['base_branch'], self.checklist_path) or default_checklist

            # 3. Concurrency Stage: Run Summary and Hunter passes in parallel
            print(f"🚀 Starting {domain_name} Parallel Analysis passes...")
            with ThreadPoolExecutor(max_workers=2) as executor:
                summary_prompt = (
                    f"Summarize in one short sentence what this PR titled '{meta['title']}' "
                    f"is attempting to achieve in the {domain_name} project. "
                    f"Title: {meta['title']}, Description: {meta['description']}"
                )
                summary_future = executor.submit(self.llm.get_completion, summary_prompt)
                
                hunter_prompt = f"{hunter_prompt_extra} Output a JSON array of objects with 'path', 'line', and 'finding'.\n\n{diff}"
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
            verifier_prompt = f"""
You are the {persona}. Verify findings and generate a dual JSON report.

### **STRICT JSON REQUIREMENTS**:
- Output MUST be valid JSON. 
- ESCAPE all newlines as `\n` inside strings. Do NOT use literal newlines.
- ESCAPE all double quotes as `\"` inside strings.
- Avoid using double quotes inside the markdown report if possible (use single quotes instead).
- Double check that the "markdown_report" string is correctly escaped.

**REQUIRED OUTPUT JSON KEYS**:
1. "markdown_report": Full Markdown report text (DoD Check, Risks, Verdict).
2. "verified_findings": JSON logic array [{{"path": "path", "line": 123, "severity": "critical|minor", "critique": "text", "surgical_fix": "code"}}]
3. "merge_verdict": 🟢 LGTM, 🟡 Needs Review, or 🔴 HARD STOP.

### ✅ Verification Verdict: DoD Check
Format the DoD Check section exactly like this. Make sure to include empty lines before and after the markdown tables:

### 🔴 Failed Checks

| Requirement | Status | Reasoning/Note |
| :--- | :--- | :--- |
| [Checklist Item] | ❌ / 🟠 | [Short reasoning] |

<details>
<summary><b>✅ Passed Checks</b></summary>

| Requirement | Status | Reasoning/Note |
| :--- | :--- | :--- |
| [Checklist Item] | ✅ | [Short reasoning] |

</details>

### ⚠️ Technical Risks ({domain_name} Context)
{verifier_risks_prompt}

### 🛑 Merge Verdict
(Exactly ONE: 🟢 **LGTM**, 🟡 **Needs Review**, 🔴 **HARD STOP**)
[1-sentence professional justification].

---

Diff: {diff}
Findings: {json.dumps(potential_issues, indent=2)}
Checklist: {checklist}
"""
            raw_verified_res = self.llm.get_completion(verifier_prompt, is_json=True)
            v_data = {}
            if isinstance(raw_verified_res, dict):
                v_data = raw_verified_res
            elif isinstance(raw_verified_res, list) and len(raw_verified_res) > 0 and isinstance(raw_verified_res[0], dict):
                v_data = raw_verified_res[0]
            
            if not v_data:
                print("⚠️ Verifier returned invalid or empty JSON.")

            # 5. Extract Valid Diff Line Numbers
            valid_lines_map = DiffParser.parse_valid_lines(diff)

            # 6. Bundle Inline Comments
            bundled_comments = []
            fallback_comments = []
            verified_findings = v_data.get('verified_findings', [])
            
            if isinstance(verified_findings, list):
                for f in verified_findings:
                    if not isinstance(f, dict): 
                        continue
                    path, line = f.get('path'), f.get('line')
                    critique = f.get('critique', 'No critique.')
                    fix = f.get('surgical_fix', '// No fix.')
                    
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
            header = f"{author_mention}\n> **Summary:** {pr_summary_text}\n\n"
            full_body = header + v_data.get('markdown_report', "⚠️ Analysis report malformed.")
            
            if fallback_comments:
                full_body += "\n\n### 📝 General Findings (Outside Diff)\n\n" + "\n\n---\n\n".join(fallback_comments)
            
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
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    if not all([repo_env, pr_num_env, gh_token, gemini_key]):
        print("❌ Missing required environment variables (REPO, PR_NUMBER, GITHUB_TOKEN, GEMINI_API_KEY).")
        sys.exit(1)

    # 1. Instantiate concrete API dependency clients (SOLID DIP)
    github_client = GitHubClient(repo=repo_env, pr_number=pr_num_env, token=gh_token)
    gemini_client = GeminiClient(model_name=model, api_key=gemini_key)

    # 2. Inject clients into orchestrator pipeline and run
    orchestrator = PRReviewOrchestrator(github_client=github_client, llm_client=gemini_client)
    orchestrator.run()
