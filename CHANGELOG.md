# CoffeeOS version history

## 5.0.0 — the product became a coffee shop

The product was rebuilt end to end around a coffee shop. This is not a change of signage: a coffee
shop's centre of gravity for money is different, and almost every calculation had to be built
again. The bakery core was not thrown away — its most proven parts were kept and re-aimed.

### The subject changed: the display case instead of baked goods

- **A drink cannot run out.** A latte is made to order, so the whole "demand ≠ sales" engine now
  applies ONLY to the display case — food with a finite stock. Previously, had drinks ended up in
  there, the system would have "detected" lattes selling out every evening after closing.
- **Baking plan → display-case order.** The logic was kept in full: recovering truncated demand via
  conditional expectation, shrinkage by day of week, a buffer from the target service level, and
  the owner's manual override taking priority.
- **Lost revenue got more expensive.** In a bakery, running out of a bun means losing the sale of a
  bun. In a coffee shop, running out of breakfast means the guest took only a coffee — and the next
  day went for breakfast somewhere with a stocked display case, and bought their coffee there too.

### Things the bakery version deliberately refused to do

The previous version did not compute cost, and that was the right call: between flour and a
baguette stand dough, proofing and the baker's hands — consumption cannot be recovered from
receipts.

In a coffee shop, nothing stands between beans and a latte. The recipe is constant: 18 g of beans,
200 ml of milk, a cup, a lid. Therefore:

- **Cost and margin for every item** are computed exactly (`costing.py`).
- **The recipe book ships ready** (`reference.py`): the owner enters prices only, rather than
  filling in technical recipe cards. Nobody fills in recipe cards.
- **A menu breakdown by money**: stars, plowhorses, puzzles, dogs — compared against the median of
  the shop's own menu, not against an industry benchmark.
- **The economics of milk, separately.** The surcharge for plant milk gets set once while the
  purchase price keeps rising. The product shows whether it still covers the difference.
- **Exact consumption and a supplier request** (`supply.py`): not "order beans" but "27 kg of
  beans, 58 l of milk; milk every 3 days, it does not keep longer".

### New things a bakery never had and never could have

- **Parsing a receipt line** (`menu.py`). "Iced latte 450 with oat + syrup" is not a unique product;
  it is a size-L latte on oat milk. Without that parse, a coffee shop's catalogue shatters into
  hundreds of names, and neither consumption nor margin nor attach rate can be computed.
- **The "coffee + food" attach rate** — the main lever on average ticket: people buy the drink
  anyway, and add food only if offered. The gap between morning and afternoon is shown separately:
  by lunchtime the display case is usually no longer working.
- **Shift throughput** (`staffing.py`). A coffee shop is constrained not by stock but by hands.
- **A maintenance schedule.** A drifting grind setting shifts the dose by 2–3 g per cup — a tenth
  of bean consumption; a clogged filter kills the boiler.
- **Waste is split**: the display case at menu price (unearned revenue) and poured-away milk at
  cost (a direct expense). Adding them into one figure would mean comparing different things.
  Poured-away milk appears nowhere at all except in that line.
- **The importer understands coffee-shop exports**: modifiers and size from separate columns,
  employee, guest card, order type (Poster, iiko, r_keeper, Evotor, Quick Resto). The modifier is
  glued onto the item name before parsing.
- **`rescan`** — rebuilding the parse across the whole history after the catalogue is corrected.
  Previously a correction applied only to new receipts.

### Three genuine defects found during the rebuild

- **Upgrading broke a working database.** The index on the new column was created in the same
  script as the tables: on a database being upgraded from the bakery version,
  `CREATE TABLE IF NOT EXISTS` does not add the column, and `CREATE INDEX ... (base)` fails with
  "no such column". Indexes were moved to run after the migrations.
- **The supplier request inflated stock.** Lead time was being added to the order volume every
  cycle. Within a month there would have been a month's supply of beans under the counter, paid for
  out of working capital. Lead time affects WHEN to order, not HOW MUCH.
- **A guaranteed milk stockout.** Perishables were ordered for 3 days while the request cycle was
  weekly — from Thursday onwards the shop had no milk. Such items now get their own order cycle,
  and the product says so out loud. The defect was found by the simulator, not by a person:
  `--supply` showed 63 days out of 69 without milk.

### The product still does not make things up

The previous version's core principle was kept and extended to the new sections.

- **An unknown price yields "not computed", not "100% margin".** Until the owner has stated the
  purchase price of a display-case item, its cost is `None`, not zero. Typical prices are labelled
  as typical.
- **Estimates are signed as estimates.** The first version of the attach rate promised +₽149k/month
  by extrapolating the best hour across the whole day. A figure like that cannot be refuted, so it
  is worth nothing; the potential is now computed from the shop's own median hour and comes out an
  order of magnitude more modest.
- **The second-barista scenario became a measurement.** It used to rest on the assumption "there is
  demand in the queue". The product now verifies that from the distribution: if an hour is merely
  popular, the cup count in it varies day to day; if hands are the constraint, the distribution is
  clipped at the top. Without a confirmed ceiling, no figure is shown at all.
