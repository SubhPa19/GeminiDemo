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
        self.github_token = os.getenv("GITHUB_TOKEN")
        self.gemini_api_key = os.getenv("ANOTHER_API_KEY") or os.getenv("GEMINI_API_KEY")
        self.checklist_path = os.getenv("CHECKLIST_PATH", ".github/checklist.md")
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.bot_marker = "<!-- gemini-bot-review -->" 
        
        if not all([self.repo, self.pr_number, self.github_token, self.gemini_api_key]):
            print("❌ Missing required environment variables (REPO, PR_NUMBER, GITHUB_TOKEN, etc.).")
            sys.exit(1)

        self.api_headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        self.base_url = f"https://api.github.com/repos/{self.repo}"
        self.gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent?key={self.gemini_api_key}"

    def _safe_request(self, method, url, **kwargs):
        """Generic request wrapper with optimized 429 (Rate Limit) handling."""
        for attempt in range(5):
            try:
                res = requests.request(method, url, **kwargs)
                if res.status_code == 429:
                    print(f"🛑 Rate limit hit (429). Attempt {attempt+1}. Waiting 10 seconds...")
                    time.sleep(10)
                    continue
                res.raise_for_status()
                return res
            except Exception as e:
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
            "reviewers": [rev['login'] for rev in data.get("requested_reviewers", [])],
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
        return "General industry best practices applied."

    def get_gemini_completion(self, prompt, is_json=False):
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        if is_json:
            payload["generationConfig"] = {"response_mime_type": "application/json"}
        
        res = self._safe_request("POST", self.gemini_url, json=payload)
        if not res: return None
        
        try:
            return res.json()['candidates'][0]['content']['parts'][0]['text']
        except (KeyError, IndexError):
            return None

    def submit_bundled_review(self, body, event, comments):
        """Submits all findings in a single Bundled Review to minimize noise."""
        print(f"📦 Submitting bundled review ({len(comments)} inline findings)...")
        url = f"{self.base_url}/pulls/{self.pr_number}/reviews"
        payload = {
            "body": body + f"\n\n{self.bot_marker}",
            "event": event,
            "comments": comments
        }
        return self._safe_request("POST", url, headers=self.api_headers, json=payload)

    def run(self):
        # 1. Fetch Metadata and Diff
        meta = self.fetch_pr_data()
        if not meta: return
        
        diff = self.fetch_diff(meta['diff_url'])
        checklist = self.fetch_checklist(meta['base_branch'])

        # 2. Concurrency: Run Summary and Hunter passes in parallel
        print("🚀 Starting optimized analysis passes...")
        with ThreadPoolExecutor(max_workers=2) as executor:
            summary_prompt = f"Summarize in one short sentence what this PR titled '{meta['title']}' is attempting to achieve. Title: {meta['title']}, Description: {meta['description']}"
            summary_future = executor.submit(self.get_gemini_completion, summary_prompt)
            
            hunter_prompt = f"Analyze this Git Diff for Android bugs/leaks. Output a JSON array of objects with 'path', 'line', and 'finding'.\n\n{diff}"
            hunter_future = executor.submit(self.get_gemini_completion, hunter_prompt, is_json=True)

            pr_summary = summary_future.result() or meta['title']
            raw_issues = hunter_future.result() or "[]"
        
        try:
            potential_issues = json.loads(raw_issues)
        except:
            potential_issues = []

        # 3. VERIFIER PASS: Hybrid JSON return for Summary + Inline
        print("🛡️ Starting verification pass...")
        verifier_prompt = f"""
You are the Lead Android Developer. Verify findings and generate a dual JSON report.

**REQUIRED OUTPUT JSON KEYS**:
1. "markdown_report": Full Markdown report text (DoD table, Severity-coded bugs, Risks).
2. "verified_findings": JSON logic array [{{"path": "path", "line": 123, "critique": "text", "surgical_fix": "code"}}]
3. "merge_verdict": 🟢 LGTM, 🟡 Needs Review, or 🔴 HARD STOP.

### DoD Requirements:
{checklist}

### Style for "markdown_report":
Use 🔴 **CRITICAL**, 🟡 **WARNING**, 🔵 **OPTIMIZATION** for findings.
Include the Merge Verdict at the bottom.

Diff: {diff}
Findings: {json.dumps(potential_issues, indent=2)}
"""
        raw_verified_res = self.get_gemini_completion(verifier_prompt, is_json=True)
        try:
            v_data = json.loads(raw_verified_res)
        except:
            v_data = {"markdown_report": "⚠️ Analysis error.", "merge_verdict": "🟡 Needs Review", "verified_findings": []}

        # 4. Map Verdict to GitHub event
        verdict = v_data.get('merge_verdict', '🟡 Needs Review')
        github_event = "COMMENT"
        if "🔴" in verdict: github_event = "REQUEST_CHANGES"
        elif "🟢" in verdict and not v_data.get('verified_findings'): github_event = "APPROVE"

        # 5. Prepare bundled comments
        bundled_comments = []
        for f in v_data.get('verified_findings', []):
            body = f"**Critique**: {f.get('critique')}\n\n**Surgical Fix**:\n```kotlin\n{f.get('surgical_fix')}\n```"
            bundled_comments.append({
                "path": f.get('path'),
                "line": int(f.get('line')),
                "body": body
            })

        # 6. Submit ONE single review
        header = f"> **Summary:** {pr_summary}\n\n"
        self.submit_bundled_review(header + v_data.get('markdown_report'), github_event, bundled_comments)
        
        print("✅ Review submitted successfully. Exactly ONE notification sent.")

if __name__ == "__main__":
    PRSummarizer().run()
