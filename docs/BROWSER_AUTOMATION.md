# Safe Chrome Browser Automation

This project can reuse the Chrome MCP pattern from
`C:\python_scripts\chrome\workers_automate`, but job applications have stricter
gates than ChatGPT worker orchestration.

## What This Automation May Do

- Build a reviewed vacancy plan.
- Open public vacancy URLs in an owner-visible Chrome session.
- Capture accessibility snapshots for local review.
- Stop when apply/send/submit language is visible.

## What It Must Not Do By Default

- Click apply/send/submit.
- Upload a resume.
- Type or transmit phone, email, CV text, or cover-letter text into a site.
- Read Chrome profile files, cookies, passwords, or saved sessions directly.
- Bypass login, CAPTCHA, paywall, rate limit, or anti-bot controls.

## Commands

Build a 5-vacancy browser prep plan:

```powershell
python src\job_site_browser_prep.py build-plan
```

Open reviewed vacancies in an existing Chrome debug session and capture
snapshots:

```powershell
python src\job_site_browser_prep.py open-tabs --i-understand-no-submit
```

If MCP navigation is slow or unavailable, open the reviewed URLs through
Chrome's local CDP HTTP endpoint:

```powershell
python src\job_site_browser_prep.py open-tabs-cdp --i-understand-no-submit
```

The CDP HTTP flow only opens tabs and writes a local JSONL log. It has no
click, type, upload, or submit implementation.

If the owner is already logged in in the main Chrome profile and that window is
not CDP-controlled, open URLs through Chrome's normal single-instance routing:

```powershell
python src\job_site_browser_prep.py open-tabs-main-chrome --i-understand-no-submit
```

This mode is deliberately weaker automation: it opens URLs only. It cannot read
cookies, inspect profile files, type into forms, upload resumes, or click final
buttons.

Chrome must already be running with remote debugging on port `9222`. The
reference launcher is:

```powershell
C:\python_scripts\chrome\workers_automate\launch_chrome.ps1
```

Because that launcher uses the real Chrome profile, the automation treats Chrome
as an owner-visible browser only. It must not inspect profile files or extract
cookies.

## Submission Gate

A later `prepare-one` flow may fill approved fields, but only after owner
approval naming the site, URL, resume, exact cover letter, and whether final
submit is allowed. The current `open-tabs` flow has no submit or upload path.