- **A flat day no longer looks like one continuous peak.** An hour counts as busy only if it is at
  least 1.4× above the median.
- **Guest retention and barista performance** are shown only if the till passes a guest and an
  employee. Otherwise the section honestly says it does not know.
- **The system works without daily manual entry.** Verified by a test and by a separate row in the
  simulation: with not a single waste entry, the display-case order does not fall apart. A product
  resting on manual entry dies along with the barista who was trained to do it.

### A full audit after the pivot: 8 more defects

The product was run against degenerate databases (empty, one receipt, drinks only, display case
only, service lines only, an export with no timestamps, zeros and negatives in prices), against
malformed exports, against staff input, and against the bot, the auto-loader and the web dashboard
all running at once.

- **The "Margin" section showed the wrong revenue.** It carried the total for drinks and display
  case only, but was labelled as the venue's revenue — a few percent below what the till said. The
  owner would have reconciled against the till and concluded the system was miscounting. Both
  figures are now shown: the venue's full revenue, and the share for which margin was actually
  computed.
- **An ambiguous write-off silently went to the wrong item.** "Write off croissant 3" with two
  croissants in the display case picked one of them — the wrong one. The consequence is worse than
  it looks: a write-off cancels the sell-out flag, so lost revenue stopped being computed for the
  croissant that genuinely ran out, while "leftovers" grew for the innocent one. The system now
  asks. The same goes for stock levels, prices and catalogue edits.
- **The "Milk" section crashed entirely** if any item had a zero price (a promotion, a service line
  from the till): an undefined food cost reached formatting as `None`. That cell now shows a dash.
