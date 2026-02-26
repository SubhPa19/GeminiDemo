# 🤖 Automated Senior Dev: AI PR Reviewer

> **Status:** Fully functional prototype. Awaiting Enterprise Service Account API Key for production deployment.

## 🎯 The Problem
Pull Requests are often merged blindly because reviewers lack context, or they sit in limbo for days waiting for a Senior Developer to find the time to review them. Standard linting tools catch syntax errors, but they don't catch bad architecture, memory leaks, or logical flaws.

## 💡 The Solution
This workflow leverages Google's Gemini LLM to act as an automated, highly-opinionated Senior Android/Kotlin Developer. 

Triggered instantly via GitHub Actions whenever a PR is opened or updated, it reads the diff and provides immediate, actionable feedback before a human reviewer even looks at it.

## ✨ Key Features
* **⏱️ Instant TL;DR:** Summarizes exactly what the PR does in 2-3 lines so reviewers don't have to guess.
* **📱 Android/Kotlin Specific:** Explicitly hunts for common mobile pitfalls (Context leaks, Main thread blocking, Coroutine misuse, inefficient Jetpack Compose recompositions).
* **⚠️ Risk Assessment:** Flags catastrophic risks and suggests specific edge-case test scenarios (e.g., device rotation, offline modes).
* **🛑 Automated Merge Verdict:** Gives a clear "LGTM", "Needs Review", or "HARD STOP" based on code quality.
* **🔔 Smart Notifications:** Automatically parses GitHub metadata to `@mention` the PR author and requested reviewers so they get emailed instantly.
* **🧹 Thread Management:** Silently updates its existing comment on new commits rather than spamming the PR thread.

## ⚙️ How it Works (Architecture)
1. Developer pushes a commit to a Pull Request.
2. GitHub Actions (`synchronize` / `opened` event) spins up an Ubuntu runner.
3. A lightweight Python script extracts the Git Diff and PR Metadata.
4. The diff is sent as a stateless request to the Gemini API with a strict, role-prompted context.
5. The AI responds, and the script patches/posts the formatted comment back to GitHub.

## 💼 Business ROI (Why we need this in Production)
* **Saves Senior Dev Time:** Catches obvious architectural mistakes so Senior Devs only have to review the complex business logic.
* **Faster PR Turnaround:** Authors get immediate feedback (within seconds) instead of waiting hours/days for a human.
* **Cost-Efficient:** The API cost per PR analysis is fractions of a cent, saving thousands of dollars in developer hourly rates and preventing expensive production crashes.
* **Stateless & Secure:** No code is stored by the workflow. It uses standard GitHub Tokens and a secure Service Account API key.
