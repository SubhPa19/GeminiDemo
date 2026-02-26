import os
import requests
import sys

repo = os.getenv("REPO")
pr_number = os.getenv("PR_NUMBER")
github_token = os.getenv("GITHUB_TOKEN")
gemini_api_key = os.getenv("GEMINI_API_KEY")

if not all([repo, pr_number, github_token, gemini_api_key]):
    print("Missing required environment variables.")
    sys.exit(1)

# --- Fetch PR Metadata to get Author and Reviewers ---
api_headers = {
    "Authorization": f"Bearer {github_token}",
    "Accept": "application/vnd.github.v3+json"
}
pr_metadata_url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
pr_meta = requests.get(pr_metadata_url, headers=api_headers).json()

# Safely extract the author's username
pr_author = pr_meta.get("user", {}).get("login", "Developer")

# Safely extract any requested reviewers
requested_reviewers = [rev['login'] for rev in pr_meta.get("requested_reviewers", [])]

# Format the tags for the comment
mentions = f"@{pr_author}"
if requested_reviewers:
    mentions += "\n*CC Reviewers:* " + " ".join([f"@{rev}" for rev in requested_reviewers])
# -----------------------------------------------------------

# Fetch the PR Diff
diff_headers = {
    "Authorization": f"Bearer {github_token}",
    "Accept": "application/vnd.github.v3.diff"
}
diff = requests.get(pr_metadata_url, headers=diff_headers).text

if len(diff) > 50000:
    diff = diff[:50000] + "\n\n...[Diff truncated due to length]..."

# --- THE PROFESSIONAL PROMPT ---
prompt = f"""
You are an expert, professional Android Developer and Senior Kotlin code reviewer. Analyze the following GitHub Pull Request diff and provide a highly accurate, objective, and constructive review formatted exactly with these headings. 

### 📝 Summary
(Provide a concise 2-3 line objective summary of the PR's purpose and changes.)

### 🔑 Key Changes
(Provide bullet points of the most important technical changes.)

### 🤖 Android & Kotlin Feedback
(Critique the code constructively for Android-specific best practices. Point out:
- Kotlin optimizations (e.g., scoping functions, null safety, idiomatic usage)
- Coroutine/Flow usage (e.g., proper Dispatchers, structured concurrency)
- Jetpack Compose performance (e.g., recomposition issues, state hoisting)
- Lifecycle, Context, or Memory management issues.
If the code is clean, state "Code adheres to Android and Kotlin best practices.")

### ⚠️ Risks
(Highlight potential risks such as unhandled exceptions, memory leaks, or main thread blocks. If none, state "No obvious risks detected.")

### 🧪 Suggested Test Cases
(Suggest specific scenarios to test, focusing on edge cases like device rotation, network states, and nullability.)

### 🛑 Merge Verdict
(Choose exactly ONE of the following verdicts based on your review, and add a brief, professional justification):
- 🟢 **LGTM (Looks Good To Merge)**: [Your professional justification]
- 🟡 **Needs Review (Revisions Recommended)**: [Your professional justification]
- 🔴 **HARD STOP (Do not merge until addressed)**: [Your professional justification]

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

# --- Appending mentions and a professional footer ---
bot_marker = ""
final_comment_body = f"{ai_summary}\n\n---\nHey {mentions}, your automated PR review is ready.\n\n{bot_marker}\n*⏳ Updated automatically based on the latest commits.*"

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
