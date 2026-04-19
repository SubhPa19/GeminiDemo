import os
import json
import requests
import sys
import time
import re
from concurrent.futures import ThreadPoolExecutor

class PRSummarizer:
    def __init__(self):
        self.repo = os.getenv("REPO")
        self.pr_number = os.getenv("PR_NUMBER")
        # Support both secret names used in different repos
        self.github_token = os.getenv("GITHUB_TOKEN") or os.getenv("TOKEN_GH")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("ANOTHER_API_KEY")
        self.checklist_path = os.getenv("CHECKLIST_PATH", ".github/checklist.md")
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.bot_marker = "<!-- gemini-bot-review -->" 
        self.last_api_error = None
        
        if not all([self.repo, self.pr_number, self.github_token, self.gemini_api_key]):
            print("❌ Missing required environment variables (REPO, PR_NUMBER, GITHUB_TOKEN, etc.).")
            sys.exit(1)

        self.api_headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        github_api_base = os.getenv("GITHUB_API_URL") or "https://api.github.com"
        self.base_url = f"{github_api_base.rstrip('/')}/repos/{self.repo}"
        self.gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent?key={self.gemini_api_key}"

    def _safe_request(self, method, url, **kwargs):
        """Generic request wrapper with optimized 429 (Rate Limit) and 422 handling."""
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
                
                res.raise_for_status()
                return res
            except Exception as e:
                if not self.last_api_error:
                    self.last_api_error = str(e)
                print(f"⚠️ Attempt {attempt+1} failed for {url}: {e}")
                time.sleep(2 ** attempt + 1)
        return None

    def fetch_pr_data(self):
        print("🔍 Fetching PR metadata...")
        url = f"{self.base_url}/pulls/{self.pr_number}"
        res = self._safe_request("GET", url, headers=self.api_headers)
        if not res: return None
        
        data = res.json()
        return {
            "author": data.get("user", {}).get("login", "Developer"),
            "base_branch": data.get("base", {}).get("ref", "main"),
            "title": data.get("title", "this PR"),
            "description": data.get("body", ""),
            "diff_url": url,
            "head_sha": data.get("head", {}).get("sha")
        }

    def fetch_diff(self, url):
        print("💾 Fetching PR diff...")
        headers = self.api_headers.copy()
        headers["Accept"] = "application/vnd.github.v3.diff"
        res = self._safe_request("GET", url, headers=headers)
        if not res: return ""
        
        diff = res.text
        if len(diff) > 60000:
            print("⚠️ Diff too large, truncating...")
            diff = diff[:60000] + "\n\n[Diff truncated for size]"
        return diff

    def fetch_checklist(self, branch):
        url = f"{self.base_url}/contents/{self.checklist_path}?ref={branch}"
        res = self._safe_request("GET", url, headers={"Authorization": f"Bearer {self.github_token}", "Accept": "application/vnd.github.v3.raw"})
        if res and res.status_code == 200:
            return res.text
        return None # Return None if not found

    def get_gemini_completion(self, prompt, is_json=False):
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        if is_json:
            payload["generationConfig"] = {
                "response_mime_type": "application/json"
            }
        
        res = self._safe_request("POST", self.gemini_url, json=payload)
        if not res: return None
        
        try:
            text = res.json()['candidates'][0]['content']['parts'][0]['text']
            if is_json:
                return self._parse_gemini_json(text)
            return text
        except (KeyError, IndexError):
            return None

    def _parse_gemini_json(self, text):
        """Robustly extracts and parses JSON from Gemini's response."""
        if not text: return None
        
        # Clean the text: sometimes Gemini wraps JSON in backticks despite response_mime_type
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
                # Replace unescaped newlines inside strings (experimental)
                # This is risky but sometimes helps with malformed markdown fields
                sanitized = re.sub(r'(?<!\\)\n', '\\n', clean_text)
                return json.loads(sanitized)
            except:
                return None

    def submit_bundled_review(self, body, event, comments):
        """Submits all findings in a single Bundled Review to minimize noise."""
        print(f"📦 Submitting bundled review ({len(comments)} inline findings) as {event}...")
        url = f"{self.base_url}/pulls/{self.pr_number}/reviews"
        payload = {
            "body": body + f"\n\n{self.bot_marker}",
            "event": event,
            "comments": comments
        }
        return self._safe_request("POST", url, headers=self.api_headers, json=payload)

    def post_failure_comment(self, error_msg):
        """Posts a standalone comment if the review process fails completely."""
        url = f"{self.base_url}/issues/{self.pr_number}/comments"
        body = f"❌ **Review Failure Report**\n\nThe PR summarizer script failed to submit a formal review.\n\n**Error Details**:\n```\n{error_msg}\n```\n\n{self.bot_marker}"
        print("🚀 Posting failure comment to PR...")
        return self._safe_request("POST", url, headers=self.api_headers, json={"body": body})

    def load_domain_config(self):
        """Loads domain-specific configuration from domain_config.json."""
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "domain_config.json")
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Failed to load domain_config.json from {config_path}: {e}")
            return None

    def run(self):
        # 0. Load Domain Config
        config = self.load_domain_config()
        if not config:
            self.post_failure_comment("Missing or malformed `domain_config.json`. Analysis cancelled.")
            return

        domain_name = config.get("domain_name", "Generic")
        persona = config.get("persona", "Lead Developer")
        lang_block = config.get("lang_block", "")
        default_checklist = config.get("default_checklist", "General best practices applied.")
        hunter_prompt_extra = config.get("hunter_prompt_extra", "Analyze this Git Diff for bugs.")
        verifier_risks_prompt = config.get("verifier_risks_prompt", "Highlight potential risks.")
        verifier_tests_prompt = config.get("verifier_tests_prompt", "Suggested test cases.")

        pr_summary_title = "PR Analysis"
        try:
            # 1. Fetch Metadata and Diff
            meta = self.fetch_pr_data()
            if not meta:
                self.post_failure_comment("Failed to fetch PR metadata.")
                return
            
            pr_summary_title = meta['title']
            diff = self.fetch_diff(meta['diff_url'])
            if not diff:
                self.post_failure_comment("PR diff is empty or could not be fetched.")
                return

            checklist = self.fetch_checklist(meta['base_branch']) or default_checklist

            # 2. Concurrency: Run Summary and Hunter passes in parallel
            print(f"🚀 Starting {domain_name} Analysis passes...")
            with ThreadPoolExecutor(max_workers=2) as executor:
                summary_prompt = f"Summarize in one short sentence what this PR titled '{meta['title']}' is attempting to achieve in the {domain_name} project. Title: {meta['title']}, Description: {meta['description']}"
                summary_future = executor.submit(self.get_gemini_completion, summary_prompt)
                
                hunter_prompt = f"{hunter_prompt_extra} Output a JSON array of objects with 'path', 'line', and 'finding'.\n\n{diff}"
                hunter_future = executor.submit(self.get_gemini_completion, hunter_prompt, is_json=True)

                pr_summary_text = summary_future.result() or meta['title']
                raw_issues = hunter_future.result() or "[]"
            
            try:
                potential_issues = json.loads(raw_issues)
                if isinstance(potential_issues, dict):
                    potential_issues = (potential_issues.get('findings') or 
                                        potential_issues.get('issues') or 
                                        potential_issues.get('potential_issues') or [])
                if not isinstance(potential_issues, list):
                    potential_issues = []
            except:
                potential_issues = []

            # 3. VERIFIER PASS
            print(f"🛡️ Starting {domain_name} verification pass...")
            verifier_prompt = f"""
You are the {persona}. Verify findings and generate a dual JSON report.

### **STRICT JSON REQUIREMENTS**:
- Output MUST be valid JSON. 
- Avoid any unescaped special characters (like double quotes or backslashes) inside string values.
- Double check that the "markdown_report" string is correctly escaped.

**REQUIRED OUTPUT JSON KEYS**:
1. "markdown_report": Full Markdown report text (DoD table, Risks, Tests, Verdict).
2. "verified_findings": JSON logic array [{{"path": "path", "line": 123, "critique": "text", "surgical_fix": "code"}}]
3. "merge_verdict": 🟢 LGTM, 🟡 Needs Review, or 🔴 HARD STOP.

### ✅ Verification Verdict: DoD Check (Sorted: Passed first, then Failed)
| Requirement | Status | Reasoning/Note |
| :--- | :--- | :--- |
| [Checklist Item] | ✅ / ❌ / 🟠 | [Short reasoning] |

**IMPORTANT**: In the table above, YOU MUST SORT THE ROWS: list all **Passed (✅)** items first, followed by all **Failed (❌ or 🔴)** items.

### ⚠️ Technical Risks ({domain_name} Context)
{verifier_risks_prompt}

### 🧪 Suggested Test Cases
{verifier_tests_prompt}

### 🛑 Merge Verdict
(Exactly ONE: 🟢 **LGTM**, 🟡 **Needs Review**, 🔴 **HARD STOP**)
[1-sentence professional justification].

---

Diff: {diff}
Findings: {json.dumps(potential_issues, indent=2)}
Checklist: {checklist}
"""
            raw_verified_res = self.get_gemini_completion(verifier_prompt, is_json=True)
            # Note: get_gemini_completion now returns the parsed object directly if is_json=True
            v_data = raw_verified_res if isinstance(raw_verified_res, (dict, list)) else {}
            
            if not v_data:
                print("⚠️ Verifier returned invalid or empty JSON.")

            # 4. Prepare bundled comments
            bundled_comments = []
            verified_findings = v_data.get('verified_findings', [])
            if isinstance(verified_findings, list):
                for f in verified_findings:
                    if not isinstance(f, dict): continue
                    path, line = f.get('path'), f.get('line')
                    critique = f.get('critique', 'No critique.')
                    fix = f.get('surgical_fix', '// No fix.')
                    if path and line:
                        try:
                            body = f"**Critique**: {critique}\n\n**Surgical Fix**:\n```{lang_block}\n{fix}\n```"
                            bundled_comments.append({ "path": path, "line": int(line), "body": body })
                        except: continue

            # 5. Determine GitHub Event (Smart Mapping)
            verdict = v_data.get('merge_verdict', '🟡 Needs Review')
            github_event = "COMMENT"
            if "🔴" in verdict:
                github_event = "REQUEST_CHANGES"
            elif "🟢" in verdict and not bundled_comments:
                github_event = "APPROVE"

            # 6. Submit Review (with Fallback)
            author_mention = f"@{meta['author']}"
            header = f"{author_mention}\n> **Summary:** {pr_summary_text}\n\n"
            full_body = header + v_data.get('markdown_report', "⚠️ Analysis report malformed.")
            
            print(f"Attempting to submit review with {len(bundled_comments)} findings...")
            res = self.submit_bundled_review(full_body, github_event, bundled_comments)
            
            if not (res and res.status_code < 300):
                print("⚠️ Bundled review failed. Attempting fallback (Summary only)...")
                error_note = f"\n\n---\n⚠️ **Review Diagnostic Info**:\nSome inline findings were skipped because the GitHub API rejected them.\n**Error**: `{self.last_api_error}`"
                # Fallback to COMMENT always if the specific event failed, to ensure delivery
                res_fallback = self.submit_bundled_review(full_body + error_note, "COMMENT", [])
                if not (res_fallback and res_fallback.status_code < 300):
                    self.post_failure_comment(self.last_api_error or "Unknown API Error")
                    sys.exit(1)
            
            print(f"✅ {domain_name} Review submitted successfully.")

        except Exception as e:
            print(f"💥 Fatal error: {e}")
            self.post_failure_comment(str(e))
            sys.exit(1)

if __name__ == "__main__":
    PRSummarizer().run()
