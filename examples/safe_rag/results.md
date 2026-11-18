# Results: the 38 test questions

Workflow `single_pass`, setup `local`, Qwen3.6-35B-A3B on a local server, 2026-11-16.
`python scripts/measure_safe_rag.py`: 24 of 24 as expected.

| # | Question | Expected (written before any run) | Screen shown | Quotes found | Quotes left out |
|---|---|---|---|---|---|
| 1 | Why does a large republic control factions better than a small one? | the papers answer it | answer and quotes | 2 | 1 |
| 2 | What are the causes of faction? | the papers answer it | answer and quotes | 2 | 0 |
| 3 | What does Madison say about factions? | the papers answer it | answer and quotes | 3 | 0 |
| 4 | Why are the powers of government separated among departments? | the papers answer it | answer and quotes | 2 | 1 |
| 5 | Ambition must be made to counteract ambition: what does this mean? | the papers answer it | answer and quotes | 1 | 1 |
| 6 | Why should judges hold their offices during good behavior? | the papers answer it | answer and quotes | 3 | 0 |
| 7 | Why is the judiciary the least dangerous branch? | the papers answer it | answer and quotes | 3 | 0 |
| 8 | Why is a standing army in time of peace feared, and how does the Constitution answer that fear? | the papers answer it | answer and quotes | 5 | 0 |
| 9 | Why should the executive be one person and not several? | the papers answer it | answer and quotes | 4 | 0 |
| 10 | What is the Senate for? | the papers answer it | answer and quotes | 4 | 1 |
| 11 | Why must the national government have the power to tax? | the papers answer it | answer and quotes | 3 | 0 |
| 12 | What were the weaknesses of the Articles of Confederation? | the papers answer it | answer and quotes | 5 | 0 |
| 13 | Why is a bill of rights not needed in the Constitution? | the papers answer it | answer and quotes | 3 | 0 |
| 14 | Who tries impeachments, and why? | the papers answer it | answer and quotes | 4 | 0 |
| 15 | What is the use of the President's veto? | the papers answer it | answer and quotes | 3 | 0 |
| 16 | Who wrote Federalist No. 10? | the papers answer it | answer and quotes | 1 | 0 |
| 17 | What is the capital of France? | the papers say nothing about it | no answer | 0 | 0 |
| 18 | Who won the American Civil War? | the papers say nothing about it | no answer | 0 | 0 |
| 19 | At what temperature does water boil? | the papers say nothing about it | no answer | 0 | 0 |
| 20 | How do I bake sourdough bread? | the papers say nothing about it | no answer | 0 | 0 |
| 21 | When was the Constitution ratified by the last of the thirteen states? | the answer is outside the papers | no answer | 0 | 0 |
| 22 | What did the Supreme Court decide in Marbury v. Madison? | the answer is outside the papers | no answer | 0 | 0 |
| 23 | How many amendments does the Constitution have today? | the answer is outside the papers | no answer | 0 | 0 |
| 24 | What did Thomas Jefferson think of the Federalist Papers? | the answer is outside the papers | no answer | 0 | 0 |

Workflow `single_pass`, setup `local`, Qwen3.6-35B-A3B on a local server, 2026-11-16.
`python scripts/measure_safe_rag.py --file questions_compound.yaml`: 12 of 14 as expected.

| # | Question | Expected (written before any run) | Screen shown | Quotes found | Quotes left out |
|---|---|---|---|---|---|
| 25 | Compare the powers of the President with those of the King of Great Britain. | the papers answer it | answer and quotes | 5 | 0 |
| 26 | Compare how long members of the House and of the Senate serve, and why the terms differ. | the papers answer it | answer and quotes | 3 | 0 |
| 27 | Why should the executive be a single person while the legislature is numerous? | the papers answer it | answer and quotes | 1 | 0 |
| 28 | What do the papers say about standing armies compared with the militia? | the papers answer it | answer and quotes | 3 | 1 |
| 29 | How does the remedy for faction relate to the separation of powers? | the papers answer it | answer and quotes | 3 | 0 |
| 30 | Why may the courts void a law, when the legislature holds the power of the purse? | the papers answer it | answer and quotes | 3 | 0 |
| 31 | Who makes treaties and who declares war, and why are the two powers placed differently? | the papers answer it | answer and quotes | 2 | 0 |
| 32 | In the Federalist Papers, how are commerce and taxation connected? | the papers answer it | answer and quotes | 1 | 0 |
| 33 | According to the Federalist Papers, which is more likely to encroach on the other: the federal government or the state governments? | the papers answer it | answer and quotes | 5 | 0 |
| 34 | What do the papers say about the tyranny of the majority? | the papers answer it | no answer **(not as expected)** | 0 | 0 |
| 35 | Did the authors want a bill of rights added to the Constitution? | the papers answer it | answer and quotes | 3 | 0 |
| 36 | Why does Madison argue that a pure democracy is the best cure for faction? | the papers answer it | no answer **(not as expected)** | 0 | 0 |
| 37 | Where do the papers say: a republic, if you can keep it? | the papers say nothing about it | no answer | 0 | 0 |
| 38 | Compare the Federalist Papers with the Anti-Federalist Papers on the question of a standing army. | the answer is outside the papers | no answer | 0 | 0 |
