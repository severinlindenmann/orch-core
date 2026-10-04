# Changelog

## 0.4.0

- The Usage page is charts: limit cards with reset time and a pace sentence, output tokens by model per day (last 7 / 30 days, or per calendar week), how much of this week's output is tied to a ticket, output per ticket, limits history with real time spacing, and the API-price equivalent per calendar week. The ticket table stays at the bottom. Days and calendar weeks are in the machine's local zone; limits history shows clock times; the menu line reads 5 h 51 % · in 3 h 11. Needs orch API 2.6 (the Chart widget, with time axis).

## 0.3.0

- The menu entry chip shows the weekly percent; under the label a line shows the 5-hour percent and a live countdown to its reset ("5 h 35 % · resets in 3 h 05"). Needs orch API 2.5.

## 0.2.0

- The menu entry shows the higher of the 5-hour and weekly limit as a chip (green below 70 %, amber to 89 %, red from 90 %); the tooltip has both limits and their resets. Needs orch API 2.5.
- Models show as Opus 5.5, Sonnet 5, Haiku 4.5, also for dated ids.
- Usage page rows: tickets first, then shared orchestrators, then "Not linked to a ticket".

## 0.1.0

- First version: a Usage panel on the ticket page, a Usage page with the 5-hour and weekly limit and this week's tickets, and a status line recorder (recorder/statusline.sh) for the limits log.
