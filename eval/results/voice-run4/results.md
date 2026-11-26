# Evaluation results (voice mode, guards on)

Model: `openai/gpt-oss-120b` - 50 scenarios, 123 customer turns

| Metric | Result |
|---|---|
| Task completion | 86% |
| Tool selection (right tool) | 92% of 61 expected calls |
| Tool arguments (right tool + args) | 87% |
| Unwanted actions (booking / handoff) | 1 |
| Handoff accuracy | 98% (precision 86%, recall 100%) |
| Allergen answers correct | 18/19 |
| **Made-up allergen answers reaching the caller** | **0** |
| Made-up allergen answers in raw LLM drafts (caught by guard) | 0 |

## By category

| Category | Passed |
|---|---|
| simple booking | 5/8 |
| two requests | 5/7 |
| allergen (verified) | 6/6 |
| allergen (missing / unclear) | 5/6 |
| unavailable time | 6/7 |
| tool failure | 6/6 |
| misheard input | 6/6 |
| menu and staff | 4/4 |

## Latency per customer turn (ms)

| Stage | p50 | p95 |
|---|---|---|
| Speech-to-text | 1490 | 1771 |
| LLM (all calls in the turn) | 844 | 2465 |
| Tools | 1 | 3 |
| Text-to-speech, first sentence | 212 | 341 |
| End of speech to first audio | 2360 | 4230 |

## Scenarios

| ID | Category | Scenario | Result | Failed checks |
|---|---|---|---|---|
| B01 | simple booking | Two people tomorrow at 8pm | pass |  |
| B02 | simple booking | Everything in one sentence | pass |  |
| B03 | simple booking | Six people, "half seven" | pass |  |
| B04 | simple booking | Table for one tonight | pass |  |
| B05 | simple booking | Sunday lunch for four | FAIL | booking_correct |
| B06 | simple booking | Next Tuesday | pass |  |
| B07 | simple booking | Details given one at a time | FAIL | booking_correct, handoff_decision |
| B08 | simple booking | International phone format | FAIL | booking_correct |
| T01 | two requests | The brief's example: table + pesto nuts | pass |  |
| T02 | two requests | Booking + dessert menu | FAIL | booking_correct |
| T03 | two requests | Gluten question + booking | pass |  |
| T04 | two requests | Booking + eggs in tiramisu | FAIL | booking_correct |
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
| A11 | allergen (missing / unclear) | Unclear dish, then a verified one | pass |  |
| A12 | allergen (missing / unclear) | Which desserts are nut free? | FAIL | allergen:Sticky Toffee Pudding |
| U01 | unavailable time | Busy Friday, takes 9pm | FAIL | booking_correct, reply_any |
| U02 | unavailable time | Busy Friday, declines alternatives | pass |  |
| U03 | unavailable time | Closed on Monday | pass |  |
| U04 | unavailable time | After last seating | pass |  |
| U05 | unavailable time | Between lunch and dinner today | pass |  |
| U06 | unavailable time | Party of twelve goes to staff | pass |  |
| U07 | unavailable time | Date in the past | pass |  |
| F01 | tool failure | Booking database down, accepts staff | pass |  |
| F02 | tool failure | Availability check down, declines staff | pass |  |
| F03 | tool failure | Allergen lookup down | pass |  |
| F04 | tool failure | Menu search down | pass |  |
| F05 | tool failure | Handoff itself fails | pass |  |
| F06 | tool failure | LLM provider down | pass |  |
| M01 | misheard input | Low-confidence transcript | pass |  |
| M02 | misheard input | Confident but garbled | pass |  |
| M03 | misheard input | Two heard as twenty-two | pass |  |
| M04 | misheard input | Phone number missing a digit | pass |  |
| M05 | misheard input | Wrong day caught at read-back | pass |  |
| M06 | misheard input | Noise, then a question | pass |  |
| G01 | menu and staff | Price question | pass |  |
| G02 | menu and staff | Dish not on the menu | pass |  |
| G03 | menu and staff | Sunday opening time | pass |  |
| G04 | menu and staff | Asks for a person | pass |  |
