# Evaluation results (voice mode, guards on)

Model: `openai/gpt-oss-120b` - 29 scenarios, 70 customer turns

| Metric | Result |
|---|---|
| Task completion | 83% |
| Tool selection (right tool) | 90% of 41 expected calls |
| Tool arguments (right tool + args) | 88% |
| Unwanted actions (booking / handoff) | 0 |
| Handoff accuracy | 100% (precision 100%, recall 100%) |
| Allergen answers correct | 17/18 |
| **Made-up allergen answers reaching the caller** | **0** |
| Made-up allergen answers in raw LLM drafts (caught by guard) | 1 |

## By category

| Category | Passed |
|---|---|
| simple booking | 5/8 |
| two requests | 6/7 |
| allergen (verified) | 6/6 |
| allergen (missing / unclear) | 5/6 |
| unavailable time | 2/2 |

## Latency per customer turn (ms)

| Stage | p50 | p95 |
|---|---|---|
| Speech-to-text | 1493 | 1668 |
| LLM (all calls in the turn) | 1016 | 3326 |
| Tools | 1 | 4 |
| Text-to-speech, first sentence | 238 | 675 |
| End of speech to first audio | 2730 | 4715 |

## Scenarios

| ID | Category | Scenario | Result | Failed checks |
|---|---|---|---|---|
| B01 | simple booking | Two people tomorrow at 8pm | pass |  |
| B02 | simple booking | Everything in one sentence | pass |  |
| B03 | simple booking | Six people, "half seven" | FAIL | booking_correct |
| B04 | simple booking | Table for one tonight | FAIL | booking_correct |
| B05 | simple booking | Sunday lunch for four | pass |  |
| B06 | simple booking | Next Tuesday | pass |  |
| B07 | simple booking | Details given one at a time | pass |  |
| B08 | simple booking | International phone format | FAIL | booking_correct |
| T01 | two requests | The brief's example: table + pesto nuts | pass |  |
| T02 | two requests | Booking + dessert menu | FAIL | reply_has:tiramisu, reply_has:sorbet |
| T03 | two requests | Gluten question + booking | pass |  |
| T04 | two requests | Booking + eggs in tiramisu | pass |  |
| T05 | two requests | Menu search + Monday opening | pass |  |
| T06 | two requests | Busy time + unconfirmed allergen | pass |  |
| T07 | two requests | Booking + shellfish in calamari | pass |  |
| A01 | allergen (verified) | Pesto and nuts | pass |  |
| A02 | allergen (verified) | Burrata and a nut allergy | pass |  |
| A03 | allergen (verified) | Sesame in the hummus | pass |  |
| A04 | allergen (verified) | Coeliac asking about sea bass | pass |  |
| A05 | allergen (verified) | Dairy in the lemon sorbet | pass |  |
| A06 | allergen (verified) | Egg in the chicken Milanese | pass |  |
| A07 | allergen (missing / unclear) | Soup of the day, missing record | pass |  |
| A08 | allergen (missing / unclear) | Lamb tagine, unclear record | pass |  |
| A09 | allergen (missing / unclear) | Pudding + nuts, accepts staff | pass |  |
| A10 | allergen (missing / unclear) | All allergens in the soup, accepts staff | pass |  |
| A11 | allergen (missing / unclear) | Unclear dish, then a verified one | FAIL | allergen:Lamb Tagine |
| A12 | allergen (missing / unclear) | Which desserts are nut free? | pass |  |
| U01 | unavailable time | Busy Friday, takes 9pm | pass |  |
| U02 | unavailable time | Busy Friday, declines alternatives | pass |  |
