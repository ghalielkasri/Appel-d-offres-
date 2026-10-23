# Radar — audit and roadmap

Audit of the version pushed on 2026-10-23, and the plan to take it from a
tender-watch tool to a bid-management platform.

## What already works well

- **Collection.** Tenders via Apify, awards scraped from the official portal,
  PRADO login for the DCE (CPS, bordereau des prix). Every raw payload is kept.
- **Matching.** A 159-term kitchen-equipment profile in nine families, with
  weighted scoring (object 3, procedure 1, buyer and place 0.5) and exclusions.
- **Comparables.** The core feature: past awards matched by buyer, equipment
  family and text, with honest rules (a shared buyer alone never counts, the
  median rather than the mean, the reason for every match shown).
- **Search** without accents or case, across tenders, awards, companies and
  buyers.
- **Alerts** by email, WhatsApp and webhook, never sent twice.
- **Exports** to CSV, Excel and JSON, plus a JSON API.
- **Operations.** Docker Compose, a scheduler, a pre-flight `doctor`, 243 tests.

## What was missing or broken

| # | Finding | Kind |
|---|---|---|
| 1 | No bid workflow. The tool says a tender exists but not what the team decided: no go / no-go, no notes, no submitted / won / lost, so no win rate. | Product gap |
| 2 | Profiles can only be edited from the command line, and an edit never re-scores the tenders already in the database: a new keyword only applies to future collections. | Bug + gap |
| 3 | Page links are built from raw query values: a search containing `&`, `#` or a space breaks page 2. | Bug |
| 4 | Buyers have no page of their own; the link only filters the awards list. | Gap |
| 5 | Company pages show a flat list: no timeline, no buyer mix, no equipment mix. | Gap |
| 6 | The monthly chart is a row of bars without axis or values. | UI |
| 7 | "Collecter maintenant" redirects silently: no sign that anything started. | UI |
| 8 | Generic styling, inline styles in every template, no shared components. | UI |
| 9 | One Basic-Auth user for everyone. | Later |
| 10 | `create_all` creates new tables but never alters existing ones; Alembic is installed but unused. | Later |
| 11 | Search matches substrings without stemming ("medical" misses "médicaux"). | Later |

## Phase 1 — done in this pass

- **Contra design system**, the one used for the brand tracker: the same
  `contra-tokens.css`, white shell with the 3px lime line, 300px sidebar with a
  workspace card, sticky top bars, hello rows with stats boxes, pill controls,
  Network tables, Jobs lists, a Details card and a stepper on the tender page.
- **Bid pipeline** (`/candidatures`): every tender can be moved through
  *À étudier → Go → En préparation → Soumis → Gagné / Perdu*, or dropped
  (*Abandonné*), with an offered amount and notes. The win rate only shows once
  bids have been decided.
- **Profile editing in the interface** (`/veille`): turn a profile on or off,
  add or remove keywords and exclusions, set the minimum score. Every change
  re-scores all tenders already stored, and so does `radar profile-edit`.
- **Buyer pages** (`/acheteurs/{id}`): spend, open tenders, awards history,
  the suppliers who win there, equipment mix.
- **Richer company pages**: wins over time, the buyers they win with, their
  equipment families.
- **Price range** on the tender page: minimum, median and maximum of the
  comparables on one scale, with the tender's estimate marked.
- **⌘K search** from any page.
- Fixed page links (finding 3) and feedback after "Collecter maintenant".

## Phase 1b — re-centred on your company (Arcade Equipement)

The platform now reads everything from one company's point of view
(`COMPANY_NAME`, default "Arcade Equipement"), the way the brand tracker reads
a brand:

- **Overview**: your rank and market share over 12 months, the leaderboard
  around you, you vs. the leader month by month, opportunities, what your
  followed competitors just won, your rank in each segment.
- **My profile** and **company pages**: the brand tracker's brand page —
  ringed logo, rank and rank change, stats box, biggest wins, tabs for
  overview, markets, competitors (the segment ranking) and buyers, over 12
  months, 3 years or everything. Competitor pages add **Face à vous**: shared
  buyers, buyers where they win without you, and the reverse.
- **Leaderboard**: by amount, with rank changes against the previous period,
  share, a 12-month trend line, four leader cards, filters by segment.
- **Segments**: the brand tracker's categories — the market by equipment
  family, each with its share, leader and your rank.
- **Competitors**: the companies you follow, and suggestions of who to follow.
- **Activity**: who won what, for the whole market, your followed competitors
  or yourself.
- Lost bids now show who won and how far your offer was.
- A fictional market (11 companies, ~150 awards over 3 years, 22 open tenders)
  stands in until real collection runs; a banner says so on every page.

## Phase 2 — next

1. **Competitor alerts**: an email or WhatsApp message when a followed
   competitor wins, or when a new tender comes from a buyer where they win and
   you don't.
2. **Deadline reminders**: an alert at D-7 and D-2 for every bid still in Go or
   En préparation, on the channels already configured.
2. **Calendar feed** (`.ics`) of deadlines and opening sessions for bids in
   progress, so they land in Google Calendar or Outlook.
3. **Team accounts**: named users instead of one shared login, an owner on each
   bid, the history of who moved what.
4. **DCE checklist per bid**: the pieces to produce (caution provisoire,
   attestations, technical offer, bordereau), each ticked off.
5. **Alembic migrations** so schema changes reach Postgres safely.

## Phase 3 — later

- **AI reading of the CPS and bordereau**: pull the lots, quantities and
  technical requirements out of the downloaded PDF into the bid.
- **Head-to-head view**: two companies side by side across shared buyers.
- **Price per line**: once bordereaux are parsed, benchmark unit prices
  rather than whole-tender amounts.
- **Stemmed search** (French), so "médical" and "médicaux" meet.
