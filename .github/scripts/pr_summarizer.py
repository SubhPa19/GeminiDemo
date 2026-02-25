import os
import requests
import sys

# Get environment variables from GitHub Actions
repo = os.getenv("REPO")
pr_number = os.getenv("PR_NUMBER")
github_token = os.getenv("GITHUB_TOKEN")
gemini_api_key = os.getenv("GEMINI_API_KEY")

if not all([repo, pr_number, github_token, gemini_api_key]):
    print("Missing required environment variables.")
    sys.exit(1)

# 1. Fetch the PR Diff from GitHub
headers = {
    "Authorization": f"Bearer {github_token}",
    "Accept": "application/vnd.github.v3.diff"
}
diff_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
response = requests.get(diff_url, headers=headers)
diff = response.text

if len(diff) > 50000:
    diff = diff[:50000] + "\n\n...[Diff truncated due to length]..."

# 2. Call the AI API
prompt = f"""
You are an expert software engineer and code reviewer. Analyze the following GitHub Pull Request diff and provide a response formatted exactly with these headings:

### 📝 Summary
(Provide a 2-3 line summary of the PR)

### 🔑 Key Changes
(Provide bullet points of the most important changes)

### ⚠️ Risks
(Highlight any potential risks, breaking changes, or security vulnerabilities. If none, state "No obvious risks detected.")

### 🧪 Suggested Test Cases
(Suggest specific test cases that should be executed to validate these changes)

Here is the diff:
{diff}
"""

gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_api_key}"
payload = {"contents": [{"parts": [{"text": prompt}]}]}

try:
    res = requests.post(gemini_url, json=payload).json()
    ai_summary = res['candidates'][0]['content']['parts'][0]['text']
except Exception as e:
    print(f"Failed to generate summary: {e}")
    sys.exit(1)

# --- NEW LOGIC: Check for existing comment and update/post ---

# Define a hidden marker to identify the bot's comment
bot_marker = ""
final_comment_body = f"{ai_summary}\n\n{bot_marker}\n*⏳ Updated automatically based on the latest commits.*"

comments_url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
comment_headers = {
    "Authorization": f"Bearer {github_token}",
    "Accept": "application/vnd.github.v3+json"
}

# 3. Search for an existing comment by the bot
comments_response = requests.get(comments_url, headers=comment_headers)
existing_comment_id = None

if comments_response.status_code == 200:
    for comment in comments_response.json():
        # Look for our hidden marker in the comment body
        if bot_marker in comment.get("body", ""):
            existing_comment_id = comment["id"]
            break

# 4. Update if exists, otherwise create new
if existing_comment_id:
    update_url = f"https://api.github.com/repos/{repo}/issues/comments/{existing_comment_id}"
    requests.patch(update_url, headers=comment_headers, json={"body": final_comment_body})
    print(f"Successfully updated existing PR summary (Comment ID: {existing_comment_id})!")
else:
    requests.post(comments_url, headers=comment_headers, json={"body": final_comment_body})
    print("Successfully posted new PR summary!")
