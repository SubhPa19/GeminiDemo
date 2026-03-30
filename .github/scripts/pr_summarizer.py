import os
import json
import requests
import sys
import time
from concurrent.futures import ThreadPoolExecutor

class PRSummarizer:
    def __init__(self):
        self.repo = os.getenv("REPO")
        self.pr_number = os.getenv("PR_NUMBER")
        self.github_token = os.getenv("GITHUB_TOKEN")
        self.gemini_api_key = os.getenv("GEMINI_API_KEY")
        self.checklist_path = os.getenv("CHECKLIST_PATH", ".github/checklist.md")
        self.model_name = "gemini-1.5-flash"
        self.bot_marker = "" # Hidden marker for finding existing comments
        
        if not all([self.repo, self.pr_number, self.github_token, self.gemini_api_key]):
            print("❌ Missing required environment variables.")
            sys.exit(1)

        self.api_headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github.v3+json"
        }
        self.base_url = f"https://api.github.com/repos/{self.repo}"
        self.gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_name}:generateContent?key={self.gemini_api_key}"

    def _safe_request(self, method, url, **kwargs):
        """Generic request wrapper with basic retry logic."""
        for attempt in range(3):
            try:
                res = requests.request(method, url, **kwargs)
                res.raise_for_status()
                return res
            except Exception as e:
                print(f"⚠️ Attempt {attempt+1} failed for {url}: {e}")
                time.sleep(2 ** attempt)
        return None

    def fetch_pr_metadata(self):
        print("Fetching PR metadata...")
        url = f"{self.base_url}/pulls/{self.pr_number}"
        res = self._safe_request("GET", url, headers=self.api_headers)
        if not res: return None
        
        data = res.json()
        return {
            "author": data.get("user", {}).get("login", "Developer"),
            "reviewers": [rev['login'] for rev in data.get("requested_reviewers", [])],
            "base_branch": data.get("base", {}).get("ref", "main"),
            "title": data.get("title", "this PR"),
            "diff_url": url
        }

    def fetch_diff(self, url):
        print("Fetching PR diff...")
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
        return res.text if res and res.status_code == 200 else "General industry best practices applied."

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

    def get_existing_comment_id(self):
        url = f"{self.base_url}/issues/{self.pr_number}/comments"
        res = self._safe_request("GET", url, headers=self.api_headers)
        if res:
            for comment in res.json():
                if self.bot_marker in comment.get("body", ""):
                    return comment["id"]
        return None

    def update_comment(self, comment_id, body):
        url = f"{self.base_url}/issues/comments/{comment_id}" if comment_id else f"{self.base_url}/issues/{self.pr_number}/comments"
        method = "PATCH" if comment_id else "POST"
        return self._safe_request(method, url, headers=self.api_headers, json={"body": body})

    def run(self):
        # 1. Fetch Metadata and Diff
        meta = self.fetch_pr_metadata()
        if not meta: return
        
        diff = self.fetch_diff(meta['diff_url'])
        checklist = self.fetch_checklist(meta['base_branch'])
        
        # 2. Concurrency: Run Summary and Hunter passes in parallel
        print("🚀 Starting parallel analysis passes...")
        with ThreadPoolExecutor(max_workers=2) as executor:
            summary_future = executor.submit(self.get_gemini_completion, 
                f"Summarize in one short sentence what this Pull Request, titled '{meta['title']}', is attempting to achieve.")
            
            hunter_prompt = f"Analyze the following Git Diff for Android bugs/leaks. Output a JSON array of objects with 'path', 'line', and 'finding'.\n\n{diff}"
            hunter_future = executor.submit(self.get_gemini_completion, hunter_prompt, is_json=True)

        pr_summary = summary_future.result() or meta['title']
        raw_issues = hunter_future.result() or "[]"
        
        try:
            potential_issues = json.loads(raw_issues)
        except:
            potential_issues = []

        # 3. Post "Processing" comment
        mentions = f"Hey @{meta['author']}" + (f" and CC Reviewers: " + " ".join([f"@{r}" for r in meta['reviewers']]) if meta['reviewers'] else "")
        initial_body = f"> **Summary:** {pr_summary}\n\n{mentions}, our agent is initiating a multi-pass technical analysis. Please wait.\n\n*⏳ Verification in progress.*"
        
        comment_id = self.get_existing_comment_id()
        self.update_comment(comment_id, initial_body)
        
        # Give GitHub a moment to register the new comment if we just posted it
        if not comment_id:
            time.sleep(1)
            comment_id = self.get_existing_comment_id()

        # 4. Verifier Pass (Serial, depends on Hunter output)
        print("🛡️ Starting verification pass...")
        verifier_prompt = f"""
        You are a Lead Android Developer. Verify these potential issues against the Diff. 
        Discard hallucinations/formatting noise. Format as Markdown.
        
        ### ✅ Verification Verdict: Passed DoD
        {checklist}
        
        ### 🤖 Verified Technical Feedback & Solutions
        (List high-severity issues with code solutions)
        
        ### ⚠️风险 (Risks)
        ### 🛑 Merge Verdict (🟢 LGTM, 🟡 Needs Review, 🔴 HARD STOP)
        
        Diff:
        {diff}
        
        Hunter Findings:
        {json.dumps(potential_issues, indent=2)}
        """
        final_report = self.get_gemini_completion(verifier_prompt) or "Verification failed to complete."

        # 5. Final Update
        final_body = (
            f"> **Summary:** {pr_summary}\n\n"
            f"{final_report}\n\n"
            f"---\n{mentions}, your automated PR Agent has completed a rigorous multi-pass verification.\n"
            f"{self.bot_marker}\n"
            f"*⏳ Reluctantly updated automatically.*"
        )
        self.update_comment(comment_id, final_body)
        print("✅ Analysis complete and comment updated.")

if __name__ == "__main__":
    PRSummarizer().run()
