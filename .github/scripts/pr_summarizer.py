import os
import json
import requests
import sys
import re

# Get environment variables from GitHub Actions
repo = os.getenv("REPO")
pr_number = os.getenv("PR_NUMBER")
github_token = os.getenv("GITHUB_TOKEN")
gemini_api_key = os.getenv("GEMINI_API_KEY")
checklist_path = os.getenv("CHECKLIST_PATH", ".github/checklist.md") 

if not all([repo, pr_number, github_token, gemini_api_key]):
    print("Missing required environment variables.")
    sys.exit(1)

# API Headers
api_headers = {
    "Authorization": f"Bearer {github_token}",
    "Accept": "application/vnd.github.v3+json"
}
pr_metadata_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"

# Fetch PR Metadata
pr_meta = requests.get(pr_metadata_url, headers=api_headers).json()
pr_author = pr_meta.get("user", {}).get("login", "Developer")
requested_reviewers = [rev['login'] for rev in pr_meta.get("requested_reviewers", [])]
base_branch = pr_meta.get("base", {}).get("ref", "main")

# Fetch the Team Checklist (Definition of Done)
checklist_url = f"https://api.github.com/repos/{repo}/contents/{checklist_path}?ref={base_branch}"
checklist_content = ""
try:
    checklist_raw_response = requests.get(checklist_url, headers={"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github.v3.raw"})
    if checklist_raw_response.status_code == 200:
        checklist_content = checklist_raw_response.text
        print(f"Successfully loaded checklist from {checklist_path}")
    else:
        print(f"Checklist not found at {checklist_path}. Using general best practices.")
except Exception as e:
    print(f"Error fetching checklist: {e}. Proceeding without it.")

# Fetch the PR Diff
diff_headers = {
    "Authorization": f"Bearer {github_token}",
    "Accept": "application/vnd.github.v3.diff"
}
diff = requests.get(pr_metadata_url, headers=diff_headers).text

# Truncate if diff is too large.
if len(diff) > 60000:
    diff = diff[:60000] + "\n\n......"

gemini_base_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_api_key}"

# --- MULTI-PASS VERIFICATION FLOW ---

# 1. Get quick context summary based only on title
pr_title = pr_meta.get("title", "this PR")
quick_summary_prompt = f"""Summarize in one short sentence what this Pull Request, titled '{pr_title}', is attempting to achieve based on its description."""
pr_summary_text = pr_title 
try:
    res_quick_summary = requests.post(gemini_base_url, json={"contents": [{"parts": [{"text": quick_summary_prompt}]}]}).json()
    pr_summary_text = res_quick_summary['candidates'][0]['content']['parts'][0]['text'].strip()
except Exception as e:
    print(f"Failed to get quick summary: {e}")

# 2. Setup the single comment identity
mentions_footer_base = f"Hey @{pr_author}" + (f" and CC Reviewers: " + " ".join([f"@{rev}" for rev in requested_reviewers]) if requested_reviewers else "")
bot_marker = ""
comments_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
comments_response = requests.get(comments_url, headers=api_headers)
existing_agent_comment_id = None

if comments_response.status_code == 200:
    for comment in comments_response.json():
        if bot_marker in comment.get("body", ""):
            existing_agent_comment_id = comment["id"]
            break

# Update initial posting to reflect immediate summary.
final_comment_body = f"> **Summary:** {pr_summary_text}\n\n{mentions_footer_base}, our agent is initiating a rigorous multi-pass technical analysis of your PR changes. Please wait.\n\n{bot_marker}\n*⏳ Analysis in progress.*"
if existing_agent_comment_id:
    update_url = f"https://api.github.com/repos/{repo}/issues/comments/{existing_agent_comment_id}"
    requests.patch(update_url, headers=api_headers, json={"body": final_comment_body})
else:
    # Post it new and capture the ID.
    post_res = requests.post(comments_url, headers=api_headers, json={"body": final_comment_body})
    if post_res.status_code == 201:
        existing_agent_comment_id = post_res.json()['id']

