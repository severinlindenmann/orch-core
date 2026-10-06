# Changelog

## 0.5.0

- No limits log at the default path is now an info card with the setup command (`orch addon setup ticket-usage`, run by the human in their own terminal), not an error; the warning stays for a configured Limits log that is not there. Once the status line runs the recorder, the card says limits appear after the next reply. `orch doctor` reports the recorder as `usage-recorder` while the addon is enabled.
- The menu entry is readable at a glance: "5h" and "Week" each get a labelled row with a small meter, the percent ("1 %", same spacing for both) and its reset ("resets in 4h49", "resets Mon 09:00"). A limit the pace sentence sees filling before its reset is amber even below 70 %. A window that has reset reads "5h — reset" in grey. Readings older than the new "Dim the menu limits after" setting (30 minutes by default) grey the rows and add "as of 13:52". Where the rows do not fit, the chip names the riskier limit ("Week 59 %"); the tooltip is unchanged. Needs orch API 2.8 (MenuRow).
- The transcript parse cache is kept in the addon's state folder (`parse-cache.json`), so the first fetch after `orch serve` restarts re-reads only transcripts that changed.
- A missing Limits log is named ("File not found: <path>") instead of blaming the recorder; a relative path is refused on save and a file that is not there yet is saved with a note. The Usage page refreshes after settings are saved. No empty Estimate column while dollar figures are off. A pace sentence no longer projects past the reset; limit cards no longer repeat their title; the weekly-pace hint says how much history there is; the limits history axis shows the date when it spans two calendar days.

## 0.4.0

- The Usage page is charts: limit cards with reset time and a pace sentence, output tokens by model per day (last 7 / 30 days, or per calendar week), how much of this week's output is tied to a ticket, output per ticket, limits history with real time spacing, and the API-price equivalent per calendar week. The ticket table stays at the bottom. Days and calendar weeks are in the machine's local zone; limits history shows clock times; the menu line reads "5h 51% · 3h05" (Countdown now draws "3h05" / "12 min", no "in"). Needs orch API 2.6 (the Chart widget, with time axis).

## 0.3.0

- The menu entry chip shows the weekly percent; under the label a line shows the 5-hour percent and a live countdown to its reset ("5 h 35 % · resets in 3 h 05"). Needs orch API 2.5.

## 0.2.0

- The menu entry shows the higher of the 5-hour and weekly limit as a chip (green below 70 %, amber to 89 %, red from 90 %); the tooltip has both limits and their resets. Needs orch API 2.5.
- Models show as Opus 5.5, Sonnet 5, Haiku 4.5, also for dated ids.
- Usage page rows: tickets first, then shared orchestrators, then "Not linked to a ticket".

## 0.1.0

- First version: a Usage panel on the ticket page, a Usage page with the 5-hour and weekly limit and this week's tickets, and a status line recorder (recorder/statusline.sh) for the limits log.
