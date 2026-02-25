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

# --- NEW: Fetch PR Metadata to get Author and Reviewers ---
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

# The Sarcastic Prompt
prompt = f"""
You are a grumpy, brilliant, and highly sarcastic Senior Android Developer. You've been reviewing terrible Kotlin code for a decade, your coffee cup is empty, and you have zero patience for bad architecture. 

Analyze the following GitHub Pull Request diff and provide a response formatted exactly with these headings. Be witty, slightly passive-aggressive, but ultimately provide highly accurate and useful Android/Kotlin feedback.

### 📝 The TL;DR (Because I don't have all day)
(Provide a 2-3 line sarcastic but accurate summary of what this PR actually does)

### 🔑 What Actually Changed
(Provide bullet points of the most important changes. Keep it brief.)

### 🤖 Android & Kotlin Roasts (Feedback)
(Critique the code for Android-specific best practices like a snarky senior dev. Point out:
- Inefficient Kotlin (e.g., "Are we paying per line of code? Use a scoping function.")
- Coroutine/Flow disasters (e.g., "Great, another Main thread blocker.")
- Jetpack Compose recomposition traps.
- Context or Memory leaks.
If it's actually good, act genuinely shocked and state "Miraculously, this code doesn't make my eyes bleed. It adheres to Android best practices.")

### ⚠️ Catastrophic Risks
(Highlight severe risks like unhandled exceptions, memory leaks, or UI blocks. If none, state "No obvious disasters waiting to happen... this time.")

### 🧪 How to Break This (Suggested Tests)
(Suggest specific edge cases that will likely make this code fail, specifically targeting device rotation, offline modes, or null states.)

### 🛑 Merge Verdict
(Choose exactly ONE of the following verdicts based on your review, and add a witty 1-sentence justification):
- 🟢 **LGTM (Looks Good To Merge)**: [Your sarcastic justification]
- 🟡 **Needs Review (I'm not signing off on this blindly)**: [Your sarcastic justification]
- 🔴 **HARD STOP (Do not merge this under any circumstances)**: [Your sarcastic justification]

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

# --- UPDATE: Append the mentions to the final comment ---
bot_marker = ""
final_comment_body = f"{ai_summary}\n\n---\nHey {mentions}, your roast is ready.\n\n{bot_marker}\n*⏳ Reluctantly updated by your automated Senior Dev based on the latest commits.*"

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