# 3. PASS 1: The "Hypersensitive Bug Hunter" Agent.
hunter_prompt = f"""
You are an expert Android Developer specializing in static code analysis and memory leak detection. Your role is a "Hypersensitive Bug Hunter." 

Analyze the following Git Diff and identify ANY and ALL potential violations, inefficient code patterns, security risks, memory leak hazards, or issues regarding null safety.

 Output your findings as a raw JSON array of objects, with each object containing:
- **`filename`**: File Name .
- **`line`**: The line number the issue applies to. Use the line number from the "new" side (the + lines).
- **`finding`**: A concise technical description of the potential issue.

If no potential issues are found, return an empty array [].

Output **ONLY** the raw JSON array.

[Git Diff Context]
{diff}
"""

try:
    res_hunter = requests.post(gemini_base_url, json={"contents": [{"parts": [{"text": hunter_prompt}]}]}).json()
    raw_hunter_json_text = res_hunter['candidates'][0]['content']['parts'][0]['text']
    clean_json_hunter = ""
    json_match = re.search(r'\[.*\]', raw_hunter_json_text, re.DOTALL)
    if json_match:
        clean_json_hunter = json_match.group(0)
    else:
        clean_json_hunter = raw_hunter_json_text.strip()
    potential_issues = json.loads(clean_json_hunter)
    print(f"Finder Agent found {len(potential_issues)} potential issues.")
except (Exception, json.JSONDecodeError) as e:
    print(f"Failed to parse hunter JSON: {e}")
    potential_issues = []

# --- PASS 2: NEW "CRISP AND CONCISE" VERIFIER PROMPT ---
verifier_prompt = f"""
You are the cynical and highly experienced Lead Android Developer at a large enterprise. You are reviewing a list of potential issues in a Git Diff found by a junior AI agent.

Your role is to act as a **Verifier and Filter**. Analyze each potential issue against the actual Git Diff. Discard any findings that are False Positives, Pedantic/Trivial, or Inaccurate.

For the high-signal findings that you verify, keep them and format them constructively as a final objective report.


1.  **NO DUMPING WHOLE CLASSES OR LARGE FUNCTIONS.** When providing refactor solutions, output only the specific lines changed, showing a surgical fix. Do not include surrounding context if it is not necessary. Focus only on the line being roasted.
2.  **BE CRISP & CONCISE.** Limit your technical critiques to **ONE OR TWO PUNCHY SENTENCES MAX**. Get to the point quickly.
3.  **DO NOT Waffle or Explain basic concepts.** Assume the team knows Android.

Output your final verification report in this exact Markdown format. Do not use JSON.

### ✅ Verification Verdict: DoD Check
(List each Team Checklist item below. Provide an objective grading: ✅ **Adherence**, or ❌ **]** bold the failure.)


{checklist_content if checklist_content else "No specific checklist provided."}

### 🤖 Verified Technical Feedback & Solutions
(List only high-severity, technical, and accurate issues you verified. Format as dynamic list.)
* **]**:
    ```kotlin
   
    ```

### ⚠️ (Risks)
(List severe technical risks only.)

### 🛑 Merge Verdict
(Choose exactly ONE: 🟢 **LGTM**, 🟡 **Needs Review**, 🔴 **HARD STOP**, and provide a 1-sentence profesional justification.)

Here is the Git Diff:
{diff}

And here are the potential issues reported by the Hunter Agent:

{json.dumps(potential_issues, indent=2)}
"""

# Call Gemini for the final verification report.
try:
    res_verified = requests.post(gemini_base_url, json={"contents": [{"parts": [{"text": verifier_prompt}]}]}).json()
    final_verified_report_text = res_verified['candidates'][0]['content']['parts'][0]['text']
except Exception as e:
    print(f"Failed to generate verification report: {e}")
    final_verified_report_text = f"### Error during verification analysis.\n\nFailed to complete the second pass of technical analysis."

# 4. Final Stage: Build the full footer and update the main comment.
footer_prefix = f"\n---\nHey {mentions_footer_base}"
final_closing = f"{footer_prefix}, your automated PR Agent has completed a rigorous multi-pass verification to ensure high-signal, crisp findings. Your roast is ready."

final_summary_body = f"> **Summary:** {pr_summary_text}\n\n{final_verified_report_text}\n\n{final_closing}\n{bot_marker}\n*⏳ Reluctantly updated automatically by your automated Senior Dev.*"

if existing_agent_comment_id:
    update_url = f"https://api.github.com/repos/{repo}/issues/comments/{existing_agent_comment_id}"
    requests.patch(update_url, headers=api_headers, json={"body": final_summary_body})
    print("Main summary comment updated with crisp verified roast.")
