# Forced-Coding
A Claude agent to check for new legislation requiring software code to be written.

Claude instructions:

# Routine prompt — by-design bill watch (file output, token push to main)

You are running as an unattended routine. Work to completion without asking
questions. The repository is already cloned and is your working directory.

## Steps

1. Run the watcher: `python billwatch.py`
   It reads config.yaml, queries LegiScan, and writes candidates.json (new or
   updated bills) plus an updated state/seen.json.

2. Open candidates.json. Note today's UTC date for the filename in step 4.

3. For each bill in candidates.json, decide whether it is RELEVANT using this
   test: the bill, if enacted, would require a specific technical feature,
   capability, or implementation to be built into software, hardware, or an
   online service (e.g. honoring an opt-out preference signal, age
   verification/estimation, a mandated interoperability or data-portability API,
   right-to-repair access, or AI content labeling/provenance/watermarking).
   Exclude bills that only fund, study, set liability, or impose disclosure
   paperwork with no required technical build. If a title is ambiguous, keep it
   and mark it "(uncertain)".

4. Write results to `digests/<UTC-date>.md` (e.g. digests/2026-06-18.md):
   a heading with the date and counts (scanned, relevant); then relevant bills
   grouped by state, each line showing status (NEW/UPDATED), the bill number as
   a Markdown link to its url, the title, the last action, and a one-line reason
   it qualifies. If none are relevant, write a one-line file saying so. Always
   write this file so each run leaves a record.

5. Commit and push to main using the GH_TOKEN secret for authentication (the
   Claude GitHub App is NOT installed, so the default git push will 403 — you
   MUST push via the token URL). Run, in order:

       git config user.email "billwatch@users.noreply.github.com"
       git config user.name "billwatch-bot"
       git add digests/ state/seen.json
       git commit -m "billwatch <UTC-date>" || echo "nothing to commit"
       git push "https://x-access-token:${GH_TOKEN}@github.com/pchestek/Forced-Coding.git" HEAD:main

   Do NOT use any GitHub connector. Do not open a pull request.

Be concise. Do not modify files other than digests/*.md, state/seen.json, and
the local candidates.json.

