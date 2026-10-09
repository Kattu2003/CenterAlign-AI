# DEMO.md — demo video script (frame by frame)

Target length: about 7–8 minutes. Record **one scene at a time** and join the clips afterwards (Clipchamp, built into
Windows 11). Real runs take a few minutes each, so speed up the waiting parts (×3–×6) and add a small "sped up" caption.

Commands are for **PowerShell on Windows** (macOS/Linux: activate with `source .venv/bin/activate`, delete folders with
`rm -rf data runs`). Task, approval and question ids assume you start from the reset below; if anything else runs in
between, read the ids from the screen (`karya tasks`, `karya inbox`).

---

## Part A — Setup (once, before the first take)

**1. Update the code and restart the sandbox** (the update stops the extra console windows on Windows):

```powershell
cd C:\Users\aayus\CenterAlign
.venv\Scripts\Activate.ps1
karya sandbox down
```

If the three extra console windows from the old version are still open, close them now (that stops the old apps). Then:

```powershell
git pull
karya sandbox up
```

**2. Pick the browser Karya drives.** Best: Playwright's own Chromium (`playwright install chromium`, then delete the
`KARYA_CHROMIUM_PATH` line from `.env`). Next best: Google Chrome
(`KARYA_CHROMIUM_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe`). Edge works too, but a fresh Edge profile
opens Bing tabs on its own; they are not Karya's (Karya's tab is the one showing the company apps), so close them
before each take.

**3. `.env`** — the Gemini block, plus these lines:

```
KARYA_HEADLESS=0
KARYA_LLM_MAX_RPM=0
KARYA_BROWSER_WINDOW=640,0,1280,1040
```

`KARYA_BROWSER_WINDOW` (x, y, width, height) makes every Karya browser window open on the right two-thirds of the screen,
so it never covers the terminals. The example is for a 1920×1080 screen at 100% scale. At 125% scale (most laptops) use
`512,0,1024,860`; at 150% use `427,0,853,720`. (Settings → System → Display shows your resolution and scale.)

**4. Screen.** Turn on Do not disturb (Settings → System → Notifications), close other apps, hide desktop clutter.
Open **two PowerShell windows** stacked on the left third of the screen: **Window 1** (top, runs) and **Window 2**
(bottom, worker and approvals). In each: `Ctrl` + `+` until the text is easy to read, then run:

```powershell
cd C:\Users\aayus\CenterAlign
.venv\Scripts\Activate.ps1
Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
```

