import os
import sys
import requests

def main():
    repo = os.getenv("REPO")
    pr_number = os.getenv("PR_NUMBER")
    token = os.getenv("GITHUB_TOKEN") or os.getenv("TOKEN_GH")
    
    if not all([repo, pr_number, token]):
        print("❌ Missing required environment variables (REPO, PR_NUMBER, GITHUB_TOKEN).")
        sys.exit(1)
        
    github_api_base = os.getenv("GITHUB_API_URL") or "https://api.github.com"
    url = f"{github_api_base.rstrip('/')}/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    # Check if we already posted the deprecation comment to avoid spam
    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            for comment in res.json():
                if "The GRACe PR bot has been migrated" in comment.get("body", ""):
                    print("Deprecation comment already exists.")
                    return
    except Exception as e:
        print(f"Warning: Failed to fetch existing comments: {e}")

    body = "The GRACe PR bot has been migrated to a central repository. Please contact the maintainer of the Grace PR bot to get this resolved."
    
    res = requests.post(url, headers=headers, json={"body": body})
    if res.status_code == 201:
        print("✅ Successfully posted deprecation notice.")
    else:
        print(f"❌ Failed to post comment: {res.status_code} {res.text}")
        sys.exit(1)

if __name__ == "__main__":
    main()
