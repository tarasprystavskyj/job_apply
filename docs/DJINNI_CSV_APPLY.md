# Djinni CSV Apply Script

Script:

```powershell
python src\djinni_csv_apply.py --csv data\job_waves\djinni_apply_sample.csv
```

Default mode is dry-run. It validates rows and writes a JSONL attempt log, but
does not click submit.

Real submit requires:

```powershell
python src\djinni_csv_apply.py --csv path\to\approved.csv --execute --i-understand-this-submits-applications
```

Required CSV columns:

- `site`: must be `djinni`
- `url`: `https://djinni.co/jobs/...`
- `message`: exact cover letter text, or leave empty and use `message_file`
- `message_file`: optional path inside this workspace
- `salary_usd`: numeric salary expectation
- `linkedin`: approved LinkedIn profile URL
- `resume_policy`: `no_resume` or `use_selected_resume`
- `approved_resume_name`: required only for `use_selected_resume`
- `approved_to_submit`: must be `true` for execute mode
- `final_submit_allowed`: must be `true` for execute mode

Optional columns:

- `title`
- `company`
- `answer_1` through `answer_8`: optional recruiter questionnaire answers.
  When present, these are copied to visible Djinni textareas with IDs
  `answer_text_1` through `answer_text_8`.

The script attaches to an already-running Chrome on `http://127.0.0.1:9222`.
It opens each Djinni URL, clicks the visible apply button, fills the visible
form, and clicks submit only when both the row and CLI flags explicitly allow
submission.

Before submit in execute mode, the script verifies that required data reached
the form fields: message, salary, LinkedIn where required, selected resume
policy, and all required visible recruiter answer textareas. If a required
recruiter answer textarea is empty, or a CSV-provided `answer_1` through
`answer_8` value was not copied into its matching visible textarea, the script
blocks before clicking submit.

It does not read cookies, browser profile files, passwords, or local CV files.
It does not upload resumes. If `resume_policy=no_resume`, it selects "Не
показувати резюме"; if `resume_policy=use_selected_resume`, it verifies that
the currently selected resume text contains `approved_resume_name`.