- **The maintenance schedule did not handle Russian noun cases.** "Сделал калибровку" ("did the
  calibration", accusative) did not match "Калибровка помола" ("grind calibration", nominative),
  and the section looked broken. Matching is now done on word stems.
- **"Price beans -500" was being routed to the supplier-request section**, and the owner was
  convinced the price had been set. It is now rejected with an explanation.
- **A typo in a stocktake was accepted unchecked**: "stock beans 99999999" produced "enough for 71
  million days" and silenced the supplier request entirely.
- **Placeholders in the modifiers column bred phantom items.** Tills put "-" or "none" into an empty
  column; glued onto the item name, they turned one latte into three different products in the
  catalogue and in deduplication.
- **Confirming a stock level did not name the unit of measure** — "Stock, coffee beans: 3" did not
  answer the question "three of what".

Separately checked and found sound: the caches see price edits, catalogue edits, a new receipt and
a new write-off; the web layer rejects malformed payloads; the supplier request never orders less
than needed; receipts at 00:00 and 23:59 land in the correct day; the display-case order is computed
instantly on a 400-item catalogue; 120k rows import in 1.6 s; the bot, the auto-loader and the web
dashboard write to the database concurrently without conflict. The bot's replies were checked
against Telegram's 4,096-character limit and for balanced markup. On a busy database the bot now
replies "an export is loading, try again in a minute" instead of "something went wrong".

### Verification

- The simulator was re-aimed: an independent demand model (a lunchtime peak instead of a morning
  one), four display-case strategies, and a new `--supply` mode that checks the supplier request
  never leaves the shop without beans or milk.
- The reference point is more honest: the comparison is not against the product's own previous
  version, which nobody has, but against **a two-week average in a spreadsheet** — how most coffee
  shops actually do the maths. It loses even to the owner's own judgement.
- 144 tests: menu parsing, cost, display case, consumption and supplier requests, shift, attach
  rate, write-offs, importing six export formats, access and health, and the interface's honesty.

---

*Below is the product's history before the pivot, when it was an operating system for a bakery. The
display-case calculations, the receipts loader, the self-monitoring, the backups and the "do not
make things up" principle all came into CoffeeOS from there.*


## 4.2.0 — the product stopped making things up

The main defect of earlier versions was not in the calculations but in the honesty: the interface
showed numbers and statuses with nothing behind them, and buttons pretended to work. The owner took
it for the truth. 31 defects fixed.

### What was invented — removed

- **The "Integrations" section** ("Evotor connected", "1,240 receipts", "63 items") was displayed
  even with synchronisation switched off. Removed.
- **The "Shifts" section**, with non-existent employees, their rotas and recommendations. There is
  no shifts table in the database and never was. Removed.
- **Three dummy buttons** on the dashboard: "Approve and send to the baker", "Send to supplier",
  "Apply" on the shift — they rendered success without a single request to the server.
- **The −/+ buttons in the baking plan** changed the number on screen only: the baker received the
  original plan while the owner was sure they had corrected it.
- **`dashboard.html`** — 60 KB of dead mockup that the server never served and that never made a
  single request. Removed.
- **The "Approve and send to the baker" button in the bot** replied "sent to the baker" while
  sending nothing.

### What became real

- Batch edits are saved to the database (`POST /api/plan/adjust`) and go to the baker with the
  plan; the plan is marked "you corrected this by hand".
- "Approve" (`POST /api/plan/approve`) genuinely sends the plan to Telegram and reports the result
  honestly — including "no recipients configured".
- **Lost revenue** — the product's main value — is now visible on the dashboard. It used to be
  computed, arrive in the API, and be displayed nowhere.
- The dashboard was rewritten from scratch: not one hardcoded number, status or name. An empty
  database yields "no data yet" rather than cheerful zeroes.

### The system admits what it does not know

- **An export with no sale time** no longer silently yields "sales peak 0:00" and "lost revenue
  ₽0": the importer, the bot, the status view and the dashboard all warn that these measures cannot
  be computed on such data.
- **An export with no receipt number** warns that receipt composition cannot be reconstructed and
  the average ticket is unreliable. Previously the flag was computed and displayed nowhere.
- **An empty database** raises an alert rather than a briefing reading "Revenue ₽0".

### Security

- **The bot no longer lets anyone in** while the allow-list is empty. Previously one unfilled line
  in `.env` made revenue and plans public to anyone who found the bot in search. Now only `/start`
  works, showing the person their own chat_id.
- `validate()` warns both about open access to the bot and about a missing password on the web
  dashboard.
- The systemd services run as a dedicated user rather than root, with basic isolation;
  `install.sh` sets `.env` to mode 600 (it holds the token and the password).

### Data and calculations

- **9 of 22 real pastry names** were falling into "Other" and permanently dropping out of the plan
  (kulebyaka, rasstegai, charlotte, pie, panettone, grissini, pretzel, chak-chak). The category
  dictionaries were extended — all 22 are now recognised.
- **The catalogue can now be edited:** "we bake focaccia", "we don't bake coffee", "catalogue".
  Previously the flag was set once at import with no way to correct it.
- **One written-off bun no longer cancels the sell-out flag for a whole day** — a threshold was
  introduced (2 units or 3% of the day's sales).
- **A single time zone** for the bot, analytics, write-offs and backups: on a server running UTC,
  "today" was off by three hours.
- The demand estimate is now **weighted by recency** (21-day half-life): without it the plan lagged
  the trend and, against an independent demand model, lost even to an owner who simply looks at
  yesterday.
- `demand_stats` is cached: a single `/api/summary` was computing it three times.
- Migrations check the schema via `PRAGMA table_info` rather than swallowing every exception with
  `except: pass`.
- `COST_SHARE`, `SALVAGE_SHARE` and the mention of the "cost …" command were removed from `.env` —
  none of them have existed in the code since 4.1.0.
- Alerts about breakage no longer repeat every 6 hours: at most once a day, plus a separate message
  when everything is working again.

### Verifiability

- **The simulator got an independent demand model.** As long as it took its hourly and weekday
  profile from the demo-data generator, it was testing the algorithm against the same world that
  algorithm grew out of, and could not refute the product even in principle.
- Against an honest model, the benefit claimed in the README **was not confirmed**: instead of
  "+₽28.5k/month" it comes out around +₽3k, with a spread from −2 to +12. The README was corrected.
  Version 3.2, meanwhile, costs the owner −₽100k/month — so the current version closed a real hole
  rather than conjuring a benefit out of thin air.
- Three tests were decorative and now test something real: the README test never executed the body
  of its condition; the shift test forbade a string that was sitting verbatim in the dashboard; the
  access test did not check the main case — an empty allow-list.
- Tests: 79 → 90.

## 4.1.1 — a full audit: 30 defects fixed

The product was reviewed not for "does the code work" but for "does it lie to the owner and does it
lose their data". Three classes of problem turned up: money lost on import, silent distortion of the
headline metrics, and small interface breakages. Every fix is covered by tests (79, against 57
before the audit).

### Receipt import: revenue was being lost and double-counted

- **A standard 1C/Evotor export loaded with zero revenue.** The headers "Item qty", "Item price"
  and "Item total" were being recognised as the item name (because of the word "item"), the money
  columns were not found at all — and the import reported success. The column is now identified by
  where the keyword sits in the header, not by list order.
- **A failure mid-import destroyed the entire sales history.** Clearing the database was committed
  separately from writing, leaving nothing to roll back. Clearing and writing are now one
  transaction with a rollback.
- **Re-importing the same receipt crashed the import** (a `ValueError` while reconciling lines), and
  in auto-load mode the file went to quarantine.
- **A price of "0" with the total filled in zeroed the line** — that is the standard fiscal-operator
  format for discounts and rounding. The price is now derived from the total.
- **Two different receipts sharing a number on one day were merged** (per-shift numbering on the
  till) and the second one's revenue was lost. At the same time, one and the same export arriving
  once with timestamps and once without no longer loads twice.
- **Corrections from the till are applied** rather than lost: a revised quantity updates the
  receipt; collapsed lines do not double-count revenue; a partial export never deletes lines already
  loaded.
- The CSV delimiter is now detected from the header row and knows about tabs; unix timestamps are
  understood; "cash"/"card" from an API are no longer counted as card-only; truncated rows go into
  the malformed counter rather than into the database at zero price.

### Metrics: silently showing the wrong thing

- **Refunds were zeroing out lost revenue.** The hour of a refund counted as "the hour of the last
  sale", sell-outs stopped being detected — and for the best-selling items the product's headline
  metric showed zero.
- **"Per month" was understated by 20%** when the history was shorter than the calculation window.
- **Over-baking a new item went unnoticed:** write-offs were averaged over the whole window while
  sales were averaged over the item's lifetime. The denominator is now shared.
- **The baking plan could suggest "bake 33 carrier bags"** — a service-item filter was added that
  does not depend on the catalogue flag.
- A discontinued item no longer lingers in the recommendations via the cache.
- A large refund no longer produces shares like "Bread 120%".

### Interface

- **Junk write-offs no longer corrupt the data:** "списание с 3" ("write-off with 3" — a stray
  Russian preposition where the item name should be) recorded 3 baguettes, and
  "write-off baguette 999999" recorded almost ₽90m — after which the system stopped seeing sell-outs
  for that day. Validation of the name and the quantity was added.
- "write-off baguette 5 units" is now recorded (previously a report was silently shown and the
  salesperson was sure the write-off had been entered), and an unparseable format gets a hint rather
  than yesterday's report.
- Product names such as "Coffee 3_in_1" no longer break the message markup.
- `/status` no longer hangs the bot while it checks connectivity to the model.
- Pressing a button in supplier-request mode no longer sends the supplier the text "📈 Revenue".
- Routing: "forecast" and "till check" reach their own sections, "the oven is broken" no longer
  opens the baking plan, and "how much was written off" is recognised.
- Backup rotation no longer deletes the copy just created (and then crash immediately afterwards),
  and demo data no longer erases staff write-offs entered by hand.
- The AI diagnostics no longer report "model ready" when a neighbouring version is installed, and
  no longer suggest `ollama pull` on a rate-limit error.

## 4.1.0 — cost was removed from the system

The owner of a small bakery does not need to know or enter their own cost of goods — and previously
both the baking plan and every financial report depended on it. It was removed completely: the
system no longer computes or stores the cost of ingredients anywhere.

- **The baking plan** is now built from a fixed target service level (70% of demand by default)
  instead of a percentile from the newsvendor formula over cost. The behaviour is close to the
  previous one for a typical bakery, but with not a single setting to configure.
- **The "What earns money" (margin) report** was removed entirely — along with the `/marzha`
  command, its button and the `margin` API field.
- **Write-offs** are shown in units and as a total at menu prices (retail price); the "real loss at
  cost" calculation was removed.
- **Lost revenue** is computed at full price (units × price) rather than at margin.
- The `COST_SHARE` and `SALVAGE_SHARE` settings, the `cost_config` table, the "cost …" chat command
  and the industry ingredient shares by category were all removed.
- `tools/simulate.py` was kept as a benchmark: it takes the ingredient share as an external
  assumption (`--cost`) purely to estimate profit; the system itself does not use it.

## 4.0.0 — the baking plan started making money

A review from the position of a business owner rather than a developer. What was tested was not
"does the code work" but "does the product make the bakery money". It turned out that it did not.

### The core: the product was losing the client money

**The baking plan was optimising the wrong quantity.** The formula was
`plan = demand − 0.6 · write-offs`: the system was trying to reduce write-offs. But losses in a
bakery are asymmetric — an unsold bun costs the owner only the ingredients (~30% of price), while a
bun that ran short costs the entire margin (~70%). By minimising visible write-offs, the product was
increasing the invisible undersell, which is twice as expensive.

A closed-loop simulation over one and the same customer stream
(`tools/simulate.py --days 120 --seeds 5`, ingredients at 30%, averaged over 5 streams):

| strategy | gross profit | vs the owner's judgement | actual service level |
|---|---|---|---|
| the owner's judgement (no system at all) | ₽988,558/month | — | 34% |
| BakerOS 3.2 | ₽932,426/month | **−₽56,132** | 17% |
| BakerOS 4.0 | ₽1,017,080/month | **+₽28,522** | 57% |

The previous version was **worse than having no system at all**, and consistently so: across all
five streams, between −52 and −₽57k/month. The gap between 3.2 and 4.0 is about ₽85k/month for a
bakery turning over ₽1.3m.

Where the advantage is reliable and where it is not (mean and spread across 5 streams):

| ingredients, % of price | 4.0 vs the owner's judgement | spread |
|---|---|---|
| 25% | +₽41,017/month | +34,107 … +44,733 |
| 30% | +₽28,522/month | +22,680 … +32,329 |
| 35% | +₽18,037/month | +13,627 … +21,219 |
| 45% | +₽1,644/month | −649 … +3,564 — **noise, the sign flips** |

The honest formulation: the product reliably makes money where ingredients cost up to ~35–40% of
price. That covers a normal bakery (25–35%) but not a venue with expensive ingredients — there the
gain is indistinguishable from zero, and promising it would be wrong.

**The actual service level is below the target** (57% against a target of 70%), and this is not
hidden: μ and σ are estimated from truncated sales and are therefore biased downwards. The simulator
prints the actual service level as its own column so the discrepancy is visible rather than buried
in a formula.

**What was done.** The plan is now computed as `mean demand + z·sigma`, where `z` corresponds to a
service level of `(1 − cost_share) / (1 − markdown_share)` — the classic newsvendor problem. All the
financial logic was moved into a new module, `coffeeos/economics.py`, with the formulas explained.

**Cost became configurable** — per item, per category and per bakery, straight from the chat: "cost
croissant 35". Until it is set, industry values per category are used. This is not cosmetic: the
batch size depends directly on that number.

### Sales ≠ demand

**The system did not distinguish "we sold 50 because that is what people wanted" from "we sold 50
because that is how many we baked".** Every forecast was built on truncated data and reproduced
yesterday's shortfall.

Sell-outs are now detected **from the receipts, with no manual entry at all**: if an item stopped
selling long before closing while customers were still coming, it ran out. The test is probabilistic
(`(1 − evening traffic share)^number of sales < threshold`), so low demand is not confused with a
sell-out. Demand on such days is recovered via the conditional expectation of a truncated
distribution, with the correction capped from above.

**The dependency on manual waste entry was removed.** Previously the only signal for "we ran short"
was write-offs, which the salesperson has to enter every evening. With 4–5 month staff turnover that
stops happening within a month, and the product goes blind. Write-offs are now a refinement, not
fuel: in simulation, the variant "the salesperson entered no write-offs at all" gives the same
result (₽1,022,857 against ₽1,022,807). The evening reminder was rewritten as optional, and that is
covered by a test.

### New: the money you cannot see in the till

- **Lost revenue** (`/upusheno`, the "💸" button) — how much was missed because an item ran out,
  broken down by item, with the hour named. Neither the till nor the fiscal operator shows this:
  they know what was sold, but not how many people turned around and left.
- **What earns money** (`/marzha`, the "💰" button) — gross profit by item. The top by turnover and
  the top by money are different lists, and the owner of a small bakery sees the second one nowhere.
- The morning briefing and the express audit were rebuilt around this money.

### Trust fixes

- **The answer about shifts was hardcoded.** "Demand peaks at 08:00 and 18:00, reinforce the evening
  shift with a second salesperson from 16:00" was issued to every bakery regardless of its data —
  while the real hourly profile was already being computed right next to it. Peaks, the quiet hour
  and the busy windows now come from the receipts; two peaks are spread apart in time (otherwise
  "08:00 and 09:00" is one morning peak).
- **The README promised a feature that is not in the code**: "setting ingredient stock from the bot
  ('stock flour 40')". No such handler existed. The promise was removed, a test was added to stop it
  coming back, and a "What the product does NOT do" section was written.
- **Write-offs were shown at retail prices.** "₽15,000 written off" against a real loss of about
  ₽4,500 — a threefold overstatement. This is not a harmless inaccuracy: it is exactly what pushes
  the owner into cutting batch sizes. Both figures are now shown, with the real one highlighted.

### The demo data no longer misleads

The generator modelled a bakery with **infinite stock**: sales never ran up against what was baked,
and sell-outs never occurred at all. On data like that the forecasting problem is artificially easy,
and the main error — under-baking — is invisible in principle.

A finite batch is now modelled: the owner bakes by judgement, reacting more sharply to leftovers
than to sell-outs (as in life), stock runs out, and some demand walks away. The demo bakery yields
around ₽1.27m/month with write-offs of ~12% of the batch in units (≈3.7% of revenue at cost) —
plausible figures.

### Verifiability

- `tools/simulate.py` — comparing strategies against one customer stream, averaged over several
  streams (`--seeds`), showing the actual service level. Any claim the product makes about money can
  be re-checked with a single command.
- Tests: **38 → 53**. The new ones cover the service level, a plan above mean demand, the response
  to expensive ingredients, detecting sell-outs from receipts, write-offs taking priority over the
  heuristic, operation with no manual entry at all, separating the menu price from the real loss,
  the absence of hardcoding in the shift answer, setting cost from the chat, and the honesty of the
  README.

### Found by an independent review and fixed

The finished 4.0 was separately attacked on edge cases. Four genuine defects turned up — all closed
and covered by tests:

- **The safety cap on the demand correction was killing seasonal items and new products.** The
  weekday averages and the overall cap were computed over different denominators. A "Saturdays only"
  item got a plan of 3 against demand of 10; a new product that sold 40 units yesterday got a plan
  of 1 — and could never climb out of it, because with a batch of 1 a sell-out is never recorded.
  Days are now counted from the moment the item appeared, and the cap is compared against the same
  slice of raw sales.
- **A crash on refunds.** With a negative mean, `mu ** 0.5` produced a complex number and brought
  down `bake_plan`, and with it the morning briefing, the dashboard and the assistant.
- **False sell-outs for "morning" items.** The check assumed an item was equally likely to appear in
  any receipt of the day. A croissant that only sells before 10 a.m. and never runs out was flagged
  as sold out on 40 days out of 40, demand was overstated, and the report showed ₽8,600/month of
  lost revenue that did not exist. An item is now compared against itself: a day counts only if it
  ran out at least two hours earlier than its own usual hour.
- **Two different estimates of the same quantity in one message** — "yesterday" and "on average"
  were computed over different bases. They were brought onto one.

Plus smaller items: sigma was understated because degrees of freedom were not accounted for;
markdowns were counted in the write-offs report but not in the margin report; "per month" was
computed over 30 days rather than the actual working days; the write-off window in the plan did not
match the demand window.

---

## 3.2.0 — final audit: regressions and the last defects closed

A fourth independent review, which also checked for regressions from earlier fixes. What it found
was fixed and covered by tests (38 tests).

Critical:
- **Encoding detection broke on files larger than 64 KB.** Only the first chunk was checked, and the
  boundary cut a Cyrillic character in half — the file was judged to be cp1251 and read as garbage.
  In testing, 40 of 40 real exports (~200 KB) failed to import at all, going to quarantine with no
  notification to the owner. The whole file is now checked: 20 of 20 large files load correctly.
- **One receipt with a date in the future zeroed out the entire product.** A till with the wrong
  clock, or a test receipt dated 2027 — and the briefing, the baking plan and the dashboard showed
  "revenue ₽90, 1 receipt", while the self-monitoring said nothing. Future dates are no longer taken
  as "the last day", and the bot warns about such receipts.
- **The demo-data auto-reset could erase a client's real data.** Deletion happened before the file
  was parsed: an unrelated CSV in the auto-load folder wiped the database, and only then did the
  error surface. On top of that, staff write-offs entered by hand — the only data people actually
  type in — were erased too. Clearing now happens only after a successful parse, and manual
  write-offs are preserved (tagged by source).
- **A refund marked negative only in the "Total" column** was counted as a sale (with a "Price"
  column present, the total was not read at all).

Regressions from earlier fixes:
- **A discount line inside a receipt spawned a phantom "refund receipt"** — halving the average
  ticket for any bakery with a loyalty programme. Only a documented refund now counts as a separate
  receipt.
- **Back-filling a receipt double-counted a line** when the price or quantity was revised (a
  cashier's correction turned ₽90 into ₽270).
- **The slice cache did not notice a back-fill** — the bot answered with stale figures for the rest
  of the day.
- **The weekday profile was built from a single observation.** One street fair on a Wednesday with a
  short history, and the plan for every subsequent Wednesday tripled (300 units against demand of
  129). At least three observations per weekday are now required, and the spread is capped within
  sensible bounds.

Security and operations:
- **The supplier had full access to the analytics** (revenue, audit, till). The bot now only sends
  them requests.
- **The log was not rotated** — around 1 MB a day, overflowing the disk next to the database by the
  third month; and the bot token was landing in the log. Rotation was enabled and service-level logs
  were quietened.
- **A live bot token shipped with the product** — removed from the settings template.
- Editing your own message crashed the handler.
- A typo in a chat_id silently locked the owner out — startup now aborts with a clear explanation.

## 3.1.0 — third audit: security and money-loss blockers closed

An independent review found defects that would have surfaced with the very first client. All were
fixed and covered by tests (28 tests).

Security:
- **The bot was open to any Telegram user.** An outsider could learn the bakery's revenue and enter
  a false write-off, corrupting the baking plan. Access is now limited to the owner, the staff and
  the supplier (by chat_id).
- **A web password containing Cyrillic letters did not work** (the owner could not log in and the
  server returned 500). Header parsing was rewritten with UTF-8 support.
- The API's service pages (/docs) were closed off; starting the web dashboard with no password now
  logs a warning.

Money and data:
- **Refunds were not recognised** when the header read "Receipt type"/"Document type" — the refund
  was added to revenue. On a ₽3,000 receipt the error was ×18.
- **A refund against an already-loaded receipt was silently lost** (revenue stayed overstated) while
  the report said the refund had been accounted for.
- **A partial export followed by a full one** double-counted lines. A receipt is now back-filled
  with the missing lines.
- **cp1251 encoding** (1C and some tills) crashed the import — the file went to quarantine and the
  owner heard nothing. The encoding is now detected automatically.
- **Half the range was ending up in "Other"** and dropping out of the baking plan (vatrushki,
  rogaliki, plyushki, kulichi, tarts, biscuits, cheesecakes…). The category dictionary was extended
  to fit a real bakery.
- **"Sausage roll" counted as a service item** (it matched on "test" inside the Russian word) and
  vanished from the plan — fixed.
- **One old write-off record inflated every batch by 10% forever**: write-offs are now checked over
  the same window as sales.
- **The baking plan was built on the demo generator's coefficients.** The weekday profile is now
  computed from the real data of the specific bakery.
- **Re-running install.sh could wipe a production database** — the script now looks at the actual
  path from the settings and does not touch a database with data in it.
- **Demo data was not being displaced** by real data: the first load of real receipts now clears the
  demo, and `/status` warns if demo figures are being shown.
- Writing off an unrecognised product no longer records junk — the bot asks for clarification.

Reliability:
- **The bot froze for up to a minute** on every free-form question: the call to the model blocked
  message handling. Moved to a separate thread.
- Backups: a failed copy is deleted and does not occupy a rotation slot, names do not collide, and
  backup status is visible in `/status` and reaches the bot's warnings.
- install.sh also installs the venv module (on a clean Ubuntu the installation failed) and warns
  about HTTPS for a production server.
- Negative amounts in parentheses, hour ranges in the settings, and a warning when the payment
  column is missing.

## 3.0.0 — ready for production use

Work aimed at what will inevitably happen with real data and on handover to a client.

Correctness on real exports:
- **Refunds** (an "Operation type" column reading Refund, or negative numbers) are now subtracted
  from revenue rather than inflating it.
- **Service items from the till** — bags, discounts, delivery, gift cards, packaging — are
  recognised and do NOT reach the baking plan. Previously the system would have suggested "bake 40
  carrier bags".
- Only bread and pastry reach the baking plan; drinks and everything else, never.
- The importer reports how many refunds and service items were accounted for.

Data safety:
- **Automatic backups** of the database every night (via SQLite's own mechanism, safe under active
  writes), keeping the last 14 with rotation. Commands: `python -m coffeeos backup` and `backups`.

Security:
- **A password on the web dashboard** (WEB_USER/WEB_PASSWORD). Without it, anyone who knew the
  server address could see the bakery's revenue. The password is generated at install time.

Installation and operation:
- **An installation script, `install.sh`**: installs the dependencies, creates the environment,
  prepares .env, writes an absolute path to the database, generates the web password, creates
  systemd services for 24/7 startup, and prints the remaining steps.
- Self-monitoring computes time in the bakery's time zone rather than the server's (otherwise a
  server running UTC would raise false alerts).
- The bot gained a cancel for the supplier request ("cancel") — previously there was no way out of
  that mode.
- A comment on a settings line in .env no longer ends up inside the value.

## 2.8.0 — optimisation for a large catalogue

On a real till catalogue (hundreds of items) answers took seconds. Fixed:

- **Baking plan: 2,456 ms → 10 ms** (on 400 items). Previously each product meant its own database
  queries; the whole catalogue is now computed in two.
- **The data slice is cached** until new receipts or write-offs appear in the database, and is no
  longer recomputed several times for one question.
- **The slice is not computed at all** if the question is obviously not about analytics.
- **Short commands answer instantly.** "Run an audit" used to wait on the model (2.6 s); it is now
  computed locally in 27 ms. Open-ended questions ("why did revenue fall", "should I raise the
  price") still go to the AI.

Measurements on a catalogue of 1,000 items / 27,000 receipts / 54,000 receipt lines: baking plan
21 ms, any question 0–45 ms, dashboard /api/summary 211 ms.

## 2.7.0 — second full audit: critical fixes

An independent code review found blockers that would have distorted real data. All were fixed and
covered by tests (19 tests).

Critical:
- **The receipts loader was rewritten.** Previously only the FIRST line of a multi-line receipt was
  imported (the rest were treated as duplicates) — silently losing up to 65% of revenue. Lines are
  now grouped by receipt correctly.
- **Deduplication by (date + receipt number).** Previously, tills that number receipts from 1 each
  day lost the whole of the following day. With no receipt number, deduplication falls back to
  content, so re-loading a file no longer double-counts revenue.
- **An empty receipt number** no longer crashes the insert or loses receipts.
- **Separate "Date" + "Time" columns** are now recognised (previously 0 rows were silently imported
  and the file moved to the archive as "loaded").
- **The baking plan was overstated by ~48%** because of incorrect normalisation of the weekday
  coefficient — fixed, deviation now 0%. "Undersell" is only determined where write-offs are
  genuinely recorded.
- **Questions about write-offs wrote junk into the database** ("show write-offs for 3 days" →
  a record reading "for — 3 units"), and did so through a GET request to the web API as well.
  Recording now happens only on a strict command and only from Telegram; the web layer is read-only.

Reliability:
- SQLite was moved to WAL mode with a timeout — the bot, the auto-loader and the web dashboard no
  longer block each other ("database is locked").
- Auto-loading: malformed files go to a `_failed` quarantine (previously one bad file stopped the
  queue permanently), a file still being written is deferred, files with the same name do not
  overwrite the archive, and network/JSON errors are handled.
- Auto-loading was moved to its own thread — it no longer hangs the bot.
- Totals from an API are no longer taken as a unit price (revenue is not multiplied by qty).
- Sensible date parsing: ISO with milliseconds and time zone, dd.mm.yyyy, dd/mm/yyyy.
- Numbers like "1 234,50" and "1,234.50" are parsed correctly.
- Malformed values in .env no longer crash startup; validate() checks the auto-load settings and a
  relative DB_PATH.
- "Purchasing" no longer intercepts the question "how much did we spend on purchasing".

## 2.6.0 — automatic receipt loading

- Auto-loading was added: the system fetches new receipts on a schedule with no manual commands.
  Modes: `folder` (picks up CSVs from a folder) and `http` (fetches from the till's or fiscal
  operator's API). Configured in .env (SYNC_MODE and others).
- Duplicate protection: every receipt is tagged with an external id, so re-loading double-counts
  nothing. Old databases are migrated automatically.
- The bot starts the background auto-load task itself, 24/7.
- The `python -m coffeeos sync` command runs one cycle manually.
- IMPORTANT: `http` mode requires API access to the bakery's till or fiscal operator — you have to
  obtain it from the owner and enter the URL and token (field formats adapt to the till).

## 2.5.0 — full audit and bug fixes

Found by an independent code review and fixed:
- Receipt import no longer crashes: the order of clearing under --reset was fixed, along with
  protection against short rows and non-numeric cells (malformed rows are skipped).
- "Card" payments no longer end up counted as cash on import.
- Baking plan: the false "+N" with a minus on weak weekdays was removed — the "demand rising" tag
  now appears only on a genuine increase in the batch.
- Average sales and write-offs are averaged over working days rather than always over 14.
- The web server creates its own tables (it no longer crashes if only the web dashboard is started).
- Category shares passed to the AI are computed over the whole period rather than one day.
- "Greedy" keywords were cleaned up (average ticket over 2 weeks, what time do you open, this week's
  new items, analytics, print). Markup was removed from the chat on the website.
- A clear error on an invalid time format. The caption "data from the database".

## 2.4.0 — a proper live web dashboard

- The live web dashboard (`python -m coffeeos web`, port 8000) now looks the same as the
  presentation mockup but runs on real data from the database: briefing, metrics with sparklines,
  hourly and weekly charts, top items, baking plan, write-offs, till, categories — all pulled from
  the receipts.
- The chat assistant on the dashboard is wired to the bot (the /api/ask endpoint) — it answers
  free-form questions with the same answers as Telegram.
- The data on the dashboard and in the bot is guaranteed to match (one database).

## 2.3.0 — a swappable AI engine: free and Russia-friendly options

- The model engine is now swappable: it works with any OpenAI-compatible model.
- You can plug in a FREE local model via Ollama right on the server — the data never leaves the
  server (Federal Law 152-FZ), there is nothing to pay, and it works from Russia.
- The free tiers of Groq/OpenRouter are supported too (your own base_url and key).
- Settings: LLM_BASE_URL, LLM_MODEL, LLM_API_KEY (falling back to OPENAI_* for compatibility).
- A reminder: the common questions and every button work with no AI at all, free of charge.

## 2.2.0 — a smart assistant that answers almost any question

- The orchestrator became genuinely smart. It used to understand only prepared phrases; the owner
  can now ask almost anything about their business in plain language and get an answer from their
  own data.
- Two levels: (1) common analytical questions (best weekday, peak hour, top products, average
  ticket, how the week is going, how things are) the bot computes itself — instantly, free and
  offline; (2) anything unusual goes to the AI, which is handed a compact slice of all the bakery's
  data (by weekday, hour, product, category, write-offs, trend, baking plan) — so the model answers
  with exact numbers.
- Buttons and commands remain instant, exact calculations.
- The dumb "I didn't quite understand" fallback was removed — replaced by a hint with examples.
- Errors calling the AI are now written to the log (to help diagnose the key).

## 2.1.0 — supplier orders by hand, without stock accounting

- Ingredient stock accounting, reorder points and technical recipe cards were removed entirely — a
  small bakery cannot maintain them correctly, and they only created an illusion of control.
- The "What to buy" button now works the way a person would: the owner writes the list by hand, the
  bot shows the request and sends it to the supplier (or prepares it for forwarding if the supplier
  is not on Telegram).
- A new SUPPLIER_CHAT_ID setting — if set, the bot sends the request to the supplier itself; if
  empty, the owner forwards the finished list.
- The "stock …" command, the stock table and the request basket were removed from the web and
  presentation dashboards. Baking plan, sales, write-offs and till are unchanged.

## 2.0.1 — fixes and final polish

- Fixed an error on the "Baking plan" and "What to buy" buttons: Telegram was rejecting the markup
  because of special characters. The problematic characters were removed.
- The bot no longer crashes on markup: if Telegram rejects the formatting, the reply goes out as
  plain text (a double safeguard, in the chat and in the morning briefing).
- The /start greeting is sent as plain text (so the ID is always visible).
- A timeout on the OpenAI request (12 s) — a free-form question will not hang the bot.
- Cosmetics in the baking plan; markup balance was checked across every reply, along with an empty
  database, CSV import, the web API and the routing of every button.

## 2.0.0 — the production version

Bringing it up to production quality (without a bakery's real data):

- **Self-monitoring health checks.** Every 6 hours the bot checks how fresh the data is and writes
  to the owner itself if the till export has not arrived or the database is empty. The `/status`
  command and the `/api/health` endpoint.
- **An evening write-off reminder.** At a configured time (CLOSE_TIME) the bot asks the salesperson
  to record what went unsold — making the baking plan more accurate.
- **Setting ingredient stock from the bot:** "stock flour 40" — the purchasing request works with no
  manual database access.
- **Weekly results** (`/nedelya`), added automatically to the Friday briefing.
- **A command menu** in Telegram: /svodka, /plan, /zakupki, /nedelya, /status, /help.
- **Error handling and logging** (the coffeeos.log file) — the bot does not crash, it writes an
  intelligible reply to the user and records the error.
- **Settings validation** at startup, with clear hints.
- **A test suite** (`python -m pytest`) — 8 checks of the core.
- Robustness against empty data, and correct Cyrillic matching.

## 1.0.0 — the first working engine

- A Telegram bot, a database, agents (sales, baking, purchasing, write-offs, shifts, till), an
  automatic morning briefing, a receipts loader for till/fiscal-operator exports, and a web
  dashboard on live data.
