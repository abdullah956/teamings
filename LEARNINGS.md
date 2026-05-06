# What I learned building this

TODO: Append entries as the project grows. Suggested format per entry:
date, what surprised me, what I'd do differently, links to commits.

## Why temperature=0 for evaluation

What I thought before: temperature is a creativity knob.

What I understand now: for evaluation, temperature=0 is mandatory. 
Same attack must give same response across runs, otherwise pass 
rates are noise. The tradeoff: I'm only testing the model's most 
likely failure mode, not its full distribution. A more rigorous 
v2 would sample N times per attack and report a failure rate.

Source: openai_target.py in this repo.