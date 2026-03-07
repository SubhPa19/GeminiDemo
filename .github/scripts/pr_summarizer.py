import os
import requests
import sys

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

# Format mentions for the footer
mentions = f"@{pr_author}"
if requested_reviewers:
    mentions += "\n*CC Reviewers:* " + " ".join([f"@{rev}" for rev in requested_reviewers])

# Feature 1: Fetch the Team Checklist (Definition of Done)
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

if len(diff) > 50000:
    diff = diff[:50000] + "\n\n...[Diff truncated due to length]..."

# --- UPDATED PROMPT: Requesting specific emoji/bold formatting ---
prompt = f"""
You are an expert, professional Android Developer and Senior Kotlin code reviewer. Analyze the following GitHub Pull Request diff and provide a highly accurate, objective, and constructive review formatted exactly with these headings. 

### 📝 Summary
(Provide a concise 2-3 line objective summary of the PR's purpose and changes.)

### ✅ Definition of Done Check (DoD)
(Critically compare the changes in the diff against the provided Team Checklist items below. Provide an objective grading for each point. **You must adhere strictly to the following formatting:**

- If an item adheres to the checklist: Start the line with the ✅ emoji. (e.g., "✅ **Item 1:** Verified unit tests are included.")
- **If an item is violated or missing:** Start the line with the ❌ emoji and ****. (e.g., "❌ **]** No KDoc comments found for new public functions.")

If no specific checklist content is provided, state "General industry best practices applied.")

[Team Checklist Context Start]
{checklist_content if checklist_content else "No specific checklist provided."}
[Team Checklist Context End]

### 🤖 Android & Kotlin Feedback & Suggestions
(Critique the code objectively for Android-specific best practices, focusing on performance, Kotlin idiomatic usage, and memory management. If you identify inefficient code that can be improved, provide the critique and then **provide the actual corrected code block in Markdown format so the developer can copy-paste it directly**.)

### ⚠️ Risks
(Highlight potential risks such as unhandled exceptions, memory leaks, or main thread blocks.)

### 🛑 Merge Verdict
(Choose exactly ONE: 🟢 **LGTM (Looks Good To Merge)**, 🟡 **Needs Review (Revisions Recommended)**, or 🔴 **HARD STOP (Do not merge until addressed)**, and add a brief professional justification based on findings.)

Here is the diff:
{diff}
"""
# ------------------------------------------------------------------

# Call Gemini API
gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_api_key}"
payload = {"contents": [{"parts": [{"text": prompt}]}]}

try:
    res = requests.post(gemini_url, json=payload).json()
    ai_summary = res['candidates'][0]['content']['parts'][0]['text']
except Exception as e:
    print(f"Failed to generate summary: {e}")
    sys.exit(1)

# Appending mentions and professional footer
bot_marker = ""
final_comment_body = f"{ai_summary}\n\n---\nHey {mentions}, your automated PR review is ready.\n\n{bot_marker}\n*⏳ Updated automatically based on the latest commits.*"

# Post or Update Comment
comments_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
comment_headers = {
    "Authorization": f"Bearer {github_token}",
    "Accept": "application/vnd.github.v3+json"
}

comments_response = requests.get(comments_url, headers=comment_headers)
existing_comment_id = None

if comments_response.status_code == 200:
    for comment in comments_response.json():
        if bot_marker in comment.get("body", ""):
            existing_comment_id = comment["id"]
            break

if existing_comment_id:
    update_url = f"https://api.github.com/repos/{repo}/issues/comments/{existing_comment_id}"
    requests.patch(update_url, headers=comment_headers, json={"body": final_comment_body})
else:
    requests.post(comments_url, headers=comment_headers, json={"body": final_comment_body})
