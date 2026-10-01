# Science and calculation model

The bot separates deterministic calculations from LLM-generated language.

## 1. Energy requirement

Initial maintenance calories use the 2023 Dietary Reference Intakes for Energy (DRI/EER) equations from the National Academies.

The equations use:

- sex;
- age;
- height;
- current weight;
- physical activity category.

There are separate coefficient sets for ages 3-18 and ages 19+.

Primary source:

- National Academies of Sciences, Engineering, and Medicine. *Dietary Reference Intakes for Energy* (2023).
  https://www.ncbi.nlm.nih.gov/books/NBK588659/
- TEE/EER equation coefficients:
  https://www.ncbi.nlm.nih.gov/books/n/nap26818/pdf/

The result is an estimate, not measured metabolism. The DRI report recommends monitoring weight over time and adjusting energy intake as needed.

## 2. Weight-loss target

For users age 19+ with a weight-loss goal, the product uses a moderate starting heuristic:

- 15% of estimated maintenance;
- minimum 250 kcal/day;
- maximum 500 kcal/day.

This is a product default, not a medical prescription.

For users age 18 or younger, the bot does not automatically prescribe a calorie deficit or surplus. It uses estimated maintenance as the diary reference and applies stricter LLM guardrails.

## 3. Protein

For ages 14-18, the reference starts from the DRI RDA of 0.85 g/kg/day. For a weight-loss goal, target weight is used as the planning basis when it is below current weight.

Reference:

- Dietary Reference Intakes protein RDA table:
  https://www.ncbi.nlm.nih.gov/books/NBK208874/

For adults, the bot currently uses a practical food-planning target rather than a medical requirement. This target is independent of the calorie engine.

## 4. Children and adolescents

Adult BMI categories should not be applied directly to children and teens. CDC interprets BMI for ages 2-19 using sex-specific BMI-for-age percentiles.

References:

- https://www.cdc.gov/bmi/child-teen-calculator/index.html
- https://www.cdc.gov/bmi/child-teen-calculator/bmi-categories.html

The current MVP does not calculate BMI-for-age percentile because accurate percentile calculation benefits from age in months. If this is added later, date/month of birth should be collected rather than estimating from integer age alone.

## 5. Activity and daily expenditure

Registration activity is used to choose the starting DRI EER equation.

Daily expenditure is shown separately as an estimate based on:

- inactive/sedentary DRI EER baseline;
- steps above a small ordinary-living baseline;
- workout type and duration.

Steps are not treated as a precise calorie meter.

For future calibration, adult step-count categories can be informed by the Tudor-Locke literature, but the bot should avoid blindly mapping adult step thresholds onto adolescents.

References:

- Tudor-Locke & Bassett (2004): https://pubmed.ncbi.nlm.nih.gov/14715035/
- Children/adolescents review: https://pubmed.ncbi.nlm.nih.gov/21798014/

## 6. LLM responsibilities

Gemini/OpenAI may:

- parse food descriptions;
- estimate meal composition with uncertainty;
- explain numbers already calculated by the deterministic engine;
- recommend recipes within the provided remaining calories/protein;
- use age/goal context to avoid inappropriate suggestions.

The LLM may not:

- invent a new calorie target;
- override maintenance calories;
- change the protein target;
- encourage extreme deficits;
- change food calorie estimates merely because the user wants to lose weight.

The shared system instruction is in `app/ai_service.py`.

## 7. Adaptation over time

When a new weight is logged:

1. current weight is updated;
2. estimated maintenance is recalculated;
3. protein target is recalculated;
4. an automatically generated calorie target is recalculated;
5. a manually overridden calorie target is preserved.

A later iteration should calibrate activity estimates from sufficient multi-day data rather than one day of steps.