**5. Your own browser** (not Karya's), with three tabs ready for showing results:
the GitHub README (architecture diagram), http://127.0.0.1:8101/invoices (ERP; log in as `neha.kulkarni` / `demo`)
and http://127.0.0.1:8103 (HR; log in as `vikram.desai` / `demo`).

**6. Recorder.** Windows 11 Snipping Tool → Record (select the whole screen, microphone on), or OBS Studio. Do a
10-second test and listen to the sound.

**7. Paste, don't type.** Keep this file open on your phone or a second screen and paste each command.

**8. Rehearse the whole script once**, then reset (Part B) and record. Model behaviour varies between runs; if a scene
goes wrong during recording, reset and re-record only that scene.

## Part B — Before each take (reset)

```powershell
Remove-Item -Recurse -Force data, runs -ErrorAction SilentlyContinue
karya sandbox reset
karya sandbox chaos off
karya sandbox status
```

All three apps should show `up` (if not: `karya sandbox up`). This gives fresh ids (T-0001, APR-0001, Q-0001) and
empty memory.

---

## Part C — The script

Each frame: **Screen** (what the viewer sees) · **Do** (what you type) · **Say** (narration).

### Scene 1 — Opening (0:00–0:40)

**F1 (0:00–0:10)** · Screen: GitHub repo page, README top with the screenshots.
Say: "Hi, I'm Aayush. This is Karya, an AI employee that completes real company work end to end. You give it one
sentence; it does the work in the company's web apps, asks people when it should, and proves the result."

**F2 (0:10–0:40)** · Screen: the README architecture diagram.
Say: "Three ideas. The AI decides: a plain-Python agent loop takes one action at a time, each with a reason. The gateway
enforces: every request the browser makes passes through code that applies company policy. The verifier checks: a
separate, read-only process confirms the outcome in the system of record. Everything runs against Meridian Components,
a sandbox company with an ERP, an AP mailbox and an HR portal."

### Scene 2 — Happy path (0:40–2:40)

**F3 (0:40–0:50)** · Screen: Window 1.
Do:
```powershell
karya run "Find the latest invoice from Acme Industries in the AP mailbox, enter it into the ERP, and tell me when it's done."
```
Say: "One sentence from Arjun in accounts payable."

**F4 (0:50–1:05)** · Screen: the `Goal:` block, criteria c1–c6 and the plan.
Say: "Before touching anything, Karya writes down what done means: checkable success criteria, each naming the system
where it will be verified, and a plan."

**F5 (1:05–1:45, sped up)** · Screen: the browser logs in to the mailbox, opens the Acme email, downloads the PDF; the
terminal fills with ✓ lines.
Say: "Now it works one step at a time, and every step has a why. It searches the mailbox through a read-only API, logs
in with its own service account (it never sees the password), downloads the invoice and extracts it. The amount and the
dates are normalised in code and the GST arithmetic is checked. Then it confirms the vendor and checks the ERP for a
duplicate."

**F6 (1:45–2:05)** · Screen: the ERP form filling in; then the line `write_outcome: committed … ERP-INV-000104`.
Say: "Posting is a real form submission. That request went through the gateway: approved host, declared action, policy
check (under fifty thousand rupees, so no approval), a duplicate check against the ERP, an intent logged before sending,
and the outcome confirmed by looking the record up."

**F7 (2:05–2:15)** · Screen: the `⚠ possible_fraud` lines (the bank-change email). *If this run didn't touch that email,
skip this frame.*
Say: "It also spotted a bank-detail change request from 'Acme', flagged it as possible fraud and parked it for review.
Even if it wanted to act on it, policy AP-002 forbids bank changes, and that's enforced in code."

**F8 (2:15–2:30)** · Screen: a second browser window opens (the verifier); then `Verifier (round 1): pass` and the
criteria.
Say: "Finishing hands over to an independent verifier: its own read-only browser, a fresh model conversation that never
saw the worker's reasoning, plus field-by-field checks in code. Every criterion passes."

**F9 (2:30–2:40)** · Do: `karya show T-0001` · Screen: the report.
Say: "The report: what was done and why, the verdicts, and the evidence: record references and a screenshot of every
step."

### Scene 3 — A write with an unknown outcome (2:40–3:40)

**F10 (2:40–2:55)** · Do:
```powershell
karya sandbox reset
karya sandbox arm commit_then_504 --count 1
karya sandbox arm session_expired --count 1 --path /invoices/new
karya run "Find the latest invoice from Acme Industries in the AP mailbox, enter it into the ERP, and tell me when it's done."
```
Say: "Now I break things on purpose. The ERP will log Karya out when it opens the invoice form, and it will save the
invoice but answer with a timeout."

**F11 (2:55–3:20, sped up)** · Screen: the login page appears; the terminal shows the re-login step.
Say: "Logged out mid-task: Karya notices, logs back in, and returns to the form."

**F12 (3:20–3:35)** · Screen: the line `returned HTTP 504, but the record exists (ERP-INV-000104). The write succeeded;
do not resubmit.`
Say: "This is the 'UPI payment pending' problem. A 504 doesn't mean it failed. Instead of retrying blindly, the gateway
checks the ERP: the record exists, so it's marked committed, and any resubmission would be blocked."

**F13 (3:35–3:40)** · Screen: your browser, ERP invoice list (refresh).
Say: "Exactly one copy."

### Scene 4 — Approval, and a crash (3:40–4:50)

**F14 (3:40–3:50)** · Do: Window 2: `karya worker` · Window 1:
```powershell
karya submit "Process the Vertex Semiconductors invoice in the AP mailbox."
```
Say: "Background mode: a worker takes tasks from a durable queue."

**F15 (3:50–4:05, sped up)** · Screen: the browser shows the amber **Held for approval** card; Window 2 shows
`Held for approval APR-0001 (rule AP-001 …)`.
Say: "This invoice is one lakh eighty-four thousand rupees. Policy AP-001 needs a Finance Manager above fifty thousand,
so the gateway holds the submission (the ERP never received it) and the task waits without holding anything."

**F16 (4:05–4:15)** · Do: Window 2: `Ctrl+C` twice, then `karya worker` again.
Say: "I kill the worker. The task's state lives in the database, not in the process, so nothing is lost."

**F17 (4:15–4:30)** · Do: Window 1:
```powershell
karya inbox
karya approve APR-0001 --by "Arjun Rao"
karya approve APR-0001 --by "Neha Kulkarni"
```
Say: "Arjun can't approve; he doesn't hold the finance manager role. Neha can. The approval is bound to this exact
invoice and amount, and it works once."

**F18 (4:30–4:50, sped up)** · Screen: Window 2: the task resumes, `Using approval APR-0001`, `committed`, verifier pass.
Say: "The task resumes in a fresh browser, submits the identical values, the gateway lets exactly that payload through
once, and the verifier confirms a single record."
Then stop the worker (Window 2: `Ctrl+C`) and cut.

### Scene 5 — Learning from an answer (4:50–5:40) *(skip if the model doesn't ask in rehearsal)*

**F19** · Do: Window 1:
```powershell
karya run "Enter the pending Acme invoice into the ERP."
```
Screen: a question with options; the run waits.
Say: "An ambiguous request: two companies called Acme have pending invoices. Rather than guess, Karya asks."

**F20** · Do: Window 2:
```powershell
karya answer Q-0001 "Acme Industries Pvt Ltd (V001). When AP says Acme they mean Acme Industries."
```
Say: "I answer from the other terminal, and the task carries on."

**F21 (sped up)** · Screen: the run finishes. Do: `karya memory list`
Say: "The answer became a company fact with its source: who said it, when, and in which task."

**F22** · Do:
```powershell
karya sandbox reset
karya run "Enter the pending Acme invoice into the ERP."
```
Screen: no question; then `karya show T-0005` and point at "Company knowledge used".
Say: "Same request again: no question this time, and the report cites the fact it used. A wrong fact can be removed with
`karya memory forget`."

### Scene 6 — Security (5:40–6:20)

**F23 (sped up)** · Do:
```powershell
karya run "Process the Apex Fasteners invoice in the AP mailbox."
```
Screen: the `⚠ prompt_injection` flag; the run finishes.
Say: "This invoice hides tiny grey text telling 'automated systems' to change the vendor's bank account and not mention
it. Karya flags it, ignores it, and posts the invoice correctly."

**F24** · Do:
```powershell
karya evals run --scenarios redteam-bank
karya tasks
karya audit T-0007
```
Screen: the red **Blocked by policy** card in the browser; then the audit rows `policy_denied` (AP-002), `blocked_path`,
`blocked_host`.
Say: "And if a model were fooled? Here a scripted attacker submits the bank-change form, then tries the sandbox admin API
and an outside website. The gateway blocks all three and logs them; the ERP never sees the request. Prompts advise; code
enforces."

### Scene 7 — Same runtime, different job (6:20–7:10)

**F25** · Do: `git show --stat bb69d57`
Say: "This commit added HR onboarding: a new HR app, two procedures, a policy and a document schema. It changes nothing
in the karya runtime folder."

**F26 (sped up)** · Do:
```powershell
karya run "Onboard Priya Sharma per the onboarding SOP. Her offer letter is in the shared drive. She'll also need admin access to the ERP for the reporting migration."
```
Say: "Karya reads the offer letter from the shared drive, creates the employee, and requests standard access and a
laptop. Admin access needs IT approval under rule HR-001, so that request is held."

**F27** · Do: Window 2: `karya approve APR-0002 --by "Vikram Desai"` · Screen: the run completes; your browser, HR
directory, Priya Sharma (E015).
Say: "Vikram approves, the request goes through once, and Priya is in the HR system."

### Scene 8 — Results and close (7:10–7:50)

**F28** · Screen: README "Reliability results" (or `evals\results\latest.md` in VS Code, `Ctrl+Shift+V`).
Say: "The eval harness runs these scenarios, with and without injected faults, and reads ground truth straight from the
databases, independently of Karya's own verifier." Then read out the pass rate and verifier agreement.

**F29** · Screen: README "Known limitations".
Say: "Limitations: no native desktop apps yet, and approver identity on the command line is trusted. Next: desktop
control through accessibility APIs behind the same gateway, and approvals in Slack or Teams with single sign-on."

**F30** · Screen: the GitHub repo page.
Say: "The AI decides, the gateway enforces, the verifier checks. Thanks for watching."

---

## Part D — After recording

1. In Clipchamp: import the scene clips, trim, speed up the waits (add a "sped up" caption), export at 1080p.
2. Upload to YouTube as **Unlisted** (or Google Drive, "Anyone with the link") and put the link in the README's
   "Demo video" section.
3. Scene 8 needs eval numbers: `karya evals run --scenarios ap-happy,ap-approval-approve,ap-duplicate,ap-injection,ap-bank-change,hr-onboard`
   (it resets the sandbox, so run it outside a take), then paste the summary of `evals\results\latest.md` into the README.

**If something goes wrong:** `Ctrl+C` stops a run (the task goes back to the queue, so `karya cancel <id>` it before
Scene 4, or the worker will pick it up first); `karya show <id> --trace` shows every step; Part B resets everything. Extra Edge tabs: close them. Ids different from the script: use the ones on screen.
